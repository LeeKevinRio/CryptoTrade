@echo off
chcp 65001 >nul
REM ============================================================
REM  CryptoTrade 真金模式（家用電腦）
REM    雙擊執行，或在 PowerShell 輸入 .\run_live.bat
REM
REM  - 金鑰讀 .env.live（與測試網的 .env 完全分開）
REM  - 交易紀錄存 live.db（與測試網分開）
REM  - 儀表板只開在本機 http://127.0.0.1:8899
REM  - 執行期間電腦不會自動睡眠；關閉此視窗 = 停止交易
REM    （已開的倉位仍有交易所端停損／停利單保護，重開後自動接管）
REM ============================================================
cd /d %~dp0

if not exist .env.live (
  copy .env.live.example .env.live >nul
  echo [首次執行] 已建立 .env.live，請在記事本填入真金 API Key / Secret，存檔後再執行一次
  notepad .env.live
  pause & exit /b 1
)
findstr /R /C:"^BINANCE_API_KEY=..*" .env.live >nul
if errorlevel 1 (
  echo [錯誤] .env.live 的 BINANCE_API_KEY 還是空的
  notepad .env.live
  pause & exit /b 1
)

REM 8899 埠是「重複啟動守門」：被占用 = 已有一個 bot 在跑
netstat -ano | findstr /R /C:":8899 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [錯誤] 8899 埠已被占用 — 已有另一個 bot 在跑，真金不允許兩台引擎同時運作
  netstat -ano | findstr /R /C:":8899 .*LISTENING"
  echo 查程式名稱：tasklist /FI "PID eq ^<最後一欄的PID^>"
  echo 關掉它：taskkill /PID ^<PID^> /F
  pause & exit /b 1
)

if not exist .venv (
  echo [首次執行] 建立虛擬環境...
  python -m venv .venv
)
.venv\Scripts\pip install -q -r requirements.txt

REM 顯示目前對外 IP —— 必須和幣安 API 白名單一致
set PUBIP=
for /f %%i in ('curl -s https://api.ipify.org 2^>nul') do set PUBIP=%%i
echo.
echo ============================================================
echo   你目前的對外 IP：%PUBIP%
echo   幣安 API 管理的 IP 白名單必須包含這個 IP，否則會連不上
echo ============================================================
echo.
echo   即將以【真金】開始自動交易。
set /p CONFIRM=  確認請輸入 YES 後按 Enter：
if /i not "%CONFIRM%"=="YES" (
  echo 已取消。
  pause & exit /b 0
)

set ENV_FILE=.env.live
set BINANCE_TESTNET=false
set LIVE_TRADING_ACK=I_UNDERSTAND
set TRADING_DISABLED=
set DATABASE_URL=sqlite:///live.db
set DASHBOARD_AUTH=false
set WEB_HOST=127.0.0.1
set WEB_PORT=8899

echo.
echo [真金模式] 儀表板: http://127.0.0.1:8899   診斷: http://127.0.0.1:8899/api/diag
.venv\Scripts\python -m src.main
pause
