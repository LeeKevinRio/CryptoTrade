"""策略稽核：判斷重算、成交配對、外來單、每筆進出、漏跑日"""

import asyncio
import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import strategy_audit as sa

DAY = 86_400_000
T0 = int(datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)   # 台灣 22:00


def _kl(closes, first_close_ms):
    """日 K：第 i 根於 first_close_ms + i 天收盤"""
    return [[0, c, c, c, str(c), 0, first_close_ms + i * DAY - 1] for i, c in enumerate(closes)]


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


# 5 日均線；T0 時已收盤到 index 5（第 6 根 = 12 > 均線）、T0+1 天收盤 index 6 = 5（跌破）
KL = _kl([10, 10, 10, 10, 10, 12, 5, 5], T0 - 6 * DAY + 10 * 3_600_000)


class TestDecisions(unittest.TestCase):
    def test_expected_uses_only_closed(self):
        hold, close, _ = sa.expected_at(KL, T0, 5)
        self.assertTrue(hold)
        self.assertEqual(close, 12.0)
        self.assertFalse(sa.expected_at(KL, T0 + DAY, 5)[0])

    def test_check_decision(self):
        ok = {"ts": _iso(T0), "symbol": "A", "action": "buy", "close": "12.0"}
        self.assertIsNone(sa.check_decision(ok, KL, 5))
        bad = {**ok, "action": "flat"}
        self.assertIn("應持有", sa.check_decision(bad, KL, 5))
        wrong_close = {**ok, "close": "11.0"}
        self.assertIn("不同", sa.check_decision(wrong_close, KL, 5))
        self.assertIsNone(sa.check_decision({**ok, "action": "skip"}, KL, 5))


class TestOrders(unittest.TestCase):
    def _fill(self, oid, sym, side, t, qty, price, pnl=0.0, fee=0.01):
        return {"orderId": oid, "symbol": sym, "side": side, "time": t, "qty": str(qty),
                "quoteQty": str(qty * price), "realizedPnl": str(pnl), "commission": str(fee)}

    def test_group_match_and_round_trip(self):
        fills = [self._fill(1, "A", "BUY", T0 + 1000, 1, 12), self._fill(1, "A", "BUY", T0 + 2000, 1, 12.2),
                 self._fill(2, "A", "SELL", T0 + DAY + 1000, 2, 5, pnl=-14.2),
                 self._fill(3, "SOL", "BUY", T0 + 5000, 3, 100)]                    # 舊機器人
        orders = sa.group_fills(fills)
        self.assertEqual(len(orders), 3)
        a_buy = next(o for o in orders if o["symbol"] == "A" and o["side"] == "BUY")
        self.assertAlmostEqual(a_buy["qty"], 2)
        self.assertAlmostEqual(a_buy["price"], 12.1)
        log = [{"ts": _iso(T0), "symbol": "A", "action": "buy", "qty": "2"},
               {"ts": _iso(T0 + DAY), "symbol": "A", "action": "sell", "qty": "2"},
               {"ts": _iso(T0 + DAY), "symbol": "B", "action": "buy", "qty": "1"}]        # 沒成交
        pairs, missing, foreign = sa.match_orders(orders, log)
        self.assertEqual(len(pairs), 2)
        self.assertEqual([r["symbol"] for r in missing], ["B"])
        self.assertEqual([o["symbol"] for o in foreign], ["SOL"])
        trips = sa.round_trips([o for _, o in pairs])
        self.assertEqual(len(trips), 1)
        self.assertAlmostEqual(trips[0]["pnl"], -14.2 - 0.02 - 0.01)
        self.assertAlmostEqual(trips[0]["days"], 1.0, places=2)

    def test_open_trip(self):
        trips = sa.round_trips([{"symbol": "A", "side": "BUY", "time": T0, "price": 1.0, "commission": 0}])
        self.assertIsNone(trips[0]["exit"])


class TestMissedAndLog(unittest.TestCase):
    def test_missed_days(self):
        rows = [{"ts": _iso(T0)}, {"ts": _iso(T0 + 2 * DAY)}]
        self.assertEqual(sa.missed_days(rows, T0 - 5 * DAY, T0 + 3 * DAY), ["10-21"])   # 機器人啟動前不算
        self.assertEqual(sa.missed_days([], T0 - DAY, T0 + 3 * DAY), [])

    def test_read_log_skips_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.csv"
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["ts", "symbol", "action", "dry_run"])
                w.writeheader()
                w.writerow({"ts": _iso(T0), "symbol": "A", "action": "buy", "dry_run": "True"})
                w.writerow({"ts": _iso(T0), "symbol": "A", "action": "buy", "dry_run": "False"})
                w.writerow({"ts": _iso(T0 - 30 * DAY), "symbol": "A", "action": "buy", "dry_run": "False"})
            self.assertEqual(len(sa.read_log(p, T0 - DAY)), 1)


