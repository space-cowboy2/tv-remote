import asyncio

import pytest
from fastapi.testclient import TestClient

from tvremote.fake import FakeTV
from tvremote.hub import create_app
from tvremote.samsung import SamsungTV

KEY = "test-access-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def fake():
    loop = asyncio.new_event_loop()
    tv = loop.run_until_complete(FakeTV().start())
    import threading
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield tv
    asyncio.run_coroutine_threadsafe(tv.stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture
def client(fake, tmp_path):
    tv = SamsungTV("127.0.0.1", port=fake.ws_port, info_port=fake.info_port, secure=False, mac=fake.mac)
    app = create_app(tv=tv, access_key=KEY, config_path=tmp_path / "tv.json")
    with TestClient(app) as c:
        yield c


def wait_for(cond, timeout=2.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_health_is_public(client):
    assert client.get("/api/health").json()["ok"] is True


@pytest.mark.parametrize("method,path", [
    ("get", "/api/status"), ("get", "/api/apps"), ("post", "/api/key"), ("post", "/api/power"),
])
def test_requires_auth(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_login_sets_cookie_and_limits_attempts(client):
    for _ in range(5):
        assert client.post("/api/login", json={"key": "nope"}).status_code == 401
    assert client.post("/api/login", json={"key": KEY}).status_code == 429


def test_login_cookie_works(client):
    r = client.post("/api/login", json={"key": KEY})
    assert r.status_code == 200
    assert client.get("/api/status").status_code == 200


def test_key_press(client, fake):
    assert client.post("/api/key", json={"key": "volup"}, headers=AUTH).status_code == 200
    assert wait_for(lambda: fake.keys == ["KEY_VOLUP"])


def test_rejects_unknown_key(client):
    assert client.post("/api/key", json={"key": "KEY_SELF_DESTRUCT"}, headers=AUTH).status_code == 422


def test_apps_and_launch(client, fake):
    apps = client.get("/api/apps", headers=AUTH).json()
    assert [a["name"] for a in apps] == ["Netflix", "YouTube"]
    assert client.post("/api/launch", json={"app_id": "3201907018807"}, headers=AUTH).status_code == 200
    assert client.post("/api/launch", json={"app_id": "bad id!"}, headers=AUTH).status_code == 422


def test_text(client, fake):
    assert client.post("/api/text", json={"text": "hi"}, headers=AUTH).status_code == 200
    assert wait_for(lambda: any(m["params"].get("DataOfCmd") == "base64" for m in fake.received))


def test_power_cycle(client, fake):
    assert client.get("/api/status", headers=AUTH).json()["power"] == "on"
    assert client.post("/api/power", json={"state": "off"}, headers=AUTH).json()["ok"]
    assert wait_for(lambda: fake.power == "standby")
    r = client.post("/api/power", json={"state": "on"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["power"] == "on"


def test_status_when_tv_off(client, fake):
    fake.power = "off"
    assert client.get("/api/status", headers=AUTH).json()["power"] == "off"


def test_websocket(client, fake):
    with client.websocket_connect("/api/ws", headers=AUTH) as ws:
        ws.send_json({"key": "KEY_UP", "id": 1})
        assert ws.receive_json() == {"ok": True, "id": 1}
        ws.send_json({"key": "KEY_BOGUS", "id": 2})
        assert ws.receive_json()["ok"] is False
    assert wait_for(lambda: fake.keys == ["KEY_UP"])


def test_websocket_rejects_without_key(client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws") as ws:
            ws.receive_json()


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cache-Control"] == "no-store"


def test_cookie_does_not_contain_key(client):
    r = client.post("/api/login", json={"key": KEY})
    cookie = r.cookies.get("tvremote_session")
    assert cookie and KEY not in cookie


def test_forged_cookie_rejected(client):
    client.cookies.set("tvremote_session", KEY)
    assert client.get("/api/status").status_code == 401


def test_websocket_accepts_session_cookie(client):
    client.post("/api/login", json={"key": KEY})
    with client.websocket_connect("/api/ws") as ws:
        ws.send_json({"key": "KEY_DOWN", "id": 7})
        assert ws.receive_json() == {"ok": True, "id": 7}


def test_secure_cookie_flag(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("TVREMOTE_SECURE_COOKIE", "1")
    tv = SamsungTV("127.0.0.1", port=fake.ws_port, info_port=fake.info_port, secure=False)
    with TestClient(create_app(tv=tv, access_key=KEY, config_path=tmp_path / "tv.json")) as c:
        r = c.post("/api/login", json={"key": KEY})
        assert "secure" in r.headers["set-cookie"].lower()
