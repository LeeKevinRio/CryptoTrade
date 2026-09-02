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

REM 8899 埠是「重複啟動守門」：被占用 = 很可能已有一個 bot 在跑
netstat -ano | findstr /R /C:":8899 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [錯誤] 8899 埠已被占用 — 很可能已有另一個 bot 在跑（舊視窗 / run_bot.bat / Docker）
  echo 佔用者：
  netstat -ano | findstr /R /C:":8899 .*LISTENING"
  echo 查程式名稱：tasklist /FI "PID eq ^<最後一欄的PID^>"
  echo 確定要換成這個新版就先關掉它：taskkill /PID ^<PID^> /F  （Docker 則 docker compose down）
  echo 兩台引擎同時交易會對同一帳戶重複下單，請只留一台。
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
