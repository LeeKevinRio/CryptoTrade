"""外部訊號來源 — TradingView webhook 等

參數掃描已證實：1920 組出場參數中沒有一組能跨標的泛化，
瓶頸在【進場訊號的資訊來源】而非參數。本模組引入新的資訊來源，
讓 TradingView 上的專有指標／Pine Script 策略能驅動交易決策。
"""

from src.external.models import ExternalSignal
from src.external.store import ExternalSignalStore

__all__ = ["ExternalSignal", "ExternalSignalStore"]
