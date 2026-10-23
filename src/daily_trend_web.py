"""日線均線機器人的儀表板 —— 一頁看懂：現在持有什麼、離均線多遠、下次何時判斷、歷史動作

舊儀表板屬於 src.main（5m 抄底機器人），新機器人是獨立程式，所以另給一個精簡頁面：
    http://127.0.0.1:8899/            頁面（每 60 秒自動重新整理）
    http://127.0.0.1:8899/api/status  JSON
DASHBOARD_AUTH=true 時需帶 ?token=WEB_AUTH_TOKEN（或 header x-auth-token），與舊儀表板相同。
"""

import csv
import html
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

TW = timezone(timedelta(hours=8))
STATUS: dict = {"rows": [], "updated_at": None}


def tw(ts: str | datetime | None) -> str:
    """UTC 時間 → 台灣時間字串"""
    if not ts:
        return "-"
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(TW).strftime("%m/%d %H:%M")


async def refresh(api, cfg: dict, last: dict[str, dict]) -> dict:
    """抓即時餘額、持倉、價格，更新 STATUS（last：每個標的最近一次每日判斷的紀錄）"""
    positions = {p["symbol"]: p for p in await api.get_open_positions()}
    try:
        balance = await api.get_usdt_balance()
    except Exception:  # noqa: BLE001
        balance = None
    rows = []
    for sym in cfg["symbols"]:
        rec = last.get(sym, {})
        try:
            price = await api.get_ticker_price(sym)
        except Exception:  # noqa: BLE001
            price = None
        sma = rec.get("sma")
        pos = positions.get(sym)
        row = {
            "symbol": sym, "price": price, "sma": sma,
            "dist_pct": (price / sma - 1) * 100 if price and sma else None,
            "state": "持有" if pos and pos["side"] == "LONG" else "空手",
            "last_action": rec.get("action"), "last_ts": rec.get("ts"), "note": rec.get("note", ""),
        }
        if pos:
            cost = pos["entry_price"] * pos["quantity"]
            row.update(qty=pos["quantity"], entry=pos["entry_price"], upnl=pos["unrealized_pnl"],
                       upnl_pct=pos["unrealized_pnl"] / cost * 100 if cost else None,
                       foreign=pos["side"] != "LONG" or int(pos.get("leverage", 1)) != int(cfg["leverage"]))
        rows.append(row)
    STATUS.update(rows=rows, balance=balance, updated_at=datetime.now(timezone.utc).isoformat())
    return STATUS


def read_history(path: Path, limit: int = 30) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:][::-1]


ACTION_TXT = {"buy": "買進", "sell": "賣出", "hold": "續抱", "flat": "空手", "skip": "略過", "error": "錯誤"}


def _fmt(x, nd=2, suffix=""):
    if x is None or x == "":
        return "-"
    try:
        return f"{float(x):,.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return html.escape(str(x))


