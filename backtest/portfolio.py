"""投資組合型候選策略 —— 與「看 K 線找進場點」不同的資訊來源

    xsmom   橫截面動能：每週做多過去 L 天最強的 k 個幣、做空最弱的 k 個（或只做多）
    carry   資金費率套利：資金費率持續偏高的幣，買現貨 + 空永續（價格中性），收資金費

共同設定：日 K、每 7 天調倉一次；週報酬已扣手續費與（多單付出 / 空單收到的）資金費。
walk-forward：訓練 52 週選參 → 測試 4 週，滾動；樣本外週報酬交給 validation 上線關卡。

事先定好的檢定調整（因為每筆＝整個組合一週，不是單筆交易）：
    樣本數門檻 500 筆 → 80 週；「不靠單一標的」改用各幣對總報酬的貢獻歸因。

    python -m backtest.portfolio --universe dev --months 36 --out reports/x.md
"""

import argparse
import itertools
import logging
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtest.validation import Criteria, Trade, gate, leverage_table

PERP_FEE = 0.04 / 100     # taker 單邊
SPOT_FEE = 0.10 / 100     # 現貨單邊
HOLD = 7                  # 調倉間隔（天）
TRAIN_W, TEST_W = 52, 4
WARMUP = 30              # 所有參數組共用同一組調倉日（≥ 最長回看），週報酬才能逐週對齊

GRIDS = {
    "xsmom": {"lookback": [7, 14, 28], "k": [3, 5], "mode": ["ls", "long"]},
    "carry": {"entry_apr": [10, 20, 30], "exit_ratio": [0.0, 0.5], "lookback": [3, 7]},
}
PORTFOLIO_CRITERIA = Criteria(min_trades=80)


def combos(strategy: str) -> list[dict]:
    g = GRIDS[strategy]
    return [dict(zip(g, v)) for v in itertools.product(*g.values())]


def total_trials() -> int:
    return sum(len(combos(s)) for s in GRIDS)


@dataclass
class Week:
    date: pd.Timestamp
    ret: float                 # 組合週報酬（小數，已扣費）
    contrib: dict              # 各幣貢獻（小數）


def _rebalance_days(n: int) -> range:
    return range(WARMUP, n - HOLD, HOLD)


def xsmom_weeks(closes: pd.DataFrame, funding: pd.DataFrame, lookback: int, k: int,
                mode: str) -> list[Week]:
    """第 t 天收盤：只用 ≤ t 的價格排名；持有到 t+7 收盤"""
    px = closes.to_numpy(float)
    fd = funding.fillna(0.0).to_numpy(float)
    cols = list(closes.columns)
    n = len(px)
    prev_w = np.zeros(len(cols))
    out = []
    for t in _rebalance_days(n):
        past = px[t] / px[t - lookback] - 1
        fwd = px[t + HOLD] / px[t] - 1
        ok = np.isfinite(past) & np.isfinite(fwd)
        w = np.zeros(len(cols))
        idx = np.where(ok)[0]
        if len(idx) >= 2 * k:
            order = idx[np.argsort(past[idx])]
            if mode == "ls":
                w[order[-k:]] = 0.5 / k
                w[order[:k]] = -0.5 / k
            else:
                w[order[-k:]] = 1.0 / k
        fund = fd[t + 1:t + HOLD + 1].sum(axis=0)          # 持有期間的資金費
        leg = w * (np.nan_to_num(fwd) - fund)
        cost = np.abs(w - prev_w).sum() * PERP_FEE
        out.append(Week(closes.index[t], float(leg.sum() - cost),
                        {cols[i]: float(leg[i]) for i in np.nonzero(w)[0]}))
        prev_w = w
    return out


def carry_weeks(closes: pd.DataFrame, funding: pd.DataFrame, entry_apr: float,
                exit_ratio: float, lookback: int) -> list[Week]:
    """現貨多 + 永續空（各占該幣配置的一半資金），價格漲跌互相抵銷，只賺資金費。

    進場：過去 lookback 天平均資金費年化 ≥ entry_apr%；出場：降到 entry_apr × exit_ratio 以下。
    簡化：忽略期現基差波動與現貨閒置資金利息（真實結果會有額外雜訊）。
    """
    fd = funding.fillna(0.0)
    has_px = closes.notna().to_numpy()
    apr = (fd.rolling(lookback).mean() * 365 * 100).to_numpy(float)    # 每日合計 × 365
    f = fd.to_numpy(float)
    cols = list(closes.columns)
    n = len(f)
    held: set[int] = set()
    prev_w = np.zeros(len(cols))
    out = []
    for t in _rebalance_days(n):
        for i in range(len(cols)):
            if not has_px[t, i] or not np.isfinite(apr[t, i]):
                held.discard(i)
            elif i in held and apr[t, i] < entry_apr * exit_ratio:
                held.discard(i)
            elif i not in held and apr[t, i] >= entry_apr:
                held.add(i)
        w = np.zeros(len(cols))
        if held:
            w[list(held)] = 1.0 / len(held)
        fund = f[t + 1:t + HOLD + 1].sum(axis=0)
        leg = w * 0.5 * fund                                    # 空永續收資金費（費率為正時）
        # 每單位配置的進出場成本：一半現貨、一半永續
        cost_rate = 0.5 * (SPOT_FEE + PERP_FEE)
        cost = np.abs(w - prev_w) * cost_rate
        leg = leg - cost
        out.append(Week(closes.index[t], float(leg.sum()),
                        {cols[i]: float(leg[i]) for i in np.nonzero(w + prev_w)[0]}))
        prev_w = w
    return out


