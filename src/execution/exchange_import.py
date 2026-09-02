"""交易所成交紀錄匯入 — 讓幣安成為績效紀錄的唯一真相來源

bot 的 SQLite 只記「這一台 bot 自己執行」的交易；雲端重新部署、
本地/雲端切換、資料庫歸零，都會讓績效統計出現斷層。本模組把幣安帳戶
的成交（fills）拉回來，依「部位由 0 建立 → 回到 0」重組成一筆筆已平倉
交易寫入 trades 表，並以 exchange_ref 去重，可重複執行、可在啟動時自動回補。

    python -m scripts.import_exchange_trades --days 30
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.utils.logger import setup_logger
from src.utils.models import TradeRecord

logger = setup_logger("exchange_import")

IMPORT_REASON = "交易所匯入"
_EPS = 1e-9


def _ts(ms: int) -> datetime:
    """Binance 毫秒時間戳 → naive UTC（與 tracker 既有寫法一致）"""
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).replace(tzinfo=None)


def group_fills_into_trades(fills: list[dict]) -> list[dict]:
    """把單一交易對的成交（時間升冪）重組為已平倉交易。

    規則：部位數量（帶號，多為正）從 0 出發，同向成交視為加倉，
    反向成交視為減倉；減至 0 即一筆完整交易。反向成交量超過現有部位
    （翻倉）時，多出的部分作為下一筆交易的開倉。
    回傳每筆 dict：symbol/side/entry_price/exit_price/quantity/pnl/pnl_pct/
    entry_time/exit_time/exchange_ref/commission
    """
    trades: list[dict] = []
    pos = 0.0            # 帶號部位
    entry_cost = 0.0     # Σ price×qty（開倉側）
    entry_qty = 0.0
    exit_cost = 0.0
    exit_qty = 0.0
    realized = 0.0
    commission = 0.0
    entry_time_ms = None
    symbol = None

    def _reset():
        nonlocal pos, entry_cost, entry_qty, exit_cost, exit_qty, realized, commission, entry_time_ms
        pos = 0.0
        entry_cost = entry_qty = exit_cost = exit_qty = realized = commission = 0.0
        entry_time_ms = None

    for f in fills:
        symbol = f["symbol"]
        qty = float(f["qty"])
        price = float(f["price"])
        signed = qty if f["side"] == "BUY" else -qty
        pnl = float(f.get("realizedPnl", 0) or 0)
        fee = float(f.get("commission", 0) or 0)
        t = int(f["time"])

        remaining = signed
        while abs(remaining) > _EPS:
            if abs(pos) < _EPS:
                # 開新倉
                _reset()
                entry_time_ms = t
                pos = remaining
                entry_cost += price * abs(remaining)
                entry_qty += abs(remaining)
                commission += fee
                remaining = 0.0
            elif (pos > 0) == (remaining > 0):
                # 同向加倉
                pos += remaining
                entry_cost += price * abs(remaining)
                entry_qty += abs(remaining)
                commission += fee
                remaining = 0.0
            else:
                # 反向 → 減倉（可能翻倉）
                close_amt = min(abs(remaining), abs(pos))
                frac = close_amt / abs(remaining)
                exit_cost += price * close_amt
                exit_qty += close_amt
                # realizedPnl 全屬平倉部分（翻倉時開倉部分無已實現損益）；手續費按量分攤
                realized += pnl
                pnl = 0.0
                commission += fee * frac
                fee -= fee * frac
                pos += close_amt if pos < 0 else -close_amt
                remaining += close_amt if remaining < 0 else -close_amt
                if abs(pos) < _EPS:
                    entry_avg = entry_cost / entry_qty if entry_qty else price
                    exit_avg = exit_cost / exit_qty if exit_qty else price
                    trade_side = "LONG" if signed < 0 else "SHORT"  # 平倉方向反推
                    pnl_pct = (realized / (entry_avg * entry_qty) * 100) if entry_qty else 0.0
                    trades.append({
                        "symbol": symbol,
                        "side": trade_side,
                        "entry_price": round(entry_avg, 8),
                        "exit_price": round(exit_avg, 8),
                        "quantity": round(entry_qty, 8),
                        "pnl": round(realized, 6),
                        "pnl_pct": round(pnl_pct, 4),
                        "commission": round(commission, 6),
                        "entry_time": _ts(entry_time_ms),
                        "exit_time": _ts(t),
                        "exchange_ref": f"{symbol}:{f['id']}",
                    })
                    _reset()
    return trades


def _already_recorded(session: Session, trade: dict, tolerance_s: int = 120) -> bool:
    """去重：同 exchange_ref 已匯入，或 bot 自己記的同標的同方向、
    平倉時間相近（±tolerance_s）的交易已存在。"""
    if session.query(TradeRecord.id).filter_by(exchange_ref=trade["exchange_ref"]).first():
        return True
    from datetime import timedelta
    lo = trade["exit_time"] - timedelta(seconds=tolerance_s)
    hi = trade["exit_time"] + timedelta(seconds=tolerance_s)
    dup = (
        session.query(TradeRecord.id)
        .filter(
            TradeRecord.symbol == trade["symbol"],
            TradeRecord.side == trade["side"],
            TradeRecord.status == "CLOSED",
            TradeRecord.exit_time >= lo,
            TradeRecord.exit_time <= hi,
        )
        .first()
    )
    return dup is not None


async def import_trades(api, session_factory, symbols: list[str], days: int,
                        bot_id: str = "futures", mode: str = "futures") -> dict:
    """從交易所拉成交、重組、去重後寫入 DB。回傳統計。"""
    import time as _time
    start_ms = int(_time.time() * 1000) - days * 86_400_000
    stats = {"symbols": 0, "fills": 0, "trades": 0, "inserted": 0, "skipped": 0, "errors": []}

    for symbol in symbols:
        try:
            fills = await api.get_account_trades(symbol, start_ms)
        except Exception as e:  # noqa: BLE001 — 單一標的失敗不阻斷其餘
            stats["errors"].append(f"{symbol}: {e}")
            logger.warning("匯入 %s 成交失敗: %s", symbol, e)
            continue
        stats["symbols"] += 1
        stats["fills"] += len(fills)
        if not fills:
            continue
        trades = group_fills_into_trades(fills)
        stats["trades"] += len(trades)

        session: Session = session_factory()
        try:
            for tr in trades:
                if _already_recorded(session, tr):
                    stats["skipped"] += 1
                    continue
                session.add(TradeRecord(
                    bot_id=bot_id, mode=mode,
                    symbol=tr["symbol"], side=tr["side"],
                    entry_price=tr["entry_price"], exit_price=tr["exit_price"],
                    quantity=tr["quantity"], pnl=tr["pnl"], pnl_pct=tr["pnl_pct"],
                    entry_time=tr["entry_time"], exit_time=tr["exit_time"],
                    strategy="exchange_import", status="CLOSED",
                    close_reason=IMPORT_REASON, exchange_ref=tr["exchange_ref"],
                ))
                stats["inserted"] += 1
            session.commit()
        finally:
            session.close()

    logger.info(
        "交易所匯入完成：%d 標的 / %d 成交 → %d 筆交易，新增 %d、略過 %d",
        stats["symbols"], stats["fills"], stats["trades"], stats["inserted"], stats["skipped"],
    )
    return stats
