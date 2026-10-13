"""實盤期間重播 — 用「現行設定」回測實盤剛跑過的同一段 K 線，和實盤成績對照

用途：實盤勝率（56%）遠低於回測（~88%）時，先分清楚是哪一種問題：
    回測在同一段期間「也賠」→ 策略本身在這段行情失效（行情/過擬合問題）
    回測在同一段期間「賺」  → 實盤執行跟回測不一致（滑價、停損位置、斷線、風控擋單）

    python -m backtest.live_replay --window 14
"""

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.fetch_klines import get_klines_df
from backtest.fidelity import SentimentReplay
from backtest.param_sweep import (
    SWEEP_GRID, _flat_cfg, _read_path, fast_backtest, precompute_sides,
)
from src.utils.config import load_config

FEE = 0.08          # % 名目，來回
WARMUP_DAYS = 30    # 4h EMA50 需 ~9 天，多抓一些讓所有時框指標穩定


def current_params(bot_cfg: dict) -> tuple[dict, float, tuple]:
    flat = _flat_cfg(bot_cfg)
    params = {name: _read_path(flat, path) for name, (path, _) in SWEEP_GRID.items()}
    risk = flat.get("risk", {})
    tp = risk.get("take_profit", {})
    close_pcts = tuple(tp.get(f"level_{k}_close_pct", d) for k, d in ((1, 40), (2, 35), (3, 25)))
    activation = risk.get("trailing_stop", {}).get("activation_pct", 1.0)
    return params, activation, close_pcts


def replay_symbol(sym: str, bot_cfg: dict, window_days: int, replay) -> list[dict]:
    df = asyncio.run(get_klines_df(sym, "5m", window_days + WARMUP_DAYS, use_cache=False))
    params, activation, close_pcts = current_params(bot_cfg)
    sides = precompute_sides(df, _flat_cfg(bot_cfg), sym, faithful=True,
                             sentiment_replay=replay)
    closes = df["close"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)

    cutoff = df.index[-1] - pd.Timedelta(days=window_days)
    start = int(df.index.searchsorted(cutoff))
    # 從視窗起點才開始模擬，避免暖身期的部位跨進視窗
    trades: list = []
    fast_backtest(closes[start:], highs[start:], lows[start:], sides[start:], params,
                  activation, warmup=0, close_pcts=close_pcts, trades_out=trades)
    out = []
    for i, j, side, pnl, reason in trades:
        out.append({
            "symbol": sym,
            "entry": df.index[start + i],
            "exit": df.index[start + j],
            "side": "LONG" if side == 1 else "SHORT",
            "pnl": pnl - FEE,
            "reason": reason,
        })
    return out


def render(all_trades: list[dict], window_days: int, notional: float, params: dict) -> str:
    L = [f"# 實盤期間重播（最近 {window_days} 天，現行設定）", ""]
    L.append(f"參數：SL {params['stop_loss_pct']}% | TP {params['level_1_pct']}/"
             f"{params['level_2_pct']}/{params['level_3_pct']}% | trail "
             f"{params['trail_callback']}% | time_stop {params['time_stop_seconds']}")
    L.append(f"金額換算：每筆名目 {notional:.0f} U（≈ 實盤 10% 倉位 × 10x）；已扣 {FEE}% 手續費")
    L.append("")
    if not all_trades:
        L.append("此期間回測沒有任何交易。")
        return "\n".join(L)

    p = np.array([t["pnl"] for t in all_trades])
    wins, losses = p[p > 0], p[p <= 0]
    L.append("## 總結")
    L.append("```")
    L.append(f"交易數   {len(p)}")
    L.append(f"勝率     {len(wins)/len(p)*100:.1f}%")
    L.append(f"平均獲利 {wins.mean() if len(wins) else 0:+.3f}%  "
             f"({(wins.mean() if len(wins) else 0)*notional/100:+.2f} U)")
    L.append(f"平均虧損 {losses.mean() if len(losses) else 0:+.3f}%  "
             f"({(losses.mean() if len(losses) else 0)*notional/100:+.2f} U)")
    L.append(f"合計     {p.sum():+.2f}%  ({p.sum()*notional/100:+.2f} U)")
    if len(wins) and len(losses):
        payoff = wins.mean() / abs(losses.mean())
        L.append(f"賺賠比   {payoff:.2f} → 損益兩平所需勝率 {1/(1+payoff)*100:.1f}%")
    L.append("```")
    L.append("")

    L.append("## 各標的")
    L.append("```")
    by_sym = defaultdict(list)
    for t in all_trades:
        by_sym[t["symbol"]].append(t["pnl"])
    for sym in sorted(by_sym):
        a = np.array(by_sym[sym])
        L.append(f"{sym:<10} n={len(a):>3}  勝率 {np.mean(a > 0)*100:5.1f}%  "
                 f"合計 {a.sum():+7.2f}%  ({a.sum()*notional/100:+8.2f} U)")
    L.append("```")
    L.append("")

    L.append("## 每日")
    L.append("```")
    by_day = defaultdict(list)
    for t in all_trades:
        by_day[t["exit"].strftime("%m-%d")].append(t["pnl"])
    for d in sorted(by_day):
        a = np.array(by_day[d])
        L.append(f"{d}  n={len(a):>3}  勝 {int(np.sum(a > 0)):>2} 敗 {int(np.sum(a <= 0)):>2}  "
                 f"{a.sum()*notional/100:+8.2f} U")
    L.append("```")
    L.append("")

    L.append("## 逐筆（UTC）")
    L.append("```")
    for t in sorted(all_trades, key=lambda x: x["entry"]):
        L.append(f"{t['entry']:%m-%d %H:%M} → {t['exit']:%m-%d %H:%M}  {t['symbol']:<9} "
                 f"{t['side']:<5} {t['pnl']:+6.2f}%  {t['pnl']*notional/100:+7.2f} U  {t['reason']}")
    L.append("```")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="實盤期間重播")
    ap.add_argument("--symbols", default=None, help="預設讀 settings.yaml trading.symbols")
    ap.add_argument("--window", type=int, default=14, help="重播最近幾天")
    ap.add_argument("--notional", type=float, default=450.0, help="每筆名目金額 U")
    ap.add_argument("--out", default=None, help="輸出 markdown 路徑")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.disable(logging.INFO)

    cfg = load_config()
    bot_cfg = cfg["bots"]["futures"]
    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            if args.symbols else list(cfg["trading"]["symbols"]))

    replay = SentimentReplay.load(days=args.window + WARMUP_DAYS + 10)
    replay = replay if replay.available else None

    all_trades: list[dict] = []
    for sym in syms:
        print(f"  {sym} 重播中...", flush=True)
        t = replay_symbol(sym, bot_cfg, args.window, replay)
        print(f"    → {len(t)} 筆", flush=True)
        all_trades += t

    params, _, _ = current_params(bot_cfg)
    md = render(all_trades, args.window, args.notional, params)
    print(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
