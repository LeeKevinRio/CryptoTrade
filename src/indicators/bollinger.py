"""布林通道"""

import pandas as pd
import numpy as np


def calculate_bollinger(
    df: pd.DataFrame, period: int = 20, std_dev: float = 2.0, column: str = "close"
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (upper, middle, lower)"""
    middle = df[column].rolling(window=period).mean()
    std = df[column].rolling(window=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def is_below_lower_band(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> bool:
    upper, middle, lower = calculate_bollinger(df, period, std_dev)
    if lower.empty or pd.isna(lower.iloc[-1]):
        return False
    return float(df["close"].iloc[-1]) <= float(lower.iloc[-1])


def is_above_upper_band(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> bool:
    upper, middle, lower = calculate_bollinger(df, period, std_dev)
    if upper.empty or pd.isna(upper.iloc[-1]):
        return False
    return float(df["close"].iloc[-1]) >= float(upper.iloc[-1])


def get_bb_position(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> float | None:
    """返回價格在布林通道中的位置 (0=下軌, 0.5=中軌, 1=上軌)"""
    upper, middle, lower = calculate_bollinger(df, period, std_dev)
    if upper.empty or pd.isna(upper.iloc[-1]):
        return None
    u = float(upper.iloc[-1])
    l = float(lower.iloc[-1])
    c = float(df["close"].iloc[-1])
    if u == l:
        return 0.5
    return (c - l) / (u - l)
