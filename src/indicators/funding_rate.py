"""資金費率分析"""


def is_negative_funding(rate: float) -> bool:
    """資金費率為負 → 空頭情緒過重 → 潛在抄底機會"""
    return rate < 0


def is_extreme_positive_funding(rate: float, threshold: float = 0.05) -> bool:
    """資金費率極高 → 多頭過度貪婪 → 潛在做空機會

    threshold: 百分比，預設 0.05%
    """
    return rate > threshold / 100


def funding_sentiment(rate: float) -> str:
    """根據資金費率判斷市場情緒"""
    pct = rate * 100
    if pct < -0.05:
        return "EXTREME_FEAR"
    elif pct < 0:
        return "FEAR"
    elif pct < 0.05:
        return "NEUTRAL"
    elif pct < 0.1:
        return "GREED"
    else:
        return "EXTREME_GREED"
