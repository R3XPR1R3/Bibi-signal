"""Daily-bar backtest of the Ladder Swing strategy.

Replays the same evaluate() logic over historical bars so you can see how
the strategy would have behaved on years of QQQ/XLE data before risking
real money.

Approximations vs. live:
    - One bar per day (not intraday). Targets/stops checked against bar's
      High/Low (intraday touch) or Close (conservative).
    - Fills assumed at the trigger price (no slippage). Acceptable for a
      strategy that uses limit-style targets but be aware.
    - No commissions/spreads (Robinhood is $0 commission).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .config import StrategyConfig, TickerConfig
from .indicators import atr, rsi, sma
from .price_fetcher import get_history
from .strategy import (
    LotSnapshot,
    MarketSnapshot,
    SignalKind,
    SignalProposal,
    compute_targets,
    evaluate,
)


@dataclass
class BTLot:
    id: int
    buy_price: float
    quantity: float
    target_price: float
    stop_price: float
    opened_index: int
    closed_index: Optional[int] = None
    sell_price: Optional[float] = None

    def realised_pnl(self) -> float:
        if self.sell_price is None:
            return 0.0
        return (self.sell_price - self.buy_price) * self.quantity


@dataclass
class BTState:
    cash: float
    next_lot_id: int = 1
    lots: list[BTLot] = field(default_factory=list)

    def open_lots(self) -> list[BTLot]:
        return [lot for lot in self.lots if lot.closed_index is None]

    def closed_lots(self) -> list[BTLot]:
        return [lot for lot in self.lots if lot.closed_index is not None]


@dataclass
class BTResult:
    ticker: str
    bars: int
    final_cash: float
    open_value: float
    total_equity: float
    realised_pnl: float
    unrealised_pnl: float
    n_buys: int
    n_sells: int
    n_stops: int
    win_rate: float
    avg_win: float
    avg_loss: float
    max_drawdown: float
    buy_hold_equity: float
    equity_curve: pd.Series

    def summary(self) -> str:
        lines = [
            f"=== Backtest: {self.ticker} ({self.bars} bars) ===",
            f"  Final cash:        ${self.final_cash:,.2f}",
            f"  Open lots value:   ${self.open_value:,.2f}",
            f"  Total equity:      ${self.total_equity:,.2f}",
            f"  Realised P&L:      ${self.realised_pnl:,.2f}",
            f"  Unrealised P&L:    ${self.unrealised_pnl:,.2f}",
            f"  Buys / Sells / Stops: {self.n_buys} / {self.n_sells} / {self.n_stops}",
            f"  Win rate:          {self.win_rate*100:.1f}%",
            f"  Avg win / loss:    ${self.avg_win:,.2f} / ${self.avg_loss:,.2f}",
            f"  Max drawdown:      {self.max_drawdown*100:.2f}%",
            f"  Buy & hold equity: ${self.buy_hold_equity:,.2f}",
        ]
        return "\n".join(lines)


def _to_lot_snapshots(lots: list[BTLot], ticker: str) -> list[LotSnapshot]:
    return [
        LotSnapshot(
            id=lot.id,
            ticker=ticker,
            buy_price=lot.buy_price,
            quantity=lot.quantity,
            target_price=lot.target_price,
            stop_price=lot.stop_price,
        )
        for lot in lots
    ]


def run_backtest(
    ticker: str,
    cfg: TickerConfig,
    starting_cash: float,
    period: str = "5y",
    interval: str = "1d",
) -> BTResult:
    df = get_history(ticker, period=period, interval=interval, use_cache=False)
    return run_backtest_on_df(ticker, cfg, starting_cash, df)


def run_backtest_on_df(
    ticker: str,
    cfg: TickerConfig,
    starting_cash: float,
    df: pd.DataFrame,
) -> BTResult:
    """Same as run_backtest but takes pre-fetched OHLCV. Used by the optimizer."""
    df = df.dropna(subset=["Close"]).copy()

    df["sma_long"] = sma(df["Close"], cfg.sma_long_period)
    df["rsi"] = rsi(df["Close"], 14)
    df["atr"] = atr(df, 14)

    state = BTState(cash=starting_cash)
    equity_curve = []

    n_buys = n_sells = n_stops = 0
    wins: list[float] = []
    losses: list[float] = []

    for i, (idx, row) in enumerate(df.iterrows()):
        price = float(row["Close"])
        high = float(row["High"])
        low = float(row["Low"])

        # 1. Check exits using intraday H/L (more realistic than close-only).
        for lot in list(state.open_lots()):
            fill_price: Optional[float] = None
            kind: Optional[SignalKind] = None

            if low <= lot.stop_price:
                fill_price = lot.stop_price
                kind = SignalKind.STOP
            elif high >= lot.target_price:
                fill_price = lot.target_price
                kind = SignalKind.SELL

            if fill_price is not None:
                proceeds = fill_price * lot.quantity
                state.cash += proceeds
                lot.sell_price = fill_price
                lot.closed_index = i
                pnl = lot.realised_pnl()
                if kind == SignalKind.SELL:
                    n_sells += 1
                else:
                    n_stops += 1
                (wins if pnl >= 0 else losses).append(pnl)

        # 2. Skip until indicators warm up.
        if pd.isna(row["sma_long"]) or pd.isna(row["rsi"]):
            equity_curve.append(_equity(state, price))
            continue

        # 3. Entry decision via the same evaluate() the live bot uses.
        market = MarketSnapshot(
            ticker=ticker,
            price=price,
            sma_long=float(row["sma_long"]),
            rsi=float(row["rsi"]),
            atr=float(row["atr"]) if not pd.isna(row["atr"]) else None,
            in_uptrend=price > float(row["sma_long"]),
        )
        proposals = evaluate(
            cfg,
            market,
            _to_lot_snapshots(state.open_lots(), ticker),
            state.cash,
        )
        for p in proposals:
            if p.kind == SignalKind.BUY and p.suggested_usd:
                size_usd = min(p.suggested_usd, state.cash)
                if size_usd < cfg.min_trade_usd:
                    continue
                qty = size_usd / price
                target, stop = compute_targets(price, cfg)
                state.lots.append(
                    BTLot(
                        id=state.next_lot_id,
                        buy_price=price,
                        quantity=qty,
                        target_price=target,
                        stop_price=stop,
                        opened_index=i,
                    )
                )
                state.next_lot_id += 1
                state.cash -= size_usd
                n_buys += 1

        equity_curve.append(_equity(state, price))

    final_price = float(df["Close"].iloc[-1])
    open_value = sum(lot.quantity * final_price for lot in state.open_lots())
    realised = sum(lot.realised_pnl() for lot in state.closed_lots())
    unrealised = sum(
        (final_price - lot.buy_price) * lot.quantity for lot in state.open_lots()
    )
    total_equity = state.cash + open_value

    eq_series = pd.Series(equity_curve, index=df.index[: len(equity_curve)])
    peak = eq_series.cummax()
    drawdown = (eq_series - peak) / peak.replace(0, np.nan)
    max_dd = float(drawdown.min()) if not drawdown.empty else 0.0

    buy_hold_qty = starting_cash / float(df["Close"].iloc[0])
    buy_hold_equity = buy_hold_qty * final_price

    win_rate = len(wins) / max(1, len(wins) + len(losses))
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0

    return BTResult(
        ticker=ticker,
        bars=len(df),
        final_cash=state.cash,
        open_value=open_value,
        total_equity=total_equity,
        realised_pnl=realised,
        unrealised_pnl=unrealised,
        n_buys=n_buys,
        n_sells=n_sells,
        n_stops=n_stops,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        max_drawdown=max_dd,
        buy_hold_equity=buy_hold_equity,
        equity_curve=eq_series,
    )


def _equity(state: BTState, price: float) -> float:
    return state.cash + sum(lot.quantity * price for lot in state.open_lots())


def cli() -> None:
    parser = argparse.ArgumentParser(description="Backtest the Ladder Swing strategy.")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--starting-cash", type=float, default=None)
    args = parser.parse_args()

    sc = StrategyConfig.from_yaml(args.config)
    if args.ticker not in sc.tickers:
        raise SystemExit(f"{args.ticker} not in config.yaml tickers: {list(sc.tickers)}")

    starting_cash = args.starting_cash if args.starting_cash else sc.starting_cash
    period = f"{args.years}y"
    result = run_backtest(args.ticker, sc.tickers[args.ticker], starting_cash, period=period)
    print(result.summary())


if __name__ == "__main__":
    cli()
