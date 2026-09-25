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
    # 引擎生命週期狀態 — main 的重試迴圈寫入，/api/diag 讀取
    # {"phase": "starting|running|retrying|dead", "error": str, "ts": str, "attempts": int}
    engine_status: dict[str, Any] = field(default_factory=dict)
    # 引擎的 BinanceAPI 參照，供 /api/diag 現場探測
    api_ref: Any = None
    # 進場閘門統計 —— 回答「為什麼最近都沒有交易」：
    # {"evaluated": n, "opened": n, "blocked": {原因: 次數}, "last": {...}}
    trade_gate: dict[str, Any] = field(default_factory=lambda: {
        "evaluated": 0, "actionable": 0, "opened": 0, "blocked": {}, "last": None,
        # 每個標的最近一次評估的多／空原始強度，讓「未達門檻」看得出差多少
        "by_symbol": {},
    })
    # 風控迴圈心跳 —— 回答「停損／停利到底有沒有在檢查」：
    # {"ts": 最近一次檢查時間, "checked": 累計檢查次數, "no_price": {symbol: 次數}}
    risk_heartbeat: dict[str, Any] = field(default_factory=lambda: {
        "ts": None, "checked": 0, "no_price": {},
    })


def note_gate(symbol: str, reason: str | None, *, evaluated: bool = False,
              actionable: bool = False, opened: bool = False,
              detail: dict | None = None):
    """記錄一次進場決策。reason 為 None 表示通過（實際開倉）。

    detail：該次評估的補充資訊（多／空原始強度等），按標的保留最近一筆。
    """
    from datetime import datetime, timezone
    g = state.trade_gate
    if detail is not None:
        g.setdefault("by_symbol", {})[symbol] = {
            **detail,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    if evaluated:
        g["evaluated"] += 1
    if actionable:
        g["actionable"] += 1
    if opened:
        g["opened"] += 1
    if reason:
        g["blocked"][reason] = g["blocked"].get(reason, 0) + 1
        g["last"] = {
            "symbol": symbol, "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


state = GlobalState()
