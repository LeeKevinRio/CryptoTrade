"""日線 50 均線、只做多、1 倍 —— 每天日 K 收盤後判斷一次

規則（與 backtest/daily_trend.py 的 sma50_long 完全一致，驗證見 reports/daily-trend-*.md）：
    最新一根「已收盤」日 K 的收盤價 > 過去 50 根日收盤均線 → 持有多單
    否則 → 空手（不做空）
標的：top_volume: N → 每次判斷前取「過去 30 天成交額前 N 名」（上市滿 90 天），每幣 capital_usdt / N；
    持有中但掉出名單的幣不強制賣，跌破均線才賣（驗證見 reports/daily-trend-top10-*.md）

與舊的 5m 抄底機器人（src.main）完全獨立：兩者不可同時跑（同一帳戶、同一標的會互相干擾）。
狀態不存本機：每次都以交易所實際持倉為準，關機重開、IP 斷線都不會亂掉。

    python -m src.daily_trend_bot --dry-run --once   # 只印出今天的判斷，不下單
    python -m src.daily_trend_bot --once             # 判斷並下單一次（可交給工作排程器每天跑）
    python -m src.daily_trend_bot                    # 常駐：啟動先跑一次，之後每天 run_at_utc 跑

錯過檢查時間（電腦沒開）不會出錯：每次啟動都會立刻補判斷一次，只是下單晚一點。
"""

import argparse
import asyncio
import csv
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.utils.logger import setup_logger

logger = setup_logger("daily_trend")

DEFAULTS = {
    "symbols": ["BTCUSDT", "ETHUSDT"],
    "sma_days": 50,
    "capital_usdt": 200.0,      # 投入本策略的總金額，等分給各標的
    "leverage": 1,
    "margin_type": "ISOLATED",
    "run_at_utc": "00:05",      # 日 K 於 UTC 00:00 收盤
    "top_volume": 0,            # > 0 → 每次判斷前改用「過去 30 天成交額前 N 名」取代 symbols
    "min_listed_days": 90,      # 名單只收上市滿 90 天的幣（與回測一致）
}
LOG_CSV = Path("logs/daily_trend.csv")


# ── 純函式（可單元測試）─────────────────────────────────────────────────────

def closed_closes(klines: list[list], now_ms: int) -> list[float]:
    """只保留已收盤的日 K（幣安最後一根是今天尚未收盤的）"""
    return [float(k[4]) for k in klines if int(k[6]) < now_ms]


def should_hold(closes: list[float], sma_days: int) -> bool | None:
    """最新收盤 > 均線 → True；否則 False；資料不足 → None（不動作）"""
    if len(closes) < sma_days:
        return None
    sma = sum(closes[-sma_days:]) / sma_days
    return closes[-1] > sma


def floor_qty(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    n = math.floor(qty / step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(step))))
    return round(n * step, decimals)


def symbol_filters(info: dict) -> tuple[float, float]:
    """(數量步進, 最小名目 USDT)"""
    step, min_notional = 0.001, 5.0
    for f in info.get("filters", []):
        if f.get("filterType") == "LOT_SIZE":
            step = float(f["stepSize"])
        elif f.get("filterType") == "MIN_NOTIONAL":
            min_notional = float(f.get("notional", f.get("minNotional", min_notional)))
    return step, min_notional


def avg_quote_volume(klines: list[list], now_ms: int, days: int = 30) -> float:
    """已收盤日 K 最近 days 根的平均成交額（USDT）"""
    vols = [float(k[7]) for k in klines if int(k[6]) < now_ms and len(k) > 7][-days:]
    return sum(vols) / len(vols) if vols else 0.0


def own_long_symbols(positions: dict[str, dict], lev: int) -> list[str]:
    """本策略的倉＝槓桿與設定相同的多單（舊機器人的 10 倍單、空單都不算）"""
    return [s for s, p in positions.items() if p["side"] == "LONG" and int(p.get("leverage", lev)) == lev]


