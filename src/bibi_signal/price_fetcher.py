"""yfinance wrapper with caching, retries, and a market-hours helper.

yfinance is unofficial Yahoo scraping — it occasionally fails. The retry
decorator masks transient errors; the cache prevents hammering Yahoo when
the scheduler runs every few minutes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import structlog
import yfinance as yf
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = structlog.get_logger(__name__)

_PRICE_TTL_SECONDS = 60
_HISTORY_TTL_SECONDS = 300

_price_cache: dict[str, tuple[float, float]] = {}  # ticker -> (price, fetched_at)
_history_cache: dict[tuple[str, str, str], tuple[pd.DataFrame, float]] = {}

US_EASTERN = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)


@dataclass(frozen=True)
class Quote:
    ticker: str
    price: float
    fetched_at: datetime


class PriceUnavailable(RuntimeError):
    pass


def is_market_open(now: datetime | None = None) -> bool:
    """Approximate US equity market hours. Doesn't account for half-days/holidays."""
    now = now or datetime.now(timezone.utc)
    et = now.astimezone(US_EASTERN)
    if et.weekday() >= 5:  # Sat/Sun
        return False
    return MARKET_OPEN <= et.time() <= MARKET_CLOSE


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
)
def _fetch_price_raw(ticker: str) -> float:
    t = yf.Ticker(ticker)
    fast = t.fast_info
    price = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
    if price is None or price <= 0:
        # Fallback to 1-day history close
        hist = t.history(period="1d", interval="1m")
        if hist.empty:
            raise PriceUnavailable(f"no price for {ticker}")
        price = float(hist["Close"].iloc[-1])
    return float(price)


def get_price(ticker: str, *, use_cache: bool = True) -> Quote:
    now = time.time()
    if use_cache and ticker in _price_cache:
        price, fetched_at = _price_cache[ticker]
        if now - fetched_at < _PRICE_TTL_SECONDS:
            return Quote(ticker, price, datetime.fromtimestamp(fetched_at, tz=timezone.utc))

    try:
        price = _fetch_price_raw(ticker)
    except Exception as e:
        log.error("price_fetch_failed", ticker=ticker, error=str(e))
        raise PriceUnavailable(f"failed to fetch {ticker}: {e}") from e

    _price_cache[ticker] = (price, now)
    return Quote(ticker, price, datetime.fromtimestamp(now, tz=timezone.utc))


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def _fetch_history_raw(ticker: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        raise PriceUnavailable(f"no history for {ticker} period={period} interval={interval}")
    return df


def get_history(
    ticker: str,
    period: str = "1y",
    interval: str = "1d",
    *,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Returns OHLCV DataFrame indexed by date."""
    key = (ticker, period, interval)
    now = time.time()
    if use_cache and key in _history_cache:
        df, fetched_at = _history_cache[key]
        if now - fetched_at < _HISTORY_TTL_SECONDS:
            return df

    df = _fetch_history_raw(ticker, period, interval)
    _history_cache[key] = (df, now)
    return df


def clear_cache() -> None:
    _price_cache.clear()
    _history_cache.clear()
