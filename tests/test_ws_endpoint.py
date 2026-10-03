"""正式站合約改用新行情網址；靜默連線逾時重連"""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from src.data import websocket_feed as wf


class TestEndpoint(unittest.TestCase):
    def test_live_futures_uses_market_path(self):
        feed = wf.WebSocketFeed(MagicMock(testnet=False), market="futures")
        feed.bsm = MagicMock()
        s = feed._make_socket("BTCUSDT", "5m")
        self.assertIsInstance(s, wf._RawStream)
        self.assertEqual(s.url, "wss://fstream.binance.com/market/ws/btcusdt@kline_5m")
        feed.bsm.kline_futures_socket.assert_not_called()

    def test_testnet_keeps_library_socket(self):
        feed = wf.WebSocketFeed(MagicMock(testnet=True), market="futures")
        feed.bsm = MagicMock()
        feed._make_socket("BTCUSDT", "5m")
        feed.bsm.kline_futures_socket.assert_called_once_with(symbol="BTCUSDT", interval="5m")

    def test_spot_unchanged(self):
        feed = wf.WebSocketFeed(MagicMock(testnet=False), market="spot")
        feed.bsm = MagicMock()
        feed._make_socket("BTCUSDT", "5m")
        feed.bsm.kline_socket.assert_called_once()


class _Silent:
    """連得上但永遠不送資料（正是舊網址的行為）"""
    opened = 0

    async def __aenter__(self):
        _Silent.opened += 1
        return self

    async def __aexit__(self, *a):
        pass

    async def recv(self):
        await asyncio.sleep(3600)


class _OneKline:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def recv(self):
        await asyncio.sleep(0)
        return {"e": "kline", "s": "BTCUSDT", "k": {"s": "BTCUSDT", "i": "5m", "o": "1", "h": "2",
                                                    "l": "0.5", "c": "1.5", "v": "10", "x": True, "t": 1}}


class TestSilentReconnect(unittest.TestCase):
    def test_silent_stream_triggers_reconnect_then_data_flows(self):
        feed = wf.WebSocketFeed(MagicMock(testnet=False), market="futures")
        got = []
        feed.on_kline(lambda d: got.append(d))
        socks = iter([_Silent(), _OneKline()])
        feed._make_socket = lambda s, i: next(socks)

        async def run():
            with patch.object(wf, "NO_DATA_TIMEOUT", 0.05):
                task = asyncio.create_task(feed._kline_loop("BTCUSDT", "5m"))
                for _ in range(200):
                    await asyncio.sleep(0.02)
                    if got:
                        break
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())
        self.assertEqual(feed.reconnects, 1)
        self.assertTrue(got)
        self.assertTrue(got[0]["is_closed"])
        self.assertGreater(feed.last_kline_at, 0)


if __name__ == "__main__":
    unittest.main()
