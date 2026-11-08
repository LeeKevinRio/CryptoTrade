"""策略稽核 — 近期每一次判斷、每一筆單是否照日線 50 均線規則（唯讀，不下單）

    python -m scripts.strategy_audit            # 近 7 天
    python -m scripts.strategy_audit --days 30

不相信機器人自己算的數字：一律用幣安日 K、幣安成交紀錄重新核對。
    ① 判斷：logs/daily_trend.csv 每一筆（非只看模式）→ 以判斷當下「已收盤」日 K 重算收盤 vs 均線
    ② 執行：紀錄說買／賣 ↔ 幣安成交紀錄（同幣、同方向、時間差 15 分鐘內）
    ③ 外來單：幣安有成交、但機器人紀錄沒有 → 舊機器人或手動下的
    ④ 結果：本策略每筆進出、損益、持有天數；漏跑的日子；與回測對照
"""

import argparse
import asyncio
import csv
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.daily_trend_bot import DEFAULTS, LOG_CSV, closed_closes, desired_side

TW = timezone(timedelta(hours=8))
DAY_MS = 86_400_000
MATCH_MS = 15 * 60 * 1000
BACKTEST_NOTE_LS = "回測（前 10 名多空都做，2021-01 ~ 2026-10）：月勝率 44%、平均每週開倉 4.4 次、年化 +4.3%、最大回撤 74.5%"
BACKTEST_NOTE = "回測（前 10 名，2020-12 ~ 2026-09）：月勝率 37%、平均每週買進 2.3 次 —— 常常小虧、靠少數大漲賺"


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _tw(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, TW).strftime("%m-%d %H:%M")


def read_log(path: Path, since_ms: int) -> list[dict]:
    """機器人實際（非只看模式）的判斷紀錄"""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("dry_run") != "True"]
    return [r for r in rows if _ms(r["ts"]) >= since_ms]


def group_fills(fills: list[dict]) -> list[dict]:
    """幣安成交（一張單可能分多筆成交）→ 每張單一列"""
    by: dict = {}
    for f in fills:
        o = by.setdefault(f["orderId"], {"symbol": f["symbol"], "side": f["side"], "time": int(f["time"]),
                                         "qty": 0.0, "quote": 0.0, "realized": 0.0, "commission": 0.0})
        o["qty"] += float(f["qty"])
        o["quote"] += float(f["quoteQty"])
        o["realized"] += float(f.get("realizedPnl", 0) or 0)
        o["commission"] += float(f.get("commission", 0) or 0)
        o["time"] = min(o["time"], int(f["time"]))
    out = []
    for o in by.values():
        o["price"] = o["quote"] / o["qty"] if o["qty"] else 0.0
        out.append(o)
    return sorted(out, key=lambda o: o["time"])


def expected_at(klines: list[list], t_ms: int, sma_days: int,
                mode: str = "long") -> tuple[str | None, float | None, float | None]:
    """在 t_ms 這個時間點規則應有的狀態（LONG／SHORT／FLAT；只用當時已收盤的日 K）"""
    closes = closed_closes(klines, t_ms)
    want = desired_side(closes, sma_days, mode)
    if want is None:
        return None, None, None
    return want, closes[-1], sum(closes[-sma_days:]) / sma_days


# 紀錄動作 → 動作完成後規則要求的狀態（sell／cover 之後若同時反手，會另有一筆 short／buy）
_ACTION_OK = {"buy": {"LONG"}, "hold": {"LONG"}, "short": {"SHORT"}, "hold_short": {"SHORT"},
              "sell": {"FLAT", "SHORT"}, "cover": {"FLAT", "LONG"}, "flat": {"FLAT"}}
_STATE_TXT = {"LONG": "做多", "SHORT": "做空", "FLAT": "空手"}


def check_decision(row: dict, klines: list[list], sma_days: int, mode: str = "long") -> str | None:
    """回傳錯誤說明；正確（或無法判斷）回傳 None"""
    act = row["action"]
    if act not in _ACTION_OK:
        return None
    want, close, sma = expected_at(klines, _ms(row["ts"]), sma_days, mode)
    if want is None:
        return None
    if want not in _ACTION_OK[act]:
        rel = ">" if close > sma else ("<" if close < sma else "=")
        return f"紀錄 {act}，但收盤 {close:g} {rel} 均線 {sma:.6g} → 應{_STATE_TXT[want]}"
    if row.get("close") and abs(float(row["close"]) / close - 1) > 1e-6:
        return f"紀錄的收盤 {row['close']} 與幣安日 K {close:g} 不同"
    return None


