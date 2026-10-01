"""出場結構重設計模擬器：四個新機制的語義"""

import unittest
import numpy as np

from backtest.exit_redesign import simulate, ENTRY_MODES, FIXED

BASE = {"entry_mode": "full", "stop_mode": "pct", "stop_loss_pct": 5.0, "breakeven_after_l1": False,
        "trail_callback_after_l1": 0.4, "tp_levels": (1.5, 2.5, 3.0), "close_pcts": (40, 35, 25), **FIXED}


def path(closes):
    c = np.array(closes, dtype=float)
    return c, c * 1.001, c * 0.999


def sim(closes, **over):
    c, h, l = path(closes)
    sides = np.zeros(len(c), dtype=np.int8)
    sides[1] = 1
    return simulate(c, h, l, sides, {**BASE, **over}, warmup=1)


class TestSimulate(unittest.TestCase):
    def test_full_entry_stop_loss(self):
        pnls = sim([100, 100, 99, 96, 94])           # 直接跌破 -5%
        self.assertEqual(len(pnls), 1)
        self.assertAlmostEqual(pnls[0], -5.0, places=2)

    def test_dca_loser_carries_full_size(self):
        # 跌 -0.5/-1.0 加碼後再跌破停損 → 100% 部位吃到虧損（攤平放大虧損）
        pnls = sim([100, 100, 99.4, 98.9, 94], entry_mode="dca")
        self.assertLess(pnls[0], -4.5)
        pnls_full = sim([100, 100, 99.4, 98.9, 94], entry_mode="full")
        self.assertLess(pnls[0], pnls_full[0] + 0.5)   # dca 不會比 full 好

    def test_pyramid_adds_only_in_profit(self):
        # 沒漲到 +0.5% 就停損 → 只有 50% 部位受傷
        pnls = sim([100, 100, 99.8, 94], entry_mode="pyramid")
        self.assertAlmostEqual(pnls[0], -2.5, places=1)

    def test_breakeven_after_l1_turns_loser_into_scratch(self):
        # 觸 L1（+1.5%）平 40%，之後跌回進場價以下 → 剩餘 60% 在保本價出場
        # （追蹤回撤設很寬，排除移動停利先觸發）
        pnls = sim([100, 100, 101.6, 100.5, 99.5, 97], breakeven_after_l1=True, trail_callback_after_l1=5.0)
        self.assertAlmostEqual(pnls[0], 0.4 * 1.5, places=1)
        pnls_no = sim([100, 100, 101.6, 100.5, 99.5, 94], breakeven_after_l1=False, trail_callback_after_l1=5.0)
        self.assertLess(pnls_no[0], 0)

    def test_wide_trail_after_l1_rides_further(self):
        # L1 後寬追蹤：+1.5 → 小回撤 0.6 不出場 → 續漲到 +6 → 回撤 1.5 出場
        c = [100, 100, 101.6, 101.0, 103, 105, 106.1, 104.5]
        wide = sim(c, trail_callback_after_l1=1.5)
        tight = sim(c, trail_callback_after_l1=0.4)
        self.assertGreater(wide[0], tight[0])

    def test_swing_stop_uses_recent_low_capped(self):
        # 最近低點在 -1%（含 buffer 0.2 → 1.2%），上限 5 → 停損 1.2%
        closes = [100] * 5 + [99.0] + [100] * 8
        c = np.array(closes, dtype=float); h = c * 1.001; l = c * 0.999
        sides = np.zeros(len(c), dtype=np.int8); sides[12] = 1
        c2 = np.append(c, [98.9, 98.5]); h2 = np.append(h, [99.0, 98.6]); l2 = np.append(l, [98.7, 98.2])
        sides = np.append(sides, [0, 0]).astype(np.int8)
        pnls = simulate(c2, h2, l2, sides, {**BASE, "stop_mode": "swing", "swing_lookback": 12}, warmup=1)
        self.assertEqual(len(pnls), 1)
        self.assertGreater(pnls[0], -1.4)
        self.assertLess(pnls[0], -1.0)

    def test_short_side_symmetry(self):
        c = np.array([100, 100, 98.4, 99.0, 100.5, 101.5, 106], dtype=float)
        h = c * 1.001; l = c * 0.999
        sides = np.zeros(len(c), dtype=np.int8); sides[1] = -1
        pnls = simulate(c, h, l, sides, {**BASE, "breakeven_after_l1": True,
                                         "trail_callback_after_l1": 5.0}, warmup=1)
        self.assertEqual(len(pnls), 1)
        self.assertAlmostEqual(pnls[0], 0.4 * 1.5, places=1)  # L1 40% 後保本出場


if __name__ == "__main__":
    unittest.main()
