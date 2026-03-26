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

    @property
    def is_actionable(self) -> bool:
        return self.type != SignalType.NEUTRAL and self.strength >= 60


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
    ) -> Signal:
        """
        分析市場狀態並產生訊號

        Args:
            symbol: 交易對
            candles: {interval: DataFrame} 多時間框架 K 線
            funding_rate: 當前資金費率

        Returns:
            Signal 物件
        """
        ...
