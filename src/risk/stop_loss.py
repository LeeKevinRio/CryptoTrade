"""停損機制 — 固定停損 + ATR 動態停損"""

from src.indicators.atr import get_current_atr
from src.utils.logger import setup_logger

logger = setup_logger("stop_loss")


class StopLossManager:
    """管理停損邏輯"""

    def __init__(self, config: dict):
        risk_cfg = config.get("risk", {})
        self.fixed_stop_pct = risk_cfg.get("stop_loss_pct", 2.0)
        self.atr_multiplier = risk_cfg.get("atr_multiplier", 1.5)
        # 停損絕對上限；不得低於用戶指定的固定停損，否則永遠被裁切
        configured_max = risk_cfg.get("max_stop_pct", 4.0)
        if configured_max < self.fixed_stop_pct:
            logger.warning(
                "max_stop_pct (%.1f) < stop_loss_pct (%.1f)，自動提升為固定停損值",
                configured_max, self.fixed_stop_pct,
            )
            configured_max = self.fixed_stop_pct
        self.max_stop_pct = configured_max

        # {symbol: {"entry_price": float, "side": str, "stop_price": float}}
        self._stops: dict[str, dict] = {}

    def set_stop(self, symbol: str, side: str, entry_price: float, atr: float | None = None):
        if atr and atr > 0:
            # 動態停損：固定停損 與 1.5 × ATR 取「較寬」（高波動時不被噪音打掉）
            atr_pct = (self.atr_multiplier * atr / entry_price) * 100
            effective_pct = max(self.fixed_stop_pct, atr_pct)
            # ATR 算出超過上限時警告，避免使用者誤以為停損很寬
            if atr_pct > self.max_stop_pct:
                logger.warning(
                    "%s ATR 停損 %.2f%% 超過上限 %.2f%%，已封頂",
                    symbol, atr_pct, self.max_stop_pct,
                )
            effective_pct = min(effective_pct, self.max_stop_pct)
        else:
            effective_pct = self.fixed_stop_pct

        if side == "LONG":
            stop_price = entry_price * (1 - effective_pct / 100)
        else:
            stop_price = entry_price * (1 + effective_pct / 100)

        self._stops[symbol] = {
            "entry_price": entry_price,
            "side": side,
            "stop_price": stop_price,
            "stop_pct": effective_pct,
        }
        logger.info(
            "設定停損: %s %s entry=%.2f stop=%.2f (%.2f%%)",
            symbol, side, entry_price, stop_price, effective_pct,
        )

    def remove_stop(self, symbol: str):
        self._stops.pop(symbol, None)

    def check(self, symbol: str, current_price: float) -> dict | None:
        """
        檢查是否觸發停損

        Returns:
            {"action": "stop_loss", "reason": str} 或 None
        """
        stop = self._stops.get(symbol)
        if not stop:
            return None

        triggered = False
        if stop["side"] == "LONG" and current_price <= stop["stop_price"]:
            triggered = True
        elif stop["side"] == "SHORT" and current_price >= stop["stop_price"]:
            triggered = True

        if triggered:
            pnl_pct = self._calc_pnl_pct(stop, current_price)
            logger.warning(
                "🛑 %s 停損觸發! price=%.2f stop=%.2f pnl=%.2f%%",
                symbol, current_price, stop["stop_price"], pnl_pct,
            )
            return {
                "action": "stop_loss",
                "reason": f"停損觸發: 價格 {current_price:.2f} 跌破停損線 {stop['stop_price']:.2f} ({pnl_pct:.2f}%)",
            }
        return None

    def get_stop_price(self, symbol: str) -> float | None:
        stop = self._stops.get(symbol)
        return stop["stop_price"] if stop else None

    def _calc_pnl_pct(self, stop: dict, current_price: float) -> float:
        entry = stop["entry_price"]
        if stop["side"] == "LONG":
            return (current_price - entry) / entry * 100
        else:
            return (entry - current_price) / entry * 100
