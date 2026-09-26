"""交易所成交重組 + 去重測試"""

import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from src.execution.exchange_import import group_fills_into_trades, import_trades, IMPORT_REASON
from src.utils.models import TradeRecord, init_db


def fill(i, side, qty, price, pnl=0.0, t=None, fee=0.01, symbol="BTCUSDT"):
    return {"id": i, "orderId": i, "symbol": symbol, "side": side, "qty": str(qty),
            "price": str(price), "realizedPnl": str(pnl), "commission": str(fee),
            "time": t if t is not None else 1_700_000_000_000 + i * 60_000}


class TestGrouping(unittest.TestCase):
    def test_simple_long_round_trip(self):
        fills = [fill(1, "BUY", 1.0, 100), fill(2, "SELL", 1.0, 110, pnl=10)]
        trades = group_fills_into_trades(fills)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["side"], "LONG")
        self.assertAlmostEqual(t["entry_price"], 100)
        self.assertAlmostEqual(t["exit_price"], 110)
        self.assertAlmostEqual(t["pnl"], 10)
        self.assertAlmostEqual(t["pnl_pct"], 10.0)
        self.assertEqual(t["exchange_ref"], "BTCUSDT:2")

    def test_batched_entry_and_ladder_exit(self):
        # 分批進場 50/30/20，階梯出場 40/35/25 —— 與 bot 實際行為一致
        fills = [
            fill(1, "BUY", 0.5, 100), fill(2, "BUY", 0.3, 99), fill(3, "BUY", 0.2, 98),
            fill(4, "SELL", 0.4, 105, pnl=2.4), fill(5, "SELL", 0.35, 106, pnl=2.5),
            fill(6, "SELL", 0.25, 107, pnl=2.0),
        ]
        trades = group_fills_into_trades(fills)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertAlmostEqual(t["quantity"], 1.0)
        self.assertAlmostEqual(t["entry_price"], (50 + 29.7 + 19.6) / 1.0, places=6)
        self.assertAlmostEqual(t["pnl"], 6.9)
        self.assertEqual(t["exchange_ref"], "BTCUSDT:6")

    def test_short_round_trip(self):
        fills = [fill(1, "SELL", 2.0, 100), fill(2, "BUY", 2.0, 95, pnl=10)]
        t = group_fills_into_trades(fills)[0]
        self.assertEqual(t["side"], "SHORT")
        self.assertAlmostEqual(t["pnl"], 10)

    def test_flip_splits_into_two_trades(self):
        # 多 1.0 → 賣 1.5：平多 1.0 + 開空 0.5 → 再買 0.5 平空
        fills = [fill(1, "BUY", 1.0, 100), fill(2, "SELL", 1.5, 110, pnl=10),
                 fill(3, "BUY", 0.5, 105, pnl=2.5)]
        trades = group_fills_into_trades(fills)
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["side"], "LONG")
        self.assertAlmostEqual(trades[0]["pnl"], 10)
        self.assertEqual(trades[1]["side"], "SHORT")
        self.assertAlmostEqual(trades[1]["quantity"], 0.5)

    def test_open_position_not_emitted(self):
        fills = [fill(1, "BUY", 1.0, 100), fill(2, "SELL", 0.4, 105, pnl=2)]
        self.assertEqual(group_fills_into_trades(fills), [])


class TestImportDedupe(unittest.TestCase):
    def setUp(self):
        self.sf = init_db("sqlite:///:memory:")

    def _api(self, fills):
        api = MagicMock()
        api.get_account_trades = AsyncMock(return_value=fills)
        return api

    def test_import_then_rerun_is_idempotent(self):
        fills = [fill(1, "BUY", 1.0, 100), fill(2, "SELL", 1.0, 110, pnl=10)]
        api = self._api(fills)
        s1 = asyncio.run(import_trades(api, self.sf, ["BTCUSDT"], 30))
        s2 = asyncio.run(import_trades(api, self.sf, ["BTCUSDT"], 30))
        self.assertEqual(s1["inserted"], 1)
        self.assertEqual(s2["inserted"], 0)
        self.assertEqual(s2["skipped"], 1)
        with self.sf() as s:
            rows = s.query(TradeRecord).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].close_reason, IMPORT_REASON)
            self.assertEqual(rows[0].status, "CLOSED")

    def test_bot_recorded_trade_not_duplicated(self):
        fills = [fill(1, "BUY", 1.0, 100), fill(2, "SELL", 1.0, 110, pnl=10)]
        # bot 自己已記過同一筆（平倉時間相差 30 秒）
        grouped = group_fills_into_trades(fills)[0]
        with self.sf() as s:
            s.add(TradeRecord(
                bot_id="futures", mode="futures", symbol="BTCUSDT", side="LONG",
                entry_price=100, exit_price=110, quantity=1.0, pnl=10, pnl_pct=10,
                entry_time=grouped["entry_time"],
                exit_time=grouped["exit_time"] + timedelta(seconds=30),
                status="CLOSED", close_reason="階梯停利",
            ))
            s.commit()
        stats = asyncio.run(import_trades(self._api(fills), self.sf, ["BTCUSDT"], 30))
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual(stats["skipped"], 1)

    def test_symbol_failure_does_not_abort_others(self):
        api = MagicMock()
        async def side_effect(symbol, start_ms):
            if symbol == "BADUSDT":
                raise RuntimeError("boom")
            return [fill(1, "BUY", 1.0, 100, symbol=symbol),
                    fill(2, "SELL", 1.0, 110, pnl=10, symbol=symbol)]
        api.get_account_trades = AsyncMock(side_effect=side_effect)
        stats = asyncio.run(import_trades(api, self.sf, ["BADUSDT", "ETHUSDT"], 30))
        self.assertEqual(stats["inserted"], 1)
        self.assertEqual(len(stats["errors"]), 1)


