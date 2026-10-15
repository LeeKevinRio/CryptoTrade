"""上線關卡與槓桿蒙地卡羅 —— 驗證「驗證工具本身」不會放行假 edge、也不會錯殺真 edge"""

import unittest

import numpy as np

from backtest.validation import Trade, gate, monte_carlo, required_t

SYMS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SUIUSDT", "LINKUSDT", "TAOUSDT", "DOGEUSDT"]


def _trades(pnls, n_periods=12, syms=SYMS):
    """交易平均分到各標的與各期間（期間依序遞增，模擬時間軸）"""
    n = len(pnls)
    return [Trade(syms[i % len(syms)], f"2026-{(i * n_periods // n) + 1:02d}", float(p))
            for i, p in enumerate(pnls)]


def _check(result, name):
    return next(ok for n, ok, _ in result.checks if n == name)


class TestRequiredT(unittest.TestCase):
    def test_single_trial_floor(self):
        self.assertAlmostEqual(required_t(1), 2.0)

    def test_grows_with_trials(self):
        self.assertAlmostEqual(required_t(36), 2.99, places=1)
        self.assertGreater(required_t(7680), 4.0)
        self.assertLess(required_t(36), required_t(7680))


class TestGate(unittest.TestCase):
    def test_random_noise_rarely_passes(self):
        """零期望的純雜訊：40 次裡最多放行 2 次（偽陽性控制）"""
        passed = 0
        for seed in range(40):
            rng = np.random.default_rng(seed)
            passed += gate(_trades(rng.normal(0.0, 1.5, 800))).passed
        self.assertLessEqual(passed, 2)

    def test_real_edge_passes(self):
        rng = np.random.default_rng(1)
        r = gate(_trades(rng.normal(0.25, 1.0, 800)))
        self.assertTrue(r.passed, r.render())

    def test_too_few_trades_fails(self):
        rng = np.random.default_rng(2)
        r = gate(_trades(rng.normal(0.5, 1.0, 120)))
        self.assertFalse(_check(r, "樣本數"))
        self.assertFalse(r.passed)

    def test_edge_from_single_symbol_fails(self):
        """只有一個標的賺、其他都賠 → 不靠單一標的檢查要擋下"""
        rng = np.random.default_rng(3)
        pnls = rng.normal(-0.05, 0.5, 800)
        star = [i for i in range(800) if i % len(SYMS) == 0]
        pnls[star] = rng.normal(1.5, 0.5, len(star))
        r = gate(_trades(pnls))
        self.assertFalse(_check(r, "不靠單一標的"), r.render())
        self.assertFalse(r.passed)

    def test_thin_edge_dies_under_costs(self):
        """顯著但很薄（每筆 +0.03%）→ 多扣 0.05% 滑價就變負，成本壓力要擋下"""
        rng = np.random.default_rng(4)
        r = gate(_trades(rng.normal(0.03, 0.3, 3000)))
        self.assertTrue(_check(r, "顯著性"), r.render())
        self.assertFalse(_check(r, "成本壓力"))
        self.assertFalse(r.passed)

    def test_edge_that_decays_fails(self):
        """前半段賺、後半段賠（策略失效）→ 前後半一致要擋下"""
        rng = np.random.default_rng(5)
        pnls = np.concatenate([rng.normal(0.6, 1.0, 400), rng.normal(-0.3, 1.0, 400)])
        r = gate(_trades(pnls))
        self.assertFalse(_check(r, "前後半一致"), r.render())

    def test_halves_are_chronological_not_input_order(self):
        """輸入依標的排列時，前後半仍要按期間切"""
        rng = np.random.default_rng(6)
        early = [Trade(s, "2026-01", float(x)) for s in SYMS for x in rng.normal(0.5, 0.5, 60)]
        late = [Trade(s, "2026-12", float(x)) for s in SYMS for x in rng.normal(-0.5, 0.5, 60)]
        mixed = sorted(early + late, key=lambda t: t.symbol)   # 逐標的排列
        r = gate(mixed)
        self.assertFalse(_check(r, "前後半一致"), r.render())

    def test_multiple_testing_raises_bar(self):
        """同一組結果：只試 1 組會過顯著性，宣稱試過 7680 組就不過"""
        rng = np.random.default_rng(7)
        z = rng.normal(0, 1, 900)
        pnls = 0.1 + (z - z.mean()) / z.std(ddof=1)   # t 恰為 0.1 × √900 = 3.0
        self.assertTrue(_check(gate(_trades(pnls), n_trials=1), "顯著性"))
        self.assertFalse(_check(gate(_trades(pnls), n_trials=7680), "顯著性"))


