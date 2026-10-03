"""即時行情 WebSocket 串流 — 含自動重連與失效偵測"""

import asyncio
import json
import os
import time
from collections import defaultdict
from typing import Callable

import websockets
from binance import AsyncClient, BinanceSocketManager

from src.utils.logger import setup_logger

logger = setup_logger("websocket_feed")

# 幣安已停用正式站合約的舊行情網址（wss://fstream.binance.com/ws/、/stream?streams=）：
# 連得上但不送任何資料。python-binance 1.0.19 內建的仍是舊網址，正式站合約改直連新的
# /market/ 路徑（2026-10-03 使用者實測：舊網址 0 則、/market/ws/ 正常）。測試網網址未變，沿用套件。
LIVE_FUTURES_WS_BASE = os.getenv("FUTURES_WS_URL", "wss://fstream.binance.com/market/ws/")
# K 線串流在有成交時每 1~2 秒推一次；這麼久沒資料 = 連線已靜默失效，主動重連
NO_DATA_TIMEOUT = 90


class _RawStream:
    """直連 wss 的單一串流（介面同 python-binance socket：async with + recv() → dict）"""

    def __init__(self, url: str):
        self.url = url
        self._ws = None

    async def __aenter__(self):
        self._ws = await websockets.connect(
            self.url, open_timeout=15, ping_interval=20, ping_timeout=20,
        )
        return self

    async def __aexit__(self, *exc):
        if self._ws is not None:
            await self._ws.close()

    async def recv(self) -> dict:
        return json.loads(await self._ws.recv())


class WebSocketFeed:
    """管理多個 WebSocket 訂閱

    Args:
        market: "futures" 或 "spot"，決定使用哪個 K 線 stream
    """

    def __init__(self, client: AsyncClient, market: str = "futures"):
        self.client = client
        self.market = market
        self.bsm: BinanceSocketManager | None = None
        self._tasks: list[asyncio.Task] = []
        self._callbacks: dict[str, list[Callable]] = defaultdict(list)
        # 最後收到 K 線的時間戳（用於監測失效）
        self.last_kline_at: float = 0.0
        self.reconnects: int = 0
        self.live_futures = market == "futures" and not getattr(client, "testnet", False)
        self.endpoint = LIVE_FUTURES_WS_BASE if self.live_futures else "python-binance 內建"

    async def start(self):
        self.bsm = BinanceSocketManager(self.client)
        logger.info("WebSocket Manager 已啟動 (market=%s)", self.market)

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        logger.info("WebSocket 已停止")

    def on_kline(self, callback: Callable):
        self._callbacks["kline"].append(callback)

    def subscribe_kline(self, symbol: str, interval: str):
        task = asyncio.create_task(self._kline_loop(symbol, interval))
        self._tasks.append(task)
        logger.info("訂閱 K 線: %s %s (%s)", symbol, interval, self.market)

    def is_stale(self, max_age_seconds: int = 60) -> bool:
        """K 線過期？避免在斷線下舊資料風控"""
        if self.last_kline_at == 0:
            return False  # 尚未有資料，視為剛啟動
        return time.time() - self.last_kline_at > max_age_seconds

    def _make_socket(self, symbol: str, interval: str):
        if self.market == "spot":
            return self.bsm.kline_socket(symbol=symbol, interval=interval)
        if self.live_futures:
            return _RawStream(f"{LIVE_FUTURES_WS_BASE}{symbol.lower()}@kline_{interval}")
        return self.bsm.kline_futures_socket(symbol=symbol, interval=interval)

    async def _kline_loop(self, symbol: str, interval: str):
        """單一訂閱迴圈，含外層自動重連"""
        backoff = 1
        while True:
            try:
                socket = self._make_socket(symbol, interval)
                async with socket as stream:
                    backoff = 1  # 重連成功，重置 backoff
                    while True:
                        # asyncio.timeout 而非 wait_for：3.11 的 wait_for 在內層剛好完成時
                        # 會吞掉取消訊號，stop() 時串流任務可能永遠關不掉
                        async with asyncio.timeout(NO_DATA_TIMEOUT):
                            msg = await stream.recv()
                        if not isinstance(msg, dict):
                            continue
                        if "data" in msg and isinstance(msg["data"], dict):
                            msg = msg["data"]
                        if "k" not in msg:
                            continue

                        kline = msg["k"]
                        if not isinstance(kline, dict):
                            continue

                        data = {
                            "symbol": kline.get("s", symbol),
                            "interval": kline.get("i", interval),
                            "open": float(kline["o"]),
                            "high": float(kline["h"]),
                            "low": float(kline["l"]),
                            "close": float(kline["c"]),
                            "volume": float(kline["v"]),
                            "is_closed": kline.get("x", False),
                            "timestamp": kline["t"],
                        }
                        self.last_kline_at = time.time()
                        for cb in self._callbacks["kline"]:
                            try:
                                if asyncio.iscoroutinefunction(cb):
                                    await cb(data)
                                else:
                                    cb(data)
                            except Exception as e:
                                logger.error("K 線回調失敗: %s", e)
            except asyncio.CancelledError:
                logger.info("K 線串流已取消: %s %s", symbol, interval)
                break
            except asyncio.TimeoutError:
                self.reconnects += 1
                logger.warning(
                    "K 線串流 %s %s 已 %ds 沒收到資料（連線靜默失效），%ds 後重連",
                    symbol, interval, NO_DATA_TIMEOUT, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                self.reconnects += 1
                logger.warning(
                    "K 線串流斷線 %s %s: %s，%ds 後重連",
                    symbol, interval, e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
