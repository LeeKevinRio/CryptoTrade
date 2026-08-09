"""多訊號聚合器 — 綜合抄底與做空策略"""

import pandas as pd

from src.strategy.base_strategy import Signal, SignalType
from src.strategy.dip_buyer import DipBuyer
from src.strategy.short_seller import ShortSeller
from src.utils.logger import setup_logger

logger = setup_logger("signal_aggregator")


class SignalAggregator:
    """
    聚合多個策略的訊號，產生最終交易決策

    - 同時運行抄底與做空策略
    - 取最強訊號作為最終決策
    - 衝突時（同時出現多空訊號）不動作
    """

    def __init__(self, config: dict):
        self.config = config
        self.dip_buyer = DipBuyer(config)
        self.short_seller = ShortSeller(config)
        self.strong_threshold = config.get("strategy", {}).get("strong_signal_threshold", 70)
        self.medium_threshold = config.get("strategy", {}).get("medium_signal_threshold", 60)

    def evaluate(
        self,
        symbol: str,
        candles: dict[str, pd.DataFrame],
        funding_rate: float = 0.0,
        sentiment=None,
        external=None,
    ) -> Signal:
        long_signal = self.dip_buyer.analyze(symbol, candles, funding_rate, sentiment, external)
        short_signal = self.short_seller.analyze(symbol, candles, funding_rate, sentiment, external)

        # 衝突檢查：同時出現多空訊號 → 不動作
        if long_signal.is_actionable and short_signal.is_actionable:
            logger.warning(
                "%s 多空訊號衝突，不動作 (多:%.1f 空:%.1f)",
                symbol, long_signal.strength, short_signal.strength,
            )
            return Signal(
                type=SignalType.NEUTRAL,
                symbol=symbol,
                strength=0,
                reasons=["多空訊號衝突，取消動作"],
            )

        # 取最強訊號
        if long_signal.is_actionable:
            return long_signal
        if short_signal.is_actionable:
            return short_signal

        # 無明確訊號
        return Signal(type=SignalType.NEUTRAL, symbol=symbol, strength=0)

    def get_position_size_ratio(self, signal: Signal) -> float:
        """根據訊號強度決定倉位比例"""
        if signal.strength >= self.strong_threshold:
            return 1.0   # 全額
        elif signal.strength >= self.medium_threshold:
            return 0.5   # 半倉
        return 0.0
