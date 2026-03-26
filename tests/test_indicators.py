"""技術指標測試"""

import pandas as pd
import numpy as np
import unittest

from src.indicators.rsi import calculate_rsi, is_oversold, is_overbought, get_current_rsi
from src.indicators.bollinger import calculate_bollinger, is_below_lower_band, is_above_upper_band, get_bb_position
from src.indicators.macd import calculate_macd
from src.indicators.volume_profile import is_volume_surge, detect_hammer, detect_shooting_star
from src.indicators.atr import calculate_atr, get_current_atr
from src.indicators.ema import calculate_ema, ema_crossover
from src.indicators.funding_rate import is_negative_funding, is_extreme_positive_funding, funding_sentiment


def make_df(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    opens = [c * 1.005 for c in closes]
    if volumes is None:
        volumes = [100.0] * n
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


class TestRSI(unittest.TestCase):
    def test_rsi_range(self):
        # 持續上漲 → RSI 應該很高
        closes = [100 + i * 2 for i in range(50)]
        df = make_df(closes)
        rsi = get_current_rsi(df)
        self.assertIsNotNone(rsi)
        self.assertGreater(rsi, 60)

    def test_rsi_oversold(self):
        # 持續下跌
        closes = [100 - i * 2 for i in range(50)]
        df = make_df(closes)
        self.assertTrue(is_oversold(df, threshold=30))

    def test_rsi_overbought(self):
        closes = [100 + i * 3 for i in range(50)]
        df = make_df(closes)
        self.assertTrue(is_overbought(df, threshold=70))


class TestBollinger(unittest.TestCase):
    def test_bands_exist(self):
        closes = [100 + np.sin(i / 5) * 10 for i in range(50)]
        df = make_df(closes)
        upper, middle, lower = calculate_bollinger(df)
        self.assertEqual(len(upper), len(df))
        # 上軌 > 中軌 > 下軌
        last_idx = -1
        self.assertGreater(upper.iloc[last_idx], middle.iloc[last_idx])
        self.assertGreater(middle.iloc[last_idx], lower.iloc[last_idx])

    def test_bb_position(self):
        closes = [100.0] * 50
        df = make_df(closes)
        pos = get_bb_position(df)
        # 平穩價格應在中間附近
        self.assertIsNotNone(pos)


class TestMACD(unittest.TestCase):
    def test_macd_calculation(self):
        closes = [100 + np.sin(i / 10) * 20 for i in range(100)]
        df = make_df(closes)
        macd_line, signal_line, hist = calculate_macd(df)
        self.assertEqual(len(macd_line), len(df))


class TestVolume(unittest.TestCase):
    def test_volume_surge(self):
        volumes = [100.0] * 25 + [300.0]
        closes = [100.0] * 26
        df = make_df(closes, volumes)
        self.assertTrue(is_volume_surge(df, multiplier=1.5))

    def test_hammer(self):
        # 長下影線：high=101, open=100.5, close=100, low=90 → 下影線=10, 全幅=11
        df = pd.DataFrame([{
            "open": 100.5, "high": 101, "low": 90, "close": 100, "volume": 100,
        }])
        self.assertTrue(detect_hammer(df))

    def test_shooting_star(self):
        # 長上影線：high=110, open=100.5, close=100, low=99.5 → 上影線=9.5, 全幅=10.5
        df = pd.DataFrame([{
            "open": 100.5, "high": 110, "low": 99.5, "close": 100, "volume": 100,
        }])
        self.assertTrue(detect_shooting_star(df))


class TestATR(unittest.TestCase):
    def test_atr(self):
        closes = [100 + np.sin(i / 5) * 5 for i in range(50)]
        df = make_df(closes)
        atr = get_current_atr(df)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0)


class TestEMA(unittest.TestCase):
    def test_ema(self):
        closes = [100 + i for i in range(50)]
        df = make_df(closes)
        ema = calculate_ema(df, 10)
        self.assertEqual(len(ema), len(df))


class TestFundingRate(unittest.TestCase):
    def test_negative(self):
        self.assertTrue(is_negative_funding(-0.001))
        self.assertFalse(is_negative_funding(0.001))

    def test_extreme_positive(self):
        self.assertTrue(is_extreme_positive_funding(0.001, threshold=0.05))
        self.assertFalse(is_extreme_positive_funding(0.0001, threshold=0.05))

    def test_sentiment(self):
        self.assertEqual(funding_sentiment(-0.001), "EXTREME_FEAR")
        self.assertEqual(funding_sentiment(0.0001), "NEUTRAL")
        self.assertEqual(funding_sentiment(0.002), "EXTREME_GREED")


if __name__ == "__main__":
    unittest.main()
