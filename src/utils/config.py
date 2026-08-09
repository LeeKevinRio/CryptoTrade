"""設定檔載入與驗證"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


def _validate(config: dict):
    """簡易但強硬的設定驗證 — 阻擋顯然錯誤的參數"""
    bots = config.get("bots", {})
    if not bots:
        raise ConfigError("settings.yaml 缺 'bots' 區塊或為空")

    enabled = [bid for bid, b in bots.items() if b.get("enabled", True)]
    if not enabled:
        raise ConfigError("沒有任何 bot 啟用 (enabled: true)")

    for bid, b in bots.items():
        if not b.get("enabled", True):
            continue
        mode = b.get("mode")
        if mode not in ("spot", "futures"):
            raise ConfigError(f"bot {bid} mode 必須是 spot 或 futures，目前: {mode}")
        lev = b.get("leverage", 1)
        if not (1 <= lev <= 125):
            raise ConfigError(f"bot {bid} leverage 必須在 1-125，目前: {lev}")
        risk = b.get("risk", {})
        max_pos = risk.get("max_position_pct", 10)
        if not (0 < max_pos <= 100):
            raise ConfigError(f"bot {bid} max_position_pct 必須在 0-100，目前: {max_pos}")
        sl = risk.get("stop_loss_pct", 2.0)
        if sl <= 0 or sl > 50:
            raise ConfigError(f"bot {bid} stop_loss_pct 不合理: {sl}")
        max_loss = risk.get("max_daily_loss_pct", 3.0)
        if max_loss <= 0 or max_loss > 100:
            raise ConfigError(f"bot {bid} max_daily_loss_pct 不合理: {max_loss}")
        if mode == "spot" and not b.get("only_long", True):
            raise ConfigError(f"bot {bid} mode=spot 必須設 only_long=true")

    syms = config.get("trading", {}).get("symbols", [])
    if not syms:
        raise ConfigError("trading.symbols 不可為空")


def load_config(config_path: str = "config/settings.yaml") -> dict:
    load_dotenv()

    cfg_file = Path(config_path)
    if not cfg_file.exists():
        raise ConfigError(f"找不到設定檔: {config_path}")

    with open(cfg_file, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        raise ConfigError(
            "BINANCE_API_KEY / BINANCE_API_SECRET 未設定（請檢查 .env）"
        )

    config["binance"] = {
        "api_key": api_key,
        "api_secret": api_secret,
        "testnet": os.getenv("BINANCE_TESTNET", "true").lower() == "true",
    }
    config["telegram"] = {
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    }

    # ── 環境變數覆寫（容器/雲端部署用）──
    # 容器內必須綁 0.0.0.0，綁 127.0.0.1 會使 port mapping 無法連入。
    web = config.setdefault("web", {})
    if os.getenv("WEB_HOST"):
        web["host"] = os.getenv("WEB_HOST")
    if os.getenv("WEB_PORT"):
        try:
            web["port"] = int(os.getenv("WEB_PORT"))
        except ValueError:
            pass
    # DB 路徑需指向掛載的 volume，否則重建容器會遺失所有交易紀錄
    if os.getenv("DATABASE_URL"):
        config.setdefault("database", {})["url"] = os.getenv("DATABASE_URL")

    _validate(config)
    return config