ORDER_SIDE = {"buy": "BUY", "sell": "SELL", "short": "SELL", "cover": "BUY"}


def match_orders(orders: list[dict], log_rows: list[dict]) -> tuple[list, list[dict], list[dict]]:
    """(配對成功 [(紀錄, 單)], 沒成交的紀錄, 不是本機器人的單)"""
    want = [r for r in log_rows if r["action"] in ORDER_SIDE]
    used: set[int] = set()
    pairs, missing = [], []
    for r in want:
        side, t = ORDER_SIDE[r["action"]], _ms(r["ts"])
        best = None
        for i, o in enumerate(orders):
            if i in used or o["symbol"] != r["symbol"] or o["side"] != side:
                continue
            if abs(o["time"] - t) <= MATCH_MS and (best is None or abs(o["time"] - t) < abs(orders[best]["time"] - t)):
                best = i
        if best is None:
            missing.append(r)
        else:
            used.add(best)
            pairs.append((r, orders[best]))
    foreign = [o for i, o in enumerate(orders) if i not in used]
    return pairs, missing, foreign


def round_trips(orders: list[dict]) -> list[dict]:
    """本策略的單 → 每筆進出。單上有 action（buy／short 開倉、sell／cover 平倉）就照它；
    沒有就視為只做多（BUY 開、SELL 平）"""
    open_: dict[str, dict] = {}
    trips = []
    for o in sorted(orders, key=lambda o: o["time"]):
        act = o.get("action") or ("buy" if o["side"] == "BUY" else "sell")
        if act in ("buy", "short"):
            open_[o["symbol"]] = {**o, "action": act}
        elif o["symbol"] in open_:
            b = open_.pop(o["symbol"])
            sign = -1 if b["action"] == "short" else 1
            pnl = o["realized"] - b["commission"] - o["commission"]
            trips.append({"symbol": o["symbol"], "side": "空" if sign < 0 else "多", "entry": b, "exit": o,
                          "pnl": pnl, "pct": sign * (o["price"] / b["price"] - 1) * 100 if b["price"] else 0.0,
                          "days": (o["time"] - b["time"]) / DAY_MS})
    for b in open_.values():
        trips.append({"symbol": b["symbol"], "side": "空" if b["action"] == "short" else "多", "entry": b,
                      "exit": None, "pnl": None, "pct": None, "days": None})
    return trips


def missed_days(log_rows: list[dict], since_ms: int, now_ms: int) -> list[str]:
    """區間內（從第一筆判斷起）完全沒有判斷紀錄的日子（台灣日期）—— 電腦沒開或程式沒跑"""
    if not log_rows:
        return []
    seen = {datetime.fromtimestamp(_ms(r["ts"]) / 1000, TW).date() for r in log_rows}
    d = min(seen)
    last = datetime.fromtimestamp(now_ms / 1000, TW).date()
    out = []
    while d < last:
        if d not in seen:
            out.append(d.strftime("%m-%d"))
        d += timedelta(days=1)
    return out


