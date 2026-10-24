"""帳戶對帳：入帳分組與策略比對"""

import asyncio
import unittest
from unittest import mock

from scripts import account_check as ac


class TestSummarize(unittest.TestCase):
    def test_groups_strategy_and_other(self):
        rows = [
            {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-3.5"},
            {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.08"},
            {"symbol": "XRPUSDT", "incomeType": "REALIZED_PNL", "income": "-20"},
            {"symbol": "XRPUSDT", "incomeType": "FUNDING_FEE", "income": "-0.5"},
            {"symbol": "", "incomeType": "TRANSFER", "income": "100"},       # 轉帳不算損益
        ]
        s = ac.summarize_income(rows, ["BTCUSDT", "ETHUSDT"])
        self.assertAlmostEqual(s["strategy"]["BTCUSDT"]["REALIZED_PNL"], -3.5)
        self.assertAlmostEqual(s["strategy"]["BTCUSDT"]["COMMISSION"], -0.08)
        self.assertAlmostEqual(sum(s["other"]["XRPUSDT"].values()), -20.5)
        self.assertNotIn("-", s["other"])


class TestVerdict(unittest.TestCase):
    def test_cases(self):
        long1 = {"side": "LONG", "leverage": 1}
        self.assertIn("✓", ac.verdict(True, long1, 1))
        self.assertIn("✓", ac.verdict(False, None, 1))
        self.assertIn("該持有但帳上沒倉", ac.verdict(True, None, 1))
        self.assertIn("該空手但帳上還有多單", ac.verdict(False, long1, 1))
        self.assertIn("不是本策略", ac.verdict(False, {"side": "SHORT", "leverage": 10}, 1))
        self.assertIn("不是本策略", ac.verdict(True, {"side": "LONG", "leverage": 10}, 1))


class TestReport(unittest.TestCase):
    def test_end_to_end_output(self):
        now = 1_800_000_000_000
        kl = [[0, 0, 0, 0, str(100 + i), 0, now - (60 - i) * 86_400_000] for i in range(55)]
        kl.append([0, 0, 0, 0, "1", 0, now + 1])                          # 今天未收盤
        api = mock.MagicMock()
        api.connect = mock.AsyncMock()
        api.disconnect = mock.AsyncMock()
        api.get_usdt_balance = mock.AsyncMock(return_value=363.0)
        api.get_open_positions = mock.AsyncMock(return_value=[
            {"symbol": "XRPUSDT", "side": "LONG", "quantity": 10, "entry_price": 2.5,
             "unrealized_pnl": -4.2, "leverage": 10}])
        api.get_klines = mock.AsyncMock(return_value=kl)
        api.client.futures_income_history = mock.AsyncMock(return_value=[
            {"symbol": "XRPUSDT", "incomeType": "REALIZED_PNL", "income": "-12", "time": now}])
        cfg = {"binance": {"api_key": "k", "api_secret": "s", "testnet": False},
               "daily_trend": {"symbols": ["BTCUSDT"]}}
        with mock.patch("src.data.binance_client.BinanceAPI", return_value=api), \
             mock.patch("src.utils.config.load_config", return_value=cfg), \
             mock.patch("time.time", return_value=now / 1000), \
             mock.patch("builtins.print") as p:
            asyncio.run(ac.amain(3))
        out = "\n".join(str(c.args[0]) if c.args else "" for c in p.call_args_list)
        self.assertIn("363.00", out)
        self.assertIn("★ 不是本策略", out)
        self.assertIn("-12.00", out)
        self.assertIn("應持有｜⚠ 不一致：該持有但帳上沒倉", out)
        api.futures_market_order.assert_not_called()                      # 唯讀


if __name__ == "__main__":
    unittest.main()
