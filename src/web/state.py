"""共享執行期狀態 — bot 寫入、web 讀取（多 bot）"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BotState:
    """單一 bot 的執行期狀態"""
    bot_id: str = ""
    mode: str = "futures"            # spot | futures
    enabled: bool = True
    leverage: int = 1
    started_at: str = ""
    balance: float = 0.0
    paused: bool = False
    last_signals: dict[str, dict] = field(default_factory=dict)
    bot_ref: Any = None


@dataclass
class GlobalState:
    """跨 bot 的共享資料（行情、總設定）"""
    testnet: bool = True
    symbols: list[str] = field(default_factory=list)
    timeframes: list[str] = field(default_factory=list)
    last_prices: dict[str, float] = field(default_factory=dict)
    last_funding: dict[str, float] = field(default_factory=dict)
    last_sentiment: dict[str, dict] = field(default_factory=dict)
    candle_manager_ref: Any = None
    ws_feed_ref: Any = None
    bots: dict[str, BotState] = field(default_factory=dict)
    # 由 reconcile_loop 維護的真實 Binance 持倉資訊（含實際爆倉價）
    exchange_positions: dict[str, dict] = field(default_factory=dict)


state = GlobalState()
