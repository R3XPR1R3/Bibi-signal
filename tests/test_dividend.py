"""Tests for dividend evaluation logic. Network-dependent fetchers are skipped."""
from __future__ import annotations

from datetime import date

from bibi_signal.config import DividendTickerConfig
from bibi_signal.dividend import DividendInfo, evaluate_dividend


def make_info(price: float, ex_in_days: int | None) -> DividendInfo:
    return DividendInfo(
        ticker="SCHD",
        last_div_amount=0.75,
        last_div_date=date(2024, 12, 1),
        ttm_total=2.85,
        annual_yield_pct=3.5,
        next_ex_date=date.today() if ex_in_days is not None else None,
        next_ex_in_days=ex_in_days,
        current_price=price,
    )


def test_no_signal_when_no_ex_date():
    cfg = DividendTickerConfig()
    info = make_info(price=80.0, ex_in_days=None)
    assert evaluate_dividend(cfg, info, free_cash=100.0, recent_avg_price=82.0) is None


def test_no_signal_when_ex_date_too_far():
    cfg = DividendTickerConfig(buy_window_days=5)
    info = make_info(price=80.0, ex_in_days=10)
    assert evaluate_dividend(cfg, info, free_cash=100.0, recent_avg_price=82.0) is None


def test_no_signal_when_no_dip():
    cfg = DividendTickerConfig(dip_threshold=0.02)
    info = make_info(price=82.0, ex_in_days=3)
    assert evaluate_dividend(cfg, info, free_cash=100.0, recent_avg_price=82.0) is None


def test_signal_when_in_dip_and_ex_date_close():
    cfg = DividendTickerConfig(buy_window_days=5, dip_threshold=0.01, min_trade_usd=5.0)
    info = make_info(price=80.0, ex_in_days=2)  # 80 vs avg 82 -> 2.4% dip
    sig = evaluate_dividend(cfg, info, free_cash=100.0, recent_avg_price=82.0)
    assert sig is not None
    assert sig.suggested_usd >= cfg.min_trade_usd
    assert "ex-div" in sig.reason


def test_no_signal_when_cash_too_low():
    cfg = DividendTickerConfig(min_trade_usd=10.0)
    info = make_info(price=80.0, ex_in_days=2)
    assert evaluate_dividend(cfg, info, free_cash=2.0, recent_avg_price=82.0) is None
