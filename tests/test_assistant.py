from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from tvremote import assistant as asst
from tvremote.hub import create_app
from tvremote.samsung import SamsungTV

from test_hub import AUTH, KEY, fake, wait_for  # noqa: F401  (fixture)


class FakeClaude:
    """Plays back scripted responses and records the requests."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return self.script.pop(0)


def tool(name, **inp):
    return NS(type="tool_use", id=f"tu_{name}", name=name, input=inp)


def text(t):
    return NS(type="text", text=t)


def resp(*content, stop="end_turn"):
    return NS(content=list(content), stop_reason=stop)


@pytest.fixture
def make_client(fake, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(asst, "DEFAULT_MACROS", {"twitch_channel": {"steps": [
        {"launch": asst.TWITCH}, {"key": "KEY_LEFT"}, {"text": "{value}", "submit": True}, {"key": "KEY_ENTER"}]}})
    fake.apps.append({"appId": asst.TWITCH, "name": "Twitch", "app_type": 2})

    def make(claude=None):
        tv = SamsungTV("127.0.0.1", port=fake.ws_port, info_port=fake.info_port, secure=False, mac=fake.mac)
        return TestClient(create_app(tv=tv, access_key=KEY, config_path=tmp_path / "tv.json",
                                     assistant_client=claude))
    return make


def test_not_configured(make_client):
    with make_client() as c:
        assert c.get("/api/assistant", headers=AUTH).json() == {"enabled": False}
        r = c.post("/api/assistant", headers=AUTH, json={"text": "open netflix"})
        assert r.status_code == 503


def test_requires_auth(make_client):
    with make_client(FakeClaude([])) as c:
        assert c.post("/api/assistant", json={"text": "hi"}).status_code == 401


def test_keys_then_reply(make_client, fake):  # noqa: F811
    claude = FakeClaude([
        resp(tool("send_keys", keys=[{"key": "volup", "repeat": 3}], delay_ms=10), stop="tool_use"),
        resp(text("Turned the volume up 3.")),
    ])
    with make_client(claude) as c:
        r = c.post("/api/assistant", headers=AUTH, json={"text": "volume up 3"})
        assert r.status_code == 200
        assert r.json()["reply"] == "Turned the volume up 3."
        assert wait_for(lambda: fake.keys.count("KEY_VOLUP") == 3)
    req = claude.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["fallbacks"] == "default"
    tool_result = claude.requests[1]["messages"][-1]["content"][0]
    assert tool_result["content"] == "pressed 3 keys" and not tool_result["is_error"]


def test_twitch_macro(make_client, fake):  # noqa: F811
    claude = FakeClaude([
        resp(tool("open_twitch", channel="@OhnePixel"), stop="tool_use"),
        resp(text("Searched Twitch for ohnepixel.")),
    ])
    with make_client(claude) as c:
        r = c.post("/api/assistant", headers=AUTH, json={"text": "open ohnepixel's twitch stream"})
        assert r.status_code == 200
        assert asst.TWITCH in fake.launched
        assert wait_for(lambda: "KEY_ENTER" in fake.keys)
        typed = [m for m in fake.received if m.get("params", {}).get("TypeOfRemote") == "SendInputString"]
        assert typed  # channel name typed
        assert "not visually confirmed" in r.json()["actions"][0]


def test_youtube_deep_link(make_client, fake):  # noqa: F811
    claude = FakeClaude([
        resp(tool("play_youtube", video_id="dQw4w9WgXcQ"), stop="tool_use"),
        resp(text("Playing it on YouTube.")),
    ])
    with make_client(claude) as c:
        assert c.post("/api/assistant", headers=AUTH, json={"text": "rick roll"}).status_code == 200
        assert wait_for(lambda: f"{asst.YOUTUBE}#v=dQw4w9WgXcQ" in fake.launched)


def test_bad_key_is_reported_not_sent(make_client, fake):  # noqa: F811
    claude = FakeClaude([
        resp(tool("send_keys", keys=[{"key": "KEY_SELFDESTRUCT", "repeat": 1}], delay_ms=10), stop="tool_use"),
        resp(text("That key doesn't exist.")),
    ])
    with make_client(claude) as c:
        c.post("/api/assistant", headers=AUTH, json={"text": "x"})
    assert "unknown keys" in claude.requests[1]["messages"][-1]["content"][0]["content"]


def test_history_carries_followups(make_client):
    claude = FakeClaude([resp(text("Opened Netflix.")), resp(text("Back home."))])
    with make_client(claude) as c:
        c.post("/api/assistant", headers=AUTH, json={"text": "open netflix"})
        c.post("/api/assistant", headers=AUTH, json={"text": "never mind"})
    msgs = claude.requests[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == "Opened Netflix."
