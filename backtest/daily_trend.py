"""日線長期順勢 —— 一年只交易十幾次、抓數個月的大行情（與先前 1h 級別的順勢不同假設）

事先定好的流程（跑之前寫死，不看結果再改）：
    規則 12 組（只用收盤價，第 t 天收盤決定、第 t+1 天起生效）：
      donchian  收盤創 N 日新高做多（ls 模式：創 N 日新低做空）；收盤跌破 N/2 日低點出場
                N ∈ {20, 55, 100} × 模式 {long 只做多, ls 多空}
      sma       收盤 > N 日均線持多；ls 模式下 < 均線持空、long 模式下空手
                N ∈ {50, 100, 200} × 模式 {long, ls}
    成本：倉位變動扣 0.04%（單邊 taker）；持倉期間多單付、空單收資金費（官方歷史費率）
    開發：BTC、ETH 等權組合 → 依上線關卡選組（過關項數多者，平手比 t 值）
    確認：8 個 2019~2020 就上市的老牌幣（開發時不碰），只驗被選中的那一組（n_trials=1）
    評估單位：組合月報酬（樣本門檻 60 個月；期間穩定看「年」）；並與買進持有比較

    python -m backtest.daily_trend --out reports/daily-trend.md
"""

import argparse
import itertools
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.validation import Criteria, Trade, gate, leverage_table

FEE = 0.04 / 100
DEV = ["BTCUSDT", "ETHUSDT"]
HOLDOUT = ["BNBUSDT", "XRPUSDT", "ADAUSDT", "LTCUSDT", "LINKUSDT", "DOGEUSDT", "BCHUSDT", "ETCUSDT"]
RULES = ([{"kind": "donchian", "n": n, "mode": m} for n, m in itertools.product((20, 55, 100), ("long", "ls"))]
         + [{"kind": "sma", "n": n, "mode": m} for n, m in itertools.product((50, 100, 200), ("long", "ls"))])
MONTHLY_CRITERIA = Criteria(min_trades=60)


def rule_name(r: dict) -> str:
    return f"{r['kind']}{r['n']}_{r['mode']}"


