"""正式站停損條件單改走 Algo Order API；掛單結果寫入 state.protection；階梯殘倉"""

import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from binance.exceptions import BinanceAPIException

from src.data.binance_client import BinanceAPI
from src.execution.order_executor import OrderExecutor
from src.risk.position_manager import PositionManager
from src.web.state import state
from tests.test_partial_close import BASE_CONFIG, maker_config


def api_err(code, msg):
    return BinanceAPIException(MagicMock(), 400, json.dumps({"code": code, "msg": msg}))


class TestStopFallback(unittest.TestCase):
    def _api(self, legacy_exc=None):
        api = BinanceAPI("k", "s", testnet=False)
        api.client = MagicMock()
        api.client.futures_create_order = AsyncMock(
            side_effect=legacy_exc, return_value={"orderId": 1})
        api.client._request_futures_api = AsyncMock(return_value={"algoId": 99})
        return api

    def test_algo_used_when_legacy_rejects_conditional(self):
        api = self._api(api_err(-4120, "Order type not supported for this endpoint. Please use the Algo Order API endpoints instead."))
        out = asyncio.run(api.futures_stop_market("SUIUSDT", "SELL", 1.1473, close_position=True))
        self.assertEqual(out["_route"], "algo")
        args, kwargs = api.client._request_futures_api.call_args
        self.assertEqual(args[:3], ("post", "algoOrder", True))
        d = kwargs["data"]
        self.assertEqual((d["algoType"], d["type"], d["triggerPrice"], d["closePosition"]),
                         ("CONDITIONAL", "STOP_MARKET", "1.1473", "true"))

    def test_legacy_ok_no_algo(self):
        api = self._api()
        out = asyncio.run(api.futures_stop_market("BTCUSDT", "SELL", 80000, close_position=True))
        self.assertEqual(out["_route"], "legacy")
        api.client._request_futures_api.assert_not_called()

    def test_other_errors_still_raise(self):
        api = self._api(api_err(-2021, "Order would immediately trigger."))
        with self.assertRaises(BinanceAPIException):
            asyncio.run(api.futures_stop_market("BTCUSDT", "SELL", 80000, close_position=True))
        api.client._request_futures_api.assert_not_called()


class TestProtectionStatus(unittest.TestCase):
    def _ex(self, cfg=None):
        cfg = cfg or copy.deepcopy(BASE_CONFIG)
        api = MagicMock()
        api.get_open_orders = AsyncMock(return_value=[])
        api.get_open_algo_orders = AsyncMock(return_value=[])
        api.futures_stop_market = AsyncMock(return_value={"_route": "algo"})
        api.futures_limit_order = AsyncMock(return_value={})
        pm = PositionManager(cfg)
        ex = OrderExecutor(api=api, position_manager=pm, config=cfg, mode="futures")
        ex._symbol_info["SUIUSDT"] = {"qty_precision": 1, "price_precision": 4, "min_qty": 0.1}
        ex._symbol_info["DOGEUSDT"] = {"qty_precision": 0, "price_precision": 5, "min_qty": 1}
        pm.open_position("SUIUSDT", "LONG", 1.2077, 125.2, leverage=5)
        return ex, api

    def setUp(self):
        state.protection.clear()

    def test_placed_recorded_with_route(self):
        ex, api = self._ex()
        out = asyncio.run(ex.ensure_protection("SUIUSDT", "LONG", 1.2077, 125.2))
        self.assertEqual(out["stop"], "placed")
        self.assertEqual(state.protection["SUIUSDT"]["stop"], "placed")
        self.assertEqual(state.protection["SUIUSDT"]["route"], "algo")

    def test_failure_recorded(self):
        ex, api = self._ex()
        api.futures_stop_market = AsyncMock(side_effect=RuntimeError("-4120 boom"))
        out = asyncio.run(ex.ensure_protection("SUIUSDT", "LONG", 1.2077, 125.2))
        self.assertEqual(out["stop"], "failed")
        self.assertEqual(state.protection["SUIUSDT"]["stop"], "FAILED")
        self.assertIn("4120", state.protection["SUIUSDT"]["error"])

    def test_existing_algo_stop_detected(self):
        ex, api = self._ex()
        api.get_open_algo_orders = AsyncMock(return_value=[{"orderType": "STOP_MARKET", "symbol": "SUIUSDT"}])
        out = asyncio.run(ex.ensure_protection("SUIUSDT", "LONG", 1.2077, 125.2))
        self.assertEqual(out["stop"], "exists")
        api.futures_stop_market.assert_not_called()


class TestLadderNoDust(unittest.TestCase):
    def test_last_level_takes_remainder(self):
        cfg = maker_config()
        api = MagicMock()
        api.futures_limit_order = AsyncMock(return_value={})
        ex = OrderExecutor(api=api, position_manager=PositionManager(cfg), config=cfg, mode="futures")
        ex._symbol_info["DOGEUSDT"] = {"qty_precision": 0, "price_precision": 5, "min_qty": 1}
        asyncio.run(ex._place_tp_ladder("DOGEUSDT", "SHORT", 0.0886, 2926))
        qtys = [c.kwargs["quantity"] for c in api.futures_limit_order.call_args_list]
        self.assertEqual(sum(qtys), 2926)          # 舊邏輯 1170+1024+731 = 2925，殘 1 顆
        self.assertEqual(qtys[-1], 2926 - 1170 - 1024)


if __name__ == "__main__":
    unittest.main()


class TestLogFile(unittest.TestCase):
    def test_module_loggers_write_to_shared_file(self):
        import tempfile, os
        from src.utils import logger as L
        old = L._file_handler
        L._file_handler = None
        try:
            early = L.setup_logger("early_module_x")          # 先建立（模組 import 時）
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "bot.log")
                L.setup_logger("cryptotrade", log_file=path)  # main() 才設定檔案
                late = L.setup_logger("late_module_x")
                early.warning("early-msg")
                late.error("late-msg")
                L._file_handler.flush()
                text = open(path, encoding="utf-8").read()
                self.assertIn("early-msg", text)
                self.assertIn("late-msg", text)
                for lg in L._loggers:
                    if L._file_handler in lg.handlers:
                        lg.removeHandler(L._file_handler)
                L._file_handler.close()
        finally:
            L._file_handler = old
