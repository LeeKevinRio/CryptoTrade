"""日線長期順勢：不偷看未來、進出場邏輯、成本與資金費方向、組合彙整"""

import unittest

import numpy as np
import pandas as pd

from backtest import daily_trend as dt


def _series(vals, start="2024-01-01"):
    return pd.Series(np.asarray(vals, float), index=pd.date_range(start, periods=len(vals), freq="D", tz="UTC"))


class TestPositions(unittest.TestCase):
    def test_prefix_invariant(self):
        rng = np.random.default_rng(0)
        c = _series(100 * np.cumprod(1 + rng.normal(0, 0.03, 400)))
        for r in dt.RULES:
            full = dt.positions(c, r)
            part = dt.positions(c.iloc[:250], r)
            np.testing.assert_array_equal(full[:250], part, err_msg=dt.rule_name(r))

    def test_donchian_entry_and_exit(self):
        c = _series([10] * 25 + [11, 12, 11.5] + [8] * 5)
        p = dt.positions(c, {"kind": "donchian", "n": 20, "mode": "long"})
        self.assertEqual(p[24], 0)
        self.assertEqual(p[25], 1)         # 收盤創 20 日新高 → 做多
        self.assertEqual(p[27], 1)         # 小回檔不出場
        self.assertEqual(p[28], 0)         # 跌破 10 日低點 → 出場

    def test_donchian_ls_goes_short(self):
        c = _series([10] * 25 + [9])
        self.assertEqual(dt.positions(c, {"kind": "donchian", "n": 20, "mode": "ls"})[25], -1)
        self.assertEqual(dt.positions(c, {"kind": "donchian", "n": 20, "mode": "long"})[25], 0)

    def test_sma_modes(self):
        c = _series([10] * 60 + [12] + [8])
        lo = dt.positions(c, {"kind": "sma", "n": 50, "mode": "long"})
        ls = dt.positions(c, {"kind": "sma", "n": 50, "mode": "ls"})
        self.assertEqual((lo[60], lo[61]), (1, 0))
        self.assertEqual((ls[60], ls[61]), (1, -1))


class TestReturns(unittest.TestCase):
    def test_position_applies_next_day_with_costs(self):
        c = _series([10] * 25 + [11, 11.11, 11.11])
        f = _series([0.0] * 26 + [0.001, 0.001])
        r = dt.daily_returns(c, f, {"kind": "donchian", "n": 20, "mode": "long"})
        self.assertAlmostEqual(r.iloc[25], -dt.FEE)               # 當天收盤進場：只扣手續費，吃不到當天漲幅
        self.assertAlmostEqual(r.iloc[26], 0.01 - 0.001, places=9)  # 隔天 +1%、多單付資金費
        self.assertAlmostEqual(r.iloc[27], -0.001, places=9)

    def test_short_receives_funding(self):
        c = _series([10] * 25 + [9, 9])
        f = _series([0.0] * 26 + [0.002])
        r = dt.daily_returns(c, f, {"kind": "donchian", "n": 20, "mode": "ls"})
        self.assertAlmostEqual(r.iloc[26], 0.002, places=9)


class TestPortfolio(unittest.TestCase):
    def test_buy_and_hold_monthly(self):
        idx = pd.date_range("2024-01-01", "2024-02-29", freq="D", tz="UTC")
        px = pd.DataFrame({"A": np.linspace(100, 120, len(idx)), "B": 100.0}, index=idx)
        res = dt.portfolio(px, pd.DataFrame(0.0, index=idx, columns=px.columns), None)
        total = (1 + res.monthly).prod() - 1
        self.assertGreater(total, 0)
        self.assertEqual(len(res.monthly), 2)
        self.assertGreater(res.contrib["A"], res.contrib["B"])

    def test_rule_grid(self):
        self.assertEqual(len(dt.RULES), 12)
        self.assertEqual(len({dt.rule_name(r) for r in dt.RULES}), 12)


if __name__ == "__main__":
    unittest.main()
