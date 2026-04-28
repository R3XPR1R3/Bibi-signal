"""Entry point with three modes: backtest, paper, live.

    bibi-signal --mode backtest --ticker QQQ --years 5
    bibi-signal --mode paper [--once] [--no-market-hours]
    bibi-signal --mode live          # default

Backtest is one-shot historical replay. Paper runs continuously on live
prices into the PAPER environment (no Telegram needed). Live runs the
full bot with Telegram + scheduler + paper-mirroring.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

import structlog

from . import database as db
from . import paper_engine, paper_runner
from .config import load_all
from .database import Environment, init_db


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(message)s",
        stream=sys.stdout,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )


def _run_backtest(args: argparse.Namespace) -> int:
    from .backtest import run_backtest
    from .config import StrategyConfig

    strategy = StrategyConfig.from_yaml(args.config)
    if args.ticker not in strategy.tickers:
        print(f"error: {args.ticker} not in config.yaml tickers: {list(strategy.tickers)}",
              file=sys.stderr)
        return 2
    starting = args.starting_cash if args.starting_cash else strategy.starting_cash
    result = run_backtest(
        args.ticker,
        strategy.tickers[args.ticker],
        starting,
        period=f"{args.years}y",
    )
    print(result.summary())
    return 0


def _run_optimize(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .config import StrategyConfig
    from .optimize import maybe_apply, optimize_ticker

    strategy = StrategyConfig.from_yaml(args.config)
    if args.ticker not in strategy.tickers:
        print(f"error: {args.ticker} not in config.yaml tickers: {list(strategy.tickers)}",
              file=sys.stderr)
        return 2
    starting = args.starting_cash if args.starting_cash else strategy.starting_cash
    report = optimize_ticker(
        args.ticker,
        strategy.tickers[args.ticker],
        starting,
        period=f"{args.years}y",
        workers=args.workers,
        top_k=max(args.top, 30),
        train_frac=args.train_frac,
    )
    print(report.render(top=args.top))
    maybe_apply(report, Path(args.config), args.apply, interactive=sys.stdin.isatty())
    return 0


def _run_paper(args: argparse.Namespace) -> int:
    from .price_fetcher import set_price_source
    settings, strategy = load_all()
    _configure_logging(settings.log_level)
    set_price_source(strategy.price_source)
    session_factory = init_db(settings.database_url)
    asyncio.run(
        paper_runner.run(
            strategy,
            session_factory,
            once=args.once,
            ignore_market_hours=args.no_market_hours,
        )
    )
    return 0


def _run_live(args: argparse.Namespace) -> int:
    from .price_fetcher import set_price_source
    from .scheduler import start as start_scheduler
    from .telegram_bot import build_application

    settings, strategy = load_all()
    _configure_logging(settings.log_level)
    set_price_source(strategy.price_source)
    log = structlog.get_logger("main")

    session_factory = init_db(settings.database_url)
    log.info("db_ready", url=settings.database_url)

    with session_factory() as session:
        if db.get_cash(session, Environment.LIVE) == 0:
            db.set_cash(session, Environment.LIVE, Decimal(str(strategy.starting_cash)))
            session.commit()
            log.info("cash_seeded", amount=strategy.starting_cash)
        if strategy.paper.enabled:
            paper_engine.ensure_paper_seeded(
                session, Decimal(str(strategy.paper.starting_cash))
            )
            session.commit()

    if not settings.telegram_bot_token:
        print(
            "error: live mode needs TELEGRAM_BOT_TOKEN in .env\n"
            "       run --mode paper to test without Telegram",
            file=sys.stderr,
        )
        return 2

    app = build_application(settings, strategy, session_factory)
    scheduler = start_scheduler(strategy, settings, session_factory, app)
    app.bot_data["scheduler"] = scheduler

    log.info("bot_running", tickers=list(strategy.tickers))
    app.run_polling()
    return 0


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="bibi-signal",
        description="Ladder Swing signal bot — three modes: backtest, paper, live.",
    )
    parser.add_argument(
        "--mode",
        choices=("backtest", "optimize", "paper", "live"),
        default="live",
        help="execution mode (default: live)",
    )
    parser.add_argument("--config", default="config.yaml", help="path to YAML strategy config")

    # backtest-only
    parser.add_argument("--ticker", help="[backtest] ticker symbol")
    parser.add_argument("--years", type=int, default=5, help="[backtest] history window in years")
    parser.add_argument("--starting-cash", type=float, default=None,
                        help="[backtest/optimize] override starting capital")

    # optimize-only
    parser.add_argument("--workers", type=int, default=None,
                        help="[optimize] parallel worker count (default: cpu_count-1)")
    parser.add_argument("--top", type=int, default=10,
                        help="[optimize] how many top combos to print (default: 10)")
    parser.add_argument("--train-frac", type=float, default=0.7,
                        help="[optimize] fraction of history used for training (default: 0.7)")
    parser.add_argument("--apply", type=int, default=None,
                        help="[optimize] auto-apply rank N (1-based) to config.yaml; "
                             "if omitted and TTY, you'll be prompted")

    # paper-only
    parser.add_argument("--once", action="store_true",
                        help="[paper] run a single tick and exit")
    parser.add_argument("--no-market-hours", action="store_true",
                        help="[paper] ignore US market hours (useful on weekends)")

    args = parser.parse_args()

    if args.mode == "backtest":
        if not args.ticker:
            parser.error("--mode backtest requires --ticker")
        sys.exit(_run_backtest(args))
    elif args.mode == "optimize":
        if not args.ticker:
            parser.error("--mode optimize requires --ticker")
        sys.exit(_run_optimize(args))
    elif args.mode == "paper":
        sys.exit(_run_paper(args))
    elif args.mode == "live":
        sys.exit(_run_live(args))


if __name__ == "__main__":
    run()
