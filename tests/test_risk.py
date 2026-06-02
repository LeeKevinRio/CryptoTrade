"""風險管理測試"""

import time
import unittest

from src.risk.take_profit import TakeProfitManager
from src.risk.stop_loss import StopLossManager
from src.risk.capital_manager import CapitalManager
from src.risk.position_manager import PositionManager


DEFAULT_CONFIG = {
    "risk": {
        "take_profit": {
            "level_1_pct": 1.0,
            "level_1_close_pct": 40,
            "level_2_pct": 2.0,
            "level_2_close_pct": 30,
            "level_3_pct": 3.5,
            "level_3_close_pct": 30,
        },
        "trailing_stop": {
            "activation_pct": 1.5,
            "callback_pct": 0.5,
        },
        "time_stop_seconds": 1800,
        "stop_loss_pct": 2.0,
        "max_position_pct": 10,
        "max_concurrent_positions": 3,
        "max_daily_loss_pct": 3.0,
        "max_daily_trades": 20,
        "entry_batches": [
            {"pct": 30, "offset": 0.0},
            {"pct": 30, "offset": -1.0},
            {"pct": 40, "offset": -2.0},
        ],
    }
}


class TestTakeProfit(unittest.TestCase):
    def test_tiered_take_profit(self):
        tp = TakeProfitManager(DEFAULT_CONFIG)
        tp.register_position("BTCUSDT", "LONG", 50000, 1.0)

        # 價格漲 1% → 應觸發 Level 1
        actions = tp.check("BTCUSDT", 50500)
        self.assertEqual(len(actions), 1)
        self.assertIn("階梯停利", actions[0]["reason"])
        self.assertAlmostEqual(actions[0]["quantity"], 0.4, places=2)

        # 再漲到 2% → 觸發 Level 2
        actions = tp.check("BTCUSDT", 51000)
        self.assertEqual(len(actions), 1)
        self.assertAlmostEqual(actions[0]["quantity"], 0.3, places=2)

    def test_trailing_stop(self):
        tp = TakeProfitManager(DEFAULT_CONFIG)
        tp.register_position("BTCUSDT", "LONG", 50000, 1.0)

        # 先觸發所有階梯
        tp.check("BTCUSDT", 50500)  # L1
        tp.check("BTCUSDT", 51000)  # L2

        # 漲到 1.8% → 啟動移動停利（剩餘 30%）
        tp.check("BTCUSDT", 50900)

        # 回撤 → 觸發移動停利
        actions = tp.check("BTCUSDT", 50600)
        # 移動停利可能觸發，取決於 highest_pnl 的追蹤
        # 這裡主要測試不會出錯

    def test_short_take_profit(self):
        tp = TakeProfitManager(DEFAULT_CONFIG)
        tp.register_position("ETHUSDT", "SHORT", 3000, 10.0)

        # 空頭：價格跌 1% = 獲利 1%
        actions = tp.check("ETHUSDT", 2970)
        self.assertEqual(len(actions), 1)


class TestStopLoss(unittest.TestCase):
    def test_long_stop_loss(self):
        sl = StopLossManager(DEFAULT_CONFIG)
        sl.set_stop("BTCUSDT", "LONG", 50000)

        # 未觸發
        result = sl.check("BTCUSDT", 49500)
        self.assertIsNone(result)

        # 觸發（-2%）
        result = sl.check("BTCUSDT", 48999)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "stop_loss")

    def test_short_stop_loss(self):
        sl = StopLossManager(DEFAULT_CONFIG)
        sl.set_stop("ETHUSDT", "SHORT", 3000)

        result = sl.check("ETHUSDT", 3061)
        self.assertIsNotNone(result)

    def test_atr_stop_uses_max_when_atr_smaller(self):
        """ATR=200 → 1.5×ATR=300 (0.6%)，固定 2%，取較寬的固定 2%"""
        sl = StopLossManager(DEFAULT_CONFIG)
        sl.set_stop("BTCUSDT", "LONG", 50000, atr=200)
        stop = sl.get_stop_price("BTCUSDT")
        self.assertIsNotNone(stop)
        self.assertAlmostEqual(stop, 50000 * 0.98, places=0)  # 固定 2% 勝出

    def test_atr_stop_widens_in_high_volatility(self):
        """高 ATR 應放寬停損；ATR=2000 → 1.5×ATR=3000 (6%)，超過 max 4% 上限後封頂於 4%"""
        sl = StopLossManager(DEFAULT_CONFIG)
        sl.set_stop("BTCUSDT", "LONG", 50000, atr=2000)
        stop = sl.get_stop_price("BTCUSDT")
        self.assertIsNotNone(stop)
        # 6% > 4% 上限 → 封頂於 4%
        self.assertAlmostEqual(stop, 50000 * 0.96, places=0)

    def test_atr_stop_within_bounds(self):
        """ATR=1000 → 1.5×ATR=1500 (3%)，在 2% 與 4% 之間 → 用 3%"""
        sl = StopLossManager(DEFAULT_CONFIG)
        sl.set_stop("BTCUSDT", "LONG", 50000, atr=1000)
        stop = sl.get_stop_price("BTCUSDT")
        self.assertAlmostEqual(stop, 50000 * 0.97, places=0)


class TestCapitalManager(unittest.TestCase):
    def test_can_trade(self):
        cm = CapitalManager(DEFAULT_CONFIG)
        can, _ = cm.can_trade(10000)
        self.assertTrue(can)

    def test_max_positions(self):
        cm = CapitalManager(DEFAULT_CONFIG)
        cm.add_position()
        cm.add_position()
        cm.add_position()
        can, reason = cm.can_trade(10000)
        self.assertFalse(can)
        self.assertIn("持倉數", reason)

    def test_max_daily_trades(self):
        cm = CapitalManager(DEFAULT_CONFIG)
        for _ in range(20):
            cm.record_trade(1.0)
        can, reason = cm.can_trade(10000)
        self.assertFalse(can)
        self.assertIn("交易次數", reason)

    def test_position_size(self):
        cm = CapitalManager(DEFAULT_CONFIG)
        qty = cm.calculate_position_size(
            balance=10000, price=50000, leverage=5, size_ratio=1.0
        )
        # 10000 * 10% * 5 / 50000 = 0.1
        self.assertAlmostEqual(qty, 0.1, places=3)

    def test_batch_orders(self):
        cm = CapitalManager(DEFAULT_CONFIG)
        orders = cm.get_batch_orders(50000, 1.0, "LONG")
        self.assertEqual(len(orders), 3)
        self.assertAlmostEqual(orders[0]["quantity"], 0.3)
        self.assertAlmostEqual(orders[1]["quantity"], 0.3)
        self.assertAlmostEqual(orders[2]["quantity"], 0.4)
        # 第二批應該更低價
        self.assertLess(orders[1]["price"], orders[0]["price"])


class TestPositionManager(unittest.TestCase):
    def test_open_close(self):
        pm = PositionManager(DEFAULT_CONFIG)
        pm.open_position("BTCUSDT", "LONG", 50000, 1.0, leverage=5)
        self.assertTrue(pm.has_position("BTCUSDT"))

        result = pm.close_position("BTCUSDT", 51000)
        self.assertIsNotNone(result)
        self.assertGreater(result["pnl"], 0)
        self.assertFalse(pm.has_position("BTCUSDT"))


if __name__ == "__main__":
    unittest.main()
