"""共用績效計算 — CLI 報表（scripts/report_stats）與 Web Dashboard 共用同一套算法

輸入：一組已平倉交易 dict（需含 pnl / symbol / side / close_reason / entry_time / exit_time）
輸出：核心指標 + 各種分組，給上層格式化成 markdown 或 JSON。
"""

from collections import defaultdict


def compute_stats(rows: list[dict]) -> dict:
    """從一組已平倉交易算核心績效指標"""
    pnls = [r.get("pnl") or 0.0 for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    flats = [p for p in pnls if p == 0]

    n = len(rows)
    total_pnl = sum(pnls)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    win_rate_all = (len(wins) / n * 100) if n else 0.0
    decisive = len(wins) + len(losses)
    win_rate_wl = (len(wins) / decisive * 100) if decisive else 0.0

    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    payoff = (avg_win / avg_loss) if avg_loss else (float("inf") if avg_win else 0.0)
    profit_factor = (gross_profit / gross_loss) if gross_loss else (float("inf") if gross_profit else 0.0)
    expectancy = (total_pnl / n) if n else 0.0

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "total_pnl": round(total_pnl, 2),
        "win_rate_all": round(win_rate_all, 1),
        "win_rate_wl": round(win_rate_wl, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff": payoff,
        "profit_factor": profit_factor,
        "expectancy": round(expectancy, 2),
        "best": round(max(pnls), 2) if pnls else 0.0,
        "worst": round(min(pnls), 2) if pnls else 0.0,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
    }


def reason_bucket(reason: str | None) -> str:
    """把冗長的 close_reason 收斂成幾個大類"""
    r = reason or "未知"
    # 「時間」必須最先判斷：出場字串如「時間停損(無波動)」「時間停利」同時含有
    # 停損/停利關鍵字，若順序在後會被誤歸類，導致時間停損在所有報表中隱形。
    if "時間" in r or "time" in r.lower():
        return "時間止損 (Time Stop)"
    if "停利" in r or "L1" in r or "L2" in r or "L3" in r:
        return "停利 (Take Profit)"
    if "停損" in r or "stop" in r.lower():
        return "停損 (Stop Loss)"
    if "追蹤" in r or "trailing" in r.lower() or "回撤" in r:
        return "移動停利 (Trailing)"
    if "手動" in r or "Web" in r:
        return "手動平倉 (Manual)"
    if "對帳" in r or "啟動" in r:
        return "對帳/啟動同步"
    return "其他"


def _group(rows, key_fn):
    g = defaultdict(list)
    for r in rows:
        g[key_fn(r)].append(r)
    return g


def _day_of(r) -> str:
    raw = r.get("exit_time") or r.get("entry_time") or ""
    return raw[:10] if raw else "?"


def diagnose(s: dict) -> list[str]:
    """從核心指標產生白話診斷句"""
    out = []
    inf = float("inf")
    if s["win_rate_wl"] >= 50 and s["total_pnl"] < 0:
        payoff_str = "∞" if s["payoff"] == inf else f"{s['payoff']:.2f}"
        out.append(
            f"勝率過半（{s['win_rate_wl']:.1f}%）卻虧錢 → 問題在盈虧比："
            f"平均賺 +{s['avg_win']:.2f}、平均虧 -{s['avg_loss']:.2f}，payoff 僅 {payoff_str}。贏太小、虧太大。"
        )
    if s["profit_factor"] != inf and s["profit_factor"] < 1 and s["n"] > 0:
        out.append(f"獲利因子 {s['profit_factor']:.2f} < 1：每虧 1 元只賺回 {s['profit_factor']:.2f} 元，長期為負期望。")
    if s["expectancy"] < 0:
        out.append(f"期望值為負（{s['expectancy']:+.2f}/筆）：以目前策略，交易越多虧越多。")
    if not out and s["n"] > 0:
        out.append("目前指標健康（期望值為正且 PF>1）。")
    return out


def full_breakdown(rows: list[dict]) -> dict:
    """完整績效拆解：總體 + 按 bot / symbol / 出場原因 / 每日。
    回傳純 JSON-safe 結構（inf 轉成 None）。
    """
    def _safe(s):
        s = dict(s)
        for k in ("payoff", "profit_factor"):
            if s[k] == float("inf"):
                s[k] = None
            elif isinstance(s[k], float):
                s[k] = round(s[k], 2)
        return s

    overall = compute_stats(rows)

    by_bot = []
    for bid, rs in sorted(_group(rows, lambda r: r.get("bot_id") or "未標記").items()):
        st = _safe(compute_stats(rs))
        st["key"] = bid
        by_bot.append(st)

    by_symbol = []
    for sym, rs in sorted(_group(rows, lambda r: r["symbol"]).items()):
        st = _safe(compute_stats(rs))
        st["key"] = sym
        by_symbol.append(st)

    by_reason = []
    rg = _group(rows, lambda r: reason_bucket(r.get("close_reason")))
    for reason, rs in sorted(rg.items(), key=lambda kv: sum(x.get("pnl") or 0 for x in kv[1])):
        tot = sum(r.get("pnl") or 0 for r in rs)
        by_reason.append({
            "key": reason,
            "n": len(rs),
            "total_pnl": round(tot, 2),
            "avg": round(tot / len(rs), 2) if rs else 0.0,
        })

    by_day = []
    dg = _group(rows, _day_of)
    cum = 0.0
    for day in sorted(dg):
        st = compute_stats(dg[day])
        cum += st["total_pnl"]
        by_day.append({
            "key": day,
            "n": st["n"],
            "wins": st["wins"],
            "losses": st["losses"],
            "total_pnl": st["total_pnl"],
            "cumulative": round(cum, 2),
        })

    time_range = None
    if rows:
        t0 = min(r.get("entry_time") or "" for r in rows)[:19]
        t1 = max((r.get("exit_time") or r.get("entry_time") or "") for r in rows)[:19]
        time_range = {"start": t0, "end": t1}

    return {
        "overall": _safe(overall),
        "diagnosis": diagnose(overall),
        "by_bot": by_bot,
        "by_symbol": by_symbol,
        "by_reason": by_reason,
        "by_day": by_day,
        "time_range": time_range,
    }
