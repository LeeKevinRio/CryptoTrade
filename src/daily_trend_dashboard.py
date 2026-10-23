"""讓日線均線機器人沿用原本的儀表板（src/web/app.py + static/ 前端），不另做新頁面

舊儀表板讀的是 src.web.state 與「bot_ref」上的 position_manager / executor / candle_manager。
這裡用輕量轉接物件把日線機器人的狀態填進去：
    合約分頁    → 持倉（交易所實際部位）、餘額、訊號（離 50 日均線多遠）、日 K 圖、指標
    總覽／績效  → 同一個資料庫（買進／賣出會寫入，與舊機器人共用 bot_id "futures" 分頁）
    暫停／恢復  → 暫停期間不買進新倉（賣出照常，避免該出場時不出場）
    手動平倉    → reduce-only 市價賣出並記錄
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from src.data.candle_manager import CandleManager
from src.risk.position_manager import Position
from src.web.state import BotState, state

BOT_ID = "futures"          # 舊前端的分頁固定是 spot / futures
STRATEGY = "sma50_long"


class _Capital:
    """風險預算面板需要的欄位；本策略沒有每日停損等限制，填入不會誤導的值"""

    def __init__(self, n_symbols: int):
        self.max_concurrent = n_symbols
        self.open_positions = 0
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.max_daily_trades = n_symbols * 2
        self.max_daily_loss_pct = 100.0
        self.consecutive_losses = 0
        self.max_consecutive_losses = 0

    def reset_daily(self):
        pass

    def can_trade(self, balance):
        paused = state.bots.get(BOT_ID) and state.bots[BOT_ID].paused
        return (False, "已暫停（不買進新倉）") if paused else (True, "OK")


class _PositionManager:
    def __init__(self, n_symbols: int):
        self.all_positions: dict[str, Position] = {}
        self.sl_manager = SimpleNamespace(_stops={})
        self.tp_manager = SimpleNamespace(_states={})
        self.capital_manager = _Capital(n_symbols)

    def get_position(self, symbol):
        return self.all_positions.get(symbol)

    def has_position(self, symbol):
        return symbol in self.all_positions


class _Executor:
    def __init__(self, api, tracker):
        self.api, self.tracker = api, tracker

    async def close_position(self, symbol: str, quantity: float, reason: str = ""):
        pos = state.bots[BOT_ID].bot_ref.position_manager.get_position(symbol)
        await self.api.futures_market_order(symbol, "SELL", quantity, reduce_only=True)
        price = await self.api.get_ticker_price(symbol)
        entry = pos.entry_price if pos else price
        return {"symbol": symbol, "trade_id": None, "exit_price": price,
                "pnl": round((price - entry) * quantity, 4),
                "pnl_pct": round((price / entry - 1) * 100, 2) if entry else 0.0}


class DailyTrendRef:
    """舊儀表板期待的 bot 物件"""

    def __init__(self, api, cfg: dict, tracker):
        self.config = {"strategy": {}}
        self.candle_manager = CandleManager()
        self.position_manager = _PositionManager(len(cfg["symbols"]))
        self.executor = _Executor(api, tracker)

    async def force_trade(self, symbol, side):
        return None          # 本策略不提供強制下單


def attach(api, cfg: dict, tracker, testnet: bool) -> DailyTrendRef:
    ref = DailyTrendRef(api, cfg, tracker)
    state.testnet = testnet
    state.symbols = list(cfg["symbols"])
    state.timeframes = ["1d"]
    state.candle_manager_ref = ref.candle_manager
    state.api_ref = api
    state.bots[BOT_ID] = BotState(bot_id=BOT_ID, mode="futures", leverage=int(cfg["leverage"]),
                                  started_at=datetime.now(timezone.utc).isoformat(), bot_ref=ref)
    state.engine_status = {"phase": "running", "error": None,
                           "ts": datetime.now(timezone.utc).isoformat(), "strategy": STRATEGY}
    return ref


def signal_payload(rec: dict, sma_days: int) -> dict:
    close, sma = rec.get("close"), rec.get("sma")
    dist = (close / sma - 1) * 100 if close and sma else None
    hold = dist is not None and dist > 0
    if dist is None:
        reason = rec.get("note") or "資料不足"
    elif hold:
        reason = f"收盤 {close:,.2f} 站上 {sma_days} 日均線 {sma:,.2f}（+{dist:.2f}%）→ 持有"
    else:
        reason = f"收盤 {close:,.2f} 低於 {sma_days} 日均線 {sma:,.2f}（{dist:.2f}%）→ 空手，收盤站回才買"
    return {"bot_id": BOT_ID, "symbol": rec["symbol"], "type": "LONG" if hold else "NEUTRAL",
            "strength": round(dist, 2) if dist is not None else 0.0, "actionable": hold,
            "reasons": [reason] + ([rec["note"]] if rec.get("note") and dist is not None else [])}


async def sync(api, cfg: dict, ref: DailyTrendRef, last: dict[str, dict], refresh_candles: bool = False):
    """把交易所實際狀態填進儀表板（每 60 秒）"""
    bs = state.bots[BOT_ID]
    positions = await api.get_open_positions()
    state.exchange_positions = {p["symbol"]: p for p in positions}
    ref.position_manager.all_positions = {
        p["symbol"]: Position(symbol=p["symbol"], side=p["side"], entry_price=p["entry_price"],
                              quantity=p["quantity"], leverage=int(p.get("leverage", 1)))
        for p in positions if p["symbol"] in cfg["symbols"]
    }
    ref.position_manager.capital_manager.open_positions = len(ref.position_manager.all_positions)
    try:
        bs.balance = await api.get_usdt_balance()
    except Exception:  # noqa: BLE001
        pass
    for sym in cfg["symbols"]:
        try:
            state.last_prices[sym] = await api.get_ticker_price(sym)
        except Exception:  # noqa: BLE001
            pass
        if refresh_candles:
            try:
                ref.candle_manager.init_from_klines(sym, "1d", await api.get_klines(sym, "1d", limit=200))
            except Exception:  # noqa: BLE001
                pass
        if sym in last:
            bs.last_signals[sym] = signal_payload(last[sym], int(cfg["sma_days"]))
    state.engine_status["ts"] = datetime.now(timezone.utc).isoformat()
