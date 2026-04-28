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
    """Read current price via the configured price_source.

    Source priority: alpaca > robinhood > yfinance fallback. If the
    configured source fails (e.g., expired Alpaca key, lost Robinhood
    session), we automatically fall back to yfinance so the bot stays
    alive — a warning is logged.
    """
    source = _resolve_source()
    if source == "alpaca":
        try:
            return _get_price_alpaca(ticker, use_cache=use_cache)
        except Exception as e:
            log.warning("alpaca_price_failed_falling_back_to_yfinance",
                        ticker=ticker, error=str(e))
    elif source == "robinhood":
        try:
            return _get_price_robinhood(ticker, use_cache=use_cache)
        except Exception as e:
            log.warning("robinhood_price_failed_falling_back_to_yfinance",
                        ticker=ticker, error=str(e))
    return _get_price_yfinance(ticker, use_cache=use_cache)


def _get_price_yfinance(ticker: str, *, use_cache: bool = True) -> Quote:
    now = time.time()
    if use_cache and ticker in _price_cache:
        price, fetched_at = _price_cache[ticker]
        if now - fetched_at < _PRICE_TTL_SECONDS:
            return Quote(ticker, price, datetime.fromtimestamp(fetched_at, tz=timezone.utc))
    try:
        price = _fetch_price_raw(ticker)
    except Exception as e:
        log.error("yfinance_price_fetch_failed", ticker=ticker, error=str(e))
        raise PriceUnavailable(f"failed to fetch {ticker}: {e}") from e
    _price_cache[ticker] = (price, now)
    return Quote(ticker, price, datetime.fromtimestamp(now, tz=timezone.utc))


# ---------- Robinhood source (lazy-loaded) ----------

_rh_source = None  # singleton RobinhoodPriceSource
_active_source: str | None = None  # set by set_price_source()


def set_price_source(source: str) -> None:
    """Called once at startup from main.py based on StrategyConfig.price_source."""
    global _active_source
    _active_source = source


def _resolve_source() -> str:
    return _active_source or "yfinance"


def _get_robinhood_source():
    global _rh_source
    if _rh_source is None:
        from .robinhood_stocks import RobinhoodPriceSource  # lazy import
        _rh_source = RobinhoodPriceSource()
    return _rh_source


def _get_price_robinhood(ticker: str, *, use_cache: bool = True) -> Quote:
    src = _get_robinhood_source()
    if not use_cache:
        src.clear_cache()
    rq = src.get_price(ticker)
    return Quote(ticker, rq.price, rq.fetched_at)


# ---------- Alpaca source (lazy-loaded) ----------

_alpaca_source = None


def _get_alpaca_source():
    global _alpaca_source
    if _alpaca_source is None:
        from .alpaca_data import AlpacaPriceSource  # lazy import
        from .config import AppSettings
        _alpaca_source = AlpacaPriceSource(AppSettings())
    return _alpaca_source


def _get_price_alpaca(ticker: str, *, use_cache: bool = True) -> Quote:
    src = _get_alpaca_source()
    if not use_cache:
        src.clear_cache()
    aq = src.get_price(ticker)
    return Quote(ticker, aq.price, aq.fetched_at)


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
