"""回測完整性 —— 回測結論可信的前提：不偷看未來、出場順序保守、樣本外真的是樣本外"""

import unittest

import numpy as np

import backtest.walk_forward as wf
from backtest.param_sweep import fast_backtest, generate_realistic_data, precompute_sides
from src.utils.config import load_config

PARAMS = dict(stop_loss_pct=5.0, level_1_pct=1.5, level_2_pct=2.5, level_3_pct=3.0,
              trail_callback=0.4, time_stop_seconds=None, time_stop_no_movement_pct=0.4)


class TestNoLookahead(unittest.TestCase):
    def test_signals_unchanged_when_future_bars_appended(self):
        """第 i 根的訊號只能由 ≤ i 的資料決定：接上未來 K 線後，前段訊號必須一模一樣"""
        cfg = load_config()["bots"]["futures"]
        flat = {"strategy": cfg["strategy"], "risk": {}}
        df = generate_realistic_data(days=4, interval_minutes=5)
        k = 700
        full = precompute_sides(df, flat, "BTCUSDT", faithful=True)
        trunc = precompute_sides(df.iloc[:k], flat, "BTCUSDT", faithful=True)
        np.testing.assert_array_equal(full[:k], trunc)


class TestFastBacktestConservative(unittest.TestCase):
    def _one_trade(self, highs, lows, closes, side=1):
        n = len(closes)
        sides = np.zeros(n, dtype=np.int8)
        sides[0] = side
        out: list = []
        pnls = fast_backtest(np.array(closes, float), np.array(highs, float),
                             np.array(lows, float), sides, PARAMS, 1.0, warmup=0,
                             trades_out=out)
        return pnls, out

    def test_stop_wins_when_bar_hits_both(self):
        """同一根同時碰到停損與停利 → 保守算停損（不知道哪個先到）"""
        pnls, out = self._one_trade(highs=[100, 104], lows=[100, 94], closes=[100, 100])
        self.assertEqual(pnls, [-5.0])
        self.assertEqual(out[0][4], "SL")

    def test_short_stop_uses_high(self):
        pnls, _ = self._one_trade(highs=[100, 105.5], lows=[100, 99], closes=[100, 105], side=-1)
        self.assertEqual(pnls, [-5.0])

    def test_full_ladder_max_gain(self):
        """一路衝過三階 → 最大獲利 = 0.4×1.5 + 0.35×2.5 + 0.25×3.0 = 2.225%"""
        pnls, out = self._one_trade(highs=[100, 103.5], lows=[100, 100], closes=[100, 103])
        self.assertAlmostEqual(pnls[0], 2.225, places=3)
        self.assertEqual(out[0][4], "TP")

    def test_trades_out_matches_pnls_and_never_overlaps(self):
        df = generate_realistic_data(days=10, interval_minutes=5)
        c, h, l = (df[x].to_numpy(float) for x in ("close", "high", "low"))
        rng = np.random.default_rng(0)
        sides = np.where(rng.random(len(c)) < 0.02, rng.choice([-1, 1], len(c)), 0).astype(np.int8)
        out: list = []
        pnls = fast_backtest(c, h, l, sides, PARAMS, 1.0, trades_out=out)
        self.assertEqual([t[3] for t in out], pnls)
        for (i, j, *_), (i2, *_ ) in zip(out, out[1:]):
            self.assertGreater(j, i)
            self.assertGreater(i2, j)          # 下一筆一定在上一筆出場之後才進場


class TestWalkForwardIsOutOfSample(unittest.TestCase):
    def test_selection_never_sees_test_window(self):
        """選參只能用訓練窗；測試窗只被呼叫一次且用選出的參數"""
        n, train, test = 1000, 300, 100
        closes = np.linspace(100, 110, n)
        highs, lows = closes + 0.5, closes - 0.5
        sides = np.zeros(n, dtype=np.int8)
        sides[::7] = 1
        calls = []
        real = wf.fast_backtest

        def spy(c, *a, **kw):
            start = int(np.argmin(np.abs(closes - c[0])))   # closes 嚴格遞增 → 以首值定位
            calls.append((start, start + len(c)))
            return real(c, *a, **kw)

        import pandas as pd
        df = pd.DataFrame({"close": closes, "high": highs, "low": lows})
        combos = wf._combos()
        wf.fast_backtest = spy
        try:
            folds = wf.run_symbol("X", df, sides, train, test, combos, min_trades=0)
        finally:
            wf.fast_backtest = real

        self.assertGreater(len(folds), 0)
        per_fold = len(combos) + 1
        self.assertEqual(len(calls), per_fold * len(folds))
        for k, f in enumerate(folds):
            chunk = calls[k * per_fold:(k + 1) * per_fold]
            for s, e in chunk[:-1]:                        # 選參呼叫
                self.assertEqual((s, e), (f.train_start, f.train_end))
            self.assertEqual(chunk[-1], (f.train_end, f.test_end))   # 測試呼叫
            self.assertEqual(f.test_end - f.train_end, test)

    def test_fee_deducted_from_test_pnls(self):
        """樣本外 pnl 必須已扣手續費：一筆衝過三階的交易 = 2.225 - FEE"""
        import pandas as pd
        closes = np.concatenate([np.full(70, 100.0), np.full(200, 104.0), np.full(130, 108.2)])
        sides = np.zeros(len(closes), dtype=np.int8)
        sides[[60, 260]] = 1                    # 訓練窗一筆、測試窗一筆
        df = pd.DataFrame({"close": closes, "high": closes + 0.01, "low": closes - 0.01})
        folds = wf.run_symbol("X", df, sides, 200, 200, [PARAMS], min_trades=0)
        self.assertEqual(len(folds), 1)
        self.assertEqual(len(folds[0].test_pnls), 1)
        self.assertAlmostEqual(folds[0].test_pnls[0], 2.225 - wf.FEE, places=6)

if __name__ == "__main__":
    unittest.main()
