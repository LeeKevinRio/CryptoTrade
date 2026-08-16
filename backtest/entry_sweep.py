"""進場參數網格掃描 — 固定出場參數（settings.yaml 現值），掃進場門檻

出場端已由 param_sweep 全網格 + 跨標的驗證定案；本工具反過來：
出場固定，掃 min_signals / medium / strong 門檻，找進場品質與頻率的最佳平衡。
每個進場組合都要全量重算訊號（掃描中最貴的部分），故網格刻意保持小。

    python -m backtest.entry_sweep --symbol BTCUSDT --days 90 \
        --dump-csv reports/entry-sweep-BTCUSDT.csv
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

from src.utils.config import load_config
from backtest.param_sweep import (
    SWEEP_GRID, _flat_cfg, _read_path, _row_from_pnls,
    fast_backtest, precompute_sides,
)

ENTRY_GRID = {
    "min_signals": [2, 3, 4],
    "medium_signal_threshold": [45, 50, 55],
    "strong_signal_threshold": [62, 70],
}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.disable(logging.INFO)

    ap = argparse.ArgumentParser(description="進場參數網格掃描")
    ap.add_argument("--bot", default="futures")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--min-trades", type=int, default=15)
    ap.add_argument("--dump-csv", default=None)
    ap.add_argument("--trend-filter", default=None, choices=["on", "off"])
    args = ap.parse_args()

    config = load_config()
    bot_cfg = config.get("bots", {}).get(args.bot)
    if not bot_cfg:
        raise SystemExit(f"config 找不到 bot: {args.bot}")
    if args.trend_filter is not None:
        bot_cfg.setdefault("strategy", {}).setdefault("trend_filter", {})["enabled"] = \
            args.trend_filter == "on"

    import asyncio
    from backtest.fetch_klines import get_klines_df
    df = asyncio.run(get_klines_df(args.symbol, "5m", args.days))
    print(f"  真實資料 {args.symbol}：{len(df)} 根 | "
          f"{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}", flush=True)

    base_flat = _flat_cfg(bot_cfg)
    # 出場端固定為 settings.yaml 現值
    exit_params = {
        name: _read_path(base_flat, path) for name, (path, _) in SWEEP_GRID.items()
    }
    trail_activation = base_flat.get("risk", {}).get("trailing_stop", {}).get("activation_pct", 1.5)

    from backtest.fidelity import SentimentReplay
    replay = SentimentReplay.load(days=max(args.days + 10, 60))
    if not replay.available:
        replay = None

    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)

    names = list(ENTRY_GRID.keys())
    combos = [dict(zip(names, values))
              for values in itertools.product(*ENTRY_GRID.values())
              if dict(zip(names, values))["medium_signal_threshold"]
              < dict(zip(names, values))["strong_signal_threshold"]]
    print(f"  進場組合 {len(combos)} 組（每組全量重算訊號）", flush=True)

    rows = []
    for i, combo in enumerate(combos, 1):
        flat_c = copy.deepcopy(base_flat)
        flat_c.setdefault("strategy", {}).update(combo)
        sides = precompute_sides(df, flat_c, args.symbol, faithful=True,
                                 sentiment_replay=replay)
        n_signals = int(np.count_nonzero(sides))
        pnls = fast_backtest(closes, highs, lows, sides, exit_params, trail_activation)
        row = _row_from_pnls(pnls, combo)
        rows.append((combo, n_signals, row))
        print(f"  [{i}/{len(combos)}] {combo} → 訊號={n_signals} 筆={row.trades} "
              f"勝率={row.win_rate:.0f}% 期望={row.expectancy_pct:+.3f}%", flush=True)

    eligible = [(c, n, r) for c, n, r in rows if r.trades >= args.min_trades]
    eligible.sort(key=lambda t: t[2].expectancy_pct, reverse=True)

    if args.dump_csv:
        Path(args.dump_csv).parent.mkdir(parents=True, exist_ok=True)
        with open(args.dump_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(names + ["n_signals", "trades", "win_rate", "total_pnl_pct",
                                "payoff", "profit_factor", "expectancy_pct", "max_dd_pct"])
            for combo, n_signals, r in eligible:
                w.writerow([combo[k] for k in names]
                           + [n_signals, r.trades, r.win_rate, r.total_pnl_pct,
                              r.payoff, r.profit_factor, r.expectancy_pct, r.max_dd_pct])
        print(f"  📄 已寫入 {args.dump_csv}（{len(eligible)} 組）", flush=True)

    print(f"\n  === {args.symbol} 進場掃描 Top 5（按期望值）===")
    for combo, n_signals, r in eligible[:5]:
        print(f"  {combo}  筆={r.trades} 勝率={r.win_rate:.0f}% "
              f"總盈虧={r.total_pnl_pct:+.1f}% 期望={r.expectancy_pct:+.3f}%")


if __name__ == "__main__":
    main()
