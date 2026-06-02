"""一鍵匯出本週交易快照到 reports/ 供遠端 agent 分析

執行：
    python -m scripts.export_weekly

產出：
    reports/snapshot-YYYY-MM-DD.json — 含 trades / daily_stats / 帳戶餘額
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main():
    db_path = Path("cryptotrade.db")
    if not db_path.exists():
        print("⚠️  找不到 cryptotrade.db，可能 bot 還沒跑過")
        return

    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_path = out_dir / f"snapshot-{today}.json"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cur = conn.cursor()

    # ── 所有交易 ──
    cur.execute("""
        SELECT id, bot_id, mode, symbol, side, entry_price, exit_price,
               quantity, pnl, pnl_pct, entry_time, exit_time, status, close_reason, strategy
        FROM trades
        ORDER BY id
    """)
    trades = [dict(r) for r in cur.fetchall()]

    # ── 每日統計 ──
    cur.execute("""
        SELECT bot_id, date, total_trades, winning_trades, total_pnl, max_drawdown
        FROM daily_stats
        ORDER BY date, bot_id
    """)
    daily_stats = [dict(r) for r in cur.fetchall()]

    conn.close()

    # ── 整理摘要 ──
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    open_trades = [t for t in trades if t.get("status") == "OPEN"]
    by_bot = {}
    for t in closed:
        bid = t.get("bot_id") or "unknown"
        by_bot.setdefault(bid, []).append(t)

    summary = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "open_trades": len(open_trades),
        "by_bot": {
            bid: {
                "count": len(rows),
                "wins": sum(1 for r in rows if (r.get("pnl") or 0) > 0),
                "total_pnl": round(sum(r.get("pnl") or 0 for r in rows), 4),
            }
            for bid, rows in by_bot.items()
        },
    }

    payload = {
        "summary": summary,
        "trades": trades,
        "daily_stats": daily_stats,
    }

    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"✅ 已匯出: {snapshot_path}")
    print(f"   交易 {summary['total_trades']} 筆（已平 {summary['closed_trades']}、開 {summary['open_trades']}）")
    for bid, s in summary["by_bot"].items():
        print(f"   [{bid}] {s['count']} 筆 / 勝 {s['wins']} / pnl {s['total_pnl']:+.2f}")

    print()
    print("📤 推到 GitHub:")
    print(f"   git add {snapshot_path}")
    print(f"   git commit -m 'chore: weekly snapshot {today}'")
    print(f"   git push")


if __name__ == "__main__":
    main()
