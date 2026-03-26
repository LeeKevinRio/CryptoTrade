"""RSI 相對強弱指標"""

import pandas as pd
import numpy as np


def calculate_rsi(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
    delta = df[column].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(np.nan)
    return rsi


def is_oversold(df: pd.DataFrame, threshold: float = 30, period: int = 14) -> bool:
    rsi = calculate_rsi(df, period)
    if rsi.empty:
        return False
    val = rsi.iloc[-1]
    if pd.isna(val):
        return False
    return float(val) < threshold


def is_overbought(df: pd.DataFrame, threshold: float = 70, period: int = 14) -> bool:
    rsi = calculate_rsi(df, period)
    if rsi.empty:
        return False
    val = rsi.iloc[-1]
    if pd.isna(val):
        return False
    return float(val) > threshold


def get_current_rsi(df: pd.DataFrame, period: int = 14) -> float | None:
    rsi = calculate_rsi(df, period)
    if rsi.empty or pd.isna(rsi.iloc[-1]):
        return None
    return float(rsi.iloc[-1])
