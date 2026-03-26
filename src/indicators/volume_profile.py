"""成交量分析"""

import pandas as pd
import numpy as np


def is_volume_surge(df: pd.DataFrame, multiplier: float = 1.5, sma_period: int = 20) -> bool:
    """當前成交量是否超過均量 N 倍"""
    if len(df) < sma_period + 1:
        return False
    vol_sma = df["volume"].rolling(sma_period).mean().iloc[-1]
    current_vol = df["volume"].iloc[-1]
    return current_vol > vol_sma * multiplier


def is_volume_declining(df: pd.DataFrame, periods: int = 5) -> bool:
    """成交量是否連續萎縮"""
    if len(df) < periods:
        return False
    recent_vols = df["volume"].iloc[-periods:]
    declining = all(
        recent_vols.iloc[i] < recent_vols.iloc[i - 1]
        for i in range(1, len(recent_vols))
    )
    return declining


def volume_ratio(df: pd.DataFrame, sma_period: int = 20) -> float | None:
    """當前成交量 / 均量"""
    if len(df) < sma_period + 1:
        return None
    vol_sma = df["volume"].rolling(sma_period).mean().iloc[-1]
    if vol_sma == 0:
        return None
    return float(df["volume"].iloc[-1] / vol_sma)


def detect_hammer(df: pd.DataFrame) -> bool:
    """偵測錘子線（長下影線，看漲反轉訊號）"""
    if df.empty:
        return False
    last = df.iloc[-1]
    body = abs(float(last["close"]) - float(last["open"]))
    full_range = float(last["high"]) - float(last["low"])
    if full_range == 0:
        return False
    lower_shadow = min(float(last["close"]), float(last["open"])) - float(last["low"])
    upper_shadow = float(last["high"]) - max(float(last["close"]), float(last["open"]))
    # 下影線佔全幅 60% 以上，且上影線短
    return lower_shadow > full_range * 0.6 and upper_shadow < full_range * 0.15


def detect_shooting_star(df: pd.DataFrame) -> bool:
    """偵測射擊之星（長上影線，看跌反轉訊號）"""
    if df.empty:
        return False
    last = df.iloc[-1]
    body = abs(float(last["close"]) - float(last["open"]))
    full_range = float(last["high"]) - float(last["low"])
    if full_range == 0:
        return False
    lower_shadow = min(float(last["close"]), float(last["open"])) - float(last["low"])
    upper_shadow = float(last["high"]) - max(float(last["close"]), float(last["open"]))
    # 上影線佔全幅 60% 以上，且下影線短
    return upper_shadow > full_range * 0.6 and lower_shadow < full_range * 0.15
