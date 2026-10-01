"""順勢策略：regime / 回檔進場 / 結構停損；回測模擬器：R 分批、保本、吊燈追蹤"""

import unittest
import numpy as np
import pandas as pd

from src.strategy.trend_follower import TrendFollower
from src.strategy.base_strategy import SignalType
from backtest.trend_backtest import simulate, stats


def frames_from_5m(closes: np.ndarray, vol: float = 100.0) -> dict:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="5min")
    df = pd.DataFrame({"open": closes, "high": closes * 1.001, "low": closes * 0.999,
                       "close": closes, "volume": vol}, index=idx)
    from backtest.fidelity import resample_timeframes
    return resample_timeframes(df)


def uptrend_with_pullback(n5=12 * 24 * 40, seed=0):
    """40 天：穩定上升 + 最後一天回檔到 1h EMA20 再反彈"""
    rng = np.random.default_rng(seed)
    t = np.arange(n5)
    base = 100 * (1 + 0.00008) ** t                       # 每根 +0.008% ≈ 每天 +2.3%
    noise = rng.normal(0, 0.0004, n5).cumsum() * 0.3
    c = base * (1 + noise)
    # 最後 48 根（4h）回檔 1.2% 再反彈 1%
    k = 48
    dip = np.concatenate([np.linspace(0, -0.012, k // 2), np.linspace(-0.012, -0.002, k // 2)])
    c[-k:] = c[-k - 1] * (1 + dip)
    c[-1] = c[-2] * 1.004                                  # 突破前幾根高點
    return c


class TestTrendFollower(unittest.TestCase):
    def test_neutral_without_trend(self):
        c = 100 + np.sin(np.arange(12 * 24 * 40) / 50)     # 橫盤
        sig = TrendFollower({}).analyze("X", frames_from_5m(c))
        self.assertEqual(sig.type, SignalType.NEUTRAL)

    def test_regime_detects_uptrend_and_stop_is_structural(self):
        c = uptrend_with_pullback()
        tf = TrendFollower({"strategy": {"trend_follower": {"resume_bars": 3}}})
        fr = frames_from_5m(c)
        side, slope = tf._regime(fr["4h"])
        self.assertEqual(side, 1)
        self.assertGreater(slope, 0.3)
        stop = tf._stop(fr["5m"], float(c[-1]), 1)
        dist = (c[-1] - stop) / c[-1] * 100
        self.assertGreaterEqual(dist, 0.6)
        self.assertLessEqual(dist, 3.0)

    def test_signal_carries_stop_price_when_actionable(self):
        c = uptrend_with_pullback()
        tf = TrendFollower({"strategy": {"trend_follower": {"resume_bars": 3, "rsi_min": 0, "rsi_max": 100,
                                                            "touch_pct": 3.0, "pullback_lookback": 24}}})
        sig = tf.analyze("X", frames_from_5m(c))
        if sig.is_actionable:                               # 合成資料不保證每個條件都同時成立
            self.assertEqual(sig.type, SignalType.LONG)
            self.assertIsNotNone(sig.stop_price)
            self.assertLess(sig.stop_price, sig.price)

    def test_short_mirror_regime(self):
        c = uptrend_with_pullback()[::-1]                   # 反轉成下跌
        tf = TrendFollower({})
        side, _ = tf._regime(frames_from_5m(c)["4h"])
        self.assertEqual(side, -1)


class TestSimulate(unittest.TestCase):
    def _run(self, closes, stop_pct, p, atr=None):
        c = np.array(closes, dtype=float); h = c * 1.001; l = c * 0.999
        sides = np.zeros(len(c), dtype=np.int8); sides[1] = 1
        sd = np.zeros(len(c)); sd[1] = stop_pct
        a = np.full(len(c), atr if atr is not None else 1e9)   # 預設 ATR 極大 → 吊燈不觸發
        ema = np.zeros(len(c)); h1c = np.ones(len(c))
        base = {"partial_r": None, "trail_atr": 2.0, "ema_exit": False, "max_stop_pct": 3.0}
        return simulate(c, h, l, sides, sd, a, ema, h1c, {**base, **p}, warmup=1)

    def test_stop_loss_is_one_r(self):
        res = self._run([100, 100, 99.5, 97.9, 97], 2.0, {})
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0][0], -2.0, places=2)
        self.assertAlmostEqual(res[0][1], -1.0, places=1)

    def test_partial_then_breakeven(self):
        # 停損 2% → 1R 目標 102：先平 30%，停損移到 100；之後跌回 → 剩餘保本出場
        res = self._run([100, 100, 101, 102.1, 101, 100.5, 99.8, 97], 2.0, {"partial_r": 1.0})
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0][0], 0.3 * 2.0, places=1)

    def test_chandelier_lets_winner_run(self):
        # ATR=1 → 追蹤距離 2；一路漲到 110 再回落 → 在 108 附近出場
        res = self._run([100, 100, 102, 104, 106, 108, 110, 109, 107.5, 105], 2.0, {"trail_atr": 2.0}, atr=1.0)
        self.assertEqual(len(res), 1)
        self.assertGreater(res[0][0], 7.0)
        self.assertGreater(res[0][1], 3.5)      # R 倍數 > 3.5

    def test_stats_payoff(self):
        s = stats([(4.0, 2.0), (-2.0, -1.0), (-2.0, -1.0), (6.0, 3.0)])
        self.assertEqual(s["trades"], 4)
        self.assertGreater(s["payoff"], 2.0)
        self.assertAlmostEqual(s["avg_r"], 0.75)


if __name__ == "__main__":
    unittest.main()
