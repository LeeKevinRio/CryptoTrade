"""市場情緒提供者 — 合成 Fear & Greed + 新聞，含 TTL 快取與離線退回"""

import asyncio
import time

from src.sentiment.fear_greed import fetch_fear_greed
from src.sentiment.models import SentimentScore, fear_greed_to_bias
from src.sentiment.news import DEFAULT_FEEDS, score_for_symbol
from src.sentiment import news as news_mod
from src.utils.logger import setup_logger

logger = setup_logger("sentiment")


class MarketSentimentProvider:
    """為每個交易對提供合成情緒 bias。

    - 原始資料（F&G + 新聞標題）以 TTL 快取，避免每根 K 線都打 API
    - 抓取失敗時沿用上次成功值；從未成功則回中性 bias=0（策略自動退回純技術面）
    - 執行緒/協程安全：refresh 以 asyncio.Lock 保護
    """

    def __init__(self, config: dict):
        scfg = config.get("sentiment", {})
        self.enabled = scfg.get("enabled", True)
        self.ttl = float(scfg.get("refresh_seconds", 900))
        self.feeds = scfg.get("news_feeds") or DEFAULT_FEEDS
        self.timeout = float(scfg.get("http_timeout", 6.0))
        # 合成權重：F&G(反向市場情緒) vs 新聞(順勢)
        self.fg_weight = float(scfg.get("fear_greed_weight", 0.6))
        self.news_weight = float(scfg.get("news_weight", 0.4))

        self._fg: tuple[int, str] | None = None
        self._headlines: list[str] = []
        self._last_fetch: float = 0.0
        self._fetched_once: bool = False
        self._lock = asyncio.Lock()

    async def get(self, symbol: str) -> SentimentScore:
        if not self.enabled:
            return SentimentScore(symbol=symbol)  # bias=0
        await self._maybe_refresh()
        return self._build_score(symbol)

    async def _maybe_refresh(self):
        now = time.time()
        if self._fetched_once and (now - self._last_fetch) < self.ttl:
            return
        async with self._lock:
            # 二次檢查：等鎖期間別的協程可能已刷新
            now = time.time()
            if self._fetched_once and (now - self._last_fetch) < self.ttl:
                return
            fg = await fetch_fear_greed(self.timeout)
            if fg is not None:
                self._fg = fg
            headlines = await news_mod.fetch_headlines(self.feeds, self.timeout)
            if headlines:
                self._headlines = headlines
            self._last_fetch = time.time()
            self._fetched_once = True
            if fg is None and not headlines:
                logger.warning("情緒資料全數抓取失敗，本輪沿用舊值 / 中性")
            else:
                logger.info(
                    "情緒資料已更新：F&G=%s 新聞標題=%d 則",
                    self._fg[0] if self._fg else "—", len(self._headlines),
                )

    def _build_score(self, symbol: str) -> SentimentScore:
        fg_val, fg_label = self._fg if self._fg else (None, "")
        news_score, top = (
            score_for_symbol(self._headlines, symbol) if self._headlines else (None, [])
        )

        components: list[float] = []
        weights: list[float] = []
        sources: list[str] = []
        if fg_val is not None:
            components.append(fear_greed_to_bias(fg_val))
            weights.append(self.fg_weight)
            sources.append("fear_greed")
        if news_score is not None:
            components.append(news_score)
            weights.append(self.news_weight)
            sources.append("news")

        if weights:
            bias = sum(c * w for c, w in zip(components, weights)) / sum(weights)
        else:
            bias = 0.0

        return SentimentScore(
            symbol=symbol,
            bias=round(max(-1.0, min(1.0, bias)), 3),
            fear_greed=fg_val,
            fear_greed_label=fg_label,
            news_score=round(news_score, 3) if news_score is not None else None,
            news_headlines=top,
            sources=sources,
            updated_at=self._last_fetch or time.time(),
        )
