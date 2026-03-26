"""下單執行器 — 負責將策略訊號轉換為實際訂單"""

import asyncio
import math

from src.data.binance_client import BinanceAPI
from src.risk.position_manager import PositionManager
from src.risk.capital_manager import CapitalManager
from src.strategy.base_strategy import Signal, SignalType
from src.indicators.atr import get_current_atr
from src.utils.logger import setup_logger

logger = setup_logger("order_executor")


class OrderExecutor:
    """執行交易訂單"""

    def __init__(self, api: BinanceAPI, position_manager: PositionManager, config: dict):
        self.api = api
        self.pm = position_manager
        self.config = config
        self.leverage = config.get("trading", {}).get("leverage", 5)

        # 快取交易對精度
        self._symbol_info: dict[str, dict] = {}

    async def init_symbol_info(self, symbols: list[str]):
        for symbol in symbols:
            info = await self.api.get_symbol_info(symbol)
            if info:
                qty_precision = 3
                price_precision = 2
                min_qty = 0.001
                for f in info.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        min_qty = float(f["minQty"])
                        step = float(f["stepSize"])
                        qty_precision = max(0, -int(math.log10(step))) if step > 0 else 3
                    elif f["filterType"] == "PRICE_FILTER":
                        tick = float(f["tickSize"])
                        price_precision = max(0, -int(math.log10(tick))) if tick > 0 else 2

                self._symbol_info[symbol] = {
                    "qty_precision": qty_precision,
                    "price_precision": price_precision,
                    "min_qty": min_qty,
                }
                await self.api.set_leverage(symbol, self.leverage)

    async def execute_signal(self, signal: Signal, balance: float, candles_df=None) -> dict | None:
        """
        根據訊號執行交易

        Returns:
            交易結果 dict 或 None
        """
        if not signal.is_actionable:
            return None

        symbol = signal.symbol

        # 風控檢查
        can_trade, reason = self.pm.capital_manager.can_trade(balance)
        if not can_trade:
            logger.warning("風控阻止交易: %s", reason)
            return None

        if self.pm.has_position(symbol):
            logger.info("%s 已有持倉，跳過", symbol)
            return None

        # 計算倉位大小
        from src.strategy.signal_aggregator import SignalAggregator
        size_ratio = 1.0 if signal.strength >= 70 else 0.5
        quantity = self.pm.capital_manager.calculate_position_size(
            balance=balance,
            price=signal.price,
            leverage=self.leverage,
            size_ratio=size_ratio,
        )
        quantity = self._round_qty(symbol, quantity)

        if quantity <= 0:
            logger.warning("%s 計算倉位為 0，跳過", symbol)
            return None

        # 計算 ATR（用於動態停損）
        atr = None
        if candles_df is not None:
            atr = get_current_atr(candles_df)

        # 決定方向
        if signal.type == SignalType.LONG:
            side = "BUY"
            pos_side = "LONG"
        else:
            side = "SELL"
            pos_side = "SHORT"

        try:
            # 市價開倉（第一批）
            batch_config = self.config.get("risk", {}).get("entry_batches", [])
            first_batch_pct = batch_config[0]["pct"] if batch_config else 100
            first_qty = self._round_qty(symbol, quantity * first_batch_pct / 100)

            order = await self.api.futures_market_order(
                symbol=symbol, side=side, quantity=first_qty
            )

            entry_price = signal.price
            if "avgPrice" in order and float(order["avgPrice"]) > 0:
                entry_price = float(order["avgPrice"])

            # 註冊持倉
            pos = self.pm.open_position(
                symbol=symbol,
                side=pos_side,
                entry_price=entry_price,
                quantity=first_qty,
                leverage=self.leverage,
                atr=atr,
            )

            # 掛剩餘批次限價單
            if len(batch_config) > 1:
                remaining_batches = self.pm.capital_manager.get_batch_orders(
                    entry_price=entry_price,
                    total_quantity=quantity,
                    side=pos_side,
                )
                for batch in remaining_batches[1:]:  # 跳過第一批（已市價成交）
                    batch_qty = self._round_qty(symbol, batch["quantity"])
                    if batch_qty > 0:
                        try:
                            await self.api.futures_limit_order(
                                symbol=symbol,
                                side=side,
                                quantity=batch_qty,
                                price=self._round_price(symbol, batch["price"]),
                            )
                        except Exception as e:
                            logger.warning("批次掛單失敗: batch %d - %s", batch["batch"], e)

            # 掛停損單
            stop_price = self.pm.sl_manager.get_stop_price(symbol)
            if stop_price:
                close_side = "SELL" if pos_side == "LONG" else "BUY"
                try:
                    await self.api.futures_stop_market(
                        symbol=symbol,
                        side=close_side,
                        quantity=first_qty,
                        stop_price=self._round_price(symbol, stop_price),
                    )
                except Exception as e:
                    logger.warning("停損單掛出失敗: %s", e)

            result = {
                "symbol": symbol,
                "side": pos_side,
                "entry_price": entry_price,
                "quantity": first_qty,
                "leverage": self.leverage,
                "signal_strength": signal.strength,
                "reasons": signal.reasons,
                "order_id": order.get("orderId"),
            }
            logger.info("✅ 開倉成功: %s", result)
            return result

        except Exception as e:
            logger.error("開倉失敗 %s: %s", symbol, e)
            return None

    async def close_position(self, symbol: str, quantity: float, reason: str) -> dict | None:
        """平倉指定數量"""
        pos = self.pm.get_position(symbol)
        if not pos:
            return None

        close_side = "SELL" if pos.side == "LONG" else "BUY"
        qty = self._round_qty(symbol, quantity)

        try:
            order = await self.api.futures_market_order(
                symbol=symbol, side=close_side, quantity=qty
            )
            exit_price = float(order.get("avgPrice", 0))
            if exit_price == 0:
                exit_price = await self.api.get_ticker_price(symbol)

            result = self.pm.close_position(symbol, exit_price)
            if result:
                result["reason"] = reason
            logger.info("✅ 平倉: %s reason=%s", symbol, reason)

            # 取消剩餘掛單
            try:
                await self.api.cancel_all_orders(symbol)
            except Exception:
                pass

            return result
        except Exception as e:
            logger.error("平倉失敗 %s: %s", symbol, e)
            return None

    def _round_qty(self, symbol: str, quantity: float) -> float:
        info = self._symbol_info.get(symbol, {})
        precision = info.get("qty_precision", 3)
        min_qty = info.get("min_qty", 0.001)
        rounded = round(quantity, precision)
        return rounded if rounded >= min_qty else 0.0

    def _round_price(self, symbol: str, price: float) -> float:
        info = self._symbol_info.get(symbol, {})
        precision = info.get("price_precision", 2)
        return round(price, precision)
