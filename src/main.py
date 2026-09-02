"""CryptoTrade 主程式 — 雙 bot（spot + futures）"""

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
from src.external.store import store as ext_store
from src.notification.notifier import TelegramNotifier
from src.sentiment import MarketSentimentProvider
from src.utils.config import load_config
from src.utils.models import init_db
from src.utils.logger import setup_logger
from src.web.app import create_app, serve as serve_web
from src.web.event_bus import bus
from src.web.state import state, BotState

logger = setup_logger("main")


class TradeBot:
    """單一交易 bot — 由 mode (spot/futures) 與 config 決定行為"""

    def __init__(
        self,
        bot_id: str,
        bot_cfg: dict,
        api: BinanceAPI,
        candle_manager: CandleManager,
        symbols: list[str],
        timeframes: list[str],
        notifier: TelegramNotifier,
        tracker: OrderTracker,
    ):
        self.bot_id = bot_id
        self.mode = bot_cfg.get("mode", "futures")
        self.config = bot_cfg
        self.api = api
        self.candle_manager = candle_manager
        self.symbols = symbols
        self.timeframes = timeframes
        self.notifier = notifier
        self.tracker = tracker
        self.logger = setup_logger(f"bot.{bot_id}")
        self.running = False

        # 每個 bot 獨立的策略 + 風控 + 執行器
        # 把 bot 的 strategy/risk 摘出當作 PM/Aggregator 期望的扁平 config
        flat_cfg = {
            "strategy": bot_cfg.get("strategy", {}),
            "risk": bot_cfg.get("risk", {}),
            "leverage": bot_cfg.get("leverage", 1),
            "only_long": bot_cfg.get("only_long", False),
            "margin_type": bot_cfg.get("margin_type", "CROSSED"),
        }
        self.aggregator = SignalAggregator(flat_cfg)
        self.position_manager = PositionManager(flat_cfg)
        self.executor = OrderExecutor(
            api=api,
            position_manager=self.position_manager,
            config=flat_cfg,
            mode=self.mode,
            bot_id=bot_id,
        )

    async def init(self):
        await self.executor.init_symbol_info(self.symbols)
        balance = await self._get_balance()

        bot_state = BotState(
            bot_id=self.bot_id,
            mode=self.mode,
            enabled=True,
            leverage=self.config.get("leverage", 1),
            started_at=datetime.now(timezone.utc).isoformat(),
            balance=balance,
            bot_ref=self,
        )
        state.bots[self.bot_id] = bot_state

        # 啟動時同步 Binance 既有持倉到本地 PM（避免孤兒倉）
        await self._sync_existing_positions()

        # 回填當日風控狀態，避免重啟繞過每日虧損牆／連敗熔斷
        self._restore_risk_state()

        self.logger.info("[%s] 初始化完成 mode=%s 餘額=%.2f", self.bot_id, self.mode, balance)
        return balance

    def _restore_risk_state(self):
        """從 DB 還原當日已實現損益、交易筆數與連敗數。"""
        if not self.tracker:
            return
        try:
            stats = self.tracker.get_today_stats(self.bot_id) or {}
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # 連敗數只計今日（與 reset_daily 的每日歸零語意一致）
            consecutive = 0
            for t in self.tracker.get_recent_trades(self.bot_id, limit=50):
                if not (t.get("exit_time") or "").startswith(today):
                    break
                pnl = t.get("pnl")
                if pnl is None or pnl == 0:
                    continue          # 平盤不影響連敗串
                if pnl < 0:
                    consecutive += 1
                else:
                    break             # 遇到獲利即中斷
            self.position_manager.capital_manager.restore_daily_state(
                daily_pnl=stats.get("pnl", 0.0),
                daily_trades=stats.get("trades", 0),
                consecutive_losses=consecutive,
            )
        except Exception as e:  # noqa: BLE001 — 還原失敗不應阻斷啟動
            self.logger.warning("[%s] 風控狀態還原失敗，以零值啟動: %s", self.bot_id, e)

    async def _sync_existing_positions(self):
        """啟動三向對帳：Binance 實際倉、本地 PM、DB OPEN 紀錄三者一致"""
        if self.mode != "futures":
            return
        try:
            existing = await self.api.get_open_positions()
        except Exception as e:
            self.logger.warning("[%s] 取得 Binance 持倉失敗: %s", self.bot_id, e)
            existing = []

        existing_by_symbol = {p["symbol"]: p for p in existing}

        # 1. DB 中本 bot 的所有 OPEN 紀錄
        db_open = self.tracker.get_open_trades(bot_id=self.bot_id)

        # 2. Binance 上有的 → 灌回本地 PM（並認領 DB 上對應 trade_id）
        from src.indicators.atr import get_current_atr
        for symbol, pos in existing_by_symbol.items():
            if symbol not in self.symbols:
                self.logger.info(
                    "[%s] 略過孤兒倉 %s（不在本 bot 交易對）", self.bot_id, symbol,
                )
                continue
            if self.position_manager.has_position(symbol):
                continue
            df = self.candle_manager.get_candles(symbol, "5m")
            atr = get_current_atr(df) if df is not None else None

            # 嘗試找對應的 DB OPEN 紀錄（同 bot+symbol 最新一筆）
            matching = [
                t for t in db_open
                if t["symbol"] == symbol and t["side"] == pos["side"]
            ]
            trade_id = matching[0]["id"] if matching else None

            self.position_manager.open_position(
                symbol=symbol,
                side=pos["side"],
                entry_price=pos["entry_price"],
                quantity=pos["quantity"],
                leverage=pos.get("leverage", self.config.get("leverage", 1)),
                atr=atr,
                trade_id=trade_id,
            )
            self.logger.warning(
                "[%s] 🔄 同步既有倉 %s %s qty=%s entry=%.4f trade_id=%s",
                self.bot_id, symbol, pos["side"], pos["quantity"], pos["entry_price"], trade_id,
            )

        # 3. DB 中 OPEN 但 Binance 沒有的 → 標記為「不一致已平」
        for trade in db_open:
            if trade["symbol"] not in existing_by_symbol:
                try:
                    last_price = await self.api.get_ticker_price(trade["symbol"])
                except Exception:
                    last_price = trade["entry_price"]
                # 估算 PnL（無法精確，因為實際出場價未知）
                qty = trade["quantity"]
                entry = trade["entry_price"]
                if trade["side"] == "LONG":
                    pnl = (last_price - entry) * qty
                    pnl_pct = (last_price - entry) / entry * 100
                else:
                    pnl = (entry - last_price) * qty
                    pnl_pct = (entry - last_price) / entry * 100
                self.tracker.record_close(
                    trade_id=trade["id"],
                    symbol=trade["symbol"],
                    exit_price=last_price,
                    pnl=round(pnl, 4),
                    pnl_pct=round(pnl_pct, 2),
                    bot_id=self.bot_id,
                    reason="啟動對帳：Binance 已無倉，DB 估算平倉",
                )
                self.logger.warning(
                    "[%s] 🧹 清理殘留 DB OPEN: trade_id=%d %s 估 pnl=%.2f",
                    self.bot_id, trade["id"], trade["symbol"], pnl,
                )

    async def _get_balance(self) -> float:
        try:
            if self.mode == "futures":
                return await self.api.get_usdt_balance()
            return await self.api.get_spot_usdt_balance()
        except Exception as e:
            self.logger.warning("[%s] 取餘額失敗: %s", self.bot_id, e)
            return 0.0

    async def on_kline_close(self, symbol: str, candles: dict, funding_rate: float,
                             sentiment=None):
        """K 線收盤時的策略評估"""
        bot_state = state.bots[self.bot_id]
        # 外部訊號（TradingView webhook）；過期者 get() 會回 None
        external = ext_store.get(symbol)
        signal = self.aggregator.evaluate(
            symbol, candles, funding_rate, sentiment, external,
        )

        # 現貨過濾 SHORT
        if self.config.get("only_long") and signal.type == SignalType.SHORT:
            signal.type = SignalType.NEUTRAL
            signal.reasons = ["現貨僅做多，忽略空頭訊號"]

        signal_payload = {
            "bot_id": self.bot_id,
            "symbol": symbol,
            "type": signal.type.value,
            "strength": signal.strength,
            "actionable": signal.is_actionable,
            "reasons": signal.reasons[:5],
        }
        bot_state.last_signals[symbol] = signal_payload
        await bus.publish(f"signal.{self.bot_id}", signal_payload)

        if signal.is_actionable:
            self.logger.info(
                "📍 [%s] %s %s 強度=%.1f",
                self.bot_id, signal.type.value, symbol, signal.strength,
            )
            if bot_state.paused:
                self.logger.info("[%s] 已暫停進場", self.bot_id)
                return
            await self._handle_signal(signal)

    async def _handle_signal(self, signal):
        try:
            balance = await self._get_balance()
            state.bots[self.bot_id].balance = balance

            main_df = self.candle_manager.get_candles(signal.symbol, "5m")
            result = await self.executor.execute_signal(
                signal=signal, balance=balance, candles_df=main_df,
            )
            if result:
                trade_id = self.tracker.record_open(result)
                # 把 trade_id 寫回 Position 供平倉精確對應
                pos = self.position_manager.get_position(result["symbol"])
                if pos:
                    pos.trade_id = trade_id
                result["trade_id"] = trade_id
                await self.notifier.notify_open(result)
                await bus.publish(f"order_open.{self.bot_id}", result)
        except Exception as e:
            self.logger.error("[%s] 處理訊號失敗: %s", self.bot_id, e)

    async def force_trade(self, symbol: str, side: str, quantity: float | None = None) -> dict | None:
        """強制下單 — 跳過訊號條件，用於 Testnet 測試管線"""
        # 雙層保險：bot 層也擋 live 環境，避免任何呼叫者繞過 web 端 guard
        if not state.testnet:
            self.logger.error("[%s] 強制下單僅允許在 Testnet", self.bot_id)
            return None

        from src.strategy.base_strategy import Signal
        sig_type = SignalType.LONG if side == "LONG" else SignalType.SHORT
        if self.config.get("only_long") and sig_type == SignalType.SHORT:
            self.logger.warning("[%s] 現貨不可做空", self.bot_id)
            return None

        price = await self.api.get_ticker_price(symbol)
        balance = await self._get_balance()

        signal = Signal(
            type=sig_type, symbol=symbol, strength=999,
            reasons=["強制下單測試"], price=price,
        )
        candles = self.candle_manager.get_candles(symbol, "5m")
        result = await self.executor.execute_signal(
            signal=signal, balance=balance, candles_df=candles,
        )
        if result:
            trade_id = self.tracker.record_open(result)
            pos = self.position_manager.get_position(result["symbol"])
            if pos:
                pos.trade_id = trade_id
            result["trade_id"] = trade_id
            await bus.publish(f"order_open.{self.bot_id}", result)
        return result

    async def balance_loop(self):
        """每 60 秒刷新餘額 — 原本只在啟動讀一次，啟動瞬間 API 失敗會讓
        儀表板永遠顯示 0.00 直到下一筆交易"""
        while self.running:
            await asyncio.sleep(60)
            try:
                if self.mode == "futures":
                    balance = await self.api.get_usdt_balance()
                else:
                    balance = await self.api.get_spot_usdt_balance()
            except Exception as e:  # noqa: BLE001 — 失敗保留舊值，不覆蓋成 0
                self.logger.debug("[%s] 餘額刷新失敗（保留舊值）: %s", self.bot_id, e)
                continue
            bot_state = state.bots.get(self.bot_id)
            if bot_state:
                bot_state.balance = balance

    async def risk_loop(self):
        """每秒檢查持倉風控"""
        while self.running:
            try:
                # 行情過期 → 暫停風控避免在舊價格上誤觸發
                ws = state.ws_feed_ref
                if ws and ws.is_stale(max_age_seconds=120):
                    self.logger.warning(
                        "[%s] WebSocket 行情已過期，暫停風控檢查", self.bot_id,
                    )
                    await asyncio.sleep(5)
                    continue

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
                                symbol=symbol, quantity=qty, reason=reason,
                            )
                            if result:
                                # 部分平倉不寫 DB（已實現損益累積在倉位上，
                                # 最終全平時一次記錄），只廣播事件
                                if result.get("partial"):
                                    await bus.publish(
                                        f"partial_close.{self.bot_id}", result,
                                    )
                                    continue
                                self.tracker.record_close(
                                    trade_id=result.get("trade_id"),
                                    symbol=symbol,
                                    exit_price=result["exit_price"],
                                    pnl=result["pnl"],
                                    pnl_pct=result["pnl_pct"],
                                    bot_id=self.bot_id,
                                    reason=reason,
                                )
                                await self.notifier.notify_close(result)
                                await bus.publish(f"order_close.{self.bot_id}", result)
            except Exception as e:
                self.logger.error("[%s] 風控錯誤: %s", self.bot_id, e)
            await asyncio.sleep(1)

    async def reconcile_loop(self):
        """每 30 秒對帳：本地持倉 vs Binance 實際持倉。
        若 Binance 已無倉但本地仍有（停損市價單在交易所被觸發），
        把本地視為平倉並寫入 tracker。
        """
        if self.mode != "futures":
            return
        while self.running:
            try:
                exch_positions = await self.api.get_open_positions()
                # 更新共享 state 供 web 端讀真實爆倉價
                state.exchange_positions = {
                    p["symbol"]: p for p in exch_positions
                }
                exch_symbols = set(state.exchange_positions.keys())
                for symbol in list(self.position_manager.all_positions.keys()):
                    if symbol in exch_symbols:
                        # 兩邊都有 → 校正數量差（maker TP 掛單成交 / 批次進場成交）
                        exch_qty = state.exchange_positions[symbol]["quantity"]
                        est_price = self.candle_manager.get_latest_price(symbol)
                        if est_price is None:
                            try:
                                est_price = await self.api.get_ticker_price(symbol)
                            except Exception:
                                continue
                        partial = self.position_manager.sync_external_fill(
                            symbol, exch_qty, est_price,
                        )
                        if partial:
                            partial.update({
                                "reason": "階梯停利(maker 掛單成交)",
                                "bot_id": self.bot_id,
                                "mode": self.mode,
                            })
                            self.logger.info(
                                "[%s] 🎯 對帳入帳 TP 掛單成交 %s qty=%.4f pnl=%.4f",
                                self.bot_id, symbol, partial["quantity"], partial["pnl"],
                            )
                            await bus.publish(f"partial_close.{self.bot_id}", partial)
                        continue
                    # Binance 上已沒這個倉位 → 視為已被交易所端停損平掉
                    pos = self.position_manager.get_position(symbol)
                    if not pos:
                        continue
                    async with self.position_manager.lock_for(symbol):
                        # double-check inside lock
                        if not self.position_manager.has_position(symbol):
                            continue
                        try:
                            exit_price = await self.api.get_ticker_price(symbol)
                        except Exception:
                            exit_price = pos.entry_price
                        result = self.position_manager.close_position(symbol, exit_price)
                    if result:
                        result.update({
                            "reason": "交易所端觸發（停損/手動）",
                            "bot_id": self.bot_id,
                            "mode": self.mode,
                        })
                        self.tracker.record_close(
                            trade_id=result.get("trade_id"),
                            symbol=symbol,
                            exit_price=result["exit_price"],
                            pnl=result["pnl"],
                            pnl_pct=result["pnl_pct"],
                            bot_id=self.bot_id,
                            reason=result["reason"],
                        )
                        self.logger.warning(
                            "[%s] 🔄 對帳同步平倉 %s pnl=%.4f", self.bot_id, symbol, result["pnl"],
                        )
                        await self.notifier.notify_close(result)
                        await bus.publish(f"order_close.{self.bot_id}", result)
            except Exception as e:
                self.logger.warning("[%s] 對帳錯誤: %s", self.bot_id, e)
            await asyncio.sleep(30)

    def stop(self):
        self.running = False