def positions(close: pd.Series, rule: dict) -> np.ndarray:
    """第 t 天收盤後的持倉（+1 / 0 / -1），只用 ≤ t 的收盤價"""
    c = close.to_numpy(float)
    n = rule["n"]
    pos = np.zeros(len(c))
    if rule["kind"] == "sma":
        sma = close.rolling(n, min_periods=n).mean().to_numpy(float)
        valid = np.isfinite(sma)
        pos[valid & (c > sma)] = 1
        if rule["mode"] == "ls":
            pos[valid & (c < sma)] = -1
        return pos
    hi = close.rolling(n, min_periods=n).max().shift(1).to_numpy(float)
    lo = close.rolling(n, min_periods=n).min().shift(1).to_numpy(float)
    m = max(2, n // 2)
    ex_lo = close.rolling(m, min_periods=m).min().shift(1).to_numpy(float)
    ex_hi = close.rolling(m, min_periods=m).max().shift(1).to_numpy(float)
    cur = 0
    for t in range(len(c)):
        if not np.isfinite(c[t]):
            cur = 0
        elif cur == 1 and np.isfinite(ex_lo[t]) and c[t] < ex_lo[t]:
            cur = 0
        elif cur == -1 and np.isfinite(ex_hi[t]) and c[t] > ex_hi[t]:
            cur = 0
        if cur == 0 and np.isfinite(hi[t]) and c[t] > hi[t]:
            cur = 1
        elif cur == 0 and rule["mode"] == "ls" and np.isfinite(lo[t]) and c[t] < lo[t]:
            cur = -1
        pos[t] = cur
    return pos


def daily_returns(close: pd.Series, funding: pd.Series, rule: dict) -> pd.Series:
    """策略日報酬：昨天收盤的持倉 × 今天漲跌 − 換倉手續費 − 持倉資金費"""
    pos = positions(close, rule)
    prev = np.concatenate([[0.0], pos[:-1]])
    ret = close.pct_change().fillna(0.0).to_numpy(float)
    fund = funding.reindex(close.index).fillna(0.0).to_numpy(float)
    turn = np.abs(pos - prev)
    r = prev * ret - turn * FEE - prev * fund
    return pd.Series(r, index=close.index)


@dataclass
class Result:
    monthly: pd.Series           # 組合月報酬（小數）
    contrib: dict                # 各幣累計貢獻（%）
    daily: pd.Series             # 組合日報酬


def portfolio(closes: pd.DataFrame, funding: pd.DataFrame, rule: dict | None) -> Result:
    """等權組合；rule=None → 買進持有（同樣扣持倉資金費，對照組）"""
    cols, rets = [], []
    for s in closes.columns:
        c = closes[s].dropna()
        if len(c) < 30:
            continue
        f = funding[s] if s in funding else pd.Series(0.0, index=c.index)
        if rule is None:
            r = c.pct_change().fillna(0.0) - f.reindex(c.index).fillna(0.0)
        else:
            r = daily_returns(c, f, rule)
        cols.append(s)
        rets.append(r)
    panel = pd.concat(rets, axis=1, keys=cols).sort_index()
    daily = panel.mean(axis=1, skipna=True).fillna(0.0)        # 當天有資料的幣等權
    monthly = (1 + daily).groupby(daily.index.to_period("M")).prod() - 1
    w = panel.notna().div(panel.notna().sum(axis=1), axis=0)
    contrib = (panel.fillna(0.0) * w).sum() * 100
    return Result(monthly, {k: float(v) for k, v in contrib.items()}, daily)


def _stats(res: Result) -> str:
    m = res.monthly.to_numpy()
    eq = np.cumprod(1 + res.daily.to_numpy())
    dd = 1 - eq / np.maximum.accumulate(eq)
    years = len(res.daily) / 365
    cagr = eq[-1] ** (1 / years) - 1 if years > 0 and eq[-1] > 0 else -1
    sharpe = m.mean() / m.std(ddof=1) * np.sqrt(12) if m.std(ddof=1) > 0 else 0
    return (f"{len(m)} 個月 | 月勝率 {np.mean(m > 0)*100:.0f}% | 年化 {cagr*100:+.1f}% | "
            f"最大回撤 {dd.max()*100:.1f}% | 夏普 {sharpe:.2f} | 累計 {(eq[-1]-1)*100:+.0f}%")


def _trades(res: Result) -> list[Trade]:
    return [Trade("PORT", str(p.year), float(r) * 100) for p, r in res.monthly.items()]


def report(closes: pd.DataFrame, funding: pd.DataFrame) -> str:
    dev_c, dev_f = closes[[s for s in DEV if s in closes]], funding
    ho = [s for s in HOLDOUT if s in closes]
    L = ["# 日線長期順勢驗證", "",
         "事先定好的規則：只看開發組（BTC、ETH）選規則（過關項數多者，平手比 t 值）；"
         "確認組只驗被選中者。評估單位＝組合月報酬；期間穩定看年。", "",
         f"資料：{closes.index[0]:%Y-%m-%d} ~ {closes.index[-1]:%Y-%m-%d}", "",
         "## 開發組（BTC、ETH）", "```",
         f"  買進持有        {_stats(portfolio(dev_c, dev_f, None))}"]
    scored = []
    for r in RULES:
        res = portfolio(dev_c, dev_f, r)
        g = gate(_trades(res), len(RULES), MONTHLY_CRITERIA, symbol_contrib=res.contrib)
        passes = sum(ok for _, ok, _ in g.checks)
        scored.append((passes, g.stats.get("t", 0.0), rule_name(r), r))
        L.append(f"  {rule_name(r):<15} {_stats(res)} | 過關 {passes}/7 | t {g.stats.get('t', 0):+.2f}")
    L.append("```")
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best = scored[0]
    L += ["", f"→ 開發組選出：**{best[2]}**（過關 {best[0]}/7，t {best[1]:+.2f}）", ""]

    ho_c = closes[ho]
    bh, pick = portfolio(ho_c, funding, None), portfolio(ho_c, funding, best[3])
    g = gate(_trades(pick), 1, MONTHLY_CRITERIA, symbol_contrib=pick.contrib)
    L += [f"## 確認組（開發時沒用過）：{' '.join(ho)}", "```",
          f"  買進持有        {_stats(bh)}",
          f"  {best[2]:<15} {_stats(pick)}", "", g.render(), "```", "",
          "### 槓桿蒙地卡羅（確認組月報酬，24 個月，起始 500 U，全額倉位）", "```",
          leverage_table(list(pick.monthly.to_numpy() * 100), 100, leverages=(1, 2, 3), n_trades=24),
          "```"]
    return "\n".join(L)


CHOSEN = {"kind": "sma", "n": 50, "mode": "long"}     # 2026-10-21 開發組選出、確認組 t=1.83 未過
NEIGHBORS = [30, 40, 45, 50, 55, 60, 70]


def robustness_report(closes_2020: pd.DataFrame, funding_2020: pd.DataFrame,
                      spot_2017: pd.DataFrame) -> str:
    """不再選參數，只檢查已選中的 sma50_long 是否穩健：
    ① 時間留出：2017-08 ~ 2019-12 現貨（開發時完全沒用過，含 2018 年 -80% 熊市），規則不改
    ② 參數敏感度：均線 30~70 天，若只有 50 天才好 → 巧合"""
    L = ["# 日線 50 均線策略：穩健度檢查", "",
         "不重新選參數；檢查 2026-10-21 選出的 sma50_long（只做多、1 倍）。", ""]
    zero = pd.DataFrame(0.0, index=spot_2017.index, columns=spot_2017.columns)
    bh, pick = portfolio(spot_2017, zero, None), portfolio(spot_2017, zero, CHOSEN)
    g = gate(_trades(pick), 1, Criteria(min_trades=24), symbol_contrib=pick.contrib)
    L += ["## ① 時間留出：2017-08 ~ 2019-12（現貨，開發時沒用過）",
          f"標的：{' '.join(spot_2017.columns)}（各幣上市日不同，當天有資料者等權）", "```",
          f"  買進持有        {_stats(bh)}",
          f"  sma50_long      {_stats(pick)}", "",
          "  逐年（組合報酬）"]
    for y in sorted({d.year for d in pick.daily.index}):
        sel = pick.daily.index.year == y
        a = (1 + pick.daily[sel]).prod() - 1
        b = (1 + bh.daily[bh.daily.index.year == y]).prod() - 1
        L.append(f"    {y}  策略 {a*100:+7.1f}%   買進持有 {b*100:+7.1f}%")
    L += ["", g.render(), "```", ""]

    L += ["## ② 參數敏感度：均線天數 30~70（2020~2026 合約，全部 10 個幣）", "```"]
    bh20 = portfolio(closes_2020, funding_2020, None)
    L.append(f"  買進持有        {_stats(bh20)}")
    for n in NEIGHBORS:
        r = portfolio(closes_2020, funding_2020, {"kind": "sma", "n": n, "mode": "long"})
        L.append(f"  {f'sma{n}_long':<15} {_stats(r)}")
    L.append("```")
    return "\n".join(L)


TOPN_CRITERIA = Criteria(min_trades=36)


def topn_universe(closes: pd.DataFrame, qvol: pd.DataFrame, top_n: int,
                  window: int = 30, min_age: int = 90) -> pd.DataFrame:
    """第 t 天的成交量前 N 名（point-in-time）：上市滿 min_age 天者中，過去 window 天平均成交額排名"""
    age = closes.notna().cumsum()
    avg = qvol.rolling(window, min_periods=window).mean().where(age >= min_age)
    return avg.rank(axis=1, ascending=False, method="first") <= top_n


@dataclass
class TopNResult(Result):
    entries_per_week: float
    avg_held: float
    max_held: int


def topn_sma(closes: pd.DataFrame, funding: pd.DataFrame, uni: pd.DataFrame,
             top_n: int, sma_days: int = 50) -> TopNResult:
    """與實盤一致：每幣固定 1/top_n 資金；收盤站上均線且當天在前 N 名 → 買進；
    已持有者不論是否掉出前 N 名，收盤跌破均線才賣（掉出名單不強制賣）"""
    c = closes.to_numpy(float)
    sma = closes.rolling(sma_days, min_periods=sma_days).mean().to_numpy(float)
    u = uni.reindex_like(closes).fillna(False).to_numpy(bool)
    T, N = c.shape
    held = np.zeros((T, N))
    cur = np.zeros(N)
    entries = 0
    for t in range(T):
        ok = np.isfinite(c[t]) & np.isfinite(sma[t])
        above = ok & (c[t] > sma[t])
        cur = np.where(~np.isfinite(c[t]) | (ok & ~above), 0.0, cur)          # 跌破均線（或下市）→ 賣
        buy = (cur == 0) & above & u[t]
        entries += int(buy.sum())
        cur = np.where(buy, 1.0, cur)
        held[t] = cur
    prev = np.vstack([np.zeros(N), held[:-1]])
    ret = np.nan_to_num(closes.pct_change().to_numpy(float), nan=0.0, posinf=0.0, neginf=0.0)
    fund = np.nan_to_num(funding.reindex_like(closes).to_numpy(float), nan=0.0)
    w = 1.0 / top_n
    per = w * (prev * ret - np.abs(held - prev) * FEE - prev * fund)
    daily = pd.Series(per.sum(axis=1), index=closes.index)
    monthly = (1 + daily).groupby(daily.index.to_period("M")).prod() - 1
    contrib = {s: float(v) * 100 for s, v in zip(closes.columns, per.sum(axis=0))}
    weeks = max(1.0, T / 7)
    return TopNResult(monthly, contrib, daily, entries / weeks,
                      float(held.sum(axis=1).mean()), int(held.sum(axis=1).max()))


def topn_buy_hold(closes: pd.DataFrame, funding: pd.DataFrame, uni: pd.DataFrame, top_n: int) -> Result:
    """對照組：每天等權持有當天的前 N 名（名單變動時換倉，扣手續費與資金費）"""
    u = uni.reindex_like(closes).fillna(False).to_numpy(float)
    prev = np.vstack([np.zeros(u.shape[1]), u[:-1]])
    ret = np.nan_to_num(closes.pct_change().to_numpy(float), nan=0.0, posinf=0.0, neginf=0.0)
    fund = np.nan_to_num(funding.reindex_like(closes).to_numpy(float), nan=0.0)
    per = (prev * ret - np.abs(u - prev) * FEE - prev * fund) / top_n
    daily = pd.Series(per.sum(axis=1), index=closes.index)
    monthly = (1 + daily).groupby(daily.index.to_period("M")).prod() - 1
    return Result(monthly, {s: float(v) * 100 for s, v in zip(closes.columns, per.sum(axis=0))}, daily)


def topn_report(closes: pd.DataFrame, funding: pd.DataFrame, qvol: pd.DataFrame, top_n: int) -> str:
    """不選參數：sma50_long 原封不動，只把標的從 BTC/ETH 換成每天的成交量前 N 名"""
    uni = topn_universe(closes, qvol, top_n)
    start = uni.any(axis=1).idxmax()                       # 第一天有完整排名
    closes, funding, uni = closes.loc[start:], funding.loc[start:], uni.loc[start:]
    pick, bh = topn_sma(closes, funding, uni, top_n), topn_buy_hold(closes, funding, uni, top_n)
    dev = [s for s in DEV if s in closes]
    base = portfolio(closes[dev], funding, CHOSEN)
    g = gate(_trades(pick), 1, TOPN_CRITERIA, symbol_contrib=pick.contrib)
    top_contrib = sorted(pick.contrib.items(), key=lambda kv: kv[1], reverse=True)
    L = [f"# 日線 50 均線 × 成交量前 {top_n} 名（point-in-time）", "",
         f"規則不變（sma50_long、只做多、1 倍）；每天只用當時過去 30 天成交額選前 {top_n} 名（上市滿 90 天），"
         f"每幣固定 1/{top_n} 資金；掉出名單的持倉照樣等跌破均線才賣（與實盤一致）。", "",
         f"資料：{closes.index[0]:%Y-%m-%d} ~ {closes.index[-1]:%Y-%m-%d}；候選池 {closes.shape[1]} 個幣"
         "（當前成交量前 100 名 → 已下市的幣不在池內，結果偏樂觀）", "", "```",
         f"  前{top_n}名 買進持有   {_stats(bh)}",
         f"  前{top_n}名 sma50      {_stats(pick)}",
         f"  BTC/ETH sma50     {_stats(base)}（同期間對照）", "",
         f"  交易頻率：平均每週買進 {pick.entries_per_week:.1f} 次；平均同時持有 {pick.avg_held:.1f} 個幣，"
         f"最多 {pick.max_held} 個（每個 1/{top_n} 資金 → 最多需 {pick.max_held / top_n * 100:.0f}% 資金）", "",
         "  逐年（組合報酬）"]
    for y in sorted({d.year for d in pick.daily.index}):
        a = (1 + pick.daily[pick.daily.index.year == y]).prod() - 1
        b = (1 + bh.daily[bh.daily.index.year == y]).prod() - 1
        c = (1 + base.daily[base.daily.index.year == y]).prod() - 1
        L.append(f"    {y}  前{top_n} sma50 {a*100:+7.1f}%   前{top_n} 買進持有 {b*100:+7.1f}%   BTC/ETH sma50 {c*100:+7.1f}%")
    L += ["", "  貢獻最多／最少的幣：" + "、".join(f"{s} {v:+.0f}%" for s, v in top_contrib[:5])
          + " ｜ " + "、".join(f"{s} {v:+.0f}%" for s, v in top_contrib[-5:]), "", g.render(), "```"]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="日線長期順勢驗證")
    ap.add_argument("--months", type=int, default=86)
    ap.add_argument("--out", default=None)
    ap.add_argument("--robust", action="store_true", help="穩健度檢查（時間留出 + 參數敏感度），不選參數")
    ap.add_argument("--top-symbols", default=None,
                    help="逗號分隔的候選池 → 驗證「每天成交量前 N 名」版本（不選參數）")
    ap.add_argument("--top-n", type=int, default=10)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    from backtest.vision_data import daily_panel, spot_daily_closes
    if args.top_symbols:
        syms = sorted({x.strip().upper() for x in args.top_symbols.split(",") if x.strip()} | set(DEV))
        closes, funding, qvol = daily_panel(syms, args.months)
        closes = closes.dropna(axis=1, how="all")
        md = topn_report(closes, funding.reindex(columns=closes.columns),
                         qvol.reindex(columns=closes.columns), args.top_n)
        print(md)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(md, encoding="utf-8")
        return
    closes, funding, _ = daily_panel(DEV + HOLDOUT, args.months)
    if args.robust:
        spot = spot_daily_closes(DEV + HOLDOUT, (2017, 8), (2019, 12))
        md = robustness_report(closes, funding, spot)
    else:
        md = report(closes, funding)
    print(md)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")


if __name__ == "__main__":
    main()
