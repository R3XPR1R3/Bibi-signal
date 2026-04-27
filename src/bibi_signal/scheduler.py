"""Periodic price polling + signal dispatch.

Three independent jobs:
    1. tick_stocks   — every check_frequency_minutes during market hours
    2. tick_crypto   — every crypto.check_frequency_minutes (24/7)
    3. tick_dividends — daily at dividends.check_hour_utc

Every actionable LIVE stock signal is also mirrored into the PAPER engine
so we get an honest parallel P&L of the strategy itself.
"""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import sessionmaker
from telegram.ext import Application

from . import database as db
from . import paper_engine
from .config import AppSettings, StrategyConfig
from .crypto_engine import evaluate_all_crypto
from .database import Environment, Signal, SignalKind as DBSignalKind
from .dividend import evaluate_dividend, fetch_dividend_info
from .indicators import latest_indicators
from .price_fetcher import PriceUnavailable, get_history, is_market_open
from .strategy import (
    LotSnapshot,
    MarketSnapshot,
    SignalKind,
    SignalProposal,
    evaluate,
)
from .telegram_bot import broadcast

log = structlog.get_logger(__name__)
LIVE = Environment.LIVE


def _format_signal(p: SignalProposal, *, prefix: str = "") -> str:
    tag = f"{prefix} " if prefix else ""
    if p.kind == SignalKind.BUY:
        return (
            f"🟢 *{tag}BUY {p.ticker}* @ ${p.price:.2f}\n"
            f"Suggested: ${p.suggested_usd:.2f}\n"
            f"_{p.reason}_"
        )
    if p.kind == SignalKind.SELL:
        return f"🔵 *{tag}SELL {p.ticker}* lot #{p.lot_id} @ ${p.price:.2f}\n_{p.reason}_"
    if p.kind == SignalKind.STOP:
        return f"🔴 *{tag}STOP {p.ticker}* lot #{p.lot_id} @ ${p.price:.2f}\n_{p.reason}_"
    return f"{p.ticker} HOLD — {p.reason}"


async def evaluate_ticker(
    ticker: str,
    strategy: StrategyConfig,
    session_factory: sessionmaker,
    app: Application,
) -> None:
    cfg = strategy.tickers.get(ticker)
    if cfg is None or not cfg.enabled:
        return

    try:
        df = get_history(ticker, period="1y", interval="1d")
    except PriceUnavailable as e:
        log.warning("history_unavailable", ticker=ticker, error=str(e))
        return

    if len(df) < cfg.sma_long_period:
        log.info("warmup", ticker=ticker, bars=len(df), need=cfg.sma_long_period)
        return

    ind = latest_indicators(df, cfg.sma_long_period)
    market = MarketSnapshot(
        ticker=ticker,
        price=ind["close"],
        sma_long=ind["sma_long"],
        rsi=ind["rsi"],
        atr=ind["atr"],
        in_uptrend=ind["in_uptrend"],
    )

    with session_factory() as session:
        # Mirror to paper first (uses same market snapshot, executes against PAPER state).
        if strategy.paper.enabled and strategy.paper.mirror_to_paper:
            paper_applied = paper_engine.evaluate_paper(session, cfg, market)
            for p in paper_applied:
                log.info("paper_executed", kind=p.kind.value, ticker=p.ticker, price=p.price)

        # Now LIVE evaluation.
        opens = db.open_lots(session, LIVE, ticker)
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
        proposals = evaluate(cfg, market, snapshots, free_cash)

        new_proposals: list[SignalProposal] = []
        for p in proposals:
            if p.kind == SignalKind.HOLD:
                continue
            fp = p.fingerprint()
            if db.signal_already_sent(session, fp):
                continue
            session.add(
                Signal(
                    environment=LIVE,
                    ticker=p.ticker,
                    kind=DBSignalKind(p.kind.value),
                    lot_id=p.lot_id,
                    price=Decimal(str(p.price)),
                    suggested_usd=Decimal(str(p.suggested_usd)) if p.suggested_usd else None,
                    fingerprint=fp,
                )
            )
            new_proposals.append(p)
        session.commit()

    for p in new_proposals:
        log.info("signal", kind=p.kind.value, ticker=p.ticker, price=p.price, reason=p.reason)
        await broadcast(app, _format_signal(p))


