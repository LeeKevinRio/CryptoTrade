"""參數網格掃描 — 在離線回測之上搜尋更好的出場參數組合

目標：把 settings.yaml 裡「人工猜值」的停損/停利/移動停利，換成回測驗證過、
盈虧比(payoff) > 1 且正期望值的組合。

用法：
    python -m backtest.param_sweep                # futures，預設網格
    python -m backtest.param_sweep --bot spot
    python -m backtest.param_sweep --days 60 --top 20 --min-trades 12
    python -m backtest.param_sweep --no-report    # 只印到終端機，不寫檔

排名邏輯：先濾掉交易筆數過少（過擬合）與盈虧比 < 1 的組合，再按總盈虧排序。
報表同時列出「目前設定」基準列，方便看出改善幅度。
"""

import argparse
import copy
import itertools
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from src.strategy.base_strategy import SignalType
from src.strategy.signal_aggregator import SignalAggregator
from src.utils.config import load_config
from backtest.backtester import Backtester
from backtest.run_offline_backtest import generate_realistic_data


# 掃描維度：name -> (config 路徑, 候選值)
# 路徑相對於扁平 config {"strategy": ..., "risk": ...}
SWEEP_GRID = {
    # 真實資料掃描顯示停損是主導變數，且最佳值落在舊網格上限，故延伸到 5.0
    "stop_loss_pct":  (("risk", "stop_loss_pct"),                  [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]),
    "level_1_pct":    (("risk", "take_profit", "level_1_pct"),     [0.7, 1.0, 1.5]),
    "level_2_pct":    (("risk", "take_profit", "level_2_pct"),     [1.5, 2.5, 3.5]),
    "level_3_pct":    (("risk", "take_profit", "level_3_pct"),     [3.0, 4.5]),
    "trail_callback": (("risk", "trailing_stop", "callback_pct"),  [0.4, 0.6, 0.8]),
}


@dataclass
class SweepRow:
    params: dict
    trades: int
    win_rate: float
    total_pnl_pct: float
    payoff: float          # 平均獲利 ÷ |平均虧損|，>1 才划算
    profit_factor: float
    expectancy_pct: float  # 每筆平均盈虧
    max_dd_pct: float
    is_baseline: bool = False


def _set_path(cfg: dict, path: tuple, value):
    node = cfg
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def _flat_cfg(bot_cfg: dict) -> dict:
    return {
        "strategy": copy.deepcopy(bot_cfg.get("strategy", {})),
        "risk": copy.deepcopy(bot_cfg.get("risk", {})),
    }


def precompute_sides(df, flat_cfg: dict, symbol: str, warmup: int = 50) -> np.ndarray:
    """對每根 K 線算一次進場訊號，回傳 side 陣列（0=無, 1=LONG, -1=SHORT）。

    這是掃描裡唯一昂貴的部分（每根重算所有指標）。因為只掃出場參數、進場策略不變，
    整個網格共用同一份訊號，故只算一次。與 backtester.run 的進場判定完全一致：
    lookback = 前 200 根、三時框都餵同一份、funding_rate=0。
    """
    aggregator = SignalAggregator(flat_cfg)
    n = len(df)
    sides = np.zeros(n, dtype=np.int8)
    for i in range(warmup, n):
        lookback = df.iloc[max(0, i - 200):i + 1]
        candles = {"5m": lookback, "15m": lookback, "1h": lookback}
        signal = aggregator.evaluate(symbol, candles, funding_rate=0.0)
        if signal.is_actionable:
            sides[i] = 1 if signal.type == SignalType.LONG else -1
    return sides


