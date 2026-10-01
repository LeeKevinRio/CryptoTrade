"""出場結構重設計掃描 —— 目標：期望值與賠率優先，勝率可以低一點

現行結構（高勝率、小賺大賠）的四個結構性原因與對應機制：
  1. 分批進場在攤平（跌了才加碼）→ entry_mode: full / dca / pyramid（漲了才加碼）
  2. 第一階停利後停損仍在 -5% → breakeven_after_l1：L1 成交後停損移到進場價
  3. 移動停利 0.4% 太緊吃不到大行情 → trail_callback_after_l1：L1 後改用寬追蹤
  4. 固定 % 停損不看結構 → stop_mode: swing（最近 N 根低點下方，stop_loss_pct 當上限）

模擬語義對齊實盤：停損優先；階梯以「訊號價」為基準、部分平倉；分批成交以 bar 高低點判定。
損益以「整筆滿額部位名目」為 100%（分批未全進時金額自然較小 = 資金效率）。

    python -m backtest.exit_redesign --real --symbol BTCUSDT --days 90 --thresholds 50,55 \
        --dump-csv reports/exit-redesign-BTCUSDT.csv
    python -m backtest.exit_redesign --consensus            # 跨標的彙整 reports/exit-redesign-*.csv
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

from src.utils.performance import EST_FEE_RATE

ENTRY_MODES = {
    "full":    [(1.0, 0.0)],
    "dca":     [(0.5, 0.0), (0.3, -0.5), (0.2, -1.0)],   # 現行：跌了加碼
    "pyramid": [(0.5, 0.0), (0.3, +0.5), (0.2, +1.0)],   # 順勢：漲了加碼
}

GRID = {
    "entry_mode":             list(ENTRY_MODES.keys()),
    "stop_mode":              ["pct", "swing"],
    "stop_loss_pct":          [3.0, 5.0],                # swing 模式下為上限
    "breakeven_after_l1":     [False, True],
    "trail_callback_after_l1": [0.4, 1.0, 1.5],
    "tp_levels":              [(1.5, 2.5, 3.0), (1.5, 3.0, 5.0)],
    "close_pcts":             [(40, 35, 25), (25, 25, 50)],
}
FIXED = {"trail_activation": 1.0, "trail_callback": 0.4, "swing_lookback": 12, "swing_buffer_pct": 0.2}


def simulate(closes, highs, lows, sides, p: dict, warmup: int = 50) -> list[float]:
    """回傳每筆交易的 pnl%（以滿額部位名目為 100%，未扣費）。"""
    batches = ENTRY_MODES[p["entry_mode"]]
    tp = p["tp_levels"]
    w = [c / 100 for c in p["close_pcts"]]
    act = p.get("trail_activation", FIXED["trail_activation"])
    cb0 = p.get("trail_callback", FIXED["trail_callback"])
    cb1 = p["trail_callback_after_l1"]
    lb = p.get("swing_lookback", FIXED["swing_lookback"])
    buf = p.get("swing_buffer_pct", FIXED["swing_buffer_pct"])
    n = len(closes)
    pnls: list[float] = []

    i = warmup
    while i < n:
        side = sides[i]
        if side == 0:
            i += 1
            continue
        p0 = closes[i]
        sgn = 1 if side == 1 else -1

        # 停損距離（% of p0）
        if p["stop_mode"] == "swing":
            ref = lows[max(0, i - lb):i + 1].min() if side == 1 else highs[max(0, i - lb):i + 1].max()
            dist = abs(p0 - ref) / p0 * 100 + buf
            dist = min(max(dist, 0.5), p["stop_loss_pct"])
        else:
            dist = p["stop_loss_pct"]
        stop_price = p0 * (1 - sgn * dist / 100)

        units = [[batches[0][0], p0]]             # [frac, price]
        pending = list(batches[1:])
        triggered = [False, False, False]
        realized = 0.0
        highest = 0.0
        trailing = False
        exited = False

        def open_frac():
            return sum(u[0] for u in units)

        def close(q, px):
            """平掉 q（滿額比例），按各批比例攤分；回傳實現 pnl%"""
            nonlocal units
            tot = open_frac()
            if tot <= 1e-12:
                return 0.0
            out = 0.0
            new = []
            for frac, price in units:
                part = q * frac / tot
                out += part * sgn * (px - price) / p0 * 100
                left = frac - part
                if left > 1e-12:
                    new.append([left, price])
            units = new
            return out

        j = i + 1
        while j < n:
            c, h, l = closes[j], highs[j], lows[j]
            best = sgn * ((h if side == 1 else l) - p0) / p0 * 100
            worst = sgn * ((l if side == 1 else h) - p0) / p0 * 100
            pnl = sgn * (c - p0) / p0 * 100
            highest = max(highest, best)

            # 分批成交（同一根先算成交再算出場；DCA 觸價在下方、pyramid 在上方）
            still = []
            for frac, off in pending:
                trig = p0 * (1 + sgn * off / 100)
                hit = (l <= trig) if (sgn * off) < 0 or (side == 1 and off < 0) else (h >= trig)
                if side == -1:
                    hit = (h >= trig) if off < 0 else (l <= trig)
                if hit:
                    units.append([frac, trig])
                else:
                    still.append((frac, off))
            pending = still

            reason = None
            # 1. 停損（優先）
            if (side == 1 and l <= stop_price) or (side == -1 and h >= stop_price):
                realized += close(open_frac(), stop_price)
                reason = "SL"
            else:
                # 2. 階梯
                for k in range(3):
                    if not triggered[k] and best >= tp[k]:
                        triggered[k] = True
                        q = min(w[k] * open_frac() / max(open_frac(), 1e-12) * open_frac(), open_frac())
                        # 以「當下已開部位」的比例平倉（對齊軟體端 total_quantity 隨加碼更新）
                        q = min(w[k] * open_frac(), open_frac())
                        realized += close(q, p0 * (1 + sgn * tp[k] / 100))
                        if k == 0 and p["breakeven_after_l1"]:
                            avg = sum(f * pr for f, pr in units) / max(open_frac(), 1e-12) if units else p0
                            stop_price = avg
                            pending = []      # 已進入獲利管理，不再攤平
                if open_frac() <= 1e-9:
                    reason = "TP"
                else:
                    # 3. 移動停利：L1 前緊、L1 後寬
                    cb = cb1 if triggered[0] else cb0
                    if highest >= act:
                        trailing = True
                    if trailing and (highest - pnl) >= cb:
                        realized += close(open_frac(), p0 * (1 + sgn * (highest - cb) / 100))
                        reason = "trail"
            if reason is not None:
                pnls.append(round(realized, 3))
                i = j + 1
                exited = True
                break
            j += 1
        if not exited:
            break
    return pnls


def stats(pnls: list[float], fee_pct: float = EST_FEE_RATE * 100) -> dict:
    if not pnls:
        return {"trades": 0}
    net = [x - fee_pct for x in pnls]
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
        "trades": len(net),
        "win_rate": round(len(wins) / len(net) * 100, 1),
        "avg_win": round(aw, 3), "avg_loss": round(al, 3),
        "payoff": round(abs(aw / al), 2) if al else 0.0,
        "net_pnl_pct": round(sum(net), 2),
        "net_expectancy_pct": round(sum(net) / len(net), 3),
        "net_profit_factor": round(sum(wins) / max(abs(sum(losses)), 0.01), 2),
        "max_dd_pct": round(dd, 2),
    }


PARAM_COLS = ["signal_threshold", "entry_mode", "stop_mode", "stop_loss_pct",
              "breakeven_after_l1", "trail_callback_after_l1", "tp_levels", "close_pcts"]
METRIC_COLS = ["trades", "win_rate", "avg_win", "avg_loss", "payoff", "net_pnl_pct",
               "net_expectancy_pct", "net_profit_factor", "max_dd_pct"]


def run(symbol: str, days: int, thresholds: list[float], dump_csv: str | None, min_trades: int = 15):
    import asyncio
    from src.utils.config import load_config
    from backtest.fetch_klines import get_klines_df
    from backtest.param_sweep import _flat_cfg, precompute_sides
    from backtest.fidelity import SentimentReplay

    config = load_config()
    bot_cfg = config["bots"]["futures"]
    base_flat = _flat_cfg(bot_cfg)
    df = asyncio.run(get_klines_df(symbol, "5m", days))
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    replay = SentimentReplay.load(days=max(days + 10, 60))
    replay = replay if replay.available else None
    print(f"  {symbol}: {len(df)} 根 | {df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}", flush=True)

    rows = []
    names = list(GRID.keys())
    for thr in thresholds:
        flat = copy.deepcopy(base_flat)
        flat.setdefault("strategy", {})["medium_signal_threshold"] = thr
        print(f"  [門檻 {thr:g}] 計算進場訊號...", flush=True)
        sides = precompute_sides(df, flat, symbol, faithful=True, sentiment_replay=replay)
        print(f"  [門檻 {thr:g}] {int(np.count_nonzero(sides))} 個訊號", flush=True)
        for values in itertools.product(*[GRID[k] for k in names]):
            p = dict(zip(names, values))
            p.update(FIXED)
            s = stats(simulate(closes, highs, lows, sides, p))
            if s["trades"] < min_trades:
                continue
            rows.append({"signal_threshold": thr, **{k: p[k] for k in names}, **s})

    # 現行設定基準（dca / pct / SL5 / 不保本 / 0.4 / 1.5,2.5,3 / 40,35,25）
    rows.sort(key=lambda r: r["net_expectancy_pct"], reverse=True)
    if dump_csv:
        Path(dump_csv).parent.mkdir(parents=True, exist_ok=True)
        with open(dump_csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(PARAM_COLS + METRIC_COLS)
            for r in rows:
                wr.writerow([r[c] for c in PARAM_COLS] + [r[c] for c in METRIC_COLS])
        print(f"  📄 {dump_csv}（{len(rows)} 組）", flush=True)
    for r in rows[:10]:
        print("  ", fmt(r), f"n={r['trades']} 勝率{r['win_rate']}% 賠率{r['payoff']} 淨期望{r['net_expectancy_pct']:+.3f} 回撤{r['max_dd_pct']}")
    return rows


def fmt(r: dict) -> str:
    return (f"門檻{float(r['signal_threshold']):g} {r['entry_mode']} {r['stop_mode']}SL{r['stop_loss_pct']} "
            f"保本{'Y' if str(r['breakeven_after_l1']) in ('True', 'true') else 'N'} 追{r['trail_callback_after_l1']} "
            f"TP{r['tp_levels']} 比{r['close_pcts']}")


BASELINE = {"signal_threshold": 55.0, "entry_mode": "dca", "stop_mode": "pct", "stop_loss_pct": 5.0,
            "breakeven_after_l1": False, "trail_callback_after_l1": 0.4,
            "tp_levels": (1.5, 2.5, 3.0), "close_pcts": (40, 35, 25)}


def _key(r: dict) -> tuple:
    return tuple(str(r[c]) for c in PARAM_COLS)


def consensus(reports: Path, symbols: list[str], top: int, write: str | None):
    data = {}
    for s in symbols:
        path = reports / f"exit-redesign-{s}.csv"
        if not path.exists():
            print(f"⚠️ 缺 {path}")
            continue
        data[s] = {_key(r): r for r in csv.DictReader(open(path, encoding="utf-8"))}
    symbols = [s for s in symbols if s in data]
    keys = set().union(*[set(d) for d in data.values()])
    out_rows = []
    for k in keys:
        per = [data[s].get(k) for s in symbols]
        if any(p is None for p in per):
            continue
        nets = [float(p["net_expectancy_pct"]) for p in per]
        pay = [float(p["payoff"]) for p in per]
        wr = [float(p["win_rate"]) for p in per]
        dd = [float(p["max_dd_pct"]) for p in per]
        trades = sum(int(p["trades"]) for p in per)
        out_rows.append({
            "key": k, "row": per[0], "n_pos": sum(x > 0 for x in nets), "worst": min(nets),
            "mean": sum(nets) / len(nets), "payoff": sum(pay) / len(pay), "wr": sum(wr) / len(wr),
            "dd": max(dd), "trades": trades, "nets": nets,
        })
    out_rows.sort(key=lambda r: (r["n_pos"], r["mean"]), reverse=True)
    lines = []
    def out(s=""):
        print(s); lines.append(s)
    base = next((r for r in out_rows if r["key"] == tuple(str(BASELINE[c]) for c in PARAM_COLS)), None)
    out(f"出場結構重設計 — 跨 {len(symbols)} 標的彙整（{', '.join(symbols)}）組合 {len(out_rows)}")
    out("排序：全標的淨正數 → 平均淨期望。欄位：淨正 / 平均淨期望% / 最差 / 平均賠率 / 平均勝率 / 最大回撤 / 總筆數")
    out("=" * 120)
    if base:
        out("【現行結構】" + fmt(base["row"]))
        out(f"    {base['n_pos']}/{len(symbols)}  期望{base['mean']:+.3f}  最差{base['worst']:+.3f}  賠率{base['payoff']:.2f}  勝率{base['wr']:.0f}%  回撤{base['dd']:.1f}%  n={base['trades']}")
        out("-" * 120)
    for r in out_rows[:top]:
        out(fmt(r["row"]))
        out(f"    {r['n_pos']}/{len(symbols)}  期望{r['mean']:+.3f}  最差{r['worst']:+.3f}  賠率{r['payoff']:.2f}  勝率{r['wr']:.0f}%  回撤{r['dd']:.1f}%  n={r['trades']}  各標的 " + " ".join(f"{x:+.2f}" for x in r["nets"]))
    full = [r for r in out_rows if r["n_pos"] == len(symbols)]
    good = [r for r in full if r["payoff"] >= 1.0]
    out("=" * 120)
    out(f"全標的淨正：{len(full)}；其中賠率 ≥ 1：{len(good)}")
    if good:
        b = max(good, key=lambda r: r["mean"])
        out(f"🏆 賠率≥1 且期望最高：{fmt(b['row'])}  期望{b['mean']:+.3f} 賠率{b['payoff']:.2f} 勝率{b['wr']:.0f}% 回撤{b['dd']:.1f}%")
    if write:
        Path(write).parent.mkdir(parents=True, exist_ok=True)
        with open(write, "w", encoding="utf-8") as f:
            f.write(f"# 出場結構重設計彙整 {datetime.now(timezone.utc):%Y-%m-%d}\n\n```\n" + "\n".join(lines) + "\n```\n")
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
    ap.add_argument("--thresholds", default="50,55")
    ap.add_argument("--min-trades", type=int, default=15)
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
    run(a.symbol, a.days, [float(t) for t in a.thresholds.split(",")], a.dump_csv, a.min_trades)


if __name__ == "__main__":
    main()
