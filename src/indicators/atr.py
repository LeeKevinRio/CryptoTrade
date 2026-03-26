"""ATR (Average True Range) — 用於動態停損"""

import pandas as pd
import numpy as np


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period).mean()
    return atr


def get_current_atr(df: pd.DataFrame, period: int = 14) -> float | None:
    atr = calculate_atr(df, period)
    if atr.empty or pd.isna(atr.iloc[-1]):
        return None
    return float(atr.iloc[-1])
