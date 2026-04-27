"""Crypto engine — reuses strategy.py against Robinhood Crypto API.

Two modes:
    - signal-only: same as stocks, just sends Telegram alert; user trades manually.
    - auto_execute: bot calls submit_market_order() directly. Allowed because
      Robinhood Crypto's trading API is OFFICIAL, no ToS issue.

Pricing for indicators (RSI/SMA/ATR) still comes from yfinance daily history
(ticker like "BTC-USD"). Live spot price for the trigger comes from Robinhood
best_bid_ask, so signals fire on the actual price you'd transact at.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

import structlog
from sqlalchemy.orm import Session

from . import database as db
from .config import AppSettings, CryptoConfig, CryptoTickerConfig, TickerConfig
from .database import Environment, Lot, LotStatus, Trade, TradeSide
from .indicators import latest_indicators
from .price_fetcher import PriceUnavailable, get_history
from .robinhood_crypto import (
    RobinhoodCryptoClient,
    RobinhoodCryptoError,
    RobinhoodCryptoNotConfigured,
)
from .strategy import (
    LotSnapshot,
    MarketSnapshot,
    SignalKind,
    SignalProposal,
    compute_targets,
    evaluate,
)

log = structlog.get_logger(__name__)
LIVE = Environment.LIVE


def _to_ticker_config(c: CryptoTickerConfig) -> TickerConfig:
    """Adapt CryptoTickerConfig -> TickerConfig so we can reuse evaluate()."""
    return TickerConfig(
        enabled=c.enabled,
        dip_percent=c.dip_percent,
        profit_percent=c.profit_percent,
        stop_loss_percent=c.stop_loss_percent,
        min_trade_usd=c.min_trade_usd,
        max_trade_usd=c.max_trade_usd,
        max_open_lots=c.max_open_lots,
        use_atr_sizing=False,
        atr_k=1.5,
        require_rsi_oversold=c.require_rsi_oversold,
        rsi_threshold=c.rsi_threshold,
        require_uptrend=c.require_uptrend,
        sma_long_period=c.sma_long_period,
    )


def _live_spot(client: Optional[RobinhoodCryptoClient], symbol: str, fallback_price: float) -> float:
    """Use Robinhood spot when client available; otherwise yfinance close."""
    if client is None:
        return fallback_price
    try:
        return client.best_bid_ask(symbol).mid or fallback_price
    except RobinhoodCryptoError as e:
        log.warning("rh_quote_failed", symbol=symbol, error=str(e))
        return fallback_price


def evaluate_crypto_ticker(
    symbol: str,
    cfg: CryptoTickerConfig,
    session: Session,
    client: Optional[RobinhoodCryptoClient],
    auto_execute: bool,
) -> list[SignalProposal]:
    if not cfg.enabled:
        return []

    try:
        df = get_history(symbol, period="6mo", interval="1d")
    except PriceUnavailable as e:
        log.warning("crypto_history_unavailable", symbol=symbol, error=str(e))
        return []
    if len(df) < cfg.sma_long_period:
        log.info("crypto_warmup", symbol=symbol, bars=len(df), need=cfg.sma_long_period)
        return []

    ind = latest_indicators(df, cfg.sma_long_period)
    spot = _live_spot(client, symbol, ind["close"])

    market = MarketSnapshot(
        ticker=symbol,
        price=spot,
        sma_long=ind["sma_long"],
        rsi=ind["rsi"],
        atr=ind["atr"],
        in_uptrend=spot > ind["sma_long"],
    )

    opens = db.open_lots(session, LIVE, symbol)
    free_cash = float(db.get_cash(session, LIVE))
    snapshots = [
        LotSnapshot(
            id=l.id,
            ticker=l.ticker,
            buy_price=float(l.buy_price),
            quantity=float(l.quantity),
            target_price=float(l.target_price),
            stop_price=float(l.stop_price),
        )
        for l in opens
    ]
    proposals = evaluate(_to_ticker_config(cfg), market, snapshots, free_cash)

    actionable = [p for p in proposals if p.kind != SignalKind.HOLD]
    if not auto_execute or client is None:
        return actionable

    # Auto-execute mode.
    for p in actionable:
        try:
            _execute(p, cfg, session, client)
        except RobinhoodCryptoError as e:
            log.error("crypto_execute_failed", symbol=symbol, kind=p.kind.value, error=str(e))
    return actionable


def _execute(
    p: SignalProposal,
    cfg: CryptoTickerConfig,
    session: Session,
    client: RobinhoodCryptoClient,
) -> None:
    if p.kind == SignalKind.BUY and p.suggested_usd:
        order = client.submit_market_order(p.ticker, side="buy", usd_amount=p.suggested_usd)
        # Best-effort fill price; actual fill may differ slightly.
        fill_price = Decimal(str(p.price))
        size = Decimal(str(p.suggested_usd))
        qty = size / fill_price
        target, stop = compute_targets(p.price, _to_ticker_config(cfg))
        lot = Lot(
            environment=LIVE,
            ticker=p.ticker,
            buy_price=fill_price,
            quantity=qty,
            cost_basis=size,
            target_price=Decimal(str(target)),
            stop_price=Decimal(str(stop)),
        )
        session.add(lot)
        session.flush()
        session.add(
            Trade(
                environment=LIVE,
                lot_id=lot.id,
                ticker=p.ticker,
                side=TradeSide.BUY,
                price=fill_price,
                quantity=qty,
                notional=size,
            )
        )
        db.adjust_cash(session, LIVE, -size)
        log.info("crypto_buy_executed", symbol=p.ticker, order_id=order.order_id, usd=float(size))

    elif p.kind in (SignalKind.SELL, SignalKind.STOP) and p.lot_id:
        lot = session.get(Lot, p.lot_id)
        if not lot or lot.status != LotStatus.OPEN:
            return
        order = client.submit_market_order(p.ticker, side="sell", quantity=float(lot.quantity))
        fill_price = Decimal(str(p.price))
        proceeds = fill_price * lot.quantity
        pnl = (fill_price - lot.buy_price) * lot.quantity
        lot.sell_price = fill_price
        lot.realised_pnl = pnl
        lot.status = LotStatus.CLOSED
        from datetime import datetime, timezone
        lot.closed_at = datetime.now(timezone.utc)
        session.add(
            Trade(
                environment=LIVE,
                lot_id=lot.id,
                ticker=p.ticker,
                side=TradeSide.SELL,
                price=fill_price,
                quantity=lot.quantity,
                notional=proceeds,
            )
        )
        db.adjust_cash(session, LIVE, proceeds)
        log.info("crypto_sell_executed", symbol=p.ticker, order_id=order.order_id,
                 pnl=float(pnl))


def make_client(settings: AppSettings) -> Optional[RobinhoodCryptoClient]:
    try:
        return RobinhoodCryptoClient(settings)
    except RobinhoodCryptoNotConfigured:
        log.info("robinhood_crypto_not_configured")
        return None


def evaluate_all_crypto(
    crypto_cfg: CryptoConfig,
    settings: AppSettings,
    session: Session,
) -> list[tuple[str, list[SignalProposal]]]:
    if not crypto_cfg.enabled or not crypto_cfg.tickers:
        return []
    client = make_client(settings)
    out: list[tuple[str, list[SignalProposal]]] = []
    try:
        for symbol, tcfg in crypto_cfg.tickers.items():
            if not tcfg.enabled:
                continue
            proposals = evaluate_crypto_ticker(
                symbol, tcfg, session, client, crypto_cfg.auto_execute
            )
            if proposals:
                out.append((symbol, proposals))
    finally:
        if client is not None:
            client.close()
    return out