async def resolve_symbols(api, cfg: dict, positions: dict[str, dict],
                          now_ms: int) -> tuple[list[str], list[str], dict[str, list]]:
    """回傳 (今天要判斷的標的, 掉出名單但持有中的標的, 已抓到的日 K)。
    top_volume > 0：先以 24h 成交額取 3N 個候選，再用過去 30 天平均成交額排出前 N 名（與回測一致）；
    持有中但掉出名單者照樣判斷 —— 只會在跌破均線時賣出，不會加碼。"""
    n = int(cfg.get("top_volume") or 0)
    held = own_long_symbols(positions, int(cfg["leverage"]))
    base = list(cfg["symbols"])
    cache: dict[str, list] = {}
    if n > 0:
        from src.data.universe import rank_symbols
        try:
            info = await api.client.futures_exchange_info()
            tickers = await api.client.futures_ticker()
            vols = []
            for sym in rank_symbols(info, tickers, n * 3, now_ms=now_ms,
                                    min_listed_days=int(cfg["min_listed_days"])):
                try:
                    cache[sym] = await api.get_klines(sym, "1d", limit=int(cfg["sma_days"]) + 5)
                except Exception as e:  # noqa: BLE001 — 抓不到就不列入
                    logger.warning("%s 日 K 取得失敗，不列入名單: %s", sym, e)
                    continue
                vols.append((sym, avg_quote_volume(cache[sym], now_ms)))
            vols.sort(key=lambda x: x[1], reverse=True)
            base = [s for s, _ in vols[:n]]
            if not base:
                raise RuntimeError("成交量排名為空")
        except Exception as e:  # noqa: BLE001 — 排名失敗就沿用 settings 的清單，不中斷交易判斷
            logger.warning("成交量前 %d 名取得失敗，改用設定清單 %s: %s", n, cfg["symbols"], e)
            base = list(cfg["symbols"])
    extra = [s for s in held if s not in base] if n > 0 else []
    return base + extra, extra, cache


async def available_usdt(api) -> float:
    """合約帳戶可用餘額；查不到就回傳無限大（交給交易所判斷）"""
    try:
        return float((await api.client.futures_account())["availableBalance"])
    except Exception:  # noqa: BLE001
        return float("inf")


# ── 一次判斷 ─────────────────────────────────────────────────────────────────

async def run_cycle(api, cfg: dict, dry_run: bool = False, now_ms: int | None = None,
                    allow_buy: bool = True, tracker=None) -> list[dict]:
    """對每個標的判斷並（必要時）下單；回傳每個標的的決策紀錄。
    allow_buy=False（儀表板按了暫停）→ 不買進新倉，但該賣照賣。
    tracker：有給就把買進／賣出寫進資料庫（儀表板的交易紀錄與績效）。"""
    now_ms = now_ms or int(time.time() * 1000)
    lev = int(cfg["leverage"])
    positions = {p["symbol"]: p for p in await api.get_open_positions()}
    symbols, extra, cache = await resolve_symbols(api, cfg, positions, now_ms)
    slots = int(cfg.get("top_volume") or 0) or len(cfg["symbols"])
    per_symbol = float(cfg["capital_usdt"]) / slots
    avail = None
    out = []
    for sym in symbols:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbol": sym}
        try:
            kl = cache.get(sym) or await api.get_klines(sym, "1d", limit=cfg["sma_days"] + 5)
            closes = closed_closes(kl, now_ms)
            hold = should_hold(closes, cfg["sma_days"])
            sma = sum(closes[-cfg["sma_days"]:]) / cfg["sma_days"] if hold is not None else None
            rec.update(close=closes[-1] if closes else None, sma=round(sma, 6) if sma else None)
            pos = positions.get(sym)

            if hold is None:
                rec["action"] = "skip"
                rec["note"] = f"日 K 不足 {cfg['sma_days']} 根"
            elif pos and (pos["side"] != "LONG" or int(pos.get("leverage", lev)) != lev):
                # 不是本策略開的倉（例如舊機器人留下的空單／10 倍單）→ 不碰，避免誤平別人的倉
                rec["action"] = "skip"
                rec["note"] = f"已有非本策略的倉位（{pos['side']} {pos.get('leverage')}x），請先手動處理"
            elif hold and not pos and not allow_buy:
                rec["action"] = "skip"
                rec["note"] = "已暫停（儀表板），不買進新倉"
            elif hold and not pos:
                price = await api.get_ticker_price(sym)
                step, min_notional = symbol_filters(await api.get_symbol_info(sym))
                qty = floor_qty(per_symbol * lev / price, step)
                if avail is None:
                    avail = await available_usdt(api)
                if qty * price < min_notional:
                    rec["action"] = "skip"
                    rec["note"] = f"金額 {per_symbol:.2f}U 低於最小下單 {min_notional}U"
                elif qty * price / lev > avail:
                    rec["action"] = "skip"
                    rec["note"] = f"可用餘額 {avail:.2f}U 不足 {qty * price / lev:.2f}U"
                else:
                    rec.update(action="buy", qty=qty, price=price)
                    avail -= qty * price / lev
                    if not dry_run:
                        await api.set_margin_type(sym, cfg["margin_type"])
                        await api.set_leverage(sym, lev)
                        await api.futures_market_order(sym, "BUY", qty)
                        if tracker is not None:
                            tracker.record_open({"bot_id": "futures", "mode": "futures", "symbol": sym,
                                                 "side": "LONG", "entry_price": price, "quantity": qty,
                                                 "strategy": "sma50_long"})
            elif not hold and pos:
                rec.update(action="sell", qty=pos["quantity"])
                if not dry_run:
                    await api.futures_market_order(sym, "SELL", pos["quantity"], reduce_only=True)
                    if tracker is not None:
                        price = await api.get_ticker_price(sym)
                        entry = float(pos["entry_price"])
                        tracker.record_close(
                            trade_id=None, symbol=sym, exit_price=price,
                            pnl=round((price - entry) * pos["quantity"], 4),
                            pnl_pct=round((price / entry - 1) * 100, 2) if entry else 0.0,
                            bot_id="futures", reason=f"收盤跌破 {cfg['sma_days']} 日均線")
            else:
                rec["action"] = "hold" if hold else "flat"
            if sym in extra and rec["action"] == "hold":
                rec["note"] = f"已掉出成交量前 {cfg['top_volume']} 名：持有到跌破均線才賣"
        except Exception as e:  # noqa: BLE001 — 單一標的失敗不影響其他標的
            rec.update(action="error", note=str(e))
            logger.error("%s 判斷／下單失敗: %s", sym, e)
        rec["dry_run"] = dry_run
        logger.info("%s %s close=%s sma=%s %s", sym, rec["action"], rec.get("close"),
                    rec.get("sma"), rec.get("note", ""))
        out.append(rec)
    return out


