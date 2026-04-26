#!/bin/bash
# CryptoTrade Bot 部署腳本 - Oracle Cloud

set -e

echo "=== CryptoTrade Bot 部署開始 ==="

# 1. 更新系統
sudo apt update && sudo apt upgrade -y

# 2. 安裝 Docker
if ! command -v docker &> /dev/null; then
    echo "安裝 Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    echo "Docker 安裝完成，請重新登入後再執行此腳本"
    exit 0
fi

# 3. 安裝 Docker Compose
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "安裝 Docker Compose..."
    sudo apt install -y docker-compose-plugin
fi

# 4. 建立並啟動
echo "建置 Docker image..."
docker compose build

echo "啟動 Bot..."
docker compose up -d

echo ""
echo "=== 部署完成 ==="
echo "查看日誌: docker compose logs -f"
echo "停止 Bot: docker compose down"
echo "重啟 Bot: docker compose restart"
