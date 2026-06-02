"""即時行情 WebSocket 串流 — 含自動重連與失效偵測"""

import asyncio
import time
from collections import defaultdict
from typing import Callable

from binance import AsyncClient, BinanceSocketManager

from src.utils.logger import setup_logger

logger = setup_logger("websocket_feed")


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
            except Exception as e:
                logger.warning(
                    "K 線串流斷線 %s %s: %s，%ds 後重連",
                    symbol, interval, e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
