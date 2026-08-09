"""Walk-forward 驗證 — 唯一能誠實回答「這策略有沒有 edge」的方法

先前的問題：在同一份資料上掃 1920 組再挑最好的，冠軍必然向上偏誤
（多重比較）。跨標的驗證已證實 BTC 冠軍在其他標的全部虧損。

Walk-forward 的紀律：
    視窗 1（訓練）→ 選出最佳參數 → 視窗 2（測試）用該參數，不再調整
    視窗 2（訓練）→ 重選參數      → 視窗 3（測試）
    ...
測試視窗的績效從未參與參數選擇，因此是真正的樣本外。
把所有測試視窗的交易合併起來做統計檢定，才是可信的 edge 估計。

    python -m backtest.walk_forward --days 730 --train 90 --test 30
"""

import argparse
import asyncio
import hashlib
import itertools
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from backtest.fetch_klines import get_klines_df
from backtest.fidelity import SentimentReplay
from backtest.param_sweep import (
    SWEEP_GRID, _valid_tp_order, fast_backtest, precompute_sides,
)
from src.utils.config import load_config

FEE = 0.08          # % 名目，來回

# Walk-forward 專用粗網格。
# 每折候選數少不是妥協：候選越多，訓練期「最佳」越可能是雜訊，
# 反而讓每一折都在過擬合。完整 1920 組留給單期掃描做探索用。
WF_GRID = {
    "stop_loss_pct":  [2.0, 3.0, 4.0],
    "level_1_pct":    [1.0, 1.5],
    "level_2_pct":    [2.5],
    "level_3_pct":    [3.0],
    "trail_callback": [0.4, 0.8],
    "time_stop_seconds": [1200, 2400, 7200, None],
    "time_stop_no_movement_pct": [0.2, 0.4],
}

SIDES_CACHE = Path("data/sides")


@dataclass
class Fold:
    symbol: str
    train_start: int
    train_end: int
    test_end: int
    chosen: dict
    train_expectancy: float
    test_pnls: list


def _combos(grid: dict | None = None) -> list[dict]:
    if grid is None:
        grid = WF_GRID
    names = list(grid)
    vals = [grid[n] for n in names]
    return [c for c in (dict(zip(names, v)) for v in itertools.product(*vals))
            if _valid_tp_order(c)]


def _cached_sides(sym: str, df, strategy: dict, replay) -> np.ndarray:
    """訊號預算結果快取到磁碟。

    precompute 是全流程最貴的一步（~250 根/秒，2 年資料約 14 分鐘/標的），
    且只要策略參數不變就完全可重用。快取讓中斷後能續跑、重跑近乎免費。
    key 納入策略參數與資料範圍，任何一項改變都會重算。
    """
    fingerprint = json.dumps({
        "strategy": strategy,
        "n": len(df),
        "start": str(df.index[0]),
        "end": str(df.index[-1]),
        "replay": bool(replay),
    }, sort_keys=True, default=str)
    key = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
    path = SIDES_CACHE / f"{sym}_{key}.npy"

    if path.exists():
        try:
            sides = np.load(path)
            if len(sides) == len(df):
                print(f"    （使用快取 {path.name}）", flush=True)
                return sides
        except Exception:  # noqa: BLE001 — 快取毀損就重算
            pass

    sides = precompute_sides(df, {"strategy": strategy, "risk": {}}, sym,
                             faithful=True, sentiment_replay=replay)
    SIDES_CACHE.mkdir(parents=True, exist_ok=True)
    np.save(path, sides)
    return sides


def _expectancy(pnls) -> float:
    return float(np.mean(pnls)) if len(pnls) else -1e9


