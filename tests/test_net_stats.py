"""扣費淨額統計 — 真金決策唯一該看的數字"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.execution.exchange_import import import_trades
from src.utils.models import TradeRecord, init_db
from src.utils.performance import compute_stats, trade_commission, EST_FEE_RATE


def row(pnl, entry=100.0, qty=1.0, commission=None):
    return {"pnl": pnl, "entry_price": entry, "quantity": qty, "commission": commission,
            "symbol": "BTCUSDT", "side": "LONG", "close_reason": "x",
            "entry_time": "2026-09-01T00:00:00", "exit_time": "2026-09-01T01:00:00"}


class TestTradeCommission(unittest.TestCase):
    def test_actual_commission_preferred(self):
        self.assertEqual(trade_commission(row(5, commission=0.42)), 0.42)

    def test_estimate_when_missing(self):
        # 名目 100 × 1 = 100 → 0.08% = 0.08
        self.assertAlmostEqual(trade_commission(row(5)), 100 * EST_FEE_RATE)


class TestNetStats(unittest.TestCase):
    def test_gross_positive_but_net_negative(self):
        # 三筆各賺 +0.05，名目 100 → 每筆估費 0.08 → 淨額為負
        rows = [row(0.05) for _ in range(3)]
        s = compute_stats(rows)
        self.assertAlmostEqual(s["total_pnl"], 0.15)
        self.assertAlmostEqual(s["total_commission"], 0.24)
        self.assertAlmostEqual(s["net_pnl"], -0.09)
        self.assertLess(s["net_expectancy"], 0)

    def test_net_uses_real_commission_when_present(self):
        rows = [row(10, commission=1.5), row(-4, commission=1.0)]
        s = compute_stats(rows)
        self.assertAlmostEqual(s["total_commission"], 2.5)
        self.assertAlmostEqual(s["net_pnl"], 3.5)
        # 淨 PF = 8.5 / 5.0
        self.assertAlmostEqual(s["net_profit_factor"], 8.5 / 5.0)

    def test_empty(self):
        s = compute_stats([])
        self.assertEqual(s["net_pnl"], 0)
        self.assertEqual(s["net_expectancy"], 0.0)


class TestImportStoresCommission(unittest.TestCase):
    def test_commission_persisted(self):
        sf = init_db("sqlite:///:memory:")
        fills = [
            {"id": 1, "symbol": "BTCUSDT", "side": "BUY", "qty": "1", "price": "100",
             "realizedPnl": "0", "commission": "0.04", "time": 1_700_000_000_000},
            {"id": 2, "symbol": "BTCUSDT", "side": "SELL", "qty": "1", "price": "110",
             "realizedPnl": "10", "commission": "0.044", "time": 1_700_000_060_000},
        ]
        api = MagicMock(get_account_trades=AsyncMock(return_value=fills))
        asyncio.run(import_trades(api, sf, ["BTCUSDT"], 30))
        with sf() as s:
            r = s.query(TradeRecord).one()
            self.assertAlmostEqual(r.commission, 0.084)


if __name__ == "__main__":
    unittest.main()
