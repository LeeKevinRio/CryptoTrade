"""大盤急跌暫停：只用過去的 BTC 價格、方向正確、彙整依事先定好的規則選組合"""

import tempfile
import unittest

import numpy as np
import pandas as pd

from backtest import crash_filter as cf
from backtest.validation import Trade, write_trades_csv


class TestFilter(unittest.TestCase):
    def setUp(self):
        self.idx = pd.date_range("2026-01-01", periods=200, freq="5min")
        px = np.full(200, 100.0)
        px[100:] = 95.0                       # 第 100 根 BTC 急跌 5%
        self.btc = pd.Series(px, index=self.idx)

    def test_return_is_causal(self):
        r = cf.btc_return(self.btc, self.idx, hours=1)
        self.assertEqual(r[99], 0.0)          # 急跌前一根：看不到未來
        self.assertAlmostEqual(r[100], -0.05)
        self.assertEqual(r[112], 0.0)         # 1 小時後回看窗口已過

    def test_blocks_longs_only_during_crash(self):
        r = cf.btc_return(self.btc, self.idx, hours=1)
        sides = np.zeros(200, dtype=np.int8)
        sides[[50, 100, 105]] = 1
        sides[[101]] = -1
        out = cf.apply_filter(sides, r, drop=3.0, both=False)
        self.assertEqual(out[50], 1)          # 平靜時照做
        self.assertEqual(out[100], 0)         # 急跌中：多單擋掉
        self.assertEqual(out[105], 0)
        self.assertEqual(out[101], -1)        # 空單不擋

    def test_both_blocks_shorts_on_pump(self):
        px = np.full(200, 100.0)
        px[100:] = 105.0
        r = cf.btc_return(pd.Series(px, index=self.idx), self.idx, hours=1)
        sides = np.zeros(200, dtype=np.int8)
        sides[100] = -1
        self.assertEqual(cf.apply_filter(sides, r, 3.0, both=False)[100], -1)
        self.assertEqual(cf.apply_filter(sides, r, 3.0, both=True)[100], 0)

    def test_variant_grid(self):
        self.assertEqual(len(cf.VARIANTS), 12)
        self.assertEqual(len({cf.variant_name(v) for v in cf.VARIANTS}), 12)


class TestReport(unittest.TestCase):
    def test_selects_on_half_a_and_confirms_on_half_b(self):
        d = tempfile.mkdtemp()
        syms = ["AUSDT", "BUSDT", "CUSDT", "DUSDT"]          # A 半 = A、C；B 半 = B、D
        rng = np.random.default_rng(0)
        good = cf.variant_name(cf.VARIANTS[3])
        for s in syms:
            for v in [{"name": "base"}] + [{"name": cf.variant_name(x)} for x in cf.VARIANTS]:
                mean = 0.3 if v["name"] == good and s in ("AUSDT", "CUSDT") else 0.0
                tr = [Trade(s, f"2026-{i % 12 + 1:02d}", float(x), -1.0)
                      for i, x in enumerate(rng.normal(mean, 1.0, 300))]
                write_trades_csv(f"{d}/crash-{v['name']}-{s}.csv", tr)
        md = cf.report(d)
        self.assertIn(f"A 半選出：**{good}**", md)
        self.assertIn("B 半", md)


if __name__ == "__main__":
    unittest.main()