async def tick_stocks(
    strategy: StrategyConfig,
    session_factory: sessionmaker,
    app: Application,
) -> None:
    if strategy.respect_market_hours and not is_market_open():
        log.debug("market_closed_skip")
        return
    await asyncio.gather(
        *(evaluate_ticker(t, strategy, session_factory, app) for t in strategy.tickers)
    )


async def tick_crypto(
    strategy: StrategyConfig,
    settings: AppSettings,
    session_factory: sessionmaker,
    app: Application,
) -> None:
    if not strategy.crypto.enabled or not strategy.crypto.tickers:
        return
    with session_factory() as session:
        results = evaluate_all_crypto(strategy.crypto, settings, session)
        new: list[SignalProposal] = []
        for symbol, proposals in results:
            for p in proposals:
                fp = "crypto:" + p.fingerprint()
                if db.signal_already_sent(session, fp):
                    continue
                session.add(
                    Signal(
                        environment=LIVE,
                        ticker=p.ticker,
                        kind=DBSignalKind(p.kind.value),
                        lot_id=p.lot_id,
                        price=Decimal(str(p.price)),
                        suggested_usd=Decimal(str(p.suggested_usd)) if p.suggested_usd else None,
                        fingerprint=fp,
                    )
                )
                new.append(p)
        session.commit()

    for p in new:
        prefix = "AUTO" if strategy.crypto.auto_execute else "CRYPTO"
        await broadcast(app, _format_signal(p, prefix=prefix))


async def tick_dividends(
    strategy: StrategyConfig,
    session_factory: sessionmaker,
    app: Application,
) -> None:
    if not strategy.dividends.enabled or not strategy.dividends.tickers:
        return

    with session_factory() as session:
        free_cash = float(db.get_cash(session, LIVE))
        for symbol, dcfg in strategy.dividends.tickers.items():
            if not dcfg.enabled:
                continue
            try:
                df = get_history(symbol, period="3mo", interval="1d")
            except PriceUnavailable:
                continue
            current_price = float(df["Close"].iloc[-1])
            recent_avg = float(df["Close"].tail(20).mean())

            info = fetch_dividend_info(symbol, current_price)
            if info is None:
                continue
            sig = evaluate_dividend(dcfg, info, free_cash, recent_avg)
            if sig is None:
                continue

            fp = (
                f"div:{symbol}:{info.next_ex_date.isoformat() if info.next_ex_date else 'na'}"
                f":{round(current_price, 2)}"
            )
            if db.signal_already_sent(session, fp):
                continue
            session.add(
                Signal(
                    environment=LIVE,
                    ticker=symbol,
                    kind=DBSignalKind.BUY,
                    lot_id=None,
                    price=Decimal(str(current_price)),
                    suggested_usd=Decimal(str(sig.suggested_usd)),
                    fingerprint=fp,
                )
            )
            session.commit()
            await broadcast(
                app,
                f"💰 *DIV-BUY {symbol}* @ ${current_price:.2f}\n"
                f"Suggested: ${sig.suggested_usd:.2f}\n"
                f"_{sig.reason}_",
            )


def start(
    strategy: StrategyConfig,
    settings: AppSettings,
    session_factory: sessionmaker,
    app: Application,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        tick_stocks,
        trigger="interval",
        minutes=strategy.check_frequency_minutes,
        kwargs={"strategy": strategy, "session_factory": session_factory, "app": app},
        max_instances=1,
        coalesce=True,
        id="stocks",
    )
    log.info("scheduled_stocks", every_minutes=strategy.check_frequency_minutes)

    if strategy.crypto.enabled:
        scheduler.add_job(
            tick_crypto,
            trigger="interval",
            minutes=strategy.crypto.check_frequency_minutes,
            kwargs={
                "strategy": strategy,
                "settings": settings,
                "session_factory": session_factory,
                "app": app,
            },
            max_instances=1,
            coalesce=True,
            id="crypto",
        )
        log.info("scheduled_crypto", every_minutes=strategy.crypto.check_frequency_minutes,
                 auto=strategy.crypto.auto_execute)

    if strategy.dividends.enabled:
        scheduler.add_job(
            tick_dividends,
            trigger="cron",
            hour=strategy.dividends.check_hour_utc,
            minute=0,
            kwargs={"strategy": strategy, "session_factory": session_factory, "app": app},
            max_instances=1,
            coalesce=True,
            id="dividends",
        )
        log.info("scheduled_dividends", at_hour_utc=strategy.dividends.check_hour_utc)

    scheduler.start()
    return scheduler