async def amain(days: int, log_path: Path):
    from src.data.binance_client import BinanceAPI
    from src.utils.config import load_config

    config = load_config()
    cfg = {**DEFAULTS, **(config.get("daily_trend") or {})}
    sma_days = int(cfg["sma_days"])
    b = config["binance"]
    now_ms = int(time.time() * 1000)
    since = now_ms - days * DAY_MS
    log_rows = read_log(log_path, since)

    api = BinanceAPI(api_key=b["api_key"], api_secret=b["api_secret"], testnet=b["testnet"])
    await api.connect()
    try:
        from scripts.account_check import _income
        traded = {r["symbol"] for r in await _income(api, since) if r.get("incomeType") == "COMMISSION" and r.get("symbol")}
        symbols = sorted({r["symbol"] for r in log_rows} | traded)
        klines, orders, failed = {}, [], {}
        for sym in symbols:
            try:
                klines[sym] = await api.get_klines(sym, "1d", limit=min(1500, days + sma_days + 5))
                orders += group_fills(await api.get_account_trades(sym, since))
            except Exception as e:  # noqa: BLE001 — 單一標的查詢失敗不中斷整份稽核
                failed[sym] = str(e)
    finally:
        await api.disconnect()
    orders.sort(key=lambda o: o["time"])

    L = [f"=== 策略稽核：近 {days} 天（{datetime.now(TW):%Y-%m-%d %H:%M} 台灣時間）===", ""]
    L += [f"⚠ {s} 查詢失敗（這個幣的結果不完整）：{e}" for s, e in failed.items()] + ([""] if failed else [])
    if not log_rows:
        L += [f"⚠ 找不到機器人的判斷紀錄 {log_path}（請在專案資料夾執行；只看模式的紀錄不算）", ""]

    mode = cfg.get("mode", "long")
    errs = [(r, e) for r in log_rows if (e := check_decision(r, klines.get(r["symbol"], []), sma_days, mode))]
    L.append(f"① 判斷是否照規則（用幣安日 K 重算）：{len(log_rows)} 筆判斷，"
             + ("✓ 全部正確" if not errs else f"✗ {len(errs)} 筆有問題"))
    L += [f"   ✗ {_tw(_ms(r['ts']))} {r['symbol']} {e}" for r, e in errs]
    skips = [r for r in log_rows if r["action"] in ("skip", "error")]
    L += [f"   · {_tw(_ms(r['ts']))} {r['symbol']} {r['action']}：{r.get('note', '')}" for r in skips]

    pairs, missing, foreign = match_orders(orders, log_rows)
    L += ["", f"② 下單是否確實成交：機器人紀錄買賣 {len(pairs) + len(missing)} 次，幣安對得上 {len(pairs)} 次"]
    for r, o in pairs:
        want = float(r["qty"]) if r.get("qty") else None
        qty_ok = want is None or abs(o["qty"] - want) <= max(1e-9, want * 1e-6)
        L.append(f"   {'✓' if qty_ok else '✗'} {_tw(o['time'])} {o['symbol']:<12} {o['side']:<4} "
                 f"{o['qty']:g} @ {o['price']:.6g}（{o['quote']:.2f}U）" + ("" if qty_ok else f" 紀錄數量 {want:g}"))
    L += [f"   ✗ {_tw(_ms(r['ts']))} {r['symbol']} 紀錄 {r['action']} 但幣安沒有成交" for r in missing]

    L += ["", f"③ 不是這支機器人下的單（舊機器人／手動）：{len(foreign)} 張"]
    by_sym = defaultdict(lambda: [0, 0.0])
    for o in foreign:
        by_sym[o["symbol"]][0] += 1
        by_sym[o["symbol"]][1] += o["realized"] - o["commission"]
    L += [f"   {s:<14} {n} 張，損益 {v:+.2f}U" for s, (n, v) in sorted(by_sym.items())] or ["   （沒有）"]

    trips = round_trips([{**o, "action": r["action"]} for r, o in pairs])
    closed = [t for t in trips if t["exit"]]
    L += ["", "④ 本策略每筆交易"]
    for t in trips:
        e = t["entry"]
        o_txt, c_txt = ("空", "補") if t["side"] == "空" else ("買", "賣")
        if t["exit"]:
            L.append(f"   {t['symbol']:<12} {t['side']} {_tw(e['time'])} {o_txt} {e['price']:.6g} → "
                     f"{_tw(t['exit']['time'])} {c_txt} {t['exit']['price']:.6g}  {t['pct']:+.2f}%  "
                     f"{t['pnl']:+.2f}U  持有 {t['days']:.1f} 天")
        else:
            L.append(f"   {t['symbol']:<12} {t['side']} {_tw(e['time'])} {o_txt} {e['price']:.6g} → 持有中")
    if not trips:
        L.append("   （區間內沒有本策略的交易）")
    if closed:
        wins = sum(t["pnl"] > 0 for t in closed)
        L.append(f"   已平倉 {len(closed)} 筆：勝 {wins} 敗 {len(closed) - wins}，合計 {sum(t['pnl'] for t in closed):+.2f}U"
                 "（含手續費，不含資金費）")
    opens = sum(r["action"] in ("buy", "short") for r, _ in pairs)
    L.append(f"   實際每週開倉 {opens / max(days, 1) * 7:.1f} 次 ｜ "
             + (BACKTEST_NOTE_LS if mode == "ls" else BACKTEST_NOTE))

    miss = missed_days(log_rows, since, now_ms)
    L += ["", "⑤ 漏跑的日子（電腦沒開／程式沒跑，當天沒有判斷）：" + ("、".join(miss) if miss else "沒有")]
    print("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description="策略稽核（唯讀）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--log", default=str(LOG_CSV))
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    asyncio.run(amain(args.days, Path(args.log)))


if __name__ == "__main__":
    main()
