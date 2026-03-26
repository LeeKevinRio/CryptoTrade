"""離線回測 — 使用模擬歷史資料，不需要 API 連線"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

from src.utils.config import load_config
from backtest.backtester import Backtester


def generate_realistic_data(days: int = 30, interval_minutes: int = 5) -> pd.DataFrame:
    """
    生成模擬 BTC 行情資料（含趨勢、波動、成交量變化）
    模擬真實市場：有上漲、下跌、震盪三種行情
    """
    np.random.seed(42)
    candles_per_day = 24 * 60 // interval_minutes
    total_candles = days * candles_per_day

    # 起始價格
    price = 65000.0
    prices = []
    volumes = []

    for i in range(total_candles):
        day = i // candles_per_day
        hour = (i % candles_per_day) * interval_minutes // 60

        # 每 5 天切換一次行情模式
        phase = (day // 5) % 4
        if phase == 0:
            # 上漲趨勢
            drift = 0.0003
            volatility = 0.003
        elif phase == 1:
            # 暴跌（抄底機會）
            drift = -0.0008
            volatility = 0.006
        elif phase == 2:
            # 反彈上漲
            drift = 0.0005
            volatility = 0.004
        else:
            # 過熱後回調（做空機會）
            drift = -0.0004
            volatility = 0.005

        # 價格變動
        change = drift + volatility * np.random.randn()
        price *= (1 + change)
        price = max(price, 30000)  # 不低於 30000

        # 生成 OHLC
        intra_vol = volatility * abs(np.random.randn())
        high = price * (1 + intra_vol)
        low = price * (1 - intra_vol)
        open_price = price * (1 + 0.001 * np.random.randn())

        # 成交量（暴跌時放量，上漲末期縮量）
        base_volume = 500
        if phase == 1:  # 暴跌放量
            vol_mult = 2.0 + abs(np.random.randn())
        elif phase == 3:  # 過熱縮量
            vol_mult = 0.5 + 0.3 * abs(np.random.randn())
        else:
            vol_mult = 1.0 + 0.5 * np.random.randn()

        volume = base_volume * max(0.1, vol_mult)

        prices.append({
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(price, 2),
            "volume": round(volume, 2),
        })
        volumes.append(volume)

    # 建立 DataFrame
    start_time = pd.Timestamp("2025-02-24")
    timestamps = pd.date_range(start=start_time, periods=total_candles, freq=f"{interval_minutes}min")
    df = pd.DataFrame(prices, index=timestamps)
    df.index.name = "timestamp"

    return df


def main():
    print("=" * 55)
    print("  CryptoTrade 離線回測（模擬 30 天 BTC 行情）")
    print("=" * 55)
    print()

    # 生成資料
    print("📊 生成模擬行情資料...")
    df = generate_realistic_data(days=30, interval_minutes=5)
    print(f"   資料筆數: {len(df)} 根 K 線")
    print(f"   時間範圍: {df.index[0]} ~ {df.index[-1]}")
    print(f"   起始價格: ${df['close'].iloc[0]:,.2f}")
    print(f"   結束價格: ${df['close'].iloc[-1]:,.2f}")
    price_change = (df['close'].iloc[-1] - df['close'].iloc[0]) / df['close'].iloc[0] * 100
    print(f"   價格變動: {price_change:+.2f}%")
    print(f"   最高價:   ${df['high'].max():,.2f}")
    print(f"   最低價:   ${df['low'].min():,.2f}")

    # 載入設定
    config = load_config()

    # 執行回測
    print("\n🔄 執行策略回測...")
    bt = Backtester(config)
    result = bt.run(df, "BTCUSDT")

    # 輸出報告
    bt.print_report(result)

    # 額外統計
    if result.trades:
        print("\n📋 所有交易明細:")
        print("-" * 90)
        print(f"  {'#':>3}  {'方向':5s}  {'進場時間':19s}  {'出場時間':19s}  {'盈虧%':>8s}  {'原因'}")
        print("-" * 90)
        for i, t in enumerate(result.trades, 1):
            emoji = "🟢" if t.pnl_pct > 0 else "🔴"
            print(
                f"  {i:3d}  {t.side:5s}  {t.entry_time[:19]:19s}  {t.exit_time[:19]:19s}  "
                f"{emoji}{t.pnl_pct:+.2f}%   {t.reason}"
            )
        print("-" * 90)

        # 統計多空分開的績效
        longs = [t for t in result.trades if t.side == "LONG"]
        shorts = [t for t in result.trades if t.side == "SHORT"]
        print(f"\n  📈 做多交易: {len(longs)} 筆", end="")
        if longs:
            long_wins = [t for t in longs if t.pnl_pct > 0]
            print(f" | 勝率 {len(long_wins)/len(longs)*100:.0f}% | 總盈虧 {sum(t.pnl_pct for t in longs):+.2f}%")
        else:
            print()
        print(f"  📉 做空交易: {len(shorts)} 筆", end="")
        if shorts:
            short_wins = [t for t in shorts if t.pnl_pct > 0]
            print(f" | 勝率 {len(short_wins)/len(shorts)*100:.0f}% | 總盈虧 {sum(t.pnl_pct for t in shorts):+.2f}%")
        else:
            print()


if __name__ == "__main__":
    main()
