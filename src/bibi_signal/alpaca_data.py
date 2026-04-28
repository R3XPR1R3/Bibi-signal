"""Alpaca Markets Data API price source.

Free tier of Alpaca's Market Data API gives **real-time IEX quotes** for
US stocks/ETFs through an officially supported, documented API. Same
NBBO numbers that Robinhood shows you, but legal — Alpaca actively
encourages programmatic access, that's their business model.

Compared to:
  yfinance   — free, ~30s lag, unofficial Yahoo scraping
  robinhood  — real-time but ToS-violation, account-lock risk
  alpaca     — real-time, official, free, recommended ✓

Setup:
    1. https://alpaca.markets/ -> sign up (free, no SSN needed for paper)
    2. Dashboard -> Paper Trading -> Generate API keys
    3. .env:
         ALPACA_PAPER_API_KEY=PK...
         ALPACA_PAPER_API_SECRET=...
    4. config.yaml:
         price_source: alpaca

The PAPER keys give you market data on the free tier. You don't need to
open a real (live) trading account. Bibi-Signal does NOT use Alpaca's
paper-trading orders — our own paper_engine.py does that internally
using whatever data source is configured.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from .config import AppSettings

log = structlog.get_logger(__name__)


class AlpacaNotConfigured(RuntimeError):
    pass


class AlpacaNotInstalled(RuntimeError):
    pass


@dataclass(frozen=True)
class AlpacaQuote:
    symbol: str
    price: float       # mid of bid/ask, or whichever side is available
    bid: float
    ask: float
    fetched_at: datetime


def _import_alpaca() -> tuple[Any, Any]:
    """Returns (StockHistoricalDataClient, StockLatestQuoteRequest)."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        return StockHistoricalDataClient, StockLatestQuoteRequest
    except ImportError as e:
        raise AlpacaNotInstalled(
            "alpaca-py is not installed. Install with:\n"
            "  pip install -e '.[alpaca]'"
        ) from e


def _mid_price(quote: Any) -> float:
    """Compute a single 'price' from an Alpaca Quote object."""
    bid = float(getattr(quote, "bid_price", 0) or 0)
    ask = float(getattr(quote, "ask_price", 0) or 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    return ask if ask > 0 else bid


class AlpacaPriceSource:
    """Cached Alpaca price reader.

    Constructed once and reused. Not thread-safe — fine for our async
    single-threaded scheduler.
    """

    def __init__(self, settings: AppSettings, cache_ttl_seconds: int = 30):
        if not (settings.alpaca_paper_api_key and settings.alpaca_paper_api_secret):
            raise AlpacaNotConfigured(
                "ALPACA_PAPER_API_KEY and ALPACA_PAPER_API_SECRET must be set in .env"
            )
        self._client_cls, self._req_cls = _import_alpaca()
        self._client = self._client_cls(
            settings.alpaca_paper_api_key,
            settings.alpaca_paper_api_secret,
        )
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[float, float, float, float]] = {}
        # symbol -> (price, bid, ask, fetched_at)

    def _cached(self, symbol: str, now: float) -> Optional[AlpacaQuote]:
        c = self._cache.get(symbol)
        if c and now - c[3] < self._cache_ttl:
            return AlpacaQuote(
                symbol=symbol,
                price=c[0],
                bid=c[1],
                ask=c[2],
                fetched_at=datetime.fromtimestamp(c[3], tz=timezone.utc),
            )
        return None

    def _store(self, symbol: str, price: float, bid: float, ask: float, now: float) -> None:
        self._cache[symbol] = (price, bid, ask, now)

    def get_price(self, symbol: str) -> AlpacaQuote:
        now = time.time()
        cached = self._cached(symbol, now)
        if cached:
            return cached
        req = self._req_cls(symbol_or_symbols=[symbol])
        try:
            resp = self._client.get_stock_latest_quote(req)
        except Exception as e:
            raise RuntimeError(f"alpaca quote failed for {symbol}: {e}") from e
        if symbol not in resp:
            raise RuntimeError(f"alpaca returned no quote for {symbol}")
        q = resp[symbol]
        bid = float(getattr(q, "bid_price", 0) or 0)
        ask = float(getattr(q, "ask_price", 0) or 0)
        price = _mid_price(q)
        if price <= 0:
            raise RuntimeError(f"alpaca returned zero price for {symbol}")
        self._store(symbol, price, bid, ask, now)
        return AlpacaQuote(
            symbol=symbol, price=price, bid=bid, ask=ask,
            fetched_at=datetime.fromtimestamp(now, tz=timezone.utc),
        )

    def get_prices(self, symbols: list[str]) -> dict[str, AlpacaQuote]:
        """Batch fetch — one request for all symbols. Always preferred when
        evaluating multiple tickers per tick."""
        now = time.time()
        result: dict[str, AlpacaQuote] = {}
        to_fetch: list[str] = []
        for s in symbols:
            cached = self._cached(s, now)
            if cached:
                result[s] = cached
            else:
                to_fetch.append(s)
        if not to_fetch:
            return result
        req = self._req_cls(symbol_or_symbols=to_fetch)
        try:
            resp = self._client.get_stock_latest_quote(req)
        except Exception as e:
            raise RuntimeError(f"alpaca batch quote failed: {e}") from e
        for s in to_fetch:
            q = resp.get(s)
            if q is None:
                continue
            bid = float(getattr(q, "bid_price", 0) or 0)
            ask = float(getattr(q, "ask_price", 0) or 0)
            price = _mid_price(q)
            if price <= 0:
                continue
            self._store(s, price, bid, ask, now)
            result[s] = AlpacaQuote(
                symbol=s, price=price, bid=bid, ask=ask,
                fetched_at=datetime.fromtimestamp(now, tz=timezone.utc),
            )
        return result

    def clear_cache(self) -> None:
        self._cache.clear()
