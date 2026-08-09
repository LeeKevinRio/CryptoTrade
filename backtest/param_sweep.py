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
    "stop_loss_pct":  (("risk", "stop_loss_pct"),                  [2.0, 3.0, 4.0, 5.0]),
    "level_1_pct":    (("risk", "take_profit", "level_1_pct"),     [0.7, 1.0, 1.5]),
    "level_2_pct":    (("risk", "take_profit", "level_2_pct"),     [1.5, 2.5]),
    "level_3_pct":    (("risk", "take_profit", "level_3_pct"),     [3.0, 4.5]),
    "trail_callback": (("risk", "trailing_stop", "callback_pct"),  [0.4, 0.6, 0.8]),
    # 時間停損：實盤 78% 的出場來源，卻是從未被優化過的參數。
    # None = 停用時間停損（即先前回測隱含的行為）。
    "time_stop_seconds": (("risk", "time_stop_seconds"),
                          [1200, 2400, 3600, 7200, 14400, None]),
    "time_stop_no_movement_pct": (("risk", "time_stop_no_movement_pct"),
                                  [0.2, 0.4, 0.8]),
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


def precompute_sides(df, flat_cfg: dict, symbol: str, warmup: int = 50,
                     faithful: bool = True, sentiment_replay=None) -> np.ndarray:
    """對每根 K 線算一次進場訊號，回傳 side 陣列（0=無, 1=LONG, -1=SHORT）。

    這是掃描裡唯一昂貴的部分（每根重算所有指標）。因為只掃出場參數、進場策略不變，
    整個網格共用同一份訊號，故只算一次。

    faithful=True（預設）會以真實重取樣的 15m/1h/4h 餵給策略，並回放歷史情緒，
    使回測評分與實盤一致；faithful=False 保留舊行為（三時框同一份 5m、無情緒），
    僅供與歷史結果對照。
    """
    aggregator = SignalAggregator(flat_cfg)
    n = len(df)
    sides = np.zeros(n, dtype=np.int8)

    if faithful:
        from backtest.fidelity import TimeframeSlicer, resample_timeframes
        slicer = TimeframeSlicer(resample_timeframes(df))
        idx = df.index
        for i in range(warmup, n):
            ts = idx[i]
            candles = slicer.at(ts)
            if "5m" not in candles or len(candles["5m"]) < 30:
                continue
            sent = sentiment_replay.at(ts, symbol) if sentiment_replay else None
            signal = aggregator.evaluate(symbol, candles, funding_rate=0.0, sentiment=sent)
            if signal.is_actionable:
                sides[i] = 1 if signal.type == SignalType.LONG else -1
        return sides

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
    bar_seconds: int = 300,
) -> list[float]:
    """純出場模擬 — 給定預算好的進場點，套一組出場參數，回傳每筆交易的 pnl%。

    出場邏輯對齊實盤：停損 → 階梯停利(×0.9 滑價) → 移動停利 → 時間停損。

    時間停損（實盤 78% 的出場來源，先前完全未模擬）語意同 TakeProfitManager：
    超時後，若倉位生命週期內 MFE 與 |MAE| 皆 < 門檻視為「無波動」平倉；
    或 L1 已觸發且當下仍獲利則鎖利出場。params 未給 time_stop_seconds 則停用。
    """
    stop = params["stop_loss_pct"]
    tp_levels = (params["level_1_pct"], params["level_2_pct"], params["level_3_pct"])
    trail_cb = params["trail_callback"]
    ts_secs = params.get("time_stop_seconds")
    ts_nomove = params.get("time_stop_no_movement_pct", 0.4)
    ts_bars = int(ts_secs / bar_seconds) if ts_secs else None
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
        lowest = 0.0          # 生命週期內最差 pnl%（≤0），供時間停損判定無波動
        l1_hit = False
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
            if worst < lowest:
                lowest = worst
            if best >= tp_levels[0]:
                l1_hit = True

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

            # 時間停損：語意對齊 TakeProfitManager.check 的第 3 段
            if reason is None and ts_bars and (j - i) >= ts_bars:
                if highest < ts_nomove and -lowest < ts_nomove:
                    reason = "time_nomove"          # 無波動、卡資金 → 以現價出場
                elif l1_hit and pnl > 0:
                    reason = "time_lock"            # 動能停滯但仍獲利 → 鎖利

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
    if not (combo["level_1_pct"] < combo["level_2_pct"] < combo["level_3_pct"]):
        return False
    # 時間停損停用時，無波動門檻無作用 → 只保留一種代表，避免重複組合灌大樣本
    if combo.get("time_stop_seconds") is None:
        return combo.get("time_stop_no_movement_pct") == 0.4
    return True


