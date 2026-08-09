"""外部訊號暫存 — 依 symbol 保留最新一則，含 TTL"""

import threading

from src.external.models import ExternalSignal
from src.utils.logger import setup_logger

logger = setup_logger("external_signal")


class ExternalSignalStore:
    """執行緒安全的最新訊號暫存。

    webhook 由 FastAPI 事件迴圈寫入、交易迴圈讀取，故加鎖保護。
    每個 symbol 只保留最新一則：TradingView 通常會重複發送同方向 alert，
    保留最新的即可，過期由 ExternalSignal.expired 判定。
    """

    def __init__(self, max_history: int = 100):
        self._latest: dict[str, ExternalSignal] = {}
        self._history: list[ExternalSignal] = []
        self._max_history = max_history
        self._lock = threading.Lock()

    def put(self, signal: ExternalSignal):
        with self._lock:
            self._latest[signal.symbol] = signal
            self._history.append(signal)
            if len(self._history) > self._max_history:
                self._history.pop(0)
        logger.info(
            "收到外部訊號 %s %s 強度=%.0f (%s)",
            signal.symbol, signal.action, signal.strength, signal.source,
        )

    def get(self, symbol: str) -> ExternalSignal | None:
        """取得未過期的最新訊號；過期則回 None。"""
        with self._lock:
            sig = self._latest.get(symbol)
        if sig is None or sig.expired:
            return None
        return sig

    def get_raw(self, symbol: str) -> ExternalSignal | None:
        """取得最新訊號（不論是否過期），供 UI 顯示。"""
        with self._lock:
            return self._latest.get(symbol)

    def all_latest(self) -> dict[str, dict]:
        with self._lock:
            items = list(self._latest.items())
        return {sym: sig.to_dict() for sym, sig in items}

    def history(self, limit: int = 20) -> list[dict]:
        with self._lock:
            items = self._history[-limit:]
        return [s.to_dict() for s in reversed(items)]

    def clear(self, symbol: str | None = None):
        with self._lock:
            if symbol:
                self._latest.pop(symbol, None)
            else:
                self._latest.clear()


# 全域單例：webhook 與交易迴圈共用
store = ExternalSignalStore()
