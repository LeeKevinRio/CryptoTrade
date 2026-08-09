"""回測保真度工具 — 讓回測的訊號評分與實盤一致

先前回測與實盤有三處系統性落差，導致回測結論無法外推到實盤：

1. 假多時框：把同一份 5m 資料同時當成 5m/15m/1h 餵給策略，
   「15m RSI 確認」「1h 錘子線」等條件實際上跑在錯誤的資料上。
   → resample_timeframes() 由 5m 正確重取樣出 15m/1h/4h。

2. 情緒缺席：回測不餵 sentiment，權重 0.20 永遠拿不到分，
   訊號分數系統性低於實盤，門檻行為不一致。
   → SentimentReplay 以歷史 Fear & Greed 逐日回放。

3. 時間停損未模擬：實盤 78% 的出場是 40 分鐘時間停損，
   回測卻完全沒有這個機制（見 param_sweep.fast_backtest）。
   → 由 param_sweep 側實作，本模組提供參數。

註：新聞情緒無公開歷史檔可回放，回放僅涵蓋 F&G（權重 0.6），
    因此回放後仍略低於實盤，但已消除大部分落差。
"""

import asyncio

import pandas as pd

from src.sentiment.fear_greed import fetch_fear_greed_history
from src.sentiment.models import SentimentScore, fear_greed_to_bias
from src.utils.logger import setup_logger

logger = setup_logger("fidelity")

# 由 5m 聚合出其他時框的規則
AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def resample_timeframes(df5m: pd.DataFrame,
                        targets=("15m", "1h", "4h")) -> dict[str, pd.DataFrame]:
    """由 5m K 線正確重取樣出其他時框（與實盤 candle_manager 的資料一致）。"""
    out: dict[str, pd.DataFrame] = {"5m": df5m}
    for tf in targets:
        rule = tf.replace("m", "min").replace("h", "h")
        # K 線 index 為「開盤時間」。closed="left" 讓桶涵蓋 [T-period, T)，
        # label="right" 使標籤 T 等於該桶的收盤時刻 —— 標籤 <= 當下時間者
        # 即為「已完整收盤」的 K 線，切片時不會取到未來資料。
        r = df5m.resample(rule, label="right", closed="left").agg(AGG).dropna()
        out[tf] = r
    return out


class TimeframeSlicer:
    """在指定時間點，取出各時框「當下已收盤」的 K 線切片。

    避免前視偏誤：只回傳 index <= 當前時間的 K 線。
    以 searchsorted 定位，避免每根都做布林遮罩掃描。
    """

    def __init__(self, frames: dict[str, pd.DataFrame], lookback: int = 200):
        self.frames = frames
        self.lookback = lookback
        # 保留 DatetimeIndex（而非 .values）：numpy datetime64 陣列無法直接與
        # pd.Timestamp 比較，searchsorted 會 TypeError
        self._idx = {tf: f.index for tf, f in frames.items()}

    def at(self, ts) -> dict[str, pd.DataFrame]:
        out = {}
        for tf, f in self.frames.items():
            pos = self._idx[tf].searchsorted(ts, side="right")
            if pos <= 0:
                continue
            out[tf] = f.iloc[max(0, pos - self.lookback):pos]
        return out


class SentimentReplay:
    """以歷史 Fear & Greed 逐日回放情緒（新聞無公開歷史，略過）。"""

    def __init__(self, fg_by_day: dict[str, int], fg_weight: float = 0.6):
        self.fg_by_day = fg_by_day
        self.fg_weight = fg_weight
        self._cache: dict[str, SentimentScore] = {}

    @classmethod
    def load(cls, days: int = 180, fg_weight: float = 0.6) -> "SentimentReplay":
        fg = asyncio.run(fetch_fear_greed_history(days=days))
        if fg:
            logger.info("情緒回放：載入 %d 天歷史 F&G", len(fg))
        else:
            logger.warning("情緒回放：無歷史 F&G，回測將不含情緒（同舊行為）")
        return cls(fg, fg_weight)

    @property
    def available(self) -> bool:
        return bool(self.fg_by_day)

    def at(self, ts, symbol: str) -> SentimentScore | None:
        day = pd.Timestamp(ts).strftime("%Y-%m-%d")
        fg = self.fg_by_day.get(day)
        if fg is None:
            return None
        key = f"{day}|{symbol}"
        hit = self._cache.get(key)
        if hit is None:
            # 僅 F&G 一個來源時，合成 bias 就是它自己的反向映射
            hit = SentimentScore(
                symbol=symbol,
                bias=round(fear_greed_to_bias(fg), 3),
                fear_greed=fg,
                fear_greed_label="",
                sources=["fear_greed(history)"],
            )
            self._cache[key] = hit
        return hit
