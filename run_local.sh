#!/usr/bin/env bash
# CryptoTrade 本地執行
#   ./run_local.sh        全功能模式（交易 + 介面）
#   ./run_local.sh view   觀察模式（只看不下單）
#
# ⚠️ 黃金規則：同一時間只能有「一台」引擎在交易。
#   - 雲端（Render/Fly）正常運作時 → 用 view 模式看盤
#   - 雲端掛掉時 → 全功能模式接手，雲端恢復前先關掉本地
# 首次執行前：複製 .env.example 為 .env 並填入金鑰
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo "[錯誤] 找不到 .env — 請複製 .env.example 為 .env 並填入金鑰"; exit 1; }

# 8899 埠是「重複啟動守門」：被占用 = 很可能已有一個 bot 在跑
if (command -v lsof >/dev/null && lsof -iTCP:8899 -sTCP:LISTEN >/dev/null 2>&1) \
   || (command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ':8899 '); then
  echo "[錯誤] 8899 埠已被占用 — 很可能已有另一個 bot 在跑（舊視窗 / Docker）"
  (lsof -iTCP:8899 -sTCP:LISTEN 2>/dev/null || ss -ltnp 2>/dev/null | grep ':8899 ') || true
  echo "確定要換成這個新版就先關掉它（kill <PID> 或 docker compose down）。"
  echo "兩台引擎同時交易會對同一帳戶重複下單，請只留一台。"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "[首次執行] 建立虛擬環境..."
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

if [ "${1:-}" = "view" ]; then
  export TRADING_DISABLED=true
  echo "[模式] 觀察模式 — 不會送出任何訂單"
else
  unset TRADING_DISABLED || true
  echo "[模式] 全功能模式 — 會實際交易！確認雲端引擎已停止"
fi

echo "儀表板: http://127.0.0.1:8899"
exec .venv/bin/python -m src.main
