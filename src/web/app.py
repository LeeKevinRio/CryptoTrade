"""FastAPI Web Dashboard — REST + WebSocket（多 bot）"""

import json
import os
import secrets
import sys
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.web.event_bus import bus
from src.web.state import state
from src.indicators.rsi import get_current_rsi
from src.indicators.bollinger import calculate_bollinger, get_bb_position
from src.indicators.macd import calculate_macd
from src.indicators.atr import get_current_atr
from src.indicators.volume_profile import volume_ratio
from src.utils.logger import setup_logger

logger = setup_logger("web")

STATIC_DIR = Path(__file__).parent / "static"

# 維持保證金率（粗估，用於爆倉價計算）
MAINTENANCE_MARGIN_RATE = 0.005


def _liquidation_price(side: str, entry: float, leverage: int) -> float:
    """估算爆倉價（孤立保證金，不含資金費率與已實現盈虧）"""
    if leverage <= 1 or entry <= 0:
        return 0.0
    margin_ratio = 1.0 / leverage
    if side == "LONG":
        return entry * (1 - margin_ratio + MAINTENANCE_MARGIN_RATE)
    return entry * (1 + margin_ratio - MAINTENANCE_MARGIN_RATE)


def _bot_or_404(bot_id: str):
    bot_state = state.bots.get(bot_id)
    if not bot_state or not bot_state.bot_ref:
        raise HTTPException(404, f"未知或未啟動的 bot: {bot_id}")
    return bot_state


def _make_auth_dependency():
    """所有 POST 動作端點需要 token；token 來自環境變數 WEB_AUTH_TOKEN
    若未設定，產生隨機 token 並列印（首次啟動）。
    """
    token = os.environ.get("WEB_AUTH_TOKEN")
    if not token:
        token = secrets.token_urlsafe(24)
        os.environ["WEB_AUTH_TOKEN"] = token
        msg = (
            "=" * 60 + "\n"
            "[WARN] 未設定 WEB_AUTH_TOKEN，已產生臨時 token：\n"
            f"     {token}\n"
            "   把這個值存到 .env 的 WEB_AUTH_TOKEN，前端送 X-Auth-Token header\n"
            + "=" * 60
        )
        try:
            print(msg)
        except UnicodeEncodeError:
            sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))

    async def verify(x_auth_token: str | None = Header(default=None)):
        if not x_auth_token or not secrets.compare_digest(x_auth_token, token):
            raise HTTPException(401, "未授權：缺少或錯誤的 X-Auth-Token")
    return verify


