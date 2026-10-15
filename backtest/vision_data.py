"""幣安官方公開歷史檔（data.binance.vision）—— U 本位合約日 K 線與資金費率

REST API 單次最多 1000 筆、資金費率歷史要逐幣翻頁，且部分機房會被地區限制；
官方 S3 月檔沒有這些問題，一個月一個 zip，並行下載數十個幣 × 3 年只要數分鐘。

    from backtest.vision_data import daily_panel
    closes, funding, quote_volume = daily_panel(["BTCUSDT", "ETHUSDT"], months=36)
"""

import asyncio
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pandas as pd

BASE = "https://data.binance.vision/data/futures/um/monthly"
CACHE = Path("data/vision")


def month_list(n: int, until: datetime | None = None) -> list[tuple[int, int]]:
    """最近 n 個「已結束」的月份（當月月檔尚未產出）"""
    now = until or datetime.now(timezone.utc)
    y, m = now.year, now.month
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append((y, m))
    return out[::-1]


def _url(kind: str, sym: str, y: int, m: int) -> str:
    if kind == "klines":
        return f"{BASE}/klines/{sym}/1d/{sym}-1d-{y}-{m:02d}.zip"
    return f"{BASE}/fundingRate/{sym}/{sym}-fundingRate-{y}-{m:02d}.zip"


def _read_csv(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode()
    first = text.split("\n", 1)[0].split(",")[0].strip()
    has_header = not first.replace(".", "").isdigit()
    return pd.read_csv(io.StringIO(text), header=0 if has_header else None)


def _to_ts(v: pd.Series) -> pd.Series:
    v = pd.to_numeric(v)
    unit = "us" if v.max() > 1e14 else "ms"       # 2025 起部分檔案改微秒
    return pd.to_datetime(v, unit=unit, utc=True)


def parse_klines(raw: bytes) -> pd.DataFrame:
    df = _read_csv(raw)
    df = df.iloc[:, :8]
    df.columns = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume"]
    df["date"] = _to_ts(df["open_time"]).dt.normalize()
    return df.set_index("date")[["open", "high", "low", "close", "volume", "quote_volume"]].astype(float)


def parse_funding(raw: bytes) -> pd.DataFrame:
    df = _read_csv(raw)
    if "calc_time" in df.columns:
        t, r = df["calc_time"], df["last_funding_rate"]
    else:
        t, r = df.iloc[:, 0], df.iloc[:, -1]
    return pd.DataFrame({"time": _to_ts(t), "rate": pd.to_numeric(r)})


async def _fetch(session, sem, kind, sym, y, m) -> bytes | None:
    path = CACHE / kind / f"{sym}-{y}-{m:02d}.zip"
    if path.exists():
        return path.read_bytes()
    async with sem:
        for attempt in range(3):
            try:
                async with session.get(_url(kind, sym, y, m),
                                       timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 404:
                        return None              # 該月尚未上市或已下市
                    r.raise_for_status()
                    raw = await r.read()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                return raw
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(1 + attempt * 2)
    return None


async def _download(symbols, months, kind, concurrency=24) -> dict[str, list[bytes]]:
    sem = asyncio.Semaphore(concurrency)
    async with aiohttp.ClientSession(headers={"User-Agent": "CryptoTrade/1.0"}) as s:
        jobs = {(sym, y, m): _fetch(s, sem, kind, sym, y, m)
                for sym in symbols for (y, m) in months}
        res = await asyncio.gather(*jobs.values())
    out: dict[str, list[bytes]] = {s: [] for s in symbols}
    for (sym, _, _), raw in zip(jobs, res):
        if raw:
            out[sym].append(raw)
    return out


def daily_panel(symbols: list[str], months: int = 36):
    """回傳 (收盤價, 每日資金費率合計, 每日成交額 USDT)，皆為 日期 × 標的；缺資料為 NaN。

    每日資金費率合計 = 當日（UTC）所有結算的費率加總；正值代表多單付給空單。
    """
    ms = month_list(months)
    kl = asyncio.run(_download(symbols, ms, "klines"))
    fr = asyncio.run(_download(symbols, ms, "funding"))
    closes, funding, qvol = {}, {}, {}
    for sym in symbols:
        if kl[sym]:
            k = pd.concat([parse_klines(r) for r in kl[sym]]).sort_index()
            k = k[~k.index.duplicated()]
            closes[sym] = k["close"]
            qvol[sym] = k["quote_volume"]
        if fr[sym]:
            f = pd.concat([parse_funding(r) for r in fr[sym]])
            funding[sym] = f.groupby(f["time"].dt.normalize())["rate"].sum()
    c = pd.DataFrame(closes).sort_index()
    f = pd.DataFrame(funding).reindex(c.index).reindex(columns=c.columns)
    v = pd.DataFrame(qvol).reindex(c.index).reindex(columns=c.columns)
    return c, f, v