if __name__ == "__main__":
    unittest.main()


class TestOrphanExitFills(unittest.TestCase):
    """窗口之前開的倉、窗口內只有平倉成交 → 仍要匯成一筆（進場價由 realizedPnl 反推）"""

    def _fill(self, i, side, qty, price, pnl, t, fee="0.01"):
        return {"id": i, "symbol": "ETHUSDT", "side": side, "qty": str(qty), "price": str(price),
                "realizedPnl": str(pnl), "commission": fee, "time": t}

    def test_single_orphan_exit(self):
        # 多單 0.476 @2421 在窗口外開；窗口內 SELL 0.476 @2674，realizedPnl = 253×0.476
        fills = [self._fill(1, "SELL", 0.476, 2674.0, 253 * 0.476, 1_700_000_000_000)]
        trades = group_fills_into_trades(fills)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["side"], "LONG")
        self.assertAlmostEqual(t["entry_price"], 2421.0, places=4)
        self.assertAlmostEqual(t["pnl"], 253 * 0.476, places=4)
        self.assertTrue(t["entry_unknown"])

    def test_ladder_orphan_exits_merge_into_one(self):
        # 階梯 40/35/25 三段平倉 → 合併成一筆
        fills = [
            self._fill(1, "SELL", 0.4, 110.0, 4.0, 1_700_000_000_000),
            self._fill(2, "SELL", 0.35, 120.0, 7.0, 1_700_000_060_000),
            self._fill(3, "SELL", 0.25, 130.0, 7.5, 1_700_000_120_000),
        ]
        trades = group_fills_into_trades(fills)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["quantity"], 1.0)
        self.assertAlmostEqual(trades[0]["pnl"], 18.5)
        self.assertEqual(trades[0]["exchange_ref"], "ETHUSDT:3")

    def test_orphan_then_real_round_trip(self):
        fills = [
            self._fill(1, "SELL", 1.0, 110.0, 10.0, 1_700_000_000_000),      # 孤兒平倉
            self._fill(2, "BUY", 1.0, 100.0, 0, 1_700_000_060_000),         # 真開倉
            self._fill(3, "SELL", 1.0, 105.0, 5.0, 1_700_000_120_000),      # 真平倉
        ]
        trades = group_fills_into_trades(fills)
        self.assertEqual(len(trades), 2)
        self.assertTrue(trades[0].get("entry_unknown"))
        self.assertFalse(trades[1].get("entry_unknown", False))
        self.assertAlmostEqual(trades[1]["entry_price"], 100.0)

    def test_orphan_short_exit(self):
        # 空單在窗口外開；BUY 平倉，pnl 為正 → entry = price + pnl/qty
        fills = [self._fill(1, "BUY", 2.0, 90.0, 20.0, 1_700_000_000_000)]
        t = group_fills_into_trades(fills)[0]
        self.assertEqual(t["side"], "SHORT")
        self.assertAlmostEqual(t["entry_price"], 100.0)

    def test_orphan_persisted_with_exit_time_as_entry(self):
        from unittest.mock import AsyncMock, MagicMock
        import asyncio
        from src.utils.models import TradeRecord, init_db
        sf = init_db("sqlite:///:memory:")
        api = MagicMock(get_account_trades=AsyncMock(return_value=[
            self._fill(1, "SELL", 1.0, 110.0, 10.0, 1_700_000_000_000)]))
        asyncio.run(import_trades(api, sf, ["ETHUSDT"], 30))
        with sf() as s:
            r = s.query(TradeRecord).one()
            self.assertIsNotNone(r.entry_time)
            self.assertEqual(r.entry_time, r.exit_time)
            self.assertIn("早於查詢窗口", r.close_reason)
