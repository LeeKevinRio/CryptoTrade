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
        # {symbol: 最後一筆 K 線 tick 的收盤價}（不分時框，含未收盤的 K 線）
        self._last_prices: dict[str, float] = {}

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
        if not df.empty:
            self._last_prices.setdefault(symbol, float(df["close"].iloc[-1]))
        logger.info("初始化 %s %s K線: %d 根", symbol, interval, len(df))

    def update_candle(self, symbol: str, interval: str, data: dict):
        key = (symbol, interval)
        ts = pd.to_datetime(data["timestamp"], unit="ms")
        self._last_prices[symbol] = float(data["close"])
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

    @staticmethod
    def _interval_seconds(interval: str) -> int:
        unit = interval[-1]
        n = int(interval[:-1]) if interval[:-1].isdigit() else 0
        return n * {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit, 10**9)

    def get_latest_price(self, symbol: str, interval: str | None = None) -> float | None:
        """該標的最新價。

        優先用最後一筆 WebSocket tick（任何時框、含未收盤 K 線），
        其次退回「訂閱中最短時框」的最後收盤價。
        ⚠️ 不能寫死某個時框：先前預設 1m，但 1m 已從訂閱清單移除後
        這裡就一直回 None，風控迴圈因此整整一個月沒檢查過任何停損／停利。
        """
        if symbol in self._last_prices:
            return self._last_prices[symbol]
        if interval is not None:
            df = self.get_candles(symbol, interval)
            if df is not None and not df.empty:
                return float(df["close"].iloc[-1])
        keys = sorted(
            (k for k in self._candles if k[0] == symbol),
            key=lambda k: self._interval_seconds(k[1]),
        )
        for key in keys:
            df = self._candles[key]
            if df is not None and not df.empty:
                return float(df["close"].iloc[-1])
        return None

    def get_volume_sma(self, symbol: str, interval: str, period: int = 20) -> float | None:
        df = self.get_candles(symbol, interval)
        if df is not None and len(df) >= period:
            return float(df["volume"].rolling(period).mean().iloc[-1])
        return None
