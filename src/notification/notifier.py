"""通知服務 — Telegram 推播"""

import asyncio
import aiohttp

from src.utils.logger import setup_logger

logger = setup_logger("notifier")


class TelegramNotifier:
    """透過 Telegram Bot 發送交易通知"""

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)

    async def send(self, message: str):
        if not self.enabled:
            logger.debug("通知已停用，跳過: %s", message[:50])
            return

        url = self.BASE_URL.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        logger.debug("Telegram 通知已送出")
                    else:
                        body = await resp.text()
                        logger.warning("Telegram 送出失敗: %s %s", resp.status, body)
        except Exception as e:
            logger.warning("Telegram 通知錯誤: %s", e)

    async def notify_open(self, trade: dict):
        msg = (
            f"🟢 *開倉通知*\n"
            f"交易對: `{trade['symbol']}`\n"
            f"方向: *{trade['side']}*\n"
            f"價格: `{trade['entry_price']:.2f}`\n"
            f"數量: `{trade['quantity']:.4f}`\n"
            f"槓桿: `{trade.get('leverage', 1)}x`\n"
            f"訊號強度: `{trade.get('signal_strength', 0):.0f}`\n"
            f"原因: {', '.join(trade.get('reasons', []))}"
        )
        await self.send(msg)

    async def notify_close(self, trade: dict):
        emoji = "🟢" if trade.get("pnl", 0) > 0 else "🔴"
        msg = (
            f"{emoji} *平倉通知*\n"
            f"交易對: `{trade['symbol']}`\n"
            f"方向: *{trade['side']}*\n"
            f"進場: `{trade['entry_price']:.2f}`\n"
            f"出場: `{trade['exit_price']:.2f}`\n"
            f"盈虧: `{trade['pnl']:.4f} USDT` ({trade['pnl_pct']:+.2f}%)\n"
            f"原因: {trade.get('reason', 'N/A')}"
        )
        await self.send(msg)

    async def notify_daily_report(self, stats: dict):
        msg = (
            f"📊 *每日績效報告*\n"
            f"交易次數: `{stats['trades']}`\n"
            f"勝率: `{stats['win_rate']:.1f}%`\n"
            f"總盈虧: `{stats['pnl']:.4f} USDT`"
        )
        await self.send(msg)

    async def notify_error(self, error_msg: str):
        msg = f"⚠️ *系統警告*\n`{error_msg}`"
        await self.send(msg)
