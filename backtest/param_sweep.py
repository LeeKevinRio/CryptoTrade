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

2026-09-26 起：
  * 出場模擬改為與實盤一致的「階梯部分平倉」（L1/L2/L3 各平 40/35/25%，
    剩餘由移動停利／時間停損處理），不再是「觸到最高階就整筆出場」——
    舊模型會把賠率高估數倍，正是先前回測看起來比實盤好很多的原因。
  * 每筆扣估算手續費 EST_FEE_RATE（0.08%/來回）出 net_* 欄，排序請用 --rank net。
  * --thresholds 50,60 可同時掃進場門檻（每個門檻要重算一次訊號）。
"""

import argparse
import copy
import itertools
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from src.strategy.base_strategy import SignalType
from src.utils.performance import EST_FEE_RATE
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
    # 扣估算手續費後（真金決策只看這三個）
    net_pnl_pct: float = 0.0
    net_expectancy_pct: float = 0.0
    net_profit_factor: float = 0.0


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
    bar_seconds: int = 300, close_pcts: tuple = (40, 35, 25),
) -> list[float]:
    """純出場模擬 — 給定預算好的進場點，套一組出場參數，回傳每筆交易的 pnl%
    （以整筆部位為 100% 的加權實現損益，未扣手續費）。

    出場邏輯對齊實盤 PositionManager.check_risk / TakeProfitManager.check：
      1. 停損：bar 低點（空單為高點）觸及 → 剩餘部位全部以 -stop 出場（優先）
      2. 階梯停利：bar 高點觸及 L1/L2/L3 → 各平 close_pcts 比例，maker 限價成交於目標價
      3. 移動停利：MFE ≥ activation 後，從高點回撤 ≥ callback → 剩餘全平於 (高點 - 回撤)
      4. 時間停損：超時後「無波動」以現價出場；或 L1 已觸發且仍獲利則鎖利出場
    舊版把「觸到最高階」當整筆出場，賠率被高估數倍；本版一筆交易的最大獲利
    是 Σ close_pct × level（預設 2.2%），這才是實盤真正拿得到的。
    """
    stop = params["stop_loss_pct"]
    tp_levels = (params["level_1_pct"], params["level_2_pct"], params["level_3_pct"])
    weights = tuple(c / 100 for c in close_pcts)
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
        side = sides[i]
        entry = closes[i]
        remaining = 1.0       # 尚未平掉的部位比例
        realized = 0.0        # 已實現損益（占整筆部位的 %）
        triggered = [False, False, False]
        highest = 0.0         # MFE
        lowest = 0.0          # MAE（≤0）
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

            reason = None
            if worst <= -stop:                       # 停損優先（保守：同根先算停損）
                realized += remaining * (-stop)
                remaining = 0.0
                reason = "SL"
            else:
                for k in range(3):                   # 階梯：依序部分平倉
                    if not triggered[k] and best >= tp_levels[k]:
                        triggered[k] = True
                        q = min(weights[k], remaining)
                        realized += q * tp_levels[k]
                        remaining -= q
                if remaining <= 1e-9:
                    reason = "TP"
                else:
                    if highest >= trail_activation:
                        trailing = True
                    if trailing and (highest - pnl) >= trail_cb:
                        realized += remaining * (highest - trail_cb)
                        remaining = 0.0
                        reason = "trail"
                    elif ts_bars and (j - i) >= ts_bars:
                        if highest < ts_nomove and -lowest < ts_nomove:
                            realized += remaining * pnl
                            remaining = 0.0
                            reason = "time_nomove"
                        elif triggered[0] and pnl > 0:
                            realized += remaining * pnl
                            remaining = 0.0
                            reason = "time_lock"

            if reason is not None:
                pnls.append(round(realized, 3))
                i = j + 1
                exited = True
                break
            j += 1
        if not exited:
            break  # 持倉到資料結尾未出場 → 不記錄，與 backtester 一致
    return pnls


def _row_from_pnls(pnls: list[float], params: dict, baseline=False,
                   fee_pct: float = EST_FEE_RATE * 100) -> SweepRow:
    total = len(pnls)
    # 扣估算手續費（每筆來回 0.08% 名目）—— 真金決策只能看淨額
    net = [p - fee_pct for p in pnls]
    net_wins = sum(p for p in net if p > 0)
    net_losses = abs(sum(p for p in net if p <= 0))
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
        net_pnl_pct=round(sum(net), 2),
        net_expectancy_pct=round(sum(net) / total, 3) if total else 0.0,
        net_profit_factor=round(net_wins / max(net_losses, 0.01), 2),
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
    "net": lambda r: r.net_expectancy_pct,
}


def run_sweep(bot_id: str, days: int, min_trades: int, top: int, rank: str = "pnl",
              real: bool = False, symbol: str = "BTCUSDT", faithful: bool = True,
              dump_csv: str | None = None, trend_filter: bool | None = None,
              thresholds: list[float] | None = None):
    config = load_config()
    bot_cfg = config.get("bots", {}).get(bot_id)
    if not bot_cfg:
        raise SystemExit(f"config 找不到 bot: {bot_id}")
    if trend_filter is not None:
        # A/B 對照用：強制覆寫趨勢過濾開關（不動 settings.yaml）
        bot_cfg.setdefault("strategy", {}).setdefault("trend_filter", {})["enabled"] = trend_filter
        print(f"  趨勢過濾: {'開' if trend_filter else '關'}（CLI 覆寫）", flush=True)

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

    mode_txt = "保真模式：真實 15m/1h/4h 重取樣" + ("＋情緒回放" if replay else "（無情緒歷史）") \
        if faithful else "舊模式：三時框同一份 5m、無情緒"
    print(f"  {mode_txt}", flush=True)

    tp_cfg = base_flat.get("risk", {}).get("take_profit", {})
    close_pcts = tuple(
        tp_cfg.get(f"level_{k}_close_pct", d) for k, d in ((1, 40), (2, 35), (3, 25))
    )
    trail_activation = base_flat.get("risk", {}).get("trailing_stop", {}).get("activation_pct", 1.5)
    base_threshold = base_flat.get("strategy", {}).get("medium_signal_threshold", 60)
    thresholds = list(thresholds) if thresholds else [base_threshold]
    if base_threshold not in thresholds:
        thresholds.append(base_threshold)        # 基準列一定要有

    names = list(SWEEP_GRID.keys())
    value_lists = [SWEEP_GRID[n][1] for n in names]
    rows: list[SweepRow] = []
    baseline = None
    tested = 0
    for thr in thresholds:
        flat = copy.deepcopy(base_flat)
        flat.setdefault("strategy", {})["medium_signal_threshold"] = thr
        # ── 進場訊號：每個門檻算一次（出場網格共用）──
        print(f"  [門檻 {thr}] 計算進場訊號中（{len(df)} 根 K 線）...", flush=True)
        sides = precompute_sides(df, flat, symbol, faithful=faithful,
                                 sentiment_replay=replay)
        print(f"  [門檻 {thr}] 完成：{int(np.count_nonzero(sides))} 個可進場訊號", flush=True)

        if thr == base_threshold:
            # ── 基準：目前 settings.yaml 的設定 ──
            base_params = {"signal_threshold": thr, **{
                name: _read_path(base_flat, path) for name, (path, _) in SWEEP_GRID.items()
            }}
            base_pnls = fast_backtest(closes, highs, lows, sides, base_params,
                                      trail_activation, close_pcts=close_pcts)
            baseline = _row_from_pnls(base_pnls, base_params, baseline=True)

        # ── 笛卡兒積掃描（每組只跑純出場模擬，極快）──
        for values in itertools.product(*value_lists):
            combo = dict(zip(names, values))
            if not _valid_tp_order(combo):
                continue
            combo = {"signal_threshold": thr, **combo}
            pnls = fast_backtest(closes, highs, lows, sides, combo, trail_activation,
                                 close_pcts=close_pcts)
            rows.append(_row_from_pnls(pnls, combo))
            tested += 1
    print(flush=True)

    # ── 排名：只濾樣本太少（過擬合），按指定指標降序；payoff>=1 僅做標註不硬過濾 ──
    eligible = [r for r in rows if r.trades >= min_trades]
    key = RANK_KEYS.get(rank, RANK_KEYS["pnl"])
    eligible.sort(key=key, reverse=True)
    # 獲利判準是 PF>1（等價於期望值>0），不是 payoff>=1：
    # payoff<1 只要勝率夠高仍可獲利（例：勝率 77% + payoff 0.36 → PF 1.17）
    n_profitable = sum(1 for r in eligible if r.profit_factor > 1.0)

    # 全量結果輸出 — 供跨標的比對（top-N 報表資訊不足以找共同穩健組）
    if dump_csv:
        import csv
        Path(dump_csv).parent.mkdir(parents=True, exist_ok=True)
        param_cols = ["signal_threshold"] + list(SWEEP_GRID.keys())
        with open(dump_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(param_cols + [
                "trades", "win_rate", "total_pnl_pct", "payoff",
                "profit_factor", "expectancy_pct", "max_dd_pct",
                "net_pnl_pct", "net_expectancy_pct", "net_profit_factor",
            ])
            for r in eligible:
                w.writerow(
                    [r.params.get(c) for c in param_cols]
                    + [r.trades, r.win_rate, r.total_pnl_pct, r.payoff,
                       r.profit_factor, r.expectancy_pct, r.max_dd_pct,
                       r.net_pnl_pct, r.net_expectancy_pct, r.net_profit_factor]
                )
        print(f"  📄 全量結果已寫入 {dump_csv}（{len(eligible)} 組）", flush=True)

    return bot_id, days, tested, min_trades, rank, n_profitable, baseline, eligible[:top]


def _read_path(cfg: dict, path: tuple):
    node = cfg
    for key in path:
        node = node.get(key, {}) if isinstance(node, dict) else None
    return node


def _fmt_params(p: dict) -> str:
    ts = p.get("time_stop_seconds")
    ts_txt = "時停關" if ts is None else f"時停{ts//60}分/{p.get('time_stop_no_movement_pct')}"
    thr = p.get("signal_threshold")
    thr_txt = f"門檻{thr:g} " if thr is not None else ""
    return (
        f"{thr_txt}SL{p['stop_loss_pct']} TP{p['level_1_pct']}/{p['level_2_pct']}/{p['level_3_pct']} "
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
    header = (f"   {'參數組合':<50}{'筆':>4}{'勝率':>6}{'總盈虧':>9}{'payoff':>8}{'PF':>6}"
              f"{'期望':>8}{'淨期望':>8}{'淨PF':>6}{'回撤':>8}")
    out(header)
    out("-" * 78)

    def row_str(r: SweepRow, tag=""):
        return (f"   {_fmt_params(r.params):<50}{r.trades:>4}{r.win_rate:>5.0f}%"
                f"{r.total_pnl_pct:>+8.1f}%{r.payoff:>8.2f}{r.profit_factor:>6.2f}"
                f"{r.expectancy_pct:>+7.2f}%{r.net_expectancy_pct:>+7.2f}%"
                f"{r.net_profit_factor:>6.2f}{r.max_dd_pct:>7.1f}%{tag}")

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
    out(f"\n  ℹ️  淨期望／淨PF 已扣每筆 {EST_FEE_RATE*100:.2f}% 估算手續費（未計資金費率）；"
        "出場模擬為實盤階梯部分平倉語義（L1/L2/L3 各平 40/35/25%）。")
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
    ap.add_argument("--dump-csv", default=None,
                    help="把全部合格組合的完整指標寫成 CSV（供跨標的比對）")
    ap.add_argument("--thresholds", default=None,
                    help="同時掃進場門檻，逗號分隔（例 50,60）；預設只用 settings 現值")
    ap.add_argument("--trend-filter", default=None, choices=["on", "off"],
                    help="強制開/關高時框趨勢過濾（A/B 對照用，預設依 settings.yaml）")
    args = ap.parse_args()

    bot_id, days, tested, min_trades, rank, n_profitable, baseline, top = run_sweep(
        args.bot, args.days, args.min_trades, args.top, args.rank,
        real=args.real, symbol=args.symbol, faithful=not args.legacy,
        dump_csv=args.dump_csv,
        trend_filter=None if args.trend_filter is None else args.trend_filter == "on",
        thresholds=[float(t) for t in args.thresholds.split(",")] if args.thresholds else None,
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
