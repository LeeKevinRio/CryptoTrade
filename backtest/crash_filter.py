"""大盤急跌暫停 —— 驗證「BTC 急跌時不開多單（可選：急漲時不開空單）」是否真的有幫助

起因（2026-10-19）：前 20 名 14 天重播的虧損集中在 10/7、10/10、10/12 三天（合計約 -235U），
大盤一急跌，多個幣的抄底多單同時停損。這個規則是「看到這段虧損才想到的」，
所以必須用一整年、且開發時沒用過的另一半標的驗證，不能直接上真金。

事先定好的流程（跑之前寫死，不看結果再改）：
    1. 12 種組合：跌幅門檻 2/3/4% × 回看 4/12 小時 × 是否同時在急漲時擋空單
    2. 18 個標的依字母序奇偶切成 A、B 兩半
    3. 只看 A 半選組合：上線關卡過關項數多者勝，平手比 t 值
    4. B 半只驗被選中的那一組（n_trials=1），並與「不加過濾」逐項對照

    # 每個標的（CI matrix）產出 baseline + 12 組的樣本外逐筆 CSV
    python -m backtest.walk_forward --symbols SOLUSDT --days 365 --fixed --crash-dump out/
    # 彙整
    python -m backtest.crash_filter --dir out/ --out reports/crash-filter.md
"""

import argparse
import glob
import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

VARIANTS = [
    {"drop": d, "hours": h, "both": b}
    for d, h, b in itertools.product((2.0, 3.0, 4.0), (4, 12), (False, True))
]
BARS_PER_HOUR = 12        # 5m


def variant_name(v: dict) -> str:
    return f"d{v['drop']:g}_h{v['hours']}_{'both' if v['both'] else 'long'}"


def btc_return(btc_close: pd.Series, index: pd.DatetimeIndex, hours: int) -> np.ndarray:
    """對齊到標的 K 線時間的 BTC 過去 hours 小時報酬（只用 ≤ 當下的 BTC 收盤）"""
    ret = btc_close / btc_close.shift(hours * BARS_PER_HOUR) - 1
    return ret.reindex(index, method="ffill").to_numpy(float)


def apply_filter(sides: np.ndarray, btc_ret: np.ndarray, drop: float, both: bool) -> np.ndarray:
    """BTC 回看報酬 ≤ -drop% → 擋多單；both=True 時 ≥ +drop% 也擋空單"""
    out = sides.copy()
    r = np.nan_to_num(btc_ret, nan=0.0)
    out[(sides == 1) & (r <= -drop / 100)] = 0
    if both:
        out[(sides == -1) & (r >= drop / 100)] = 0
    return out


# ── 彙整 ────────────────────────────────────────────────────────────────────

def _load(dir_: str, name: str, syms: set[str] | None = None):
    from backtest.validation import read_trades_csv
    trades = []
    for f in sorted(glob.glob(f"{dir_}/crash-{name}-*.csv")):
        sym = Path(f).stem.split("-", 2)[2]
        if syms is None or sym in syms:
            trades += read_trades_csv(f)
    return trades


def _summary(trades) -> str:
    p = np.array([t.pnl_pct for t in trades])
    if not len(p):
        return "0 筆"
    return (f"{len(p)} 筆 | 勝率 {np.mean(p > 0)*100:.1f}% | 每筆 {p.mean():+.4f}% | "
            f"累計 {p.sum():+.1f}%")


def report(dir_: str) -> str:
    from backtest.validation import gate
    files = glob.glob(f"{dir_}/crash-base-*.csv")
    syms = sorted(Path(f).stem.split("-", 2)[2] for f in files)
    half_a, half_b = set(syms[0::2]), set(syms[1::2])
    L = ["# 大盤急跌暫停驗證（一年 walk-forward 樣本外，現行參數）", "",
         "事先定好的規則：只看 A 半選組合（過關項數多者，平手比 t 值）；B 半只確認被選中者。", "",
         f"A 半（開發）：{' '.join(sorted(half_a))}", f"B 半（確認）：{' '.join(sorted(half_b))}", ""]

    # ── A 半：12 組 + 不過濾 ──
    L += ["## A 半：各組合", "```"]
    base_a = _load(dir_, "base", half_a)
    L.append(f"  不過濾          {_summary(base_a)}")
    scored = []
    for v in VARIANTS:
        name = variant_name(v)
        tr = _load(dir_, name, half_a)
        g = gate(tr, n_trials=len(VARIANTS)) if tr else None
        passes = sum(ok for _, ok, _ in g.checks) if g else 0
        t = g.stats.get("t", 0.0) if g else 0.0
        scored.append((passes, t, name, v))
        L.append(f"  {name:<14}  {_summary(tr)} | 過關 {passes}/7 | t {t:+.2f}")
    L.append("```")
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best = scored[0]
    L += ["", f"→ A 半選出：**{best[2]}**（過關 {best[0]}/7，t {best[1]:+.2f}）", ""]

    # ── B 半：只驗選中者，與不過濾對照 ──
    base_b, pick_b = _load(dir_, "base", half_b), _load(dir_, best[2], half_b)
    L += ["## B 半（開發時沒用過的標的）：最終確認", "",
          "### 不加過濾", "```", gate(base_b, n_trials=1).render() if base_b else "  無資料",
          f"  {_summary(base_b)}", "```", "",
          f"### 加上 {best[2]}", "```", gate(pick_b, n_trials=1).render() if pick_b else "  無資料",
          f"  {_summary(pick_b)}", "```", ""]
    if base_b and pick_b:
        from backtest.validation import leverage_table
        L += ["### 加上過濾後的槓桿蒙地卡羅（B 半）", "```",
              leverage_table([t.pnl_pct for t in pick_b], 10, leverages=(3, 5, 10),
                             mae_pct=[t.mae_pct for t in pick_b]), "```"]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="大盤急跌暫停 — 彙整報告")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    md = report(args.dir)
    print(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
