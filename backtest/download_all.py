"""批次下載多標的長歷史 K 線

統計驗證需要的是「樣本外交易」而非「未來的交易」——只要歷史資料沒被
用來調過參數，它就是合法的樣本外。擴大歷史範圍可在數小時內取得
遠超過數月實盤累積的樣本數。

    python -m backtest.download_all --days 730
"""

import argparse
import asyncio
import time

from backtest.fetch_klines import get_klines_df

# 依 24h 成交額挑選的流動性標的
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT",
]


async def main_async(symbols, interval, days):
    ok, fail = [], []
    for i, sym in enumerate(symbols, 1):
        t0 = time.time()
        try:
            df = await get_klines_df(sym, interval, days, use_cache=True)
            span = (df.index[-1] - df.index[0]).days
            print(f"[{i}/{len(symbols)}] {sym:<10} {len(df):>7,} 根  "
                  f"{span:>4} 天  {df.index[0]:%Y-%m-%d}~{df.index[-1]:%Y-%m-%d}  "
                  f"({time.time()-t0:.0f}s)", flush=True)
            ok.append(sym)
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(symbols)}] {sym:<10} 失敗: {e}", flush=True)
            fail.append(sym)
    print(f"\n完成 {len(ok)}/{len(symbols)}" + (f"，失敗: {', '.join(fail)}" if fail else ""))
    return ok


def main():
    ap = argparse.ArgumentParser(description="批次下載長歷史 K 線")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=730)
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    asyncio.run(main_async(syms, args.interval, args.days))


if __name__ == "__main__":
    main()
