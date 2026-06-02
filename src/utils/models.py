"""資料模型與資料庫定義"""

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def _utc_now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeRecord(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_id = Column(String, nullable=True, index=True)        # spot / futures
    mode = Column(String, nullable=True)                       # spot / futures
    symbol = Column(String, nullable=False, index=True)
    side = Column(String, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=False)
    pnl = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    entry_time = Column(DateTime, default=_utc_now)
    exit_time = Column(DateTime, nullable=True)
    strategy = Column(String, nullable=True)
    status = Column(String, default="OPEN")
    close_reason = Column(String, nullable=True)


class DailyStats(Base):
    __tablename__ = "daily_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_id = Column(String, nullable=True, index=True)
    date = Column(String, nullable=False, index=True)
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    total_pnl = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)


# 簡易遷移：對 SQLite 補缺欄位（避免每次升級都要砍 DB）
_NEW_TRADE_COLUMNS = [
    ("bot_id", "VARCHAR"),
    ("mode", "VARCHAR"),
    ("close_reason", "VARCHAR"),
]
_NEW_STATS_COLUMNS = [("bot_id", "VARCHAR")]


def _migrate_sqlite(engine):
    insp = inspect(engine)
    if "trades" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("trades")}
        for col, dtype in _NEW_TRADE_COLUMNS:
            if col not in cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE trades ADD COLUMN {col} {dtype}"))
    if "daily_stats" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("daily_stats")}
        for col, dtype in _NEW_STATS_COLUMNS:
            if col not in cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE daily_stats ADD COLUMN {col} {dtype}"))


def init_db(db_url: str = "sqlite:///cryptotrade.db"):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    if db_url.startswith("sqlite"):
        _migrate_sqlite(engine)
    return sessionmaker(bind=engine)
