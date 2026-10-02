"""順勢策略 — 趨勢中的回檔／突破進場，結構停損，目標「大賺小賠」

與抄底策略（高勝率、小目標）互補：這裡只在高時框趨勢明確時順勢進場，
停損放在結構低點（被證明看錯才出場），不設固定停利，讓部位隨趨勢跑。
預期形狀：勝率 40~50%、賠率 2 以上。

進場（多；空完全鏡像）：
  regime   4h EMA50 斜率 > slope_threshold_pct 且收盤在 EMA50 上方
  pullback 1h 最近 pullback_lookback 根內低點曾觸及 EMA20（± touch_pct），
           當前 1h 收盤站回 EMA20 上方，1h RSI 在 [rsi_min, rsi_max]（回檔、未過熱），
           5m 收盤突破前 resume_bars 根高點（回檔結束、動能恢復）
  breakout 5m 收盤突破前 breakout_bars 根最高點，且量 > volume_mult × 20 均量
停損：最近 stop_lookback 根 5m 低點下方 stop_buffer_pct，距離夾在 [min_stop_pct, max_stop_pct]
"""

import pandas as pd

from src.indicators.ema import calculate_ema
from src.indicators.rsi import calculate_rsi
from src.strategy.base_strategy import BaseStrategy, Signal, SignalType

DEFAULTS = {
    "entry_type": "pullback",      # pullback | breakout
    "slope_threshold_pct": 0.3,
    "slope_lookback": 6,
    "ema_trend_period": 50,
    "ema_pullback_period": 20,
    "pullback_lookback": 6,
    "touch_pct": 0.2,
    "rsi_min": 40,
    "rsi_max": 65,
    "resume_bars": 6,
    "breakout_bars": 48,
    "volume_mult": 1.2,
    "stop_lookback": 36,
    "stop_buffer_pct": 0.1,
    "min_stop_pct": 0.6,
    "max_stop_pct": 3.0,
    "strength": 70,
}


class TrendFollower(BaseStrategy):
    def __init__(self, config: dict):
        super().__init__(config)
        self.p = {**DEFAULTS, **(config.get("strategy", {}).get("trend_follower", {}) or {})}

    def _regime(self, df4h: pd.DataFrame) -> tuple[int, float]:
        p = self.p
        n = p["ema_trend_period"] + p["slope_lookback"] + 1
        if df4h is None or len(df4h) < n:
            return 0, 0.0
        ema = calculate_ema(df4h, p["ema_trend_period"])
        slope = (float(ema.iloc[-1]) - float(ema.iloc[-1 - p["slope_lookback"]])) \
            / float(ema.iloc[-1 - p["slope_lookback"]]) * 100
        close = float(df4h["close"].iloc[-1])
        if slope >= p["slope_threshold_pct"] and close > float(ema.iloc[-1]):
            return 1, slope
        if slope <= -p["slope_threshold_pct"] and close < float(ema.iloc[-1]):
            return -1, slope
        return 0, slope

    def _stop(self, df5m: pd.DataFrame, price: float, side: int) -> float:
        p = self.p
        win = df5m.iloc[-p["stop_lookback"]:]
        ref = float(win["low"].min()) if side == 1 else float(win["high"].max())
        dist = abs(price - ref) / price * 100 + p["stop_buffer_pct"]
        dist = min(max(dist, p["min_stop_pct"]), p["max_stop_pct"])
        return price * (1 - side * dist / 100)

    def analyze(self, symbol: str, candles: dict[str, pd.DataFrame],
                funding_rate: float = 0.0, sentiment=None, external=None) -> Signal:
        p = self.p
        df5 = candles.get("5m")
        df1h = candles.get("1h")
        df4h = candles.get("4h")
        neutral = Signal(type=SignalType.NEUTRAL, symbol=symbol, strength=0)
        if df5 is None or df1h is None or df4h is None:
            return neutral
        if len(df5) < max(p["stop_lookback"], p["breakout_bars"], p["resume_bars"]) + 2 \
                or len(df1h) < p["ema_pullback_period"] + 16:
            return neutral
        side, slope = self._regime(df4h)
        if side == 0:
            return neutral

        price = float(df5["close"].iloc[-1])
        reasons = [f"4h 趨勢{'向上' if side == 1 else '向下'}（EMA{p['ema_trend_period']} 斜率 {slope:+.2f}%）"]

        if p["entry_type"] == "breakout":
            prev = df5.iloc[-1 - p["breakout_bars"]:-1]
            vol_sma = float(df5["volume"].iloc[-21:-1].mean()) if len(df5) > 21 else 0.0
            vol_ok = vol_sma > 0 and float(df5["volume"].iloc[-1]) >= p["volume_mult"] * vol_sma
            if side == 1:
                ok = price > float(prev["high"].max())
            else:
                ok = price < float(prev["low"].min())
            if not (ok and vol_ok):
                return neutral
            reasons.append(f"5m 突破前 {p['breakout_bars']} 根{'高' if side == 1 else '低'}點，量能 {float(df5['volume'].iloc[-1]) / vol_sma:.1f}×")
        else:
            ema20 = calculate_ema(df1h, p["ema_pullback_period"])
            rsi = calculate_rsi(df1h, 14)
            recent = df1h.iloc[-p["pullback_lookback"]:]
            e_recent = ema20.iloc[-p["pullback_lookback"]:]
            c1h = float(df1h["close"].iloc[-1])
            e_now = float(ema20.iloc[-1])
            r_now = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0
            if side == 1:
                touched = bool((recent["low"].values <= e_recent.values * (1 + p["touch_pct"] / 100)).any())
                holding = c1h > e_now
                resume = price > float(df5["high"].iloc[-1 - p["resume_bars"]:-1].max())
            else:
                touched = bool((recent["high"].values >= e_recent.values * (1 - p["touch_pct"] / 100)).any())
                holding = c1h < e_now
                resume = price < float(df5["low"].iloc[-1 - p["resume_bars"]:-1].min())
            rsi_ok = p["rsi_min"] <= r_now <= p["rsi_max"] if side == 1 \
                else (100 - p["rsi_max"]) <= r_now <= (100 - p["rsi_min"])
            if not (touched and holding and resume and rsi_ok):
                return neutral
            reasons.append(f"1h 回檔觸及 EMA{p['ema_pullback_period']} 後站回，RSI {r_now:.0f}，5m 動能恢復")

        stop = self._stop(df5, price, side)
        reasons.append(f"結構停損 {stop:.4g}（{abs(price - stop) / price * 100:.2f}%）")
        return Signal(
            type=SignalType.LONG if side == 1 else SignalType.SHORT, symbol=symbol,
            strength=p["strength"], reasons=reasons, price=price, min_strength=60,
            stop_price=stop,
        )
