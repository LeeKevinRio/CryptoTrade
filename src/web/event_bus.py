"""簡易非同步事件匯流排 — 供 bot 與 web 介面解耦通訊"""

import asyncio
from collections import deque
from typing import Any


class EventBus:
    """
    支援多訂閱者的非同步事件匯流排。
    每個訂閱者持有獨立 queue；同時保留近期事件以便剛連線的 client 補拉。
    """

    def __init__(self, history_size: int = 200):
        self._subscribers: list[asyncio.Queue] = []
        self._history: deque = deque(maxlen=history_size)
        self._lock = asyncio.Lock()

    async def publish(self, event_type: str, data: dict):
        event = {"type": event_type, "data": data}
        async with self._lock:
            self._history.append(event)
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue):
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def recent(self, event_type: str | None = None, limit: int = 50) -> list[dict]:
        items = list(self._history)
        if event_type:
            items = [e for e in items if e["type"] == event_type]
        return items[-limit:]


bus = EventBus()
