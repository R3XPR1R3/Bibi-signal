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
    # base = 5 dip * 5 profit * 3 stop * 2 uptrend = 150
    # with require_rsi_oversold: 4 thresholds, off: 1 -> rsi_dim = 4 + 1 = 5 -> 750 base combos
    # with trailing: 3 trail percents, off: 1 -> trail_dim = 3 + 1 = 4 -> 750 * 4 = 3000
    assert len(combos) == 3000


def test_generate_combos_custom_grid():
    grid = {
        "dip_percent": [0.02],
        "profit_percent": [0.04, 0.08],
        "stop_loss_percent": [0.15],
        "require_rsi_oversold": [True],
        "rsi_threshold": [35],
        "require_uptrend": [True, False],
        "trailing_take_profit": [False, True],
        "trail_percent": [0.02, 0.05],
    }
    combos = generate_combos(grid)
    # 1 * 2 * 1 * 1 * 1 * 2 = 4 base combos
    # trailing off: 1 trail_percent (deduped) -> 4
    # trailing on: 2 trail_percents -> 8
    # total: 4 + 8 = 12
    assert len(combos) == 12


def test_combo_to_ticker_config_inherits_base():
    base = _base_cfg()
    combo = Combo(
        dip_percent=0.05,
        profit_percent=0.10,
        stop_loss_percent=0.20,
        require_rsi_oversold=False,
        rsi_threshold=30,
        require_uptrend=False,
        trailing_take_profit=True,
        trail_percent=0.05,
    )
    cfg = combo.to_ticker_config(base=base)
    assert cfg.dip_percent == 0.05
    assert cfg.profit_percent == 0.10
    assert cfg.require_uptrend is False
    assert cfg.trailing_take_profit is True
    assert cfg.trail_percent == 0.05
    # Inherited from base:
    assert cfg.min_trade_usd == base.min_trade_usd
    assert cfg.sma_long_period == base.sma_long_period


def test_generate_combos_dedupes_trail_percent_when_trailing_off():
    grid = {
        "dip_percent": [0.02],
        "profit_percent": [0.04],
        "stop_loss_percent": [0.15],
        "require_rsi_oversold": [True],
        "rsi_threshold": [35],
        "require_uptrend": [True],
        "trailing_take_profit": [False],
        "trail_percent": [0.02, 0.05, 0.10],
    }
    combos = generate_combos(grid)
    # Only one combo even though trail_percent has 3 values, because
    # trailing is off so trail_percent is dominated.
    assert len(combos) == 1
    assert combos[0].trail_percent == 0.02  # the first value


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


# ---------- apply_combo_to_yaml ----------

from bibi_signal.optimize import apply_combo_to_yaml
from pathlib import Path


def _yaml_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text("""\
starting_cash: 100.0
check_frequency_minutes: 10
respect_market_hours: true
price_source: yfinance
tickers:
  QQQ:
    enabled: true
    dip_percent: 0.02         # buy on this dip
    profit_percent: 0.04
    stop_loss_percent: 0.15
    min_trade_usd: 5.0
    max_trade_usd: 20.0
    max_open_lots: 5
    use_atr_sizing: false
    atr_k: 1.5
    require_rsi_oversold: true
    rsi_threshold: 35
    require_uptrend: true
    sma_long_period: 200
    trailing_take_profit: false
    trail_percent: 0.02
  XLE:
    enabled: true
    dip_percent: 0.025
    profit_percent: 0.05
    stop_loss_percent: 0.15
    min_trade_usd: 5.0
    max_trade_usd: 20.0
    max_open_lots: 5
    use_atr_sizing: false
    atr_k: 1.5
    require_rsi_oversold: true
    rsi_threshold: 35
    require_uptrend: true
    sma_long_period: 200
    trailing_take_profit: false
    trail_percent: 0.025
""")
    return p


def _winner_combo() -> Combo:
    return Combo(
        dip_percent=0.05,
        profit_percent=0.08,
        stop_loss_percent=0.10,
        require_rsi_oversold=False,
        rsi_threshold=35,
        require_uptrend=False,
        trailing_take_profit=True,
        trail_percent=0.05,
    )


def test_apply_changes_only_target_ticker(tmp_path):
    p = _yaml_fixture(tmp_path)
    changes = apply_combo_to_yaml(p, "QQQ", _winner_combo())
    text = p.read_text()
    # QQQ is updated
    assert "dip_percent: 0.05" in text
    assert "profit_percent: 0.08" in text
    assert "trailing_take_profit: true" in text
    assert "trail_percent: 0.05" in text
    assert "require_uptrend: false" in text
    assert "require_rsi_oversold: false" in text
    # XLE is NOT touched
    assert "dip_percent: 0.025" in text
    assert "profit_percent: 0.05\n    stop_loss_percent" in text  # the XLE block
    assert len(changes) > 0


def test_apply_preserves_comments(tmp_path):
    p = _yaml_fixture(tmp_path)
    apply_combo_to_yaml(p, "QQQ", _winner_combo())
    text = p.read_text()
    assert "# buy on this dip" in text


def test_apply_preserves_unrelated_keys(tmp_path):
    p = _yaml_fixture(tmp_path)
    apply_combo_to_yaml(p, "QQQ", _winner_combo())
    text = p.read_text()
    # Things NOT in our update list must be untouched
    assert "min_trade_usd: 5.0" in text
    assert "max_open_lots: 5" in text
    assert "sma_long_period: 200" in text
    assert "starting_cash: 100.0" in text
    assert "price_source: yfinance" in text


def test_apply_returns_changes_list(tmp_path):
    p = _yaml_fixture(tmp_path)
    changes = apply_combo_to_yaml(p, "QQQ", _winner_combo())
    joined = "\n".join(changes)
    assert "dip_percent" in joined
    assert "0.02" in joined and "0.05" in joined  # before/after for dip


def test_apply_no_changes_when_already_matching(tmp_path):
    p = _yaml_fixture(tmp_path)
    # Combo that matches the QQQ defaults exactly
    same = Combo(
        dip_percent=0.02,
        profit_percent=0.04,
        stop_loss_percent=0.15,
        require_rsi_oversold=True,
        rsi_threshold=35,
        require_uptrend=True,
        trailing_take_profit=False,
        trail_percent=0.02,
    )
    changes = apply_combo_to_yaml(p, "QQQ", same)
    assert changes == []


def test_apply_validates_after_write(tmp_path):
    """If the result is invalid YAML, the original should be restored."""
    p = _yaml_fixture(tmp_path)
    original = p.read_text()
    # Corrupt the apply path: pass a combo with an out-of-range value
    # by mutating the dataclass post-construction. Pydantic validates on load.
    # Easiest: swap the YAML to something that won't parse back.
    bad = Combo(
        dip_percent=999.0,  # >= 0.5 will fail TickerConfig validation
        profit_percent=0.04,
        stop_loss_percent=0.15,
        require_rsi_oversold=True,
        rsi_threshold=35,
        require_uptrend=True,
        trailing_take_profit=False,
        trail_percent=0.02,
    )
    try:
        apply_combo_to_yaml(p, "QQQ", bad)
    except RuntimeError:
        pass
    # File must be restored
    assert p.read_text() == original
