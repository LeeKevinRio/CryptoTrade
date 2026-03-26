"""EMA 指數移動平均"""

import pandas as pd


def calculate_ema(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    return df[column].ewm(span=period, adjust=False).mean()


def calculate_sma(df: pd.DataFrame, period: int, column: str = "close") -> pd.Series:
    return df[column].rolling(window=period).mean()


def ema_crossover(df: pd.DataFrame, fast: int = 9, slow: int = 21) -> str | None:
    """返回 'golden_cross', 'death_cross' 或 None"""
    ema_fast = calculate_ema(df, fast)
    ema_slow = calculate_ema(df, slow)

    if len(ema_fast) < 2:
        return None

    prev_diff = ema_fast.iloc[-2] - ema_slow.iloc[-2]
    curr_diff = ema_fast.iloc[-1] - ema_slow.iloc[-1]

    if prev_diff <= 0 and curr_diff > 0:
        return "golden_cross"
    elif prev_diff >= 0 and curr_diff < 0:
        return "death_cross"
    return None