class TestMonteCarlo(unittest.TestCase):
    def test_constant_gain_compounds(self):
        r = monte_carlo([1.0] * 50, leverage=1, position_pct=100, n_trades=10, n_paths=10)
        self.assertAlmostEqual(r.median_final, 1.01 ** 10, places=6)
        self.assertEqual(r.ruin_prob, 0.0)
        self.assertAlmostEqual(r.median_max_dd, 0.0)

    def test_constant_loss_ruins(self):
        # 10% 倉位 × 10x，每筆 -5% → 權益每筆 -5%，300 筆必破 -50%
        r = monte_carlo([-5.0] * 50, leverage=10, position_pct=10, n_trades=300, n_paths=10)
        self.assertEqual(r.ruin_prob, 1.0)

    def test_isolated_loss_capped_at_margin(self):
        """逐倉：單筆最多賠掉該筆保證金（-20% × 27x 只賠 10% 保證金，不是 54%）"""
        r = monte_carlo([-20.0], leverage=27, position_pct=10, n_trades=1, n_paths=5, block=1)
        self.assertAlmostEqual(r.median_final, 0.9, places=6)

    def test_more_leverage_more_ruin(self):
        rng = np.random.default_rng(8)
        pnls = np.where(rng.random(1000) < 0.837, 1.1, -5.08)   # 現行策略的勝率/賺賠形狀
        lo = monte_carlo(pnls, 5, 10, n_paths=2000).ruin_prob
        hi = monte_carlo(pnls, 27, 10, n_paths=2000).ruin_prob
        self.assertLessEqual(lo, hi)

    def test_all_in_27x_busts(self):
        """截圖那種下法：全帳戶 27x、-5% 停損 → 第一次停損就歸零"""
        rng = np.random.default_rng(9)
        pnls = np.where(rng.random(1000) < 0.837, 1.1, -5.08)
        r = monte_carlo(pnls, 27, 100, n_trades=100, n_paths=2000)
        self.assertGreater(r.bust_prob, 0.95)

    def test_wash_then_rally_is_liquidated_at_high_leverage(self):
        """最後 +1% 獲利、但中途曾逆向 -4%：10x（強平 9.5%）撐得住，27x（強平 3.2%）中途就爆"""
        kw = dict(position_pct=10, n_trades=1, n_paths=5, block=1, mae_pct=[-4.0])
        self.assertAlmostEqual(monte_carlo([1.0], 10, **kw).median_final, 1.01, places=6)
        r27 = monte_carlo([1.0], 27, **kw)
        self.assertAlmostEqual(r27.median_final, 0.9, places=6)     # 賠掉 10% 保證金
        self.assertEqual(r27.liquidated_share, 1.0)

    def test_missing_mae_flags_unreliable_when_liq_inside_stop(self):
        pnls = [1.1, 1.1, -5.08]
        self.assertFalse(monte_carlo(pnls, 10, 10, n_paths=10).unreliable)   # 強平 9.5% > 停損
        self.assertTrue(monte_carlo(pnls, 27, 10, n_paths=10).unreliable)    # 強平 3.2% < 停損

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            monte_carlo([], 10, 10)


if __name__ == "__main__":
    unittest.main()
