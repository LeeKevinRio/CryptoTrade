"""風控迴圈取價 + 接管倉保護單 + 診斷強度

背景：1m 時框從訂閱清單移除後，get_latest_price 預設抓 1m → 永遠 None →
risk_loop 每秒 continue，整整一個月沒檢查過任何停損／停利（DOGE 空單 -9% 未停損、
四檔多單 +6~15% 未停利）。
"""

import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.data.candle_manager import CandleManager
from src.risk.position_manager import PositionManager
from src.execution.order_executor import OrderExecutor
from src.strategy.base_strategy import Signal, SignalType
from src.strategy.signal_aggregator import SignalAggregator
from tests.test_partial_close import BASE_CONFIG, maker_config


def _klines(closes, start_ms=1_700_000_000_000, step_ms=300_000):
    return [[start_ms + i * step_ms, c, c, c, c, 1.0] for i, c in enumerate(closes)]


class TestLatestPrice(unittest.TestCase):
    def test_falls_back_to_shortest_subscribed_interval(self):
        cm = CandleManager()
        cm.init_from_klines("BTCUSDT", "1h", _klines([100, 101]))
        cm.init_from_klines("BTCUSDT", "5m", _klines([100, 105]))
        cm._last_prices.clear()                          # 模擬尚無 tick
        self.assertEqual(cm.get_latest_price("BTCUSDT"), 105)   # 取 5m 而非 1h

    def test_no_1m_does_not_return_none(self):
        cm = CandleManager()
        cm.init_from_klines("BTCUSDT", "5m", _klines([100, 102]))
        self.assertIsNotNone(cm.get_latest_price("BTCUSDT"))
        self.assertIsNotNone(cm.get_latest_price("BTCUSDT", "1m"))  # 舊呼叫方式也要活

    def test_tick_price_preferred_even_if_candle_not_closed(self):
        cm = CandleManager()
        cm.init_from_klines("BTCUSDT", "5m", _klines([100, 102]))
        cm.update_candle("BTCUSDT", "4h", {
            "timestamp": 1_700_000_000_000, "open": 100, "high": 110,
            "low": 99, "close": 109.5, "volume": 1.0,
        })
        self.assertEqual(cm.get_latest_price("BTCUSDT"), 109.5)

    def test_unknown_symbol_is_none(self):
        self.assertIsNone(CandleManager().get_latest_price("NOPE"))


class TestEnsureProtection(unittest.TestCase):
    def _executor(self, cfg, open_orders):
        api = MagicMock()
        api.get_open_orders = AsyncMock(return_value=open_orders)
        api.futures_stop_market = AsyncMock(return_value={"orderId": 9})
        api.futures_limit_order = AsyncMock(return_value={"orderId": 1})
        pm = PositionManager(cfg)
        ex = OrderExecutor(api=api, position_manager=pm, config=cfg, mode="futures")
        ex._symbol_info["BTCUSDT"] = {"qty_precision": 3, "price_precision": 2, "min_qty": 0.001}
        pm.open_position("BTCUSDT", "LONG", 50000, 1.0, leverage=5)
        return ex, api

    def test_places_stop_and_ladder_when_missing(self):
        ex, api = self._executor(maker_config(), open_orders=[])
        out = asyncio.run(ex.ensure_protection("BTCUSDT", "LONG", 50000, 1.0))
        self.assertEqual(out, {"stop": "placed", "tp": "placed"})
        kw = api.futures_stop_market.call_args.kwargs
        self.assertEqual(kw["side"], "SELL")
        self.assertTrue(kw["close_position"])
        self.assertEqual(kw["stop_price"], 48000.0)          # 4% 停損
        self.assertEqual(api.futures_limit_order.call_count, 3)

    def test_keeps_existing_orders(self):
        existing = [
            {"type": "STOP_MARKET", "side": "SELL", "closePosition": "true"},
            {"type": "LIMIT", "side": "SELL", "reduceOnly": "true"},
        ]
        ex, api = self._executor(maker_config(), open_orders=existing)
        out = asyncio.run(ex.ensure_protection("BTCUSDT", "LONG", 50000, 1.0))
        self.assertEqual(out, {"stop": "exists", "tp": "exists"})
        api.futures_stop_market.assert_not_called()
        api.futures_limit_order.assert_not_called()

    def test_stop_rejected_is_reported_not_raised(self):
        # 價格已越過停損價 → 交易所拒單（-2021），軟體風控接手
        ex, api = self._executor(copy.deepcopy(BASE_CONFIG), open_orders=[])
        api.futures_stop_market = AsyncMock(side_effect=RuntimeError("-2021 would trigger"))
        out = asyncio.run(ex.ensure_protection("BTCUSDT", "LONG", 50000, 1.0))
        self.assertEqual(out["stop"], "failed")
        self.assertEqual(out["tp"], "skipped")               # 非 maker 模式不掛階梯

    def test_view_mode_never_places_orders(self):
        ex, api = self._executor(maker_config(), open_orders=[])
        ex.trading_disabled = True
        out = asyncio.run(ex.ensure_protection("BTCUSDT", "LONG", 50000, 1.0))
        self.assertEqual(out, {"stop": "skipped", "tp": "skipped"})
        api.get_open_orders.assert_not_called()


class TestAdoptedPositionGetsManaged(unittest.TestCase):
    """接管的倉位一有價格就必須被軟體風控處理（此前因取不到價永遠不會）"""

    def test_short_beyond_stop_is_closed_by_software_check(self):
        pm = PositionManager(copy.deepcopy(BASE_CONFIG))
        pm.open_position("DOGEUSDT", "SHORT", 0.0874, 16609.0, leverage=5)
        cm = CandleManager()
        cm.init_from_klines("DOGEUSDT", "5m", _klines([0.095, 0.0955]))
        price = cm.get_latest_price("DOGEUSDT")
        actions = pm.check_risk("DOGEUSDT", price)
        self.assertEqual(actions[0]["action"], "stop_loss")


class TestNeutralSignalKeepsStrength(unittest.TestCase):
    def test_neutral_reports_sub_strategy_strengths(self):
        agg = SignalAggregator(copy.deepcopy(BASE_CONFIG))
        agg.dip_buyer.analyze = MagicMock(return_value=Signal(
            type=SignalType.LONG, symbol="BTCUSDT", strength=35, price=100))
        agg.short_seller.analyze = MagicMock(return_value=Signal(
            type=SignalType.NEUTRAL, symbol="BTCUSDT", strength=20))
        agg._trend_bias = MagicMock(return_value=(0, 0.0))
        sig = agg.evaluate("BTCUSDT", {})
        self.assertEqual(sig.type, SignalType.NEUTRAL)
        self.assertFalse(sig.is_actionable)
        self.assertEqual(sig.strength, 35)
        self.assertEqual((sig.long_strength, sig.short_strength), (35, 20))
        self.assertIn("門檻 60", sig.reasons[0])


if __name__ == "__main__":
    unittest.main()
