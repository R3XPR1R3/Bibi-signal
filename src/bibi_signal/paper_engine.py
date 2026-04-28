"""Paper-trading engine — fully internal simulation using live prices.

Why this exists:
    The bot's main job is to suggest LIVE trades to the user. But manual
    execution introduces slippage and timing noise — so we don't really
    know if the strategy itself is good or if the user is just lucky.

    This module runs a parallel "shadow" portfolio in the same SQLite DB
    under environment=PAPER. Every signal evaluated against the LIVE state
    is simultaneously *executed at the live price* in PAPER state with no
    user interaction. After a few weeks you compare:

        - PAPER P&L  -> what the strategy would have earned
        - LIVE P&L   -> what you actually earned

    Big gap = your manual execution is hurting you (delay/slippage).
    PAPER negative = the strategy itself is bad — fix it before scaling.

No external broker needed. No Alpaca, no Robinhood API, no creds — just
yfinance + our DB.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.orm import Session

from . import database as db
from .config import StrategyConfig, TickerConfig
from .database import Environment, Lot, LotStatus, Trade, TradeSide
from .strategy import (
    LotSnapshot,
    MarketSnapshot,
    SignalKind,
    SignalProposal,
    compute_targets,
    evaluate,
    trail_updates,
)

log = structlog.get_logger(__name__)
PAPER = Environment.PAPER


def ensure_paper_seeded(session: Session, starting_cash: Decimal) -> None:
    """First-run only: seed paper cash so the simulation has working capital."""
    if db.get_cash(session, PAPER) == 0:
        db.set_cash(session, PAPER, starting_cash)


def _exec_buy(session: Session, p: SignalProposal, cfg: TickerConfig) -> Lot | None:
    cash = db.get_cash(session, PAPER)
    size = Decimal(str(p.suggested_usd or 0))
    if size <= 0 or cash < size:
        return None
    price = Decimal(str(p.price))
    qty = size / price
    target, stop = compute_targets(p.price, cfg)
    lot = Lot(
        environment=PAPER,
        ticker=p.ticker,
        buy_price=price,
        quantity=qty,
        cost_basis=size,
        target_price=Decimal(str(target)),
        stop_price=Decimal(str(stop)),
    )
    session.add(lot)
    session.flush()
    session.add(
        Trade(
            environment=PAPER,
            lot_id=lot.id,
            ticker=p.ticker,
            side=TradeSide.BUY,
            price=price,
            quantity=qty,
            notional=size,
        )
    )
    db.adjust_cash(session, PAPER, -size)
    return lot


def _exec_close(session: Session, p: SignalProposal) -> Lot | None:
    if p.lot_id is None:
        return None
    lot = session.get(Lot, p.lot_id)
    if not lot or lot.environment != PAPER or lot.status != LotStatus.OPEN:
        return None
    price = Decimal(str(p.price))
    proceeds = price * lot.quantity
    pnl = (price - lot.buy_price) * lot.quantity
    lot.sell_price = price
    lot.realised_pnl = pnl
    lot.status = LotStatus.CLOSED
    lot.closed_at = datetime.now(timezone.utc)
    session.add(
        Trade(
            environment=PAPER,
            lot_id=lot.id,
            ticker=p.ticker,
            side=TradeSide.SELL,
            price=price,
            quantity=lot.quantity,
            notional=proceeds,
        )
    )
    db.adjust_cash(session, PAPER, proceeds)
    return lot


def evaluate_paper(
    session: Session,
    cfg: TickerConfig,
    market: MarketSnapshot,
) -> list[SignalProposal]:
    """Same as scheduler.evaluate_ticker but executes against PAPER state.

    Returns the proposals that were applied so the caller can log them.
    """
    opens = db.open_lots(session, PAPER, market.ticker)
    free_cash = float(db.get_cash(session, PAPER))

    def _snap():
        return [
            LotSnapshot(
                id=l.id, ticker=l.ticker,
                buy_price=float(l.buy_price), quantity=float(l.quantity),
                target_price=float(l.target_price), stop_price=float(l.stop_price),
                peak_price=float(l.peak_price) if l.peak_price is not None else None,
                trail_active=bool(l.trail_active),
            )
            for l in opens
        ]

    snapshots = _snap()
    updates = trail_updates(cfg, snapshots, market.price)
    if updates:
        by_id = {l.id: l for l in opens}
        for upd in updates:
            row = by_id.get(upd.lot_id)
            if row is None:
                continue
            if upd.arm:
                row.trail_active = True
            if upd.new_peak_price is not None:
                row.peak_price = Decimal(str(upd.new_peak_price))
        session.flush()
        snapshots = _snap()
    proposals = evaluate(cfg, market, snapshots, free_cash)

    applied: list[SignalProposal] = []
    for p in proposals:
        if p.kind == SignalKind.BUY:
            if _exec_buy(session, p, cfg) is not None:
                applied.append(p)
        elif p.kind in (SignalKind.SELL, SignalKind.STOP):
            if _exec_close(session, p) is not None:
                applied.append(p)
        # HOLD: nothing to record in paper either.
    return applied


def paper_summary(session: Session, current_prices: dict[str, float]) -> dict:
    """Snapshot of paper portfolio for /paper command and reports."""
    cash = float(db.get_cash(session, PAPER))
    opens = db.open_lots(session, PAPER)
    closed = db.closed_lots(session, PAPER)

    open_value = 0.0
    unrealised = 0.0
    for lot in opens:
        last = current_prices.get(lot.ticker)
        if last is None:
            continue
        open_value += last * float(lot.quantity)
        unrealised += (last - float(lot.buy_price)) * float(lot.quantity)

    realised = sum(float(l.realised_pnl or 0) for l in closed)
    wins = sum(1 for l in closed if (l.realised_pnl or 0) > 0)
    losses = sum(1 for l in closed if (l.realised_pnl or 0) <= 0)
    total = wins + losses
    win_rate = (wins / total) if total else 0.0

    return {
        "cash": cash,
        "open_lots": len(opens),
        "open_value": open_value,
        "total_equity": cash + open_value,
        "realised_pnl": realised,
        "unrealised_pnl": unrealised,
        "closed_trades": total,
        "win_rate": win_rate,
    }


def initialize_for_strategy(session: Session, strategy: StrategyConfig) -> None:
    """Convenience helper: seed paper cash and commit if needed."""
    ensure_paper_seeded(session, Decimal(str(strategy.starting_cash)))
    session.commit()
