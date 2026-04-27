"""Unit tests for the pure strategy logic."""
from __future__ import annotations

import pytest

from bibi_signal.config import TickerConfig
from bibi_signal.strategy import (
    LotSnapshot,
    MarketSnapshot,
    SignalKind,
    compute_targets,
    evaluate,
)


def make_cfg(**overrides) -> TickerConfig:
    base = dict(
        enabled=True,
        dip_percent=0.02,
        profit_percent=0.04,
        stop_loss_percent=0.15,
        min_trade_usd=5.0,
        max_trade_usd=20.0,
        max_open_lots=5,
        use_atr_sizing=False,
        atr_k=1.5,
        require_rsi_oversold=False,
        rsi_threshold=35,
        require_uptrend=False,
        sma_long_period=200,
    )
    base.update(overrides)
    return TickerConfig(**base)


def make_market(price: float, *, sma_long: float = 100.0, rsi: float = 50.0, atr: float = 1.0,
                in_uptrend: bool = True) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="QQQ",
        price=price,
        sma_long=sma_long,
        rsi=rsi,
        atr=atr,
        in_uptrend=in_uptrend,
    )


def make_lot(buy_price: float, *, lot_id: int = 1, target: float = None, stop: float = None,
             qty: float = 1.0) -> LotSnapshot:
    target = target if target is not None else buy_price * 1.04
    stop = stop if stop is not None else buy_price * 0.85
    return LotSnapshot(
        id=lot_id,
        ticker="QQQ",
        buy_price=buy_price,
        quantity=qty,
        target_price=target,
        stop_price=stop,
    )


# ---------- BUY ----------

def test_first_rung_buy_when_no_lots():
    cfg = make_cfg()
    market = make_market(price=100.0)
    out = evaluate(cfg, market, open_lots_for_ticker=[], free_cash=100.0)
    assert len(out) == 1
    assert out[0].kind == SignalKind.BUY
    assert 5.0 <= out[0].suggested_usd <= 20.0


def test_buy_only_after_dip_when_ladder_started():
    cfg = make_cfg(dip_percent=0.02)
    last = make_lot(buy_price=100.0)
    # No dip yet
    out = evaluate(cfg, make_market(price=99.0), [last], free_cash=100.0)
    assert out[0].kind == SignalKind.HOLD

    # Exactly at threshold
    out = evaluate(cfg, make_market(price=98.0), [last], free_cash=100.0)
    assert out[0].kind == SignalKind.BUY

    # Below threshold
    out = evaluate(cfg, make_market(price=95.0), [last], free_cash=100.0)
    assert out[0].kind == SignalKind.BUY


def test_buy_blocked_by_uptrend_filter():
    cfg = make_cfg(require_uptrend=True)
    market = make_market(price=100.0, sma_long=110.0, in_uptrend=False)
    out = evaluate(cfg, market, [], free_cash=100.0)
    assert out[0].kind == SignalKind.HOLD
    assert "trend" in out[0].reason.lower()


def test_buy_blocked_by_rsi_filter():
    cfg = make_cfg(require_rsi_oversold=True, rsi_threshold=35)
    market = make_market(price=100.0, rsi=50.0)
    out = evaluate(cfg, market, [], free_cash=100.0)
    assert out[0].kind == SignalKind.HOLD
    assert "rsi" in out[0].reason.lower()


def test_buy_passes_when_rsi_oversold():
    cfg = make_cfg(require_rsi_oversold=True, rsi_threshold=35)
    market = make_market(price=100.0, rsi=30.0)
    out = evaluate(cfg, market, [], free_cash=100.0)
    assert out[0].kind == SignalKind.BUY


def test_buy_blocked_when_ladder_full():
    cfg = make_cfg(max_open_lots=2)
    lots = [make_lot(buy_price=100.0, lot_id=1), make_lot(buy_price=98.0, lot_id=2)]
    out = evaluate(cfg, make_market(price=90.0), lots, free_cash=100.0)
    assert out[0].kind == SignalKind.HOLD
    assert "ladder full" in out[0].reason


