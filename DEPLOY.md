# 雲端部署指南

本機關機就停止交易 → 部署到雲端 VPS 讓它 24/7 執行。

---

## 一、選擇主機

加密貨幣 24 小時交易，需要一台不關機的機器。

| 方案 | 月費 | 說明 |
|------|------|------|
| **Oracle Cloud 永久免費** | $0 | ARM 4 核 / 24GB，最划算；申請較嚴格、偶爾缺貨 |
| **Hetzner CX22** | ~€4 | 歐洲，CP 值高 |
| **Vultr / DigitalOcean** | ~$6 | 節點多，可選東京/新加坡 |
| **AWS Lightsail** | ~$5 | 生態完整 |

**選節點以靠近幣安為準**：東京或新加坡延遲最低。歐美節點下單延遲會多 150-250ms，
對本專案（5m K 線收盤才決策）影響有限，但仍建議選亞洲。

規格 **1 核 / 1GB RAM** 即可跑 bot。若要在同一台跑參數掃描／walk-forward，
建議 2 核 / 4GB 以上。

---

## 二、部署步驟

```bash
# 1) 主機上安裝 Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# 2) 取得程式碼
git clone <你的 repo> cryptotrade && cd cryptotrade

# 3) 建立 .env（不要進版控）
cat > .env <<'EOF'
BINANCE_API_KEY=xxx
BINANCE_API_SECRET=xxx
BINANCE_TESTNET=true
WEB_AUTH_TOKEN=<夠長的隨機字串>
WEBHOOK_TOKEN=<另一組隨機字串>
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
chmod 600 .env

# 4) 啟動
docker compose up -d --build
docker compose logs -f
```

`restart: always` 會在崩潰與主機重開後自動拉起，取代本機的 `run_bot.bat`。

---

## 三、安全設定（務必做）

### API 金鑰
- 幣安 API 只開 **合約交易**，**絕不開提幣**
- 設 **IP 白名單** 指向 VPS 的固定 IP —— 金鑰外洩時這是最後一道防線

### 儀表板不要裸奔
預設 `ports: "127.0.0.1:8899:8899"` 只綁本機。從自己電腦看儀表板用 SSH 隧道：

```bash
ssh -L 8899:127.0.0.1:8899 user@你的VPS
# 然後本機瀏覽器開 http://127.0.0.1:8899
```

這樣完全不暴露到公網，最安全。

### 防火牆
```bash
sudo ufw allow OpenSSH && sudo ufw enable
```

---

## 四、TradingView Webhook 對外開放

TradingView 需要能連到你的機器，這是唯一必須開公網的部分。

**做法：用 Caddy 自動申請 HTTPS 憑證**（需要一個網域指向 VPS）

```bash
# docker-compose.yml 中加入
#   caddy:
#     image: caddy:2
#     restart: always
#     ports: ["80:80", "443:443"]
#     volumes:
#       - ./Caddyfile:/etc/caddy/Caddyfile
#       - caddy_data:/data
```

`Caddyfile`：
```
你的網域.com {
    # 只把 webhook 路徑對外，其餘一律不開
    handle /webhook/* {
        reverse_proxy cryptotrade-bot:8899
    }
    handle {
        respond "not found" 404
    }
}
```

TradingView alert 的 Webhook URL 填：
```
https://你的網域.com/webhook/tradingview/<WEBHOOK_TOKEN>
```

**沒有網域的替代方案**：`ngrok http 8899`（免費版網址每次重啟會變，需重設 alert）。

> ⚠️ 只把 `/webhook/*` 對外。整個儀表板暴露到公網等於把交易控制權（暫停、平倉、
> 強制下單）交給任何掃到你 IP 的人。

---

## 五、日常維運

```bash
docker compose logs -f --tail 100        # 看日誌
docker compose restart                    # 重啟
docker compose up -d --build              # 更新程式碼後重新部署

# 績效與 edge 檢定（在容器內執行）
docker compose exec cryptotrade-bot python -m scripts.check_edge
docker compose exec cryptotrade-bot python -m scripts.report_stats --days 7
```

### 備份（重要）
`./data/cryptotrade.db` 是全部交易紀錄，也是重啟後還原風控狀態的依據。

```bash
# 每天備份一次
0 3 * * * cd ~/cryptotrade && cp data/cryptotrade.db backups/db-$(date +\%F).db
```

---

## 六、跑長時間運算（參數掃描 / walk-forward）

這類工作跑數小時，本機關機就中斷 —— 這正是雲端的另一個用途：

```bash
# 用 tmux 讓它在斷線後繼續跑
tmux new -s sweep
docker compose exec cryptotrade-bot python -m backtest.walk_forward --days 730
# Ctrl+B 再按 D 離開，之後 tmux attach -t sweep 回來看
```

---

## 七、上真金前的檢查清單

- [ ] `BINANCE_TESTNET=false` 之前，先確認 `scripts/check_edge.py` 的 t > 2 且信賴區間下界 > 0
- [ ] API 金鑰已設 IP 白名單、未開提幣權限
- [ ] `.env` 權限 600、未進版控
- [ ] DB 已設定每日備份
- [ ] 儀表板未暴露公網（或已加驗證）
- [ ] 先以小額跑滿 100 筆，比對真實滑價與 testnet 的落差
