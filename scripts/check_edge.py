"""實盤 edge 統計檢定 — 回答「這是真的有優勢，還是運氣？」

用 pnl_pct（價格變動%）而非 USDT：槓桿與部位大小調整過多次，
USDT 金額會把「部位變大」混進「策略變好」，是錯誤的衡量基準。

    python -m scripts.check_edge
    python -m scripts.check_edge --days 30 --bot futures

判準：
    t > 2 且 bootstrap 信賴區間不跨過 0 → 統計上證實有 edge
    在那之前，獲利可能只是隨機波動，不應據此加碼。
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import numpy as np


def fetch(db: str, days: int | None, bot: str | None):
    con = sqlite3.connect(db)
    q = ("SELECT pnl_pct, close_reason, exit_time FROM trades "
         "WHERE status='CLOSED' AND pnl_pct IS NOT NULL")
    args: list = []
    if bot:
        q += " AND bot_id = ?"
        args.append(bot)
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        q += " AND exit_time >= ?"
        args.append(since)
    rows = con.execute(q + " ORDER BY exit_time", args).fetchall()
    con.close()
    return rows


def bucket(reason: str | None) -> str:
    r = reason or "未知"
    if "時間" in r:
        return "時間停損"
    if "停利" in r:
        return "停利"
    if "停損" in r:
        return "停損"
    if "追蹤" in r or "回撤" in r:
        return "移動停利"
    if "對帳" in r or "啟動" in r:
        return "對帳同步"
    return "其他"


def main():
    ap = argparse.ArgumentParser(description="實盤 edge 統計檢定")
    ap.add_argument("--db", default="cryptotrade.db")
    ap.add_argument("--days", type=int, default=None, help="只看最近 N 天（預設全部）")
    ap.add_argument("--bot", default=None)
    ap.add_argument("--boot", type=int, default=20000, help="bootstrap 次數")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    rows = fetch(args.db, args.days, args.bot)
    if len(rows) < 5:
        print(f"樣本太少（{len(rows)} 筆），無法檢定")
        return

    p = np.array([r[0] for r in rows], dtype=float)
    n = len(p)
    wins, losses = p[p > 0], p[p <= 0]
    wr = len(wins) / n
    payoff = abs(wins.mean() / losses.mean()) if len(losses) and len(wins) else float("inf")
    be_wr = 1 / (1 + payoff) if payoff and payoff != float("inf") else 0.0

    mean = p.mean()
    se = p.std(ddof=1) / np.sqrt(n)
    t = mean / se if se else 0.0
    rng = np.random.default_rng(0)
    boot = np.array([rng.choice(p, n, replace=True).mean() for _ in range(args.boot)])
    lo, hi = np.percentile(boot, [2.5, 97.5])
    p_pos = (boot > 0).mean()

    scope = f"最近 {args.days} 天" if args.days else "全部"
    print("=" * 62)
    print(f"  實盤 edge 檢定（{scope}，以 pnl_pct 衡量）")
    print("=" * 62)
    print(f"  樣本數       {n} 筆")
    print(f"  勝率         {wr*100:.1f}%   （打平需 {be_wr*100:.1f}%，"
          f"邊際 {(wr-be_wr)*100:+.1f} 個百分點）")
    print(f"  payoff       {payoff:.2f}")
    print(f"  期望值       {mean:+.4f}% / 筆")
    print(f"  累計         {p.sum():+.2f}%")
    print("-" * 62)
    print(f"  bootstrap CI [{lo:+.4f}%, {hi:+.4f}%]")
    print(f"  t 統計量     {t:.2f}")
    print(f"  P(期望>0)    {p_pos*100:.1f}%")
    print("-" * 62)

    proven = t > 2 and lo > 0
    if proven:
        print("  ✅ 統計上證實有 edge（t>2 且信賴區間不跨 0）")
    else:
        need = int((1.96 * p.std(ddof=1) / mean) ** 2) if mean > 0 else None
        print("  ⏳ 尚未達統計顯著 —— 目前獲利無法排除是隨機波動")
        if need:
            print(f"     以現有變異度估算，約需 {need} 筆（還差 {max(0, need-n)} 筆）")
        else:
            print("     期望值為負，加大部位只會加速虧損")
        print("     在此之前不應因短期獲利而加碼")

    # 出場原因分布：實盤行為是否與回測假設一致
    print("-" * 62)
    print("  出場原因分布（與回測假設比對）")
    from collections import Counter
    c = Counter(bucket(r[1]) for r in rows)
    for k, v in c.most_common():
        sub = np.array([r[0] for r in rows if bucket(r[1]) == k])
        print(f"    {k:<10}{v:>4} 筆 ({v/n*100:4.0f}%)  平均 {sub.mean():+.3f}%")
    print("=" * 62)


if __name__ == "__main__":
    main()
