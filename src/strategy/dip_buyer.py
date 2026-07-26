"""抄底策略 — 多指標共振偵測超賣底部"""

import pandas as pd

from src.indicators.rsi import get_current_rsi, is_oversold
from src.indicators.bollinger import is_below_lower_band
from src.indicators.macd import detect_bullish_divergence
from src.indicators.volume_profile import is_volume_surge, detect_hammer
from src.indicators.funding_rate import is_negative_funding
from src.strategy.base_strategy import BaseStrategy, Signal, SignalType
from src.utils.logger import setup_logger

logger = setup_logger("dip_buyer")


class DipBuyer(BaseStrategy):
    """
    抄底策略：偵測市場恐慌性下跌的底部

    觸發條件（多指標共振）：
    1. RSI(14) < 30 超賣
    2. 價格觸及布林下軌
    3. MACD 底背離
    4. 成交量放大（> 20日均量 × 1.5）
    5. 15m RSI < 35（多時框架確認）
    6. 1h 出現錘子線
    7. 資金費率為負
    """

    def analyze(
        self,
        symbol: str,
        candles: dict[str, pd.DataFrame],
        funding_rate: float = 0.0,
        sentiment=None,
    ) -> Signal:
        cfg = self.config.get("strategy", {}).get("dip_buyer", {})
        rsi_threshold = cfg.get("rsi_oversold", 30)
        rsi_15m_threshold = cfg.get("rsi_15m_threshold", 35)
        vol_multiplier = cfg.get("volume_multiplier", 1.5)
        min_signals = self.config.get("strategy", {}).get("min_signals", 3)
        sentiment_threshold = self.config.get("strategy", {}).get("sentiment_threshold", 0.2)
        min_strength = self.config.get("strategy", {}).get("medium_signal_threshold", 60)

        reasons = []
        score = 0
        weights = self.config.get("strategy", {}).get("weights", {})

        # 主要時框（5m 或 15m 作為主要分析框架）
        main_tf = candles.get("5m")
        if main_tf is None:
            main_tf = candles.get("15m")
        if main_tf is None or len(main_tf) < 30:
            return Signal(type=SignalType.NEUTRAL, symbol=symbol, strength=0)

        current_price = float(main_tf["close"].iloc[-1])

        # ── 條件 1: RSI 超賣 ──
        rsi_val = get_current_rsi(main_tf)
        if rsi_val is not None and rsi_val < rsi_threshold:
            w = weights.get("rsi", 0.20)
            score += w * 100
            reasons.append(f"RSI={rsi_val:.1f} 超賣 (<{rsi_threshold})")

        # ── 條件 2: 布林下軌 ──
        if is_below_lower_band(main_tf):
            w = weights.get("bollinger", 0.15)
            score += w * 100
            reasons.append("價格觸及布林下軌")

        # ── 條件 3: MACD 底背離 ──
        if detect_bullish_divergence(main_tf):
            w = weights.get("macd", 0.20)
            score += w * 100
            reasons.append("MACD 底背離")

        # ── 條件 4: 成交量放大 ──
        if is_volume_surge(main_tf, multiplier=vol_multiplier):
            w = weights.get("volume", 0.15)
            score += w * 100
            reasons.append(f"成交量放大 (>{vol_multiplier}x 均量)")

        # ── 條件 5 & 6: 多時框架確認 ──
        mtf_score = 0
        tf_15m = candles.get("15m")
        if tf_15m is not None and len(tf_15m) > 14:
            rsi_15m = get_current_rsi(tf_15m)
            if rsi_15m is not None and rsi_15m < rsi_15m_threshold:
                mtf_score += 50
                reasons.append(f"15m RSI={rsi_15m:.1f} 確認超賣")

        tf_1h = candles.get("1h")
        if tf_1h is not None and len(tf_1h) > 5:
            if detect_hammer(tf_1h):
                mtf_score += 50
                reasons.append("1h 出現錘子線")

        if mtf_score > 0:
            w = weights.get("multi_timeframe", 0.15)
            score += w * min(mtf_score, 100)

        # ── 條件 7: 資金費率 ──
        if is_negative_funding(funding_rate):
            w = weights.get("funding_rate", 0.15)
            score += w * 100
            reasons.append(f"資金費率為負 ({funding_rate * 100:.4f}%)")

        # ── 條件 8: 消息面/市場情緒（極度恐慌 + 利多新聞 → 挺多）──
        if sentiment is not None and sentiment.long_support(sentiment_threshold):
            w = weights.get("sentiment", 0.20)
            score += w * 100 * min(1.0, sentiment.bias)   # 依 bias 強度加權
            reasons.append(f"消息面挺多 ({sentiment.summary()})")

        # ── 產生訊號 ──
        signal_type = SignalType.NEUTRAL
        if len(reasons) >= min_signals:
            signal_type = SignalType.LONG

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
                "🟢 抄底訊號 %s | 強度=%.1f | %s",
                symbol, score, " | ".join(reasons),
            )

        return signal
