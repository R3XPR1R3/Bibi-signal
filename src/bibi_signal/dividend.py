"""Dividend tracking for income-focused ETFs (SCHD / JEPI / JEPQ / etc.).

Three jobs:
    1. Pull historical dividends from yfinance, project forward yield.
    2. Detect upcoming ex-dividend dates (window: next N days).
    3. Suggest "buy before ex-date" if the price is currently in a dip
       relative to its recent average — captures the dividend AND a discount.

To collect a dividend you must own the share at the *start of the
ex-dividend day*. Practically: you must buy by market close the day
before ex-date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import pandas as pd
import structlog
import yfinance as yf
from sqlalchemy.orm import Session

from . import database as db
from .config import DividendConfig, DividendTickerConfig
from .database import DividendEvent, Environment

log = structlog.get_logger(__name__)
LIVE = Environment.LIVE


@dataclass(frozen=True)
class DividendInfo:
    ticker: str
    last_div_amount: float
    last_div_date: date
    ttm_total: float          # trailing-twelve-month dividends per share
    annual_yield_pct: float   # ttm_total / current_price * 100
    next_ex_date: Optional[date]
    next_ex_in_days: Optional[int]
    current_price: float


def _safe_yf_dividends(ticker: str) -> pd.Series:
    try:
        s = yf.Ticker(ticker).dividends
        return s if s is not None else pd.Series(dtype=float)
    except Exception as e:
        log.warning("dividend_history_unavailable", ticker=ticker, error=str(e))
        return pd.Series(dtype=float)


def _safe_yf_calendar(ticker: str) -> dict:
    try:
        cal = yf.Ticker(ticker).calendar
        if isinstance(cal, dict):
            return cal
        if isinstance(cal, pd.DataFrame) and not cal.empty:
            return cal.to_dict()
        return {}
    except Exception as e:
        log.warning("dividend_calendar_unavailable", ticker=ticker, error=str(e))
        return {}


def _to_date(x) -> Optional[date]:
    if x is None:
        return None
    if isinstance(x, date) and not isinstance(x, datetime):
        return x
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, pd.Timestamp):
        return x.date()
    return None


def fetch_dividend_info(ticker: str, current_price: float) -> Optional[DividendInfo]:
    divs = _safe_yf_dividends(ticker)
    if divs.empty:
        return None

    last_amount = float(divs.iloc[-1])
    last_date = _to_date(divs.index[-1]) or date.today()

    cutoff = pd.Timestamp.now(tz=divs.index.tz) - pd.Timedelta(days=365)
    ttm = float(divs[divs.index >= cutoff].sum())
    yield_pct = (ttm / current_price * 100) if current_price > 0 else 0.0

    cal = _safe_yf_calendar(ticker)
    raw_ex = cal.get("Ex-Dividend Date") or cal.get("exDividendDate")
    if isinstance(raw_ex, list) and raw_ex:
        raw_ex = raw_ex[0]
    next_ex = _to_date(raw_ex)
    next_ex_in: Optional[int] = None
    if next_ex:
        delta = (next_ex - date.today()).days
        next_ex_in = delta if delta >= 0 else None
        if next_ex_in is None:
            next_ex = None

    return DividendInfo(
        ticker=ticker,
        last_div_amount=last_amount,
        last_div_date=last_date,
        ttm_total=ttm,
        annual_yield_pct=yield_pct,
        next_ex_date=next_ex,
        next_ex_in_days=next_ex_in,
        current_price=current_price,
    )


@dataclass(frozen=True)
class DividendSignal:
    ticker: str
    info: DividendInfo
    reason: str
    suggested_usd: float


def evaluate_dividend(
    cfg: DividendTickerConfig,
    info: DividendInfo,
    free_cash: float,
    recent_avg_price: float,
) -> Optional[DividendSignal]:
    """Recommend buying before ex-dividend if the price is currently in a dip.

    Conditions:
      - ex-date is within `buy_window_days` (e.g. <=5 days away)
      - price is at least `dip_threshold` below `recent_avg_price`
      - free cash >= min_trade_usd
    """
    if info.next_ex_in_days is None or info.next_ex_in_days > cfg.buy_window_days:
        return None
    if recent_avg_price <= 0:
        return None
    discount = (recent_avg_price - info.current_price) / recent_avg_price
    if discount < cfg.dip_threshold:
        return None
    if free_cash < cfg.min_trade_usd:
        return None

    suggested = min(cfg.max_trade_usd, max(cfg.min_trade_usd, free_cash * 0.20))
    suggested = round(min(suggested, free_cash), 2)

    reason = (
        f"ex-div in {info.next_ex_in_days}d, price ${info.current_price:.2f} is "
        f"{discount*100:.2f}% below 20d avg ${recent_avg_price:.2f}, "
        f"yield ~{info.annual_yield_pct:.2f}%"
    )
    return DividendSignal(
        ticker=info.ticker,
        info=info,
        reason=reason,
        suggested_usd=suggested,
    )


def record_received_dividend(
    session: Session,
    ticker: str,
    amount_usd: Decimal,
    pay_date: date,
) -> DividendEvent:
    """User reports a dividend they received via /divreceived; we add to cash."""
    event = DividendEvent(
        environment=LIVE,
        ticker=ticker,
        amount_usd=amount_usd,
        pay_date=datetime.combine(pay_date, datetime.min.time(), tzinfo=timezone.utc),
    )
    session.add(event)
    db.adjust_cash(session, LIVE, amount_usd)
    return event


def total_dividends_received(session: Session, env: Environment = LIVE) -> Decimal:
    from sqlalchemy import select
    rows = session.execute(
        select(DividendEvent.amount_usd).where(DividendEvent.environment == env)
    ).scalars()
    return sum((r for r in rows), Decimal("0"))
