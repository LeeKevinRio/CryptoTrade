"""公網模式整站認證閘門測試"""

import pytest
from fastapi.testclient import TestClient

from src.web.app import create_app, AUTH_COOKIE

TOKEN = "test-token-123"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("DASHBOARD_AUTH", "true")
    return TestClient(create_app(tracker=None))


@pytest.fixture
def client_no_auth(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("DASHBOARD_AUTH", "false")
    return TestClient(create_app(tracker=None))


def test_healthz_exempt(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_healthz_trailing_slash_exempt(client):
    # 監測服務（UptimeRobot 等）可能自動補尾斜線，不可被閘門擋成 401
    r = client.get("/healthz/", follow_redirects=True)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_healthz_head_request(client):
    # UptimeRobot 部分監測模式用 HEAD
    assert client.head("/healthz").status_code == 200


def test_blocked_without_token(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/").status_code == 401


def test_wrong_token_blocked(client):
    assert client.get("/api/status", params={"token": "wrong"}).status_code == 401
    assert client.get("/api/status", headers={"X-Auth-Token": "wrong"}).status_code == 401


def test_query_token_passes_and_sets_cookie(client):
    r = client.get("/api/status", params={"token": TOKEN})
    assert r.status_code == 200
    assert r.cookies.get(AUTH_COOKIE) == TOKEN
    # cookie 已種下，後續請求不帶 token 也能通過
    assert client.get("/api/status").status_code == 200


def test_header_token_passes(client):
    r = client.get("/api/status", headers={"X-Auth-Token": TOKEN})
    assert r.status_code == 200


def test_webhook_keeps_own_token_check(client):
    # webhook 豁免整站閘門，但自身 token 驗證仍生效
    r = client.post("/webhook/tradingview/wrong-token", json={})
    assert r.status_code == 401


def test_websocket_rejected_without_token(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_websocket_accepts_query_token(client):
    with client.websocket_connect(f"/ws?token={TOKEN}"):
        pass


def test_diag_endpoint(client):
    r = client.get("/api/diag", params={"token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert "engine" in body and "probes" in body
    # 引擎未啟動時要優雅回報而非爆炸
    assert body["probes"].get("note")


def test_disabled_mode_keeps_open_dashboard(client_no_auth):
    # 預設（本機）行為不變：GET 免認證
    assert client_no_auth.get("/api/status").status_code == 200
    # 但 POST 動作端點仍要 token
    r = client_no_auth.post("/api/bots/x/actions/pause")
    assert r.status_code == 401
