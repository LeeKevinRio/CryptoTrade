"""緊急出場：基準與日線回測一致、盤中觸價出場、跳空用開盤價、判定規則"""

import unittest

import numpy as np
import pandas as pd

from backtest import daily_trend as dt
from backtest import emergency_exit as ee


def _idx(n):
    return pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")


class TestSimulate(unittest.TestCase):
    def test_no_emergency_matches_daily_trend(self):
        rng = np.random.default_rng(0)
        c = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.03, 300)), index=_idx(300))
        f = pd.Series(0.0002, index=c.index)
        base = dt.daily_returns(c, f, {"kind": "sma", "n": 50, "mode": "long"})
        mine = ee.simulate(c, c * 0.98, c, f, None)
        np.testing.assert_allclose(mine.to_numpy()[1:], base.to_numpy()[1:], atol=1e-12)

    def _uptrend_then(self, last_open, last_low, last_close):
        n = 60
        c = np.linspace(100, 160, n)                       # 一路站上均線 → 持多
        o, lo = c.copy(), c * 0.999
        o, lo, c = np.append(o, last_open), np.append(lo, last_low), np.append(c, last_close)
        idx = _idx(n + 1)
        s = lambda a: pd.Series(a, index=idx)
        return s(o), s(lo), s(c), s(np.zeros(n + 1))

    def test_day_drop_exits_at_trigger(self):
        o, lo, c, f = self._uptrend_then(160, 130, 150)     # 盤中跌到 -18.75%，收盤 150
        r = ee.simulate(o, lo, c, f, {"kind": "day_drop", "x": 0.10})
        prev = c.iloc[-2]
        # 在 -10% 觸價賣出（扣手續費）；收盤 150 仍站上均線 → 收盤買回（再扣一次手續費）
        self.assertAlmostEqual(r.iloc[-1], -0.10 - 2 * dt.FEE, places=9)
        no = ee.simulate(o, lo, c, f, None)
        self.assertAlmostEqual(no.iloc[-1], 150 / prev - 1, places=9)

    def test_gap_down_fills_at_open(self):
        o, lo, c, f = self._uptrend_then(120, 110, 115)     # 開盤就跳空 -25%
        r = ee.simulate(o, lo, c, f, {"kind": "day_drop", "x": 0.10})
        self.assertAlmostEqual(r.iloc[-1], 120 / 160 - 1 - dt.FEE, places=9)   # 收盤跌破均線 → 不買回

    def test_not_triggered_when_low_stays_above(self):
        o, lo, c, f = self._uptrend_then(160, 150, 155)     # 盤中最多 -6.25%
        a = ee.simulate(o, lo, c, f, {"kind": "day_drop", "x": 0.10})
        b = ee.simulate(o, lo, c, f, None)
        self.assertAlmostEqual(a.iloc[-1], b.iloc[-1], places=12)


class TestDecision(unittest.TestCase):
    def _ds(self, seed):
        rng = np.random.default_rng(seed)
        n = 900
        c = pd.DataFrame(100 * np.cumprod(1 + rng.normal(0.001, 0.04, (n, 2)), axis=0),
                         index=_idx(n), columns=["A", "B"])
        lo = c * (1 - np.abs(rng.normal(0, 0.03, c.shape)))
        return (c.shift(1).fillna(c), lo, c, None)

    def test_report_has_all_variants_and_a_verdict(self):
        md = ee.evaluate({"開發": self._ds(1), "確認1": self._ds(2), "確認2": self._ds(3)})
        for v in ee.VARIANTS:
            self.assertIn(ee.variant_name(v), md)
        self.assertTrue("不採用" in md or "採用" in md)

    def test_variant_grid(self):
        self.assertEqual(len(ee.VARIANTS), 6)


if __name__ == "__main__":
    unittest.main()
