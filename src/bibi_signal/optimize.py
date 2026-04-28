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
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
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


# ---------- apply best combo to config.yaml ----------

_PARAMS_TO_APPLY: tuple[str, ...] = (
    "dip_percent",
    "profit_percent",
    "stop_loss_percent",
    "require_rsi_oversold",
    "rsi_threshold",
    "require_uptrend",
    "trailing_take_profit",
    "trail_percent",
)


def _format_yaml_value(v) -> str:
    """Render a Python value the way YAML expects."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        # Avoid scientific notation; trim trailing zeros without losing precision.
        s = f"{v:.6f}".rstrip("0").rstrip(".")
        return s if s else "0"
    return str(v)


def apply_combo_to_yaml(yaml_path: Path, ticker: str, combo: Combo) -> list[str]:
    """Rewrite the ticker's strategy params in config.yaml.

    Preserves comments and unrelated keys. Backs up the original text in
    memory; if the result fails to parse as a valid StrategyConfig, the
    original is restored and a RuntimeError is raised.

    Returns a list of human-readable change descriptions ("dip_percent: 0.02 → 0.03").
    """
    backup = yaml_path.read_text()
    lines = backup.splitlines()
    updates: dict[str, object] = {p: getattr(combo, p) for p in _PARAMS_TO_APPLY}

    in_block = False
    block_indent = -1
    changes: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Detect ticker header — e.g. "  QQQ:" with no further content.
        if stripped == f"{ticker}:":
            in_block = True
            block_indent = len(line) - len(line.lstrip())
            continue
        if not in_block:
            continue
        if not stripped:
            continue  # blank line, stay in block
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= block_indent:
            in_block = False
            continue
        # Inside the ticker's block.
        if ":" not in stripped:
            continue
        param = stripped.split(":", 1)[0].strip()
        if param not in updates:
            continue
        new_val = updates[param]
        formatted = _format_yaml_value(new_val)
        old_value = stripped.split(":", 1)[1].split("#")[0].strip()
        # Compare YAML values, not raw lines, so whitespace before a
        # comment doesn't get mistaken for a real change.
        if old_value == formatted:
            continue
        # Preserve trailing comment exactly.
        colon_idx = line.find(":")
        comment_idx = line.find("#", colon_idx)
        comment_suffix = ""
        if comment_idx != -1:
            comment_suffix = "  " + line[comment_idx:].rstrip()
        lines[i] = f"{' ' * line_indent}{param}: {formatted}{comment_suffix}"
        changes.append(f"{param}: {old_value} → {formatted}")

    yaml_path.write_text("\n".join(lines) + "\n")

    # Validate the result; restore the backup if anything is wrong.
    try:
        from .config import StrategyConfig
        StrategyConfig.from_yaml(yaml_path)
    except Exception as e:
        yaml_path.write_text(backup)
        raise RuntimeError(f"YAML invalid after apply, restored original: {e}") from e

    return changes


# ---------- CLI hook ----------

def maybe_apply(
    report: OptimizeReport,
    yaml_path: Path,
    apply_rank: int | None,
    interactive: bool,
) -> None:
    """If --apply was given, or stdin is a TTY and the user picks one,
    write the chosen combo into config.yaml and print a diff."""
    rank = apply_rank
    if rank is None and interactive and report.rows:
        try:
            raw = input(
                "\nApply which rank to config.yaml? "
                "(rank number, or empty to skip): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            return
        try:
            rank = int(raw)
        except ValueError:
            print(f"  not a number: {raw!r} — skipping")
            return
    if rank is None:
        return
    if not 1 <= rank <= len(report.rows):
        print(f"  rank {rank} out of range (1..{len(report.rows)})")
        return
    combo: Combo = report.rows[rank - 1]["combo"]
    try:
        changes = apply_combo_to_yaml(yaml_path, report.ticker, combo)
    except RuntimeError as e:
        print(f"  ✗ apply failed: {e}")
        return
    if not changes:
        print(f"\n  {yaml_path} already matches rank #{rank} — nothing to change")
        return
    print(f"\n  ✓ Applied rank #{rank} to {yaml_path} for {report.ticker}:")
    for c in changes:
        print(f"    {c}")
    print("  Run `bibi-signal --mode backtest` to verify the new params.")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Grid-search strategy parameters with train/test split.")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--starting-cash", type=float, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--train-frac", type=float, default=0.7)
    parser.add_argument(
        "--apply",
        type=int,
        default=None,
        help="apply rank N (1-based) to config.yaml automatically; "
             "if omitted and stdin is a TTY, you'll be prompted",
    )
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
    maybe_apply(report, Path(args.config), args.apply, interactive=sys.stdin.isatty())


if __name__ == "__main__":
    cli()
