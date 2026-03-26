"""訂單追蹤 — 記錄所有交易到資料庫"""

from datetime import datetime

from sqlalchemy.orm import Session

from src.utils.models import TradeRecord, DailyStats
from src.utils.logger import setup_logger

logger = setup_logger("order_tracker")


class OrderTracker:
    """追蹤並記錄所有交易"""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def record_open(self, trade_data: dict) -> int:
        session: Session = self.session_factory()
        try:
            record = TradeRecord(
                symbol=trade_data["symbol"],
                side=trade_data["side"],
                entry_price=trade_data["entry_price"],
                quantity=trade_data["quantity"],
                strategy=trade_data.get("strategy", "auto"),
                status="OPEN",
            )
            session.add(record)
            session.commit()
            logger.info("記錄開倉: id=%d %s", record.id, trade_data["symbol"])
            return record.id
        finally:
            session.close()

    def record_close(self, symbol: str, exit_price: float, pnl: float, pnl_pct: float):
        session: Session = self.session_factory()
        try:
            record = (
                session.query(TradeRecord)
                .filter_by(symbol=symbol, status="OPEN")
                .order_by(TradeRecord.id.desc())
                .first()
            )
            if record:
                record.exit_price = exit_price
                record.pnl = pnl
                record.pnl_pct = pnl_pct
                record.exit_time = datetime.utcnow()
                record.status = "CLOSED"
                session.commit()
                logger.info("記錄平倉: id=%d pnl=%.4f", record.id, pnl)

            # 更新每日統計
            today = datetime.utcnow().strftime("%Y-%m-%d")
            stats = session.query(DailyStats).filter_by(date=today).first()
            if not stats:
                stats = DailyStats(date=today)
                session.add(stats)
            stats.total_trades += 1
            if pnl > 0:
                stats.winning_trades += 1
            stats.total_pnl += pnl
            session.commit()
        finally:
            session.close()

    def get_today_stats(self) -> dict:
        session: Session = self.session_factory()
        try:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            stats = session.query(DailyStats).filter_by(date=today).first()
            if not stats:
                return {"trades": 0, "wins": 0, "pnl": 0.0, "win_rate": 0.0}
            win_rate = (stats.winning_trades / stats.total_trades * 100) if stats.total_trades > 0 else 0
            return {
                "trades": stats.total_trades,
                "wins": stats.winning_trades,
                "pnl": stats.total_pnl,
                "win_rate": round(win_rate, 1),
            }
        finally:
            session.close()

    def get_open_trades(self) -> list[dict]:
        session: Session = self.session_factory()
        try:
            records = session.query(TradeRecord).filter_by(status="OPEN").all()
            return [
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "side": r.side,
                    "entry_price": r.entry_price,
                    "quantity": r.quantity,
                    "entry_time": r.entry_time.isoformat() if r.entry_time else "",
                }
                for r in records
            ]
        finally:
            session.close()
