"""標的初始化容錯：槓桿退階與剔除行為"""

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.risk.position_manager import PositionManager
from src.execution.order_executor import OrderExecutor

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
    api.get_max_leverage = AsyncMock(return_value=20)
    pm = PositionManager(copy.deepcopy(CONFIG))
    return OrderExecutor(api=api, position_manager=pm, config=CONFIG, mode="futures"), api


class TestLeverageFallback(unittest.TestCase):
    def test_normal_leverage_kept(self):
        ex, api = make_executor()
        symbols = ["BTCUSDT"]
        asyncio.run(ex.init_symbol_info(symbols))
        self.assertEqual(symbols, ["BTCUSDT"])
        self.assertEqual(ex._symbol_info["BTCUSDT"]["leverage"], 25)

    def test_invalid_leverage_falls_back_to_max(self):
        ex, api = make_executor()
        # 第一次設 25x 失敗（-4028），退階後設 20x 成功
        api.set_leverage = AsyncMock(
            side_effect=[Exception("APIError(code=-4028): Leverage 25 is not valid"), None])
        symbols = ["XPLUSDT"]
        asyncio.run(ex.init_symbol_info(symbols))
        self.assertEqual(symbols, ["XPLUSDT"])                       # 保留標的
        self.assertEqual(ex._symbol_info["XPLUSDT"]["leverage"], 20)  # 退為上限
        api.set_leverage.assert_called_with("XPLUSDT", 20)

    def test_persistent_failure_drops_symbol_not_engine(self):
        ex, api = make_executor()
        api.set_leverage = AsyncMock(side_effect=Exception("other error"))
        symbols = ["BADUSDT", "BTCUSDT"]
        # 第二個標的正常：換一個乾淨 mock 序列
        def side_effect(symbol, lev):
            if symbol == "BADUSDT":
                raise Exception("other error")
        api.set_leverage = AsyncMock(side_effect=side_effect)
        asyncio.run(ex.init_symbol_info(symbols))
        self.assertEqual(symbols, ["BTCUSDT"])            # 壞標的就地剔除
        self.assertNotIn("BADUSDT", ex._symbol_info)
        self.assertIn("BTCUSDT", ex._symbol_info)         # 好標的不受影響

    def test_missing_info_drops_symbol(self):
        ex, api = make_executor()
        api.get_symbol_info = AsyncMock(return_value={})
        symbols = ["GHOSTUSDT"]
        asyncio.run(ex.init_symbol_info(symbols))
        self.assertEqual(symbols, [])


if __name__ == "__main__":
    unittest.main()
