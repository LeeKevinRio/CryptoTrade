"""市場情緒/消息面子系統

在技術分析之外，加入「消息面」訊號源，作為訊號聚合器的第 7 個加權條件：
- Fear & Greed Index（市場情緒，反向解讀：極度恐慌挺多、極度貪婪挺空）
- 加密新聞情緒（RSS 標題 + 詞典評分，按 BTC/ETH 分別加權）

設計原則：抓不到網路時 bias=0，自動退回純技術分析，絕不阻斷交易主迴圈。
"""

from src.sentiment.models import SentimentScore
from src.sentiment.provider import MarketSentimentProvider

__all__ = ["SentimentScore", "MarketSentimentProvider"]
