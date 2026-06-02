"""一鍵清空 Binance Testnet 上的所有合約持倉與掛單

用途：
    bot 啟動前若 Testnet 殘留孤兒倉，先用此腳本歸零再啟動。
    僅在 testnet=true 時運行，避免誤觸正式環境。

執行：
    python -m scripts.close_all_testnet
"""

import asyncio
import sys

from src.utils.config import load_config
from src.data.binance_client import BinanceAPI


async def main():
    cfg = load_config()
    if not cfg["binance"]["testnet"]:
        print("⚠️  此腳本僅允許在 testnet=true 時執行，已中止")
        sys.exit(1)

    api = BinanceAPI(
        api_key=cfg["binance"]["api_key"],
        api_secret=cfg["binance"]["api_secret"],
        testnet=True,
    )
    await api.connect()
    try:
        positions = await api.get_open_positions()
        if not positions:
            print("✅ Testnet 無持倉")
        else:
            print(f"發現 {len(positions)} 筆持倉:")
            for p in positions:
                print(f"  - {p['symbol']} {p['side']} qty={p['quantity']} "
                      f"entry={p['entry_price']} pnl={p['unrealized_pnl']:.2f}")

            for p in positions:
                # 取消殘留掛單（停損/限價單）
                try:
                    await api.cancel_all_orders(p["symbol"])
                except Exception as e:
                    print(f"  ⚠️ 取消 {p['symbol']} 掛單失敗: {e}")

                # 反向市價平倉
                close_side = "SELL" if p["side"] == "LONG" else "BUY"
                try:
                    order = await api.futures_market_order(
                        symbol=p["symbol"],
                        side=close_side,
                        quantity=p["quantity"],
                    )
                    print(f"  ✅ 已平倉 {p['symbol']} order_id={order.get('orderId')}")
                except Exception as e:
                    print(f"  ❌ 平倉 {p['symbol']} 失敗: {e}")

        # 確認結果
        await asyncio.sleep(1)
        remaining = await api.get_open_positions()
        balance = await api.get_usdt_balance()
        print()
        print(f"📊 清倉後狀態：USDT 餘額 = {balance:.2f}, 剩餘持倉 = {len(remaining)}")
        if remaining:
            print("⚠️  仍有持倉未平：")
            for p in remaining:
                print(f"  - {p}")
    finally:
        await api.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
