import json
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


def tool(name_, **inp):
    return NS(type="tool_use", id=f"tu_{name_}", name=name_, input=inp)


def text(t):
    return NS(type="text", text=t)


def resp(*content, stop="end_turn"):
    return NS(content=list(content), stop_reason=stop)


@pytest.fixture
def make_client(fake, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Same steps as the real macros, without the waits (tests run in seconds).
    fast = {name: {"description": m.get("description", ""),
                   "steps": [st for st in m["steps"] if "wait" not in st]}
            for name, m in asst.DEFAULT_MACROS.items()}
    monkeypatch.setattr(asst, "DEFAULT_MACROS", fast)
    monkeypatch.setattr(asst, "youtube_title", lambda vid: f"Title for {vid} | Live Set (4K) | Channel")
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


def typed(fake):  # noqa: F811
    import base64
    return [base64.b64decode(m["params"]["Cmd"]).decode() for m in fake.received
            if m.get("params", {}).get("TypeOfRemote") == "SendInputString"]


def test_youtube_plays_by_searching_natively(make_client, fake):  # noqa: F811
    claude = FakeClaude([
        resp(tool("play_youtube", video="https://youtu.be/dQw4w9WgXcQ?si=x"), stop="tool_use"),
        resp(text("Playing it on YouTube.")),
    ])
    with make_client(claude) as c:
        r = c.post("/api/assistant", headers=AUTH, json={"text": "play this"})
        assert r.status_code == 200
        assert asst.YOUTUBE in fake.launched  # launched over REST, no deep link
        assert wait_for(lambda: fake.keys[-1:] == ["KEY_ENTER"] and len(fake.keys) >= 5)
        assert fake.keys == ["KEY_HOME", "KEY_LEFT", "KEY_UP", "KEY_ENTER", "KEY_RIGHT", "KEY_ENTER"]
        assert typed(fake) == ["Title for dQw4w9WgXcQ Live Set 4K"]  # title trimmed for the TV search box
    assert "searched YouTube" in claude.requests[1]["messages"][-1]["content"][0]["content"]


def test_youtube_endpoint_takes_a_title(make_client, fake):  # noqa: F811
    with make_client(FakeClaude([])) as c:
        r = c.post("/api/youtube", headers=AUTH, json={"video": "lofi hip hop radio"})
        assert r.status_code == 200
        assert typed(fake) == ["lofi hip hop radio"]


def test_memory_persists(make_client, tmp_path):
    claude = FakeClaude([
        resp(tool("remember", note="Owner likes volume 12 at night."),
             tool("save_macro", name="netflix_search", description="Search in Netflix",
                  steps=[{"launch": "3201907018807"}, {"wait": 5}, {"key": "up", "repeat": 2},
                         {"text": "{value}", "submit": True}]), stop="tool_use"),
        resp(text("Noted.")),
        resp(text("ok")),
    ])
    with make_client(claude) as c:
        c.post("/api/assistant", headers=AUTH, json={"text": "remember stuff"})
        c.post("/api/assistant", headers=AUTH, json={"text": "hi"})
    assert "volume 12" in (tmp_path / "notes.md").read_text()
    saved = json.loads((tmp_path / "macros.json").read_text())
    assert saved["netflix_search"]["steps"][2] == {"key": "KEY_UP", "repeat": 2}
    assert "netflix_search" not in saved or "youtube_search" not in saved  # defaults aren't duplicated
    # the next request sees the note in the system prompt, and a fresh Memory reloads both
    assert "volume 12" in claude.requests[2]["system"]
    mem = asst.Memory(tmp_path)
    assert "netflix_search" in mem.macros and "youtube_search" in mem.macros


@pytest.mark.parametrize("text,vid", [
    ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=DPBt5qXBGhk", "DPBt5qXBGhk"),
    ("https://youtu.be/DPBt5qXBGhk?si=abc", "DPBt5qXBGhk"),
    ("https://youtube.com/shorts/DPBt5qXBGhk", "DPBt5qXBGhk"),
    ("play something", None),
])
def test_extract_video_id(text, vid):
    assert asst.extract_video_id(text) == vid


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


YT_PAGE = """<html><script nonce="x">var ytInitialData = {"contents":{"twoColumnSearchResultsRenderer":
{"primaryContents":{"sectionListRenderer":{"contents":[{"itemSectionRenderer":{"contents":[
{"channelRenderer":{"channelId":"UCx","title":{"simpleText":"Jazz Channel"}}},
{"videoRenderer":{"videoId":"Dx5qFachd3A","title":{"runs":[{"text":"Relaxing Jazz Piano Radio - 24/7 Live"}]},
 "ownerText":{"runs":[{"text":"Cafe Music BGM channel"}]},"viewCountText":{"runs":[{"text":"1,234"},{"text":" watching"}]},
 "badges":[{"metadataBadgeRenderer":{"style":"BADGE_STYLE_TYPE_LIVE_NOW","label":"LIVE"}}]}},
{"reelShelfRenderer":{"items":[]}},
{"videoRenderer":{"videoId":"neV3EPgvZ3g","title":{"runs":[{"text":"Smooth Jazz Mix | 3 Hours"}]},
 "longBylineText":{"runs":[{"text":"Jazz Vibes"}]},"lengthText":{"simpleText":"3:02:11"},
 "viewCountText":{"simpleText":"5,000,000 views"}}},
{"videoRenderer":{"videoId":"neV3EPgvZ3g","title":{"runs":[{"text":"dup"}]}}}
]}}]}}}}};</script><script>var other = 1;</script></html>"""


def test_parse_youtube_results():
    got = asst.parse_youtube_results(YT_PAGE)
    assert got == [
        {"id": "Dx5qFachd3A", "title": "Relaxing Jazz Piano Radio - 24/7 Live", "channel": "Cafe Music BGM channel",
         "length": None, "live": True, "views": "1,234 watching"},
        {"id": "neV3EPgvZ3g", "title": "Smooth Jazz Mix | 3 Hours", "channel": "Jazz Vibes",
         "length": "3:02:11", "live": False, "views": "5,000,000 views"},
    ]
    assert asst.parse_youtube_results("<html>consent page</html>") == []


def test_vague_youtube_ask_resolves_to_a_video(make_client, fake, monkeypatch):  # noqa: F811
    queries = []

    def search(q, limit=8):
        queries.append(q)
        return asst.parse_youtube_results(YT_PAGE, limit)
    monkeypatch.setattr(asst, "youtube_search", search)
    claude = FakeClaude([
        resp(tool("search_youtube", query="jazz music mix"), stop="tool_use"),
        resp(tool("play_youtube", video="neV3EPgvZ3g"), stop="tool_use"),
        resp(text("Playing Smooth Jazz Mix by Jazz Vibes.")),
    ])
    with make_client(claude) as c:
        r = c.post("/api/assistant", headers=AUTH, json={"text": "play some jazz on youtube"})
        assert r.status_code == 200
        assert queries == ["jazz music mix"]
        found = claude.requests[1]["messages"][-1]["content"][0]["content"]
        assert "neV3EPgvZ3g" in found and "3:02:11" in found
        assert wait_for(lambda: typed(fake) == ["Title for neV3EPgvZ3g Live Set 4K"])


def test_search_youtube_reports_network_errors(make_client, monkeypatch):
    def boom(q, limit=8):
        raise OSError("offline")
    monkeypatch.setattr(asst, "youtube_search", boom)
    with make_client(FakeClaude([])) as c:
        r = c.post("/api/tools/search_youtube", headers=AUTH, json={"query": "jazz"})
        assert r.status_code == 200 and "search failed" in r.text