def append_log(rows: list[dict], path: Path = LOG_CSV):
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["ts", "symbol", "action", "close", "sma", "qty", "price", "dry_run", "note"]
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def seconds_until(hhmm: str, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    h, m = (int(x) for x in hhmm.split(":"))
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


async def amain(args):
    from src.data.binance_client import BinanceAPI
    from src.utils.config import load_config

    config = load_config()
    cfg = {**DEFAULTS, **(config.get("daily_trend") or {})}
    b = config["binance"]
    if not b["testnet"] and not b.get("live_ack") and not args.dry_run:
        raise SystemExit("真金模式需設定 LIVE_TRADING_ACK=I_UNDERSTAND（或先用 --dry-run）")
    mode = "測試網" if b["testnet"] else "真金"
    universe = f"成交量前 {cfg['top_volume']} 名（每次判斷前更新）" if int(cfg["top_volume"] or 0) > 0 else cfg["symbols"]
    logger.info("日線均線機器人（%s%s）：%s，均線 %d 天，投入 %.0fU，%dx",
                mode, "・只看不下單" if args.dry_run else "", universe,
                cfg["sma_days"], float(cfg["capital_usdt"]), int(cfg["leverage"]))

    api = BinanceAPI(api_key=b["api_key"], api_secret=b["api_secret"], testnet=b["testnet"])
    await connect_with_retry(api, attempts=1 if args.once else None)
    server = None
    try:
        from src import daily_trend_dashboard as dash
        from src.execution.order_tracker import OrderTracker
        from src.utils.models import init_db
        db_url = config.get("database", {}).get("url", "sqlite:///cryptotrade.db").replace("+aiosqlite", "")
        tracker = OrderTracker(init_db(db_url))
        ref = dash.attach(api, cfg, tracker, b["testnet"])
        wcfg = config.get("web", {})
        if not args.once and wcfg.get("enabled", True):
            # 沿用原本的儀表板（src/web/app.py + static/），不另做頁面
            from src.web.app import create_app
            server = await _start_web(create_app(tracker=tracker), wcfg)
        last: dict[str, dict] = {}
        while True:
            paused = dash.state.bots[dash.BOT_ID].paused
            try:
                rows = await asyncio.wait_for(
                    run_cycle(api, cfg, dry_run=args.dry_run, allow_buy=not paused,
                              tracker=None if args.dry_run else tracker), CYCLE_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 — 網路斷線等：5 分鐘後重試，不要等到明天
                if args.once:
                    raise
                logger.error("這次判斷失敗（%s），%d 分鐘後重試", e or type(e).__name__, RETRY_CYCLE_S // 60)
                dash.state.engine_status["error"] = f"判斷失敗，{RETRY_CYCLE_S // 60} 分鐘後重試：{e}"
                await asyncio.sleep(RETRY_CYCLE_S)
                continue
            dash.state.engine_status["error"] = None
            append_log(rows)
            active = [r["symbol"] for r in rows] or list(cfg["symbols"])
            dash.state.symbols = active
            last.clear()
            last.update({r["symbol"]: r for r in rows})
            if args.once:
                return
            wait = seconds_until(cfg["run_at_utc"])
            next_run = datetime.now(timezone.utc) + timedelta(seconds=wait)
            dash.state.engine_status["next_run"] = next_run.isoformat()
            logger.info("下次判斷：%.1f 小時後（UTC %s）", wait / 3600, cfg["run_at_utc"])
            # 等待期間每 60 秒更新儀表板（餘額、持倉、現價）；日 K 圖每 15 分鐘更新
            tick = 0
            while datetime.now(timezone.utc) < next_run:
                try:
                    await asyncio.wait_for(dash.sync(api, {**cfg, "symbols": active}, ref, last,
                                                     refresh_candles=(tick % 15 == 0)), 50)
                except Exception as e:  # noqa: BLE001 — 儀表板更新失敗（含網路卡住逾時）不影響交易判斷
                    logger.warning("儀表板更新失敗: %s", e or type(e).__name__)
                tick += 1
                remaining = (next_run - datetime.now(timezone.utc)).total_seconds()
                await asyncio.sleep(max(1.0, min(60.0, remaining)))
    finally:
        if server is not None:
            server.should_exit = True
        await api.disconnect()


CONNECT_TIMEOUT_S = 30
RETRY_CONNECT_S = 60
CYCLE_TIMEOUT_S = 600
RETRY_CYCLE_S = 300


async def connect_with_retry(api, attempts: int | None = None, timeout: float = CONNECT_TIMEOUT_S,
                             wait: float = RETRY_CONNECT_S):
    """連幣安：逾時（例如 DNS 卡住）或失敗就等一下再試；attempts=None 表示一直試到成功"""
    n = 0
    while True:
        n += 1
        try:
            await asyncio.wait_for(api.connect(), timeout)
            return
        except Exception as e:  # noqa: BLE001 — asyncio.TimeoutError 也在這裡
            if api.client is not None:                   # 逾時被取消時 connect() 來不及自己關
                try:
                    await api.client.close_connection()
                except Exception:  # noqa: BLE001
                    pass
                api.client = None
            reason = "逾時（網路或 DNS 沒有回應）" if isinstance(e, asyncio.TimeoutError) else str(e)
            if attempts is not None and n >= attempts:
                raise SystemExit(f"連不上幣安：{reason}")
            logger.warning("連不上幣安：%s —— %d 秒後重試（第 %d 次）", reason, int(wait), n)
            await asyncio.sleep(wait)


async def _start_web(app, wcfg: dict):
    """在同一個事件迴圈裡跑儀表板；port 被占用（例如舊機器人還開著）就只記警告、不影響交易"""
    import socket

    import uvicorn
    host, port = wcfg.get("host", "127.0.0.1"), int(wcfg.get("port", 8899))
    # 先試綁定：uvicorn 綁定失敗會 sys.exit，會連帶把交易程式關掉
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            logger.warning("儀表板未啟動：port %d 已被占用（舊機器人是否還開著？）—— 交易判斷照常進行", port)
            return None
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.5)
    if task.done():
        logger.warning("儀表板啟動失敗（port %d 可能被占用，舊機器人是否還開著？）", port)
        return None
    logger.info("📊 儀表板：http://%s:%d/", host, port)
    return server


def main():
    ap = argparse.ArgumentParser(description="日線 50 均線只做多機器人")
    ap.add_argument("--once", action="store_true", help="只跑一次就結束")
    ap.add_argument("--dry-run", action="store_true", help="只印判斷，不下單")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
