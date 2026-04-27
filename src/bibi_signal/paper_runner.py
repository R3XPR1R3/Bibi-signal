"""Standalone paper-mode runner.

Doesn't depend on Telegram. Uses APScheduler to tick the strategy on a
configurable interval, executes signals into the PAPER environment, and
prints everything to stdout.

Usage:
    bibi-signal --mode paper           # continuous, every check_frequency_minutes
    bibi-signal --mode paper --once    # one tick and exit (good for smoke test)
    bibi-signal --mode paper --no-market-hours  # ignore US market hours (test on weekends)
"""
from __future__ import annotations

import asyncio
import signal as _signal
from decimal import Decimal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import sessionmaker

from . import database as db
from . import paper_engine
from .config import StrategyConfig
from .database import Environment
from .indicators import latest_indicators
from .price_fetcher import PriceUnavailable, get_history, get_price, is_market_open
from .strategy import MarketSnapshot, SignalKind

log = structlog.get_logger("paper")
PAPER = Environment.PAPER


def _format_signal_console(p) -> str:
    if p.kind == SignalKind.BUY:
        return f"  🟢 PAPER BUY {p.ticker} @ ${p.price:.2f}  size ${p.suggested_usd:.2f}  ({p.reason})"
    if p.kind == SignalKind.SELL:
        return f"  🔵 PAPER SELL {p.ticker} lot #{p.lot_id} @ ${p.price:.2f}  ({p.reason})"
    if p.kind == SignalKind.STOP:
        return f"  🔴 PAPER STOP {p.ticker} lot #{p.lot_id} @ ${p.price:.2f}  ({p.reason})"
    return f"  · PAPER {p.kind.value} {p.ticker}  ({p.reason})"


def print_summary(session_factory: sessionmaker, current_prices: dict[str, float]) -> None:
    with session_factory() as session:
        s = paper_engine.paper_summary(session, current_prices)
        opens = db.open_lots(session, PAPER)

    print("\n────────── PAPER PORTFOLIO ──────────")
    print(f"  Cash:           ${s['cash']:.2f}")
    print(f"  Open lots:      {s['open_lots']}")
    print(f"  Open value:     ${s['open_value']:.2f}")
    print(f"  Total equity:   ${s['total_equity']:.2f}")
    print(f"  Realised P&L:   ${s['realised_pnl']:+.2f}")
    print(f"  Unrealised P&L: ${s['unrealised_pnl']:+.2f}")
    print(f"  Closed trades:  {s['closed_trades']}  win-rate {s['win_rate']*100:.1f}%")
    if opens:
        print("  Open positions:")
        for lot in opens:
            cur = current_prices.get(lot.ticker, 0)
            pnl = (cur - float(lot.buy_price)) * float(lot.quantity) if cur else 0
            print(
                f"    #{lot.id} {lot.ticker}  qty={float(lot.quantity):.4f}  "
                f"buy ${float(lot.buy_price):.2f}  cur ${cur:.2f}  "
                f"pnl ${pnl:+.2f}  target ${float(lot.target_price):.2f}"
            )
    print("─────────────────────────────────────\n")


async def tick_paper(
    strategy: StrategyConfig,
    session_factory: sessionmaker,
    *,
    ignore_market_hours: bool,
) -> None:
    if strategy.respect_market_hours and not ignore_market_hours and not is_market_open():
        log.debug("market_closed_skip")
        return

    current_prices: dict[str, float] = {}

    for ticker, cfg in strategy.tickers.items():
        if not cfg.enabled:
            continue
        try:
            df = get_history(ticker, period="1y", interval="1d")
        except PriceUnavailable as e:
            log.warning("history_unavailable", ticker=ticker, error=str(e))
            continue
        if len(df) < cfg.sma_long_period:
            log.info("warmup", ticker=ticker, bars=len(df), need=cfg.sma_long_period)
            continue

        ind = latest_indicators(df, cfg.sma_long_period)
        # Use latest live quote when market is open (more accurate trigger);
        # fall back to last bar close.
        try:
            live_price = get_price(ticker).price if is_market_open() else ind["close"]
        except PriceUnavailable:
            live_price = ind["close"]
        current_prices[ticker] = live_price

        market = MarketSnapshot(
            ticker=ticker,
            price=live_price,
            sma_long=ind["sma_long"],
            rsi=ind["rsi"],
            atr=ind["atr"],
            in_uptrend=live_price > ind["sma_long"],
        )

        with session_factory() as session:
            applied = paper_engine.evaluate_paper(session, cfg, market)
            session.commit()

        if not applied:
            log.info("hold", ticker=ticker, price=round(live_price, 2),
                     rsi=round(ind["rsi"], 1) if ind["rsi"] else None)
        for p in applied:
            print(_format_signal_console(p))
            log.info("paper_signal", kind=p.kind.value, ticker=p.ticker,
                     price=p.price, reason=p.reason)

    print_summary(session_factory, current_prices)


async def run(
    strategy: StrategyConfig,
    session_factory: sessionmaker,
    *,
    once: bool,
    ignore_market_hours: bool,
) -> None:
    # Seed paper cash from config if first run.
    with session_factory() as session:
        paper_engine.ensure_paper_seeded(
            session, Decimal(str(strategy.paper.starting_cash))
        )
        session.commit()

    print(f"\n[paper] starting with ${strategy.paper.starting_cash:.2f} virtual capital")
    print(f"[paper] tickers: {list(strategy.tickers)}")
    print(f"[paper] interval: {strategy.check_frequency_minutes} min "
          f"(ignore_market_hours={ignore_market_hours})\n")

    if once:
        await tick_paper(strategy, session_factory, ignore_market_hours=ignore_market_hours)
        return

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        tick_paper,
        trigger="interval",
        minutes=strategy.check_frequency_minutes,
        kwargs={
            "strategy": strategy,
            "session_factory": session_factory,
            "ignore_market_hours": ignore_market_hours,
        },
        max_instances=1,
        coalesce=True,
        next_run_time=None,  # APScheduler runs the first job after one interval; we kick once now:
    )
    scheduler.start()
    # Kick off an immediate tick so the user sees output right away.
    await tick_paper(strategy, session_factory, ignore_market_hours=ignore_market_hours)

    # Wait for SIGINT/SIGTERM.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows fallback
            pass

    print("[paper] running — Ctrl+C to stop")
    await stop_event.wait()
    scheduler.shutdown(wait=False)
    print("\n[paper] stopped")
