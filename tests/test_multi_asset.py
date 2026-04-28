"""Tests for the multi-asset rotation strategy.

Pure unit tests — no network calls. Histories are synthetic so we can
control exactly what momentum and SMA values look like.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bibi_signal.config import MultiAssetConfig
from bibi_signal.multi_asset_engine import (
    CASH,
    AssetSnapshot,
    compute_snapshot,
    diff_allocation,
    run_multi_asset_backtest,
    select_target,
)


def _cfg(**kw) -> MultiAssetConfig:
    base = dict(
        enabled=True,
        universe=["A", "B", "C"],
        lookback_days=10,
        sma_long_period=20,
        rebalance_frequency_days=5,
        top_n=1,
        starting_cash=100.0,
    )
    base.update(kw)
    return MultiAssetConfig(**base)


def _df_from_close(prices: list[float]) -> pd.DataFrame:
    """Synthesise OHLCV that has a Close column the engine actually uses."""
    return pd.DataFrame({
        "Open": prices, "High": prices, "Low": prices, "Close": prices,
        "Volume": [1_000_000] * len(prices),
    })


# ---------- compute_snapshot ----------

def test_snapshot_warmup_when_history_too_short():
    cfg = _cfg(lookback_days=10, sma_long_period=20)
    df = _df_from_close([100.0] * 5)
    s = compute_snapshot("A", df, cfg)
    assert s.has_data is False


def test_snapshot_computes_lookback_return():
    cfg = _cfg(lookback_days=10, sma_long_period=15)
    # Prices flat 100 for 15 bars, then rising to 110: 10% gain over the last 10 bars.
    prices = [100.0] * 15 + [101.0, 102.0, 103.0, 104.0, 105.0,
                              106.0, 107.0, 108.0, 109.0, 110.0]
    df = _df_from_close(prices)
    s = compute_snapshot("A", df, cfg)
    assert s.has_data is True
    assert s.return_lookback == pytest.approx((110 - 100) / 100, abs=1e-9)


def test_snapshot_above_sma_check():
    cfg = _cfg(lookback_days=5, sma_long_period=10)
    prices = [50.0] * 10 + [50.0, 50.0, 50.0, 50.0, 100.0]
    df = _df_from_close(prices)
    s = compute_snapshot("A", df, cfg)
    assert s.above_sma_long is True

    prices = [50.0] * 10 + [50.0, 50.0, 50.0, 50.0, 30.0]
    df = _df_from_close(prices)
    s = compute_snapshot("A", df, cfg)
    assert s.above_sma_long is False


# ---------- select_target ----------

def test_target_picks_best_above_sma():
    cfg = _cfg(top_n=1)
    snaps = [
        AssetSnapshot("A", 100, return_lookback=0.05, above_sma_long=True, has_data=True),
        AssetSnapshot("B", 50, return_lookback=0.10, above_sma_long=True, has_data=True),
        AssetSnapshot("C", 200, return_lookback=0.20, above_sma_long=False, has_data=True),
    ]
    target = select_target(cfg, snaps)
    # C has highest return but is below SMA, so it's filtered out.
    # Among A and B, B's return is higher.
    assert target.weights == {"B": 1.0}


def test_target_top_n_equal_weight():
    cfg = _cfg(top_n=2)
    snaps = [
        AssetSnapshot("A", 100, 0.05, True, True),
        AssetSnapshot("B", 50, 0.10, True, True),
        AssetSnapshot("C", 200, 0.20, True, True),
    ]
    target = select_target(cfg, snaps)
    assert set(target.weights.keys()) == {"C", "B"}
    assert all(abs(w - 0.5) < 1e-9 for w in target.weights.values())


def test_target_all_cash_when_nothing_above_sma():
    cfg = _cfg()
    snaps = [
        AssetSnapshot("A", 100, 0.05, False, True),
        AssetSnapshot("B", 50, 0.10, False, True),
    ]
    target = select_target(cfg, snaps)
    assert target.is_all_cash()
    assert "defensive" in target.reason


def test_target_skips_warmup_assets():
    cfg = _cfg(top_n=1)
    snaps = [
        AssetSnapshot("A", 100, 0.05, True, has_data=False),  # not enough history
        AssetSnapshot("B", 50, 0.03, True, has_data=True),
    ]
    target = select_target(cfg, snaps)
    assert target.weights == {"B": 1.0}


# ---------- diff_allocation ----------

def test_diff_emits_no_signals_when_aligned():
    assert diff_allocation({"QQQ": 1.0}, {"QQQ": 1.0}) == []


def test_diff_emits_sell_then_buy():
    signals = diff_allocation({"QQQ": 1.0}, {"XLE": 1.0})
    assert len(signals) == 2
    assert signals[0].sell_ticker == "QQQ"
    assert signals[1].buy_ticker == "XLE"


def test_diff_ignores_tiny_drift():
    # 49% vs 51% — within default 5% threshold, no signal
    signals = diff_allocation({"QQQ": 0.49, "XLE": 0.51}, {"QQQ": 0.51, "XLE": 0.49})
    assert signals == []


def test_diff_cash_to_asset():
    signals = diff_allocation({CASH: 1.0}, {"QQQ": 1.0})
    # Going from cash to QQQ is a single buy signal; CASH isn't traded.
    buys = [s for s in signals if s.buy_ticker == "QQQ"]
    assert len(buys) == 1


# ---------- run_multi_asset_backtest ----------

def _trending_history(prices: list[float], dates: pd.DatetimeIndex) -> pd.DataFrame:
    df = pd.DataFrame({
        "Open": prices, "High": prices, "Low": prices, "Close": prices,
        "Volume": [1_000_000] * len(prices),
    }, index=dates)
    return df


def test_backtest_picks_winner_in_two_asset_universe():
    """A is a steady winner, B underperforms. Bot should hold A most of the time."""
    cfg = _cfg(universe=["A", "B"], lookback_days=20, sma_long_period=30,
               rebalance_frequency_days=10, starting_cash=1000.0)
    n = 200
    dates = pd.date_range("2024-01-01", periods=n)
    a_prices = [100 * (1.001 ** i) for i in range(n)]   # +0.1%/day
    b_prices = [100 * (0.999 ** i) for i in range(n)]   # −0.1%/day
    histories = {
        "A": _trending_history(a_prices, dates),
        "B": _trending_history(b_prices, dates),
    }
    result = run_multi_asset_backtest(cfg, histories)
    assert result.days_per_ticker.get("A", 0) > result.days_per_ticker.get("B", 0)
    assert result.total_return_pct > 0  # made money on A


def test_backtest_goes_defensive_when_all_falling():
    """All assets crash below their SMA — bot should sit in cash for most days."""
    cfg = _cfg(universe=["A", "B"], lookback_days=20, sma_long_period=30,
               rebalance_frequency_days=5, starting_cash=1000.0)
    n = 200
    dates = pd.date_range("2024-01-01", periods=n)
    # Both rise for first 100 bars (build SMA), then crash.
    a_prices = [100 + i for i in range(100)] + [200 - 0.5 * i for i in range(100)]
    b_prices = [100 + i for i in range(100)] + [200 - 0.6 * i for i in range(100)]
    histories = {
        "A": _trending_history(a_prices, dates),
        "B": _trending_history(b_prices, dates),
    }
    result = run_multi_asset_backtest(cfg, histories)
    # Defensive cash mode should kick in at some point.
    assert result.days_in_cash > 0
    # Drawdown should be limited.
    assert result.max_drawdown_pct < 50  # not catastrophic


def test_backtest_raises_on_empty_histories():
    cfg = _cfg()
    with pytest.raises(ValueError):
        run_multi_asset_backtest(cfg, {})
