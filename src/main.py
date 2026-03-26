"""CryptoTrade 主程式 — 幣安自動交易機器人"""

import asyncio
import signal
import sys

from src.data.binance_client import BinanceAPI
from src.data.websocket_feed import WebSocketFeed
from src.data.candle_manager import CandleManager
from src.strategy.signal_aggregator import SignalAggregator
from src.strategy.base_strategy import SignalType
from src.risk.position_manager import PositionManager
from src.execution.order_executor import OrderExecutor
from src.execution.order_tracker import OrderTracker
from src.notification.notifier import TelegramNotifier
from src.utils.config import load_config
from src.utils.models import init_db
from src.utils.logger import setup_logger

logger = setup_logger("main")


class CryptoTradeBot:
    """主交易機器人"""

    def __init__(self, config: dict):
        self.config = config
        self.running = False

        # 核心組件
        self.api = BinanceAPI(
            api_key=config["binance"]["api_key"],
            api_secret=config["binance"]["api_secret"],
            testnet=config["binance"]["testnet"],
        )
        self.candle_manager = CandleManager()
        self.aggregator = SignalAggregator(config)
        self.position_manager = PositionManager(config)
        self.executor = OrderExecutor(self.api, self.position_manager, config)

        # 資料庫
        db_url = config.get("database", {}).get("url", "sqlite:///cryptotrade.db")
        sync_url = db_url.replace("+aiosqlite", "")
        session_factory = init_db(sync_url)
        self.tracker = OrderTracker(session_factory)

        # 通知
        tg = config.get("telegram", {})
        self.notifier = TelegramNotifier(
            bot_token=tg.get("bot_token", ""),
            chat_id=tg.get("chat_id", ""),
            enabled=config.get("notification", {}).get("enabled", True),
        )

        # WebSocket
        self.ws_feed: WebSocketFeed | None = None

        # 交易設定
        self.symbols = config.get("trading", {}).get("symbols", ["BTCUSDT"])
        self.timeframes = config.get("trading", {}).get("timeframes", ["5m", "15m", "1h"])

    async def start(self):
        """啟動機器人"""
        logger.info("=" * 50)
        logger.info("  CryptoTrade Bot 啟動中...")
        logger.info("=" * 50)

        # 連接 Binance
        await self.api.connect()

        # 初始化交易對資訊
        await self.executor.init_symbol_info(self.symbols)

        # 載入歷史 K 線
        await self._load_historical_candles()

        # 啟動 WebSocket
        self.ws_feed = WebSocketFeed(self.api.client)
        await self.ws_feed.start()
        self.ws_feed.on_kline(self._on_kline)

        for symbol in self.symbols:
            for tf in self.timeframes:
                self.ws_feed.subscribe_kline(symbol, tf)

        # 顯示餘額
        balance = await self.api.get_usdt_balance()
        logger.info("帳戶 USDT 餘額: %.2f", balance)

        await self.notifier.send(
            f"🚀 *CryptoTrade Bot 已啟動*\n"
            f"交易對: {', '.join(self.symbols)}\n"
            f"餘額: `{balance:.2f} USDT`\n"
            f"模式: {'Testnet' if self.config['binance']['testnet'] else '正式環境'}"
        )

        self.running = True
        logger.info("Bot 已啟動，監聽行情中...")

        # 啟動風控監控循環
        asyncio.create_task(self._risk_monitor_loop())

        # 保持運行
        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        """停止機器人"""
        self.running = False
        if self.ws_feed:
            await self.ws_feed.stop()
        await self.api.disconnect()
        await self.notifier.send("🛑 *CryptoTrade Bot 已停止*")
        logger.info("Bot 已停止")

    async def _load_historical_candles(self):
        """啟動時載入歷史 K 線"""
        for symbol in self.symbols:
            for tf in self.timeframes:
                try:
                    klines = await self.api.get_klines(symbol, tf, limit=500)
                    self.candle_manager.init_from_klines(symbol, tf, klines)
                except Exception as e:
                    logger.error("載入 %s %s K線失敗: %s", symbol, tf, e)

    async def _on_kline(self, data: dict):
        """K 線更新回調"""
        symbol = data["symbol"]
        interval = data["interval"]

        self.candle_manager.update_candle(symbol, interval, data)

        # 只在 K 線收盤時評估訊號（避免過度交易）
        if not data["is_closed"]:
            return

        # 收集所有時框的 K 線
        candles = {}
        for tf in self.timeframes:
            df = self.candle_manager.get_candles(symbol, tf)
            if df is not None:
                candles[tf] = df

        if not candles:
            return

        # 取得資金費率
        try:
            funding_rate = await self.api.get_funding_rate(symbol)
        except Exception:
            funding_rate = 0.0

        # 策略評估
        signal = self.aggregator.evaluate(symbol, candles, funding_rate)

        if signal.is_actionable:
            logger.info(
                "📍 訊號: %s %s 強度=%.1f",
                signal.type.value, symbol, signal.strength,
            )
            await self._handle_signal(signal)

    async def _handle_signal(self, signal):
        """處理交易訊號"""
        try:
            balance = await self.api.get_usdt_balance()

            # 取得主要時框的 K 線（用於 ATR 計算）
            main_df = self.candle_manager.get_candles(signal.symbol, "5m")

            result = await self.executor.execute_signal(
                signal=signal,
                balance=balance,
                candles_df=main_df,
            )

            if result:
                self.tracker.record_open(result)
                await self.notifier.notify_open(result)

        except Exception as e:
            logger.error("處理訊號失敗: %s", e)
            await self.notifier.notify_error(f"處理訊號失敗: {e}")

    async def _risk_monitor_loop(self):
        """持續監控持倉的風控狀態（每秒檢查）"""
        while self.running:
            try:
                for symbol in list(self.position_manager.all_positions.keys()):
                    price = self.candle_manager.get_latest_price(symbol)
                    if price is None:
                        continue

                    actions = self.position_manager.check_risk(symbol, price)

                    for action in actions:
                        qty = action.get("quantity", 0)
                        reason = action.get("reason", "未知")

                        if action["action"] in ("close", "stop_loss"):
                            result = await self.executor.close_position(
                                symbol=symbol, quantity=qty, reason=reason
                            )
                            if result:
                                self.tracker.record_close(
                                    symbol=symbol,
                                    exit_price=result["exit_price"],
                                    pnl=result["pnl"],
                                    pnl_pct=result["pnl_pct"],
                                )
                                await self.notifier.notify_close(result)

            except Exception as e:
                logger.error("風控監控錯誤: %s", e)

            await asyncio.sleep(1)


async def main():
    config = load_config()

    log_cfg = config.get("logging", {})
    setup_logger("cryptotrade", log_cfg.get("level", "INFO"), log_cfg.get("file"))

    bot = CryptoTradeBot(config)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop()))

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
