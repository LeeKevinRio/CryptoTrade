"""FastAPI Web Dashboard — REST + WebSocket"""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.web.event_bus import bus
from src.web.state import state
from src.utils.logger import setup_logger

logger = setup_logger("web")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(tracker=None) -> FastAPI:
    app = FastAPI(title="CryptoTrade Dashboard")

    # ── REST ─────────────────────────────────────

    @app.get("/api/status")
    async def get_status():
        return {
            "started_at": state.started_at,
            "testnet": state.testnet,
            "paused": state.paused,
            "symbols": state.symbols,
            "timeframes": state.timeframes,
            "balance": state.balance,
        }

    @app.get("/api/positions")
    async def get_positions():
        if not state.bot_ref:
            return []
        positions = []
        for sym, pos in state.bot_ref.position_manager.all_positions.items():
            current = state.last_prices.get(sym, pos.entry_price)
            if pos.side == "LONG":
                pnl = (current - pos.entry_price) * pos.quantity
                pnl_pct = (current - pos.entry_price) / pos.entry_price * 100
            else:
                pnl = (pos.entry_price - current) * pos.quantity
                pnl_pct = (pos.entry_price - current) / pos.entry_price * 100
            positions.append({
                "symbol": sym,
                "side": pos.side,
                "entry_price": pos.entry_price,
                "current_price": current,
                "quantity": pos.quantity,
                "leverage": pos.leverage,
                "unrealized_pnl": round(pnl, 4),
                "unrealized_pnl_pct": round(pnl_pct, 2),
            })
        return positions

    @app.get("/api/signals")
    async def get_signals():
        return list(state.last_signals.values())

    @app.get("/api/trades")
    async def get_trades(limit: int = 50):
        if not tracker:
            return {"open": [], "today": {}}
        return {
            "open": tracker.get_open_trades(),
            "today": tracker.get_today_stats(),
        }

    @app.get("/api/candles/{symbol}/{interval}")
    async def get_candles(symbol: str, interval: str, limit: int = 200):
        if not state.bot_ref:
            raise HTTPException(404, "bot 未啟動")
        df = state.bot_ref.candle_manager.get_candles(symbol, interval)
        if df is None:
            raise HTTPException(404, f"無 {symbol} {interval} 資料")
        df = df.tail(limit)
        return [
            {
                "time": int(row.timestamp / 1000),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
                "volume": float(row.volume),
            }
            for row in df.itertuples()
        ]

    # ── 動作 ─────────────────────────────────────

    class CloseRequest(BaseModel):
        symbol: str

    @app.post("/api/actions/close")
    async def close_position(req: CloseRequest):
        if not state.bot_ref:
            raise HTTPException(503, "bot 未啟動")
        pos = state.bot_ref.position_manager.get_position(req.symbol)
        if not pos:
            raise HTTPException(404, f"{req.symbol} 無持倉")
        result = await state.bot_ref.executor.close_position(
            symbol=req.symbol,
            quantity=pos.quantity,
            reason="手動平倉（Web）",
        )
        if result and tracker:
            tracker.record_close(
                symbol=req.symbol,
                exit_price=result["exit_price"],
                pnl=result["pnl"],
                pnl_pct=result["pnl_pct"],
            )
        await bus.publish("manual_close", {"symbol": req.symbol, "result": result})
        return {"ok": True, "result": result}

    @app.post("/api/actions/pause")
    async def pause():
        state.paused = True
        await bus.publish("paused", {"paused": True})
        return {"paused": True}

    @app.post("/api/actions/resume")
    async def resume():
        state.paused = False
        await bus.publish("paused", {"paused": False})
        return {"paused": False}

    # ── WebSocket ─────────────────────────────────

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        queue = await bus.subscribe()
        try:
            # 推送歷史事件讓剛連線的前端有初始狀態
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


async def serve(app: FastAPI, host: str = "0.0.0.0", port: int = 8000):
    """於現有 asyncio loop 內啟動 uvicorn"""
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
