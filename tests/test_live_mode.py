"""真金模式：覆寫只在 live 生效；預檢硬性／軟性項目"""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.execution.live_preflight import run_preflight

BOT = {"leverage": 5, "mode": "futures", "risk": {"max_position_pct": 5}}
INFO = {"filters": [{"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}]}


def api(balance=500.0, dual=False, price=84000.0):
    a = MagicMock()
    a.get_usdt_balance = AsyncMock(return_value=balance)
    a.client.futures_get_position_mode = AsyncMock(return_value={"dualSidePosition": dual})
    a.get_symbol_info = AsyncMock(return_value=INFO)
    a.get_ticker_price = AsyncMock(return_value=price)
    a.get_max_leverage = AsyncMock(return_value=125)
    return a


class TestPreflight(unittest.TestCase):
    def test_all_good(self):
        # 500 U × 5% × 5x = 125 U 全倉 → 0.001 BTC 可下；半倉 62 U 不足 → 只警告
        r = asyncio.run(run_preflight(api(), ["BTCUSDT"], BOT, live_ack=True))
        self.assertTrue(r["ok"])
        self.assertTrue(all(c["ok"] for c in r["checks"] if c["hard"]))
        self.assertTrue(next(c for c in r["checks"] if c["name"] == "symbol:BTCUSDT")["ok"])
        self.assertIn("half:BTCUSDT", [c["name"] for c in r["checks"]])

    def test_large_account_no_half_warning(self):
        r = asyncio.run(run_preflight(api(balance=2000.0), ["BTCUSDT"], BOT, live_ack=True))
        self.assertTrue(r["ok"])
        self.assertNotIn("half:BTCUSDT", [c["name"] for c in r["checks"]])

    def test_missing_ack_blocks(self):
        r = asyncio.run(run_preflight(api(), ["BTCUSDT"], BOT, live_ack=False))
        self.assertFalse(r["ok"])
        self.assertFalse(next(c for c in r["checks"] if c["name"] == "ack")["ok"])

    def test_hedge_mode_auto_switched(self):
        a = api(dual=True)
        a.client.futures_change_position_mode = AsyncMock()
        a.client.futures_get_position_mode = AsyncMock(side_effect=[
            {"dualSidePosition": True}, {"dualSidePosition": False}])
        r = asyncio.run(run_preflight(a, ["BTCUSDT"], BOT, live_ack=True))
        self.assertTrue(r["ok"])
        a.client.futures_change_position_mode.assert_called_once_with(dualSidePosition="false")
        self.assertIn("已自動切成單向", next(c for c in r["checks"] if c["name"] == "position_mode")["detail"])

    def test_hedge_mode_blocks_when_switch_fails(self):
        a = api(dual=True)
        a.client.futures_change_position_mode = AsyncMock(side_effect=Exception("-4068 open positions"))
        r = asyncio.run(run_preflight(a, ["BTCUSDT"], BOT, live_ack=True))
        self.assertFalse(r["ok"])

    def test_low_balance_blocks(self):
        r = asyncio.run(run_preflight(api(balance=50), ["BTCUSDT"], BOT, live_ack=True))
        self.assertFalse(r["ok"])

    def test_small_account_skips_btc_but_not_blocked_if_other_usable(self):
        # 300 U × 5% × 5x = 75 U 全倉 → BTC 0.000 (< 0.001) 跳過；ETH 可用
        a = api(balance=300.0)
        a.get_ticker_price = AsyncMock(side_effect=lambda s: 84000.0 if s == "BTCUSDT" else 2700.0)
        r = asyncio.run(run_preflight(a, ["BTCUSDT", "ETHUSDT"], BOT, live_ack=True))
        self.assertTrue(r["ok"])
        btc = next(c for c in r["checks"] if c["name"] == "symbol:BTCUSDT")
        eth = next(c for c in r["checks"] if c["name"] == "symbol:ETHUSDT")
        self.assertFalse(btc["ok"]); self.assertFalse(btc["hard"])
        self.assertTrue(eth["ok"])

    def test_no_usable_symbol_blocks(self):
        r = asyncio.run(run_preflight(api(balance=120.0), ["BTCUSDT"], BOT, live_ack=True))
        self.assertFalse(r["ok"])


class TestLiveOverrides(unittest.TestCase):
    def _load(self, testnet: str, ack: str = ""):
        from src.utils.config import load_config
        env = {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s", "BINANCE_TESTNET": testnet,
               "LIVE_TRADING_ACK": ack}
        with patch.dict(os.environ, env, clear=False):
            return load_config()

    def test_testnet_untouched(self):
        c = self._load("true")
        self.assertTrue(c["binance"]["testnet"])
        self.assertEqual(c["bots"]["futures"]["risk"]["max_daily_loss_pct"], 5.0)
        self.assertEqual(c["bots"]["futures"]["risk"]["max_concurrent_positions"], 3)

    def test_live_applies_overrides_and_ack(self):
        c = self._load("false", "I_UNDERSTAND")
        self.assertFalse(c["binance"]["testnet"])
        self.assertTrue(c["binance"]["live_ack"])
        self.assertEqual(c["bots"]["futures"]["risk"]["max_daily_loss_pct"], 3.0)
        self.assertEqual(c["bots"]["futures"]["risk"]["max_concurrent_positions"], 2)
        # 其餘參數不受影響
        self.assertEqual(c["bots"]["futures"]["risk"]["stop_loss_pct"], 5.0)

    def test_live_without_ack(self):
        c = self._load("false")
        self.assertFalse(c["binance"]["live_ack"])
