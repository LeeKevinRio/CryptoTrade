"""Binance API 封裝 — 支援現貨與合約"""

import asyncio
from typing import Any

from binance import AsyncClient, BinanceSocketManager
from binance.enums import *

from src.utils.logger import setup_logger

logger = setup_logger("binance_client")


class BinanceAPI:
    """幣安 API 統一封裝"""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.client: AsyncClient | None = None

    async def connect(self):
        self.client = await AsyncClient.create(
            api_key=self.api_key,
            api_secret=self.api_secret,
            testnet=self.testnet,
        )
        logger.info("Binance API 連接成功 (testnet=%s)", self.testnet)

    async def disconnect(self):
        if self.client:
            await self.client.close_connection()
            logger.info("Binance API 連接已關閉")

    # ── 帳戶資訊 ──

    async def get_account_balance(self) -> dict[str, float]:
        if self.testnet:
            info = await self.client.futures_account()
        else:
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

    # ── 行情資料 ──

    async def get_klines(self, symbol: str, interval: str, limit: int = 500) -> list[list]:
        return await self.client.futures_klines(
            symbol=symbol, interval=interval, limit=limit
        )

    async def get_ticker_price(self, symbol: str) -> float:
        ticker = await self.client.futures_symbol_ticker(symbol=symbol)
        return float(ticker["price"])

    async def get_order_book(self, symbol: str, limit: int = 20) -> dict:
        return await self.client.futures_order_book(symbol=symbol, limit=limit)

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
        except Exception as e:
            logger.warning("設定槓桿失敗: %s", e)

    async def futures_market_order(
        self, symbol: str, side: str, quantity: float
    ) -> dict:
        order = await self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=quantity,
        )
        logger.info("市價單成交: %s %s %s qty=%s", symbol, side, order["orderId"], quantity)
        return order

    async def futures_limit_order(
        self, symbol: str, side: str, quantity: float, price: float
    ) -> dict:
        order = await self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type=FUTURE_ORDER_TYPE_LIMIT,
            timeInForce=TIME_IN_FORCE_GTC,
            quantity=quantity,
            price=str(price),
        )
        logger.info("限價單掛出: %s %s price=%s qty=%s", symbol, side, price, quantity)
        return order

    async def futures_stop_market(
        self, symbol: str, side: str, quantity: float, stop_price: float
    ) -> dict:
        order = await self.client.futures_create_order(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            stopPrice=str(stop_price),
            quantity=quantity,
        )
        logger.info("停損市價單: %s %s stop=%s qty=%s", symbol, side, stop_price, quantity)
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

    async def get_open_positions(self) -> list[dict]:
        account = await self.client.futures_account()
        positions = []
        for pos in account.get("positions", []):
            amt = float(pos.get("positionAmt", 0))
            if amt != 0:
                positions.append({
                    "symbol": pos["symbol"],
                    "side": "LONG" if amt > 0 else "SHORT",
                    "quantity": abs(amt),
                    "entry_price": float(pos["entryPrice"]),
                    "unrealized_pnl": float(pos["unrealizedProfit"]),
                    "leverage": int(pos.get("leverage", 1)),
                })
        return positions

    async def get_symbol_info(self, symbol: str) -> dict:
        info = await self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s
        return {}
