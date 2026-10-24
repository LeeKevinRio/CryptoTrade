"""日線 50 均線機器人：只用已收盤日 K、判斷正確、只動自己的倉、只看模式不下單"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src import daily_trend_bot as bot

NOW = 1_800_000_000_000
DAY = 86_400_000


def _klines(closes, last_open=False):
    """最後一根是否為今天尚未收盤的 K 線"""
    rows = []
    n = len(closes)
    for i, c in enumerate(closes):
        close_time = NOW - (n - i) * DAY + DAY - 1
        if last_open and i == n - 1:
            close_time = NOW + DAY
        rows.append([0, c, c, c, str(c), 0, close_time])
    return rows


CFG = {**bot.DEFAULTS, "sma_days": 5, "capital_usdt": 200.0}
INFO = {"filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}]}


def _api(closes, positions=(), last_open=True, price=100.0):
    api = MagicMock()
    api.get_klines = AsyncMock(return_value=_klines(closes, last_open))
    api.get_open_positions = AsyncMock(return_value=list(positions))
    api.get_ticker_price = AsyncMock(return_value=price)
    api.get_symbol_info = AsyncMock(return_value=INFO)
    api.set_margin_type = AsyncMock()
    api.set_leverage = AsyncMock()
    api.futures_market_order = AsyncMock()
    return api


class TestPure(unittest.TestCase):
    def test_drops_unclosed_candle(self):
        self.assertEqual(bot.closed_closes(_klines([1, 2, 3], last_open=True), NOW), [1.0, 2.0])
        self.assertEqual(bot.closed_closes(_klines([1, 2, 3], last_open=False), NOW), [1.0, 2.0, 3.0])

    def test_should_hold(self):
        self.assertTrue(bot.should_hold([10, 10, 10, 10, 11], 5))
        self.assertFalse(bot.should_hold([10, 10, 10, 10, 9], 5))
        self.assertFalse(bot.should_hold([10] * 5, 5))           # 等於均線不算站上
        self.assertIsNone(bot.should_hold([10] * 4, 5))

    def test_floor_qty(self):
        self.assertEqual(bot.floor_qty(1.23456, 0.001), 1.234)
        self.assertEqual(bot.floor_qty(0.0049, 0.001), 0.004)
        self.assertEqual(bot.floor_qty(17.9, 1.0), 17.0)

    def test_seconds_until(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 22, 23, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(bot.seconds_until("00:05", now), 65 * 60)


class TestCycle(unittest.TestCase):
    def run_(self, api, dry=False):
        return asyncio.run(bot.run_cycle(api, {**CFG, "symbols": ["BTCUSDT"]}, dry_run=dry, now_ms=NOW))

    def test_buys_when_above_and_flat(self):
        # 已收盤 5 根：10,10,10,10,12 → 站上均線；今天未收盤那根（1）必須被忽略
        api = _api([10, 10, 10, 10, 12, 1])
        rec = self.run_(api)[0]
        self.assertEqual(rec["action"], "buy")
        self.assertEqual(rec["qty"], 2.0)                       # 200U × 1x ÷ 100
        api.set_leverage.assert_awaited_with("BTCUSDT", 1)
        api.futures_market_order.assert_awaited_with("BTCUSDT", "BUY", 2.0)

    def test_sells_when_below_and_long(self):
        api = _api([10, 10, 10, 10, 8, 50], [{"symbol": "BTCUSDT", "side": "LONG", "quantity": 2.0, "leverage": 1}])
        self.assertEqual(self.run_(api)[0]["action"], "sell")
        api.futures_market_order.assert_awaited_with("BTCUSDT", "SELL", 2.0, reduce_only=True)

    def test_holds_and_stays_flat(self):
        api = _api([10, 10, 10, 10, 12, 1], [{"symbol": "BTCUSDT", "side": "LONG", "quantity": 2.0, "leverage": 1}])
        self.assertEqual(self.run_(api)[0]["action"], "hold")
        api2 = _api([10, 10, 10, 10, 8, 50])
        self.assertEqual(self.run_(api2)[0]["action"], "flat")
        api.futures_market_order.assert_not_awaited()
        api2.futures_market_order.assert_not_awaited()

    def test_never_touches_foreign_positions(self):
        """舊機器人留下的 10 倍多單／空單：不買、不賣、只提醒"""
        for pos in ({"symbol": "BTCUSDT", "side": "LONG", "quantity": 0.1, "leverage": 10},
                    {"symbol": "BTCUSDT", "side": "SHORT", "quantity": 0.1, "leverage": 1}):
            api = _api([10, 10, 10, 10, 8, 50], [pos])
            rec = self.run_(api)[0]
            self.assertEqual(rec["action"], "skip")
            api.futures_market_order.assert_not_awaited()

    def test_dry_run_places_no_orders(self):
        api = _api([10, 10, 10, 10, 12, 1])
        self.assertEqual(self.run_(api, dry=True)[0]["action"], "buy")
        api.futures_market_order.assert_not_awaited()
        api.set_leverage.assert_not_awaited()

    def test_below_min_notional_skips(self):
        api = _api([10, 10, 10, 10, 12, 1], price=1e9)
        self.assertEqual(self.run_(api)[0]["action"], "skip")
        api.futures_market_order.assert_not_awaited()

    def test_not_enough_history_does_nothing(self):
        api = _api([10, 12, 1])
        self.assertEqual(self.run_(api)[0]["action"], "skip")

    def test_one_symbol_error_does_not_stop_others(self):
        api = _api([10, 10, 10, 10, 12, 1])
        api.get_klines = AsyncMock(side_effect=[RuntimeError("boom"), _klines([10, 10, 10, 10, 12, 1], True)])
        out = asyncio.run(bot.run_cycle(api, {**CFG, "symbols": ["AUSDT", "BUSDT"]}, now_ms=NOW))
        self.assertEqual([r["action"] for r in out], ["error", "buy"])
        self.assertEqual(out[1]["qty"], 1.0)                    # 200U 等分給兩個標的

    def test_matches_backtest_rule(self):
        """與回測 backtest/daily_trend.positions(sma) 的判斷一致"""
        import numpy as np
        import pandas as pd
        from backtest import daily_trend as dt
        rng = np.random.default_rng(3)
        closes = list(100 * np.cumprod(1 + rng.normal(0, 0.03, 120)))
        s = pd.Series(closes, index=pd.date_range("2026-01-01", periods=120, freq="D", tz="UTC"))
        pos = dt.positions(s, {"kind": "sma", "n": 50, "mode": "long"})
        for t in range(50, 120):
            self.assertEqual(bool(bot.should_hold(closes[:t + 1], 50)), pos[t] == 1)


if __name__ == "__main__":
    unittest.main()


class TestTopNBacktest(unittest.TestCase):
    def test_pit_universe_and_hold_outside(self):
        import numpy as np
        import pandas as pd
        from backtest import daily_trend as d
        idx = pd.date_range("2024-01-01", periods=200, freq="D", tz="UTC")
        up = pd.Series(np.linspace(10, 30, 200), index=idx)
        closes = pd.DataFrame({"AUSDT": up, "BUSDT": up, "CUSDT": up})
        qvol = pd.DataFrame({"AUSDT": 3.0, "BUSDT": 2.0, "CUSDT": 1.0}, index=idx)
        qvol.loc[idx[150]:, "AUSDT"] = 0.5                         # A 第 150 天後掉出前 2 名
        uni = d.topn_universe(closes, qvol, top_n=2, min_age=90)
        self.assertFalse(uni.iloc[80].any())                        # 上市未滿 90 天：不在名單
        self.assertTrue(uni.iloc[100][["AUSDT", "BUSDT"]].all())
        self.assertFalse(uni.iloc[100]["CUSDT"])
        res = d.topn_sma(closes, closes * 0, uni, top_n=2)
        self.assertEqual(res.max_held, 3)                           # A 掉出名單仍持有、C 遞補買進
        self.assertGreater(res.daily.sum(), 0)
