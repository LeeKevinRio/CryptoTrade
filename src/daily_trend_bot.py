"""日線 50 均線、只做多、1 倍 —— 每天日 K 收盤後判斷一次

規則（與 backtest/daily_trend.py 的 sma50_long 完全一致，驗證見 reports/daily-trend-*.md）：
    最新一根「已收盤」日 K 的收盤價 > 過去 50 根日收盤均線 → 持有多單
    否則 → 空手（不做空）

與舊的 5m 抄底機器人（src.main）完全獨立：兩者不可同時跑（同一帳戶、同一標的會互相干擾）。
狀態不存本機：每次都以交易所實際持倉為準，關機重開、IP 斷線都不會亂掉。

    python -m src.daily_trend_bot --dry-run --once   # 只印出今天的判斷，不下單
    python -m src.daily_trend_bot --once             # 判斷並下單一次（可交給工作排程器每天跑）
    python -m src.daily_trend_bot                    # 常駐：啟動先跑一次，之後每天 UTC 00:05 跑
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


# ── 一次判斷 ─────────────────────────────────────────────────────────────────

async def run_cycle(api, cfg: dict, dry_run: bool = False, now_ms: int | None = None) -> list[dict]:
    """對每個標的判斷並（必要時）下單；回傳每個標的的決策紀錄"""
    now_ms = now_ms or int(time.time() * 1000)
    symbols = cfg["symbols"]
    per_symbol = float(cfg["capital_usdt"]) / len(symbols)
    lev = int(cfg["leverage"])
    positions = {p["symbol"]: p for p in await api.get_open_positions()}
    out = []
    for sym in symbols:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbol": sym}
        try:
            kl = await api.get_klines(sym, "1d", limit=cfg["sma_days"] + 5)
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
            elif hold and not pos:
                price = await api.get_ticker_price(sym)
                step, min_notional = symbol_filters(await api.get_symbol_info(sym))
                qty = floor_qty(per_symbol * lev / price, step)
                if qty * price < min_notional:
                    rec["action"] = "skip"
                    rec["note"] = f"金額 {per_symbol:.2f}U 低於最小下單 {min_notional}U"
                else:
                    rec.update(action="buy", qty=qty, price=price)
                    if not dry_run:
                        await api.set_margin_type(sym, cfg["margin_type"])
                        await api.set_leverage(sym, lev)
                        await api.futures_market_order(sym, "BUY", qty)
            elif not hold and pos:
                rec.update(action="sell", qty=pos["quantity"])
                if not dry_run:
                    await api.futures_market_order(sym, "SELL", pos["quantity"], reduce_only=True)
            else:
                rec["action"] = "hold" if hold else "flat"
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
    logger.info("日線均線機器人（%s%s）：%s，均線 %d 天，投入 %.0fU，%dx",
                mode, "・只看不下單" if args.dry_run else "", cfg["symbols"],
                cfg["sma_days"], float(cfg["capital_usdt"]), int(cfg["leverage"]))

    api = BinanceAPI(api_key=b["api_key"], api_secret=b["api_secret"], testnet=b["testnet"])
    await api.connect()
    try:
        while True:
            rows = await run_cycle(api, cfg, dry_run=args.dry_run)
            append_log(rows)
            if args.once:
                return
            wait = seconds_until(cfg["run_at_utc"])
            logger.info("下次判斷：%.1f 小時後（UTC %s）", wait / 3600, cfg["run_at_utc"])
            await asyncio.sleep(wait)
    finally:
        await api.disconnect()


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
