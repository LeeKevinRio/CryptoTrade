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

from src.daily_trend_bot import DEFAULTS, closed_closes, should_hold

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


def verdict(hold: bool | None, pos: dict | None, lev: int) -> str:
    """策略應有狀態 vs 帳戶實際持倉"""
    if hold is None:
        return "資料不足，無法判斷"
    if pos and (pos["side"] != "LONG" or int(pos.get("leverage", lev)) != lev):
        return f"⚠ 帳上是 {pos['side']} {pos.get('leverage')}x，不是本策略的倉（舊機器人留下的？）"
    if hold and pos:
        return "✓ 一致：該持有、帳上有多單"
    if not hold and not pos:
        return "✓ 一致：該空手、帳上沒倉"
    if hold and not pos:
        return "⚠ 不一致：該持有但帳上沒倉（暫停中？或下次檢查才會買）"
    return "⚠ 不一致：該空手但帳上還有多單（下次檢查會賣）"


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
    lev, sma_days, syms = int(cfg["leverage"]), int(cfg["sma_days"]), list(cfg["symbols"])
    api = BinanceAPI(api_key=b["api_key"], api_secret=b["api_secret"], testnet=b["testnet"])
    await api.connect()
    try:
        now_ms = int(time.time() * 1000)
        balance = await api.get_usdt_balance()
        positions = {p["symbol"]: p for p in await api.get_open_positions()}
        income = summarize_income(await _income(api, now_ms - days * 86_400_000), syms)

        print(f"=== 帳戶對帳（{datetime.now(TW):%Y-%m-%d %H:%M} 台灣時間，{'測試網' if b['testnet'] else '真金'}）===")
        print(f"錢包餘額：{balance:.2f} U\n")
        print("── 目前持倉 ──")
        if not positions:
            print("  （沒有持倉）")
        for p in positions.values():
            tag = "本策略標的" if p["symbol"] in syms else "★ 不是本策略（舊機器人留下的？）"
            print(f"  {p['symbol']:<14} {p['side']:<5} {p['leverage']}x  數量 {p['quantity']}  "
                  f"均價 {p['entry_price']}  未實現 {p['unrealized_pnl']:+.2f} U  {tag}")

        print(f"\n── 近 {days} 天幣安實際入帳：本策略標的（{' '.join(syms)}）──")
        print("\n".join(_fmt_income(income["strategy"])))
        print(f"\n── 近 {days} 天幣安實際入帳：其他標的 ──")
        print("\n".join(_fmt_income(income["other"])))

        print(f"\n── 策略比對（最新已收盤日 K vs {sma_days} 日均線，日 K 於台灣 08:00 收盤）──")
        for sym in syms:
            closes = closed_closes(await api.get_klines(sym, "1d", limit=sma_days + 5), now_ms)
            hold = should_hold(closes, sma_days)
            if hold is None:
                print(f"  {sym:<10} {verdict(None, None, lev)}")
                continue
            sma = sum(closes[-sma_days:]) / sma_days
            print(f"  {sym:<10} 收盤 {closes[-1]:,.2f}  均線 {sma:,.2f}（{(closes[-1] / sma - 1) * 100:+.2f}%）"
                  f" → 應{'持有' if hold else '空手'}｜{verdict(hold, positions.get(sym), lev)}")
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
