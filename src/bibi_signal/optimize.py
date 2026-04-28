"""Parameter optimizer with train/test split.

This is NOT trading and NOT prediction. It's a grid search over historical
data to find which parameter combination would have worked best in the past,
with an out-of-sample (test) check to detect overfitting.

Workflow:
    1. Fetch N years of OHLCV once.
    2. Split into train (70%) and test (30%).
    3. Generate every combination of strategy params from the grid.
    4. Run a backtest on TRAIN for each combination — in parallel across cores.
    5. For the top K by train-score, also run on TEST.
    6. Print a table sorted by test-score so the user can pick a robust combo.

We avoid running ALL combos on test because:
    a) test data is reserved for honest out-of-sample evaluation,
    b) running every combo on test reintroduces the overfitting we were trying
       to detect.
"""
from __future__ import annotations

import argparse
import itertools
import math
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from .backtest import BTResult, run_backtest_on_df
from .config import StrategyConfig, TickerConfig
from .price_fetcher import get_history


# ---------- parameter grid ----------

DEFAULT_GRID: dict[str, list] = {
    "dip_percent": [0.01, 0.02, 0.03, 0.05, 0.08],
    "profit_percent": [0.03, 0.05, 0.08, 0.12, 0.20],
    "stop_loss_percent": [0.10, 0.15, 0.25],
    "require_rsi_oversold": [True, False],
    "rsi_threshold": [30, 35, 50, 70],
    "require_uptrend": [True, False],
    "trailing_take_profit": [False, True],
    "trail_percent": [0.02, 0.05, 0.10],
}


@dataclass(frozen=True)
class Combo:
    dip_percent: float
    profit_percent: float
    stop_loss_percent: float
    require_rsi_oversold: bool
    rsi_threshold: float
    require_uptrend: bool
    trailing_take_profit: bool
    trail_percent: float

    def to_ticker_config(self, *, base: TickerConfig) -> TickerConfig:
        """Apply this combo on top of a baseline config."""
        return TickerConfig(
            enabled=True,
            dip_percent=self.dip_percent,
            profit_percent=self.profit_percent,
            stop_loss_percent=self.stop_loss_percent,
            min_trade_usd=base.min_trade_usd,
            max_trade_usd=base.max_trade_usd,
            max_open_lots=base.max_open_lots,
            use_atr_sizing=False,
            atr_k=base.atr_k,
            require_rsi_oversold=self.require_rsi_oversold,
            rsi_threshold=self.rsi_threshold,
            require_uptrend=self.require_uptrend,
            sma_long_period=base.sma_long_period,
            trailing_take_profit=self.trailing_take_profit,
            trail_percent=self.trail_percent,
        )


def generate_combos(grid: dict[str, list] | None = None) -> list[Combo]:
    grid = grid or DEFAULT_GRID
    keys = list(grid)
    out: list[Combo] = []
    for values in itertools.product(*(grid[k] for k in keys)):
        kw = dict(zip(keys, values))
        # Dedupe dominated combos:
        #   - When RSI filter is OFF, threshold doesn't matter — keep one.
        #   - When trailing is OFF, trail_percent doesn't matter — keep one.
        if not kw["require_rsi_oversold"] and kw["rsi_threshold"] != grid["rsi_threshold"][0]:
            continue
        if not kw["trailing_take_profit"] and kw["trail_percent"] != grid["trail_percent"][0]:
            continue
        out.append(Combo(**kw))
    return out


# ---------- scoring ----------

@dataclass(frozen=True)
class Score:
    """Composite score for picking robust combos.

    return_pct  — total return over starting cash, in percent
    calmar      — return_pct / |max_drawdown_pct|. Higher = better risk-adjusted
    n_trades    — number of completed buys; used to penalise too-few-trade combos
    score       — composite: calmar with smoothed penalty for low trade count
    """
    return_pct: float
    max_drawdown_pct: float
    n_trades: int
    buy_hold_pct: float
    score: float


def _score(result: BTResult, starting_cash: float) -> Score:
    return_pct = (result.total_equity - starting_cash) / starting_cash * 100
    bh_pct = (result.buy_hold_equity - starting_cash) / starting_cash * 100
    dd_pct = abs(result.max_drawdown) * 100
    n_trades = result.n_buys

    # Calmar-like: reward per unit of pain.
    if dd_pct < 0.01:
        calmar = return_pct  # no drawdown observed, fall back to raw return
    else:
        calmar = return_pct / dd_pct

    # Trade-count penalty: combos with <5 trades over the whole window are
    # statistical noise. Smooth with sqrt rather than hard cutoff.
    trade_factor = min(1.0, math.sqrt(max(0, n_trades) / 10))
    composite = calmar * trade_factor

    return Score(
        return_pct=return_pct,
        max_drawdown_pct=dd_pct,
        n_trades=n_trades,
        buy_hold_pct=bh_pct,
        score=composite,
    )


# ---------- worker ----------

def _split_train_test(df: pd.DataFrame, train_frac: float = 0.7) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.dropna(subset=["Close"])
    cut = int(len(df) * train_frac)
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()