class TradeOrchestrator:
    """協調多個 bot 共享 K 線資料、各自下單"""

    def __init__(self, config: dict):
        self.config = config
        self.api = BinanceAPI(
            api_key=config["binance"]["api_key"],
            api_secret=config["binance"]["api_secret"],
            testnet=config["binance"]["testnet"],
        )
        self.candle_manager = CandleManager()
        self.symbols = config.get("trading", {}).get("symbols", ["BTCUSDT"])
        self.timeframes = config.get("trading", {}).get("timeframes", ["5m", "15m", "1h"])

        # 共用市場情緒提供者（F&G + 新聞），所有 bot 共用同一份快取
        self.sentiment_provider = MarketSentimentProvider(config)

        # 共用通知 / DB tracker
        tg = config.get("telegram", {})
        self.notifier = TelegramNotifier(
            bot_token=tg.get("bot_token", ""),
            chat_id=tg.get("chat_id", ""),
            enabled=config.get("notification", {}).get("enabled", True),
        )
        db_url = config.get("database", {}).get("url", "sqlite:///cryptotrade.db")
        sync_url = db_url.replace("+aiosqlite", "")
        session_factory = init_db(sync_url)
        self.tracker = OrderTracker(session_factory)

        # 建立 bots
        self.bots: dict[str, TradeBot] = {}
        bots_cfg = config.get("bots", {})
        for bot_id, bot_cfg in bots_cfg.items():
            if not bot_cfg.get("enabled", True):
                logger.info("[%s] 已停用，跳過", bot_id)
                continue
            self.bots[bot_id] = TradeBot(
                bot_id=bot_id,
                bot_cfg=bot_cfg,
                api=self.api,
                candle_manager=self.candle_manager,
                symbols=self.symbols,
                timeframes=self.timeframes,
                notifier=self.notifier,
                tracker=self.tracker,
            )

        self.ws_feed: WebSocketFeed | None = None
        self.running = False

    async def start(self):
        logger.info("=" * 50)
        logger.info("  CryptoTrade Orchestrator 啟動 (bots=%s)", list(self.bots.keys()))
        logger.info("=" * 50)

        await self.api.connect()
        await self._import_exchange_history()
        await self._load_historical_candles()

        # 寫共享行情狀態
        state.testnet = self.config["binance"]["testnet"]
        state.symbols = self.symbols
        state.timeframes = self.timeframes
        state.candle_manager_ref = self.candle_manager

        for bot in self.bots.values():
            await bot.init()
            bot.running = True
            asyncio.create_task(bot.risk_loop())
            asyncio.create_task(bot.reconcile_loop())
            asyncio.create_task(bot.balance_loop())

        # 啟 WebSocket — market 由「是否有 futures bot」決定
        has_futures_bot = any(b.mode == "futures" for b in self.bots.values())
        ws_market = "futures" if has_futures_bot else "spot"
        self.ws_feed = WebSocketFeed(self.api.client, market=ws_market)
        state.ws_feed_ref = self.ws_feed
        await self.ws_feed.start()
        self.ws_feed.on_kline(self._on_kline)
        for sym in self.symbols:
            for tf in self.timeframes:
                self.ws_feed.subscribe_kline(sym, tf)

        bot_summary = ", ".join(
            f"{bid}({b.balance:.0f}USDT)" for bid, b in state.bots.items()
        )
        await self.notifier.send(
            f"🚀 *CryptoTrade 已啟動*\n"
            f"模式: {'Testnet' if state.testnet else '正式環境'}\n"
            f"Bots: {bot_summary}\n"
            f"交易對: {', '.join(self.symbols)}"
        )

        self.running = True
        state.engine_status = {
            "phase": "running", "error": None,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("Orchestrator 已啟動，監聽行情中...")
        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        was_running = self.running
        self.running = False
        for bot in self.bots.values():
            bot.stop()
        if self.ws_feed:
            await self.ws_feed.stop()
        await self.api.disconnect()
        # 啟動失敗後的清理不發通知，避免退避重試循環對 Telegram 洗版
        if was_running:
            await self.notifier.send("🛑 *CryptoTrade 已停止*")

    async def _import_exchange_history(self):
        """啟動時從交易所回補成交紀錄 —— 資料庫歸零 / 本地雲端切換後績效不斷層"""
        import os
        try:
            days = int(os.environ.get("IMPORT_EXCHANGE_TRADES_DAYS", "30"))
        except ValueError:
            days = 30
        if days <= 0:
            return
        from src.execution.exchange_import import import_trades
        try:
            stats = await import_trades(
                self.api, self.tracker.session_factory, list(self.symbols), days,
            )
            if stats["inserted"]:
                logger.info("📥 已從交易所回補 %d 筆歷史交易（%d 天）", stats["inserted"], days)
        except Exception as e:  # noqa: BLE001 — 回補失敗不應阻斷交易
            logger.warning("交易所歷史回補失敗（不影響交易）: %s", e)

    async def _load_historical_candles(self):
        failed = []
        for sym in self.symbols:
            for tf in self.timeframes:
                try:
                    klines = await self.api.get_klines(sym, tf, limit=500)
                    self.candle_manager.init_from_klines(sym, tf, klines)
                except Exception as e:
                    logger.error("載入 %s %s K線失敗: %s", sym, tf, e)
                    failed.append((sym, tf))
        if failed:
            # 主時框（5m/15m）失敗的「標的」自動剔除，其餘標的照常交易 ——
            # 使用者可自由增減 symbols，單一標的打錯字/未上市不應癱瘓整台 bot
            bad_symbols = {pair[0] for pair in failed if pair[1] in ("5m", "15m")}
            if bad_symbols:
                # 就地修改：bots 在建構時持有同一個 list 參照，需一併生效
                self.symbols[:] = [s for s in self.symbols if s not in bad_symbols]
                logger.warning(
                    "⚠️ 剔除無法載入關鍵K線的標的: %s（剩餘: %s）",
                    sorted(bad_symbols), self.symbols,
                )
            if not self.symbols:
                raise RuntimeError(
                    f"所有標的的關鍵 K 線都載入失敗，拒絕啟動: {sorted(bad_symbols)}"
                )

    async def _on_kline(self, data: dict):
        symbol = data["symbol"]
        interval = data["interval"]
        self.candle_manager.update_candle(symbol, interval, data)
        state.last_prices[symbol] = data["close"]
        await bus.publish("kline", data)

        if not data["is_closed"]:
            return

        candles = {}
        for tf in self.timeframes:
            df = self.candle_manager.get_candles(symbol, tf)
            if df is not None:
                candles[tf] = df
        if not candles:
            return

        try:
            funding_rate = await self.api.get_funding_rate(symbol)
        except Exception:
            funding_rate = 0.0
        state.last_funding[symbol] = funding_rate

        # 消息面/市場情緒（TTL 快取，抓取失敗自動退回中性）
        try:
            sentiment = await self.sentiment_provider.get(symbol)
        except Exception as e:  # noqa: BLE001 — 情緒層絕不阻斷交易
            logger.warning("情緒取得失敗，退回中性: %s", e)
            sentiment = None
        if sentiment is not None:
            state.last_sentiment[symbol] = sentiment.to_dict()

        # 廣播給所有 bot
        for bot in self.bots.values():
            await bot.on_kline_close(symbol, candles, funding_rate, sentiment)


RETRY_DELAY_SECONDS = 60


async def main():
    config = load_config()
    log_cfg = config.get("logging", {})
    setup_logger("cryptotrade", log_cfg.get("level", "INFO"), log_cfg.get("file"))

    def _mark(phase: str, error: str | None = None, attempts: int = 0):
        state.engine_status = {
            "phase": phase, "error": error,
            "ts": datetime.now(timezone.utc).isoformat(), "attempts": attempts,
        }

    # 交易引擎建構失敗（如 DB 問題）不應阻止儀表板啟動 —— 保持可觀測
    orchestrator: TradeOrchestrator | None = None
    try:
        orchestrator = TradeOrchestrator(config)
        state.api_ref = orchestrator.api
    except Exception as e:  # noqa: BLE001
        logger.error("交易引擎建構失敗，稍後重試: %s", e)
        _mark("retrying", f"建構失敗: {e}")

    web_cfg = config.get("web", {})
    web_enabled = web_cfg.get("enabled", True)
    if web_enabled:
        app = create_app(tracker=orchestrator.tracker if orchestrator else None)
        web_task = asyncio.create_task(serve_web(
            app,
            host=web_cfg.get("host", "0.0.0.0"),
            port=int(web_cfg.get("port", 8788)),
        ))
        logger.info("Web Dashboard: http://%s:%d", web_cfg.get("host", "0.0.0.0"), web_cfg.get("port", 8788))
    else:
        web_task = None

    # 引擎失敗（交易所斷線 / 金鑰失效 / 測試網重置）→ 記錄後退避重試，
    # 絕不讓整個程序退出：程序退出會拖垮儀表板並讓平台陷入重啟循環(503)
    attempts = 0
    try:
        while True:
            if orchestrator is None:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                try:
                    orchestrator = TradeOrchestrator(config)
                    state.api_ref = orchestrator.api
                except Exception as e:  # noqa: BLE001
                    attempts += 1
                    logger.error("交易引擎重建失敗，%d 秒後重試: %s", RETRY_DELAY_SECONDS, e)
                    _mark("retrying", f"重建失敗: {e}", attempts)
                    orchestrator = None
                continue
            try:
                _mark("starting", attempts=attempts)
                await orchestrator.start()
                break  # stop() 正常結束
            except (KeyboardInterrupt, asyncio.CancelledError):
                await orchestrator.stop()
                break
            except Exception as e:  # noqa: BLE001
                attempts += 1
                logger.error(
                    "⚠️ 交易引擎異常（%s），%d 秒後重試。儀表板持續運作中",
                    e, RETRY_DELAY_SECONDS,
                )
                _mark("retrying", str(e), attempts)
                try:
                    await orchestrator.stop()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                try:
                    orchestrator = TradeOrchestrator(config)
                    state.api_ref = orchestrator.api
                except Exception as e2:  # noqa: BLE001
                    logger.error("交易引擎重建失敗: %s", e2)
                    _mark("retrying", f"重建失敗: {e2}", attempts)
                    orchestrator = None
    finally:
        if web_task:
            web_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
