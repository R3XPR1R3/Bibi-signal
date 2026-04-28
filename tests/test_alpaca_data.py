"""Tests for the Alpaca data price source.

We mock alpaca-py via sys.modules so the tests run even when the optional
[alpaca] extra isn't installed.
"""
from __future__ import annotations

import sys
import time
import types
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from bibi_signal.config import AppSettings


# ---------- fake alpaca module ----------

@dataclass
class FakeQuote:
    bid_price: float
    ask_price: float


class FakeClient:
    def __init__(self, key, secret):
        self.key = key
        self.secret = secret
        self.calls: list[list[str]] = []
        self.next_response: dict[str, FakeQuote] = {}
        self.raise_on_next: Exception | None = None

    def get_stock_latest_quote(self, req):
        if self.raise_on_next:
            err, self.raise_on_next = self.raise_on_next, None
            raise err
        symbols = req.symbols  # we'll attach this on the fake request
        self.calls.append(symbols)
        return {s: q for s, q in self.next_response.items() if s in symbols}


class FakeRequest:
    def __init__(self, symbol_or_symbols):
        if isinstance(symbol_or_symbols, str):
            self.symbols = [symbol_or_symbols]
        else:
            self.symbols = list(symbol_or_symbols)


@pytest.fixture
def fake_alpaca(monkeypatch):
    """Inject a fake alpaca-py module tree."""
    # Create the parent package and submodules.
    fake_data = types.ModuleType("alpaca.data")
    fake_historical = types.ModuleType("alpaca.data.historical")
    fake_requests = types.ModuleType("alpaca.data.requests")
    fake_alpaca_pkg = types.ModuleType("alpaca")

    fake_historical.StockHistoricalDataClient = FakeClient
    fake_requests.StockLatestQuoteRequest = FakeRequest

    monkeypatch.setitem(sys.modules, "alpaca", fake_alpaca_pkg)
    monkeypatch.setitem(sys.modules, "alpaca.data", fake_data)
    monkeypatch.setitem(sys.modules, "alpaca.data.historical", fake_historical)
    monkeypatch.setitem(sys.modules, "alpaca.data.requests", fake_requests)
    yield


def _settings_with_keys() -> AppSettings:
    return AppSettings(
        _env_file=None,
        alpaca_paper_api_key="PKfake",
        alpaca_paper_api_secret="secret",
    )


# ---------- tests ----------

def test_missing_creds_raises(monkeypatch, fake_alpaca):
    monkeypatch.delenv("ALPACA_PAPER_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_API_SECRET", raising=False)
    from bibi_signal.alpaca_data import AlpacaPriceSource, AlpacaNotConfigured
    s = AppSettings(_env_file=None)
    with pytest.raises(AlpacaNotConfigured):
        AlpacaPriceSource(s)


def test_get_price_uses_mid(fake_alpaca):
    from bibi_signal.alpaca_data import AlpacaPriceSource
    src = AlpacaPriceSource(_settings_with_keys(), cache_ttl_seconds=60)
    src._client.next_response = {"QQQ": FakeQuote(bid_price=480.0, ask_price=481.0)}
    q = src.get_price("QQQ")
    assert q.price == 480.5
    assert q.bid == 480.0
    assert q.ask == 481.0


def test_get_price_falls_back_to_ask_when_no_bid(fake_alpaca):
    from bibi_signal.alpaca_data import AlpacaPriceSource
    src = AlpacaPriceSource(_settings_with_keys())
    src._client.next_response = {"X": FakeQuote(bid_price=0, ask_price=100.0)}
    q = src.get_price("X")
    assert q.price == 100.0


def test_cache_avoids_duplicate_request(fake_alpaca):
    from bibi_signal.alpaca_data import AlpacaPriceSource
    src = AlpacaPriceSource(_settings_with_keys(), cache_ttl_seconds=60)
    src._client.next_response = {"QQQ": FakeQuote(480.0, 481.0)}
    src.get_price("QQQ")
    src.get_price("QQQ")
    assert len(src._client.calls) == 1


def test_batch_fetch_uses_one_call(fake_alpaca):
    from bibi_signal.alpaca_data import AlpacaPriceSource
    src = AlpacaPriceSource(_settings_with_keys())
    src._client.next_response = {
        "QQQ": FakeQuote(480.0, 481.0),
        "XLE": FakeQuote(92.0, 92.5),
        "SCHD": FakeQuote(82.0, 82.10),
    }
    out = src.get_prices(["QQQ", "XLE", "SCHD"])
    assert set(out.keys()) == {"QQQ", "XLE", "SCHD"}
    assert out["QQQ"].price == 480.5
    assert len(src._client.calls) == 1


def test_batch_fetch_uses_cache_for_known_symbols(fake_alpaca):
    from bibi_signal.alpaca_data import AlpacaPriceSource
    src = AlpacaPriceSource(_settings_with_keys(), cache_ttl_seconds=60)
    src._client.next_response = {"QQQ": FakeQuote(480.0, 481.0)}
    src.get_price("QQQ")  # caches
    src._client.next_response = {"XLE": FakeQuote(92.0, 92.5)}
    out = src.get_prices(["QQQ", "XLE"])
    assert "QQQ" in out
    assert "XLE" in out
    # Second request should only fetch XLE
    assert src._client.calls[-1] == ["XLE"]


def test_clear_cache(fake_alpaca):
    from bibi_signal.alpaca_data import AlpacaPriceSource
    src = AlpacaPriceSource(_settings_with_keys(), cache_ttl_seconds=60)
    src._client.next_response = {"QQQ": FakeQuote(480.0, 481.0)}
    src.get_price("QQQ")
    src.clear_cache()
    src.get_price("QQQ")
    assert len(src._client.calls) == 2


def test_zero_price_raises(fake_alpaca):
    from bibi_signal.alpaca_data import AlpacaPriceSource
    src = AlpacaPriceSource(_settings_with_keys())
    src._client.next_response = {"BAD": FakeQuote(0, 0)}
    with pytest.raises(RuntimeError, match="zero price"):
        src.get_price("BAD")


def test_price_fetcher_dispatches_to_alpaca(monkeypatch):
    from bibi_signal import price_fetcher
    monkeypatch.setattr(price_fetcher, "_active_source", "alpaca")
    monkeypatch.setattr(
        price_fetcher,
        "_get_price_alpaca",
        lambda t, **kw: price_fetcher.Quote(t, 555.55, None),
    )
    q = price_fetcher.get_price("QQQ")
    assert q.price == 555.55


def test_price_fetcher_falls_back_when_alpaca_fails(monkeypatch):
    from bibi_signal import price_fetcher
    monkeypatch.setattr(price_fetcher, "_active_source", "alpaca")

    def boom(*a, **kw):
        raise RuntimeError("alpaca rate limit")

    monkeypatch.setattr(price_fetcher, "_get_price_alpaca", boom)
    monkeypatch.setattr(
        price_fetcher,
        "_get_price_yfinance",
        lambda t, **kw: price_fetcher.Quote(t, 12.34, None),
    )
    q = price_fetcher.get_price("QQQ")
    assert q.price == 12.34
