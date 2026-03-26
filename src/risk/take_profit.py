"""快速停利機制 — 核心模組

三種停利策略：
1. 階梯式停利：分批平倉鎖定利潤
2. 移動停利：獲利後追蹤，回撤即出場
3. 時間停利：超時未達目標則平倉
"""

import time
from dataclasses import dataclass, field

from src.utils.logger import setup_logger

logger = setup_logger("take_profit")


@dataclass
class TakeProfitLevel:
    pct: float         # 獲利百分比觸發點
    close_pct: float   # 平倉比例
    triggered: bool = False


@dataclass
class TakeProfitState:
    """單一持倉的停利狀態"""
    symbol: str
    side: str                            # LONG or SHORT
    entry_price: float
    total_quantity: float
    remaining_quantity: float
    entry_time: float = field(default_factory=time.time)

    # 階梯式停利
    levels: list[TakeProfitLevel] = field(default_factory=list)

    # 移動停利
    trailing_active: bool = False
    highest_pnl_pct: float = 0.0        # 持倉期間最高獲利%
    trailing_activation_pct: float = 1.5
    trailing_callback_pct: float = 0.5

    # 時間停利
    time_stop_seconds: int = 1800        # 30 分鐘


class TakeProfitManager:
    """管理所有持倉的停利邏輯"""

    def __init__(self, config: dict):
        tp_cfg = config.get("risk", {}).get("take_profit", {})
        trail_cfg = config.get("risk", {}).get("trailing_stop", {})

        self.levels_config = [
            {"pct": tp_cfg.get("level_1_pct", 1.0), "close_pct": tp_cfg.get("level_1_close_pct", 40)},
            {"pct": tp_cfg.get("level_2_pct", 2.0), "close_pct": tp_cfg.get("level_2_close_pct", 30)},
            {"pct": tp_cfg.get("level_3_pct", 3.5), "close_pct": tp_cfg.get("level_3_close_pct", 30)},
        ]
        self.trailing_activation = trail_cfg.get("activation_pct", 1.5)
        self.trailing_callback = trail_cfg.get("callback_pct", 0.5)
        self.time_stop = config.get("risk", {}).get("time_stop_seconds", 1800)

        # {symbol: TakeProfitState}
        self._states: dict[str, TakeProfitState] = {}

    def register_position(self, symbol: str, side: str, entry_price: float, quantity: float):
        levels = [
            TakeProfitLevel(pct=lv["pct"], close_pct=lv["close_pct"])
            for lv in self.levels_config
        ]
        state = TakeProfitState(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            total_quantity=quantity,
            remaining_quantity=quantity,
            levels=levels,
            trailing_activation_pct=self.trailing_activation,
            trailing_callback_pct=self.trailing_callback,
            time_stop_seconds=self.time_stop,
        )
        self._states[symbol] = state
        logger.info(
            "註冊停利: %s %s entry=%.2f qty=%.4f",
            symbol, side, entry_price, quantity,
        )

    def remove_position(self, symbol: str):
        self._states.pop(symbol, None)

    def check(self, symbol: str, current_price: float) -> list[dict]:
        """
        檢查是否觸發停利條件

        Returns:
            list of {"action": "close", "quantity": float, "reason": str}
        """
        state = self._states.get(symbol)
        if not state or state.remaining_quantity <= 0:
            return []

        pnl_pct = self._calc_pnl_pct(state, current_price)
        actions = []

        # ── 1. 階梯式停利 ──
        for level in state.levels:
            if not level.triggered and pnl_pct >= level.pct:
                close_qty = state.total_quantity * (level.close_pct / 100)
                close_qty = min(close_qty, state.remaining_quantity)
                if close_qty > 0:
                    level.triggered = True
                    state.remaining_quantity -= close_qty
                    actions.append({
                        "action": "close",
                        "quantity": close_qty,
                        "reason": f"階梯停利 L{state.levels.index(level)+1}: +{pnl_pct:.2f}% (目標 +{level.pct}%)",
                    })
                    logger.info(
                        "🎯 %s 階梯停利觸發 +%.2f%% 平倉 %.4f",
                        symbol, pnl_pct, close_qty,
                    )

        # ── 2. 移動停利 ──
        if pnl_pct > state.highest_pnl_pct:
            state.highest_pnl_pct = pnl_pct

        if not state.trailing_active and pnl_pct >= state.trailing_activation_pct:
            state.trailing_active = True
            logger.info("🔄 %s 移動停利啟動 (當前 +%.2f%%)", symbol, pnl_pct)

        if state.trailing_active and state.remaining_quantity > 0:
            drawdown = state.highest_pnl_pct - pnl_pct
            if drawdown >= state.trailing_callback_pct:
                close_qty = state.remaining_quantity
                state.remaining_quantity = 0
                actions.append({
                    "action": "close",
                    "quantity": close_qty,
                    "reason": f"移動停利: 從高點 +{state.highest_pnl_pct:.2f}% 回撤 {drawdown:.2f}%",
                })
                logger.info(
                    "🔄 %s 移動停利觸發 回撤 %.2f%% 全部平倉",
                    symbol, drawdown,
                )

        # ── 3. 時間停利 ──
        if state.remaining_quantity > 0:
            elapsed = time.time() - state.entry_time
            if elapsed > state.time_stop_seconds and pnl_pct > 0:
                close_qty = state.remaining_quantity
                state.remaining_quantity = 0
                actions.append({
                    "action": "close",
                    "quantity": close_qty,
                    "reason": f"時間停利: 持倉 {elapsed/60:.0f} 分鐘，獲利 +{pnl_pct:.2f}%",
                })
                logger.info("⏰ %s 時間停利 %.0f 分鐘", symbol, elapsed / 60)

        return actions

    def _calc_pnl_pct(self, state: TakeProfitState, current_price: float) -> float:
        if state.side == "LONG":
            return (current_price - state.entry_price) / state.entry_price * 100
        else:
            return (state.entry_price - current_price) / state.entry_price * 100
