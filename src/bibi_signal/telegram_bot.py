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
        "Signal-only for stocks. Auto for crypto (if enabled).\n\n"
        "*Trading*\n"
        "`/buy <ticker> <usd> <price>` — record a buy\n"
        "`/sell <ticker> <lot_id> <price>` — record a sell of a specific lot\n"
        "`/cash <usd>` — set free cash balance\n"
        "`/status` — portfolio + open lots\n"
        "`/history [n]` — last n closed trades\n\n"
        "*Income*\n"
        "`/dividends` — yields & upcoming ex-dates for SCHD/JEPI/JEPQ\n"
        "`/divreceived <ticker> <usd> [date]` — record a dividend you got\n\n"
        "*Strategy*\n"
        "`/rules [ticker]` — show strategy parameters\n"
        "`/set <ticker> <param> <value>` — tune a parameter (in-memory only)\n\n"
        "*Simulation & crypto*\n"
        "`/paper` — shadow paper portfolio (compare vs LIVE)\n"
        "`/crypto` — crypto holdings & quotes (Robinhood Crypto API)\n\n"
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


# ---------- /paper ----------

@_auth
async def cmd_paper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from . import paper_engine
    from .price_fetcher import get_price, PriceUnavailable

    strategy = _strategy(context)
    with _session(context) as session:
        opens = db.open_lots(session, Environment.PAPER)
        prices: dict[str, float] = {}
        for lot in opens:
            try:
                prices[lot.ticker] = get_price(lot.ticker).price
            except PriceUnavailable:
                pass
        s = paper_engine.paper_summary(session, prices)

    live_cash = float(0)
    with _session(context) as session:
        live_cash = float(db.get_cash(session, LIVE))

    text = (
        "*📊 Paper portfolio* (shadow simulation)\n"
        f"Cash:           ${s['cash']:.2f}\n"
        f"Open lots:      {s['open_lots']}\n"
        f"Open value:     ${s['open_value']:.2f}\n"
        f"Total equity:   ${s['total_equity']:.2f}\n"
        f"Realised P&L:   ${s['realised_pnl']:+.2f}\n"
        f"Unrealised P&L: ${s['unrealised_pnl']:+.2f}\n"
        f"Closed trades:  {s['closed_trades']}  win-rate {s['win_rate']*100:.1f}%\n\n"
        f"_LIVE cash for comparison: ${live_cash:.2f}_"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------- /dividends ----------

@_auth
async def cmd_dividends(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .dividend import fetch_dividend_info, total_dividends_received
    from .price_fetcher import get_price, PriceUnavailable

    strategy = _strategy(context)
    if not strategy.dividends.enabled or not strategy.dividends.tickers:
        await update.message.reply_text("Dividend tracker is disabled.")
        return

    blocks: list[str] = []
    for sym in strategy.dividends.tickers:
        try:
            price = get_price(sym).price
        except PriceUnavailable:
            blocks.append(f"*{sym}* — price unavailable")
            continue
        info = fetch_dividend_info(sym, price)
        if info is None:
            blocks.append(f"*{sym}* — no dividend history")
            continue
        ex_str = (
            f"ex-date {info.next_ex_date.isoformat()} (in {info.next_ex_in_days}d)"
            if info.next_ex_date else "no upcoming ex-date"
        )
        blocks.append(
            f"*{sym}*  ${price:.2f}\n"
            f"  Last div ${info.last_div_amount:.4f} on {info.last_div_date}\n"
            f"  TTM ${info.ttm_total:.4f}  yield {info.annual_yield_pct:.2f}%\n"
            f"  {ex_str}"
        )

    with _session(context) as session:
        total = total_dividends_received(session, Environment.LIVE)
    blocks.append(f"\n*Total dividends received (LIVE):* ${float(total):.2f}")

    await update.message.reply_text("\n\n".join(blocks), parse_mode=ParseMode.MARKDOWN)


# ---------- /divreceived ----------

@_auth
async def cmd_divreceived(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User reports a dividend they actually got from Robinhood."""
    from datetime import date as _date
    from .dividend import record_received_dividend

    if len(context.args) not in (2, 3):
        await update.message.reply_text("Usage: /divreceived <ticker> <usd> [YYYY-MM-DD]")
        return
    try:
        ticker = context.args[0].upper()
        amount = Decimal(context.args[1])
        pay_date = _date.fromisoformat(context.args[2]) if len(context.args) == 3 else _date.today()
    except (InvalidOperation, ValueError) as e:
        await update.message.reply_text(f"Bad input: {e}")
        return

    with _session(context) as session:
        record_received_dividend(session, ticker, amount, pay_date)
        session.commit()
        new_cash = db.get_cash(session, LIVE)

    await update.message.reply_text(
        f"💰 Dividend recorded: ${amount:.2f} from {ticker} on {pay_date}.\n"
        f"Cash now ${float(new_cash):.2f}."
    )


# ---------- /crypto ----------

@_auth
async def cmd_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .robinhood_crypto import RobinhoodCryptoClient, RobinhoodCryptoError, RobinhoodCryptoNotConfigured

    settings: AppSettings = context.application.bot_data["settings"]
    strategy = _strategy(context)
    if not strategy.crypto.enabled:
        await update.message.reply_text("Crypto module is disabled in config.yaml.")
        return

    try:
        client = RobinhoodCryptoClient(settings)
    except RobinhoodCryptoNotConfigured:
        await update.message.reply_text(
            "Robinhood Crypto API keys are not set. See .env.example."
        )
        return

    lines = [f"*Crypto* (auto-execute: {'ON' if strategy.crypto.auto_execute else 'OFF'})"]
    try:
        bp = client.buying_power_usd()
        lines.append(f"Buying power: ${bp:.2f}")
        for sym in strategy.crypto.tickers:
            if not strategy.crypto.tickers[sym].enabled:
                continue
            try:
                q = client.best_bid_ask(sym)
                lines.append(f"{sym}: bid ${q.bid:.2f}  ask ${q.ask:.2f}")
            except RobinhoodCryptoError as e:
                lines.append(f"{sym}: quote error — {e}")
        holdings = client.get_holdings()
        if holdings:
            lines.append("\n*Holdings:*")
            for h in holdings:
                lines.append(f"  {h.symbol}  qty {h.quantity:.6f}")
    finally:
        client.close()

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


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
    app.add_handler(CommandHandler("paper", cmd_paper))
    app.add_handler(CommandHandler("dividends", cmd_dividends))
    app.add_handler(CommandHandler("divreceived", cmd_divreceived))
    app.add_handler(CommandHandler("crypto", cmd_crypto))
    return app


async def broadcast(app: Application, text: str) -> None:
    """Send a message to every allowed chat. Used by the scheduler."""
    settings: AppSettings = app.bot_data["settings"]
    for chat_id in settings.allowed_chat_ids:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.error("telegram_send_failed", chat_id=chat_id, error=str(e))
