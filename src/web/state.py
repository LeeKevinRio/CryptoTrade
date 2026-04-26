"""共享執行期狀態 — bot 寫入、web 讀取"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BotState:
    started_at: str = ""
    testnet: bool = True
    symbols: list[str] = field(default_factory=list)
    timeframes: list[str] = field(default_factory=list)
    balance: float = 0.0
    paused: bool = False
    last_prices: dict[str, float] = field(default_factory=dict)
    last_signals: dict[str, dict] = field(default_factory=dict)

    bot_ref: Any = None  # 後端可呼叫 bot.executor.close_position 等


state = BotState()
