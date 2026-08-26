# 雲端部署指南

本機關機就停止交易 → 部署到雲端讓它 24/7 執行。

**只是想要一個網址測試、不想管主機？直接看下面「零、最快拿到公開網址」。**
要長期跑正式環境、完全掌控機器，再看「一、選擇主機」以後的 VPS 方案。

---

## 零、最快拿到公開網址（免 VPS、免買網域）

部署到 PaaS 平台，平台直接送你一個 HTTPS 網址（如 `https://cryptotrade.onrender.com`），
手機、任何電腦都能開。repo 已放好兩份現成設定檔：

| 方案 | 設定檔 | 費用 | 適合 |
|------|--------|------|------|
| **Render**（推薦入門） | `render.yaml` | 免費起 | 快速測試儀表板 |
| **Fly.io** | `fly.toml` | 約 $2-3/月 | 24/7 常駐 + 保留交易紀錄 |

### 方案 A：Render（點幾下就完成）

1. 用 GitHub 帳號登入 [dashboard.render.com](https://dashboard.render.com)
2. **New → Blueprint** → 選擇本 repo（首次需授權 Render 讀取）
3. Render 會讀取 `render.yaml` 自動帶出所有設定，此時填入兩個欄位：
   - `BINANCE_API_KEY`、`BINANCE_API_SECRET`：填 **testnet 金鑰**
     （在 [testnet.binancefuture.com](https://testnet.binancefuture.com) 免費申請，與正式金鑰無關）
4. 按 **Apply**，等 3-5 分鐘建置完成
5. 到服務頁面 → **Environment** → 複製自動產生的 `WEB_AUTH_TOKEN`
6. 瀏覽器打開：

   ```
   https://<你的服務名>.onrender.com/?token=<WEB_AUTH_TOKEN>
   ```

   通過一次後會種 cookie，同一瀏覽器之後直接開網址即可。

之後每次 `git push`，Render 會自動重新部署 —— 改完策略推上去就能在網址上看結果。

**免費方案的兩個限制（測試夠用，跑真交易不行）：**
- 閒置 15 分鐘會休眠，交易迴圈跟著停，有人開網址才醒來
- 沒有持久磁碟，重新部署後 SQLite 交易紀錄歸零

要 24/7 不中斷：把 `render.yaml` 的 `plan: free` 改成 `plan: starter`（$7/月），
並取消 `disk:` 區塊的註解讓交易紀錄跨部署保留。

### 方案 B：Fly.io（24/7 常駐 + 持久磁碟）

```bash
# 安裝 CLI 並登入
curl -L https://fly.io/install.sh | sh
fly auth login

# 在 repo 目錄執行（沿用 fly.toml；app 名稱被占用時換一個，網址跟著變）
fly launch --no-deploy
fly volumes create cryptotrade_data --region sin --size 1
fly secrets set \
  BINANCE_API_KEY=你的testnet金鑰 \
  BINANCE_API_SECRET=你的testnet秘鑰 \
  WEB_AUTH_TOKEN=$(openssl rand -hex 24) \
  WEBHOOK_TOKEN=$(openssl rand -hex 24)
fly deploy

# 完成後
fly status          # 看網址：https://<app名稱>.fly.dev
fly logs            # 看日誌
```

瀏覽器開 `https://<app名稱>.fly.dev/?token=<你設定的WEB_AUTH_TOKEN>`。

### 本地執行（雲端的備援與看盤）

雲端卡住時本地要能接手，平時也能在本地看介面。首次準備：
複製 `.env.example` 為 `.env` 填入金鑰，之後雙擊 / 執行：

```
run_local.bat          # Windows — 全功能模式（交易 + 介面）
run_local.bat view     # Windows — 觀察模式（只看不下單）
./run_local.sh         # macOS/Linux — 全功能模式
./run_local.sh view    # macOS/Linux — 觀察模式
```

儀表板在 http://127.0.0.1:8899（本地預設免 token）。

**⚠️ 黃金規則：同一時間只能有一台引擎在交易。**

| 情境 | 本地用哪個模式 |
|---|---|
| 雲端正常運作 | `view`（觀察模式：完整看行情/持倉/績效，保證不下單、不動帳戶設定） |
| 雲端掛掉 | 全功能模式接手；**雲端恢復前先關掉本地**，再讓雲端接回 |

兩台全功能引擎同時跑會對同一個幣安帳戶重複開倉、互搶平倉。觀察模式由
`TRADING_DISABLED=true` 環境變數實現，`/api/status` 的 `trading_disabled`
欄位可確認當前模式。

### 頻寬（免費方案的真正瓶頸）

Render Hobby workspace 每月含 **5 GB 流量**,超過整個 workspace 會被暫停。
儀表板本身就是流量大戶,已做四項優化(2026-08-23):

| 項目 | 優化前 | 優化後 |
|---|---|---|
| WebSocket K 線 | 推送全部 symbol×timeframe(10×5=50 條串流) | 只推當下檢視的一組 |
| 回應壓縮 | 無 | gzip(JSON 約省 80%) |
| 背景分頁 | 照常輪詢 | 暫停輪詢並關閉行情串流 |
| 輪詢間隔 | 8/15/30s | 20/30/60s |

估算:開著分頁一天由約 1 GB 降至數十 MB。即使如此,**長時間開著儀表板仍會累積流量**
—— 平時關掉分頁,要看再開;bot 在伺服器端照常交易,不受影響。

### 公網安全（兩個方案共通）

- `DASHBOARD_AUTH=true` 已在設定檔中開啟：**整個**儀表板（頁面、API、WebSocket）
  都要 token，沒 token 的人只會看到 401。豁免的只有 `/healthz` 與自帶驗證的 webhook。
- TradingView alert 的 Webhook URL 填：

  ```
  https://<你的網址>/webhook/tradingview/<WEBHOOK_TOKEN>
  ```

  網址是現成 HTTPS，不用再架 Caddy / ngrok。
- 節點選新加坡（設定檔已指定）：幣安正式環境會擋美國 IP，testnet 也建議照做。
- 幣安 API 金鑰仍然只開合約交易、不開提幣。

### 想用自己的網域？

兩個平台都支援免費綁定自訂網域（Render: Settings → Custom Domains；
Fly: `fly certs add 你的網域.com`），到網域商加一筆 CNAME 指過去即可，
HTTPS 憑證平台自動簽發。沒有網域也完全不影響使用。

### 自動化體檢（GitHub Actions，已內建）

repo 已設定三條自動化管線（`.github/workflows/`），全部在 GitHub 免費額度內執行：

| workflow | 觸發 | 產出 |
|---|---|---|
| 參數掃描 | 每週一 04:00 UTC + 更新 `backtest/.sweep-trigger` | 10 標的出場參數全網格 → `reports/sweep-*.md` + `sweep-full-*.csv` |
| 進場參數掃描 | 更新 `backtest/.entry-sweep-trigger` | 進場門檻網格 → `reports/entry-sweep-*.csv` |
| 每週績效快照 | 每週一 03:30 UTC + 更新 `backtest/.weekly-stats-trigger` | 線上實盤近 7 天績效 → `reports/weekly-perf-*.{json,md}` |

**每週績效快照需要設定兩個 secrets** 才會生效（沒設會自動跳過）：
GitHub repo → Settings → Secrets and variables → Actions → New repository secret

- `DASHBOARD_URL`：`https://你的服務名.onrender.com`（結尾不要斜線）
- `WEB_AUTH_TOKEN`：與 Render 環境變數中相同的 token

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
