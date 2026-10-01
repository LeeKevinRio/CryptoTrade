"""順勢策略驗證 —— 與抄底策略同一套標準：7 標的 × 90 天真實 K 線 × 扣費

進場由 TrendFollower.analyze 逐根（保真時框切片）產生，含結構停損價。
出場模擬：
  partial_r   達 R 倍（風險距離的倍數）先平 30%，停損移到進場價（None = 不分批）
  trail_atr   剩餘部位用 1h ATR(14) × mult 的吊燈追蹤（從最高點回落）
  ema_exit    1h 收盤跌破 EMA20 視為趨勢結束出場
損益以滿額部位名目為 100%（% 計），每筆扣 0.08%。另輸出平均 R 倍數。

    python -m backtest.trend_backtest --real --symbol BTCUSDT --days 90 --dump-csv reports/trend-BTCUSDT.csv
    python -m backtest.trend_backtest --consensus --write reports/trend-consensus-2026-10-01.md
"""

import argparse
import copy
import csv
import itertools
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from src.utils.performance import EST_FEE_RATE

ENTRY_GRID = {"entry_type": ["pullback", "breakout"], "slope_threshold_pct": [0.3, 0.5]}
EXIT_GRID = {
    "partial_r": [None, 1.0, 1.5],
    "trail_atr": [2.0, 3.0],
    "ema_exit": [False, True],
    "max_stop_pct": [2.0, 3.0],
}
PARAM_COLS = list(ENTRY_GRID) + list(EXIT_GRID)
METRIC_COLS = ["trades", "win_rate", "avg_win", "avg_loss", "payoff", "avg_r", "net_pnl_pct",
               "net_expectancy_pct", "net_profit_factor", "max_dd_pct"]


def precompute(df5m: pd.DataFrame, symbol: str, strat_cfg: dict, warmup: int = 300):
    """逐根呼叫策略：回傳 sides(±1/0)、stop_dist_pct、以及 1h ATR 與 EMA20 對齊到 5m 的序列"""
    from backtest.fidelity import TimeframeSlicer, resample_timeframes
    from src.strategy.trend_follower import TrendFollower
    from src.indicators.atr import calculate_atr
    from src.indicators.ema import calculate_ema

    frames = resample_timeframes(df5m)
    slicer = TimeframeSlicer(frames, lookback=260)
    strat = TrendFollower({"strategy": {"trend_follower": strat_cfg}})
    n = len(df5m)
    sides = np.zeros(n, dtype=np.int8)
    stop_dist = np.zeros(n, dtype=float)
    idx = df5m.index
    for i in range(warmup, n):
        c = slicer.at(idx[i])
        sig = strat.analyze(symbol, c)
        if sig.is_actionable and sig.stop_price:
            sides[i] = 1 if sig.type.value == "LONG" else -1
            stop_dist[i] = abs(sig.price - sig.stop_price) / sig.price * 100
    # 1h ATR / EMA20 對齊到每根 5m（只用已收盤的 1h）
    h1 = frames["1h"]
    atr1h = calculate_atr(h1, 14)
    ema1h = calculate_ema(h1, 20)
    pos = h1.index.searchsorted(idx, side="right") - 1
    pos = np.clip(pos, 0, len(h1) - 1)
    atr_al = atr1h.to_numpy(dtype=float)[pos]
    ema_al = ema1h.to_numpy(dtype=float)[pos]
    h1close_al = h1["close"].to_numpy(dtype=float)[pos]
    return sides, stop_dist, atr_al, ema_al, h1close_al


