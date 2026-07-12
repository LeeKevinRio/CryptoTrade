"""加密新聞情緒 — RSS 標題擷取 + 詞典評分

免費、無金鑰：讀取加密新聞媒體的 RSS，對標題做詞典式情緒評分。
可按交易對（BTC/ETH）過濾出相關標題，算出該幣的新聞情緒。

刻意不引入第三方 NLP：用可維護的關鍵字詞典即可產生穩定的 -1..+1 分數；
若日後要更準，可在 score_headline() 換成 LLM 評分。
"""

import re
import xml.etree.ElementTree as ET

import aiohttp

from src.utils.logger import setup_logger

logger = setup_logger("news_sentiment")

# 免費 RSS 來源（可於 config 覆寫）
DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://cryptopotato.com/feed/",
]

# 交易對 → 標題關鍵字（小寫）
SYMBOL_KEYWORDS = {
    "BTCUSDT": ("btc", "bitcoin"),
    "ETHUSDT": ("eth", "ethereum", "ether"),
}

# 詞典（小寫、詞邊界比對）
BULLISH_WORDS = {
    "surge", "surges", "rally", "rallies", "soar", "soars", "soaring", "jump", "jumps",
    "gain", "gains", "bullish", "breakout", "adoption", "approval", "approved", "etf",
    "inflow", "inflows", "record high", "all-time high", "ath", "upgrade", "partnership",
    "institutional", "accumulate", "accumulation", "buy", "buying", "green", "moon",
    "recover", "recovery", "rebound", "optimistic", "boost", "milestone", "surpass",
}
BEARISH_WORDS = {
    "crash", "crashes", "plunge", "plunges", "plummet", "dump", "dumps", "selloff",
    "sell-off", "bearish", "hack", "hacked", "exploit", "breach", "ban", "banned",
    "lawsuit", "sue", "sued", "sec", "crackdown", "fraud", "scam", "collapse",
    "liquidation", "liquidations", "outflow", "outflows", "fear", "fud", "warning",
    "decline", "drops", "drop", "fall", "falls", "tumble", "slump", "red", "risk",
    "investigation", "delist", "delisted", "bankruptcy", "default",
}


def score_headline(title: str) -> float | None:
    """對單一標題評分：(利多詞 - 利空詞) / 命中詞數，範圍 -1..+1。無命中回 None。"""
    text = title.lower()
    tokens = set(re.findall(r"[a-z\-']+", text))
    # 多字詞（如 all-time high）另外用子字串比對
    pos = sum(1 for w in BULLISH_WORDS if (w in tokens) or (" " in w and w in text))
    neg = sum(1 for w in BEARISH_WORDS if (w in tokens) or (" " in w and w in text))
    total = pos + neg
    if total == 0:
        return None
    return (pos - neg) / total


def _parse_titles(xml_text: str) -> list[str]:
    """從 RSS/Atom XML 取出所有標題。"""
    titles: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("RSS 解析失敗：%s", e)
        return titles
    # RSS: .//item/title；Atom: .//{ns}entry/{ns}title
    for tag in (".//item/title", ".//{http://www.w3.org/2005/Atom}entry/"
                "{http://www.w3.org/2005/Atom}title"):
        for el in root.iterfind(tag):
            if el.text and el.text.strip():
                titles.append(el.text.strip())
    return titles


async def fetch_headlines(feeds: list[str], timeout: float = 6.0,
                          max_per_feed: int = 30) -> list[str]:
    """抓取多個 RSS 來源的標題，合併回傳。單一來源失敗不影響其他。"""
    headlines: list[str] = []
    try:
        async with aiohttp.ClientSession() as session:
            for url in feeds:
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=timeout),
                        headers={"User-Agent": "CryptoTrade/1.0"},
                    ) as resp:
                        if resp.status != 200:
                            logger.warning("RSS %s 回應 %s", url, resp.status)
                            continue
                        text = await resp.text()
                except Exception as e:  # noqa: BLE001
                    logger.warning("RSS %s 抓取失敗：%s", url, e)
                    continue
                headlines.extend(_parse_titles(text)[:max_per_feed])
    except Exception as e:  # noqa: BLE001
        logger.warning("news session 錯誤：%s", e)
    return headlines


def score_for_symbol(headlines: list[str], symbol: str,
                     min_matches: int = 3) -> tuple[float | None, list[str]]:
    """對某交易對算新聞情緒分數。

    優先用「標題含該幣關鍵字」的新聞；若相關新聞太少（< min_matches），
    退回用全體加密新聞的整體情緒，避免樣本過小造成雜訊。

    Returns:
        (score -1..+1 或 None, 用到的代表性標題)
    """
    keywords = SYMBOL_KEYWORDS.get(symbol.upper(), ())
    relevant = [h for h in headlines if any(k in h.lower() for k in keywords)]
    pool = relevant if len(relevant) >= min_matches else headlines

    scored = [(h, score_headline(h)) for h in pool]
    scored = [(h, s) for h, s in scored if s is not None]
    if not scored:
        return None, []

    avg = sum(s for _, s in scored) / len(scored)
    # 取最具方向性的標題作為代表（供 log / dashboard）
    scored.sort(key=lambda x: abs(x[1]), reverse=True)
    top = [h for h, _ in scored[:5]]
    return max(-1.0, min(1.0, avg)), top
