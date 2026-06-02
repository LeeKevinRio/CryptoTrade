"""訂單追蹤 — 記錄所有交易到資料庫（分 bot）"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.utils.models import TradeRecord, DailyStats
from src.utils.logger import setup_logger

logger = setup_logger("order_tracker")


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_now():
    return datetime.now(timezone.utc)


class OrderTracker:
    """追蹤並記錄所有交易，每筆 trade 用 id 精確對應"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def record_open(self, trade_data: dict) -> int:
        """開倉記錄；返回 trade_id 供 caller 保存以便精確平倉"""
        session: Session = self.session_factory()
        try:
            record = TradeRecord(
                bot_id=trade_data.get("bot_id"),
                mode=trade_data.get("mode"),
                symbol=trade_data["symbol"],
                side=trade_data["side"],
                entry_price=trade_data["entry_price"],
                quantity=trade_data["quantity"],
                strategy=trade_data.get("strategy", "auto"),
                entry_time=_utc_now(),
                status="OPEN",
            )
            session.add(record)
            session.commit()
            logger.info(
                "[%s] 記錄開倉: id=%d %s",
                trade_data.get("bot_id", "?"), record.id, trade_data["symbol"],
            )
            return record.id
        finally:
            session.close()

    def record_close(
        self,
        trade_id: int | None,
        symbol: str,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        bot_id: str | None = None,
        reason: str | None = None,
    ):
        """平倉記錄

        優先以 trade_id 對應；若 trade_id 未知，回退到 (bot_id, symbol, OPEN) 最新一筆
        """
        session: Session = self.session_factory()
        try:
            record = None
            if trade_id is not None:
                record = session.query(TradeRecord).filter_by(id=trade_id).first()
            if record is None:
                q = session.query(TradeRecord).filter_by(symbol=symbol, status="OPEN")
                if bot_id:
                    q = q.filter_by(bot_id=bot_id)
                record = q.order_by(TradeRecord.id.desc()).first()

            if record:
                record.exit_price = exit_price
                record.pnl = pnl
                record.pnl_pct = pnl_pct
                record.exit_time = _utc_now()
                record.status = "CLOSED"
                record.close_reason = reason
                session.commit()
                logger.info(
                    "[%s] 記錄平倉: id=%d pnl=%.4f reason=%s",
                    record.bot_id or "?", record.id, pnl, reason,
                )

            # 每日統計（以 bot_id 切分）
            today = _utc_today()
            stats = (
                session.query(DailyStats)
                .filter_by(date=today, bot_id=bot_id)
                .first()
            )
            if not stats:
                stats = DailyStats(
                    date=today, bot_id=bot_id,
                    total_trades=0, winning_trades=0, total_pnl=0.0,
                )
                session.add(stats)
            stats.total_trades = (stats.total_trades or 0) + 1
            if pnl > 0:
                stats.winning_trades = (stats.winning_trades or 0) + 1
            stats.total_pnl = (stats.total_pnl or 0.0) + pnl
            session.commit()
        finally:
            session.close()

    def get_today_stats(self, bot_id: str | None = None) -> dict:
        session: Session = self.session_factory()
        try:
            today = _utc_today()
            q = session.query(DailyStats).filter_by(date=today)
            if bot_id:
                q = q.filter_by(bot_id=bot_id)
                stats = q.first()
                if not stats:
                    return {"trades": 0, "wins": 0, "pnl": 0.0, "win_rate": 0.0}
                trades = stats.total_trades or 0
                wins = stats.winning_trades or 0
                pnl = stats.total_pnl or 0.0
            else:
                # 跨 bot 彙總
                rows = q.all()
                trades = sum(r.total_trades or 0 for r in rows)
                wins = sum(r.winning_trades or 0 for r in rows)
                pnl = sum(r.total_pnl or 0.0 for r in rows)

            win_rate = (wins / trades * 100) if trades else 0
            return {
                "trades": trades,
                "wins": wins,
                "pnl": round(pnl, 4),
                "win_rate": round(win_rate, 1),
            }
        finally:
            session.close()

    def get_recent_trades(self, bot_id: str | None = None, limit: int = 10) -> list[dict]:
        """最近 N 筆已平倉交易（依平倉時間新到舊）"""
        session: Session = self.session_factory()
        try:
            q = session.query(TradeRecord).filter_by(status="CLOSED")
            if bot_id:
                q = q.filter_by(bot_id=bot_id)
            records = (
                q.order_by(TradeRecord.exit_time.desc(), TradeRecord.id.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "bot_id": r.bot_id,
                    "symbol": r.symbol,
                    "side": r.side,
                    "entry_price": r.entry_price,
                    "exit_price": r.exit_price,
                    "pnl": round(r.pnl, 4) if r.pnl is not None else None,
                    "pnl_pct": round(r.pnl_pct, 2) if r.pnl_pct is not None else None,
                    "exit_time": (
                        r.exit_time.isoformat() + "Z"
                        if r.exit_time and r.exit_time.tzinfo is None
                        else (r.exit_time.isoformat() if r.exit_time else "")
                    ),
                    "close_reason": r.close_reason,
                }
                for r in records
            ]
        finally:
            session.close()

    def get_performance(self, bot_id: str | None = None, days: int | None = None) -> dict:
        """完整績效拆解（勝率 / 盈虧比 / 期望值 / 分組）— 供 Web Dashboard 與 CLI 共用"""
        from datetime import timedelta
        from src.utils.performance import full_breakdown

        session: Session = self.session_factory()
        try:
            q = session.query(TradeRecord).filter(
                TradeRecord.status == "CLOSED", TradeRecord.pnl.isnot(None)
            )
            if bot_id:
                q = q.filter(TradeRecord.bot_id == bot_id)
            if days is not None:
                cutoff = _utc_now() - timedelta(days=days)
                # entry_time 存的是 naive UTC，比較用 naive
                q = q.filter(TradeRecord.entry_time >= cutoff.replace(tzinfo=None))
            records = q.order_by(TradeRecord.entry_time).all()
            rows = [
                {
                    "bot_id": r.bot_id,
                    "symbol": r.symbol,
                    "side": r.side,
                    "pnl": r.pnl,
                    "pnl_pct": r.pnl_pct,
                    "close_reason": r.close_reason,
                    "entry_time": r.entry_time.isoformat() if r.entry_time else "",
                    "exit_time": r.exit_time.isoformat() if r.exit_time else "",
                }
                for r in records
            ]
            return full_breakdown(rows)
        finally:
            session.close()

    def get_open_trades(self, bot_id: str | None = None) -> list[dict]:
        session: Session = self.session_factory()
        try:
            q = session.query(TradeRecord).filter_by(status="OPEN")
            if bot_id:
                q = q.filter_by(bot_id=bot_id)
            records = q.all()
            return [
                {
                    "id": r.id,
                    "bot_id": r.bot_id,
                    "symbol": r.symbol,
                    "side": r.side,
                    "entry_price": r.entry_price,
                    "quantity": r.quantity,
                    "entry_time": r.entry_time.isoformat() + "Z" if r.entry_time and r.entry_time.tzinfo is None else (r.entry_time.isoformat() if r.entry_time else ""),
                }
                for r in records
            ]
        finally:
            session.close()