def _run_one(
    combo: Combo, base: TickerConfig, ticker: str, starting_cash: float, df: pd.DataFrame
) -> tuple[Combo, Score]:
    cfg = combo.to_ticker_config(base=base)
    res = run_backtest_on_df(ticker, cfg, starting_cash, df)
    return combo, _score(res, starting_cash)


# Module-level wrapper because ProcessPoolExecutor needs picklable callables.
def _worker_train(args):
    return _run_one(*args)


# ---------- main optimizer ----------

@dataclass
class OptimizeReport:
    ticker: str
    train_bars: int
    test_bars: int
    starting_cash: float
    rows: list[dict]  # one per top-K combo: train + test fields

    def render(self, top: int = 10) -> str:
        rows = self.rows[:top]
        header = (
            f"=== Optimize {self.ticker}  "
            f"train {self.train_bars}d / test {self.test_bars}d  "
            f"start ${self.starting_cash:.0f} ===\n"
        )
        col = (
            f"{'rank':>4}  "
            f"{'dip':>5} {'profit':>6} {'stop':>5} "
            f"{'rsi':>10} {'up':>3} {'trail':>7} | "
            f"{'tr ret%':>7} {'tr dd%':>6} {'tr#':>4} | "
            f"{'te ret%':>7} {'te dd%':>6} {'te#':>4} | "
            f"{'te BH%':>7}  overfit"
        )
        lines = [header, col, "-" * len(col)]
        for i, r in enumerate(rows, 1):
            c: Combo = r["combo"]
            tr: Score = r["train"]
            te: Score = r["test"]
            rsi_label = f"<{int(c.rsi_threshold)}" if c.require_rsi_oversold else "off"
            up = "yes" if c.require_uptrend else "no"
            trail = f"{c.trail_percent*100:.1f}%" if c.trailing_take_profit else "off"
            overfit = "⚠️" if (tr.return_pct - te.return_pct) > max(20, abs(te.return_pct)) else " "
            lines.append(
                f"{i:>4}  "
                f"{c.dip_percent*100:>5.1f} {c.profit_percent*100:>6.1f} "
                f"{c.stop_loss_percent*100:>5.1f} "
                f"{rsi_label:>10} {up:>3} {trail:>7} | "
                f"{tr.return_pct:>7.1f} {tr.max_drawdown_pct:>6.1f} {tr.n_trades:>4d} | "
                f"{te.return_pct:>7.1f} {te.max_drawdown_pct:>6.1f} {te.n_trades:>4d} | "
                f"{te.buy_hold_pct:>7.1f}  {overfit}"
            )
        lines.append("")
        lines.append("Pick a row where TEST return is positive AND not flagged ⚠️.")
        lines.append("Edit config.yaml with those values and re-run --mode backtest to confirm.")
        return "\n".join(lines)


def optimize_ticker(
    ticker: str,
    base: TickerConfig,
    starting_cash: float,
    period: str = "5y",
    workers: int | None = None,
    top_k: int = 30,
    train_frac: float = 0.7,
    grid: dict[str, list] | None = None,
) -> OptimizeReport:
    df = get_history(ticker, period=period, interval="1d", use_cache=False)
    train_df, test_df = _split_train_test(df, train_frac)

    combos = generate_combos(grid)
    print(f"[optimize] {ticker}: {len(combos)} combos, "
          f"train {len(train_df)} bars, test {len(test_df)} bars")

    # Phase 1: run all combos on TRAIN in parallel.
    workers = workers or max(1, (mp.cpu_count() or 2) - 1)
    train_results: list[tuple[Combo, Score]] = []
    args = [(c, base, ticker, starting_cash, train_df) for c in combos]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed(ex.submit(_worker_train, a) for a in args):
            train_results.append(fut.result())

    train_results.sort(key=lambda x: x[1].score, reverse=True)
    top = train_results[:top_k]

    # Phase 2: run only top-K on TEST (sequential — small set).
    rows: list[dict] = []
    for combo, train_score in top:
        cfg = combo.to_ticker_config(base=base)
        test_res = run_backtest_on_df(ticker, cfg, starting_cash, test_df)
        test_score = _score(test_res, starting_cash)
        rows.append({"combo": combo, "train": train_score, "test": test_score})

    # Sort the displayed rows by TEST score so robust combos float up.
    rows.sort(key=lambda r: r["test"].score, reverse=True)

    return OptimizeReport(
        ticker=ticker,
        train_bars=len(train_df),
        test_bars=len(test_df),
        starting_cash=starting_cash,
        rows=rows,
    )


# ---------- CLI hook ----------

def cli() -> None:
    parser = argparse.ArgumentParser(description="Grid-search strategy parameters with train/test split.")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--starting-cash", type=float, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--train-frac", type=float, default=0.7)
    args = parser.parse_args()

    sc = StrategyConfig.from_yaml(args.config)
    if args.ticker not in sc.tickers:
        raise SystemExit(f"{args.ticker} not in config.yaml tickers: {list(sc.tickers)}")

    starting = args.starting_cash if args.starting_cash else sc.starting_cash
    report = optimize_ticker(
        args.ticker,
        sc.tickers[args.ticker],
        starting,
        period=f"{args.years}y",
        workers=args.workers,
        top_k=max(args.top, 30),
        train_frac=args.train_frac,
    )
    print(report.render(top=args.top))


if __name__ == "__main__":
    cli()
