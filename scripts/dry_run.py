"""Dry-run 檢查腳本 — 驗證 Testnet 連線、行情、訊號評估，但不下任何單。

用法:
    python -m scripts.dry_run                 # 預設跑 30 秒 WebSocket
    python -m scripts.dry_run --ws-seconds 0  # 跳過 WebSocket 測試
"""

import argparse
import asyncio
import sys

from src.data.binance_client import BinanceAPI
from src.data.candle_manager import CandleManager
from src.data.websocket_feed import WebSocketFeed
from src.strategy.signal_aggregator import SignalAggregator
from src.utils.config import load_config
from src.utils.logger import setup_logger

logger = setup_logger("dry_run")

PLACEHOLDERS = {"", "your_api_key_here", "your_api_secret_here"}


def _section(title: str):
    print("\n" + "=" * 56)
    print(f"  {title}")
    print("=" * 56)


def check_env(config: dict) -> bool:
    _section("1. 環境變數檢查")
    key = config["binance"]["api_key"]
    secret = config["binance"]["api_secret"]
    testnet = config["binance"]["testnet"]

    ok = True
    if key in PLACEHOLDERS or secret in PLACEHOLDERS:
        print("  [X] BINANCE_API_KEY / BINANCE_API_SECRET 未設定（仍為範本值）")
        print("      → 到 https://testnet.binancefuture.com 申請後填入 .env")
        ok = False
    else:
        print(f"  [OK] API key 已設定（前 6 碼: {key[:6]}...）")

    print(f"  [OK] testnet 模式: {testnet}")
    if not testnet:
        print("  [!] 警告: testnet=False 表示會打到正式環境！")

    return ok


async def check_connection(api: BinanceAPI) -> bool:
    _section("2. Binance API 連線")
    try:
        await api.connect()
        balance = await api.get_usdt_balance()
        print(f"  [OK] 連線成功，USDT 餘額: {balance:.2f}")
        if balance == 0:
            print("  [!] 餘額為 0 — 到 Testnet 領水後重試（每次 10000 USDT）")
        return True
    except Exception as e:
        print(f"  [X] 連線失敗: {e}")
        return False


async def check_market_data(api: BinanceAPI, symbols: list[str], timeframes: list[str]) -> dict:
    _section("3. 行情資料抓取")
    candle_mgr = CandleManager()
    ok_count = 0
    total = len(symbols) * len(timeframes)

    for symbol in symbols:
        for tf in timeframes:
            try:
                klines = await api.get_klines(symbol, tf, limit=500)
                candle_mgr.init_from_klines(symbol, tf, klines)
                df = candle_mgr.get_candles(symbol, tf)
                last_close = df["close"].iloc[-1] if df is not None else None
                print(f"  [OK] {symbol} {tf:>3}: {len(klines)} 根, 最新收盤 {last_close}")
                ok_count += 1
            except Exception as e:
                print(f"  [X] {symbol} {tf}: {e}")

    print(f"\n  共 {ok_count}/{total} 組 K 線載入成功")
    return {"candle_mgr": candle_mgr, "ok": ok_count == total}


async def check_signal_evaluation(
    api: BinanceAPI,
    candle_mgr: CandleManager,
    aggregator: SignalAggregator,
    symbols: list[str],
    timeframes: list[str],
):
    _section("4. 訊號評估（不下單）")
    for symbol in symbols:
        candles = {}
        for tf in timeframes:
            df = candle_mgr.get_candles(symbol, tf)
            if df is not None:
                candles[tf] = df

        try:
            funding = await api.get_funding_rate(symbol)
        except Exception:
            funding = 0.0

        signal = aggregator.evaluate(symbol, candles, funding)
        actionable = "✓ 會進場" if signal.is_actionable else "  不動作"
        print(
            f"  {symbol}: {signal.type.value:<7} 強度={signal.strength:>5.1f}  "
            f"funding={funding:+.4%}  [{actionable}]"
        )
        if signal.reasons:
            for r in signal.reasons[:3]:
                print(f"      · {r}")


async def check_websocket(api: BinanceAPI, symbols: list[str], seconds: int):
    if seconds <= 0:
        print("\n  (略過 WebSocket 測試)")
        return

    _section(f"5. WebSocket 即時串流（{seconds} 秒）")
    received = {"count": 0, "last_symbol": None}

    async def on_kline(data: dict):
        received["count"] += 1
        received["last_symbol"] = data["symbol"]

    ws = WebSocketFeed(api.client)
    await ws.start()
    ws.on_kline(on_kline)

    for symbol in symbols:
        ws.subscribe_kline(symbol, "1m")

    print(f"  訂閱 {len(symbols)} 個交易對的 1m K 線，等待 {seconds} 秒...")
    await asyncio.sleep(seconds)
    await ws.stop()

    if received["count"] > 0:
        print(f"  [OK] 共收到 {received['count']} 則訊息（最新: {received['last_symbol']}）")
    else:
        print("  [X] 完全沒收到任何訊息 — 檢查網路或 testnet WS 端點")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws-seconds", type=int, default=30, help="WebSocket 測試秒數，0 跳過")
    args = parser.parse_args()

    config = load_config()

    print("\n" + "#" * 56)
    print("#         CryptoTrade Dry-Run 檢查（不會下單）         #")
    print("#" * 56)

    if not check_env(config):
        print("\n結論: 環境設定未完成，先填好 .env 再跑。")
        sys.exit(1)

    api = BinanceAPI(
        api_key=config["binance"]["api_key"],
        api_secret=config["binance"]["api_secret"],
        testnet=config["binance"]["testnet"],
    )

    if not await check_connection(api):
        print("\n結論: API 無法連線，停止後續檢查。")
        sys.exit(1)

    symbols = config.get("trading", {}).get("symbols", ["BTCUSDT"])
    timeframes = config.get("trading", {}).get("timeframes", ["5m", "15m", "1h"])

    try:
        market = await check_market_data(api, symbols, timeframes)
        if market["ok"]:
            aggregator = SignalAggregator(config)
            await check_signal_evaluation(
                api, market["candle_mgr"], aggregator, symbols, timeframes
            )

        await check_websocket(api, symbols, args.ws_seconds)

    finally:
        await api.disconnect()

    _section("結論")
    print("  Dry-run 完成。若以上全部 [OK]，即可改用 `python -m src.main` 跑實盤。")
    print("  注意：本腳本不會送出任何訂單。")


if __name__ == "__main__":
    asyncio.run(main())
