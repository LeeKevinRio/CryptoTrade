"""抓幣安 24h 成交量排名 — 供 TOP-N 標的自動檢定用

    python -m backtest.top_volume --top 10          # 人類可讀
    python -m backtest.top_volume --top 10 --json   # JSON 陣列（給 CI matrix）

資料源：data-api.binance.vision 公開鏡像（現貨 24hr ticker）。
現貨與合約成交量排名高度一致；合約端以 1000 前綴掛牌的迷因幣
（PEPE/SHIB/BONK/FLOKI 等）名稱不對應且回測價格尺度不同，先行排除。
"""

import argparse
import json
import urllib.request

TICKER_URL = "https://data-api.binance.vision/api/v3/ticker/24hr"

# 穩定幣/法幣計價對 —— 成交量大但不是交易標的
EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "EURI",
    "GBP", "AUD", "TRY", "BRL", "ARS", "COP", "XUSD", "USD1", "AEUR",
}
# 合約端掛牌名稱帶 1000 前綴、與現貨名稱不對應者
FUTURES_1000_PREFIXED = {
    "PEPE", "SHIB", "BONK", "FLOKI", "SATS", "RATS", "LUNC", "XEC", "CAT", "WHY",
}


def top_usdt_symbols(n: int = 10) -> list[dict]:
    req = urllib.request.Request(TICKER_URL, headers={"User-Agent": "CryptoTrade/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        tickers = json.load(resp)

    rows = []
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in EXCLUDED_BASES:
            continue
        if base.endswith(("UP", "DOWN", "BULL", "BEAR")) and base not in ("SUP",):
            continue  # 槓桿代幣
        skipped = base in FUTURES_1000_PREFIXED
        rows.append({
            "symbol": sym,
            "quote_volume": float(t.get("quoteVolume", 0)),
            "skipped_1000_prefix": skipped,
        })

    rows.sort(key=lambda r: r["quote_volume"], reverse=True)
    return rows[:n]


def main():
    ap = argparse.ArgumentParser(description="幣安 24h 成交量排名")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true",
                    help="輸出可檢定的 symbol JSON 陣列（排除 1000 前綴者）")
    args = ap.parse_args()

    rows = top_usdt_symbols(args.top)
    if args.json:
        print(json.dumps([r["symbol"] for r in rows if not r["skipped_1000_prefix"]]))
        return
    for i, r in enumerate(rows, 1):
        note = "（合約為 1000 前綴掛牌，跳過自動檢定）" if r["skipped_1000_prefix"] else ""
        print(f"{i:>2}. {r['symbol']:<14} 24h成交額 {r['quote_volume']/1e9:.2f}B {note}")


if __name__ == "__main__":
    main()
