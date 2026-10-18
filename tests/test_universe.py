"""自動選股：合約成交額前 N 名、排除項、持倉保留、失敗沿用設定檔"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.data.universe import merge_with_held, rank_symbols

NOW = 1_800_000_000_000
OLD = NOW - 365 * 86_400_000


def _sym(s, **kw):
    d = dict(symbol=s, contractType="PERPETUAL", quoteAsset="USDT", status="TRADING",
             underlyingType="COIN", onboardDate=OLD)
    d.update(kw)
    return d


class TestRankSymbols(unittest.TestCase):
    def test_orders_by_quote_volume_and_cuts(self):
        info = {"symbols": [_sym("A"), _sym("B"), _sym("C")]}
        tickers = [{"symbol": "A", "quoteVolume": "10"}, {"symbol": "B", "quoteVolume": "30"},
                   {"symbol": "C", "quoteVolume": "20"}]
        self.assertEqual(rank_symbols(info, tickers, 2, NOW), ["B", "C"])

    def test_exclusions(self):
        info = {"symbols": [
            _sym("OKUSDT"),
            _sym("USDCUSDT"),                                   # 穩定幣
            _sym("XAUTUSDT"),                                   # 黃金
            _sym("NEWUSDT", onboardDate=NOW - 60 * 86_400_000), # 上市 60 天（未滿 90）
            _sym("QUARTUSDT", contractType="CURRENT_QUARTER"),  # 交割合約
            _sym("BUSDT_", quoteAsset="USDC"),                  # 非 USDT 計價
            _sym("HALTUSDT", status="SETTLING"),                # 停止交易
            _sym("TSLAUSDT", underlyingType="TRADIFI"),         # TradFi
        ]}
        tickers = [{"symbol": s["symbol"], "quoteVolume": "1e12"} for s in info["symbols"]]
        tickers[0]["quoteVolume"] = "1"
        self.assertEqual(rank_symbols(info, tickers, 20, NOW), ["OKUSDT"])

    def test_missing_ticker_is_skipped(self):
        info = {"symbols": [_sym("A"), _sym("B")]}
        self.assertEqual(rank_symbols(info, [{"symbol": "B", "quoteVolume": "5"}], 5, NOW), ["B"])


class TestHypeFilter(unittest.TestCase):
    def test_thresholds(self):
        from src.data.universe import is_hyped
        self.assertFalse(is_hyped([100, 150]))        # +50%：正常
        self.assertTrue(is_hyped([100, 250]))         # +150%：炒作
        self.assertTrue(is_hyped([100, 30]))          # -70%：崩盤
        self.assertTrue(is_hyped([100]))              # 資料不足

    def test_top_volume_skips_hyped_and_backfills(self):
        from src.data.universe import top_volume_symbols
        info = {"symbols": [_sym("A"), _sym("QUSDT"), _sym("B"), _sym("C")]}
        tickers = [{"symbol": s, "quoteVolume": v} for s, v in
                   (("A", "40"), ("QUSDT", "30"), ("B", "20"), ("C", "10"))]
        def kl(symbol, interval, limit):
            end = "400" if symbol == "QUSDT" else "110"
            return [[0, 0, 0, 0, "100"]] * 30 + [[0, 0, 0, 0, end]]
        api = SimpleNamespace(client=SimpleNamespace(
            futures_exchange_info=AsyncMock(return_value=info),
            futures_ticker=AsyncMock(return_value=tickers),
            futures_klines=AsyncMock(side_effect=kl)))
        top, skipped = asyncio.run(top_volume_symbols(api, 2))
        self.assertEqual(top, ["A", "B"])                 # Q 被剔除，由 B 遞補
        self.assertIn("QUSDT", skipped)


class TestMergeWithHeld(unittest.TestCase):
    def test_held_outside_top_kept(self):
        merged, extra = merge_with_held(["A", "B"], ["B", "TAOUSDT"])
        self.assertEqual(merged, ["A", "B", "TAOUSDT"])
        self.assertEqual(extra, ["TAOUSDT"])


class TestResolveUniverse(unittest.TestCase):
    def _orch(self, n, api):
        from src.main import TradeOrchestrator as Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.config = {"trading": {"auto_top_volume": n}}
        o.symbols = ["BTCUSDT", "TAOUSDT"]
        o.api = api
        return o

    def _api(self, top_fail=False):
        api = MagicMock()
        api.client = SimpleNamespace(
            futures_exchange_info=AsyncMock(return_value={"symbols": [_sym("ETHUSDT"), _sym("SOLUSDT")]}),
            futures_ticker=AsyncMock(side_effect=RuntimeError("boom") if top_fail else None,
                                     return_value=[{"symbol": "ETHUSDT", "quoteVolume": "9"},
                                                   {"symbol": "SOLUSDT", "quoteVolume": "8"}]),
            futures_klines=AsyncMock(return_value=[[0, 0, 0, 0, "100"]] * 31),
        )
        api.get_open_positions = AsyncMock(return_value=[{"symbol": "TAOUSDT"}])
        return api

    def test_replaces_list_in_place_and_keeps_held(self):
        o = self._orch(20, self._api())
        ref = o.symbols
        asyncio.run(o._resolve_universe())
        self.assertIs(o.symbols, ref)                       # bots 共用同一 list
        self.assertEqual(o.symbols, ["ETHUSDT", "SOLUSDT", "TAOUSDT"])
        from src.web.state import state
        self.assertEqual(state.universe["held_extra"], ["TAOUSDT"])

    def test_failure_keeps_config_list(self):
        o = self._orch(20, self._api(top_fail=True))
        asyncio.run(o._resolve_universe())
        self.assertEqual(o.symbols, ["BTCUSDT", "TAOUSDT"])
        from src.web.state import state
        self.assertIn("boom", state.universe["error"])

    def test_disabled(self):
        api = self._api()
        o = self._orch(0, api)
        asyncio.run(o._resolve_universe())
        self.assertEqual(o.symbols, ["BTCUSDT", "TAOUSDT"])
        api.client.futures_ticker.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestManageOnly(unittest.TestCase):
    def _bot(self):
        from src.main import TradeBot
        from src.strategy.base_strategy import Signal, SignalType
        from src.web.state import BotState, state
        bot = TradeBot.__new__(TradeBot)
        bot.bot_id = "t"
        bot.config = {}
        bot.logger = MagicMock()
        sig = Signal(type=SignalType.LONG, strength=90.0, symbol="TAOUSDT", reasons=["x"])
        bot.aggregator = MagicMock(evaluate=MagicMock(return_value=sig))
        bot._handle_signal = AsyncMock()
        state.bots["t"] = BotState(bot_id="t", mode="futures")
        return bot, state

    def test_held_extra_blocks_new_entries(self):
        bot, state = self._bot()
        state.universe = {"held_extra": ["TAOUSDT"]}
        asyncio.run(bot.on_kline_close("TAOUSDT", {}, 0.0))
        bot._handle_signal.assert_not_called()

    def test_top_symbol_still_trades(self):
        bot, state = self._bot()
        state.universe = {"held_extra": []}
        asyncio.run(bot.on_kline_close("TAOUSDT", {}, 0.0))
        bot._handle_signal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