class TestReport(unittest.TestCase):
    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.csv"
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["ts", "symbol", "action", "close", "sma", "qty", "price",
                                                  "dry_run", "note"])
                w.writeheader()
                w.writerow({"ts": _iso(T0), "symbol": "A", "action": "buy", "close": "12.0", "qty": "2",
                            "dry_run": "False"})
            api = mock.MagicMock()
            api.connect = mock.AsyncMock()
            api.disconnect = mock.AsyncMock()
            api.client.futures_income_history = mock.AsyncMock(return_value=[
                {"symbol": "SOLUSDT", "incomeType": "COMMISSION", "income": "-0.1", "time": T0}])
            api.get_klines = mock.AsyncMock(return_value=KL)
            fills = {"A": [{"orderId": 1, "symbol": "A", "side": "BUY", "time": T0 + 500, "qty": "2",
                            "quoteQty": "24", "realizedPnl": "0", "commission": "0.01"}],
                     "SOLUSDT": [{"orderId": 9, "symbol": "SOLUSDT", "side": "SELL", "time": T0, "qty": "3",
                                  "quoteQty": "300", "realizedPnl": "-20", "commission": "0.1"}]}
            api.get_account_trades = mock.AsyncMock(side_effect=lambda sym, since: fills[sym])
            cfg = {"binance": {"api_key": "k", "api_secret": "s", "testnet": False},
                   "daily_trend": {"sma_days": 5}}
            with mock.patch("src.data.binance_client.BinanceAPI", return_value=api), \
                 mock.patch("src.utils.config.load_config", return_value=cfg), \
                 mock.patch("time.time", return_value=(T0 + 2 * DAY) / 1000), \
                 mock.patch("builtins.print") as pr:
                asyncio.run(sa.amain(3, p))
        out = pr.call_args_list[0].args[0]
        self.assertIn("✓ 全部正確", out)
        self.assertIn("幣安對得上 1 次", out)
        self.assertIn("SOLUSDT        1 張，損益 -20.10U", out)
        self.assertIn("持有中", out)
        api.futures_market_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestAccountTradesClock(unittest.TestCase):
    """本機時鐘比幣安快時，查詢的 startTime 不能落在伺服器的未來（-4181）"""

    def test_windows_never_start_after_server_now(self):
        import time as _t
        from src.data.binance_client import BinanceAPI
        api = BinanceAPI("k", "s", testnet=True)
        api.client = mock.MagicMock()
        api.client.timestamp_offset = -3000                      # 本機比伺服器快 3 秒
        api.client.futures_account_trades = mock.AsyncMock(return_value=[])
        local_now = int(_t.time() * 1000)
        asyncio.run(api.get_account_trades("BTCUSDT", local_now - 7 * DAY))
        calls = api.client.futures_account_trades.await_args_list
        self.assertEqual(len(calls), 1)                          # 剛好 7 天 → 一段就好
        for c in calls:
            self.assertLessEqual(c.kwargs["startTime"], local_now - 3000)
            self.assertLessEqual(c.kwargs["endTime"], int(_t.time() * 1000) - 3000)

    def test_audit_survives_symbol_failure(self):
        with tempfile.TemporaryDirectory() as d:
            api = mock.MagicMock()
            api.connect = mock.AsyncMock()
            api.disconnect = mock.AsyncMock()
            api.client.futures_income_history = mock.AsyncMock(return_value=[
                {"symbol": "XUSDT", "incomeType": "COMMISSION", "income": "-0.1", "time": T0}])
            api.get_klines = mock.AsyncMock(return_value=KL)
            api.get_account_trades = mock.AsyncMock(side_effect=RuntimeError("APIError(code=-4181)"))
            cfg = {"binance": {"api_key": "k", "api_secret": "s", "testnet": False}, "daily_trend": {}}
            with mock.patch("src.data.binance_client.BinanceAPI", return_value=api), \
                 mock.patch("src.utils.config.load_config", return_value=cfg), \
                 mock.patch("builtins.print") as pr:
                asyncio.run(sa.amain(7, Path(d) / "none.csv"))
        self.assertIn("XUSDT 查詢失敗", pr.call_args_list[0].args[0])
