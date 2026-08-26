@echo off
chcp 65001 >nul
REM ============================================================
REM  CryptoTrade 本地執行
REM    run_local.bat        全功能模式（交易 + 介面）
REM    run_local.bat view   觀察模式（只看不下單）
REM
REM  ⚠️ 黃金規則：同一時間只能有「一台」引擎在交易。
REM    - Render 正常運作時 → 用 view 模式看盤
REM    - Render 掛掉時     → 全功能模式接手，Render 恢復前先關掉本地
REM  首次執行前：複製 .env.example 為 .env 並填入金鑰
REM ============================================================
cd /d %~dp0

if not exist .env (
  echo [錯誤] 找不到 .env — 請複製 .env.example 為 .env 並填入金鑰
  pause & exit /b 1
)

if not exist .venv (
  echo [首次執行] 建立虛擬環境...
  python -m venv .venv
)
.venv\Scripts\pip install -q -r requirements.txt

if /i "%1"=="view" (
  set TRADING_DISABLED=true
  echo [模式] 觀察模式 — 不會送出任何訂單
) else (
  set TRADING_DISABLED=
  echo [模式] 全功能模式 — 會實際交易！確認雲端引擎已停止
)

echo 儀表板: http://127.0.0.1:8899
.venv\Scripts\python -m src.main
pause
