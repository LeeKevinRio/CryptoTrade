"""高時框趨勢過濾器測試"""

import unittest
from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from src.strategy.base_strategy import Signal, SignalType
from src.strategy.signal_aggregator import SignalAggregator


def make_4h_df(direction: str, bars: int = 80) -> pd.DataFrame:
    """direction: up / down / flat 的合成 4h K 線"""
    idx = pd.date_range("2026-01-01", periods=bars, freq="4h")
    if direction == "up":
        close = np.linspace(100, 130, bars)
    elif direction == "down":
        close = np.linspace(130, 100, bars)
    else:
        close = np.full(bars, 100.0)
    return pd.DataFrame({
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": np.full(bars, 1000.0),
    }, index=idx)


def make_aggregator(enabled: bool = True) -> SignalAggregator:
    cfg = {
        "strategy": {
            "trend_filter": {
                "enabled": enabled,
                "timeframe": "4h",
                "ema_period": 50,
                "slope_lookback": 6,
                "slope_threshold_pct": 0.3,
            },
        },
    }
    return SignalAggregator(cfg)


def actionable(sig_type: SignalType) -> Signal:
    return Signal(type=sig_type, symbol="BTCUSDT", strength=80, min_strength=60)


def neutral() -> Signal:
    return Signal(type=SignalType.NEUTRAL, symbol="BTCUSDT", strength=0)


class TestTrendBias(unittest.TestCase):
    def test_up_down_flat(self):
        agg = make_aggregator()
        self.assertEqual(agg._trend_bias({"4h": make_4h_df("up")})[0], 1)
        self.assertEqual(agg._trend_bias({"4h": make_4h_df("down")})[0], -1)
        self.assertEqual(agg._trend_bias({"4h": make_4h_df("flat")})[0], 0)

    def test_disabled_or_missing_data(self):
        self.assertEqual(make_aggregator(enabled=False)._trend_bias(
            {"4h": make_4h_df("down")})[0], 0)
        agg = make_aggregator()
        self.assertEqual(agg._trend_bias({})[0], 0)                     # 無 4h 資料
        self.assertEqual(agg._trend_bias(
            {"4h": make_4h_df("down", bars=20)})[0], 0)                 # 根數不足


class TestTrendVeto(unittest.TestCase):
    def _eval(self, direction: str, long_sig: Signal, short_sig: Signal,
              enabled: bool = True) -> Signal:
        agg = make_aggregator(enabled=enabled)
        agg.dip_buyer = MagicMock(analyze=MagicMock(return_value=long_sig))
        agg.short_seller = MagicMock(analyze=MagicMock(return_value=short_sig))
        return agg.evaluate("BTCUSDT", {"4h": make_4h_df(direction)})

    def test_downtrend_blocks_long(self):
        result = self._eval("down", actionable(SignalType.LONG), neutral())
        self.assertEqual(result.type, SignalType.NEUTRAL)

    def test_uptrend_blocks_short(self):
        result = self._eval("up", neutral(), actionable(SignalType.SHORT))
        self.assertEqual(result.type, SignalType.NEUTRAL)

    def test_downtrend_allows_short(self):
        result = self._eval("down", neutral(), actionable(SignalType.SHORT))
        self.assertEqual(result.type, SignalType.SHORT)

    def test_uptrend_allows_long(self):
        result = self._eval("up", actionable(SignalType.LONG), neutral())
        self.assertEqual(result.type, SignalType.LONG)

    def test_flat_trend_passes_everything(self):
        result = self._eval("flat", actionable(SignalType.LONG), neutral())
        self.assertEqual(result.type, SignalType.LONG)

    def test_disabled_passes_countertrend(self):
        result = self._eval("down", actionable(SignalType.LONG), neutral(),
                            enabled=False)
        self.assertEqual(result.type, SignalType.LONG)


if __name__ == "__main__":
    unittest.main()
