"""交易績效總表 — 勝率 / 盈虧比 / 期望值 / 分組統計

執行：
    python -m scripts.report_stats                 # 全期間
    python -m scripts.report_stats --days 7        # 近 7 天
    python -m scripts.report_stats --since 2026-05-01 --until 2026-06-01
    python -m scripts.report_stats --bot futures   # 只看某個 bot

產出：
    1. Console 摘要
    2. reports/stats-YYYY-MM-DD.md 完整 Markdown 報表（UTF-8，避免主控台中文亂碼）
"""

import argparse
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.utils.performance import (
    compute_stats as _stats,
    reason_bucket as _reason_bucket,
    diagnose,
    _group,
    _day_of,
)


def _parse_args():
    p = argparse.ArgumentParser(description="交易績效總表")
    p.add_argument("--days", type=int, default=None, help="只統計近 N 天（依進場時間）")
    p.add_argument("--since", type=str, default=None, help="起始日 YYYY-MM-DD")
    p.add_argument("--until", type=str, default=None, help="結束日 YYYY-MM-DD（含當日）")
    p.add_argument("--bot", type=str, default=None, help="只看指定 bot_id")
    p.add_argument("--db", type=str, default="cryptotrade.db", help="資料庫路徑")
    return p.parse_args()


def _load_closed(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE status='CLOSED' AND pnl IS NOT NULL ORDER BY entry_time"
    )]
    conn.close()
    return rows


def _filter(rows, args):
    out = rows
    if args.bot:
        out = [r for r in out if (r.get("bot_id") or "") == args.bot]

    since = until = None
    if args.days is not None:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    if args.until:
        until = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc) + timedelta(days=1)

    def _ts(r):
        raw = r.get("entry_time")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            return None

    if since or until:
        filtered = []
        for r in out:
            t = _ts(r)
            if t is None:
                continue
            if since and t < since:
                continue
            if until and t >= until:
                continue
            filtered.append(r)
        out = filtered
    return out


def _fmt_inf(v, fmt="{:.2f}"):
    return "∞" if v == float("inf") else fmt.format(v)