def fast_backtest(
    closes: np.ndarray, highs: np.ndarray, lows: np.ndarray,
    sides: np.ndarray, params: dict, trail_activation: float, warmup: int = 50,
) -> list[float]:
    """純出場模擬 — 給定預算好的進場點，套一組出場參數，回傳每筆交易的 pnl%。

    出場邏輯逐行對齊 backtester.run（停損 → 取最高觸發的階梯停利×0.9 滑價 → 移動停利）。
    """
    stop = params["stop_loss_pct"]
    tp_levels = (params["level_1_pct"], params["level_2_pct"], params["level_3_pct"])
    trail_cb = params["trail_callback"]
    n = len(closes)
    pnls: list[float] = []

    i = warmup
    while i < n:
        if sides[i] == 0:
            i += 1
            continue
        # 進場
        side = sides[i]
        entry = closes[i]
        highest = 0.0
        trailing = False
        j = i + 1
        exited = False
        while j < n:
            c, h, l = closes[j], highs[j], lows[j]
            if side == 1:  # LONG
                pnl = (c - entry) / entry * 100
                worst = (l - entry) / entry * 100
                best = (h - entry) / entry * 100
            else:          # SHORT
                pnl = (entry - c) / entry * 100
                worst = (entry - h) / entry * 100
                best = (entry - l) / entry * 100
            if best > highest:
                highest = best

            reason = None
            if worst <= -stop:
                pnl = -stop
                reason = "SL"
            if reason is None:
                for lv in reversed(tp_levels):   # 取最高觸發層
                    if best >= lv:
                        pnl = lv * 0.9           # 模擬滑價，與 backtester 一致
                        reason = "TP"
                        break
            if reason is None and highest >= trail_activation:
                trailing = True
            if reason is None and trailing and (highest - pnl) >= trail_cb:
                pnl = highest - trail_cb
                reason = "trail"

            if reason is not None:
                pnls.append(round(pnl, 2))
                i = j + 1
                exited = True
                break
            j += 1

        if not exited:
            break  # 持倉到資料結尾未出場 → 不記錄，與 backtester 一致
    return pnls


def _row_from_pnls(pnls: list[float], params: dict, baseline=False) -> SweepRow:
    total = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = round(sum(pnls), 2)
    win_rate = round(len(wins) / max(1, total) * 100, 1)
    avg_win = np.mean(wins) if wins else 0.0
    avg_loss = np.mean(losses) if losses else 0.0
    payoff = abs(avg_win / avg_loss) if avg_loss else 0.0
    total_win = sum(wins)
    total_loss = abs(sum(losses))
    pf = round(total_win / max(total_loss, 0.01), 2)
    # 最大回撤（權益曲線）
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return SweepRow(
        params=params,
        trades=total,
        win_rate=win_rate,
        total_pnl_pct=total_pnl,
        payoff=round(payoff, 2),
        profit_factor=pf,
        expectancy_pct=round(total_pnl / total, 3) if total else 0.0,
        max_dd_pct=round(max_dd, 2),
        is_baseline=baseline,
    )


def _valid_tp_order(combo: dict) -> bool:
    """三階停利必須嚴格遞增（否則階梯無意義）。"""
    return combo["level_1_pct"] < combo["level_2_pct"] < combo["level_3_pct"]


RANK_KEYS = {
    "pnl": lambda r: r.total_pnl_pct,
    "winrate": lambda r: r.win_rate,
    "payoff": lambda r: r.payoff,
    "expectancy": lambda r: r.expectancy_pct,
}


