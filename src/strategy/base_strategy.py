"""策略基底類別"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


class SignalType(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


@dataclass
class Signal:
    type: SignalType
    symbol: str
    strength: float              # 0-100 訊號強度
    reasons: list[str] = field(default_factory=list)
    price: float = 0.0
    timestamp: str = ""
    # 可動作門檻：由策略依 config 的 medium_signal_threshold 帶入。
    # 預設 60 維持既有行為；調低可提高交易頻率（訊號較不嚴格）。
    min_strength: float = 60.0

    @property
    def is_actionable(self) -> bool:
        return self.type != SignalType.NEUTRAL and self.strength >= self.min_strength


class BaseStrategy(ABC):
    """所有策略的基底類別"""

    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    def analyze(
        self,
        symbol: str,
        candles: dict[str, pd.DataFrame],
        funding_rate: float = 0.0,
        sentiment=None,
    ) -> Signal:
        """
        分析市場狀態並產生訊號

        Args:
            symbol: 交易對
            candles: {interval: DataFrame} 多時間框架 K 線
            funding_rate: 當前資金費率
            sentiment: SentimentScore | None，消息面/市場情緒（可為 None → 純技術面）

        Returns:
            Signal 物件
        """
        ...
