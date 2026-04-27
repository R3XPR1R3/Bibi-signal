"""Technical indicators. Plain pandas/numpy — no TA-Lib dependency."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(window=period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    # Wilder's smoothing: equivalent to EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range. Expects columns: High, Low, Close."""
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def latest_indicators(
    df: pd.DataFrame,
    sma_long_period: int = 200,
    rsi_period: int = 14,
    atr_period: int = 14,
) -> dict[str, float]:
    """Snapshot of indicators at the most recent bar."""
    close = df["Close"]
    sma_long = sma(close, sma_long_period).iloc[-1]
    rsi_v = rsi(close, rsi_period).iloc[-1]
    atr_v = atr(df, atr_period).iloc[-1]
    last_close = float(close.iloc[-1])
    return {
        "close": last_close,
        "sma_long": float(sma_long) if not np.isnan(sma_long) else float("nan"),
        "rsi": float(rsi_v) if not np.isnan(rsi_v) else float("nan"),
        "atr": float(atr_v) if not np.isnan(atr_v) else float("nan"),
        "in_uptrend": (not np.isnan(sma_long)) and last_close > sma_long,
    }