def run_sweep(bot_id: str, days: int, min_trades: int, top: int, rank: str = "pnl",
              real: bool = False, symbol: str = "BTCUSDT"):
    config = load_config()
    bot_cfg = config.get("bots", {}).get(bot_id)
    if not bot_cfg:
        raise SystemExit(f"config 找不到 bot: {bot_id}")

    if real:
        import asyncio
        from backtest.fetch_klines import get_klines_df
        df = asyncio.run(get_klines_df(symbol, "5m", days))
        print(f"  真實資料 {symbol}：{len(df)} 根 | "
              f"{df.index[0]:%Y-%m-%d} ~ {df.index[-1]:%Y-%m-%d}", flush=True)
    else:
        df = generate_realistic_data(days=days, interval_minutes=5)
    base_flat = _flat_cfg(bot_cfg)

    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)

    # ── 進場訊號只算一次（整個網格共用）──
    print(f"  計算進場訊號中（{len(df)} 根 K 線，只算一次）...", flush=True)
    sides = precompute_sides(df, base_flat, symbol)
    n_signals = int(np.count_nonzero(sides))
    print(f"  完成：{n_signals} 個可進場訊號\n", flush=True)

    trail_activation = base_flat.get("risk", {}).get("trailing_stop", {}).get("activation_pct", 1.5)

    # ── 基準：目前 settings.yaml 的設定 ──
    base_params = {
        name: _read_path(base_flat, path) for name, (path, _) in SWEEP_GRID.items()
    }
    base_pnls = fast_backtest(closes, highs, lows, sides, base_params, trail_activation)
    baseline = _row_from_pnls(base_pnls, base_params, baseline=True)

    # ── 保真度檢查：快速模擬 vs 真 Backtester（同一組基準參數應吻合）──
    real = Backtester(base_flat).run(df, symbol)
    drift = abs(real.total_pnl_pct - baseline.total_pnl_pct)
    fidelity = (
        f"保真度檢查 OK（快速模擬 {baseline.total_pnl_pct:+.2f}% vs "
        f"真回測 {real.total_pnl_pct:+.2f}%）"
        if drift < 0.5 and real.total_trades == baseline.trades
        else f"⚠️ 保真度偏差：模擬 {baseline.total_pnl_pct:+.2f}%/{baseline.trades}筆 "
             f"vs 真回測 {real.total_pnl_pct:+.2f}%/{real.total_trades}筆"
    )
    print(f"  {fidelity}\n", flush=True)

    # ── 笛卡兒積掃描（每組只跑純出場模擬，極快）──
    names = list(SWEEP_GRID.keys())
    value_lists = [SWEEP_GRID[n][1] for n in names]
    rows: list[SweepRow] = []
    tested = 0
    for values in itertools.product(*value_lists):
        combo = dict(zip(names, values))
        if not _valid_tp_order(combo):
            continue
        pnls = fast_backtest(closes, highs, lows, sides, combo, trail_activation)
        rows.append(_row_from_pnls(pnls, combo))
        tested += 1

    # ── 排名：只濾樣本太少（過擬合），按指定指標降序；payoff>=1 僅做標註不硬過濾 ──
    eligible = [r for r in rows if r.trades >= min_trades]
    key = RANK_KEYS.get(rank, RANK_KEYS["pnl"])
    eligible.sort(key=key, reverse=True)
    # 獲利判準是 PF>1（等價於期望值>0），不是 payoff>=1：
    # payoff<1 只要勝率夠高仍可獲利（例：勝率 77% + payoff 0.36 → PF 1.17）
    n_profitable = sum(1 for r in eligible if r.profit_factor > 1.0)

    return bot_id, days, tested, min_trades, rank, n_profitable, baseline, eligible[:top]


def _read_path(cfg: dict, path: tuple):
    node = cfg
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else None
    return node


def _fmt_params(p: dict) -> str:
    return (
        f"SL {p['stop_loss_pct']} | TP {p['level_1_pct']}/{p['level_2_pct']}/{p['level_3_pct']} "
        f"| trail回撤 {p['trail_callback']}"
    )


