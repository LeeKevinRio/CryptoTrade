"""Crypto Fear & Greed Index 提供者

資料源：alternative.me 免費 API（無需金鑰）
    https://api.alternative.me/fng/?limit=1
回傳 0-100 的市場情緒指數與分類文字。
"""

import aiohttp

from src.utils.logger import setup_logger

logger = setup_logger("fear_greed")

FNG_URL = "https://api.alternative.me/fng/"


async def fetch_fear_greed(timeout: float = 6.0) -> tuple[int, str] | None:
    """抓取當前 Fear & Greed Index。

    Returns:
        (value 0-100, classification) 或 None（失敗時）
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                FNG_URL, params={"limit": "1"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    logger.warning("F&G API 回應 %s", resp.status)
                    return None
                payload = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001 — 網路層任何錯誤都退回 None，不阻斷主迴圈
        logger.warning("F&G 抓取失敗：%s", e)
        return None

    data = (payload or {}).get("data") or []
    if not data:
        return None
    return _parse_item(data[0])


async def fetch_fear_greed_history(days: int = 180, timeout: float = 10.0) -> dict[str, int]:
    """抓取歷史 F&G，回傳 {YYYY-MM-DD: value}。

    供回測情緒回放使用：回測若不餵情緒，sentiment 權重永遠拿不到分，
    回測與實盤的評分行為會有系統性落差。
    """
    from datetime import datetime, timezone

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                FNG_URL, params={"limit": str(days)},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    logger.warning("F&G 歷史 API 回應 %s", resp.status)
                    return {}
                payload = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        logger.warning("F&G 歷史抓取失敗：%s", e)
        return {}

    out: dict[str, int] = {}
    for item in (payload or {}).get("data") or []:
        try:
            ts = int(item["timestamp"])
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            out[day] = max(0, min(100, int(item["value"])))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _parse_item(item: dict) -> tuple[int, str] | None:
    try:
        value = int(item["value"])
        label = str(item.get("value_classification", "")).strip()
        return max(0, min(100, value)), label
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("F&G 解析失敗：%s", e)
        return None