def simulate(closes, highs, lows, sides, stop_dist, atr, ema1h, h1close, p: dict, warmup: int = 300):
    """回傳 [(pnl_pct, r_multiple)]"""
    out = []
    n = len(closes)
    i = warmup
    while i < n:
        side = sides[i]
        if side == 0:
            i += 1
            continue
        p0 = closes[i]
        sgn = 1 if side == 1 else -1
        dist = min(stop_dist[i], p["max_stop_pct"])
        if dist <= 0:
            i += 1
            continue
        stop = p0 * (1 - sgn * dist / 100)
        risk = p0 * dist / 100
        frac = 1.0
        realized = 0.0
        partial_done = False
        best = p0
        j = i + 1
        exited = False
        while j < n:
            c, h, l = closes[j], highs[j], lows[j]
            ext = h if side == 1 else l
            if sgn * (ext - best) > 0:
                best = ext
            reason = None
            # 停損（含保本）優先
            if (side == 1 and l <= stop) or (side == -1 and h >= stop):
                realized += frac * sgn * (stop - p0) / p0 * 100
                frac = 0.0
                reason = "SL"
            else:
                # 分批：達 partial_r × 風險 → 平 30%，停損移到進場價
                if p["partial_r"] and not partial_done:
                    target = p0 + sgn * p["partial_r"] * risk
                    if (side == 1 and h >= target) or (side == -1 and l <= target):
                        realized += 0.3 * sgn * (target - p0) / p0 * 100
                        frac -= 0.3
                        stop = p0
                        partial_done = True
                # 吊燈追蹤：最高點回落 ATR × mult
                a = atr[j] if not np.isnan(atr[j]) else p0 * 0.01
                chand = best - sgn * p["trail_atr"] * a
                if sgn * (chand - stop) > 0:
                    stop = chand          # 追蹤只往有利方向移
                if p["ema_exit"] and ((side == 1 and h1close[j] < ema1h[j]) or (side == -1 and h1close[j] > ema1h[j])):
                    realized += frac * sgn * (c - p0) / p0 * 100
                    frac = 0.0
                    reason = "ema"
            if reason is not None:
                r_mult = realized / (dist) if dist else 0.0
                out.append((round(realized, 3), round(r_mult, 2)))
                i = j + 1
                exited = True
                break
            j += 1
        if not exited:
            break
    return out


def stats(res, fee_pct: float = EST_FEE_RATE * 100) -> dict:
    if not res:
        return {"trades": 0}
    net = [x - fee_pct for x, _ in res]
    rs = [r for _, r in res]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    aw = float(np.mean(wins)) if wins else 0.0
    al = float(np.mean(losses)) if losses else 0.0
    peak = eq = dd = 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "trades": len(net), "win_rate": round(len(wins) / len(net) * 100, 1),
        "avg_win": round(aw, 3), "avg_loss": round(al, 3),
        "payoff": round(abs(aw / al), 2) if al else 0.0,
        "avg_r": round(float(np.mean(rs)), 2),
        "net_pnl_pct": round(sum(net), 2),
        "net_expectancy_pct": round(sum(net) / len(net), 3),
        "net_profit_factor": round(sum(wins) / max(abs(sum(losses)), 0.01), 2),
        "max_dd_pct": round(dd, 2),
    }


