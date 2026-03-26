"""K線管理與快取"""

import pandas as pd
import numpy as np

from src.utils.logger import setup_logger

logger = setup_logger("candle_manager")

KLINE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


class CandleManager:
    """管理多交易對、多時間框架的 K 線資料"""

    def __init__(self, max_candles: int = 500):
        self.max_candles = max_candles
        # {(symbol, interval): DataFrame}
        self._candles: dict[tuple[str, str], pd.DataFrame] = {}

    def init_from_klines(self, symbol: str, interval: str, raw_klines: list[list]):
        rows = []
        for k in raw_klines:
            rows.append({
                "timestamp": pd.to_datetime(k[0], unit="ms"),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        df = pd.DataFrame(rows, columns=KLINE_COLUMNS)
        df.set_index("timestamp", inplace=True)
        self._candles[(symbol, interval)] = df
        logger.info("初始化 %s %s K線: %d 根", symbol, interval, len(df))

    def update_candle(self, symbol: str, interval: str, data: dict):
        key = (symbol, interval)
        ts = pd.to_datetime(data["timestamp"], unit="ms")
        row = {
            "open": data["open"],
            "high": data["high"],
            "low": data["low"],
            "close": data["close"],
            "volume": data["volume"],
        }

        if key not in self._candles:
            df = pd.DataFrame([row], index=[ts])
            df.index.name = "timestamp"
            self._candles[key] = df
            return

        df = self._candles[key]

        if ts in df.index:
            for col, val in row.items():
                df.at[ts, col] = val
        else:
            new_row = pd.DataFrame([row], index=[ts])
            new_row.index.name = "timestamp"
            df = pd.concat([df, new_row])
            if len(df) > self.max_candles:
                df = df.iloc[-self.max_candles:]
            self._candles[key] = df

    def get_candles(self, symbol: str, interval: str) -> pd.DataFrame | None:
        return self._candles.get((symbol, interval))

    def get_latest_price(self, symbol: str, interval: str = "1m") -> float | None:
        df = self.get_candles(symbol, interval)
        if df is not None and not df.empty:
            return float(df["close"].iloc[-1])
        return None

    def get_volume_sma(self, symbol: str, interval: str, period: int = 20) -> float | None:
        df = self.get_candles(symbol, interval)
        if df is not None and len(df) >= period:
            return float(df["volume"].rolling(period).mean().iloc[-1])
        return None
