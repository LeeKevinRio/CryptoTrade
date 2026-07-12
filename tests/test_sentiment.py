"""消息面/市場情緒測試（全離線，不打網路）"""

import asyncio
import unittest

from src.sentiment.models import SentimentScore, fear_greed_to_bias
from src.sentiment.news import score_headline, score_for_symbol, _parse_titles
from src.sentiment.provider import MarketSentimentProvider


class TestFearGreedBias(unittest.TestCase):
    def test_extreme_fear_is_bullish(self):
        self.assertAlmostEqual(fear_greed_to_bias(0), 1.0)

    def test_extreme_greed_is_bearish(self):
        self.assertAlmostEqual(fear_greed_to_bias(100), -1.0)

    def test_neutral(self):
        self.assertAlmostEqual(fear_greed_to_bias(50), 0.0)

    def test_monotonic(self):
        self.assertGreater(fear_greed_to_bias(20), fear_greed_to_bias(60))


class TestHeadlineScoring(unittest.TestCase):
    def test_bullish(self):
        self.assertGreater(score_headline("Bitcoin ETF approval sparks massive rally"), 0)

    def test_bearish(self):
        self.assertLess(score_headline("Major exchange hacked, BTC plunges in selloff"), 0)

    def test_neutral_returns_none(self):
        self.assertIsNone(score_headline("Ethereum developers hold weekly meeting"))

    def test_mixed_balances(self):
        # 一利多一利空 → 0
        self.assertEqual(score_headline("rally then crash"), 0.0)


class TestSymbolScoring(unittest.TestCase):
    def test_filters_by_symbol_when_enough(self):
        headlines = [
            "Bitcoin surges to new high on ETF inflows",
            "Bitcoin adoption accelerates among institutions",
            "Bitcoin rally continues past resistance",
            "Ethereum network sees routine upgrade",   # 非 BTC
        ]
        score, top = score_for_symbol(headlines, "BTCUSDT", min_matches=3)
        self.assertIsNotNone(score)
        self.assertGreater(score, 0)
        self.assertTrue(top)

    def test_falls_back_to_overall_when_few(self):
        # 只有 1 則 BTC 新聞 (< min_matches=3) → 用全體
        headlines = [
            "Bitcoin dips slightly",
            "Solana rallies hard on new partnership and adoption",
            "Cardano surges with bullish breakout",
        ]
        score, _ = score_for_symbol(headlines, "BTCUSDT", min_matches=3)
        self.assertIsNotNone(score)  # 用全體池，仍有分數


class TestSentimentScore(unittest.TestCase):
    def test_support_thresholds(self):
        s = SentimentScore(symbol="BTCUSDT", bias=0.3)
        self.assertTrue(s.long_support(0.2))
        self.assertFalse(s.short_support(0.2))
        s2 = SentimentScore(symbol="BTCUSDT", bias=-0.5)
        self.assertTrue(s2.short_support(0.2))
        self.assertEqual(s2.label, "強烈看空")

    def test_neutral_label(self):
        self.assertEqual(SentimentScore(symbol="X", bias=0.0).label, "中性")


class TestProviderCombination(unittest.TestCase):
    def _provider(self, **over):
        cfg = {"sentiment": {"enabled": True, **over}}
        return MarketSentimentProvider(cfg)

    def test_build_score_combines_fg_and_news(self):
        p = self._provider(fear_greed_weight=0.5, news_weight=0.5)
        p._fg = (10, "Extreme Fear")     # 反向 → +0.8
        p._headlines = ["Bitcoin surges in bullish rally with record inflows"]
        p._fetched_once = True
        score = p._build_score("BTCUSDT")
        self.assertGreater(score.bias, 0)          # 恐慌 + 利多 → 明確看多
        self.assertEqual(score.fear_greed, 10)
        self.assertIn("fear_greed", score.sources)
        self.assertIn("news", score.sources)

    def test_fg_only_when_no_news(self):
        p = self._provider()
        p._fg = (90, "Extreme Greed")    # 反向 → -0.8
        p._headlines = []
        p._fetched_once = True
        score = p._build_score("ETHUSDT")
        self.assertLess(score.bias, 0)
        self.assertIsNone(score.news_score)

    def test_no_data_is_neutral(self):
        p = self._provider()
        p._fg = None
        p._headlines = []
        p._fetched_once = True
        self.assertEqual(p._build_score("BTCUSDT").bias, 0.0)

    def test_disabled_returns_neutral_without_network(self):
        p = MarketSentimentProvider({"sentiment": {"enabled": False}})
        score = asyncio.run(p.get("BTCUSDT"))
        self.assertEqual(score.bias, 0.0)


class TestRssParsing(unittest.TestCase):
    def test_parse_rss_titles(self):
        xml = """<rss><channel>
            <item><title>Bitcoin rallies hard</title></item>
            <item><title>Ethereum upgrade live</title></item>
        </channel></rss>"""
        titles = _parse_titles(xml)
        self.assertEqual(len(titles), 2)
        self.assertIn("Bitcoin rallies hard", titles)

    def test_parse_bad_xml_returns_empty(self):
        self.assertEqual(_parse_titles("<not valid"), [])


if __name__ == "__main__":
    unittest.main()
