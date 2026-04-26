"""即時行情 WebSocket 串流"""

import asyncio
from collections import defaultdict
from typing import Callable

from binance import AsyncClient, BinanceSocketManager

from src.utils.logger import setup_logger

logger = setup_logger("websocket_feed")


class WebSocketFeed:
    """管理多個 WebSocket 訂閱"""

    def __init__(self, client: AsyncClient):
        self.client = client
        self.bsm: BinanceSocketManager | None = None
        self._tasks: list[asyncio.Task] = []
        self._callbacks: dict[str, list[Callable]] = defaultdict(list)

    async def start(self):
        self.bsm = BinanceSocketManager(self.client)
        logger.info("WebSocket Manager 已啟動")

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        logger.info("WebSocket 已停止")

    def on_kline(self, callback: Callable):
        self._callbacks["kline"].append(callback)

    def on_depth(self, callback: Callable):
        self._callbacks["depth"].append(callback)

    def on_trade(self, callback: Callable):
        self._callbacks["trade"].append(callback)

    def subscribe_kline(self, symbol: str, interval: str):
        task = asyncio.create_task(self._kline_loop(symbol, interval))
        self._tasks.append(task)
        logger.info("訂閱 K 線: %s %s", symbol, interval)

    def subscribe_depth(self, symbol: str):
        task = asyncio.create_task(self._depth_loop(symbol))
        self._tasks.append(task)
        logger.info("訂閱深度: %s", symbol)

    async def _kline_loop(self, symbol: str, interval: str):
        socket = self.bsm.kline_futures_socket(symbol=symbol, interval=interval)
        async with socket as stream:
            while True:
                try:
                    msg = await stream.recv()
                    if not isinstance(msg, dict):
                        continue

                    # 處理可能的 wrapper 格式 {"stream": ..., "data": {...}}
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
                    for cb in self._callbacks["kline"]:
                        await cb(data) if asyncio.iscoroutinefunction(cb) else cb(data)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("K線串流錯誤: %s (msg=%s)", e, str(msg)[:200])
                    await asyncio.sleep(1)

    async def _depth_loop(self, symbol: str):
        socket = self.bsm.depth_socket(symbol=symbol, depth=BinanceSocketManager.WEBSOCKET_DEPTH_20)
        async with socket as stream:
            while True:
                try:
                    msg = await stream.recv()
                    if msg:
                        data = {
                            "symbol": symbol,
                            "bids": [(float(p), float(q)) for p, q in msg.get("bids", [])[:10]],
                            "asks": [(float(p), float(q)) for p, q in msg.get("asks", [])[:10]],
                        }
                        for cb in self._callbacks["depth"]:
                            await cb(data) if asyncio.iscoroutinefunction(cb) else cb(data)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("深度串流錯誤: %s", e)
                    await asyncio.sleep(1)
