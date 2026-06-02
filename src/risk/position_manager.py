"""倉位管理 — 整合停利、停損、資金管理"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from src.risk.take_profit import TakeProfitManager
from src.risk.stop_loss import StopLossManager
from src.risk.capital_manager import CapitalManager
from src.utils.logger import setup_logger

logger = setup_logger("position_manager")


@dataclass
class Position:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    leverage: int = 1
    trade_id: int | None = None       # 對應 TradeRecord.id，用於精確 record_close
    batch_orders: list[dict] = field(default_factory=list)
    filled_batches: int = 0


class PositionManager:
    """統一管理所有持倉與風控"""

    def __init__(self, config: dict):
        self.config = config
        self.tp_manager = TakeProfitManager(config)
        self.sl_manager = StopLossManager(config)
        self.capital_manager = CapitalManager(config)
        self._positions: dict[str, Position] = {}
        # 每個 symbol 一把 asyncio.Lock，僅在事件迴圈中按需建立避免錯誤綁定
        self._symbol_locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, symbol: str) -> asyncio.Lock:
        lock = self._symbol_locks.get(symbol)
        if lock is None:
            # 在當前 running loop 中建立，避免 defaultdict 在錯誤的 loop 上產生 lock
            lock = asyncio.Lock()
            self._symbol_locks[symbol] = lock
        return lock

    def open_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        leverage: int = 1,
        atr: float | None = None,
        trade_id: int | None = None,
    ) -> Position:
        pos = Position(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            leverage=leverage,
            trade_id=trade_id,
        )
        self._positions[symbol] = pos
        self.capital_manager.add_position()

        # 註冊停利
        self.tp_manager.register_position(symbol, side, entry_price, quantity)

        # 註冊停損
        self.sl_manager.set_stop(symbol, side, entry_price, atr)

        logger.info(
            "開倉: %s %s price=%.2f qty=%.4f lev=%dx",
            symbol, side, entry_price, quantity, leverage,
        )
        return pos

    def close_position(self, symbol: str, exit_price: float) -> dict | None:
        pos = self._positions.pop(symbol, None)
        if not pos:
            return None

        if pos.side == "LONG":
            pnl = (exit_price - pos.entry_price) * pos.quantity
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
        else:
            pnl = (pos.entry_price - exit_price) * pos.quantity
            pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100

        self.tp_manager.remove_position(symbol)
        self.sl_manager.remove_stop(symbol)
        self.capital_manager.remove_position()
        self.capital_manager.record_trade(pnl)

        result = {
            "symbol": symbol,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "quantity": pos.quantity,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "trade_id": pos.trade_id,
        }
        logger.info(
            "平倉: %s %s pnl=%.4f (%.2f%%)",
            symbol, pos.side, pnl, pnl_pct,
        )
        return result

    def check_risk(self, symbol: str, current_price: float) -> list[dict]:
        """檢查指定持倉的所有風控條件，回傳需要執行的動作"""
        actions = []

        # 檢查停損
        sl_action = self.sl_manager.check(symbol, current_price)
        if sl_action:
            pos = self._positions.get(symbol)
            if pos:
                sl_action["quantity"] = pos.quantity
            actions.append(sl_action)
            return actions  # 停損優先，立即返回

        # 檢查停利
        tp_actions = self.tp_manager.check(symbol, current_price)
        actions.extend(tp_actions)

        return actions

    def get_position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    @property
    def all_positions(self) -> dict[str, Position]:
        return self._positions.copy()
