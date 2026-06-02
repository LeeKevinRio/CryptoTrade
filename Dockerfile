FROM python:3.11-slim

WORKDIR /app

# 安裝依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼
COPY src/ src/
COPY config/ config/

# .env 由 docker-compose env_file 注入，避免烤進 image
EXPOSE 8788

CMD ["python", "-m", "src.main"]
