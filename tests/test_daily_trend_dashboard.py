"""日線均線機器人沿用原本儀表板：各端點有資料、暫停不買、買賣寫進資料庫"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from src import daily_trend_bot as bot
from src import daily_trend_dashboard as dash
from src.execution.order_tracker import OrderTracker
from src.utils.models import TradeRecord, init_db
from src.web.app import create_app

CFG = {**bot.DEFAULTS, "sma_days": 5, "capital_usdt": 200.0}
NOW = 1_800_000_000_000
DAY = 86_400_000


def _klines(closes):
    """最後一根是今天尚未收盤的日 K（與幣安實際回傳一致）"""
    n = len(closes)
    rows = [[NOW - (n - i) * DAY, c, c * 1.01, c * 0.99, str(c), 10.0, NOW - (n - i - 1) * DAY - 1]
            for i, c in enumerate(closes)]
    rows[-1][6] = NOW + DAY
    return rows


def _db():
    import tempfile
    return OrderTracker(init_db(f"sqlite:///{tempfile.mkdtemp()}/t.db"))


def _api(positions=(), closes=(10, 10, 10, 10, 12, 1), chart=False):
    api = MagicMock()
    api.get_open_positions = AsyncMock(return_value=list(positions))
    api.get_usdt_balance = AsyncMock(return_value=363.24)
    api.get_ticker_price = AsyncMock(return_value=100.0)
    api.get_klines = AsyncMock(return_value=_klines(list(closes) * (8 if chart else 1)))
    api.get_symbol_info = AsyncMock(return_value={"filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"}]})
    api.set_margin_type = AsyncMock()
    api.set_leverage = AsyncMock()
    api.futures_market_order = AsyncMock()
    return api


class TestOldDashboard(unittest.TestCase):
    def setUp(self):
        dash.state.bots.clear()
        self.tracker = _db()

    def tearDown(self):
        # 全域 state 會被其他測試（例如 /api/diag）讀到，用完要還原
        st = dash.state
        st.bots.clear()
        st.api_ref = st.candle_manager_ref = None
        st.symbols, st.timeframes = [], []
        st.engine_status, st.exchange_positions, st.last_prices = {}, {}, {}

    def _setup(self, positions=()):
        api = _api(positions, chart=True)
        cfg = {**CFG, "symbols": ["BTCUSDT", "ETHUSDT"]}
        ref = dash.attach(api, cfg, self.tracker, testnet=False)
        last = {"BTCUSDT": {"symbol": "BTCUSDT", "close": 81384.6, "sma": 81784.5, "action": "flat"},
                "ETHUSDT": {"symbol": "ETHUSDT", "close": 2600.0, "sma": 2550.0, "action": "hold"}}
        asyncio.run(dash.sync(api, cfg, ref, last, refresh_candles=True))
        return api, TestClient(create_app(tracker=self.tracker))

    def test_endpoints_used_by_frontend(self):
        _, c = self._setup([{"symbol": "ETHUSDT", "side": "LONG", "quantity": 0.04,
                             "entry_price": 2500.0, "unrealized_pnl": 2.0, "leverage": 1}])
        self.assertEqual(c.get("/").status_code, 200)               # 原本的頁面
        st = c.get("/api/status").json()
        self.assertEqual(st["symbols"], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(st["bots"][0]["bot_id"], "futures")
        self.assertAlmostEqual(st["bots"][0]["balance"], 363.24)
        self.assertEqual(c.get("/api/overview").json()["bots"][0]["open_positions"], 1)
        pos = c.get("/api/bots/futures/positions").json()
        self.assertEqual([p["symbol"] for p in pos], ["ETHUSDT"])
        sig = {s["symbol"]: s for s in c.get("/api/bots/futures/signals").json()}
        self.assertEqual(sig["ETHUSDT"]["type"], "LONG")
        self.assertIn("站上", sig["ETHUSDT"]["reasons"][0])
        self.assertIn("空手", sig["BTCUSDT"]["reasons"][0])
        self.assertTrue(c.get("/api/bots/futures/risk_budget").json()["can_trade"])
        self.assertEqual(c.get("/api/candles/BTCUSDT/1d").status_code, 200)
        self.assertEqual(c.get("/api/bots/futures/indicators/BTCUSDT").status_code, 200)
        self.assertIn("overall", c.get("/api/performance").json())

    def test_pause_shows_in_risk_budget(self):
        _, c = self._setup()
        dash.state.bots["futures"].paused = True
        rb = c.get("/api/bots/futures/risk_budget").json()
        self.assertFalse(rb["can_trade"])


class TestCycleIntegration(unittest.TestCase):
    def setUp(self):
        self.tracker = _db()

    def test_paused_does_not_buy_but_still_sells(self):
        api = _api(closes=(10, 10, 10, 10, 12, 1))
        rec = asyncio.run(bot.run_cycle(api, {**CFG, "symbols": ["BTCUSDT"]}, now_ms=NOW, allow_buy=False))[0]
        self.assertEqual(rec["action"], "skip")
        api.futures_market_order.assert_not_awaited()
        api2 = _api([{"symbol": "BTCUSDT", "side": "LONG", "quantity": 2.0, "entry_price": 90.0, "leverage": 1}],
                    closes=(10, 10, 10, 10, 8, 50))
        rec2 = asyncio.run(bot.run_cycle(api2, {**CFG, "symbols": ["BTCUSDT"]}, now_ms=NOW, allow_buy=False))[0]
        self.assertEqual(rec2["action"], "sell")

    def test_buy_and_sell_are_recorded(self):
        cfg = {**CFG, "symbols": ["BTCUSDT"]}
        asyncio.run(bot.run_cycle(_api(closes=(10, 10, 10, 10, 12, 1)), cfg, now_ms=NOW, tracker=self.tracker))
        with self.tracker.session_factory() as s:
            r = s.query(TradeRecord).one()
            self.assertEqual((r.status, r.strategy, r.quantity), ("OPEN", "sma50_long", 2.0))
        api = _api([{"symbol": "BTCUSDT", "side": "LONG", "quantity": 2.0, "entry_price": 90.0, "leverage": 1}],
                   closes=(10, 10, 10, 10, 8, 50))
        asyncio.run(bot.run_cycle(api, cfg, now_ms=NOW, tracker=self.tracker))
        with self.tracker.session_factory() as s:
            r = s.query(TradeRecord).one()
            self.assertEqual(r.status, "CLOSED")
            self.assertAlmostEqual(r.pnl, (100.0 - 90.0) * 2.0)

    def test_dry_run_records_nothing(self):
        asyncio.run(bot.run_cycle(_api(closes=(10, 10, 10, 10, 12, 1)), {**CFG, "symbols": ["BTCUSDT"]},
                                  dry_run=True, now_ms=NOW, tracker=self.tracker))
        with self.tracker.session_factory() as s:
            self.assertEqual(s.query(TradeRecord).count(), 0)


if __name__ == "__main__":
    unittest.main()
