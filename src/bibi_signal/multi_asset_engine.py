"""Multi-asset rotation strategy (Dual Momentum).

Concept (Gary Antonacci):
    1. Maintain a universe of ETFs (e.g., QQQ, XLE, SCHD, IWM, GLD).
    2. Each rebalance period, rank assets by recent return (e.g., 90 days).
    3. Hold the top-N — but only if their price is above their long SMA
       (absolute momentum filter). If none qualify, sit in cash.
    4. When ranking changes, signal a rebalance: sell the dropouts, buy
       the new winners.

Why this works:
    Capital naturally rotates between sectors as macro conditions shift.
    Tech crashes 2022 → energy outperforms. Bear market → bonds/gold or
    cash. The strategy follows that flow mechanically rather than trying
    to predict it.

This module is **pure** (no I/O). Backtest and any future paper/live
runner pass it OHLCV history; it returns a target allocation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

from .config import MultiAssetConfig

CASH = "CASH"


@dataclass(frozen=True)
class AssetSnapshot:
    """One asset's state at a point in time."""
    ticker: str
    price: float
    return_lookback: float       # fractional return over lookback_days
    above_sma_long: bool          # absolute momentum filter
    has_data: bool                # False if not enough history yet


@dataclass(frozen=True)
class TargetAllocation:
    """Bot's chosen allocation for the next holding period.

    weights map ticker -> fraction of portfolio (sums to 1.0). CASH is a
    special key meaning "stay in cash for this slice."
    """
    weights: dict[str, float]
    reason: str
    snapshots: list[AssetSnapshot]

    def is_all_cash(self) -> bool:
        return self.weights == {CASH: 1.0}


@dataclass(frozen=True)
class RebalanceSignal:
    """One concrete instruction to move capital."""
    sell_ticker: Optional[str]   # what to liquidate
    buy_ticker: Optional[str]    # what to enter
    weight: float
    reason: str


def compute_snapshot(
    ticker: str,
    history: pd.DataFrame,
    cfg: MultiAssetConfig,
    as_of_index: int | None = None,
) -> AssetSnapshot:
    """Snapshot for a given asset at a given bar (default = last bar).

    history must have a "Close" column and at least max(lookback,sma) bars
    of data prior to as_of_index.
    """
    closes = history["Close"]
    if as_of_index is None:
        as_of_index = len(closes) - 1
    if as_of_index < cfg.lookback_days or as_of_index < cfg.sma_long_period:
        return AssetSnapshot(
            ticker=ticker, price=float(closes.iloc[as_of_index]),
            return_lookback=0.0, above_sma_long=False, has_data=False,
        )
    price = float(closes.iloc[as_of_index])
    past = float(closes.iloc[as_of_index - cfg.lookback_days])
    ret = (price - past) / past if past > 0 else 0.0
    sma_val = float(closes.iloc[as_of_index - cfg.sma_long_period: as_of_index].mean())
    return AssetSnapshot(
        ticker=ticker, price=price,
        return_lookback=ret,
        above_sma_long=price > sma_val,
        has_data=True,
    )


def select_target(
    cfg: MultiAssetConfig,
    snapshots: list[AssetSnapshot],
) -> TargetAllocation:
    """Pick the top-N assets by lookback return that pass the SMA filter.

    If no asset passes the SMA filter, target = 100% cash (defensive).
    """
    valid = [s for s in snapshots if s.has_data and s.above_sma_long]
    if not valid:
        reasons = []
        for s in snapshots:
            if not s.has_data:
                reasons.append(f"{s.ticker}=warmup")
            elif not s.above_sma_long:
                reasons.append(f"{s.ticker}<SMA")
        return TargetAllocation(
            weights={CASH: 1.0},
            reason="defensive: no asset above its long SMA  (" + ", ".join(reasons) + ")",
            snapshots=snapshots,
        )
    valid.sort(key=lambda s: s.return_lookback, reverse=True)
    selected = valid[: cfg.top_n]
    weight_each = 1.0 / len(selected)
    weights = {s.ticker: weight_each for s in selected}
    top_summary = ", ".join(
        f"{s.ticker} {s.return_lookback*100:+.1f}%" for s in selected
    )
    return TargetAllocation(
        weights=weights,
        reason=f"top-{cfg.top_n} momentum: {top_summary}",
        snapshots=snapshots,
    )


def diff_allocation(
    current: dict[str, float],
    target: dict[str, float],
    *,
    threshold: float = 0.05,
) -> list[RebalanceSignal]:
    """Convert (current → target) into a minimal list of trades.

    `threshold` lets us ignore tiny weight drifts (e.g., 49% vs 51%) so
    we don't churn fees on rounding noise.
    """
    signals: list[RebalanceSignal] = []
    all_tickers = set(current) | set(target)
    deltas: dict[str, float] = {}
    for t in all_tickers:
        delta = target.get(t, 0.0) - current.get(t, 0.0)
        if abs(delta) >= threshold:
            deltas[t] = delta
    # Sells first (frees cash), then buys.
    sells = [(t, -d) for t, d in deltas.items() if d < 0 and t != CASH]
    buys = [(t, d) for t, d in deltas.items() if d > 0 and t != CASH]
    for t, w in sells:
        signals.append(RebalanceSignal(
            sell_ticker=t, buy_ticker=None, weight=w,
            reason=f"reduce {t} by {w*100:.1f}%"
        ))
    for t, w in buys:
        signals.append(RebalanceSignal(
            sell_ticker=None, buy_ticker=t, weight=w,
            reason=f"add {t} by {w*100:.1f}%"
        ))
    return signals


