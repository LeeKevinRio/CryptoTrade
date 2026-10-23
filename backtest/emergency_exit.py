"""日線 50 均線 + 盤中緊急出場 —— 驗證「不等收盤、盤中暴跌就先賣」有沒有幫助

事先定好的流程（跑之前寫死，不看結果再改）：
    基準：sma50_long（收盤 > 50 日均線持多，收盤判斷，隔天生效）
    6 種緊急出場（持多時，盤中最低價觸價就賣；開盤已跳空越過 → 以更差的開盤價成交）：
      below_sma  盤中跌到「前一天的均線 × (1 − x)」，x ∈ {5, 10, 15}%
      day_drop   盤中比前一天收盤跌 y，y ∈ {10, 15, 20}%
    出場後空手；當天收盤若仍站上均線，照原規則收盤買回（等同機器人隔天早上買回）
    判定：開發組（BTC、ETH，2020~2026）在「最大回撤不大於基準」的版本中選夏普最高者；
          被選中的版本必須在確認組（8 個老牌幣 2020~2026）與時間留出（10 幣 2017~2019 現貨）
          兩組都「夏普 ≥ 基準且最大回撤 ≤ 基準」才採用；否則維持不加緊急出場

    python -m backtest.emergency_exit --out reports/emergency-exit.md
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.daily_trend import DEV, FEE, HOLDOUT, Result, _stats

SMA = 50
VARIANTS = ([{"kind": "below_sma", "x": x} for x in (0.05, 0.10, 0.15)]
            + [{"kind": "day_drop", "x": x} for x in (0.10, 0.15, 0.20)])


def variant_name(v: dict | None) -> str:
    return "不加緊急出場" if v is None else f"{v['kind']}_{int(v['x'] * 100)}"


def simulate(o: pd.Series, lo: pd.Series, c: pd.Series, f: pd.Series, v: dict | None) -> pd.Series:
    """單一標的日報酬（小數）。第 t 天：先處理盤中緊急出場，再於收盤依均線決定隔夜持倉。"""
    idx = c.dropna().index
    o, lo, c = o.reindex(idx).to_numpy(float), lo.reindex(idx).to_numpy(float), c.reindex(idx).to_numpy(float)
    fund = f.reindex(idx).fillna(0.0).to_numpy(float)
    sma = pd.Series(c).rolling(SMA, min_periods=SMA).mean().to_numpy(float)
    r = np.zeros(len(c))
    pos = 0
    for t in range(1, len(c)):
        day = 0.0
        if pos == 1:
            trig = None
            if v is not None:
                if v["kind"] == "below_sma" and np.isfinite(sma[t - 1]):
                    trig = sma[t - 1] * (1 - v["x"])
                elif v["kind"] == "day_drop":
                    trig = c[t - 1] * (1 - v["x"])
            if trig is not None and np.isfinite(lo[t]) and lo[t] <= trig:
                px = min(trig, o[t]) if np.isfinite(o[t]) else trig
                day = px / c[t - 1] - 1 - FEE - fund[t]
                pos = 0
            else:
                day = c[t] / c[t - 1] - 1 - fund[t]
        want = 1 if np.isfinite(sma[t]) and c[t] > sma[t] else 0
        if want != pos:
            day -= FEE
            pos = want
        r[t] = day
    return pd.Series(r, index=idx)


def portfolio(o: pd.DataFrame, lo: pd.DataFrame, c: pd.DataFrame, f: pd.DataFrame | None,
              v: dict | None) -> Result:
    rets = {}
    for s in c.columns:
        if c[s].dropna().shape[0] < SMA + 10:
            continue
        fs = f[s] if f is not None and s in f else pd.Series(0.0, index=c.index)
        rets[s] = simulate(o[s], lo[s], c[s], fs, v)
    panel = pd.DataFrame(rets).sort_index()
    daily = panel.mean(axis=1, skipna=True).fillna(0.0)
    monthly = (1 + daily).groupby(daily.index.to_period("M")).prod() - 1
    w = panel.notna().div(panel.notna().sum(axis=1), axis=0)
    contrib = (panel.fillna(0.0) * w).sum() * 100
    return Result(monthly, {k: float(x) for k, x in contrib.items()}, daily)


def metrics(res: Result) -> tuple[float, float]:
    """(夏普, 最大回撤)"""
    m = res.monthly.to_numpy()
    sharpe = m.mean() / m.std(ddof=1) * np.sqrt(12) if len(m) > 1 and m.std(ddof=1) > 0 else 0.0
    eq = np.cumprod(1 + res.daily.to_numpy())
    return float(sharpe), float((1 - eq / np.maximum.accumulate(eq)).max())


def evaluate(datasets: dict[str, tuple]) -> str:
    """datasets：{名稱: (open, low, close, funding)}，第一組為開發組"""
    names = list(datasets)
    table = {n: {variant_name(v): portfolio(*datasets[n], v) for v in [None] + VARIANTS} for n in names}
    L = ["# 緊急出場驗證（日線 50 均線只做多 + 盤中暴跌先賣）", "",
         "事先定好的判定：開發組在「回撤不大於基準」的版本中選夏普最高者；"
         "被選中者須在另外兩組都「夏普 ≥ 基準且回撤 ≤ 基準」才採用。", ""]
    for n in names:
        L += [f"## {n}", "```"]
        for k, res in table[n].items():
            L.append(f"  {k:<14} {_stats(res)}")
        L += ["```", ""]

    dev = names[0]
    base_sh, base_dd = metrics(table[dev]["不加緊急出場"])
    cands = [(metrics(table[dev][variant_name(v)]), variant_name(v)) for v in VARIANTS]
    ok = [(sh, name) for (sh, dd), name in cands if dd <= base_dd + 1e-9 and sh > base_sh]
    if not ok:
        L.append("→ 開發組沒有任何緊急出場版本在不增加回撤的情況下提高夏普 → **不採用緊急出場**")
        return "\n".join(L)
    pick = max(ok)[1]
    L += [f"→ 開發組選出：**{pick}**", "", "## 確認", "```"]
    passed = True
    for n in names[1:]:
        b_sh, b_dd = metrics(table[n]["不加緊急出場"])
        p_sh, p_dd = metrics(table[n][pick])
        good = p_sh >= b_sh and p_dd <= b_dd + 1e-9
        passed &= good
        L.append(f"  {n:<24} 夏普 {b_sh:.2f} → {p_sh:.2f} | 回撤 {b_dd*100:.1f}% → {p_dd*100:.1f}% | "
                 f"{'✅' if good else '❌'}")
    L += ["```", "", f"→ {'**採用** ' + pick if passed else '**不採用**（確認組沒有一致改善）'}"]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="緊急出場驗證")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    from backtest.vision_data import daily_panel, month_list, month_range, ohlc_panels
    months = month_list(86)
    _, fund, _ = daily_panel(DEV + HOLDOUT, 86)
    o, lo, c = ohlc_panels(DEV + HOLDOUT, months)
    so, slo, sc = ohlc_panels(DEV + HOLDOUT, month_range((2017, 8), (2019, 12)), spot=True)
    dev = [s for s in DEV if s in c]
    ho = [s for s in HOLDOUT if s in c]
    datasets = {
        "開發組 BTC、ETH（2020~2026）": (o[dev], lo[dev], c[dev], fund),
        "確認組 8 個老牌幣（2020~2026）": (o[ho], lo[ho], c[ho], fund),
        "時間留出 10 幣（2017~2019 現貨）": (so, slo, sc, None),
    }
    md = evaluate(datasets)
    print(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