def test_buy_blocked_when_cash_below_min():
    cfg = make_cfg(min_trade_usd=5.0)
    out = evaluate(cfg, make_market(price=100.0), [], free_cash=2.0)
    assert out[0].kind == SignalKind.HOLD
    assert "cash" in out[0].reason.lower()


# ---------- SELL / STOP ----------

def test_sell_when_price_at_target():
    cfg = make_cfg(profit_percent=0.04)
    lot = make_lot(buy_price=100.0)  # target 104
    out = evaluate(cfg, make_market(price=104.0), [lot], free_cash=100.0)
    assert any(p.kind == SignalKind.SELL and p.lot_id == 1 for p in out)


def test_stop_when_price_at_stop():
    cfg = make_cfg(stop_loss_percent=0.15)
    lot = make_lot(buy_price=100.0)  # stop 85
    out = evaluate(cfg, make_market(price=85.0), [lot], free_cash=100.0)
    assert any(p.kind == SignalKind.STOP and p.lot_id == 1 for p in out)


def test_multiple_lots_emit_multiple_exits():
    cfg = make_cfg()
    lot1 = make_lot(buy_price=100.0, lot_id=1, target=104.0)
    lot2 = make_lot(buy_price=98.0, lot_id=2, target=101.92)
    out = evaluate(cfg, make_market(price=105.0), [lot1, lot2], free_cash=100.0)
    sell_ids = sorted(p.lot_id for p in out if p.kind == SignalKind.SELL)
    assert sell_ids == [1, 2]


def test_exit_takes_precedence_over_entry():
    """If a lot triggers SELL, we should NOT also emit a BUY in the same tick."""
    cfg = make_cfg(dip_percent=0.02)
    lot = make_lot(buy_price=100.0, target=101.0)  # tiny target so SELL fires
    # price 102 = above target AND much higher than dip ref, so neither buy
    out = evaluate(cfg, make_market(price=102.0), [lot], free_cash=100.0)
    kinds = {p.kind for p in out}
    assert SignalKind.SELL in kinds
    assert SignalKind.BUY not in kinds


# ---------- targets / pnl helpers ----------

def test_compute_targets():
    cfg = make_cfg(profit_percent=0.04, stop_loss_percent=0.15)
    target, stop = compute_targets(100.0, cfg)
    assert target == pytest.approx(104.0)
    assert stop == pytest.approx(85.0)


# ---------- ATR sizing ----------

def test_atr_sizing_overrides_dip_percent():
    cfg = make_cfg(use_atr_sizing=True, atr_k=2.0, dip_percent=0.10)
    last = make_lot(buy_price=100.0)
    # ATR=1.0 price=99.0 -> dip = 2.0 * 1.0 / 99.0 ≈ 2.02%
    # threshold ≈ 100 * (1 - 0.0202) = 97.98
    market = make_market(price=99.0, atr=1.0)
    out = evaluate(cfg, market, [last], free_cash=100.0)
    assert out[0].kind == SignalKind.HOLD  # 99 > 97.98

    market = make_market(price=97.0, atr=1.0)
    out = evaluate(cfg, market, [last], free_cash=100.0)
    assert out[0].kind == SignalKind.BUY


# ---------- idempotency ----------

def test_signal_fingerprint_is_stable():
    cfg = make_cfg()
    out1 = evaluate(cfg, make_market(price=100.0), [], free_cash=100.0)
    out2 = evaluate(cfg, make_market(price=100.0), [], free_cash=100.0)
    assert out1[0].fingerprint() == out2[0].fingerprint()


def test_signal_fingerprint_changes_with_price():
    cfg = make_cfg()
    out1 = evaluate(cfg, make_market(price=100.0), [], free_cash=100.0)
    out2 = evaluate(cfg, make_market(price=101.0), [], free_cash=100.0)
    assert out1[0].fingerprint() != out2[0].fingerprint()
