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
    realized_pnl: float = 0.0         # 部分平倉已實現損益（最終平倉時併入總 pnl）


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

    def close_position(self, symbol: str, exit_price: float,
                       quantity: float | None = None) -> dict | None:
        """平倉。quantity=None 或 >= 持倉量 → 全平；否則部分平倉。

        部分平倉：倉位保留、風控（停損/停利狀態）續存，已實現損益累積在
        Position.realized_pnl，待最終全平時併入總 pnl 一次記錄 —— 修正舊版
        「階梯停利平 40% 卻移除整個本地倉，剩餘 60% 無停損裸奔」的缺陷。
        """
        pos = self._positions.get(symbol)
        if not pos:
            return None

        full_close = quantity is None or quantity >= pos.quantity - 1e-12
        close_qty = pos.quantity if full_close else quantity

        if pos.side == "LONG":
            leg_pnl = (exit_price - pos.entry_price) * close_qty
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
        else:
            leg_pnl = (pos.entry_price - exit_price) * close_qty
            pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100

        if not full_close:
            pos.quantity -= close_qty
            pos.realized_pnl += leg_pnl
            self.capital_manager.record_trade(leg_pnl)
            result = {
                "symbol": symbol,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "quantity": close_qty,
                "pnl": round(leg_pnl, 4),
                "pnl_pct": round(pnl_pct, 2),
                "trade_id": pos.trade_id,
                "partial": True,
                "remaining_quantity": pos.quantity,
            }
            logger.info(
                "部分平倉: %s %s qty=%.4f pnl=%.4f 剩餘=%.4f",
                symbol, pos.side, close_qty, leg_pnl, pos.quantity,
            )
            return result

        # ── 全平 ──
        self._positions.pop(symbol, None)
        total_pnl = pos.realized_pnl + leg_pnl

        self.tp_manager.remove_position(symbol)
        self.sl_manager.remove_stop(symbol)
        self.capital_manager.remove_position()
        self.capital_manager.record_trade(leg_pnl)

        result = {
            "symbol": symbol,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "quantity": close_qty,
            "pnl": round(total_pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "trade_id": pos.trade_id,
        }
        logger.info(
            "平倉: %s %s pnl=%.4f (%.2f%%)",
            symbol, pos.side, total_pnl, pnl_pct,
        )
        return result

    def sync_external_fill(self, symbol: str, exchange_qty: float,
                           est_price: float) -> dict | None:
        """對帳同步：交易所實際數量與本地不一致時校正。

        - 交易所數量變少 → 掛單（maker 停利階梯）部分成交，按差額入帳部分平倉
        - 交易所數量變多 → 批次進場限價單成交，擴增本地數量
        回傳部分平倉 result（供事件廣播），無事發生回 None。
        """
        pos = self._positions.get(symbol)
        if not pos:
            return None
        diff = pos.quantity - exchange_qty
        if abs(diff) < max(pos.quantity, exchange_qty) * 1e-6:
            return None

        if diff > 0:
            # 部分被平（TP 掛單成交）— 用估價入帳，同步停利狀態
            result = self.close_position(symbol, est_price, quantity=diff)
            self.tp_manager.sync_remaining(symbol, exchange_qty)
            return result

        # 數量變多：批次限價進場成交
        pos.quantity = exchange_qty
        self.tp_manager.sync_remaining(symbol, exchange_qty, grew=True)
        logger.info("對帳擴增 %s 數量 → %.4f（批次進場成交）", symbol, exchange_qty)
        return None

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
