"""情緒訊號資料模型"""

import time
from dataclasses import dataclass, field


@dataclass
class SentimentScore:
    """單一交易對在某一時刻的綜合情緒評分。

    bias 是最終給策略用的方向性分數：
        +1.0 = 極度看多、-1.0 = 極度看空、0 = 中性/無資料
    """

    symbol: str
    bias: float = 0.0                       # -1..+1，合成方向分數
    fear_greed: int | None = None           # 0-100，0=極度恐慌
    fear_greed_label: str = ""
    news_score: float | None = None         # -1..+1，新聞情緒
    news_headlines: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def long_support(self, threshold: float) -> bool:
        """情緒是否足以支持做多（bias 夠正）"""
        return self.bias >= threshold

    def short_support(self, threshold: float) -> bool:
        """情緒是否足以支持做空（bias 夠負）"""
        return self.bias <= -threshold

    @property
    def label(self) -> str:
        if self.bias >= 0.5:
            return "強烈看多"
        if self.bias >= 0.2:
            return "偏多"
        if self.bias <= -0.5:
            return "強烈看空"
        if self.bias <= -0.2:
            return "偏空"
        return "中性"

    def summary(self) -> str:
        parts = [f"bias={self.bias:+.2f}({self.label})"]
        if self.fear_greed is not None:
            parts.append(f"F&G={self.fear_greed}({self.fear_greed_label})")
        if self.news_score is not None:
            parts.append(f"news={self.news_score:+.2f}")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "bias": round(self.bias, 3),
            "label": self.label,
            "fear_greed": self.fear_greed,
            "fear_greed_label": self.fear_greed_label,
            "news_score": round(self.news_score, 3) if self.news_score is not None else None,
            "news_headlines": self.news_headlines[:5],
            "sources": self.sources,
            "updated_at": self.updated_at,
        }


def fear_greed_to_bias(fg: int) -> float:
    """Fear & Greed（0-100）→ 反向 bias（-1..+1）。

    反向解讀：極度恐慌(0) → +1 挺多；極度貪婪(100) → -1 挺空。
    以 50 為中性點線性映射。
    """
    return max(-1.0, min(1.0, (50 - fg) / 50.0))
