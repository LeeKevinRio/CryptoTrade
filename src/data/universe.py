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
MIN_LISTED_DAYS = 90     # 上市未滿 90 天的新幣：價格行為不穩定、常被炒作（2026-10-18 QUSDT 單日 3 連停損）
MAX_30D_RISE = 1.0       # 過去 30 天漲幅 > 100% → 正在被炒作，逆勢抄底策略在這類幣上反覆停損
MAX_30D_DROP = -0.6      # 過去 30 天跌幅 > 60% → 崩盤中，同樣排除


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


def is_hyped(daily_closes: list[float], max_rise: float = MAX_30D_RISE,
             max_drop: float = MAX_30D_DROP) -> bool:
    """過去 30 根日 K 的漲跌幅是否超出正常範圍（資料不足視為不正常）"""
    if len(daily_closes) < 2 or daily_closes[0] <= 0:
        return True
    chg = daily_closes[-1] / daily_closes[0] - 1
    return chg > max_rise or chg < max_drop


async def top_volume_symbols(api, n: int) -> tuple[list[str], dict[str, str]]:
    """回傳 (前 n 名, {被排除的標的: 原因})。先多取候選，再剔除被炒作／崩盤中的幣。"""
    info = await api.client.futures_exchange_info()
    tickers = await api.client.futures_ticker()
    candidates = rank_symbols(info, tickers, n * 2 + 10)
    picked: list[str] = []
    skipped: dict[str, str] = {}
    for sym in candidates:
        if len(picked) >= n:
            break
        try:
            kl = await api.client.futures_klines(symbol=sym, interval="1d", limit=31)
            closes = [float(k[4]) for k in kl]
        except Exception as e:  # noqa: BLE001 — 抓不到日 K 就保守排除
            skipped[sym] = f"日 K 取得失敗: {e}"
            continue
        if is_hyped(closes):
            chg = (closes[-1] / closes[0] - 1) * 100 if len(closes) >= 2 and closes[0] > 0 else float("nan")
            skipped[sym] = f"30 天漲跌 {chg:+.0f}%"
            continue
        picked.append(sym)
    return picked, skipped


def merge_with_held(top: list[str], held: list[str]) -> tuple[list[str], list[str]]:
    """持倉中但掉出前 N 名的標的仍要納入，否則停損／停利沒人管。回傳 (合併清單, 額外納入的)"""
    extra = [h for h in held if h not in top]
    return top + extra, extra