RANK_KEYS = {
    "pnl": lambda r: r.total_pnl_pct,
    "winrate": lambda r: r.win_rate,
    "payoff": lambda r: r.payoff,
    "expectancy": lambda r: r.expectancy_pct,
}


def run_sweep(bot_id: str, days: int, min_trades: int, top: int, rank: str = "pnl",
              real: bool = False, symbol: str = "BTCUSDT", faithful: bool = True):
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

    # ── 情緒回放（讓 sentiment 權重在回測中也能得分，與實盤一致）──
    replay = None
    if faithful:
        from backtest.fidelity import SentimentReplay
        replay = SentimentReplay.load(days=max(days + 10, 60))
        if not replay.available:
            replay = None

    # ── 進場訊號只算一次（整個網格共用）──
    mode_txt = "保真模式：真實 15m/1h/4h 重取樣" + ("＋情緒回放" if replay else "（無情緒歷史）") \
        if faithful else "舊模式：三時框同一份 5m、無情緒"
    print(f"  {mode_txt}", flush=True)
    print(f"  計算進場訊號中（{len(df)} 根 K 線，只算一次）...", flush=True)
    sides = precompute_sides(df, base_flat, symbol, faithful=faithful,
                             sentiment_replay=replay)
    n_signals = int(np.count_nonzero(sides))
    print(f"  完成：{n_signals} 個可進場訊號\n", flush=True)

    trail_activation = base_flat.get("risk", {}).get("trailing_stop", {}).get("activation_pct", 1.5)

    # ── 基準：目前 settings.yaml 的設定 ──
    base_params = {
        name: _read_path(base_flat, path) for name, (path, _) in SWEEP_GRID.items()
    }
    base_pnls = fast_backtest(closes, highs, lows, sides, base_params, trail_activation)
    baseline = _row_from_pnls(base_pnls, base_params, baseline=True)

    # ── 保真度檢查 ──
    # 舊模式下 Backtester 與 fast_backtest 應完全吻合（驗證出場數學）。
    # 保真模式下 Backtester 仍是舊行為（假時框、無情緒、無時間停損），
    # 兩者本就不該相等，故不做比對以免報出假警告。
    if not faithful:
        real = Backtester(base_flat).run(df, symbol)
        drift = abs(real.total_pnl_pct - baseline.total_pnl_pct)
        ok = drift < 0.5 and real.total_trades == baseline.trades
        print("  " + (
            f"保真度檢查 OK（快速模擬 {baseline.total_pnl_pct:+.2f}% vs "
            f"真回測 {real.total_pnl_pct:+.2f}%）" if ok else
            f"⚠️ 保真度偏差：模擬 {baseline.total_pnl_pct:+.2f}%/{baseline.trades}筆 "
            f"vs 真回測 {real.total_pnl_pct:+.2f}%/{real.total_trades}筆"
        ) + "\n", flush=True)

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
    ts = p.get("time_stop_seconds")
    ts_txt = "時停關" if ts is None else f"時停{ts//60}分/{p.get('time_stop_no_movement_pct')}"
    return (
        f"SL{p['stop_loss_pct']} TP{p['level_1_pct']}/{p['level_2_pct']}/{p['level_3_pct']} "
        f"tr{p['trail_callback']} {ts_txt}"
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
    ap.add_argument("--legacy", action="store_true",
                    help="用舊行為（三時框同一份 5m、無情緒、無時間停損）跑，僅供對照")
    args = ap.parse_args()

    bot_id, days, tested, min_trades, rank, n_profitable, baseline, top = run_sweep(
        args.bot, args.days, args.min_trades, args.top, args.rank,
        real=args.real, symbol=args.symbol, faithful=not args.legacy,
    )
    source = f"真實歷史K線 {args.symbol}" if args.real else "模擬資料(seed=42)"
    source += "｜舊模式" if args.legacy else "｜保真模式(真時框+情緒回放+時間停損)"
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
