"""外部訊號資料模型"""

import time
from dataclasses import dataclass, field


VALID_ACTIONS = ("LONG", "SHORT", "CLOSE", "NEUTRAL")


@dataclass
class ExternalSignal:
    """來自 TradingView（或其他外部來源）的一則訊號。

    strength 0-100：由發送端決定信心度；未提供時預設 100（完全信任）。
    ttl_seconds：訊號有效期；過期後不再計入評分，避免用陳舊訊號下單。
    """

    symbol: str
    action: str                       # LONG / SHORT / CLOSE / NEUTRAL
    strength: float = 100.0
    source: str = "tradingview"
    note: str = ""
    indicators: dict = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)
    ttl_seconds: float = 900.0

    @property
    def age_seconds(self) -> float:
        return time.time() - self.received_at

    @property
    def expired(self) -> bool:
        return self.age_seconds > self.ttl_seconds

    @property
    def bias(self) -> float:
        """轉成 -1..+1 的方向分數，與 sentiment 的介面一致。"""
        if self.expired or self.action in ("CLOSE", "NEUTRAL"):
            return 0.0
        mag = max(0.0, min(100.0, self.strength)) / 100.0
        return mag if self.action == "LONG" else -mag

    def supports(self, side: str, threshold: float) -> bool:
        """此訊號是否足以支持指定方向（LONG/SHORT）。"""
        if self.expired:
            return False
        b = self.bias
        return b >= threshold if side == "LONG" else b <= -threshold

    def summary(self) -> str:
        parts = [f"{self.source}:{self.action}"]
        if self.strength != 100.0:
            parts.append(f"強度{self.strength:.0f}")
        if self.note:
            parts.append(self.note[:40])
        parts.append(f"{self.age_seconds:.0f}s前")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "strength": self.strength,
            "source": self.source,
            "note": self.note,
            "indicators": self.indicators,
            "bias": round(self.bias, 3),
            "age_seconds": round(self.age_seconds, 1),
            "expired": self.expired,
            "received_at": self.received_at,
        }


def parse_payload(payload: dict, default_ttl: float = 900.0) -> ExternalSignal:
    """把 TradingView alert 的 JSON 轉成 ExternalSignal。

    接受的欄位（皆大小寫不敏感）：
        symbol / ticker   必填
        action / side     必填：LONG|BUY / SHORT|SELL / CLOSE|EXIT / NEUTRAL
        strength          選填 0-100
        note / comment    選填
        indicators        選填 dict，保留你在 TradingView 算出的指標值
    """
    if not isinstance(payload, dict):
        raise ValueError("payload 必須是 JSON 物件")

    lower = {str(k).lower(): v for k, v in payload.items()}

    symbol = lower.get("symbol") or lower.get("ticker")
    if not symbol:
        raise ValueError("缺少 symbol/ticker")
    symbol = str(symbol).upper().replace(".P", "").replace("PERP", "")

    raw_action = lower.get("action") or lower.get("side")
    if not raw_action:
        raise ValueError("缺少 action/side")
    a = str(raw_action).strip().upper()
    action = {
        "BUY": "LONG", "LONG": "LONG",
        "SELL": "SHORT", "SHORT": "SHORT",
        "CLOSE": "CLOSE", "EXIT": "CLOSE", "FLAT": "CLOSE",
        "NEUTRAL": "NEUTRAL",
    }.get(a)
    if action is None:
        raise ValueError(f"不支援的 action: {raw_action}")

    try:
        strength = float(lower.get("strength", 100))
    except (TypeError, ValueError):
        strength = 100.0
    strength = max(0.0, min(100.0, strength))

    indicators = lower.get("indicators")
    if not isinstance(indicators, dict):
        indicators = {}

    try:
        ttl = float(lower.get("ttl_seconds", default_ttl))
    except (TypeError, ValueError):
        ttl = default_ttl

    return ExternalSignal(
        symbol=symbol,
        action=action,
        strength=strength,
        source=str(lower.get("source", "tradingview"))[:32],
        note=str(lower.get("note") or lower.get("comment") or "")[:200],
        indicators=indicators,
        ttl_seconds=ttl,
    )
