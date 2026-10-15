"""順勢候選策略 —— 訊號不偷看未來、出場模擬保守、walk-forward 樣本外隔離"""

import unittest

import numpy as np
import pandas as pd

from backtest import candidates as cd
from backtest.param_sweep import generate_realistic_data


def _df(rows):
    """rows: [(open, high, low, close), ...]"""
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="1h")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


class TestSignalsNoLookahead(unittest.TestCase):
    def test_prefix_invariant(self):
        """接上未來 K 線後，前段每一根的訊號都不能改變"""
        df = generate_realistic_data(days=60, interval_minutes=60)
        k = 900
        for fam in cd.FAMILIES:
            for p in cd.combos(fam):
                full = cd.SIGNALS[fam](df, **p)
                part = cd.SIGNALS[fam](df.iloc[:k], **p)
                np.testing.assert_array_equal(full[:k], part, err_msg=f"{fam} {p}")

    def test_donchian_excludes_current_bar(self):
        rows = [(100, 101, 99, 100)] * 5 + [(100, 103, 100, 102.5)]
        s = cd.signal_donchian(_df(rows), n=5)
        self.assertEqual(s[-1], 1)              # 收盤 102.5 > 前 5 根最高 101


class TestSimulate(unittest.TestCase):
    ATR = np.full(10, 1.0)

    def test_stop_before_new_high_on_same_bar(self):
        """同一根先創高又跌破停損 → 以原停損出場，不能先用新高把停損拉上去"""
        rows = [(100, 100, 100, 100), (100, 110, 97, 99)]
        sides = np.array([1, 0], dtype=np.int8)
        t = cd.simulate(_df(rows), sides, stop_atr=2.0, trail_atr=3.0, atr=self.ATR[:2])
        self.assertAlmostEqual(t[0][3], -2.0 - cd.FEE)

    def test_gap_through_stop_exits_at_open(self):
        rows = [(100, 100, 100, 100), (95, 96, 94, 95)]
        sides = np.array([1, 0], dtype=np.int8)
        t = cd.simulate(_df(rows), sides, stop_atr=2.0, trail_atr=3.0, atr=self.ATR[:2])
        self.assertAlmostEqual(t[0][3], -5.0 - cd.FEE)     # 開盤 95 已低於停損 98

    def test_trailing_locks_profit(self):
        """漲到 110 後回落：吊燈停損 = 110 - 3 = 107 → 獲利 +7%"""
        rows = [(100, 100, 100, 100), (100, 105, 100, 105), (105, 110, 105, 110),
                (110, 110, 106, 106)]
        sides = np.array([1, 0, 0, 0], dtype=np.int8)
        t = cd.simulate(_df(rows), sides, stop_atr=2.0, trail_atr=3.0, atr=self.ATR[:4])
        self.assertAlmostEqual(t[0][3], 7.0 - cd.FEE)

    def test_short_mirror(self):
        rows = [(100, 100, 100, 100), (100, 100, 95, 95), (95, 95, 90, 90), (90, 94, 90, 94)]
        sides = np.array([-1, 0, 0, 0], dtype=np.int8)
        t = cd.simulate(_df(rows), sides, stop_atr=2.0, trail_atr=3.0, atr=self.ATR[:4])
        self.assertAlmostEqual(t[0][3], 7.0 - cd.FEE)      # 停損追到 93

    def test_mae_recorded(self):
        rows = [(100, 100, 100, 100), (100, 100, 98.5, 99), (99, 112, 99, 112), (112, 112, 100, 100)]
        sides = np.array([1, 0, 0, 0], dtype=np.int8)
        t = cd.simulate(_df(rows), sides, stop_atr=2.0, trail_atr=3.0, atr=self.ATR[:4])
        self.assertAlmostEqual(t[0][4], -1.5)

    def test_one_position_at_a_time(self):
        df = generate_realistic_data(days=60, interval_minutes=60)
        sides = cd.signal_donchian(df, n=24)
        t = cd.simulate(df, sides, stop_atr=1.5, trail_atr=3.0)
        for a, b in zip(t, t[1:]):
            self.assertGreater(b[0], a[1])


class TestWalkForward(unittest.TestCase):
    def test_test_trades_only_from_test_windows(self):
        df = generate_realistic_data(days=120, interval_minutes=60)
        train, test = 60 * 24, 15 * 24
        trades = cd.walk_forward(df, "donchian", "X", train, test, min_trades=1)
        first_test_month = f"{df.index[train]:%Y-%m}"
        self.assertTrue(trades)
        self.assertTrue(all(t.period >= first_test_month for t in trades))
        self.assertTrue(all(t.mae_pct is not None and t.mae_pct <= 0 for t in trades))

    def test_selection_ignores_trades_that_exit_after_train(self):
        """訓練窗內進場、但出場落在測試期的交易不能拿來選參"""
        calls = {}
        real = cd.simulate

        def fake_sim(df, sides, stop_atr, trail_atr, atr=None, **_):
            # 參數 A：唯一的大賺交易「跨越」訓練窗終點；參數 B：小賺且完全在窗內
            if stop_atr == 1.5:
                return [(10, 30, 1, 50.0, -1.0, 1.0), (35, 39, 1, -1.0, -1.0, 1.0)]
            return [(10, 12, 1, 0.5, -0.1, 1.0), (35, 37, 1, 0.5, -0.1, 1.0)]

        cd.simulate = fake_sim
        try:
            df = _df([(100, 100, 100, 100)] * 40)
            trades = cd.walk_forward(df, "donchian", "X", 20, 20, min_trades=1)
        finally:
            cd.simulate = real
        # 選參若偷看到 A 的 +50%，測試期會拿到 A 的 -1；正確應選 B → 測試期 +0.5
        self.assertTrue(trades)
        self.assertTrue(all(t.pnl_pct == 0.5 for t in trades), [t.pnl_pct for t in trades])

    def test_total_trials(self):
        self.assertEqual(cd.total_trials(), 2 * (12 + 12 + 8))   # 加入趨勢過濾版後照實累計

    def test_trend_filter_never_trades_against_trend(self):
        df = generate_realistic_data(days=120, interval_minutes=60)
        trend = cd.trend_direction(df)
        for fam in ("donchian_t", "ema_cross_t", "squeeze_t"):
            s = cd.SIGNALS[fam](df, **cd.combos(fam)[0])
            nz = s != 0
            np.testing.assert_array_equal(s[nz], trend[nz], err_msg=fam)

    def test_risk_pct_is_initial_stop_distance(self):
        rows = [(100, 100, 100, 100), (100, 100, 97, 99)]
        t = cd.simulate(_df(rows), np.array([1, 0], dtype=np.int8), stop_atr=2.0,
                        trail_atr=3.0, atr=np.full(2, 1.0))
        self.assertAlmostEqual(t[0][5], 2.0)       # 2 × ATR 1 ÷ 100


if __name__ == "__main__":
    unittest.main()
