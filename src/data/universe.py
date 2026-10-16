"""交易標的池 —— 啟動時依幣安 U 本位合約 24h 成交額自動選出前 N 名

先前標的清單寫死在 settings.yaml（2026-09-07 的成交量 TOP10），一個月後排名早已變動
（TAO、LINK 掉出前 15）卻沒人更新。改成每次啟動現抓，清單永遠跟著當下的成交量走。
"""

import time

from src.utils.logger import setup_logger

logger = setup_logger("universe")

# 非加密資產、穩定幣、指數類 —— 成交量可能很大，但不是這個策略要交易的東西
EXCLUDED = {
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "USDPUSDT", "DAIUSDT",
    "XAUTUSDT", "PAXGUSDT", "BTCDOMUSDT", "DEFIUSDT",
}
MIN_LISTED_DAYS = 30     # 新上市不足 30 天：4h 指標暖身不夠、價格行為也不穩定


def rank_symbols(exchange_info: dict, tickers: list[dict], n: int,
                 now_ms: int | None = None, min_listed_days: int = MIN_LISTED_DAYS,
                 exclude: set[str] = EXCLUDED) -> list[str]:
    """純函式：從交易所資訊 + 24h ticker 選出成交額前 n 名的可交易 USDT 永續合約"""
    now_ms = now_ms or int(time.time() * 1000)
    min_age_ms = min_listed_days * 86_400_000
    ok = set()
    for s in exchange_info.get("symbols", []):
        if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
                and s.get("underlyingType", "COIN") == "COIN"       # 排除 TradFi／指數合約
                and s["symbol"] not in exclude
                and now_ms - int(s.get("onboardDate", 0)) >= min_age_ms):
            ok.add(s["symbol"])
    vol = [(t["symbol"], float(t.get("quoteVolume", 0) or 0)) for t in tickers if t.get("symbol") in ok]
    vol.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in vol[:n]]


async def top_volume_symbols(api, n: int) -> list[str]:
    info = await api.client.futures_exchange_info()
    tickers = await api.client.futures_ticker()
    return rank_symbols(info, tickers, n)


def merge_with_held(top: list[str], held: list[str]) -> tuple[list[str], list[str]]:
    """持倉中但掉出前 N 名的標的仍要納入，否則停損／停利沒人管。回傳 (合併清單, 額外納入的)"""
    extra = [h for h in held if h not in top]
    return top + extra, extra
