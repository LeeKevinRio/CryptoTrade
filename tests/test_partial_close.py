"""部分平倉語義 + maker 停利階梯測試"""

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.risk.position_manager import PositionManager
from src.risk.take_profit import TakeProfitManager
from src.execution.order_executor import OrderExecutor

BASE_CONFIG = {
    "leverage": 5,
    "risk": {
        "take_profit": {
            "level_1_pct": 1.5, "level_1_close_pct": 40,
            "level_2_pct": 2.5, "level_2_close_pct": 35,
            "level_3_pct": 3.0, "level_3_close_pct": 25,
        },
        "trailing_stop": {"activation_pct": 1.0, "callback_pct": 0.4},
        "time_stop_seconds": 2400,
        "stop_loss_pct": 4.0,
        "max_position_pct": 12,
        "max_concurrent_positions": 4,
        "max_daily_loss_pct": 25.0,
        "max_daily_trades": 150,
    },
}


def maker_config():
    cfg = copy.deepcopy(BASE_CONFIG)
    cfg["risk"]["use_maker_tp"] = True
    return cfg


class TestPartialClose(unittest.TestCase):
    def _open(self, pm):
        pm.open_position("BTCUSDT", "LONG", 50000, 1.0, leverage=5, trade_id=7)

    def test_partial_close_keeps_position_and_risk_state(self):
        pm = PositionManager(copy.deepcopy(BASE_CONFIG))
        self._open(pm)
        result = pm.close_position("BTCUSDT", 51000, quantity=0.4)
        self.assertTrue(result["partial"])
        self.assertAlmostEqual(result["pnl"], 400, places=4)          # (51000-50000)*0.4
        self.assertAlmostEqual(result["remaining_quantity"], 0.6)
        # 倉位仍在、風控狀態仍在
        self.assertTrue(pm.has_position("BTCUSDT"))
        self.assertIsNotNone(pm.sl_manager.get_stop_price("BTCUSDT"))
        self.assertIn("BTCUSDT", pm.tp_manager._states)

    def test_final_close_includes_realized_pnl(self):
        pm = PositionManager(copy.deepcopy(BASE_CONFIG))
        self._open(pm)
        pm.close_position("BTCUSDT", 51000, quantity=0.4)   # +400
        result = pm.close_position("BTCUSDT", 52000)         # 剩 0.6 × 2000 = +1200
        self.assertNotIn("partial", result)
        self.assertAlmostEqual(result["pnl"], 1600, places=4)
        self.assertFalse(pm.has_position("BTCUSDT"))

    def test_close_with_full_quantity_is_full_close(self):
        pm = PositionManager(copy.deepcopy(BASE_CONFIG))
        self._open(pm)
        result = pm.close_position("BTCUSDT", 50500, quantity=1.0)
        self.assertNotIn("partial", result)
        self.assertFalse(pm.has_position("BTCUSDT"))

    def test_sync_external_fill_shrink_books_partial(self):
        pm = PositionManager(maker_config())
        self._open(pm)
        result = pm.sync_external_fill("BTCUSDT", 0.6, 50750)   # 掛單成交 0.4
        self.assertTrue(result["partial"])
        self.assertAlmostEqual(result["quantity"], 0.4)
        self.assertAlmostEqual(pm.get_position("BTCUSDT").quantity, 0.6)
        # tp 狀態同步
        self.assertAlmostEqual(
            pm.tp_manager._states["BTCUSDT"].remaining_quantity, 0.6)

    def test_sync_external_fill_grow_updates_qty(self):
        pm = PositionManager(maker_config())
        self._open(pm)
        result = pm.sync_external_fill("BTCUSDT", 1.5, 49800)   # 批次進場成交
        self.assertIsNone(result)
        self.assertAlmostEqual(pm.get_position("BTCUSDT").quantity, 1.5)
        self.assertAlmostEqual(pm.tp_manager._states["BTCUSDT"].total_quantity, 1.5)

    def test_sync_external_fill_no_change(self):
        pm = PositionManager(maker_config())
        self._open(pm)
        self.assertIsNone(pm.sync_external_fill("BTCUSDT", 1.0, 50000))


