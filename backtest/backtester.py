"""回測引擎 — 使用歷史資料驗證策略績效"""

import asyncio
from dataclasses import dataclass, field

import pandas as pd
import numpy as np

from src.strategy.signal_aggregator import SignalAggregator
from src.strategy.base_strategy import SignalType
from src.indicators.atr import get_current_atr
from src.utils.config import load_config
from src.utils.logger import setup_logger
from src.data.binance_client import BinanceAPI

logger = setup_logger("backtester")


@dataclass
class BacktestTrade:
    entry_time: str
    exit_time: str
    side: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    reason: str


@dataclass
class BacktestResult:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)


class Backtester:
    """歷史回測引擎"""

    def __init__(self, config: dict):
        self.config = config
        self.aggregator = SignalAggregator(config)
        risk = config.get("risk", {})
        self.stop_loss_pct = risk.get("stop_loss_pct", 2.0)
        tp = risk.get("take_profit", {})
        self.tp_levels = [
            {"pct": tp.get("level_1_pct", 1.0), "close": tp.get("level_1_close_pct", 40)},
            {"pct": tp.get("level_2_pct", 2.0), "close": tp.get("level_2_close_pct", 30)},
            {"pct": tp.get("level_3_pct", 3.5), "close": tp.get("level_3_close_pct", 30)},
        ]
        trail = risk.get("trailing_stop", {})
        self.trail_activation = trail.get("activation_pct", 1.5)
        self.trail_callback = trail.get("callback_pct", 0.5)

    def run(self, df: pd.DataFrame, symbol: str = "BTCUSDT") -> BacktestResult:
        """
        對單一時框 DataFrame 進行回測

        df 需要包含: open, high, low, close, volume
        """
        result = BacktestResult()
        equity_curve = [0.0]
        peak = 0.0

        i = 50  # 跳過前 50 根用於指標預熱
        in_position = False
        entry_price = 0.0
        side = ""
        entry_idx = 0
        highest_pnl = 0.0
        trailing_active = False

        while i < len(df):
            if not in_position:
                # 用到 i 為止的資料做分析
                lookback = df.iloc[max(0, i - 200):i + 1].copy()
                candles = {"5m": lookback, "15m": lookback, "1h": lookback}

                signal = self.aggregator.evaluate(symbol, candles, funding_rate=0.0)

                if signal.is_actionable:
                    in_position = True
                    entry_price = float(df["close"].iloc[i])
                    side = "LONG" if signal.type == SignalType.LONG else "SHORT"
                    entry_idx = i
                    highest_pnl = 0.0
                    trailing_active = False
                i += 1
                continue

            # 有持倉 — 逐根檢查停利停損
            current = float(df["close"].iloc[i])
            high = float(df["high"].iloc[i])
            low = float(df["low"].iloc[i])

            if side == "LONG":
                pnl_pct = (current - entry_price) / entry_price * 100
                worst_pnl = (low - entry_price) / entry_price * 100
                best_pnl = (high - entry_price) / entry_price * 100
            else:
                pnl_pct = (entry_price - current) / entry_price * 100
                worst_pnl = (entry_price - high) / entry_price * 100
                best_pnl = (entry_price - low) / entry_price * 100

            highest_pnl = max(highest_pnl, best_pnl)
            exit_reason = None

            # 停損
            if worst_pnl <= -self.stop_loss_pct:
                pnl_pct = -self.stop_loss_pct
                exit_reason = "停損"

            # 階梯停利 — 簡化：取最高層觸發
            if exit_reason is None:
                for lv in reversed(self.tp_levels):
                    if best_pnl >= lv["pct"]:
                        pnl_pct = lv["pct"] * 0.9  # 模擬滑價
                        exit_reason = f"停利 +{lv['pct']}%"
                        break

            # 移動停利
            if exit_reason is None and highest_pnl >= self.trail_activation:
                trailing_active = True
            if exit_reason is None and trailing_active:
                drawdown = highest_pnl - pnl_pct
                if drawdown >= self.trail_callback:
                    pnl_pct = highest_pnl - self.trail_callback
                    exit_reason = f"移動停利 (高點+{highest_pnl:.1f}%)"

            if exit_reason:
                trade = BacktestTrade(
                    entry_time=str(df.index[entry_idx]),
                    exit_time=str(df.index[i]),
                    side=side,
                    entry_price=entry_price,
                    exit_price=current,
                    pnl_pct=round(pnl_pct, 2),
                    reason=exit_reason,
                )
                result.trades.append(trade)
                equity_curve.append(equity_curve[-1] + pnl_pct)
                peak = max(peak, equity_curve[-1])
                dd = peak - equity_curve[-1]
                result.max_drawdown_pct = max(result.max_drawdown_pct, dd)

                in_position = False

            i += 1

        # 統計
        result.total_trades = len(result.trades)
        wins = [t for t in result.trades if t.pnl_pct > 0]
        losses = [t for t in result.trades if t.pnl_pct <= 0]
        result.winning_trades = len(wins)
        result.losing_trades = len(losses)
        result.total_pnl_pct = round(sum(t.pnl_pct for t in result.trades), 2)
        result.win_rate = round(len(wins) / max(1, result.total_trades) * 100, 1)
        result.avg_win_pct = round(np.mean([t.pnl_pct for t in wins]), 2) if wins else 0
        result.avg_loss_pct = round(np.mean([t.pnl_pct for t in losses]), 2) if losses else 0
        total_loss = abs(sum(t.pnl_pct for t in losses)) if losses else 1
        total_win = sum(t.pnl_pct for t in wins) if wins else 0
        result.profit_factor = round(total_win / max(total_loss, 0.01), 2)

        pnls = [t.pnl_pct for t in result.trades]
        if pnls and np.std(pnls) > 0:
            result.sharpe_ratio = round(np.mean(pnls) / np.std(pnls) * np.sqrt(252), 2)

        result.max_drawdown_pct = round(result.max_drawdown_pct, 2)

        return result

    def print_report(self, result: BacktestResult):
        print("\n" + "=" * 50)
        print("           回測績效報告")
        print("=" * 50)
        print(f"  總交易次數:     {result.total_trades}")
        print(f"  勝率:           {result.win_rate}%")
        print(f"  總盈虧:         {result.total_pnl_pct:+.2f}%")
        print(f"  平均獲利:       {result.avg_win_pct:+.2f}%")
        print(f"  平均虧損:       {result.avg_loss_pct:.2f}%")
        print(f"  盈虧比:         {result.profit_factor}")
        print(f"  最大回撤:       {result.max_drawdown_pct:.2f}%")
        print(f"  夏普比率:       {result.sharpe_ratio}")
        print("=" * 50)

        if result.trades:
            print("\n最近 10 筆交易:")
            for t in result.trades[-10:]:
                emoji = "+" if t.pnl_pct > 0 else ""
                print(
                    f"  {t.side:5s} | {t.entry_time[:16]} → {t.exit_time[:16]} | "
                    f"{emoji}{t.pnl_pct:.2f}% | {t.reason}"
                )


async def download_and_backtest(symbol: str = "BTCUSDT", interval: str = "5m", limit: int = 1000):
    """下載歷史資料並回測"""
    config = load_config()
    api = BinanceAPI(
        api_key=config["binance"]["api_key"],
        api_secret=config["binance"]["api_secret"],
        testnet=config["binance"]["testnet"],
    )
    await api.connect()

    try:
        raw = await api.get_klines(symbol, interval, limit=limit)
        rows = []
        for k in raw:
            rows.append({
                "timestamp": pd.to_datetime(k[0], unit="ms"),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })
        df = pd.DataFrame(rows)
        df.set_index("timestamp", inplace=True)

        bt = Backtester(config)
        result = bt.run(df, symbol)
        bt.print_report(result)
        return result
    finally:
        await api.disconnect()


if __name__ == "__main__":
    asyncio.run(download_and_backtest())
