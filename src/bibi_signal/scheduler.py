"""Periodic price polling + signal dispatch.

The scheduler is the only component that calls evaluate() on live data.
It writes signals to the DB (with idempotency on fingerprint) and
broadcasts new ones via Telegram.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import sessionmaker
from telegram.ext import Application

from . import database as db
from .config import StrategyConfig
from .database import Environment, Signal, SignalKind as DBSignalKind
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


def _format_signal(p: SignalProposal) -> str:
    if p.kind == SignalKind.BUY:
        return (
            f"🟢 *BUY {p.ticker}* @ ${p.price:.2f}\n"
            f"Suggested: ${p.suggested_usd:.2f}\n"
            f"_{p.reason}_"
        )
    if p.kind == SignalKind.SELL:
        return (
            f"🔵 *SELL {p.ticker}* lot #{p.lot_id} @ ${p.price:.2f}\n"
            f"_{p.reason}_"
        )
    if p.kind == SignalKind.STOP:
        return (
            f"🔴 *STOP {p.ticker}* lot #{p.lot_id} @ ${p.price:.2f}\n"
            f"_{p.reason}_"
        )
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


async def tick(
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


def start(
    strategy: StrategyConfig,
    session_factory: sessionmaker,
    app: Application,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        tick,
        trigger="interval",
        minutes=strategy.check_frequency_minutes,
        kwargs={"strategy": strategy, "session_factory": session_factory, "app": app},
        next_run_time=None,  # don't fire immediately on start
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("scheduler_started", every_minutes=strategy.check_frequency_minutes)
    return scheduler
