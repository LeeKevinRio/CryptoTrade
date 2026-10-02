"""真金上線預檢 —— 任何一項硬性檢查失敗就拒絕啟動交易

測試網上「算出倉位為 0」「槓桿設不進去」「持倉模式不對」都只是少一筆或警告；
真金模式這些會直接變成錯誤部位或無法停損。啟動前逐項確認，結果寫入
state.live_preflight 供 /api/diag 顯示，使用者不必翻主機日誌。

硬性（任一失敗 → 不啟動）：
  ack        LIVE_TRADING_ACK=I_UNDERSTAND
  balance    USDT 餘額 ≥ min_balance
  position_mode  單向持倉（dualSidePosition=false）—— 下單邏輯以 BOTH 為前提
  symbols    至少一個標的的規劃部位 ≥ 交易所最小名目與最小數量
軟性（警告，不阻斷）：
  每個標的的最小名目／數量檢查（不足者會被跳過）、槓桿上限低於設定值
"""

from __future__ import annotations

from src.utils.logger import setup_logger

logger = setup_logger("live_preflight")


def _filters(info: dict) -> dict:
    out = {"min_qty": 0.0, "step": 0.0, "min_notional": 0.0}
    for f in info.get("filters", []):
        t = f.get("filterType")
        if t == "LOT_SIZE":
            out["min_qty"] = float(f.get("minQty", 0) or 0)
            out["step"] = float(f.get("stepSize", 0) or 0)
        elif t == "MIN_NOTIONAL":
            out["min_notional"] = float(f.get("notional", 0) or 0)
    return out


async def run_preflight(api, symbols: list[str], bot_cfg: dict, *, live_ack: bool,
                        min_balance: float = 100.0) -> dict:
    checks: list[dict] = []
    hard_fail = False

    def add(name, ok, detail, hard=True):
        nonlocal hard_fail
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "hard": hard})
        if hard and not ok:
            hard_fail = True

    add("ack", live_ack, "LIVE_TRADING_ACK=I_UNDERSTAND" if live_ack
        else "未設定 LIVE_TRADING_ACK=I_UNDERSTAND（真金必須明確確認）")

    balance = 0.0
    try:
        balance = float(await api.get_usdt_balance())
        add("balance", balance >= min_balance, f"USDT {balance:.2f}（下限 {min_balance:.0f}）")
    except Exception as e:  # noqa: BLE001
        add("balance", False, f"取得餘額失敗: {e}")

    try:
        mode = await api.client.futures_get_position_mode()
        dual = bool(mode.get("dualSidePosition"))
        add("position_mode", not dual,
            "單向持倉（One-way）" if not dual else "目前是雙向持倉（Hedge）—— 請在幣安合約設定改為單向")
    except Exception as e:  # noqa: BLE001
        add("position_mode", False, f"查詢持倉模式失敗: {e}")

    risk = bot_cfg.get("risk", {})
    lev = int(bot_cfg.get("leverage", 1))
    pct = float(risk.get("max_position_pct", 5)) / 100
    # 全倉（強訊號）可下才算可用；半倉（中等強度訊號）不足只警告 —— 那些單會被跳過
    full_notional = balance * pct * lev
    half_notional = full_notional * 0.5
    usable = []

    def _qty(notional, price, f):
        q = notional / price if price else 0.0
        if f["step"] > 0:
            q = int(q / f["step"] + 1e-9) * f["step"]
        return q

    def _ok(q, price, f):
        return q > 0 and q >= f["min_qty"] and q * price >= f["min_notional"]
    for sym in symbols:
        try:
            info = await api.get_symbol_info(sym)
            if not info:
                add(f"symbol:{sym}", False, "查無合約資訊", hard=False)
                continue
            f = _filters(info)
            price = float(await api.get_ticker_price(sym))
            q_full = _qty(full_notional, price, f)
            q_half = _qty(half_notional, price, f)
            ok_full = _ok(q_full, price, f)
            ok_half = _ok(q_half, price, f)
            if ok_full:
                usable.append(sym)
            detail = (f"全倉 {full_notional:.0f} U → {q_full:g}，半倉 {half_notional:.0f} U → {q_half:g}"
                      f"（最小量 {f['min_qty']:g}、最小名目 {f['min_notional']:g} U）")
            if not ok_full:
                detail += " ← 全倉也不足，此標的會被跳過；請提高餘額或 max_position_pct"
            elif not ok_half:
                detail += " ← 半倉（中等訊號）不足會被跳過，只有強訊號會下單"
            add(f"symbol:{sym}", ok_full, detail, hard=False)
            if ok_full and not ok_half:
                add(f"half:{sym}", False, "半倉不足最小單位（僅警告）", hard=False)
        except Exception as e:  # noqa: BLE001
            add(f"symbol:{sym}", False, f"檢查失敗: {e}", hard=False)
        try:
            max_lev = int(await api.get_max_leverage(sym))
            if max_lev < lev:
                add(f"leverage:{sym}", False, f"上限 {max_lev}x < 設定 {lev}x（會自動退階）", hard=False)
        except Exception:  # noqa: BLE001
            pass
    add("symbols", bool(usable), f"可下單標的 {len(usable)}/{len(symbols)}: {', '.join(usable) or '無'}")

    result = {"ok": not hard_fail, "balance": balance, "checks": checks}
    for c in checks:
        (logger.info if c["ok"] else logger.warning)("預檢 %s: %s", c["name"], c["detail"])
    return result
