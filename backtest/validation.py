"""策略上線關卡 + 槓桿蒙地卡羅 —— 用歷史資料一次驗證，不必拿實盤慢慢等

實盤一週才十幾筆，要累積到統計上有意義（數百筆）得等好幾個月。
改用 walk-forward 產生的「樣本外」交易（一年 × 多標的 → 上千筆），在這裡一次判定：

    gate()         上線關卡：7 項檢查全過才算「有 edge」
    monte_carlo()  給定倉位 % 與槓桿，重抽交易序列模擬帳戶 → 爆倉機率、回撤分佈

任何策略只要能輸出「每筆樣本外交易的 symbol / 期間 / 淨 pnl%」就能套用：

    python -m backtest.validation --trades reports/wf-trades.csv --n-trials 36 \\
        --leverage 10 --position-pct 10
"""

import argparse
import csv
import sys
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np


@dataclass
class Trade:
    symbol: str
    period: str        # 例如測試窗起始月 "2026-04"，用來看是否只靠少數月份
    pnl_pct: float     # 已扣手續費，以名目為 100%
    mae_pct: float | None = None   # 持倉期間最大逆向波動（≤0）；高槓桿強平判定用


@dataclass
class Criteria:
    min_trades: int = 500
    min_t: float = 2.0             # 會再依 n_trials 做多重比較校正（取較大者）
    alpha: float = 0.05
    slippage_pct: float = 0.05     # 成本壓力：每筆再扣 0.05%（滑價／手續費低估）
    min_positive_period_share: float = 0.55
    n_boot: int = 5000


@dataclass
class GateResult:
    passed: bool
    checks: list = field(default_factory=list)   # (name, ok, detail)
    stats: dict = field(default_factory=dict)

    def render(self) -> str:
        lines = ["  上線關卡（全部通過才可上真金）"]
        for name, ok, detail in self.checks:
            lines.append(f"    {'✅' if ok else '❌'} {name:<14} {detail}")
        lines.append(f"  → {'通過' if self.passed else '未通過'}")
        return "\n".join(lines)


def required_t(n_trials: int, alpha: float = 0.05, floor: float = 2.0) -> float:
    """多重比較校正後的 t 門檻（Bonferroni，單尾）。

    從 36 組參數挑冠軍，本來就會有組合「看起來」顯著；
    試越多組，門檻越高。n_trials=1 → 2.0，36 → ~2.99，7680 → ~4.2。
    """
    z = NormalDist().inv_cdf(1 - alpha / max(1, n_trials))
    return max(floor, z)


def _t_stat(p: np.ndarray) -> float:
    if len(p) < 2:
        return 0.0
    se = p.std(ddof=1) / np.sqrt(len(p))
    return float(p.mean() / se) if se > 0 else 0.0


def gate(trades: list[Trade], n_trials: int = 1, c: Criteria | None = None,
         seed: int = 0) -> GateResult:
    c = c or Criteria()
    p = np.array([t.pnl_pct for t in trades], dtype=float)
    checks = []
    n = len(p)

    checks.append(("樣本數", n >= c.min_trades, f"{n} 筆（需 ≥ {c.min_trades}）"))
    if n < 2:
        return GateResult(False, checks, {"n": n})

    mean = float(p.mean())
    t = _t_stat(p)
    t_need = required_t(n_trials, c.alpha, c.min_t)
    checks.append(("顯著性", t >= t_need,
                   f"t = {t:.2f}（需 ≥ {t_need:.2f}，已校正 {n_trials} 組參數試驗）"))

    rng = np.random.default_rng(seed)
    boot = p[rng.integers(0, n, size=(c.n_boot, n))].mean(axis=1)
    lo, hi = np.percentile(boot, [2.5, 97.5])
    checks.append(("信賴區間", lo > 0, f"每筆期望 95% CI [{lo:+.4f}%, {hi:+.4f}%]（下限需 > 0）"))

    stressed = mean - c.slippage_pct
    checks.append(("成本壓力", stressed > 0,
                   f"每筆再扣 {c.slippage_pct}% 後期望 {stressed:+.4f}%（需 > 0）"))

    by_period: dict[str, float] = {}
    for tr in trades:
        by_period[tr.period] = by_period.get(tr.period, 0.0) + tr.pnl_pct
    pos_share = np.mean([v > 0 for v in by_period.values()]) if by_period else 0.0
    checks.append(("期間穩定", pos_share >= c.min_positive_period_share,
                   f"{pos_share*100:.0f}% 的期間獲利（{len(by_period)} 期，需 ≥ "
                   f"{c.min_positive_period_share*100:.0f}%）"))

    by_sym: dict[str, float] = {}
    for tr in trades:
        by_sym[tr.symbol] = by_sym.get(tr.symbol, 0.0) + tr.pnl_pct
    best = max(by_sym, key=by_sym.get)
    rest = sum(v for k, v in by_sym.items() if k != best)
    checks.append(("不靠單一標的", rest > 0,
                   f"拿掉最賺的 {best} 後其餘合計 {rest:+.1f}%（需 > 0）"))

    # 依期間排序後切兩半（輸入常是「逐標的」排列，直接切會變成比標的而非比時間）
    chrono = np.array([t.pnl_pct for t in sorted(trades, key=lambda x: x.period)])
    half = n // 2
    first, second = float(chrono[:half].sum()), float(chrono[half:].sum())
    checks.append(("前後半一致", first > 0 and second > 0,
                   f"前半 {first:+.1f}% / 後半 {second:+.1f}%（兩段都需 > 0）"))

    stats = {"n": n, "mean": mean, "t": t, "t_need": t_need, "ci": (float(lo), float(hi)),
             "win_rate": float(np.mean(p > 0))}
    return GateResult(all(ok for _, ok, _ in checks), checks, stats)


@dataclass
class MCResult:
    leverage: float
    position_pct: float
    n_trades: int
    ruin_prob: float          # 期間內任一時點權益 ≤ ruin_level 的機率
    bust_prob: float          # 權益 ≤ 5%（實質歸零）
    median_final: float       # 期末權益中位數（起始 = 1.0）
    p05_final: float
    p95_final: float
    median_max_dd: float      # 最大回撤中位數（0~1）
    liquidated_share: float = 0.0   # 因中途觸及強平線而整筆保證金歸零的交易比例
    unreliable: bool = False        # 沒有 MAE 且強平距離比停損近 → 結果高估，不可採信

    def render(self, start_balance: float = 1.0) -> str:
        s = start_balance
        if self.unreliable:
            return (f"    {self.leverage:>5.0f}x × 倉位 {self.position_pct:>4.0f}%  "
                    f"⚠ 強平線（{liquidation_distance(self.leverage):.1f}%）比停損近、又沒有 MAE 資料 → 無法模擬")
        return (f"    {self.leverage:>5.0f}x × 倉位 {self.position_pct:>4.0f}%  "
                f"爆倉(-50%) {self.ruin_prob*100:5.1f}%  歸零 {self.bust_prob*100:5.1f}%  "
                f"期末中位 {self.median_final*s:8.1f}  [5%: {self.p05_final*s:7.1f}, "
                f"95%: {self.p95_final*s:8.1f}]  回撤中位 {self.median_max_dd*100:4.1f}%  "
                f"中途強平 {self.liquidated_share*100:4.1f}%")


def liquidation_distance(leverage: float, maint_pct: float = 0.5) -> float:
    """逐倉強平距離（%）≈ 100/槓桿 − 維持保證金率；10x ≈ 9.5%、27x ≈ 3.2%"""
    return 100.0 / leverage - maint_pct


def monte_carlo(pnls_pct, leverage: float, position_pct: float, n_trades: int = 300,
                n_paths: int = 5000, block: int = 5, ruin_level: float = 0.5,
                seed: int = 0, mae_pct=None) -> MCResult:
    """重抽歷史交易序列模擬帳戶權益（複利、逐筆）。

    每筆對權益的影響 = 保證金占比 × 槓桿 × pnl%；逐倉下單筆最多賠掉該筆保證金
    （強平即止），故下限為 -保證金占比。以 block bootstrap 保留連敗的群聚性。
    強平：有 MAE 時，持倉中途逆向波動 ≥ 強平距離的交易一律視為保證金歸零 ——
    即使它最後會反彈成獲利（高槓桿最致命的就是這種「先洗再拉」）。
    簡化：交易依序發生、不重疊（實盤可同時多倉，真實風險只會更高）。
    """
    p = np.asarray(pnls_pct, dtype=float) / 100.0
    if len(p) == 0:
        raise ValueError("沒有交易可模擬")
    frac = position_pct / 100.0
    liq = liquidation_distance(leverage)
    if mae_pct is not None and all(m is not None for m in mae_pct):
        liquidated = -np.asarray(mae_pct, dtype=float) >= liq
        unreliable = False
    else:
        liquidated = np.zeros(len(p), dtype=bool)
        # 沒有 MAE 時，只要強平線比最大單筆虧損還近，就一定低估了強平
        unreliable = bool(len(p) and liq <= -p.min() * 100)
    per_trade = np.where(liquidated, -frac, np.maximum(frac * leverage * p, -frac))
    rng = np.random.default_rng(seed)
    n_blocks = -(-n_trades // block)
    starts = rng.integers(0, max(1, len(p) - block + 1), size=(n_paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_paths, -1)[:, :n_trades]
    idx = np.minimum(idx, len(p) - 1)
    rets = per_trade[idx]
    equity = np.cumprod(1.0 + rets, axis=1)
    equity = np.maximum(equity, 0.0)
    peak = np.maximum.accumulate(np.concatenate([np.ones((n_paths, 1)), equity], axis=1), axis=1)[:, 1:]
    dd = 1.0 - equity / peak
    final = equity[:, -1]
    return MCResult(
        leverage=leverage, position_pct=position_pct, n_trades=n_trades,
        ruin_prob=float(np.mean(equity.min(axis=1) <= ruin_level)),
        bust_prob=float(np.mean(equity.min(axis=1) <= 0.05)),
        median_final=float(np.median(final)),
        p05_final=float(np.percentile(final, 5)),
        p95_final=float(np.percentile(final, 95)),
        median_max_dd=float(np.median(dd.max(axis=1))),
        liquidated_share=float(liquidated.mean()),
        unreliable=unreliable,
    )


def leverage_table(pnls_pct, position_pct: float, leverages=(3, 5, 10, 20, 27),
                   n_trades: int = 300, start_balance: float = 500.0, mae_pct=None) -> str:
    lines = [f"  槓桿蒙地卡羅（{n_trades} 筆 ≈ 實盤數個月，起始 {start_balance:.0f} U，"
             f"倉位 = 每筆保證金占帳戶 %）"]
    for lev in leverages:
        lines.append(monte_carlo(pnls_pct, lev, position_pct, n_trades,
                                 mae_pct=mae_pct).render(start_balance))
    return "\n".join(lines)


def write_trades_csv(path: str, trades: list[Trade]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "period", "pnl_pct", "mae_pct"])
        for t in trades:
            w.writerow([t.symbol, t.period, f"{t.pnl_pct:.4f}",
                        "" if t.mae_pct is None else f"{t.mae_pct:.4f}"])


def read_trades_csv(path: str) -> list[Trade]:
    with open(path, encoding="utf-8") as f:
        return [Trade(r["symbol"], r["period"], float(r["pnl_pct"]),
                      float(r["mae_pct"]) if r.get("mae_pct") else None)
                for r in csv.DictReader(f)]


def main():
    ap = argparse.ArgumentParser(description="策略上線關卡 + 槓桿蒙地卡羅")
    ap.add_argument("--trades", required=True, help="CSV：symbol,period,pnl_pct（樣本外、已扣費）")
    ap.add_argument("--n-trials", type=int, default=1, help="選參時試過幾組（多重比較校正）")
    ap.add_argument("--position-pct", type=float, default=10.0)
    ap.add_argument("--leverage", type=float, default=10.0)
    ap.add_argument("--horizon", type=int, default=300, help="蒙地卡羅模擬筆數")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    trades = read_trades_csv(args.trades)
    print(gate(trades, args.n_trials).render())
    print()
    levs = sorted({3, 5, 10, 20, 27, int(args.leverage)})
    maes = [t.mae_pct for t in trades]
    print(leverage_table([t.pnl_pct for t in trades], args.position_pct, levs, args.horizon,
                         mae_pct=maes if all(m is not None for m in maes) else None))


if __name__ == "__main__":
    main()
