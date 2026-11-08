"""帳戶對帳 — 虧損是從哪裡來的？實際帳戶與日線均線策略是否一致？（唯讀，不下單）

    python -m scripts.account_check            # 近 3 天
    python -m scripts.account_check --days 7

輸出：
    1. 錢包餘額、所有持倉（含不是本策略的倉位）
    2. 近 N 天幣安實際入帳（已實現損益／手續費／資金費），按「本策略標的」與「其他」分開
    3. 每個策略標的：最新已收盤日 K vs 50 日均線 → 策略應該持有或空手，和帳戶實際是否一致
"""

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.daily_trend_bot import DEFAULTS, allowed_sides, closed_closes, desired_side, resolve_symbols

TW = timezone(timedelta(hours=8))
INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")


def summarize_income(rows: list[dict], strategy_symbols: list[str]) -> dict:
    """{"strategy"/"other": {symbol: {type: 金額}}}；只計算損益相關的入帳類型"""
    out = {"strategy": defaultdict(lambda: defaultdict(float)),
           "other": defaultdict(lambda: defaultdict(float))}
    for r in rows:
        t = r.get("incomeType")
        if t not in INCOME_TYPES:
            continue
        sym = r.get("symbol") or "-"
        group = "strategy" if sym in strategy_symbols else "other"
        out[group][sym][t] += float(r["income"])
    return out


SIDE_TXT = {"LONG": "多單", "SHORT": "空單", "FLAT": "空手"}


def verdict(want, pos: dict | None, lev: int, mode: str = "long") -> str:
    """策略應有狀態（LONG／SHORT／FLAT；舊呼叫可傳 True＝LONG、False＝FLAT）vs 帳戶實際持倉"""
    if want is None:
        return "資料不足，無法判斷"
    if isinstance(want, bool):
        want = "LONG" if want else "FLAT"
    if pos and (pos["side"] not in allowed_sides(mode) or int(pos.get("leverage", lev)) != lev):
        return f"⚠ 帳上是 {pos['side']} {pos.get('leverage')}x，不是本策略的倉（舊機器人留下的？）"
    have = pos["side"] if pos else "FLAT"
    if have == want:
        return f"✓ 一致：應{SIDE_TXT[want]}、帳上{'有' + SIDE_TXT[have] if pos else '沒倉'}"
    if want == "FLAT":
        return f"⚠ 不一致：該空手但帳上還有{SIDE_TXT[have]}（下次檢查會平倉）"
    if not pos:
        return f"⚠ 不一致：應{SIDE_TXT[want]}但帳上沒倉（暫停中？或下次檢查才會開倉）"
    return f"⚠ 不一致：應{SIDE_TXT[want]}但帳上是{SIDE_TXT[have]}（下次檢查會反手）"


def _fmt_income(group: dict) -> list[str]:
    lines, total = [], 0.0
    for sym in sorted(group):
        v = group[sym]
        s = sum(v.values())
        total += s
        lines.append(f"  {sym:<14} 已實現 {v['REALIZED_PNL']:+9.2f}  手續費 {v['COMMISSION']:+8.2f}  "
                     f"資金費 {v['FUNDING_FEE']:+8.2f}  合計 {s:+9.2f}")
    lines.append(f"  {'小計':<12} {total:+.2f} U" if lines else "  （沒有）")
    return lines


async def _income(api, start_ms: int) -> list[dict]:
    rows, cursor = [], start_ms
    while True:
        page = await api.client.futures_income_history(startTime=cursor, limit=1000)
        rows += page
        if len(page) < 1000:
            return rows
        cursor = int(page[-1]["time"]) + 1


async def amain(days: int):
    from src.data.binance_client import BinanceAPI
    from src.utils.config import load_config

    config = load_config()
    cfg = {**DEFAULTS, **(config.get("daily_trend") or {})}
    b = config["binance"]
    lev, sma_days, mode = int(cfg["leverage"]), int(cfg["sma_days"]), cfg.get("mode", "long")
    api = BinanceAPI(api_key=b["api_key"], api_secret=b["api_secret"], testnet=b["testnet"])
    await api.connect()
    try:
        now_ms = int(time.time() * 1000)
        balance = await api.get_usdt_balance()
        positions = {p["symbol"]: p for p in await api.get_open_positions()}
        syms, extra, cache = await resolve_symbols(api, cfg, positions, now_ms)
        income = summarize_income(await _income(api, now_ms - days * 86_400_000), syms)

        print(f"=== 帳戶對帳（{datetime.now(TW):%Y-%m-%d %H:%M} 台灣時間，{'測試網' if b['testnet'] else '真金'}）===")
        print(f"錢包餘額：{balance:.2f} U\n")
        print("── 目前持倉 ──")
        if not positions:
            print("  （沒有持倉）")
        for p in positions.values():
            mine = p["symbol"] in syms and p["side"] in allowed_sides(mode) and int(p["leverage"]) == lev
            tag = "本策略" if mine else "★ 不是本策略（舊機器人留下的？）"
            print(f"  {p['symbol']:<14} {p['side']:<5} {p['leverage']}x  數量 {p['quantity']}  "
                  f"均價 {p['entry_price']}  未實現 {p['unrealized_pnl']:+.2f} U  {tag}")

        print(f"\n── 近 {days} 天幣安實際入帳：本策略名單內（{' '.join(syms)}）──")
        print("\n".join(_fmt_income(income["strategy"])))
        print(f"\n── 近 {days} 天幣安實際入帳：其他標的 ──")
        print("\n".join(_fmt_income(income["other"])))

        print(f"\n── 策略比對（最新已收盤日 K vs {sma_days} 日均線，日 K 於台灣 08:00 收盤）──")
        for sym in syms:
            kl = cache.get(sym) or await api.get_klines(sym, "1d", limit=sma_days + 5)
            closes = closed_closes(kl, now_ms)
            want = desired_side(closes, sma_days, mode)
            if want is None:
                print(f"  {sym:<10} {verdict(None, None, lev, mode)}")
                continue
            sma = sum(closes[-sma_days:]) / sma_days
            tag = "（已掉出名單，穿越均線才平倉）" if sym in extra else ""
            print(f"  {sym:<10}{tag} 收盤 {closes[-1]:,.6g}  均線 {sma:,.6g}（{(closes[-1] / sma - 1) * 100:+.2f}%）"
                  f" → 應{SIDE_TXT[want]}｜{verdict(want, positions.get(sym), lev, mode)}")
    finally:
        await api.disconnect()


def main():
    ap = argparse.ArgumentParser(description="帳戶對帳（唯讀）")
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    asyncio.run(amain(args.days))


if __name__ == "__main__":
    main()
