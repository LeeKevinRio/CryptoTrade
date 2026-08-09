"""做空策略 — 多指標共振偵測過熱頂部"""

import pandas as pd

from src.indicators.rsi import get_current_rsi, is_overbought
from src.indicators.bollinger import is_above_upper_band
from src.indicators.macd import detect_bearish_divergence
from src.indicators.volume_profile import is_volume_declining, detect_shooting_star
from src.indicators.funding_rate import is_extreme_positive_funding
from src.strategy.base_strategy import BaseStrategy, Signal, SignalType
from src.utils.logger import setup_logger

logger = setup_logger("short_seller")


class ShortSeller(BaseStrategy):
    """
    做空策略：偵測市場過熱頂部

    觸發條件（多指標共振）：
    1. RSI(14) > 70 超買
    2. 價格觸及布林上軌
    3. MACD 頂背離
    4. 成交量萎縮（上漲無量）
    5. 15m RSI > 65（多時框架確認）
    6. 1h 出現射擊之星
    7. 資金費率極高 (> 0.05%)
    """

    def analyze(
        self,
        symbol: str,
        candles: dict[str, pd.DataFrame],
        funding_rate: float = 0.0,
        sentiment=None,
        external=None,
    ) -> Signal:
        cfg = self.config.get("strategy", {}).get("short_seller", {})
        rsi_threshold = cfg.get("rsi_overbought", 70)
        rsi_15m_threshold = cfg.get("rsi_15m_threshold", 65)
        fr_threshold = cfg.get("funding_rate_high", 0.05)
        min_signals = self.config.get("strategy", {}).get("min_signals", 3)
        sentiment_threshold = self.config.get("strategy", {}).get("sentiment_threshold", 0.2)
        external_threshold = self.config.get("strategy", {}).get("external_threshold", 0.2)
        min_strength = self.config.get("strategy", {}).get("medium_signal_threshold", 60)

        reasons = []
        score = 0
        weights = self.config.get("strategy", {}).get("weights", {})

        main_tf = candles.get("5m")
        if main_tf is None:
            main_tf = candles.get("15m")
        if main_tf is None or len(main_tf) < 30:
            return Signal(type=SignalType.NEUTRAL, symbol=symbol, strength=0)

        current_price = float(main_tf["close"].iloc[-1])

        # ── 條件 1: RSI 超買 ──
        rsi_val = get_current_rsi(main_tf)
        if rsi_val is not None and rsi_val > rsi_threshold:
            w = weights.get("rsi", 0.20)
            score += w * 100
            reasons.append(f"RSI={rsi_val:.1f} 超買 (>{rsi_threshold})")

        # ── 條件 2: 布林上軌 ──
        if is_above_upper_band(main_tf):
            w = weights.get("bollinger", 0.15)
            score += w * 100
            reasons.append("價格觸及布林上軌")

        # ── 條件 3: MACD 頂背離 ──
        if detect_bearish_divergence(main_tf):
            w = weights.get("macd", 0.20)
            score += w * 100
            reasons.append("MACD 頂背離")

        # ── 條件 4: 成交量萎縮 ──
        if is_volume_declining(main_tf):
            w = weights.get("volume", 0.15)
            score += w * 100
            reasons.append("上漲量能萎縮")

        # ── 條件 5 & 6: 多時框架確認 ──
        mtf_score = 0
        tf_15m = candles.get("15m")
        if tf_15m is not None and len(tf_15m) > 14:
            rsi_15m = get_current_rsi(tf_15m)
            if rsi_15m is not None and rsi_15m > rsi_15m_threshold:
                mtf_score += 50
                reasons.append(f"15m RSI={rsi_15m:.1f} 確認超買")

        tf_1h = candles.get("1h")
        if tf_1h is not None and len(tf_1h) > 5:
            if detect_shooting_star(tf_1h):
                mtf_score += 50
                reasons.append("1h 出現射擊之星")

        if mtf_score > 0:
            w = weights.get("multi_timeframe", 0.15)
            score += w * min(mtf_score, 100)

        # ── 條件 7: 資金費率過高 ──
        if is_extreme_positive_funding(funding_rate, threshold=fr_threshold):
            w = weights.get("funding_rate", 0.15)
            score += w * 100
            reasons.append(f"資金費率過高 ({funding_rate * 100:.4f}%)")

        # ── 條件 8: 消息面/市場情緒（極度貪婪 + 利空新聞 → 挺空）──
        if sentiment is not None and sentiment.short_support(sentiment_threshold):
            w = weights.get("sentiment", 0.20)
            score += w * 100 * min(1.0, abs(sentiment.bias))   # 依 bias 強度加權
            reasons.append(f"消息面挺空 ({sentiment.summary()})")

        # ── 條件 9: 外部訊號（TradingView 專有指標）──
        if external is not None and external.supports("SHORT", external_threshold):
            w = weights.get("external", 0.0)
            score += w * 100 * min(1.0, abs(external.bias))
            reasons.append(f"外部訊號挺空 ({external.summary()})")

        # ── 產生訊號 ──
        signal_type = SignalType.NEUTRAL
        if len(reasons) >= min_signals:
            signal_type = SignalType.SHORT

        signal = Signal(
            type=signal_type,
            symbol=symbol,
            strength=round(score, 1),
            reasons=reasons,
            price=current_price,
            min_strength=min_strength,
        )

        if signal.is_actionable:
            logger.info(
                "🔴 做空訊號 %s | 強度=%.1f | %s",
                symbol, score, " | ".join(reasons),
            )

        return signal
