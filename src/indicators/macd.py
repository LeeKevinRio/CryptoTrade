"""MACD 指標與背離偵測"""

import pandas as pd
import numpy as np


def calculate_macd(
    df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9, column: str = "close"
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """返回 (macd_line, signal_line, histogram)"""
    ema_fast = df[column].ewm(span=fast, adjust=False).mean()
    ema_slow = df[column].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def detect_bullish_divergence(df: pd.DataFrame, lookback: int = 30) -> bool:
    """偵測底背離：價格新低但 MACD 不新低"""
    if len(df) < lookback + 10:
        return False

    macd_line, _, _ = calculate_macd(df)
    recent = df.iloc[-lookback:]
    recent_macd = macd_line.iloc[-lookback:]

    # 找最近兩個價格低點
    price_lows = []
    for i in range(2, len(recent) - 2):
        if (recent["close"].iloc[i] <= recent["close"].iloc[i - 1] and
                recent["close"].iloc[i] <= recent["close"].iloc[i - 2] and
                recent["close"].iloc[i] <= recent["close"].iloc[i + 1] and
                recent["close"].iloc[i] <= recent["close"].iloc[i + 2]):
            price_lows.append(i)

    if len(price_lows) < 2:
        return False

    # 取最後兩個低點
    idx1, idx2 = price_lows[-2], price_lows[-1]
    price_lower = recent["close"].iloc[idx2] < recent["close"].iloc[idx1]
    macd_higher = recent_macd.iloc[idx2] > recent_macd.iloc[idx1]

    return price_lower and macd_higher


def detect_bearish_divergence(df: pd.DataFrame, lookback: int = 30) -> bool:
    """偵測頂背離：價格新高但 MACD 不新高"""
    if len(df) < lookback + 10:
        return False

    macd_line, _, _ = calculate_macd(df)
    recent = df.iloc[-lookback:]
    recent_macd = macd_line.iloc[-lookback:]

    price_highs = []
    for i in range(2, len(recent) - 2):
        if (recent["close"].iloc[i] >= recent["close"].iloc[i - 1] and
                recent["close"].iloc[i] >= recent["close"].iloc[i - 2] and
                recent["close"].iloc[i] >= recent["close"].iloc[i + 1] and
                recent["close"].iloc[i] >= recent["close"].iloc[i + 2]):
            price_highs.append(i)

    if len(price_highs) < 2:
        return False

    idx1, idx2 = price_highs[-2], price_highs[-1]
    price_higher = recent["close"].iloc[idx2] > recent["close"].iloc[idx1]
    macd_lower = recent_macd.iloc[idx2] < recent_macd.iloc[idx1]

    return price_higher and macd_lower
