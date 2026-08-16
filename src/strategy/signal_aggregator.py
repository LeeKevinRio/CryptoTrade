"""多訊號聚合器 — 綜合抄底與做空策略"""

import pandas as pd

from src.strategy.base_strategy import Signal, SignalType
from src.strategy.dip_buyer import DipBuyer
from src.strategy.short_seller import ShortSeller
from src.indicators.ema import calculate_ema
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
        # 高時框趨勢過濾：下跌趨勢擋逆勢多單、上升趨勢擋逆勢空單
        self.trend_cfg = config.get("strategy", {}).get("trend_filter", {})

    def _trend_bias(self, candles: dict[str, pd.DataFrame]) -> tuple[int, float]:
        """以高時框 EMA 斜率判定趨勢。回傳 (bias, slope_pct)：
        bias 1=上升 -1=下降 0=中性/停用/資料不足
        """
        if not self.trend_cfg.get("enabled", False):
            return 0, 0.0
        tf = self.trend_cfg.get("timeframe", "4h")
        df = candles.get(tf)
        period = int(self.trend_cfg.get("ema_period", 50))
        lookback = int(self.trend_cfg.get("slope_lookback", 6))
        if df is None or len(df) < period + lookback:
            return 0, 0.0
        ema = calculate_ema(df, period)
        prev = float(ema.iloc[-1 - lookback])
        if prev == 0:
            return 0, 0.0
        slope_pct = (float(ema.iloc[-1]) - prev) / prev * 100
        threshold = float(self.trend_cfg.get("slope_threshold_pct", 0.3))
        if slope_pct >= threshold:
            return 1, slope_pct
        if slope_pct <= -threshold:
            return -1, slope_pct
        return 0, slope_pct

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

        # 趨勢過濾：逆勢訊號直接中性化（順勢訊號不加分，只做風險閘門）
        bias, slope = self._trend_bias(candles)
        if bias < 0 and long_signal.is_actionable:
            logger.info("%s 4h 趨勢向下(EMA斜率 %.2f%%)，擋掉逆勢多單", symbol, slope)
            long_signal = Signal(
                type=SignalType.NEUTRAL, symbol=symbol, strength=0,
                reasons=[f"趨勢過濾：4h EMA 斜率 {slope:.2f}% 向下，擋逆勢多單"],
            )
        elif bias > 0 and short_signal.is_actionable:
            logger.info("%s 4h 趨勢向上(EMA斜率 %.2f%%)，擋掉逆勢空單", symbol, slope)
            short_signal = Signal(
                type=SignalType.NEUTRAL, symbol=symbol, strength=0,
                reasons=[f"趨勢過濾：4h EMA 斜率 {slope:.2f}% 向上，擋逆勢空單"],
            )

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