BUILDERS = {"xsmom": xsmom_weeks, "carry": carry_weeks}


def walk_forward(closes, funding, strategy: str) -> list[Week]:
    """每組參數先算出整段週報酬（本身是因果的），再以前 52 週平均選參、套用到後 4 週"""
    series = [(p, BUILDERS[strategy](closes, funding, **p)) for p in combos(strategy)]
    n = min(len(s) for _, s in series)
    out: list[Week] = []
    start = 0
    while start + TRAIN_W + TEST_W <= n:
        tr = slice(start, start + TRAIN_W)
        best = max(series, key=lambda ps: np.mean([w.ret for w in ps[1][tr]]))
        out += best[1][start + TRAIN_W:start + TRAIN_W + TEST_W]
        start += TEST_W
    return out


def evaluate(weeks: list[Week], n_trials: int) -> str:
    trades = [Trade("PORT", f"{w.date:%Y-%m}", w.ret * 100) for w in weeks]
    contrib: dict[str, float] = {}
    for w in weeks:
        for s, v in w.contrib.items():
            contrib[s] = contrib.get(s, 0.0) + v * 100
    r = np.array([w.ret for w in weeks])
    eq = np.cumprod(1 + r)
    dd = 1 - eq / np.maximum.accumulate(eq)
    lines = [
        gate(trades, n_trials, PORTFOLIO_CRITERIA, symbol_contrib=contrib).render(),
        "",
        f"  樣本外 {len(r)} 週 | 週勝率 {np.mean(r > 0)*100:.1f}% | 平均週報酬 {r.mean()*100:+.3f}% "
        f"| 年化 {((1 + r.mean()) ** 52 - 1)*100:+.1f}% | 1x 累計 {(eq[-1]-1)*100 if len(eq) else 0:+.1f}% "
        f"| 最大回撤 {dd.max()*100 if len(dd) else 0:.1f}%",
        "",
        leverage_table(list(r * 100), 100, leverages=(1, 2, 3, 5), n_trades=52),
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="投資組合型候選策略 walk-forward")
    ap.add_argument("--symbols", required=True, help="逗號分隔的標的清單")
    ap.add_argument("--months", type=int, default=36)
    ap.add_argument("--strategies", default="xsmom,carry")
    ap.add_argument("--n-trials", type=int, default=None, help="預設 = 全部策略組合總數")
    ap.add_argument("--title", default="")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.disable(logging.INFO)

    from backtest.vision_data import daily_panel
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    closes, funding = daily_panel(syms, args.months)
    closes = closes.dropna(axis=1, how="all")
    nt = args.n_trials or total_trials()
    md = [f"# 投資組合型候選 {args.title}".rstrip(), "",
          f"資料：{closes.index[0]:%Y-%m-%d} ~ {closes.index[-1]:%Y-%m-%d}，{closes.shape[1]} 個幣："
          f"{' '.join(closes.columns)}", "",
          f"walk-forward 訓練 {TRAIN_W} 週 / 測試 {TEST_W} 週；多重比較校正 {nt} 組；"
          f"樣本數門檻 {PORTFOLIO_CRITERIA.min_trades} 週", ""]
    for s in args.strategies.split(","):
        weeks = walk_forward(closes, funding.reindex(columns=closes.columns), s)
        md += [f"## {s}", "```", evaluate(weeks, nt) if weeks else "  無樣本外資料", "```", ""]
        if s == "carry":
            md += ["> 註：carry 為價格中性（現貨多 + 永續空），回測未計期現基差波動與交易所風險；",
                   "> 槓桿表對它意義不大 —— 它的報酬上限就是資金費率本身（低風險低報酬型）。", ""]
    text = "\n".join(md)
    print(text)
    if args.out:
        from pathlib import Path
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
