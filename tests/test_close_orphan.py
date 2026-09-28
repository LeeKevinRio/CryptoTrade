"""孤兒倉平倉端點：只平「不屬於任何 bot」的交易所部位"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from src.web.app import create_app
from src.web.state import state

TOKEN = "test-token-123"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("DASHBOARD_AUTH", "false")
    api = MagicMock()
    api.get_open_positions = AsyncMock(return_value=[
        {"symbol": "HEMIUSDT", "side": "LONG", "quantity": 96561.0, "entry_price": 0.01, "leverage": 25},
    ])
    api.cancel_all_orders = AsyncMock()
    api.futures_market_order = AsyncMock(return_value={"orderId": 77, "avgPrice": "0.0061"})
    api.get_account_trades = AsyncMock(return_value=[])
    monkeypatch.setattr(state, "api_ref", api)
    monkeypatch.setattr(state, "bots", {})
    return TestClient(create_app(tracker=None)), api


def test_closes_orphan_with_reduce_only_market(client):
    c, api = client
    r = c.post("/api/diag/close_orphan/hemiusdt", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["order_id"] == 77
    kw = api.futures_market_order.call_args.kwargs
    assert kw["side"] == "SELL" and kw["quantity"] == 96561.0 and kw["reduce_only"] is True
    api.cancel_all_orders.assert_called_once_with("HEMIUSDT")


def test_unknown_symbol_404(client):
    c, _ = client
    r = c.post("/api/diag/close_orphan/SOLUSDT", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 404


def test_requires_token(client):
    c, api = client
    r = c.post("/api/diag/close_orphan/HEMIUSDT")
    assert r.status_code == 401
    api.futures_market_order.assert_not_called()


def test_refuses_bot_managed_symbol(client, monkeypatch):
    c, api = client
    bot = MagicMock(); bot.position_manager.has_position = lambda s: s == "HEMIUSDT"
    bs = MagicMock(bot_ref=bot, bot_id="futures")
    monkeypatch.setattr(state, "bots", {"futures": bs})
    r = c.post("/api/diag/close_orphan/HEMIUSDT", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 409
    api.futures_market_order.assert_not_called()
