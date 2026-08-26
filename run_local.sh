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