def _print_report(bot_id, days, tested, min_trades, rank, n_profitable,
                  baseline: SweepRow, top: list[SweepRow], source: str = "模擬資料(seed=42)"):
    lines = []
    def out(s=""):
        print(s)
        lines.append(s)

    out("=" * 78)
    out(f"  參數掃描結果 — bot={bot_id}  資料={days}天  測試組合={tested}  "
        f"排序={rank}  (min_trades={min_trades})")
    out(f"  資料來源：{source}")
    out(f"  獲利組合（PF>1）：{n_profitable}/{tested}")
    out("=" * 78)
    header = f"  {'參數組合':<46}{'筆':>4}{'勝率':>6}{'總盈虧':>9}{'payoff':>8}{'PF':>6}{'期望':>8}{'回撤':>8}"
    out(header)
    out("-" * 78)

    def row_str(r: SweepRow, tag=""):
        return (f"  {_fmt_params(r.params):<46}{r.trades:>4}{r.win_rate:>5.0f}%"
                f"{r.total_pnl_pct:>+8.1f}%{r.payoff:>8.2f}{r.profit_factor:>6.2f}"
                f"{r.expectancy_pct:>+7.2f}%{r.max_dd_pct:>7.1f}%{tag}")

    out(row_str(baseline, "  ← 目前設定"))
    out("-" * 78)
    if not top:
        out("  ⚠️  沒有組合滿足 min_trades，試著放寬 --min-trades")
    for i, r in enumerate(top, 1):
        out(f"{i:>2}." + row_str(r)[3:])
    out("=" * 78)

    if top:
        best = top[0]
        out(f"\n  🏆 排序第一（vs 目前設定）：")
        out(f"     {_fmt_params(best.params)}")
        out(f"     總盈虧 {baseline.total_pnl_pct:+.1f}% → {best.total_pnl_pct:+.1f}%   "
            f"勝率 {baseline.win_rate:.0f}% → {best.win_rate:.0f}%   "
            f"PF {baseline.profit_factor:.2f} → {best.profit_factor:.2f}   "
            f"期望 {baseline.expectancy_pct:+.3f}% → {best.expectancy_pct:+.3f}%")
        if n_profitable == 0:
            out("\n  ⚠️  沒有任何出場參數組合能讓 PF>1 —— 瓶頸在【進場訊號品質】，"
                "\n     光調停損/停利救不了。下一步應調進場條件（min_signals / 訊號門檻）。")
        else:
            out(f"\n  套用方式：把排序第一的 SL / TP 三階 / trail回撤 填回 config/settings.yaml。")
            out("  ⚠️  套用前務必用另一個交易對／另一段期間做樣本外驗證，避免過擬合。")
    out("\n  ⚠️  回測未計手續費與資金費率。高槓桿下每筆來回約吃掉帳戶 "
        "(2 × 0.04% × 槓桿 × 單筆%)，交易越頻繁侵蝕越大。")
    return "\n".join(lines)


def main():
    # Windows console 預設 cp950 無法輸出中文/emoji/數學符號；強制 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    # 掃描時關掉 INFO 級 log，避免每根 K 線的訊號日誌洗版（保留 WARNING 以上）
    logging.disable(logging.INFO)

    ap = argparse.ArgumentParser(description="參數網格掃描")
    ap.add_argument("--bot", default="futures", help="要掃描的 bot id（預設 futures）")
    ap.add_argument("--days", type=int, default=45, help="模擬資料天數（預設 45）")
    ap.add_argument("--min-trades", type=int, default=8, help="最少交易筆數門檻（預設 8）")
    ap.add_argument("--top", type=int, default=15, help="顯示前 N 名（預設 15）")
    ap.add_argument("--rank", default="pnl", choices=list(RANK_KEYS.keys()),
                    help="排序指標：pnl / winrate / payoff / expectancy（預設 pnl）")
    ap.add_argument("--no-report", action="store_true", help="不寫入 reports/ 檔案")
    ap.add_argument("--real", action="store_true",
                    help="使用真實歷史 K 線（Binance 公開端點 + 本地快取）而非模擬資料")
    ap.add_argument("--symbol", default="BTCUSDT", help="--real 時的交易對")
    args = ap.parse_args()

    bot_id, days, tested, min_trades, rank, n_profitable, baseline, top = run_sweep(
        args.bot, args.days, args.min_trades, args.top, args.rank,
        real=args.real, symbol=args.symbol,
    )
    source = f"真實歷史K線 {args.symbol}" if args.real else "模擬資料(seed=42)"
    report = _print_report(bot_id, days, tested, min_trades, rank, n_profitable,
                           baseline, top, source)
    if not args.no_report:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        src = f"real-{args.symbol}" if args.real else "sim"
        path = f"reports/sweep-{bot_id}-{src}-{ts}.md"
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 參數掃描報表\n\n```\n")
            f.write(report)
            f.write("\n```\n")
        print(f"\n  📄 已寫入 {path}")


if __name__ == "__main__":
    main()