def create_app(tracker=None) -> FastAPI:
    # 關閉預設的 docs/openapi 暴露面
    app = FastAPI(
        title="CryptoTrade Dashboard",
        docs_url=None, redoc_url=None, openapi_url=None,
    )
    require_auth = _make_auth_dependency()

    # ── 全域狀態 ─────────────────────────────────

    @app.get("/api/status")
    async def get_status():
        return {
            "testnet": state.testnet,
            "symbols": state.symbols,
            "timeframes": state.timeframes,
            "bots": [
                {
                    "bot_id": b.bot_id,
                    "mode": b.mode,
                    "leverage": b.leverage,
                    "balance": b.balance,
                    "paused": b.paused,
                    "started_at": b.started_at,
                }
                for b in state.bots.values()
            ],
        }

    @app.get("/api/overview")
    async def get_overview():
        """總覽 — 跨 bot 彙總"""
        total_balance = sum(b.balance for b in state.bots.values())
        today_total = {"trades": 0, "wins": 0, "pnl": 0.0}
        if tracker:
            stats = tracker.get_today_stats() or {}
            today_total = {
                "trades": stats.get("trades", 0),
                "wins": stats.get("wins", 0),
                "pnl": stats.get("pnl", 0.0),
                "win_rate": stats.get("win_rate", 0),
            }
        return {
            "total_balance": round(total_balance, 2),
            "today": today_total,
            "bots": [
                {
                    "bot_id": b.bot_id,
                    "mode": b.mode,
                    "balance": b.balance,
                    "open_positions": len(b.bot_ref.position_manager.all_positions) if b.bot_ref else 0,
                    "paused": b.paused,
                }
                for b in state.bots.values()
            ],
        }

    # ── K 線（共用） ────────────────────────────

    @app.get("/api/candles/{symbol}/{interval}")
    async def get_candles(symbol: str, interval: str, limit: int = 200):
        cm = state.candle_manager_ref
        if cm is None:
            raise HTTPException(404, "candle manager 未啟動")
        df = cm.get_candles(symbol, interval)
        if df is None:
            raise HTTPException(404, f"無 {symbol} {interval} 資料")
        df = df.tail(limit)
        return [
            {
                "time": int(row.Index.timestamp()),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
            }
            for row in df.itertuples()
        ]

    # ── Bot-scoped 端點 ─────────────────────────

    @app.get("/api/bots/{bot_id}/positions")
    async def get_positions(bot_id: str):
        bot_state = _bot_or_404(bot_id)
        bot = bot_state.bot_ref
        pm = bot.position_manager

        out = []
        for sym, pos in pm.all_positions.items():
            current = state.last_prices.get(sym, pos.entry_price)
            if pos.side == "LONG":
                pnl = (current - pos.entry_price) * pos.quantity
                pnl_pct = (current - pos.entry_price) / pos.entry_price * 100
            else:
                pnl = (pos.entry_price - current) * pos.quantity
                pnl_pct = (pos.entry_price - current) / pos.entry_price * 100

            sl_data = pm.sl_manager._stops.get(sym, {})
            sl_price = sl_data.get("stop_price")
            sl_pct = sl_data.get("stop_pct")
            sl_distance_pct = (
                abs(current - sl_price) / current * 100 if sl_price else None
            )

            tp_state = pm.tp_manager._states.get(sym)
            next_tp = None
            tp_remaining_pct = None
            trailing_active = False
            highest_pnl_pct = 0.0
            hold_seconds = 0
            if tp_state:
                trailing_active = tp_state.trailing_active
                highest_pnl_pct = round(tp_state.highest_pnl_pct, 2)
                hold_seconds = int(time.time() - tp_state.entry_time)
                tp_remaining_pct = round(
                    tp_state.remaining_quantity / tp_state.total_quantity * 100, 1,
                )
                for level in tp_state.levels:
                    if not level.triggered:
                        if pos.side == "LONG":
                            target = pos.entry_price * (1 + level.pct / 100)
                        else:
                            target = pos.entry_price * (1 - level.pct / 100)
                        next_tp = {
                            "level_pct": level.pct,
                            "close_pct": level.close_pct,
                            "price": round(target, 4),
                            "distance_pct": round(abs(target - current) / current * 100, 2),
                        }
                        break

            r_multiple = None
            if sl_pct and sl_pct > 0:
                r_multiple = round(pnl_pct / sl_pct, 2)

            liq_price = None
            liq_distance_pct = None
            liq_source = None
            if bot_state.mode == "futures" and pos.leverage > 1:
                # 優先用 Binance 回傳的實際爆倉價
                exch = state.exchange_positions.get(sym)
                if exch and exch.get("liquidation_price"):
                    liq_price = round(exch["liquidation_price"], 4)
                    liq_source = "binance"
                else:
                    liq_price = round(_liquidation_price(pos.side, pos.entry_price, pos.leverage), 4)
                    liq_source = "estimate"
                liq_distance_pct = round(abs(current - liq_price) / current * 100, 2) if current else None

            out.append({
                "bot_id": bot_id,
                "symbol": sym,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "current_price": current,
                "quantity": pos.quantity,
                "leverage": pos.leverage,
                "unrealized_pnl": round(pnl, 4),
                "unrealized_pnl_pct": round(pnl_pct, 2),
                "stop_price": round(sl_price, 4) if sl_price else None,
                "stop_distance_pct": round(sl_distance_pct, 2) if sl_distance_pct is not None else None,
                "stop_pct": sl_pct,
                "next_tp": next_tp,
                "tp_remaining_qty_pct": tp_remaining_pct,
                "trailing_active": trailing_active,
                "highest_pnl_pct": highest_pnl_pct,
                "hold_seconds": hold_seconds,
                "r_multiple": r_multiple,
                "liq_price": liq_price,
                "liq_distance_pct": liq_distance_pct,
                "liq_source": liq_source,
            })
        return out

    @app.get("/api/bots/{bot_id}/signals")
    async def get_signals(bot_id: str):
        bot_state = _bot_or_404(bot_id)
        return list(bot_state.last_signals.values())

    @app.get("/api/bots/{bot_id}/risk_budget")
    async def get_risk_budget(bot_id: str):
        bot_state = _bot_or_404(bot_id)
        cm = bot_state.bot_ref.position_manager.capital_manager
        cm.reset_daily()
        balance = bot_state.balance or 0
        max_loss_usd = balance * (cm.max_daily_loss_pct / 100)
        loss_used_pct = 0.0
        if max_loss_usd > 0 and cm.daily_pnl < 0:
            loss_used_pct = min(100.0, abs(cm.daily_pnl) / max_loss_usd * 100)
        can_trade, reason = cm.can_trade(balance) if balance > 0 else (True, "OK")
        return {
            "bot_id": bot_id,
            "daily_pnl": round(cm.daily_pnl, 4),
            "daily_pnl_pct": round(cm.daily_pnl / balance * 100, 3) if balance else 0,
            "daily_trades": cm.daily_trades,
            "max_daily_trades": cm.max_daily_trades,
            "open_positions": cm.open_positions,
            "max_concurrent": cm.max_concurrent,
            "max_daily_loss_pct": cm.max_daily_loss_pct,
            "max_daily_loss_usd": round(max_loss_usd, 2),
            "loss_budget_used_pct": round(loss_used_pct, 1),
            "can_trade": can_trade,
            "reason": reason,
        }

    @app.get("/api/bots/{bot_id}/indicators/{symbol}")
    async def get_indicators(bot_id: str, symbol: str):
        bot_state = _bot_or_404(bot_id)
        bot = bot_state.bot_ref
        cm = bot.candle_manager
        cfg = bot.config.get("strategy", {})
        dip_cfg = cfg.get("dip_buyer", {})
        short_cfg = cfg.get("short_seller", {})

        thresholds = {
            "rsi_oversold": dip_cfg.get("rsi_oversold", 30),
            "rsi_overbought": short_cfg.get("rsi_overbought", 70),
            "rsi_15m_long": dip_cfg.get("rsi_15m_threshold", 35),
            "rsi_15m_short": short_cfg.get("rsi_15m_threshold", 65),
            "volume_multiplier": dip_cfg.get("volume_multiplier", 1.5),
        }

        out_tfs = {}
        for tf in state.timeframes:
            df = cm.get_candles(symbol, tf)
            if df is None or len(df) < 30:
                continue
            close = float(df["close"].iloc[-1])
            rsi = get_current_rsi(df)
            bb_pos = get_bb_position(df)
            upper, middle, lower = calculate_bollinger(df)
            macd_line, sig_line, hist = calculate_macd(df)
            atr = get_current_atr(df)
            vr = volume_ratio(df)

            def _last(s):
                if s is None or s.empty:
                    return None
                v = s.iloc[-1]
                return None if v != v else round(float(v), 4)

            out_tfs[tf] = {
                "close": close,
                "rsi": round(rsi, 2) if rsi is not None else None,
                "bb_position": round(bb_pos, 3) if bb_pos is not None else None,
                "bb_upper": _last(upper),
                "bb_middle": _last(middle),
                "bb_lower": _last(lower),
                "macd": _last(macd_line),
                "macd_signal": _last(sig_line),
                "macd_hist": _last(hist),
                "atr": round(atr, 4) if atr is not None else None,
                "atr_pct": round(atr / close * 100, 3) if atr is not None and close else None,
                "volume_ratio": round(vr, 2) if vr is not None else None,
            }

        return {
            "bot_id": bot_id,
            "symbol": symbol,
            "funding_rate": state.last_funding.get(symbol, 0.0),
            "timeframes": out_tfs,
            "thresholds": thresholds,
            "last_signal": bot_state.last_signals.get(symbol),
        }

    @app.get("/api/bots/{bot_id}/trades")
    async def get_bot_trades(bot_id: str):
        if not tracker:
            return {"open": [], "today": {}, "recent": []}
        return {
            "open": tracker.get_open_trades(bot_id=bot_id),
            "today": tracker.get_today_stats(bot_id=bot_id),
            "recent": tracker.get_recent_trades(bot_id=bot_id, limit=10),
        }

    @app.get("/api/performance")
    async def get_performance(bot_id: str | None = None, days: int | None = None):
        """績效總表 — 勝率 / 盈虧比 / 期望值 / 分組。
        bot_id 省略=跨 bot 全部；days 省略=全期間。
        """
        if not tracker:
            raise HTTPException(404, "tracker 未啟動")
        return tracker.get_performance(bot_id=bot_id, days=days)

    # ── 動作 ─────────────────────────────────────

    class CloseRequest(BaseModel):
        symbol: str

    class ForceTradeRequest(BaseModel):
        symbol: str
        side: str  # LONG / SHORT

    @app.post("/api/bots/{bot_id}/actions/close", dependencies=[Depends(require_auth)])
    async def close_position(bot_id: str, req: CloseRequest):
        bot_state = _bot_or_404(bot_id)
        bot = bot_state.bot_ref
        pos = bot.position_manager.get_position(req.symbol)
        if not pos:
            raise HTTPException(404, f"{req.symbol} 無持倉")
        result = await bot.executor.close_position(
            symbol=req.symbol, quantity=pos.quantity, reason="手動平倉（Web）",
        )
        if result and tracker:
            tracker.record_close(
                trade_id=result.get("trade_id"),
                symbol=req.symbol,
                exit_price=result["exit_price"],
                pnl=result["pnl"],
                pnl_pct=result["pnl_pct"],
                bot_id=bot_id,
                reason="手動平倉（Web）",
            )
        await bus.publish(f"order_close.{bot_id}", result)
        return {"ok": True, "result": result}

    @app.post("/api/bots/{bot_id}/actions/force_trade", dependencies=[Depends(require_auth)])
    async def force_trade(bot_id: str, req: ForceTradeRequest):
        """強制下單測試 — 跳過訊號條件，僅 Testnet 建議使用"""
        bot_state = _bot_or_404(bot_id)
        if not state.testnet:
            raise HTTPException(403, "強制下單僅允許在 Testnet 使用")
        side = req.side.upper()
        if side not in ("LONG", "SHORT"):
            raise HTTPException(400, "side 必須是 LONG 或 SHORT")
        if req.symbol not in state.symbols:
            raise HTTPException(400, f"symbol {req.symbol} 不在已配置的交易對清單")
        result = await bot_state.bot_ref.force_trade(req.symbol, side)
        if not result:
            raise HTTPException(400, "強制下單失敗（可能違反現貨限制或已有持倉）")
        return {"ok": True, "result": result}

    @app.post("/api/bots/{bot_id}/actions/pause", dependencies=[Depends(require_auth)])
    async def pause(bot_id: str):
        bot_state = _bot_or_404(bot_id)
        bot_state.paused = True
        await bus.publish(f"paused.{bot_id}", {"paused": True})
        return {"paused": True}

    @app.post("/api/bots/{bot_id}/actions/resume", dependencies=[Depends(require_auth)])
    async def resume(bot_id: str):
        bot_state = _bot_or_404(bot_id)
        bot_state.paused = False
        await bus.publish(f"paused.{bot_id}", {"paused": False})
        return {"paused": False}

    # ── WebSocket ─────────────────────────────────

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        queue = await bus.subscribe()
        try:
            for event in bus.recent(limit=20):
                await websocket.send_text(json.dumps(event))
            while True:
                event = await queue.get()
                await websocket.send_text(json.dumps(event))
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning("WebSocket 錯誤: %s", e)
        finally:
            await bus.unsubscribe(queue)

    # ── 靜態檔案 ─────────────────────────────────

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        async def index():
            return FileResponse(str(STATIC_DIR / "index.html"))

    return app


async def serve(app: FastAPI, host: str = "0.0.0.0", port: int = 8788):
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
