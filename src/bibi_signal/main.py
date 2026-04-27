"""Entry point: wire config + DB + Telegram + scheduler and run."""
from __future__ import annotations

import logging
import sys
from decimal import Decimal

import structlog

from . import database as db
from .config import load_all
from .database import Environment, init_db
from .scheduler import start as start_scheduler
from .telegram_bot import build_application


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


def run() -> None:
    settings, strategy = load_all()
    _configure_logging(settings.log_level)
    log = structlog.get_logger("main")

    session_factory = init_db(settings.database_url)
    log.info("db_ready", url=settings.database_url)

    # First-run: seed cash from config if no row exists yet.
    with session_factory() as session:
        if db.get_cash(session, Environment.LIVE) == 0:
            db.set_cash(session, Environment.LIVE, Decimal(str(strategy.starting_cash)))
            session.commit()
            log.info("cash_seeded", amount=strategy.starting_cash)

    app = build_application(settings, strategy, session_factory)
    scheduler = start_scheduler(strategy, session_factory, app)
    app.bot_data["scheduler"] = scheduler

    log.info("bot_running", tickers=list(strategy.tickers))
    app.run_polling()


if __name__ == "__main__":
    run()
