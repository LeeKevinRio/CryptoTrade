FROM python:3.11-slim

WORKDIR /app

# curl 供 healthcheck 使用
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 安裝依賴（先複製 requirements 以利用 layer cache）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 程式碼。scripts/ 與 backtest/ 也要帶進去，
# 否則無法在容器內執行 check_edge / report_stats / 參數掃描
COPY src/ src/
COPY config/ config/
COPY scripts/ scripts/
COPY backtest/ backtest/

# 資料目錄（對應 compose 的 volume；DB 放這裡才不會隨容器重建消失）
RUN mkdir -p /app/data /app/logs /app/reports

ENV PYTHONUNBUFFERED=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8899 \
    DATABASE_URL=sqlite+aiosqlite:////app/data/cryptotrade.db

EXPOSE 8899

# /healthz 不需認證 —— /api/status 在 DASHBOARD_AUTH=true 時會回 401
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WEB_PORT:-8899}/healthz" || exit 1

CMD ["python", "-m", "src.main"]
