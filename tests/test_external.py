"""外部訊號（TradingView webhook）測試"""

import time
import unittest

from src.external.models import ExternalSignal, parse_payload
from src.external.store import ExternalSignalStore


class TestParsePayload(unittest.TestCase):
    def test_minimal(self):
        s = parse_payload({"symbol": "BTCUSDT", "action": "buy"})
        self.assertEqual(s.symbol, "BTCUSDT")
        self.assertEqual(s.action, "LONG")
        self.assertEqual(s.strength, 100.0)

    def test_aliases(self):
        for raw, want in (("BUY", "LONG"), ("sell", "SHORT"), ("exit", "CLOSE"),
                          ("flat", "CLOSE"), ("Short", "SHORT")):
            s = parse_payload({"ticker": "ETHUSDT", "side": raw})
            self.assertEqual(s.action, want, raw)

    def test_strips_tradingview_suffix(self):
        s = parse_payload({"symbol": "BTCUSDT.P", "action": "long"})
        self.assertEqual(s.symbol, "BTCUSDT")

    def test_strength_clamped(self):
        self.assertEqual(parse_payload({"symbol": "X", "action": "buy", "strength": 500}).strength, 100.0)
        self.assertEqual(parse_payload({"symbol": "X", "action": "buy", "strength": -10}).strength, 0.0)

    def test_indicators_preserved(self):
        s = parse_payload({"symbol": "X", "action": "buy",
                           "indicators": {"myIndicator": 42.5}})
        self.assertEqual(s.indicators["myIndicator"], 42.5)

    def test_missing_fields_raise(self):
        with self.assertRaises(ValueError):
            parse_payload({"action": "buy"})
        with self.assertRaises(ValueError):
            parse_payload({"symbol": "X"})

    def test_bad_action_raises(self):
        with self.assertRaises(ValueError):
            parse_payload({"symbol": "X", "action": "hodl"})

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            parse_payload("not a dict")


class TestBias(unittest.TestCase):
    def test_long_short_bias(self):
        self.assertEqual(ExternalSignal("X", "LONG").bias, 1.0)
        self.assertEqual(ExternalSignal("X", "SHORT").bias, -1.0)
        self.assertEqual(ExternalSignal("X", "CLOSE").bias, 0.0)
        self.assertEqual(ExternalSignal("X", "NEUTRAL").bias, 0.0)

    def test_strength_scales_bias(self):
        self.assertAlmostEqual(ExternalSignal("X", "LONG", strength=50).bias, 0.5)
        self.assertAlmostEqual(ExternalSignal("X", "SHORT", strength=25).bias, -0.25)

    def test_expired_signal_has_zero_bias(self):
        s = ExternalSignal("X", "LONG", ttl_seconds=1)
        s.received_at = time.time() - 10
        self.assertTrue(s.expired)
        self.assertEqual(s.bias, 0.0)
        self.assertFalse(s.supports("LONG", 0.2))

    def test_supports_direction(self):
        s = ExternalSignal("X", "LONG", strength=60)
        self.assertTrue(s.supports("LONG", 0.2))
        self.assertFalse(s.supports("SHORT", 0.2))

    def test_supports_respects_threshold(self):
        weak = ExternalSignal("X", "LONG", strength=10)   # bias 0.1
        self.assertFalse(weak.supports("LONG", 0.2))


class TestStore(unittest.TestCase):
    def test_put_and_get(self):
        st = ExternalSignalStore()
        st.put(ExternalSignal("BTCUSDT", "LONG"))
        self.assertIsNotNone(st.get("BTCUSDT"))

    def test_expired_not_returned(self):
        st = ExternalSignalStore()
        s = ExternalSignal("BTCUSDT", "LONG", ttl_seconds=1)
        s.received_at = time.time() - 10
        st.put(s)
        self.assertIsNone(st.get("BTCUSDT"))         # 過期不回傳
        self.assertIsNotNone(st.get_raw("BTCUSDT"))  # 但仍可供 UI 顯示

    def test_latest_replaces(self):
        st = ExternalSignalStore()
        st.put(ExternalSignal("BTCUSDT", "LONG"))
        st.put(ExternalSignal("BTCUSDT", "SHORT"))
        self.assertEqual(st.get("BTCUSDT").action, "SHORT")

    def test_history_capped(self):
        st = ExternalSignalStore(max_history=3)
        for i in range(5):
            st.put(ExternalSignal("BTCUSDT", "LONG", note=str(i)))
        self.assertEqual(len(st.history(10)), 3)

    def test_unknown_symbol_returns_none(self):
        self.assertIsNone(ExternalSignalStore().get("NOPE"))


class TestStrategyIntegration(unittest.TestCase):
    """外部訊號應依權重加分，且方向要正確"""

    def _cfg(self, w=0.25):
        return {"strategy": {
            "min_signals": 1, "external_threshold": 0.2,
            "medium_signal_threshold": 50,
            "weights": {"rsi": 0.2, "bollinger": 0.1, "macd": 0.15, "volume": 0.1,
                        "multi_timeframe": 0.1, "funding_rate": 0.1,
                        "sentiment": 0.0, "external": w},
            "dip_buyer": {"rsi_oversold": 30},
            "short_seller": {"rsi_overbought": 70},
        }}

    def _candles(self):
        import numpy as np
        import pandas as pd
        n = 60
        close = np.linspace(100, 95, n)
        df = pd.DataFrame({"open": close, "high": close + 0.2,
                           "low": close - 0.2, "close": close,
                           "volume": np.full(n, 1000.0)})
        return {"5m": df, "15m": df, "1h": df}

    def test_external_adds_weighted_score(self):
        from src.strategy.dip_buyer import DipBuyer
        db = DipBuyer(self._cfg(w=0.25))
        c = self._candles()
        base = db.analyze("BTCUSDT", c, 0.0, None, None)
        with_ext = db.analyze("BTCUSDT", c, 0.0, None,
                              ExternalSignal("BTCUSDT", "LONG", strength=100))
        self.assertAlmostEqual(with_ext.strength - base.strength, 25.0, places=1)
        self.assertTrue(any("外部訊號" in r for r in with_ext.reasons))

    def test_short_signal_does_not_boost_long(self):
        from src.strategy.dip_buyer import DipBuyer
        db = DipBuyer(self._cfg())
        c = self._candles()
        base = db.analyze("BTCUSDT", c, 0.0, None, None)
        with_ext = db.analyze("BTCUSDT", c, 0.0, None,
                              ExternalSignal("BTCUSDT", "SHORT", strength=100))
        self.assertAlmostEqual(with_ext.strength, base.strength, places=1)

    def test_zero_weight_is_noop(self):
        from src.strategy.dip_buyer import DipBuyer
        db = DipBuyer(self._cfg(w=0.0))
        c = self._candles()
        base = db.analyze("BTCUSDT", c, 0.0, None, None)
        with_ext = db.analyze("BTCUSDT", c, 0.0, None,
                              ExternalSignal("BTCUSDT", "LONG", strength=100))
        self.assertAlmostEqual(with_ext.strength, base.strength, places=1)


if __name__ == "__main__":
    unittest.main()