def build_report(rows, args) -> str:
    s = _stats(rows)
    lines = []
    A = lines.append

    scope = []
    if args.bot:
        scope.append(f"bot={args.bot}")
    if args.days:
        scope.append(f"近 {args.days} 天")
    if args.since:
        scope.append(f"自 {args.since}")
    if args.until:
        scope.append(f"至 {args.until}")
    scope_str = "，".join(scope) if scope else "全期間"

    if rows:
        t0 = min(r.get("entry_time") or "" for r in rows)[:19]
        t1 = max((r.get("exit_time") or r.get("entry_time") or "") for r in rows)[:19]
    else:
        t0 = t1 = "—"

    A(f"# 📊 交易績效總表（{scope_str}）\n")
    A(f"- 統計範圍：`{t0}` ~ `{t1}`")
    A(f"- 已平倉交易：**{s['n']}** 筆（勝 {s['wins']} / 負 {s['losses']} / 平 {s['flats']}）\n")

    verdict = "🟢 整體獲利" if s["total_pnl"] > 0 else ("🔴 整體虧損" if s["total_pnl"] < 0 else "⚪ 損益兩平")
    A("## 核心指標\n")
    A("| 指標 | 數值 | 說明 |")
    A("|------|------|------|")
    A(f"| **總損益** | **{s['total_pnl']:+.2f} USDT** | {verdict} |")
    A(f"| 勝率（勝/總） | {s['win_rate_all']:.1f}% | 含平盤計入分母 |")
    A(f"| 勝率（勝/負對比） | {s['win_rate_wl']:.1f}% | 排除平盤，較反映方向準度 |")
    A(f"| 平均獲利 | +{s['avg_win']:.2f} USDT | 每筆贏單平均 |")
    A(f"| 平均虧損 | -{s['avg_loss']:.2f} USDT | 每筆輸單平均 |")
    A(f"| 盈虧比（payoff） | {_fmt_inf(s['payoff'])} | 平均賺 ÷ 平均虧，>1 才划算 |")
    A(f"| 獲利因子（PF） | {_fmt_inf(s['profit_factor'])} | 總賺 ÷ 總虧，>1 才賺錢 |")
    A(f"| 期望值 | {s['expectancy']:+.2f} USDT/筆 | 每下一筆的平均結果 |")
    A(f"| 最佳單筆 | {s['best']:+.2f} USDT | |")
    A(f"| 最差單筆 | {s['worst']:+.2f} USDT | |")
    A("")

    # 診斷
    A("## 🔍 診斷\n")
    for d in diagnose(s):
        A(f"- {d}")
    A("")

    # 按 bot
    A("## 按 Bot 分組\n")
    A("| bot | 筆數 | 勝率(勝/負) | 總損益 | 期望值/筆 | PF |")
    A("|-----|------|-------------|--------|-----------|----|")
    for bid, rs in sorted(_group(rows, lambda r: r.get("bot_id") or "未標記").items()):
        st = _stats(rs)
        A(f"| {bid} | {st['n']} | {st['win_rate_wl']:.0f}% | {st['total_pnl']:+.2f} | {st['expectancy']:+.2f} | {_fmt_inf(st['profit_factor'])} |")
    A("")

    # 按 symbol
    A("## 按交易對分組\n")
    A("| 交易對 | 筆數 | 勝率(勝/負) | 總損益 | 平均賺 | 平均虧 |")
    A("|--------|------|-------------|--------|--------|--------|")
    for sym, rs in sorted(_group(rows, lambda r: r["symbol"]).items()):
        st = _stats(rs)
        A(f"| {sym} | {st['n']} | {st['win_rate_wl']:.0f}% | {st['total_pnl']:+.2f} | +{st['avg_win']:.2f} | -{st['avg_loss']:.2f} |")
    A("")

    # 按出場原因
    A("## 按出場原因分組\n")
    A("| 出場原因 | 筆數 | 總損益 | 平均/筆 |")
    A("|----------|------|--------|---------|")
    reason_groups = _group(rows, lambda r: _reason_bucket(r.get("close_reason")))
    for reason, rs in sorted(reason_groups.items(), key=lambda kv: sum(x["pnl"] or 0 for x in kv[1])):
        tot = sum(r["pnl"] or 0 for r in rs)
        A(f"| {reason} | {len(rs)} | {tot:+.2f} | {tot/len(rs):+.2f} |")
    A("")

    # 按日
    A("## 每日損益\n")
    A("| 日期 | 筆數 | 勝/負 | 當日損益 | 累計 |")
    A("|------|------|-------|----------|------|")
    day_groups = _group(rows, _day_of)
    cum = 0.0
    for day in sorted(day_groups):
        rs = day_groups[day]
        st = _stats(rs)
        cum += st["total_pnl"]
        A(f"| {day} | {st['n']} | {st['wins']}/{st['losses']} | {st['total_pnl']:+.2f} | {cum:+.2f} |")
    A("")

    return "\n".join(lines)


def main():
    args = _parse_args()
    if not Path(args.db).exists():
        print(f"⚠️  找不到 {args.db}")
        return

    all_rows = _load_closed(args.db)
    rows = _filter(all_rows, args)

    if not rows:
        print("⚠️  此範圍內沒有已平倉交易")
        return

    report = build_report(rows, args)

    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = out_dir / f"stats-{today}.md"
    out_path.write_text(report, encoding="utf-8")

    # Console 摘要（純數字，避免中文在 Windows 主控台亂碼）
    s = _stats(rows)
    print("=" * 48)
    print(f"  Trades   : {s['n']} (W{s['wins']} / L{s['losses']} / F{s['flats']})")
    print(f"  Win rate : {s['win_rate_wl']:.1f}% (W/L)  |  {s['win_rate_all']:.1f}% (all)")
    print(f"  Total PnL: {s['total_pnl']:+.2f} USDT")
    print(f"  Avg win  : +{s['avg_win']:.2f}  |  Avg loss: -{s['avg_loss']:.2f}")
    print(f"  Payoff   : {_fmt_inf(s['payoff'])}  |  Profit factor: {_fmt_inf(s['profit_factor'])}")
    print(f"  Expectancy: {s['expectancy']:+.2f} USDT/trade")
    print("=" * 48)
    print(f"  Full report -> {out_path}")


if __name__ == "__main__":
    main()
