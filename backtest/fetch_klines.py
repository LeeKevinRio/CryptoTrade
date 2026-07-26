"""真實歷史 K 線下載器 — Binance 公開端點（免 API key）+ 本地快取

用途：讓回測/參數掃描跑在真實市場資料上，而非 seed=42 的模擬資料。

    python -m backtest.fetch_klines --symbol BTCUSDT --interval 5m --days 90

資料來源優先序：
    1. fapi.binance.com  （USDT-M 合約，與 bot 實際交易的市場一致）
    2. api.binance.com   （現貨，合約端點不可用時退回）
    3. data-api.binance.vision（公開鏡像）

快取：data/klines/{market}_{symbol}_{interval}.csv，重複執行只補抓缺少的區間。
"""

import argparse
import asyncio
import time
from pathlib import Path

import aiohttp
import pandas as pd

from src.utils.logger import setup_logger

logger = setup_logger("fetch_klines")

CACHE_DIR = Path("data/klines")

ENDPOINTS = [
    ("futures", "https://fapi.binance.com/fapi/v1/klines"),
    ("spot", "https://api.binance.com/api/v3/klines"),
    ("mirror", "https://data-api.binance.vision/api/v3/klines"),
]

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


async def _fetch_page(session, url, symbol, interval, start_ms, limit=1000):
    params = {"symbol": symbol, "interval": interval,
              "startTime": str(start_ms), "limit": str(limit)}
    async with session.get(
        url, params=params, timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from {url}")
        return await resp.json(content_type=None)


async def download(symbol: str, interval: str, days: int) -> tuple[pd.DataFrame, str]:
    """分頁下載指定天數的真實 K 線。"""
    step = INTERVAL_MS.get(interval)
    if not step:
        raise ValueError(f"不支援的 interval: {interval}")

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    rows: list[list] = []

    async with aiohttp.ClientSession(
        headers={"User-Agent": "CryptoTrade/1.0"},
    ) as session:
        market, url = None, None
        # 挑一個可用端點
        for name, candidate in ENDPOINTS:
            try:
                await _fetch_page(session, candidate, symbol, interval, start_ms, 1)
                market, url = name, candidate
                logger.info("使用資料源: %s (%s)", name, candidate)
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("端點 %s 不可用: %s", name, e)
        if url is None:
            raise RuntimeError("所有 Binance 端點都不可用")

        cursor = start_ms
        while cursor < end_ms:
            try:
                page = await _fetch_page(session, url, symbol, interval, cursor)
            except Exception as e:  # noqa: BLE001
                logger.warning("抓取失敗，重試一次: %s", e)
                await asyncio.sleep(1.0)
                page = await _fetch_page(session, url, symbol, interval, cursor)
            if not page:
                break
            rows.extend(page)
            last_open = int(page[-1][0])
            cursor = last_open + step
            if len(page) < 1000:
                break
            await asyncio.sleep(0.12)   # 尊重 rate limit

    if not rows:
        raise RuntimeError("沒有取得任何 K 線")

    df = pd.DataFrame(
        [{
            "timestamp": pd.to_datetime(int(r[0]), unit="ms"),
            "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
            "close": float(r[4]), "volume": float(r[5]),
        } for r in rows]
    )
    df = df.drop_duplicates(subset="timestamp").set_index("timestamp").sort_index()
    return df, market


def cache_path(symbol: str, interval: str, market: str = "futures") -> Path:
    return CACHE_DIR / f"{market}_{symbol}_{interval}.csv"


def load_cached(symbol: str, interval: str, market: str = "futures") -> pd.DataFrame | None:
    p = cache_path(symbol, interval, market)
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["timestamp"]).set_index("timestamp").sort_index()
    return df if len(df) else None


async def get_klines_df(symbol: str, interval: str, days: int,
                        use_cache: bool = True) -> pd.DataFrame:
    """取得真實 K 線 DataFrame（優先讀快取，不足才下載）。"""
    if use_cache:
        for market, _ in ENDPOINTS:
            cached = load_cached(symbol, interval, market)
            if cached is not None:
                span_days = (cached.index[-1] - cached.index[0]).total_seconds() / 86400
                if span_days >= days * 0.95:
                    logger.info("使用快取 %s（%d 根，%.1f 天）",
                                cache_path(symbol, interval, market), len(cached), span_days)
                    return cached
    df, market = await download(symbol, interval, days)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = cache_path(symbol, interval, market)
    df.to_csv(out)
    logger.info("已存檔 %s（%d 根）", out, len(df))
    return df


def main():
    ap = argparse.ArgumentParser(description="下載真實歷史 K 線")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    df = asyncio.run(get_klines_df(args.symbol, args.interval, args.days,
                                   use_cache=not args.no_cache))
    print(f"{args.symbol} {args.interval}: {len(df)} 根")
    print(f"  區間: {df.index[0]} ~ {df.index[-1]}")
    print(f"  起始價: {df['close'].iloc[0]:,.2f}  結束價: {df['close'].iloc[-1]:,.2f}")
    change = (df['close'].iloc[-1] - df['close'].iloc[0]) / df['close'].iloc[0] * 100
    print(f"  期間變動: {change:+.2f}%")


if __name__ == "__main__":
    main()
