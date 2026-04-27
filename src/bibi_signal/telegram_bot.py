"""Telegram bot — user interface for recording trades and reading state.

The bot itself does NOT place trades. It records what the user did manually
in the Robinhood app, and pushes signals coming out of the scheduler.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Awaitable, Callable

import structlog
from sqlalchemy.orm import Session, sessionmaker
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from . import database as db
from .config import AppSettings, StrategyConfig
from .database import Environment, Lot, LotStatus, Trade, TradeSide
from .strategy import compute_targets

log = structlog.get_logger(__name__)

LIVE = Environment.LIVE


def _auth(func: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        settings: AppSettings = context.application.bot_data["settings"]
        chat_id = update.effective_chat.id if update.effective_chat else None
        if settings.allowed_chat_ids and chat_id not in settings.allowed_chat_ids:
            log.warning("unauthorized_chat", chat_id=chat_id)
            if update.message:
                await update.message.reply_text("Unauthorized.")
            return
        await func(update, context)

    return wrapper


def _session(context: ContextTypes.DEFAULT_TYPE) -> Session:
    factory: sessionmaker = context.application.bot_data["session_factory"]
    return factory()


def _strategy(context: ContextTypes.DEFAULT_TYPE) -> StrategyConfig:
    return context.application.bot_data["strategy"]


# ---------- /help ----------

@_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*Bibi-Signal — Ladder Swing Bot*\n\n"
        "Signal-only. I tell you what to do; you tap in Robinhood.\n\n"
        "*Commands*\n"
        "`/buy <ticker> <usd> <price>` — record a buy\n"
        "`/sell <ticker> <lot_id> <price>` — record a sell of a specific lot\n"
        "`/cash <usd>` — set free cash balance\n"
        "`/status` — portfolio + open lots\n"
        "`/rules [ticker]` — show strategy parameters\n"
        "`/set <ticker> <param> <value>` — tune a parameter (in-memory only)\n"
        "`/history [n]` — last n closed trades\n"
        "`/help` — this message\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------- /buy ----------

@_auth
async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /buy <ticker> <usd> <price>")
        return
    try:
        ticker = context.args[0].upper()
        usd = Decimal(context.args[1])
        price = Decimal(context.args[2])
    except InvalidOperation:
        await update.message.reply_text("Could not parse numbers.")
        return

    strategy = _strategy(context)
    if ticker not in strategy.tickers:
        await update.message.reply_text(f"{ticker} not in config.yaml.")
        return
    cfg = strategy.tickers[ticker]
    target, stop = compute_targets(float(price), cfg)
    qty = usd / price

    with _session(context) as session:
        cash = db.get_cash(session, LIVE)
        if cash < usd:
            await update.message.reply_text(
                f"Free cash ${cash:.2f} < buy ${usd:.2f}. Update with /cash."
            )
            return
        lot = Lot(
            environment=LIVE,
            ticker=ticker,
            buy_price=price,
            quantity=qty,
            cost_basis=usd,
            target_price=Decimal(str(target)),
            stop_price=Decimal(str(stop)),
        )
        session.add(lot)
        session.flush()
        session.add(
            Trade(
                environment=LIVE,
                lot_id=lot.id,
                ticker=ticker,
                side=TradeSide.BUY,
                price=price,
                quantity=qty,
                notional=usd,
            )
        )
        db.adjust_cash(session, LIVE, -usd)
        session.commit()
        await update.message.reply_text(
            f"BUY recorded: lot #{lot.id} {ticker} qty={qty:.4f} @ ${price:.2f}\n"
            f"target ${target:.2f}  stop ${stop:.2f}  cash now ${db.get_cash(session, LIVE):.2f}"
        )


# ---------- /sell ----------

@_auth
async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /sell <ticker> <lot_id> <price>")
        return
    try:
        ticker = context.args[0].upper()
        lot_id = int(context.args[1])
        price = Decimal(context.args[2])
    except (ValueError, InvalidOperation):
        await update.message.reply_text("Could not parse arguments.")
        return

    with _session(context) as session:
        lot = session.get(Lot, lot_id)
        if not lot or lot.environment != LIVE:
            await update.message.reply_text(f"Lot #{lot_id} not found.")
            return
        if lot.ticker != ticker:
            await update.message.reply_text(f"Lot #{lot_id} is {lot.ticker}, not {ticker}.")
            return
        if lot.status != LotStatus.OPEN:
            await update.message.reply_text(f"Lot #{lot_id} already closed.")
            return

        proceeds = price * lot.quantity
        pnl = (price - lot.buy_price) * lot.quantity
        lot.sell_price = price
        lot.realised_pnl = pnl
        lot.status = LotStatus.CLOSED
        from datetime import datetime, timezone

        lot.closed_at = datetime.now(timezone.utc)
        session.add(
            Trade(
                environment=LIVE,
                lot_id=lot.id,
                ticker=ticker,
                side=TradeSide.SELL,
                price=price,
                quantity=lot.quantity,
                notional=proceeds,
            )
        )
        db.adjust_cash(session, LIVE, proceeds)
        session.commit()
        roi = (pnl / lot.cost_basis) * 100 if lot.cost_basis else Decimal("0")
        await update.message.reply_text(
            f"SELL recorded: lot #{lot.id} {ticker} @ ${price:.2f}\n"
            f"P&L ${pnl:.2f} ({roi:.2f}%)  cash now ${db.get_cash(session, LIVE):.2f}"
        )


# ---------- /cash ----------

@_auth
async def cmd_cash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /cash <usd>")
        return
    try:
        amount = Decimal(context.args[0])
    except InvalidOperation:
        await update.message.reply_text("Bad number.")
        return
    with _session(context) as session:
        db.set_cash(session, LIVE, amount)
        session.commit()
        await update.message.reply_text(f"Free cash set to ${amount:.2f}.")


# ---------- /status ----------

@_auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with _session(context) as session:
        cash = db.get_cash(session, LIVE)
        opens = db.open_lots(session, LIVE)

        lines = [f"*Free cash:* ${cash:.2f}", f"*Open lots:* {len(opens)}"]
        for lot in opens:
            lines.append(
                f"#{lot.id} {lot.ticker}  qty={float(lot.quantity):.4f}  "
                f"buy ${float(lot.buy_price):.2f}  "
                f"target ${float(lot.target_price):.2f}  "
                f"stop ${float(lot.stop_price):.2f}"
            )
        if not opens:
            lines.append("_no open positions_")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------- /rules ----------

@_auth
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    strategy = _strategy(context)
    args = context.args
    tickers = (
        [args[0].upper()] if args and args[0].upper() in strategy.tickers else list(strategy.tickers)
    )
    blocks = []
    for t in tickers:
        cfg = strategy.tickers[t]
        blocks.append(
            f"*{t}*\n"
            f"  dip {cfg.dip_percent*100:.2f}%  profit {cfg.profit_percent*100:.2f}%  "
            f"stop {cfg.stop_loss_percent*100:.2f}%\n"
            f"  size ${cfg.min_trade_usd:.0f}–${cfg.max_trade_usd:.0f}  "
            f"max lots {cfg.max_open_lots}\n"
            f"  trend filter {'on' if cfg.require_uptrend else 'off'}, "
            f"RSI<{cfg.rsi_threshold} {'on' if cfg.require_rsi_oversold else 'off'}"
        )
    await update.message.reply_text("\n\n".join(blocks), parse_mode=ParseMode.MARKDOWN)


# ---------- /set ----------

@_auth
async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /set <ticker> <param> <value>")
        return
    ticker, param, value = context.args
    ticker = ticker.upper()
    strategy = _strategy(context)
    if ticker not in strategy.tickers:
        await update.message.reply_text(f"{ticker} not in config.")
        return
    cfg = strategy.tickers[ticker]
    if not hasattr(cfg, param):
        await update.message.reply_text(f"Unknown param '{param}'.")
        return
    try:
        current = getattr(cfg, param)
        coerced = type(current)(value) if not isinstance(current, bool) else value.lower() in ("1", "true", "yes")
        setattr(cfg, param, coerced)
    except Exception as e:
        await update.message.reply_text(f"Bad value: {e}")
        return
    await update.message.reply_text(
        f"{ticker}.{param} = {coerced}  (in-memory; edit config.yaml to persist)"
    )


# ---------- /history ----------

@_auth
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = 10
    if context.args:
        try:
            n = max(1, min(50, int(context.args[0])))
        except ValueError:
            pass
    with _session(context) as session:
        lots = db.closed_lots(session, LIVE, limit=n)
        if not lots:
            await update.message.reply_text("No closed trades yet.")
            return
        total = sum(float(l.realised_pnl or 0) for l in lots)
        wins = sum(1 for l in lots if (l.realised_pnl or 0) > 0)
        lines = [f"Last {len(lots)}: P&L ${total:.2f}  win-rate {wins}/{len(lots)}"]
        for l in lots:
            roi = (float(l.realised_pnl or 0) / float(l.cost_basis)) * 100 if l.cost_basis else 0
            lines.append(
                f"#{l.id} {l.ticker}  ${float(l.buy_price):.2f}→${float(l.sell_price):.2f}  "
                f"P&L ${float(l.realised_pnl or 0):+.2f} ({roi:+.2f}%)"
            )
        await update.message.reply_text("\n".join(lines))


# ---------- application factory ----------

def build_application(
    settings: AppSettings,
    strategy: StrategyConfig,
    session_factory: sessionmaker,
) -> Application:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    app.bot_data["settings"] = settings
    app.bot_data["strategy"] = strategy
    app.bot_data["session_factory"] = session_factory

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("cash", cmd_cash))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("history", cmd_history))
    return app


async def broadcast(app: Application, text: str) -> None:
    """Send a message to every allowed chat. Used by the scheduler."""
    settings: AppSettings = app.bot_data["settings"]
    for chat_id in settings.allowed_chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.error("telegram_send_failed", chat_id=chat_id, error=str(e))