def run(symbol: str, days: int, dump_csv: str | None, min_trades: int = 12):
    import asyncio
    from backtest.fetch_klines import get_klines_df
    df = asyncio.run(get_klines_df(symbol, "5m", days))
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    print(f"  {symbol}: {len(df)} 根 | {df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}", flush=True)
    rows = []
    for ev in itertools.product(*[ENTRY_GRID[k] for k in ENTRY_GRID]):
        ecfg = dict(zip(ENTRY_GRID, ev))
        print(f"  進場 {ecfg} 計算訊號...", flush=True)
        sides, sd, atr, ema, h1c = precompute(df, symbol, {**ecfg, "max_stop_pct": 3.0})
        print(f"    {int(np.count_nonzero(sides))} 個訊號", flush=True)
        for xv in itertools.product(*[EXIT_GRID[k] for k in EXIT_GRID]):
            xcfg = dict(zip(EXIT_GRID, xv))
            s = stats(simulate(closes, highs, lows, sides, sd, atr, ema, h1c, xcfg))
            if s["trades"] < min_trades:
                continue
            rows.append({**ecfg, **xcfg, **s})
    rows.sort(key=lambda r: r["net_expectancy_pct"], reverse=True)
    if dump_csv:
        Path(dump_csv).parent.mkdir(parents=True, exist_ok=True)
        with open(dump_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(PARAM_COLS + METRIC_COLS)
            for r in rows:
                w.writerow([r[c] for c in PARAM_COLS] + [r[c] for c in METRIC_COLS])
        print(f"  📄 {dump_csv}（{len(rows)} 組）", flush=True)
    for r in rows[:8]:
        print("  ", fmt(r), f"n={r['trades']} 勝率{r['win_rate']}% 賠率{r['payoff']} R{r['avg_r']} 淨期望{r['net_expectancy_pct']:+.3f} 回撤{r['max_dd_pct']}")
    return rows


def fmt(r: dict) -> str:
    pr = r["partial_r"]
    return (f"{r['entry_type']} 斜率{float(r['slope_threshold_pct']):g} 分批R{pr if pr not in (None, 'None', '') else '無'} "
            f"ATR×{r['trail_atr']} EMA出{'Y' if str(r['ema_exit']) in ('True', 'true') else 'N'} 停損上限{r['max_stop_pct']}")


def _key(r):
    return tuple(str(r[c]) for c in PARAM_COLS)


def consensus(reports: Path, symbols: list[str], top: int, write: str | None):
    data = {}
    for s in symbols:
        path = reports / f"trend-{s}.csv"
        if path.exists():
            data[s] = {_key(r): r for r in csv.DictReader(open(path, encoding="utf-8"))}
    symbols = [s for s in symbols if s in data]
    keys = set().union(*[set(d) for d in data.values()]) if data else set()
    rows = []
    for k in keys:
        per = [data[s].get(k) for s in symbols]
        present = [p for p in per if p]
        nets = [float(p["net_expectancy_pct"]) for p in present]
        rows.append({
            "key": k, "row": present[0], "cover": len(present), "n_pos": sum(x > 0 for x in nets),
            "mean": sum(nets) / len(nets), "worst": min(nets),
            "payoff": sum(float(p["payoff"]) for p in present) / len(present),
            "wr": sum(float(p["win_rate"]) for p in present) / len(present),
            "avg_r": sum(float(p["avg_r"]) for p in present) / len(present),
            "dd": max(float(p["max_dd_pct"]) for p in present),
            "trades": sum(int(p["trades"]) for p in present), "nets": nets,
            "missing": [s for s, p in zip(symbols, per) if not p],
        })
    rows.sort(key=lambda r: (r["n_pos"], r["mean"]), reverse=True)
    lines = []
    def out(s=""):
        print(s); lines.append(s)
    out(f"順勢策略驗證 — {len(symbols)} 標的（{', '.join(symbols)}）組合 {len(rows)}")
    out("判準：全標的淨正、平均賠率 ≥ 1.5、淨 PF ≥ 1.3。欄位：淨正/覆蓋 期望 最差 賠率 勝率 平均R 回撤 總筆")
    out("=" * 120)
    for r in rows[:top]:
        out(fmt(r["row"]))
        out(f"    {r['n_pos']}/{r['cover']}  期望{r['mean']:+.3f}  最差{r['worst']:+.3f}  賠率{r['payoff']:.2f}  勝率{r['wr']:.0f}%  R{r['avg_r']:.2f}  回撤{r['dd']:.1f}%  n={r['trades']}  各標的 " + " ".join(f"{x:+.2f}" for x in r["nets"]) + (f"  缺{r['missing']}" if r["missing"] else ""))
    good = [r for r in rows if r["cover"] == len(symbols) and r["n_pos"] == len(symbols) and r["payoff"] >= 1.5]
    out("=" * 120)
    out(f"全標的淨正且賠率 ≥ 1.5：{len(good)} / {len(rows)}")
    if good:
        b = max(good, key=lambda r: r["mean"])
        out(f"🏆 {fmt(b['row'])}  期望{b['mean']:+.3f} 賠率{b['payoff']:.2f} 勝率{b['wr']:.0f}% R{b['avg_r']:.2f} 回撤{b['dd']:.1f}%")
    else:
        out("⚠️ 沒有組合通過判準 —— 順勢策略在這段資料上不成立，不應上線。")
    if write:
        Path(write).parent.mkdir(parents=True, exist_ok=True)
        with open(write, "w", encoding="utf-8") as f:
            f.write(f"# 順勢策略驗證 {datetime.now(timezone.utc):%Y-%m-%d}\n\n```\n" + "\n".join(lines) + "\n```\n")
        print(f"\n📄 已寫入 {write}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.disable(logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--min-trades", type=int, default=12)
    ap.add_argument("--dump-csv", default=None)
    ap.add_argument("--consensus", action="store_true")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--write", default=None)
    a = ap.parse_args()
    if a.consensus:
        if a.symbols:
            syms = a.symbols.split(",")
        else:
            import yaml
            syms = list(yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))["trading"]["symbols"])
        consensus(Path(a.reports), syms, a.top, a.write)
        return
    run(a.symbol, a.days, a.dump_csv, a.min_trades)


if __name__ == "__main__":
    main()
