"""下單執行器 — 支援現貨 / 合約雙模式"""

import math

from src.data.binance_client import BinanceAPI
from src.risk.position_manager import PositionManager
from src.strategy.base_strategy import Signal, SignalType
from src.indicators.atr import get_current_atr
from src.utils.logger import setup_logger


class OrderExecutor:
    """執行交易訂單

    Args:
        mode: "spot" 或 "futures"
    """

    def __init__(
        self,
        api: BinanceAPI,
        position_manager: PositionManager,
        config: dict,
        mode: str = "futures",
        bot_id: str = "futures",
    ):
        self.api = api
        self.pm = position_manager
        self.config = config
        self.mode = mode
        self.bot_id = bot_id
        self.leverage = config.get("leverage", 5) if mode == "futures" else 1
        self.only_long = config.get("only_long", mode == "spot")
        self.logger = setup_logger(f"executor.{bot_id}")

        self._symbol_info: dict[str, dict] = {}

    async def init_symbol_info(self, symbols: list[str]):
        # 任何單一標的初始化失敗（不支援的槓桿/缺合約資訊）→ 剔除該標的續行，
        # 不可拖垮整個引擎（例：測試網上新幣槓桿上限低於設定值曾致全面停擺）
        for symbol in list(symbols):
            try:
                if self.mode == "futures":
                    info = await self.api.get_symbol_info(symbol)
                else:
                    info = await self.api.get_spot_symbol_info(symbol)
                if not info:
                    raise RuntimeError("查無合約/交易對資訊")

                qty_precision = 3
                price_precision = 2
                min_qty = 0.001
                for f in info.get("filters", []):
                    ftype = f["filterType"]
                    if ftype == "LOT_SIZE":
                        min_qty = float(f["minQty"])
                        step = float(f["stepSize"])
                        qty_precision = max(0, -int(math.log10(step))) if step > 0 else 3
                    elif ftype == "PRICE_FILTER":
                        tick = float(f["tickSize"])
                        price_precision = max(0, -int(math.log10(tick))) if tick > 0 else 2

                self._symbol_info[symbol] = {
                    "qty_precision": qty_precision,
                    "price_precision": price_precision,
                    "min_qty": min_qty,
                }

                if self.mode == "futures":
                    # 先鎖定保證金模式再設槓桿：兩者的爆倉特性差異極大，
                    # 不可依賴帳戶預設值（詳見 set_margin_type doc）
                    await self.api.set_margin_type(
                        symbol, self.config.get("margin_type", "CROSSED"),
                    )
                    eff_lev = await self._set_leverage_with_fallback(symbol)
                    self._symbol_info[symbol]["leverage"] = eff_lev
            except Exception as e:  # noqa: BLE001
                self.logger.warning("⚠️ %s 初始化失敗，剔除此標的: %s", symbol, e)
                self._symbol_info.pop(symbol, None)
                if symbol in symbols:
                    symbols.remove(symbol)   # 就地移除，orchestrator/bots 共用同一 list

    async def _set_leverage_with_fallback(self, symbol: str) -> int:
        """設定槓桿；標的不支援設定值時自動退到其允許的最大槓桿。"""
        try:
            await self.api.set_leverage(symbol, self.leverage)
            return self.leverage
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "-4028" not in msg and "not valid" not in msg.lower():
                raise
        max_lev = await self.api.get_max_leverage(symbol)
        eff = max(1, min(self.leverage, max_lev))
        await self.api.set_leverage(symbol, eff)
        self.logger.warning(
            "%s 不支援 %dx 槓桿，退為 %dx（該標的上限 %dx）",
            symbol, self.leverage, eff, max_lev,
        )
        return eff

    async def execute_signal(self, signal: Signal, balance: float, candles_df=None) -> dict | None:
        if not signal.is_actionable:
            return None

        # 現貨只接受 LONG 訊號
        if self.only_long and signal.type != SignalType.LONG:
            return None

        symbol = signal.symbol

        # per-symbol 鎖：防止與 reconcile_loop / risk_loop 競爭
        async with self.pm.lock_for(symbol):
            return await self._execute_signal_locked(signal, balance, candles_df)

    async def _execute_signal_locked(self, signal: Signal, balance: float, candles_df=None) -> dict | None:
        symbol = signal.symbol
        can_trade, reason = self.pm.capital_manager.can_trade(balance)
        if not can_trade:
            self.logger.warning("風控阻止交易: %s", reason)
            return None

        if self.pm.has_position(symbol):
            self.logger.info("%s 已有持倉，跳過", symbol)
            return None

        # 用該標的「實際生效」的槓桿計算部位 —— 標的槓桿上限低於設定值時
        # 已自動退階，沿用設定值會高估可開名目
        eff_leverage = self._symbol_info.get(symbol, {}).get("leverage", self.leverage)
        size_ratio = 1.0 if signal.strength >= 70 else 0.5
        quantity = self.pm.capital_manager.calculate_position_size(
            balance=balance,
            price=signal.price,
            leverage=eff_leverage,
            size_ratio=size_ratio,
        )
        quantity = self._round_qty(symbol, quantity)
        if quantity <= 0:
            self.logger.warning("%s 計算倉位為 0，跳過", symbol)
            return None

        atr = get_current_atr(candles_df) if candles_df is not None else None

        if signal.type == SignalType.LONG:
            order_side = "BUY"
            pos_side = "LONG"
        else:
            order_side = "SELL"
            pos_side = "SHORT"

        try:
            batch_config = self.config.get("risk", {}).get("entry_batches", [])
            first_batch_pct = batch_config[0]["pct"] if batch_config else 100
            requested_qty = self._round_qty(symbol, quantity * first_batch_pct / 100)

            order = await self._place_market(symbol, order_side, requested_qty)

            # 取真實成交數量（含部分成交）
            filled_qty = self._extract_filled_qty(order, requested_qty)
            if filled_qty <= 0:
                self.logger.error("%s 訂單 %s 成交量為 0，放棄記錄持倉", symbol, order.get("orderId"))
                return None

            entry_price = signal.price
            if "avgPrice" in order and float(order["avgPrice"]) > 0:
                entry_price = float(order["avgPrice"])

            self.pm.open_position(
                symbol=symbol,
                side=pos_side,
                entry_price=entry_price,
                quantity=filled_qty,
                leverage=eff_leverage,
                atr=atr,
            )

            # 剩餘批次（合約用限價，現貨先省略以簡化）
            if self.mode == "futures" and len(batch_config) > 1:
                remaining = self.pm.capital_manager.get_batch_orders(
                    entry_price=entry_price,
                    total_quantity=quantity,
                    side=pos_side,
                )
                for batch in remaining[1:]:
                    bq = self._round_qty(symbol, batch["quantity"])
                    if bq > 0:
                        try:
                            await self.api.futures_limit_order(
                                symbol=symbol, side=order_side, quantity=bq,
                                price=self._round_price(symbol, batch["price"]),
                            )
                        except Exception as e:
                            self.logger.warning("批次掛單失敗: %s", e)

            # 合約掛硬停損；現貨用軟停損（風控循環監控）
            if self.mode == "futures":
                stop_price = self.pm.sl_manager.get_stop_price(symbol)
                if stop_price:
                    close_side = "SELL" if pos_side == "LONG" else "BUY"
                    try:
                        # closePosition：部分停利成交後仍精確平掉剩餘倉位
                        await self.api.futures_stop_market(
                            symbol=symbol, side=close_side,
                            stop_price=self._round_price(symbol, stop_price),
                            close_position=True,
                        )
                    except Exception as e:
                        self.logger.warning("停損單掛出失敗: %s", e)

                # maker 停利階梯：reduce-only + post-only 限價單掛在交易所，
                # 成交吃 maker 費率（taker 一半以下），由 reconcile 對帳入帳
                if self.config.get("risk", {}).get("use_maker_tp", False):
                    await self._place_tp_ladder(symbol, pos_side, entry_price, filled_qty)

            result = {
                "bot_id": self.bot_id,
                "mode": self.mode,
                "symbol": symbol,
                "side": pos_side,
                "entry_price": entry_price,
                "quantity": filled_qty,
                "leverage": eff_leverage,
                "signal_strength": signal.strength,
                "reasons": signal.reasons,
                "order_id": order.get("orderId"),
            }
            self.logger.info("✅ 開倉成功: %s", result)
            return result

        except Exception as e:
            self.logger.error("開倉失敗 %s: %s", symbol, e)
            return None

    async def _place_tp_ladder(self, symbol: str, pos_side: str,
                               entry_price: float, quantity: float):
        """把三階停利掛成 reduce-only + post-only(GTX) 限價單（maker 費率）"""
        tp_cfg = self.config.get("risk", {}).get("take_profit", {})
        close_side = "SELL" if pos_side == "LONG" else "BUY"
        sign = 1 if pos_side == "LONG" else -1
        for i in (1, 2, 3):
            pct = tp_cfg.get(f"level_{i}_pct")
            close_pct = tp_cfg.get(f"level_{i}_close_pct")
            if not pct or not close_pct:
                continue
            qty = self._round_qty(symbol, quantity * close_pct / 100)
            if qty <= 0:
                continue
            price = self._round_price(symbol, entry_price * (1 + sign * pct / 100))
            try:
                await self.api.futures_limit_order(
                    symbol=symbol, side=close_side, quantity=qty, price=price,
                    time_in_force="GTX", reduce_only=True,
                )
            except Exception as e:
                # 單一階失敗不阻斷：軟體端 trailing/時停/停損仍完整備援
                self.logger.warning("TP L%d 掛單失敗 %s: %s", i, symbol, e)

    async def close_position(self, symbol: str, quantity: float, reason: str) -> dict | None:
        # per-symbol 鎖：防止 risk_loop 與 web close 同時觸發雙重平倉
        async with self.pm.lock_for(symbol):
            pos = self.pm.get_position(symbol)
            if not pos:
                self.logger.info("%s 無持倉，略過平倉", symbol)
                return None

            close_side = "SELL" if pos.side == "LONG" else "BUY"
            qty = self._round_qty(symbol, min(quantity, pos.quantity))
            if qty <= 0:
                return None
            is_partial = qty < pos.quantity - 1e-12

            try:
                order = await self._place_market(symbol, close_side, qty, reduce_only=True)
                exit_price = float(order.get("avgPrice", 0) or 0)
                if exit_price == 0:
                    try:
                        exit_price = await self.api.get_ticker_price(symbol)
                    except Exception:
                        exit_price = pos.entry_price  # 最後 fallback

                result = self.pm.close_position(symbol, exit_price, quantity=qty)
                if result:
                    result["reason"] = reason
                    result["bot_id"] = self.bot_id
                    result["mode"] = self.mode
                self.logger.info("✅ %s平倉: %s reason=%s",
                                 "部分" if is_partial else "", symbol, reason)

                # 部分平倉：保留停損(closePosition)與未觸發的 TP 掛單，不可全取消
                if not is_partial:
                    try:
                        if self.mode == "futures":
                            await self.api.cancel_all_orders(symbol)
                        else:
                            await self.api.spot_cancel_all_orders(symbol)
                    except Exception:
                        pass

                return result
            except Exception as e:
                self.logger.error("平倉失敗 %s: %s", symbol, e)
                return None

    @staticmethod
    def _extract_filled_qty(order: dict, fallback: float) -> float:
        """從訂單回應取真實成交數量（合約 / 現貨皆相容）"""
        for key in ("executedQty", "cumQty", "origQty"):
            v = order.get(key)
            if v is not None:
                try:
                    qty = float(v)
                    if qty > 0:
                        return qty
                except (TypeError, ValueError):
                    pass
        return fallback

    async def _place_market(self, symbol: str, side: str, qty: float,
                            reduce_only: bool = False) -> dict:
        if self.mode == "futures":
            return await self.api.futures_market_order(
                symbol=symbol, side=side, quantity=qty, reduce_only=reduce_only,
            )
        return await self.api.spot_market_order(symbol=symbol, side=side, quantity=qty)

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
