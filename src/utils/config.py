"""設定檔載入"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv


def load_config(config_path: str = "config/settings.yaml") -> dict:
    load_dotenv()

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 注入環境變數
    config["binance"] = {
        "api_key": os.getenv("BINANCE_API_KEY", ""),
        "api_secret": os.getenv("BINANCE_API_SECRET", ""),
        "testnet": os.getenv("BINANCE_TESTNET", "true").lower() == "true",
    }
    config["telegram"] = {
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    }

    return config
