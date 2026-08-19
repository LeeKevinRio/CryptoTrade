"""Binance API 封裝 — 支援現貨與合約"""

import asyncio
import functools
from typing import Any

from binance import AsyncClient, BinanceSocketManager
from binance.enums import *
from binance.exceptions import BinanceAPIException

from src.utils.logger import setup_logger

logger = setup_logger("binance_client")


# ── 可重試例外與 Binance 短暫錯誤碼 ──
_RETRY_BINANCE_CODES = {-1003, -1015, -1021}  # 速率限制、過多請求、時間戳偏差
_RETRY_HTTP = {429, 500, 502, 503, 504}


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, BinanceAPIException):
        if exc.status_code in _RETRY_HTTP:
            return True
        if exc.code in _RETRY_BINANCE_CODES:
            return True
        return False
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    return False


def with_retry(retries: int = 3, base_delay: float = 1.0):
    """指數退避重試裝飾器"""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if not _retryable(exc) or attempt == retries - 1:
                        raise
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "%s 第 %d 次失敗（%s），%.1fs 後重試",
                        func.__name__, attempt + 1, exc, delay,
                    )
                    await asyncio.sleep(delay)
            if last_exc:
                raise last_exc
        return wrapper
    return decorator


class BinanceAPI:
    """幣安 API 統一封裝"""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.client: AsyncClient | None = None

    async def connect(self):
        # 繞過 AsyncClient.create() 內建的 spot ping/server_time —
        # spot testnet 偶有 502，會擋住整個啟動流程（連 futures-only 都炸）。
        # 直接 __init__ 後用 futures_time 校時。
        import time
        self.client = AsyncClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet,
        )
        try:
            res = await self.client.futures_time()
            self.client.timestamp_offset = res["serverTime"] - int(time.time() * 1000)
        except Exception as exc:
            await self.client.close_connection()
            self.client = None
            raise
        logger.info("Binance API 連接成功 (testnet=%s)", self.testnet)

    async def disconnect(self):
        if self.client:
            await self.client.close_connection()
            logger.info("Binance API 連接已關閉")

    # ── 帳戶資訊（合約） ──

    async def get_account_balance(self) -> dict[str, float]:
        info = await self.client.futures_account()
        balances = {}
        for asset in info.get("assets", []):
            bal = float(asset.get("walletBalance", 0))
            if bal > 0:
                balances[asset["asset"]] = bal
        return balances

    async def get_usdt_balance(self) -> float:
        balances = await self.get_account_balance()
        return balances.get("USDT", 0.0)

    # ── 帳戶資訊（現貨） ──

    async def get_spot_balances(self) -> dict[str, float]:
        info = await self.client.get_account()
        balances = {}
        for b in info.get("balances", []):
            free = float(b.get("free", 0))
            if free > 0:
                balances[b["asset"]] = free
        return balances

    async def get_spot_usdt_balance(self) -> float:
        balances = await self.get_spot_balances()
        return balances.get("USDT", 0.0)

    # ── 行情資料 ──

    @with_retry()
    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[list]:
        return await self.client.futures_klines(
            symbol=symbol, interval=interval, limit=limit
        )

    @with_retry()
    async def get_ticker_price(self, symbol: str) -> float:
        ticker = await self.client.futures_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    @with_retry()
    async def get_order_book(self, symbol: str, limit: int = 20) -> dict:
        return await self.client.futures_order_book(symbol=symbol, limit=limit)

    @with_retry()
    async def get_funding_rate(self, symbol: str) -> float:
        info = await self.client.futures_funding_rate(symbol=symbol, limit=1)
        if info:
            return float(info[-1]["fundingRate"])
        return 0.0

    # ── 合約交易 ──

    async def set_leverage(self, symbol: str, leverage: int):
        try:
            await self.client.futures_change_leverage(
                symbol=symbol, leverage=leverage
            )
            logger.info("%s 槓桿設為 %dx", symbol, leverage)
        except BinanceAPIException as e:
            # -4046: no need to change leverage（已是該值）
            if e.code == -4046:
                logger.info("%s 槓桿已為 %dx", symbol, leverage)
                return
            # 其他失敗一律 raise，避免錯誤槓桿下單
            logger.error("%s 設定槓桿 %dx 失敗: %s", symbol, leverage, e)
            raise

    async def set_margin_type(self, symbol: str, margin_type: str = "CROSSED"):
        """明確設定保證金模式，不依賴帳戶預設值。

        逐倉(ISOLATED)下爆倉線約在 1/槓桿 處，高槓桿會在停損成交前先爆倉；
        全倉(CROSSED)以整體淨值撐倉，爆倉幅度取決於總名目/淨值。
        兩者風險特性差異極大，必須顯式指定以確保行為可預期。
        """
        try:
            await self.client.futures_change_margin_type(
                symbol=symbol, marginType=margin_type
            )
            logger.info("%s 保證金模式設為 %s", symbol, margin_type)
        except BinanceAPIException as e:
            # -4046: no need to change margin type（已是該模式）
            if e.code == -4046:
                logger.info("%s 保證金模式已為 %s", symbol, margin_type)
                return
            # 有持倉時無法切換；記錄但不中斷（既有倉沿用原模式）
            logger.warning("%s 設定保證金模式 %s 失敗: %s", symbol, margin_type, e)

    @with_retry()
    async def futures_market_order(
        self, symbol: str, side: str, quantity: float, reduce_only: bool = False
    ) -> dict:
        params: dict[str, Any] = dict(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=quantity,
        )
        # 平倉單一律 reduceOnly：即使數量計算或狀態不同步，也絕不會反向開新倉
        if reduce_only:
            params["reduceOnly"] = "true"
        order = await self.client.futures_create_order(**params)
        logger.info("市價單成交: %s %s %s qty=%s", symbol, side, order["orderId"], quantity)
        return order

    @with_retry()
    async def futures_limit_order(
        self, symbol: str, side: str, quantity: float, price: float,
        time_in_force: str = TIME_IN_FORCE_GTC, reduce_only: bool = False,
    ) -> dict:
        params: dict[str, Any] = dict(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_LIMIT,
            timeInForce=time_in_force,   # GTX = post-only，只當 maker（吃單會被拒）
            quantity=quantity,
            price=str(price),
        )
        if reduce_only:
            params["reduceOnly"] = "true"
        order = await self.client.futures_create_order(**params)
        logger.info("限價單掛出: %s %s price=%s qty=%s tif=%s", symbol, side, price, quantity, time_in_force)
        return order

    @with_retry()
    async def futures_stop_market(
        self, symbol: str, side: str, stop_price: float,
        quantity: float | None = None, close_position: bool = False,
    ) -> dict:
        params: dict[str, Any] = dict(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            stopPrice=str(stop_price),
        )
        # closePosition=true：觸發時平掉「當下」整個倉位 ——
        # 部分停利成交後數量會變，固定 quantity 的停損單會多平而反向開倉
        if close_position:
            params["closePosition"] = "true"
        else:
            params["quantity"] = quantity
        order = await self.client.futures_create_order(**params)
        logger.info("停損市價單: %s %s stop=%s %s", symbol, side, stop_price,
                    "closePosition" if close_position else f"qty={quantity}")
        return order

    async def futures_take_profit_market(
        self, symbol: str, side: str, quantity: float, stop_price: float
    ) -> dict:
        order = await self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=str(stop_price),
            quantity=quantity,
        )
        logger.info("停利市價單: %s %s tp=%s qty=%s", symbol, side, stop_price, quantity)
        return order

    async def cancel_all_orders(self, symbol: str):
        await self.client.futures_cancel_all_open_orders(symbol=symbol)
        logger.info("已取消 %s 所有掛單", symbol)

    # ── 現貨交易 ──

    @with_retry()
    async def spot_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        order = await self.client.create_order(
            symbol=symbol,
            side=side,
            type=ORDER_TYPE_MARKET,
            quantity=quantity,
        )
        fills = order.get("fills", [])
        if fills:
            avg = sum(float(f["price"]) * float(f["qty"]) for f in fills) / sum(float(f["qty"]) for f in fills)
            order["avgPrice"] = avg
        logger.info("現貨市價單: %s %s qty=%s", symbol, side, quantity)
        return order

    async def spot_cancel_all_orders(self, symbol: str):
        try:
            await self.client.cancel_open_orders(symbol=symbol)
        except Exception:
            pass

    async def get_spot_symbol_info(self, symbol: str) -> dict:
        info = await self.client.get_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s
        return {}

    @with_retry()
    async def get_open_positions(self) -> list[dict]:
        # futures_position_information 直接帶 liquidationPrice
        info = await self.client.futures_position_information()
        positions = []
        for pos in info:
            amt = float(pos.get("positionAmt", 0))
            if amt == 0:
                continue
            positions.append({
                "symbol": pos["symbol"],
                "side": "LONG" if amt > 0 else "SHORT",
                "quantity": abs(amt),
                "entry_price": float(pos["entryPrice"]),
                "unrealized_pnl": float(pos.get("unRealizedProfit", 0)),
                "leverage": int(pos.get("leverage", 1)),
                "liquidation_price": float(pos.get("liquidationPrice", 0) or 0),
                "margin_type": pos.get("marginType", "cross"),
            })
        return positions

    async def get_max_leverage(self, symbol: str) -> int:
        """查詢交易對允許的最大槓桿（新/小市值標的常低於主流幣）"""
        data = await self.client.futures_leverage_bracket(symbol=symbol)
        entry = data[0] if isinstance(data, list) and data else data or {}
        brackets = entry.get("brackets", [])
        if brackets:
            return int(brackets[0].get("initialLeverage", 20))
        return 20

    async def get_symbol_info(self, symbol: str) -> dict:
        info = await self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s
        return {}
