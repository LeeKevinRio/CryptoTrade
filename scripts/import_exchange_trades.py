"""從幣安匯入成交紀錄重建績效 — 不管哪台引擎下的單、資料庫清過幾次都能補回

    python -m scripts.import_exchange_trades --days 30
    python -m scripts.import_exchange_trades --days 90 --symbols BTCUSDT,ETHUSDT

可重複執行（exchange_ref 去重）。bot 啟動時也會依 IMPORT_EXCHANGE_TRADES_DAYS
自動回補（預設 30，設 0 關閉）。
"""

import argparse
import asyncio

from src.data.binance_client import BinanceAPI
from src.execution.exchange_import import import_trades
from src.utils.config import load_config
from src.utils.models import init_db


async def main_async(days: int, symbols: list[str] | None):
    config = load_config()
    syms = symbols or config.get("trading", {}).get("symbols", [])
    api = BinanceAPI(
        api_key=config["binance"]["api_key"],
        api_secret=config["binance"]["api_secret"],
        testnet=config["binance"]["testnet"],
    )
    await api.connect()
    try:
        db_url = config.get("database", {}).get("url", "sqlite:///cryptotrade.db")
        session_factory = init_db(db_url.replace("+aiosqlite", ""))
        stats = await import_trades(api, session_factory, syms, days)
    finally:
        await api.disconnect()

    print(f"\n匯入 {days} 天：{stats['symbols']} 標的、{stats['fills']} 筆成交 → "
          f"{stats['trades']} 筆交易；新增 {stats['inserted']}、略過(已存在) {stats['skipped']}")
    for err in stats["errors"]:
        print(f"  ⚠️ {err}")
    print("儀表板績效頁現在應能看到這些交易（出場原因顯示為「交易所匯入」）。")


def main():
    ap = argparse.ArgumentParser(description="從幣安匯入成交紀錄")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", default=None, help="逗號分隔，預設用 settings.yaml 的清單")
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    asyncio.run(main_async(args.days, syms))


if __name__ == "__main__":
    main()
