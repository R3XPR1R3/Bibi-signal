"""Unit tests for the parameter optimizer.

These tests cover the pure pieces (grid generation, scoring, train/test
split). End-to-end runs that fetch yfinance data are intentionally not
included — they're verified by smoke-testing the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from bibi_signal.backtest import BTResult
from bibi_signal.config import TickerConfig
from bibi_signal.optimize import (
    Combo,
    DEFAULT_GRID,
    _score,
    _split_train_test,
    generate_combos,
)


def _base_cfg() -> TickerConfig:
    return TickerConfig(
        enabled=True,
        dip_percent=0.02,
        profit_percent=0.04,
        stop_loss_percent=0.15,
        min_trade_usd=5.0,
        max_trade_usd=20.0,
        max_open_lots=5,
        use_atr_sizing=False,
        atr_k=1.5,
        require_rsi_oversold=True,
        rsi_threshold=35,
        require_uptrend=True,
        sma_long_period=200,
    )


def test_generate_combos_default_count():
    combos = generate_combos()
    # 5 dip * 5 profit * 3 stop * 4 rsi-th * 2 uptrend  for require_rsi=True  = 600
    # 5 dip * 5 profit * 3 stop * 1 rsi-th * 2 uptrend  for require_rsi=False = 150
    assert len(combos) == 750


def test_generate_combos_custom_grid():
    grid = {
        "dip_percent": [0.02],
        "profit_percent": [0.04, 0.08],
        "stop_loss_percent": [0.15],
        "require_rsi_oversold": [True],
        "rsi_threshold": [35],
        "require_uptrend": [True, False],
    }
    combos = generate_combos(grid)
    assert len(combos) == 4  # 1 * 2 * 1 * 1 * 1 * 2


def test_combo_to_ticker_config_inherits_base():
    base = _base_cfg()
    combo = Combo(
        dip_percent=0.05,
        profit_percent=0.10,
        stop_loss_percent=0.20,
        require_rsi_oversold=False,
        rsi_threshold=30,
        require_uptrend=False,
    )
    cfg = combo.to_ticker_config(base=base)
    assert cfg.dip_percent == 0.05
    assert cfg.profit_percent == 0.10
    assert cfg.require_uptrend is False
    # Inherited from base:
    assert cfg.min_trade_usd == base.min_trade_usd
    assert cfg.sma_long_period == base.sma_long_period


def _fake_result(total_equity: float, max_dd: float, n_buys: int, bh: float = 100.0) -> BTResult:
    return BTResult(
        ticker="QQQ",
        bars=100,
        final_cash=total_equity,
        open_value=0,
        total_equity=total_equity,
        realised_pnl=total_equity - 100,
        unrealised_pnl=0,
        n_buys=n_buys,
        n_sells=n_buys,
        n_stops=0,
        win_rate=1.0,
        avg_win=1.0,
        avg_loss=0.0,
        max_drawdown=max_dd,
        buy_hold_equity=bh,
        equity_curve=pd.Series([100, total_equity]),
    )


def test_score_calmar_rewards_low_drawdown():
    starting = 100.0
    s_a = _score(_fake_result(120, max_dd=-0.10, n_buys=20), starting)  # +20% / 10% dd
    s_b = _score(_fake_result(120, max_dd=-0.30, n_buys=20), starting)  # +20% / 30% dd
    assert s_a.score > s_b.score


def test_score_penalises_few_trades():
    starting = 100.0
    s_many = _score(_fake_result(120, max_dd=-0.10, n_buys=20), starting)
    s_few = _score(_fake_result(120, max_dd=-0.10, n_buys=2), starting)
    assert s_many.score > s_few.score


def test_score_returns_buy_hold_pct():
    s = _score(_fake_result(120, -0.10, 20, bh=150.0), starting_cash=100.0)
    assert s.buy_hold_pct == 50.0


def test_split_train_test_proportions():
    df = pd.DataFrame({"Close": np.arange(100), "High": np.arange(100), "Low": np.arange(100)})
    train, test = _split_train_test(df, train_frac=0.7)
    assert len(train) == 70
    assert len(test) == 30
    # Train must come BEFORE test in time.
    assert train.index.max() < test.index.min()


def test_split_train_test_drops_nan_close():
    df = pd.DataFrame({
        "Close": [1, 2, np.nan, 4, 5, 6, 7, 8, 9, 10],
        "High": list(range(10)),
        "Low": list(range(10)),
    })
    train, test = _split_train_test(df, train_frac=0.5)
    # 9 rows after dropping NaN, split 50/50 -> 4 train, 5 test (cut = 4)
    assert len(train) + len(test) == 9