class TestExchangeLadderMode(unittest.TestCase):
    def test_ladder_marks_but_does_not_close(self):
        tp = TakeProfitManager(maker_config())
        tp.register_position("BTCUSDT", "LONG", 50000, 1.0)
        actions = tp.check("BTCUSDT", 50800)   # +1.6% 過 L1
        self.assertEqual(actions, [])
        self.assertTrue(tp._states["BTCUSDT"].levels[0].triggered)
        # remaining 不因觸價變動（等對帳）
        self.assertAlmostEqual(tp._states["BTCUSDT"].remaining_quantity, 1.0)

    def test_trailing_still_fires_in_ladder_mode(self):
        tp = TakeProfitManager(maker_config())
        tp.register_position("BTCUSDT", "LONG", 50000, 1.0)
        tp.check("BTCUSDT", 50800)             # +1.6% 啟動 trailing (activation 1.0)
        actions = tp.check("BTCUSDT", 50500)   # 回撤 0.6% > callback 0.4
        self.assertEqual(len(actions), 1)
        self.assertIn("移動停利", actions[0]["reason"])

    def test_software_ladder_unchanged_when_flag_off(self):
        tp = TakeProfitManager(copy.deepcopy(BASE_CONFIG))
        tp.register_position("BTCUSDT", "LONG", 50000, 1.0)
        actions = tp.check("BTCUSDT", 50800)
        self.assertEqual(len(actions), 1)
        self.assertIn("階梯停利", actions[0]["reason"])


