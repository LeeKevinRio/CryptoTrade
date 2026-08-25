"""WebSocket K 線過濾 — 頻寬保護

不過濾時，10 標的 × 5 時框 = 50 條串流的每次跳動都會推給每個瀏覽器，
而前端只用得到當下檢視的那一組（其餘 98% 收到即丟）。
"""

import os
import unittest

from fastapi.testclient import TestClient

from src.web.app import create_app
from src.web.event_bus import bus


def kline_event(symbol, interval):
    return {"type": "kline",
            "data": {"symbol": symbol, "interval": interval,
                     "close": 1.0, "is_closed": False}}


class TestWsKlineFilter(unittest.TestCase):
    def setUp(self):
        os.environ["DASHBOARD_AUTH"] = "false"
        os.environ["WEB_AUTH_TOKEN"] = "t"
        bus._history.clear()
        self.client = TestClient(create_app(tracker=None))

    def _drain(self, url, expected):
        """連線後收 expected 則訊息（連線時會補送 bus.recent 的過濾結果）"""
        out = []
        with self.client.websocket_connect(url) as ws:
            for _ in range(expected):
                out.append(ws.receive_json())
        return out

    def test_only_subscribed_stream_is_sent(self):
        bus._history.extend([
            kline_event("BTCUSDT", "5m"),
            kline_event("ETHUSDT", "5m"),
            kline_event("BTCUSDT", "1h"),
            {"type": "order_open.futures", "data": {"symbol": "ETHUSDT"}},
        ])
        msgs = self._drain("/ws?symbol=BTCUSDT&interval=5m", 2)
        klines = [m for m in msgs if m["type"] == "kline"]
        self.assertEqual(len(klines), 1)
        self.assertEqual(klines[0]["data"]["symbol"], "BTCUSDT")
        self.assertEqual(klines[0]["data"]["interval"], "5m")

    def test_no_filter_means_no_klines(self):
        bus._history.extend([
            kline_event("BTCUSDT", "5m"),
            {"type": "order_open.futures", "data": {"symbol": "BTCUSDT"}},
        ])
        msgs = self._drain("/ws", 1)
        self.assertEqual(msgs[0]["type"], "order_open.futures")

    def test_non_kline_events_always_pass(self):
        bus._history.extend([
            {"type": "order_close.futures", "data": {"symbol": "SOLUSDT"}},
            {"type": "signal.futures", "data": {"symbol": "DOGEUSDT"}},
        ])
        msgs = self._drain("/ws?symbol=BTCUSDT&interval=5m", 2)
        self.assertEqual([m["type"] for m in msgs],
                         ["order_close.futures", "signal.futures"])


if __name__ == "__main__":
    unittest.main()
