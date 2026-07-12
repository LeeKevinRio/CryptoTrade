"""資金管理 — 倉位大小計算與風控限制"""

from datetime import datetime, timezone

from src.utils.logger import setup_logger


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

logger = setup_logger("capital_manager")


class CapitalManager:
    """資金分配與風控管理"""

    def __init__(self, config: dict):
        risk_cfg = config.get("risk", {})
        self.max_position_pct = risk_cfg.get("max_position_pct", 10)
        self.max_concurrent = risk_cfg.get("max_concurrent_positions", 3)
        self.max_daily_loss_pct = risk_cfg.get("max_daily_loss_pct", 3.0)
        self.max_daily_trades = risk_cfg.get("max_daily_trades", 20)
        # 連續虧損熔斷：當日連敗達此筆數即停止開新倉（0 = 停用）。
        # 針對「連敗級聯」設計：每日虧損%熔斷以餘額為門檻，擋不住一連串各自在門檻內的虧損；
        # 連敗計數能在資金大幅流失前就先剎車。
        self.max_consecutive_losses = risk_cfg.get("max_consecutive_losses", 0)
        self.entry_batches = risk_cfg.get("entry_batches", [
            {"pct": 30, "offset": 0.0},
            {"pct": 30, "offset": -1.0},
            {"pct": 40, "offset": -2.0},
        ])

        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._consecutive_losses: int = 0
        self._current_date: str = _utc_today()
        self._open_positions: int = 0

    def reset_daily(self):
        today = _utc_today()
        if self._current_date != today:
            self._current_date = today
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._consecutive_losses = 0
            logger.info("每日計數器已重置 (UTC %s)", today)

    def can_trade(self, balance: float) -> tuple[bool, str]:
        """檢查是否可以開新倉"""
        self.reset_daily()

        if self._open_positions >= self.max_concurrent:
            return False, f"已達最大持倉數 {self.max_concurrent}"

        if self._daily_trades >= self.max_daily_trades:
            return False, f"已達每日最大交易次數 {self.max_daily_trades}"

        max_loss = balance * (self.max_daily_loss_pct / 100)
        if self._daily_pnl <= -max_loss:
            return False, f"已達每日最大虧損 {self.max_daily_loss_pct}%"

        if (
            self.max_consecutive_losses > 0
            and self._consecutive_losses >= self.max_consecutive_losses
        ):
            return False, (
                f"已達連續虧損熔斷 {self._consecutive_losses}/"
                f"{self.max_consecutive_losses} 筆，今日暫停開倉"
            )

        return True, "OK"

    def calculate_position_size(
        self, balance: float, price: float, leverage: int = 1, size_ratio: float = 1.0,
    ) -> float:
        """計算倉位大小（合約數量）"""
        max_capital = balance * (self.max_position_pct / 100) * size_ratio
        notional = max_capital * leverage
        quantity = notional / price
        return quantity

    def get_batch_orders(
        self, entry_price: float, total_quantity: float, side: str,
    ) -> list[dict]:
        """
        根據分批建倉設定產生批次訂單

        Returns:
            [{"price": float, "quantity": float, "batch": int}]
        """
        orders = []
        for i, batch in enumerate(self.entry_batches):
            qty = total_quantity * (batch["pct"] / 100)
            offset = batch["offset"] / 100  # 百分比轉小數

            if side == "LONG":
                price = entry_price * (1 + offset)
            else:
                price = entry_price * (1 - offset)

            orders.append({
                "price": round(price, 2),
                "quantity": qty,
                "batch": i + 1,
            })

        return orders

    def record_trade(self, pnl: float):
        # 先檢查跨日，確保平倉 PnL 歸屬到正確的日期
        self.reset_daily()
        self._daily_trades += 1
        self._daily_pnl += pnl
        # 連敗計數：虧損累加、獲利歸零；平盤（pnl == 0）不影響連敗串
        if pnl < 0:
            self._consecutive_losses += 1
        elif pnl > 0:
            self._consecutive_losses = 0
        logger.info(
            "記錄交易: pnl=%.2f | 今日累計: %d 筆, pnl=%.2f, 連敗 %d",
            pnl, self._daily_trades, self._daily_pnl, self._consecutive_losses,
        )

    def add_position(self):
        self._open_positions += 1

    def remove_position(self):
        self._open_positions = max(0, self._open_positions - 1)

    @property
    def open_positions(self) -> int:
        return self._open_positions

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def daily_trades(self) -> int:
        return self._daily_trades

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses
