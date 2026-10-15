"""順勢候選策略（高風險高報酬型）—— 小停損、不設固定停利、用追蹤停損讓獲利單跑

與現行抄底策略相反的結構：勝率預期 30~45%，靠少數大波段賺錢（賺賠比 > 2）。
三個經典家族，參數網格刻意小（每族 ≤ 12 組），避免「從幾千組挑冠軍」的過擬合：

    donchian   突破過去 N 根 1h 高點做多 / 跌破低點做空
    ema_cross  收盤穿越 EMA(N) 且 EMA 斜率同向
    squeeze    布林帶寬縮到近期低檔後，收盤突破上/下軌

出場（三族共用）：
    初始停損 = 進場價 ∓ stop_atr × ATR(14)
    吊燈追蹤 = 持倉期間最高價 − trail_atr × ATR（只往有利方向移動）
    同一根同時觸及 → 先算停損；跳空越過停損 → 以開盤價出場（更差）

驗證：1h K 線 × 2 年 × 多標的，walk-forward（訓練 180 天選參 → 測試 30 天），
樣本外交易交給 backtest.validation 的上線關卡與槓桿蒙地卡羅。

    python -m backtest.candidates --symbol BTCUSDT --days 730 --out out/
"""

import argparse
import asyncio
import itertools
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.validation import Trade, write_trades_csv
from src.indicators.atr import calculate_atr

FEE = 0.08   # % 名目，來回（taker 進出 0.04% × 2）

_BASE = {
    "donchian": {"n": [24, 72, 168], "stop_atr": [1.5, 2.5], "trail_atr": [3.0, 5.0]},
    "ema_cross": {"n": [50, 100, 200], "stop_atr": [1.5, 2.5], "trail_atr": [3.0, 5.0]},
    "squeeze": {"pct": [0.1, 0.2], "stop_atr": [1.5, 2.5], "trail_atr": [3.0, 5.0]},
}
# 第二輪（2026-10-15 首輪三族皆未過關後加入）：大方向過濾 —— 只做順著 30 天趨勢的方向。
# 假設：逆大勢的突破多為假突破。過濾參數固定不掃（EMA 720 根 1h ≈ 30 天、斜率看 24 根），
# 不增加選參自由度；但族數翻倍，多重比較的試驗數照實累計。
FAMILIES = {**_BASE, **{f"{k}_t": v for k, v in _BASE.items()}}
TREND_EMA, TREND_SLOPE = 720, 24

# 非加密資產或結構特殊、不納入順勢驗證的標的
EXCLUDED = {"XAUTUSDT", "PAXGUSDT", "USDCUSDT", "FDUSDUSDT"}
MIN_HISTORY_SHARE = 0.9    # 歷史不足要求天數 90% 的新幣不納入（walk-forward 折數不夠）


def combos(family: str) -> list[dict]:
    g = FAMILIES[family]
    return [dict(zip(g, v)) for v in itertools.product(*g.values())]


def total_trials() -> int:
    """三族全部組合數 —— 最後會挑表現最好的那族，所以多重比較要算總數"""
    return sum(len(combos(f)) for f in FAMILIES)


# ── 進場訊號：第 t 根收盤時，只用 ≤ t 的資料決定 ──────────────────────────

def signal_donchian(df: pd.DataFrame, n: int, **_) -> np.ndarray:
    hi = df["high"].rolling(n).max().shift(1)       # 不含當根
    lo = df["low"].rolling(n).min().shift(1)
    s = np.where(df["close"] > hi, 1, np.where(df["close"] < lo, -1, 0))
    return s.astype(np.int8)


def signal_ema_cross(df: pd.DataFrame, n: int, **_) -> np.ndarray:
    ema = df["close"].ewm(span=n, adjust=False, min_periods=n).mean()
    slope = ema - ema.shift(5)
    above, prev_above = df["close"] > ema, df["close"].shift(1) > ema.shift(1)
    up = above & ~prev_above & (slope > 0)
    dn = ~above & prev_above & (slope < 0)
    return np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)


def signal_squeeze(df: pd.DataFrame, pct: float, bb_n: int = 20, rank_n: int = 500, **_) -> np.ndarray:
    mid = df["close"].rolling(bb_n).mean()
    sd = df["close"].rolling(bb_n).std(ddof=0)
    upper, lower = mid + 2 * sd, mid - 2 * sd
    width = (upper - lower) / mid
    # 前一根的帶寬落在過去 rank_n 根的最低 pct 分位 → 正在壓縮
    rank = width.rolling(rank_n).rank(pct=True).shift(1)
    tight = rank <= pct
    s = np.where(tight & (df["close"] > upper), 1, np.where(tight & (df["close"] < lower), -1, 0))
    return s.astype(np.int8)


def trend_direction(df: pd.DataFrame) -> np.ndarray:
    """大方向：收盤在 EMA720 之上且 EMA 上彎 → +1；之下且下彎 → -1；其餘 0"""
    ema = df["close"].ewm(span=TREND_EMA, adjust=False, min_periods=TREND_EMA).mean()
    slope = ema - ema.shift(TREND_SLOPE)
    up = (df["close"] > ema) & (slope > 0)
    dn = (df["close"] < ema) & (slope < 0)
    return np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)


def _with_trend(fn):
    def wrapped(df, **p):
        s = fn(df, **p)
        return np.where(s == trend_direction(df), s, 0).astype(np.int8)
    return wrapped


_BASE_SIGNALS = {"donchian": signal_donchian, "ema_cross": signal_ema_cross, "squeeze": signal_squeeze}
SIGNALS = {**_BASE_SIGNALS, **{f"{k}_t": _with_trend(fn) for k, fn in _BASE_SIGNALS.items()}}


# ── 出場模擬 ───────────────────────────────────────────────────────────────

def simulate(df: pd.DataFrame, sides: np.ndarray, stop_atr: float, trail_atr: float,
             atr: np.ndarray | None = None, **_) -> list[tuple]:
    """逐根模擬，同時只持有一筆。
    回傳 [(entry_i, exit_i, side, pnl_pct(已扣費), mae_pct, risk_pct(初始停損距離))]"""
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    if atr is None:
        atr = calculate_atr(df, 14).to_numpy(float)
    n = len(c)
    out = []
    i = 0
    while i < n:
        side = sides[i]
        if side == 0 or not np.isfinite(atr[i]) or atr[i] <= 0:
            i += 1
            continue
        entry, a = c[i], atr[i]
        stop = entry - side * stop_atr * a
        best = entry          # 多單＝最高價、空單＝最低價
        worst = entry
        j = i + 1
        exit_px = None
        while j < n:
            # 1) 先檢查本根是否觸及「上一根結束時」的停損（保守：停損優先於本根新高）
            if side == 1 and l[j] <= stop:
                exit_px = min(stop, o[j])
                worst = min(worst, l[j])
                break
            if side == -1 and h[j] >= stop:
                exit_px = max(stop, o[j])
                worst = max(worst, h[j])
                break
            worst = min(worst, l[j]) if side == 1 else max(worst, h[j])
            # 2) 本根收完才更新吊燈追蹤停損（下一根才生效）
            best = max(best, h[j]) if side == 1 else min(best, l[j])
            trail = best - side * trail_atr * a
            stop = max(stop, trail) if side == 1 else min(stop, trail)
            j += 1
        if exit_px is None:
            break            # 持倉到資料結尾 → 不計入
        pnl = (exit_px - entry) / entry * 100 * side - FEE
        mae = (worst - entry) / entry * 100 * side
        risk = stop_atr * a / entry * 100
        out.append((i, j, int(side), float(pnl), float(min(mae, 0.0)), float(risk)))
        i = j + 1
    return out


# ── Walk-forward ───────────────────────────────────────────────────────────

def walk_forward(df: pd.DataFrame, family: str, symbol: str, train_bars: int, test_bars: int,
                 min_trades: int = 5) -> list[Trade]:
    """選參指標＝訓練窗平均 R 倍數（與風險定倉一致）。
    每個參數組合只模擬一次全序列（模擬本身是因果的），再依進場時點切窗：
    訓練窗只採計「進場與出場都在窗內」的交易 → 選參絕不會看到測試期；
    測試窗採計「在窗內進場」的交易（出場可延伸到窗外，那只是該筆的實際結果）。"""
    atr = calculate_atr(df, 14).to_numpy(float)
    sims = []
    for p in combos(family):
        sides = SIGNALS[family](df, **p)
        sims.append((p, simulate(df, sides, atr=atr, **p)))

    idx = df.index
    trades: list[Trade] = []
    start = 0
    n = len(df)
    while start + train_bars + test_bars <= n:
        tr_s, tr_e, te_e = start, start + train_bars, start + train_bars + test_bars
        best_p, best_e = None, -1e9
        for p, tr in sims:
            pn = [t[3] / t[5] for t in tr if tr_s <= t[0] and t[1] < tr_e]
            if len(pn) >= min_trades and np.mean(pn) > best_e:
                best_p, best_e = p, float(np.mean(pn))
        if best_p is not None:
            chosen = next(tr for p, tr in sims if p is best_p)
            period = f"{idx[tr_e]:%Y-%m}"
            trades += [Trade(symbol, period, t[3], t[4], t[5]) for t in chosen if tr_e <= t[0] < te_e]
        start += test_bars
    return trades


def main():
    ap = argparse.ArgumentParser(description="順勢候選策略 walk-forward")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--train", type=int, default=180, help="訓練窗（天）")
    ap.add_argument("--test", type=int, default=30, help="測試窗（天）")
    ap.add_argument("--out", default="out", help="輸出資料夾：<family>-<symbol>.csv")
    ap.add_argument("--families", default=None, help="逗號分隔；預設全部")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.disable(logging.INFO)

    from backtest.fetch_klines import get_klines_df
    if args.symbol in EXCLUDED:
        print(f"  {args.symbol}: 排除（非加密資產）")
        return
    df = asyncio.run(get_klines_df(args.symbol, "1h", args.days))
    print(f"  {args.symbol}: {len(df):,} 根 1h（{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}）")
    if len(df) < args.days * 24 * MIN_HISTORY_SHARE:
        print(f"  {args.symbol}: 歷史不足 {MIN_HISTORY_SHARE*100:.0f}% × {args.days} 天，排除")
        return
    Path(args.out).mkdir(parents=True, exist_ok=True)
    fams = args.families.split(",") if args.families else list(FAMILIES)
    for fam in fams:
        t = walk_forward(df, fam, args.symbol, args.train * 24, args.test * 24)
        write_trades_csv(f"{args.out}/{fam}-{args.symbol}.csv", t)
        p = np.array([x.pnl_pct for x in t])
        r = np.array([x.r() for x in t])
        if len(p):
            print(f"    {fam:<12} 樣本外 {len(p):>4} 筆  勝率 {np.mean(p > 0)*100:5.1f}%  "
                  f"每筆 {p.mean():+.3f}% / {r.mean():+.3f}R  累計 {r.sum():+7.1f}R")
        else:
            print(f"    {fam:<10} 樣本外 0 筆")


if __name__ == "__main__":
    main()
