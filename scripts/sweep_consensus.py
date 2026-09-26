"""跨標的彙整參數掃描結果 — 找「所有現役標的都扣費淨正」的穩健組合

單一標的排名第一的組合多半是過擬合；真金要用的參數必須在每個標的、
同一段期間都站得住。本工具讀 reports/sweep-full-<SYMBOL>.csv（param_sweep --dump-csv
產出），對每個參數組合統計：幾個標的淨期望 > 0、最差標的淨期望、平均淨期望。

    python -m scripts.sweep_consensus                      # 現役七標的
    python -m scripts.sweep_consensus --symbols BTCUSDT,ETHUSDT --top 20
    python -m scripts.sweep_consensus --write reports/sweep-consensus-2026-09-26.md

排名：先比「淨正標的數」，再比「最差標的淨期望」（min-max，保守），最後比平均。
組合若在某標的被 min_trades 濾掉（樣本不足）視為該標的不通過。
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PARAM_COLS = [
    "signal_threshold", "stop_loss_pct", "level_1_pct", "level_2_pct", "level_3_pct",
    "trail_callback", "time_stop_seconds", "time_stop_no_movement_pct",
]
EST_FEE = 0.08   # % / 筆，與 src.utils.performance.EST_FEE_RATE 一致


def _active_symbols() -> list[str]:
    try:
        import yaml
        cfg = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
        return list(cfg["trading"]["symbols"])
    except Exception:
        return ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SUIUSDT", "LINKUSDT", "TAOUSDT", "DOGEUSDT"]


def _num(v):
    if v in ("", "None", None):
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except ValueError:
        return v


def load(symbol: str, reports: Path) -> dict[tuple, dict]:
    path = reports / f"sweep-full-{symbol}.csv"
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = tuple(_num(row.get(c)) for c in PARAM_COLS)
            exp = float(row["expectancy_pct"])
            net = row.get("net_expectancy_pct")
            net = float(net) if net not in (None, "") else exp - EST_FEE
            out[key] = {
                "trades": int(row["trades"]), "win_rate": float(row["win_rate"]),
                "expectancy": exp, "net": net,
                "net_pf": float(row.get("net_profit_factor") or 0),
                "dd": float(row["max_dd_pct"]),
            }
    return out


def fmt_key(key: tuple) -> str:
    thr, sl, l1, l2, l3, tr, ts, nm = key
    ts_txt = "時停關" if ts is None else f"時停{int(ts)//60}分/{nm}"
    thr_txt = f"門檻{thr:g} " if thr is not None else ""
    return f"{thr_txt}SL{sl} TP{l1}/{l2}/{l3} tr{tr} {ts_txt}"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="跨標的彙整參數掃描")
    ap.add_argument("--symbols", default=None, help="逗號分隔；預設 settings.yaml 現役標的")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--write", default=None, help="同時把結果寫成 markdown")
    args = ap.parse_args()

    symbols = args.symbols.split(",") if args.symbols else _active_symbols()
    reports = Path(args.reports)
    data = {s: load(s, reports) for s in symbols}
    missing = [s for s, d in data.items() if not d]
    if missing:
        print(f"⚠️  缺少報表：{', '.join(missing)}（略過）")
        symbols = [s for s in symbols if data[s]]
    if not symbols:
        raise SystemExit("沒有任何 sweep-full-*.csv 可讀")

    keys = set()
    for s in symbols:
        keys |= set(data[s].keys())

    rows = []
    for key in keys:
        per = [data[s].get(key) for s in symbols]
        nets = [p["net"] if p else None for p in per]
        n_pos = sum(1 for n in nets if n is not None and n > 0)
        present = [n for n in nets if n is not None]
        worst = min(present) if len(present) == len(symbols) else (min(present + [-99]) if present else -99)
        mean = sum(present) / len(present) if present else -99
        trades = sum(p["trades"] for p in per if p)
        rows.append((n_pos, worst, mean, trades, key, per))
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)

    lines = []
    def out(s=""):
        print(s); lines.append(s)

    out(f"跨標的參數彙整 — {len(symbols)} 標的：{', '.join(symbols)}   組合數 {len(rows)}")
    out(f"判準：淨期望（扣 {EST_FEE}%/筆）> 0 的標的數 → 最差標的淨期望 → 平均")
    out("=" * 100)
    hdr = f"{'參數組合':<52}{'淨正':>5}{'最差':>8}{'平均':>8}{'總筆':>6}  各標的淨期望%"
    out(hdr); out("-" * 100)
    for n_pos, worst, mean, trades, key, per in rows[: args.top]:
        cells = " ".join(f"{p['net']:+.2f}" if p else "  n/a" for p in per)
        out(f"{fmt_key(key):<52}{n_pos:>3}/{len(symbols)}{worst:>+8.2f}{mean:>+8.2f}{trades:>6}  {cells}")
    out("=" * 100)
    full = [r for r in rows if r[0] == len(symbols)]
    out(f"全標的淨正的組合：{len(full)} / {len(rows)}")
    if full:
        best = full[0]
        out(f"🏆 最穩健（最差標的最高）：{fmt_key(best[4])}  最差 {best[1]:+.2f}%  平均 {best[2]:+.2f}%")
    else:
        out("⚠️  沒有任何組合能讓所有標的扣費後淨正 —— 不要換參數，問題在進場品質或標的清單。")

    if args.write:
        Path(args.write).parent.mkdir(parents=True, exist_ok=True)
        with open(args.write, "w", encoding="utf-8") as f:
            f.write(f"# 跨標的參數彙整 {datetime.now(timezone.utc):%Y-%m-%d}\n\n```\n")
            f.write("\n".join(lines)); f.write("\n```\n")
        print(f"\n📄 已寫入 {args.write}")


if __name__ == "__main__":
    main()
