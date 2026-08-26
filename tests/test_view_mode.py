"""觀察模式（TRADING_DISABLED）— 保證不送出任何訂單"""

import asyncio
import copy
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.execution.order_executor import OrderExecutor
from src.risk.position_manager import PositionManager
from src.strategy.base_strategy import Signal, SignalType

CONFIG = {
    "leverage": 25,
    "margin_type": "CROSSED",
    "risk": {
        "take_profit": {
            "level_1_pct": 1.5, "level_1_close_pct": 40,
            "level_2_pct": 2.5, "level_2_close_pct": 35,
            "level_3_pct": 3.0, "level_3_close_pct": 25,
        },
        "trailing_stop": {"activation_pct": 1.0, "callback_pct": 0.4},
        "stop_loss_pct": 4.0,
        "max_position_pct": 10,
        "max_concurrent_positions": 5,
        "max_daily_loss_pct": 25.0,
        "max_daily_trades": 150,
        "use_maker_tp": True,
    },
}

SYMBOL_INFO = {
    "filters": [
        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
    ],
}


def make_executor():
    api = MagicMock()
    api.get_symbol_info = AsyncMock(return_value=SYMBOL_INFO)
    api.set_margin_type = AsyncMock()
    api.set_leverage = AsyncMock()
    api.futures_market_order = AsyncMock()
    api.futures_limit_order = AsyncMock()
    api.futures_stop_market = AsyncMock()
    api.cancel_all_orders = AsyncMock()
    pm = PositionManager(copy.deepcopy(CONFIG))
    ex = OrderExecutor(api=api, position_manager=pm, config=CONFIG, mode="futures")
    ex._symbol_info["BTCUSDT"] = {
        "qty_precision": 3, "price_precision": 2, "min_qty": 0.001, "leverage": 25}
    return ex, api, pm


class TestViewMode(unittest.TestCase):
    def setUp(self):
        os.environ["TRADING_DISABLED"] = "true"

    def tearDown(self):
        os.environ.pop("TRADING_DISABLED", None)

    def test_execute_signal_refused(self):
        ex, api, _ = make_executor()
        sig = Signal(type=SignalType.LONG, symbol="BTCUSDT", strength=99,
                     price=50000, min_strength=50)
        result = asyncio.run(ex.execute_signal(sig, balance=10000))
        self.assertIsNone(result)
        api.futures_market_order.assert_not_called()
        api.futures_limit_order.assert_not_called()

    def test_close_position_refused(self):
        ex, api, pm = make_executor()
        pm.open_position("BTCUSDT", "LONG", 50000, 1.0, leverage=25)
        result = asyncio.run(ex.close_position("BTCUSDT", 1.0, "測試"))
        self.assertIsNone(result)
        api.futures_market_order.assert_not_called()
        api.cancel_all_orders.assert_not_called()

    def test_init_does_not_touch_account_settings(self):
        ex, api, _ = make_executor()
        symbols = ["BTCUSDT"]
        asyncio.run(ex.init_symbol_info(symbols))
        api.set_margin_type.assert_not_called()
        api.set_leverage.assert_not_called()
        # 標的仍保留、精度資訊可用（顯示用）
        self.assertEqual(symbols, ["BTCUSDT"])
        self.assertIn("BTCUSDT", ex._symbol_info)

    def test_normal_mode_unaffected(self):
        os.environ.pop("TRADING_DISABLED", None)
        ex, api, _ = make_executor()
        self.assertFalse(ex.trading_disabled)


if __name__ == "__main__":
    unittest.main()