def render(status: dict, history: list[dict]) -> str:
    e = html.escape
    head = (f"{e(status.get('mode', ''))}{'・只看不下單' if status.get('dry_run') else ''}｜"
            f"均線 {status.get('sma_days', 50)} 天｜投入 {_fmt(status.get('capital'), 0)} U｜"
            f"{status.get('leverage', 1)} 倍｜餘額 {_fmt(status.get('balance'))} U")
    cards = []
    for r in status.get("rows", []):
        dist = r.get("dist_pct")
        color = "#16a34a" if (dist or 0) > 0 else "#dc2626"
        pos = ""
        if r.get("qty"):
            pc = "#16a34a" if (r.get("upnl") or 0) >= 0 else "#dc2626"
            pos = (f"<div>持倉 {_fmt(r['qty'], 4)}｜進場 {_fmt(r.get('entry'))}｜"
                   f"<b style='color:{pc}'>{_fmt(r.get('upnl'))} U（{_fmt(r.get('upnl_pct'), 1, '%')}）</b></div>")
            if r.get("foreign"):
                pos += "<div style='color:#dc2626'>⚠️ 這張不是本策略的倉位（槓桿或方向不符），機器人不會動它</div>"
        cards.append(f"""
        <div class="card">
          <div class="sym">{e(r['symbol'])} <span class="tag {'on' if r['state'] == '持有' else 'off'}">{r['state']}</span></div>
          <div>現價 {_fmt(r.get('price'))}｜50 日均線 {_fmt(r.get('sma'))}</div>
          <div>離均線 <b style="color:{color}">{_fmt(dist, 2, '%')}</b>
               {'（站上 → 收盤若維持就持有）' if (dist or 0) > 0 else '（在均線下 → 收盤站回才會買）'}</div>
          {pos}
          <div class="muted">最近判斷：{ACTION_TXT.get(r.get('last_action'), '-')} @ {tw(r.get('last_ts'))}
               {e(r.get('note') or '')}</div>
        </div>""")
    hist = "".join(
        f"<tr><td>{tw(h.get('ts'))}</td><td>{e(h.get('symbol', ''))}</td>"
        f"<td>{ACTION_TXT.get(h.get('action'), e(h.get('action', '')))}{'（模擬）' if h.get('dry_run') == 'True' else ''}</td>"
        f"<td>{_fmt(h.get('close'))}</td><td>{_fmt(h.get('sma'))}</td><td>{_fmt(h.get('qty'), 4)}</td>"
        f"<td>{e(h.get('note', ''))}</td></tr>"
        for h in history) or "<tr><td colspan=7 class='muted'>尚無紀錄</td></tr>"
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="60">
<title>日線均線機器人</title>
<style>
:root{{--bg:#f8fafc;--fg:#0f172a;--card:#fff;--muted:#64748b;--line:#e2e8f0}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0f172a;--fg:#e2e8f0;--card:#1e293b;--muted:#94a3b8;--line:#334155}}}}
body{{margin:0;padding:16px;font-family:system-ui,-apple-system,"Noto Sans TC",sans-serif;background:var(--bg);color:var(--fg)}}
h1{{font-size:20px;margin:0 0 4px}} .muted{{color:var(--muted);font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin:12px 0}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;line-height:1.8}}
.sym{{font-size:18px;font-weight:700}} .tag{{font-size:12px;padding:2px 8px;border-radius:99px;margin-left:6px}}
.tag.on{{background:#dcfce7;color:#166534}} .tag.off{{background:#e2e8f0;color:#334155}}
table{{width:100%;border-collapse:collapse;background:var(--card);font-size:13px}}
td,th{{border-bottom:1px solid var(--line);padding:6px;text-align:left;white-space:nowrap}}
.wrap{{overflow-x:auto}}
</style></head><body>
<h1>日線 50 均線機器人</h1>
<div class="muted">{head}</div>
<div class="muted">下次判斷：{tw(status.get('next_run'))}（台灣時間）｜頁面更新：{tw(status.get('updated_at'))}・每 60 秒自動重新整理</div>
<div class="grid">{''.join(cards) or '<div class="card muted">啟動中…</div>'}</div>
<h2 style="font-size:16px">歷史判斷</h2>
<div class="wrap"><table><tr><th>時間</th><th>幣</th><th>動作</th><th>收盤</th><th>均線</th><th>數量</th><th>備註</th></tr>{hist}</table></div>
</body></html>"""


def create_app(history_path: Path) -> FastAPI:
    app = FastAPI(title="日線均線機器人")

    def check(request: Request):
        if os.environ.get("DASHBOARD_AUTH", "").lower() != "true":
            return
        expected = os.environ.get("WEB_AUTH_TOKEN", "")
        got = request.query_params.get("token") or request.headers.get("x-auth-token")
        if not expected or got != expected:
            raise HTTPException(401, "需要 token")

    @app.get("/", response_class=HTMLResponse)
    async def page(request: Request):
        check(request)
        return render(STATUS, read_history(history_path))

    @app.get("/api/status")
    async def api_status(request: Request):
        check(request)
        return {**STATUS, "history": read_history(history_path)}

    return app
