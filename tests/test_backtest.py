"""回測保真度測試 — 時間停損模擬、時框重取樣、情緒回放"""

import unittest

import numpy as np
import pandas as pd

from backtest.fidelity import (
    SentimentReplay, TimeframeSlicer, resample_timeframes,
)
from backtest.param_sweep import fast_backtest, _valid_tp_order


BASE = dict(stop_loss_pct=4.0, level_1_pct=1.5, level_2_pct=2.5,
            level_3_pct=3.0, trail_callback=0.4)


def _flat_series(n=400, price=100.0, drift=0.0):
    """幾乎無波動的價格序列 → 不會觸發 SL/TP，只有時間停損能結束交易"""
    close = np.full(n, price) + np.arange(n) * drift
    return close, close + 0.01, close - 0.01


class TestTimeStopSimulation(unittest.TestCase):
    def _sides(self, n, entry=60):
        s = np.zeros(n, dtype=np.int8)
        s[entry] = 1
        return s

    def test_no_movement_closes_position(self):
        c, h, l = _flat_series()
        # 2400s / 300s = 8 根後判定無波動
        r = fast_backtest(c, h, l, self._sides(len(c)),
                          {**BASE, "time_stop_seconds": 2400,
                           "time_stop_no_movement_pct": 0.4}, 1.0)
        self.assertEqual(len(r), 1)
        self.assertAlmostEqual(r[0], 0.0, places=1)   # 無波動 → 接近 0

    def test_disabled_leaves_position_open(self):
        c, h, l = _flat_series()
        r = fast_backtest(c, h, l, self._sides(len(c)),
                          {**BASE, "time_stop_seconds": None}, 1.0)
        self.assertEqual(len(r), 0)   # 沒有任何出場機制觸發

    def test_longer_timeout_still_closes(self):
        c, h, l = _flat_series(n=2000)
        r = fast_backtest(c, h, l, self._sides(len(c)),
                          {**BASE, "time_stop_seconds": 14400,
                           "time_stop_no_movement_pct": 0.4}, 1.0)
        self.assertEqual(len(r), 1)

    def test_movement_prevents_no_movement_exit(self):
        """有明顯走勢時不應以「無波動」出場。

        走勢須發生在時間停損觸發（進場後 8 根）之前，否則會先被無波動平掉。
        """
        n = 400
        close = np.full(n, 100.0)
        close[63:] = 101.2          # 進場(60)後第 3 根即 +1.2%，早於第 8 根
        h = close + 0.01; l = close - 0.01
        r = fast_backtest(close, h, l, self._sides(n),
                          {**BASE, "time_stop_seconds": 2400,
                           "time_stop_no_movement_pct": 0.4}, 1.0)
        # MFE 1.2% > 門檻 0.4% → 非無波動；L1(1.5%) 未達 → 無 time_lock
        self.assertEqual(len(r), 0)

    def test_time_lock_exits_when_l1_hit_and_profitable(self):
        """L1 已觸發、超時後仍獲利 → 應鎖利出場"""
        n = 400
        close = np.full(n, 100.0)
        close[62:] = 101.6          # 早於時間停損即突破 L1(1.5%)
        h = close + 0.01; l = close - 0.01
        r = fast_backtest(close, h, l, self._sides(n),
                          {**BASE, "time_stop_seconds": 2400,
                           "time_stop_no_movement_pct": 0.4}, 5.0)  # trail 不啟動
        self.assertEqual(len(r), 1)
        self.assertGreater(r[0], 0)

    def test_stop_loss_still_takes_priority(self):
        n = 400
        close = np.full(n, 100.0)
        close[65:] = 95.0            # -5% → 觸發 4% 停損
        h = close + 0.01; l = close - 0.01
        r = fast_backtest(close, h, l, self._sides(n),
                          {**BASE, "time_stop_seconds": 2400}, 1.0)
        self.assertEqual(len(r), 1)
        self.assertAlmostEqual(r[0], -4.0, places=1)


class TestResampling(unittest.TestCase):
    def _df(self, n=120):
        idx = pd.date_range("2026-01-01", periods=n, freq="5min")
        return pd.DataFrame({
            "open": np.arange(n, dtype=float), "high": np.arange(n) + 1.0,
            "low": np.arange(n) - 1.0, "close": np.arange(n, dtype=float),
            "volume": np.ones(n),
        }, index=idx)

    def test_resample_counts(self):
        df = self._df(120)                    # 120 根 5m = 10 小時
        f = resample_timeframes(df)
        self.assertEqual(len(f["5m"]), 120)
        self.assertEqual(len(f["15m"]), 40)   # 每 3 根 5m
        self.assertEqual(len(f["1h"]), 10)

    def test_ohlc_aggregation_correct(self):
        df = self._df(6)
        f = resample_timeframes(df, targets=("15m",))
        first = f["15m"].iloc[0]
        self.assertEqual(first["high"], max(df["high"].iloc[:3]))
        self.assertEqual(first["low"], min(df["low"].iloc[:3]))
        self.assertEqual(first["volume"], 3.0)

    def test_slicer_has_no_lookahead(self):
        df = self._df(120)
        slicer = TimeframeSlicer(resample_timeframes(df))
        ts = df.index[50]
        got = slicer.at(ts)
        for tf, frame in got.items():
            if len(frame):
                self.assertLessEqual(frame.index[-1], ts,
                                     f"{tf} 出現前視資料")


class TestSentimentReplay(unittest.TestCase):
    def test_replay_maps_day_to_bias(self):
        rp = SentimentReplay({"2026-01-01": 10, "2026-01-02": 90})
        s1 = rp.at(pd.Timestamp("2026-01-01 05:00"), "BTCUSDT")
        s2 = rp.at(pd.Timestamp("2026-01-02 23:00"), "BTCUSDT")
        self.assertGreater(s1.bias, 0)     # 極度恐慌 → 挺多
        self.assertLess(s2.bias, 0)        # 極度貪婪 → 挺空
        self.assertEqual(s1.fear_greed, 10)

    def test_missing_day_returns_none(self):
        rp = SentimentReplay({"2026-01-01": 50})
        self.assertIsNone(rp.at(pd.Timestamp("2026-02-01"), "BTCUSDT"))

    def test_unavailable_when_empty(self):
        self.assertFalse(SentimentReplay({}).available)


class TestGridValidation(unittest.TestCase):
    def test_time_stop_none_dedup(self):
        """時間停損停用時，不同 no_movement 值應只保留一個代表"""
        base = dict(level_1_pct=1.0, level_2_pct=2.0, level_3_pct=3.0)
        keep = [v for v in (0.2, 0.4, 0.8)
                if _valid_tp_order({**base, "time_stop_seconds": None,
                                    "time_stop_no_movement_pct": v})]
        self.assertEqual(keep, [0.4])

    def test_tp_order_enforced(self):
        self.assertFalse(_valid_tp_order(
            {"level_1_pct": 2.0, "level_2_pct": 1.0, "level_3_pct": 3.0,
             "time_stop_seconds": 2400, "time_stop_no_movement_pct": 0.4}))


if __name__ == "__main__":
    unittest.main()
