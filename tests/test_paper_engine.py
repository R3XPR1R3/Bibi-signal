"""Tests for the in-database paper-trading engine."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import sessionmaker

from bibi_signal import database as db
from bibi_signal import paper_engine
from bibi_signal.config import TickerConfig
from bibi_signal.database import Environment, init_db
from bibi_signal.strategy import MarketSnapshot


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


def make_market(price: float) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="QQQ",
        price=price,
        sma_long=100.0,
        rsi=50.0,
        atr=1.0,
        in_uptrend=True,
    )


@pytest.fixture
def session_factory(tmp_path) -> sessionmaker:
    url = f"sqlite:///{tmp_path / 'test.db'}"
    return init_db(url)


def test_paper_seeded_idempotent(session_factory):
    with session_factory() as s:
        paper_engine.ensure_paper_seeded(s, Decimal("500"))
        paper_engine.ensure_paper_seeded(s, Decimal("999"))  # second call ignored
        s.commit()
        assert db.get_cash(s, Environment.PAPER) == Decimal("500")


def test_paper_buy_records_lot_and_decreases_cash(session_factory):
    cfg = make_cfg()
    with session_factory() as s:
        paper_engine.ensure_paper_seeded(s, Decimal("100"))
        s.commit()
        applied = paper_engine.evaluate_paper(s, cfg, make_market(price=100.0))
        s.commit()
        assert len(applied) == 1
        assert applied[0].kind.value == "BUY"
        opens = db.open_lots(s, Environment.PAPER, "QQQ")
        assert len(opens) == 1
        # Spent ~$15 (15% of $100, clamped to [5, 20])
        cash = db.get_cash(s, Environment.PAPER)
        assert Decimal("80") <= cash <= Decimal("95")


def test_paper_sell_when_target_hit(session_factory):
    cfg = make_cfg(profit_percent=0.04)
    with session_factory() as s:
        paper_engine.ensure_paper_seeded(s, Decimal("100"))
        s.commit()
        # Buy at $100
        paper_engine.evaluate_paper(s, cfg, make_market(price=100.0))
        s.commit()
        # Price jumps above target ($104)
        applied = paper_engine.evaluate_paper(s, cfg, make_market(price=105.0))
        s.commit()
        assert any(p.kind.value == "SELL" for p in applied)
        opens = db.open_lots(s, Environment.PAPER, "QQQ")
        assert len(opens) == 0


def test_paper_summary_counts_open_lots(session_factory):
    cfg = make_cfg()
    with session_factory() as s:
        paper_engine.ensure_paper_seeded(s, Decimal("100"))
        s.commit()
        paper_engine.evaluate_paper(s, cfg, make_market(price=100.0))
        s.commit()
        summary = paper_engine.paper_summary(s, current_prices={"QQQ": 102.0})
        assert summary["open_lots"] == 1
        assert summary["unrealised_pnl"] > 0
