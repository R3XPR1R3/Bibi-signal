"""Tests for the Robinhood STOCKS price source guardrails.

We don't actually call Robinhood — robin-stocks isn't even required to be
installed. Tests mock the underlying library so the guard logic can be
verified in isolation.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from bibi_signal.robinhood_stocks import (
    RobinhoodNotConfigured,
    RobinhoodNotInstalled,
    RobinhoodPriceSource,
    RobinhoodRateLimited,
    _RateLimitGuard,
    _import_rh,
)


# ---------- rate limit guard ----------

def test_rate_limit_allows_under_threshold():
    g = _RateLimitGuard(max_per_minute=5)
    for _ in range(5):
        g.check()  # no error


def test_rate_limit_refuses_over_threshold():
    g = _RateLimitGuard(max_per_minute=3)
    for _ in range(3):
        g.check()
    with pytest.raises(RobinhoodRateLimited):
        g.check()


def test_rate_limit_window_slides():
    g = _RateLimitGuard(max_per_minute=2)
    g.check()
    g.check()
    # Force the recorded calls to be older than 60 seconds.
    g._calls.clear()
    g._calls.append(time.time() - 70)
    g._calls.append(time.time() - 70)
    g.check()  # window slid, allowed again


# ---------- import / config errors ----------

def test_import_raises_when_rh_missing(monkeypatch):
    # Pretend robin_stocks isn't installed
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kw):
        if name.startswith("robin_stocks"):
            raise ImportError("not installed")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RobinhoodNotInstalled):
        _import_rh()


def _install_fake_rh(monkeypatch, get_latest_price_impl):
    """Install a fake robin_stocks.robinhood module for the duration of a test."""
    fake_stocks = types.SimpleNamespace(get_latest_price=get_latest_price_impl)
    fake_robinhood = types.SimpleNamespace(stocks=fake_stocks, login=lambda **kw: None)
    fake_pkg = types.SimpleNamespace(robinhood=fake_robinhood)
    monkeypatch.setitem(sys.modules, "robin_stocks", fake_pkg)
    monkeypatch.setitem(sys.modules, "robin_stocks.robinhood", fake_robinhood)


def test_no_session_raises_not_configured(monkeypatch, tmp_path):
    _install_fake_rh(monkeypatch, lambda *a, **k: ["100.00"])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(RobinhoodNotConfigured):
        RobinhoodPriceSource()


# ---------- caching ----------

def test_cache_returns_same_price_within_ttl(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_get(symbol):
        calls["n"] += 1
        return ["480.50"]

    _install_fake_rh(monkeypatch, fake_get)
    # Fake a cached session
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".tokens").mkdir()
    (tmp_path / ".tokens" / "robinhood.pickle").write_bytes(b"fake")

    src = RobinhoodPriceSource(cache_ttl_seconds=60)
    q1 = src.get_price("QQQ")
    q2 = src.get_price("QQQ")
    assert q1.price == q2.price == 480.50
    assert calls["n"] == 1  # second call hit cache


def test_batch_fetch_uses_one_call(monkeypatch, tmp_path):
    calls = {"args": None, "n": 0}

    def fake_get(symbols):
        calls["args"] = symbols
        calls["n"] += 1
        return ["480.50", "92.31", "82.10"]

    _install_fake_rh(monkeypatch, fake_get)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".tokens").mkdir()
    (tmp_path / ".tokens" / "robinhood.pickle").write_bytes(b"fake")

    src = RobinhoodPriceSource()
    out = src.get_prices(["QQQ", "XLE", "SCHD"])
    assert calls["n"] == 1
    assert calls["args"] == ["QQQ", "XLE", "SCHD"]
    assert set(out.keys()) == {"QQQ", "XLE", "SCHD"}
    assert out["QQQ"].price == 480.50


def test_rate_limit_kicks_in(monkeypatch, tmp_path):
    _install_fake_rh(monkeypatch, lambda s: ["100.00"])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".tokens").mkdir()
    (tmp_path / ".tokens" / "robinhood.pickle").write_bytes(b"fake")

    src = RobinhoodPriceSource(cache_ttl_seconds=0, rate_limit_per_min=3)
    src.get_price("A")
    src.get_price("B")
    src.get_price("C")
    with pytest.raises(RobinhoodRateLimited):
        src.get_price("D")


# ---------- price_fetcher dispatch ----------

def test_price_fetcher_dispatches_to_yfinance_when_configured(monkeypatch):
    from bibi_signal import price_fetcher
    monkeypatch.setattr(price_fetcher, "_active_source", "yfinance")
    monkeypatch.setattr(
        price_fetcher,
        "_get_price_yfinance",
        lambda t, **kw: price_fetcher.Quote(t, 123.45, None),
    )
    q = price_fetcher.get_price("QQQ")
    assert q.price == 123.45


def test_price_fetcher_falls_back_to_yfinance_when_robinhood_fails(monkeypatch):
    from bibi_signal import price_fetcher
    monkeypatch.setattr(price_fetcher, "_active_source", "robinhood")

    def boom(*a, **kw):
        raise RuntimeError("rh down")

    monkeypatch.setattr(price_fetcher, "_get_price_robinhood", boom)
    monkeypatch.setattr(
        price_fetcher,
        "_get_price_yfinance",
        lambda t, **kw: price_fetcher.Quote(t, 99.99, None),
    )
    q = price_fetcher.get_price("QQQ")
    assert q.price == 99.99