# ---------- backtest ----------

@dataclass
class MultiAssetState:
    cash: float
    holdings: dict[str, float]  # ticker -> shares
    next_rebalance_index: int


@dataclass
class MultiAssetResult:
    universe: list[str]
    bars: int
    starting_cash: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    n_rebalances: int
    days_in_cash: int
    days_per_ticker: dict[str, int]   # how many days held each ticker
    benchmark_qqq_pct: float          # buy-and-hold QQQ over same period
    equity_curve: pd.Series

    def summary(self) -> str:
        lines = [
            f"=== Multi-asset Dual Momentum  ({self.bars} bars) ===",
            f"  Universe:          {', '.join(self.universe)}",
            f"  Final equity:      ${self.final_equity:,.2f}",
            f"  Total return:      {self.total_return_pct:+.2f}%",
            f"  Max drawdown:      {self.max_drawdown_pct:.2f}%",
            f"  Rebalances:        {self.n_rebalances}",
            f"  Days in CASH:      {self.days_in_cash}",
            "  Time in each asset:",
        ]
        for t, d in sorted(self.days_per_ticker.items(), key=lambda x: x[1], reverse=True):
            pct = d / max(1, self.bars) * 100
            lines.append(f"    {t:<8} {d:>5d} bars ({pct:.1f}%)")
        lines.append(f"  vs QQQ buy & hold: {self.benchmark_qqq_pct:+.2f}%")
        return "\n".join(lines)


def run_multi_asset_backtest(
    cfg: MultiAssetConfig,
    histories: dict[str, pd.DataFrame],
) -> MultiAssetResult:
    """Replay history with weekly rebalance.

    `histories` maps ticker -> OHLCV DataFrame (must include "Close").
    All frames must share the same index (calendar-aligned).
    """
    if not histories:
        raise ValueError("histories is empty")
    universe = list(cfg.universe)
    # Align all dataframes on common index (intersection).
    common_index = None
    for df in histories.values():
        common_index = df.index if common_index is None else common_index.intersection(df.index)
    if common_index is None or len(common_index) == 0:
        raise ValueError("no overlapping calendar between universe assets")
    aligned = {t: histories[t].loc[common_index] for t in universe if t in histories}

    bars = len(common_index)
    state = MultiAssetState(cash=cfg.starting_cash, holdings={}, next_rebalance_index=0)
    equity_curve = []
    n_rebal = 0
    days_in_cash = 0
    days_per_ticker: dict[str, int] = {t: 0 for t in universe}
    peak = cfg.starting_cash
    max_dd = 0.0

    def equity_at(i: int) -> float:
        eq = state.cash
        for t, shares in state.holdings.items():
            if shares <= 0:
                continue
            eq += shares * float(aligned[t]["Close"].iloc[i])
        return eq

    for i in range(bars):
        if i >= state.next_rebalance_index:
            # Rebalance using snapshots taken at bar i-1 (use today's OPEN
            # but we approximate with prior close to avoid look-ahead).
            evaluation_index = max(0, i - 1)
            snapshots = [
                compute_snapshot(t, aligned[t], cfg, as_of_index=evaluation_index)
                for t in universe
            ]
            target = select_target(cfg, snapshots)
            # Liquidate everything (simplification: close all positions, then buy target).
            for t, shares in list(state.holdings.items()):
                if shares <= 0:
                    continue
                price = float(aligned[t]["Close"].iloc[i])
                state.cash += shares * price
                state.holdings[t] = 0.0
            # Allocate per target weights.
            equity = state.cash
            for t, w in target.weights.items():
                if t == CASH or w <= 0:
                    continue
                price = float(aligned[t]["Close"].iloc[i])
                if price <= 0:
                    continue
                dollars = equity * w
                shares = dollars / price
                state.holdings[t] = state.holdings.get(t, 0.0) + shares
                state.cash -= dollars
            state.next_rebalance_index = i + cfg.rebalance_frequency_days
            n_rebal += 1

        # Track time in each asset.
        if state.cash > 0 and not state.holdings:
            days_in_cash += 1
        else:
            for t, shares in state.holdings.items():
                if shares > 0:
                    days_per_ticker[t] = days_per_ticker.get(t, 0) + 1
                    break

        eq = equity_at(i)
        equity_curve.append(eq)
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    final_equity = equity_curve[-1] if equity_curve else cfg.starting_cash
    total_ret = (final_equity - cfg.starting_cash) / cfg.starting_cash * 100

    # QQQ benchmark
    bench_pct = 0.0
    if "QQQ" in aligned and len(aligned["QQQ"]) > 0:
        first_qqq = float(aligned["QQQ"]["Close"].iloc[0])
        last_qqq = float(aligned["QQQ"]["Close"].iloc[-1])
        bench_pct = (last_qqq - first_qqq) / first_qqq * 100

    return MultiAssetResult(
        universe=universe,
        bars=bars,
        starting_cash=cfg.starting_cash,
        final_equity=final_equity,
        total_return_pct=total_ret,
        max_drawdown_pct=max_dd * 100,
        n_rebalances=n_rebal,
        days_in_cash=days_in_cash,
        days_per_ticker=days_per_ticker,
        benchmark_qqq_pct=bench_pct,
        equity_curve=pd.Series(equity_curve, index=common_index),
    )