class TestExecutorMakerTP(unittest.TestCase):
    def _executor(self, cfg):
        api = MagicMock()
        api.futures_limit_order = AsyncMock(return_value={"orderId": 1})
        api.futures_market_order = AsyncMock(
            return_value={"orderId": 2, "avgPrice": "50500", "executedQty": "0.4"})
        api.cancel_all_orders = AsyncMock()
        pm = PositionManager(cfg)
        ex = OrderExecutor(api=api, position_manager=pm, config=cfg, mode="futures")
        ex._symbol_info["BTCUSDT"] = {
            "qty_precision": 3, "price_precision": 2, "min_qty": 0.001}
        return ex, api, pm

    def test_place_tp_ladder_long(self):
        ex, api, _ = self._executor(maker_config())
        asyncio.run(ex._place_tp_ladder("BTCUSDT", "LONG", 50000, 1.0))
        calls = api.futures_limit_order.call_args_list
        self.assertEqual(len(calls), 3)
        prices = [c.kwargs["price"] for c in calls]
        self.assertEqual(prices, [50750.0, 51250.0, 51500.0])   # +1.5/2.5/3.0%
        qtys = [c.kwargs["quantity"] for c in calls]
        self.assertEqual(qtys, [0.4, 0.35, 0.25])
        for c in calls:
            self.assertEqual(c.kwargs["side"], "SELL")
            self.assertEqual(c.kwargs["time_in_force"], "GTX")
            self.assertTrue(c.kwargs["reduce_only"])

    def test_place_tp_ladder_short_prices_below_entry(self):
        ex, api, _ = self._executor(maker_config())
        asyncio.run(ex._place_tp_ladder("BTCUSDT", "SHORT", 50000, 1.0))
        prices = [c.kwargs["price"] for c in api.futures_limit_order.call_args_list]
        self.assertEqual(prices, [49250.0, 48750.0, 48500.0])   # -1.5/2.5/3.0%
        for c in api.futures_limit_order.call_args_list:
            self.assertEqual(c.kwargs["side"], "BUY")

    def test_ladder_failure_does_not_raise(self):
        ex, api, _ = self._executor(maker_config())
        api.futures_limit_order = AsyncMock(side_effect=RuntimeError("boom"))
        asyncio.run(ex._place_tp_ladder("BTCUSDT", "LONG", 50000, 1.0))  # 不應拋出

    def test_partial_close_keeps_exchange_orders(self):
        ex, api, pm = self._executor(maker_config())
        pm.open_position("BTCUSDT", "LONG", 50000, 1.0, leverage=5)
        result = asyncio.run(ex.close_position("BTCUSDT", 0.4, "測試部分平倉"))
        self.assertTrue(result["partial"])
        api.cancel_all_orders.assert_not_called()
        # 平倉市價單必須 reduceOnly
        self.assertTrue(api.futures_market_order.call_args.kwargs["reduce_only"])

    def test_full_close_cancels_exchange_orders(self):
        ex, api, pm = self._executor(maker_config())
        pm.open_position("BTCUSDT", "LONG", 50000, 0.4, leverage=5)
        result = asyncio.run(ex.close_position("BTCUSDT", 0.4, "測試全平"))
        self.assertNotIn("partial", result)
        api.cancel_all_orders.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TestDailyTradeCounting(unittest.TestCase):
    """部分平倉不得消耗每日交易次數／污染連敗計數

    maker 停利階梯一筆倉位會產生 3 次部分成交 + 1 次全平；若每段都計為一筆，
    max_daily_trades=20 只需 5 個真實部位就會耗盡，當天不再開倉。
    """

    def _pm(self):
        pm = PositionManager(copy.deepcopy(BASE_CONFIG))
        pm.capital_manager.max_daily_trades = 20
        pm.capital_manager.max_consecutive_losses = 3
        return pm

    def test_partial_closes_do_not_count_as_trades(self):
        pm = self._pm()
        cm = pm.capital_manager
        pm.open_position("BTCUSDT", "LONG", 50000, 1.0, leverage=5)
        pm.close_position("BTCUSDT", 50750, quantity=0.4)   # TP1
        pm.close_position("BTCUSDT", 51250, quantity=0.35)  # TP2
        self.assertEqual(cm.daily_trades, 0)                # 尚未完成一筆交易
        pm.close_position("BTCUSDT", 51500)                 # 全平
        self.assertEqual(cm.daily_trades, 1)                # 整段只算一筆

    def test_daily_pnl_still_accumulates_every_leg(self):
        pm = self._pm()
        cm = pm.capital_manager
        pm.open_position("BTCUSDT", "LONG", 100, 1.0, leverage=5)
        pm.close_position("BTCUSDT", 110, quantity=0.5)     # +5
        pm.close_position("BTCUSDT", 120)                   # +10
        self.assertAlmostEqual(cm.daily_pnl, 15.0)

    def test_consecutive_losses_use_total_not_leg(self):
        # 分段小賺、整筆實虧 → 應計為一次連敗
        pm = self._pm()
        cm = pm.capital_manager
        pm.open_position("BTCUSDT", "LONG", 100, 1.0, leverage=5)
        pm.close_position("BTCUSDT", 110, quantity=0.2)     # 分段 +2
        pm.close_position("BTCUSDT", 80)                    # 剩 0.8 × -20 = -16，總計 -14
        self.assertEqual(cm.consecutive_losses, 1)

    def test_full_close_profit_resets_streak(self):
        pm = self._pm()
        cm = pm.capital_manager
        cm._consecutive_losses = 2
        pm.open_position("BTCUSDT", "LONG", 100, 1.0, leverage=5)
        pm.close_position("BTCUSDT", 110)
        self.assertEqual(cm.consecutive_losses, 0)

    def test_twenty_real_trades_before_daily_cap(self):
        pm = self._pm()
        cm = pm.capital_manager
        for i in range(20):
            pm.open_position("BTCUSDT", "LONG", 100, 1.0, leverage=5)
            pm.close_position("BTCUSDT", 101, quantity=0.4)   # 階梯分段
            pm.close_position("BTCUSDT", 102, quantity=0.35)
            pm.close_position("BTCUSDT", 103)                 # 全平
            self.assertEqual(cm.daily_trades, i + 1)
        self.assertFalse(cm.can_trade(10000)[0])              # 第 21 筆才擋
