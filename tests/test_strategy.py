"""策略測試"""

import unittest
import pandas as pd
import numpy as np

from src.strategy.dip_buyer import DipBuyer
from src.strategy.short_seller import ShortSeller
from src.strategy.signal_aggregator import SignalAggregator
from src.strategy.base_strategy import SignalType


DEFAULT_CONFIG = {
    "strategy": {
        "min_signals": 3,
        "weights": {
            "rsi": 0.20,
            "bollinger": 0.15,
            "macd": 0.20,
            "volume": 0.15,
            "multi_timeframe": 0.15,
            "funding_rate": 0.15,
        },
        "strong_signal_threshold": 70,
        "medium_signal_threshold": 60,
        "dip_buyer": {
            "rsi_oversold": 30,
            "rsi_15m_threshold": 35,
            "volume_multiplier": 1.5,
        },
        "short_seller": {
            "rsi_overbought": 70,
            "rsi_15m_threshold": 65,
            "funding_rate_high": 0.05,
        },
    }
}


def make_downtrend_df(n=100) -> pd.DataFrame:
    """生成下跌趨勢資料"""
    np.random.seed(42)
    closes = [50000]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 - np.random.uniform(0.001, 0.015)))
    volumes = [100 + np.random.uniform(50, 200) for _ in range(n)]
    # 最後放量
    volumes[-1] = 500
    return pd.DataFrame({
        "open": [c * 1.002 for c in closes],
        "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes],
        "close": closes,
        "volume": volumes,
    })


def make_uptrend_df(n=100) -> pd.DataFrame:
    """生成上漲趨勢資料"""
    np.random.seed(42)
    closes = [50000]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + np.random.uniform(0.001, 0.015)))
    volumes = [200 - i * 0.5 for i in range(n)]  # 量能遞減
    return pd.DataFrame({
        "open": [c * 0.998 for c in closes],
        "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes],
        "close": closes,
        "volume": volumes,
    })


class TestDipBuyer(unittest.TestCase):
    def test_neutral_in_normal_market(self):
        """正常市場不應產生抄底訊號"""
        df = pd.DataFrame({
            "open": [100.0] * 50,
            "high": [101.0] * 50,
            "low": [99.0] * 50,
            "close": [100.0] * 50,
            "volume": [100.0] * 50,
        })
        buyer = DipBuyer(DEFAULT_CONFIG)
        signal = buyer.analyze("BTCUSDT", {"5m": df, "15m": df, "1h": df})
        self.assertEqual(signal.type, SignalType.NEUTRAL)

    def test_signal_has_reasons(self):
        """下跌趨勢應產生一些原因（不一定觸發）"""
        df = make_downtrend_df()
        buyer = DipBuyer(DEFAULT_CONFIG)
        signal = buyer.analyze("BTCUSDT", {"5m": df, "15m": df, "1h": df}, funding_rate=-0.001)
        # 至少應有資金費率這個原因
        self.assertTrue(len(signal.reasons) >= 1)


class TestShortSeller(unittest.TestCase):
    def test_neutral_in_normal_market(self):
        df = pd.DataFrame({
            "open": [100.0] * 50,
            "high": [101.0] * 50,
            "low": [99.0] * 50,
            "close": [100.0] * 50,
            "volume": [100.0] * 50,
        })
        seller = ShortSeller(DEFAULT_CONFIG)
        signal = seller.analyze("BTCUSDT", {"5m": df, "15m": df, "1h": df})
        self.assertEqual(signal.type, SignalType.NEUTRAL)


class TestSignalAggregator(unittest.TestCase):
    def test_neutral_default(self):
        df = pd.DataFrame({
            "open": [100.0] * 50,
            "high": [101.0] * 50,
            "low": [99.0] * 50,
            "close": [100.0] * 50,
            "volume": [100.0] * 50,
        })
        agg = SignalAggregator(DEFAULT_CONFIG)
        signal = agg.evaluate("BTCUSDT", {"5m": df, "15m": df, "1h": df})
        self.assertEqual(signal.type, SignalType.NEUTRAL)

    def test_position_size_ratio(self):
        agg = SignalAggregator(DEFAULT_CONFIG)
        from src.strategy.base_strategy import Signal
        strong = Signal(type=SignalType.LONG, symbol="BTC", strength=75)
        medium = Signal(type=SignalType.LONG, symbol="BTC", strength=65)
        weak = Signal(type=SignalType.LONG, symbol="BTC", strength=40)
        self.assertEqual(agg.get_position_size_ratio(strong), 1.0)
        self.assertEqual(agg.get_position_size_ratio(medium), 0.5)
        self.assertEqual(agg.get_position_size_ratio(weak), 0.0)


if __name__ == "__main__":
    unittest.main()