def run_symbol(sym: str, df, sides, train_bars: int, test_bars: int,
               combos: list[dict], min_trades: int) -> list[Fold]:
    closes = df["close"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    n = len(closes)
    folds: list[Fold] = []

    start = 0
    while start + train_bars + test_bars <= n:
        tr_s, tr_e = start, start + train_bars
        te_e = tr_e + test_bars

        # ── 訓練視窗：選出期望值最高、且樣本足夠的參數 ──
        best, best_exp = None, -1e9
        tr_sides = sides[tr_s:tr_e]
        for c in combos:
            p = fast_backtest(closes[tr_s:tr_e], highs[tr_s:tr_e], lows[tr_s:tr_e],
                              tr_sides, c, 1.0)
            if len(p) < min_trades:
                continue
            e = _expectancy(np.array(p) - FEE)
            if e > best_exp:
                best, best_exp = c, e
        if best is None:
            start += test_bars
            continue

        # ── 測試視窗：只用上面選出的參數，不再調整 ──
        te_p = fast_backtest(closes[tr_e:te_e], highs[tr_e:te_e], lows[tr_e:te_e],
                             sides[tr_e:te_e], best, 1.0)
        folds.append(Fold(sym, tr_s, tr_e, te_e, best, best_exp,
                          list(np.array(te_p) - FEE) if len(te_p) else []))
        start += test_bars

    return folds


def report(folds: list[Fold]):
    all_test = np.array([p for f in folds for p in f.test_pnls])
    train_exp = np.array([f.train_expectancy for f in folds])

    print("\n" + "=" * 72)
    print("  Walk-forward 樣本外結果")
    print("=" * 72)
    print(f"  折數(fold)     {len(folds)}")
    print(f"  樣本外交易數   {len(all_test)}")
    if len(all_test) < 30:
        print("  樣本太少，無法檢定")
        return

    m = all_test.mean()
    se = all_test.std(ddof=1) / np.sqrt(len(all_test))
    t = m / se if se else 0.0
    rng = np.random.default_rng(0)
    boot = np.array([rng.choice(all_test, len(all_test), replace=True).mean()
                     for _ in range(10000)])
    lo, hi = np.percentile(boot, [2.5, 97.5])

    wins = all_test[all_test > 0]
    print(f"  勝率           {len(wins)/len(all_test)*100:.1f}%")
    print(f"  期望值         {m:+.4f}% / 筆（已扣手續費）")
    print(f"  bootstrap CI   [{lo:+.4f}%, {hi:+.4f}%]")
    print(f"  t 統計量       {t:.2f}")
    print(f"  累計           {all_test.sum():+.1f}%")
    print("-" * 72)
    print(f"  訓練期平均期望 {train_exp.mean():+.4f}%  vs  樣本外 {m:+.4f}%")
    drop = train_exp.mean() - m
    print(f"  過擬合落差     {drop:+.4f}%  "
          f"({'訓練期表現大幅無法延續' if drop > 0.1 else '落差可接受'})")
    print("-" * 72)
    if t > 2 and lo > 0:
        print("  ✅ 樣本外證實有 edge")
    else:
        print("  ❌ 樣本外未證實 edge —— 訓練期的優勢無法延續到未見資料")
    print("=" * 72)

    # 每個標的的樣本外表現，檢查是否只靠少數標的
    print("\n  各標的樣本外:")
    for sym in sorted({f.symbol for f in folds}):
        p = np.array([x for f in folds if f.symbol == sym for x in f.test_pnls])
        if len(p):
            print(f"    {sym:<10} n={len(p):>5}  期望 {p.mean():+.4f}%  累計 {p.sum():+7.1f}%")


def main():
    ap = argparse.ArgumentParser(description="Walk-forward 驗證")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--train", type=int, default=90, help="訓練視窗天數")
    ap.add_argument("--test", type=int, default=30, help="測試視窗天數")
    ap.add_argument("--min-trades", type=int, default=20, help="訓練視窗最少交易數")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.disable(logging.INFO)

    cfg = load_config()
    strategy = cfg["bots"]["futures"]["strategy"]
    combos = _combos()
    bars_per_day = 288
    train_bars, test_bars = args.train * bars_per_day, args.test * bars_per_day

    replay = SentimentReplay.load(days=min(args.days + 10, 400))
    replay = replay if replay.available else None
    print(f"  參數組合 {len(combos)} 組 | 訓練 {args.train}天 / 測試 {args.test}天 "
          f"| 情緒回放 {'有' if replay else '無'}")

    folds: list[Fold] = []
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    for sym in syms:
        df = asyncio.run(get_klines_df(sym, "5m", args.days))
        print(f"  {sym}: {len(df):,} 根，計算訊號中...", flush=True)
        sides = _cached_sides(sym, df, strategy, replay)
        f = run_symbol(sym, df, sides, train_bars, test_bars, combos, args.min_trades)
        folds += f
        print(f"    → {len(f)} folds，樣本外 {sum(len(x.test_pnls) for x in f)} 筆",
              flush=True)

    report(folds)


if __name__ == "__main__":
    main()
