"""CryptoTrade 主程式 — 幣安自動交易機器人"""

import asyncio
from datetime import datetime, timezone

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
from src.web.app import create_app, serve as serve_web
from src.web.event_bus import bus
from src.web.state import state

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

        # 寫入共享 state（供 Web 讀取）
        state.bot_ref = self
        state.testnet = self.config["binance"]["testnet"]
        state.symbols = self.symbols
        state.timeframes = self.timeframes
        state.balance = balance
        state.started_at = datetime.now(timezone.utc).isoformat()

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

        # 更新即時價格 + 推送
        state.last_prices[symbol] = data["close"]
        await bus.publish("kline", data)

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

        # 推送訊號（無論是否進場）
        signal_payload = {
            "symbol": symbol,
            "type": signal.type.value,
            "strength": signal.strength,
            "actionable": signal.is_actionable,
            "reasons": signal.reasons[:5],
        }
        state.last_signals[symbol] = signal_payload
        await bus.publish("signal", signal_payload)

        if signal.is_actionable:
            logger.info(
                "📍 訊號: %s %s 強度=%.1f",
                signal.type.value, symbol, signal.strength,
            )
            if state.paused:
                logger.info("Bot 已暫停進場，跳過訊號")
                return
            await self._handle_signal(signal)

    async def _handle_signal(self, signal):
        """處理交易訊號"""
        try:
            balance = await self.api.get_usdt_balance()
            state.balance = balance

            main_df = self.candle_manager.get_candles(signal.symbol, "5m")

            result = await self.executor.execute_signal(
                signal=signal,
                balance=balance,
                candles_df=main_df,
            )

            if result:
                self.tracker.record_open(result)
                await self.notifier.notify_open(result)
                await bus.publish("order_open", result)

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
                                await bus.publish("order_close", result)

            except Exception as e:
                logger.error("風控監控錯誤: %s", e)

            await asyncio.sleep(1)


async def main():
    config = load_config()

    log_cfg = config.get("logging", {})
    setup_logger("cryptotrade", log_cfg.get("level", "INFO"), log_cfg.get("file"))

    bot = CryptoTradeBot(config)

    # Web Dashboard
    web_cfg = config.get("web", {})
    web_enabled = web_cfg.get("enabled", True)
    web_host = web_cfg.get("host", "0.0.0.0")
    web_port = int(web_cfg.get("port", 8000))

    if web_enabled:
        app = create_app(tracker=bot.tracker)
        web_task = asyncio.create_task(serve_web(app, host=web_host, port=web_port))
        logger.info("Web Dashboard: http://%s:%d", web_host, web_port)
    else:
        web_task = None

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()
    finally:
        if web_task:
            web_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
