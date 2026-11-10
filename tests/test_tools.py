"""The agent tools over HTTP, as the plugin and Duet call them (simulated TV)."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from tvremote import tools as tools_mod
from tvremote.config import TVConfig
from tvremote.hub import create_app
from tvremote.samsung import SamsungTV
from tvremote.tools import youtube_query

from sim import Internet
from test_hub import AUTH, KEY, fake, wait_for  # noqa: F401  (fake is a fixture)


@pytest.fixture
def client(fake, tmp_path, monkeypatch):
    async def jump(self, seconds):  # virtual time: the simulated TV's clock moves, the test does not wait
        await asyncio.sleep(0.02)
        fake.clock += seconds
        await asyncio.sleep(0.005)
    monkeypatch.setattr(tools_mod.Tools, "sleep", jump)
    net = Internet()
    monkeypatch.setattr(tools_mod.youtube, "search", net.search)
    monkeypatch.setattr(tools_mod.youtube, "lookup", net.lookup)
    fake.youtube.catalog = {v["id"]: v["title"] for v in net.videos}
    tv = SamsungTV("127.0.0.1", port=fake.ws_port, info_port=fake.info_port, dial_port=fake.info_port,
                   upnp_port=fake.info_port, secure=False, mac=fake.mac)
    with TestClient(create_app(tv=tv, access_key=KEY, config_path=tmp_path / "tv.json")) as c:
        c.dir = tmp_path
        yield c


def call(client, _tool, **args):
    return client.post(f"/api/tools/{_tool}", json=args, headers=AUTH)


def test_tools_need_auth(client):
    assert client.get("/api/tools").status_code == 401
    assert client.post("/api/tools/get_status", json={}).status_code == 401


def test_list_matches_plugin_contract(client):
    body = client.get("/api/tools", headers=AUTH).json()
    names = {t["name"] for t in body["tools"]}
    assert names == {"get_state", "find_tvs", "pair_tv", "use_tv", "set_power", "set_volume", "play", "search_youtube",
                     "open_app", "list_apps", "playback", "switch_input", "schedule", "cancel_schedule", "skip_ad",
                     "open_twitch", "send_keys", "type_text", "wait", "run_macro", "list_macros", "save_macro",
                     "remember"}
    assert all(hasattr(tools_mod.Tools, "t_" + n) for n in names)
    assert all(t["description"] and t["input_schema"]["type"] == "object" for t in body["tools"])
    assert body["notes"] == "" and "## Saying what happened" in body["guide"]
    # what older clients call still answers, without being advertised
    for old in ("get_status", "launch_app", "play_youtube"):
        assert old not in names and hasattr(tools_mod.Tools, "t_" + old)
    # everything that can be scheduled is a real tool
    assert all(hasattr(tools_mod.Tools, "t_" + n) for n in tools_mod.SCHEDULABLE)


def test_unknown_tool_and_bad_args(client):
    assert call(client, "self_destruct").status_code == 404
    assert call(client, "send_keys", keys=[{"key": "KEY_NOPE", "repeat": 1}], delay_ms=0).status_code == 422
    assert call(client, "send_keys", keys=[{"key": "KEY_UP", "repeat": 999}], delay_ms=0).status_code == 422
    assert call(client, "wait", seconds=99).status_code == 422
    assert call(client, "save_macro", name="Bad Name", description="", steps=[{"key": "KEY_UP"}]).status_code == 422
    assert call(client, "save_macro", name="ok_name", description="", steps=[{"key": "KEY_UP", "wait": 1}]).status_code == 422


def test_status_keys_text_launch(client, fake):
    assert '"power"' in call(client, "get_status").json()["result"]
    assert call(client, "send_keys", keys=[{"key": "volup", "repeat": 3}, {"key": "KEY_MUTE", "repeat": 1}], delay_ms=0).status_code == 200
    assert wait_for(lambda: fake.keys == ["KEY_VOLUP"] * 3 + ["KEY_MUTE"])
    assert call(client, "type_text", text="hello", submit=True).status_code == 200
    assert call(client, "launch_app", app_id="3201907018807").status_code == 200
    assert wait_for(lambda: "3201907018807" in fake.launched)
    assert "YouTube" in call(client, "list_apps").json()["result"]


def test_notes_and_macros_persist(client, fake):
    call(client, "remember", note="Danny  likes\nvolume 12")
    assert client.get("/api/tools", headers=AUTH).json()["notes"] == "- Danny likes volume 12\n"
    assert call(client, "save_macro", name="vol_reset", description="x",
                steps=[{"key": "KEY_VOLDOWN", "repeat": 2}, {"wait": 1}, {"key": "KEY_VOLUP"}]).status_code == 200
    assert "vol_reset" in call(client, "list_macros").json()["result"]
    assert (client.dir / "macros.json").exists()
    assert call(client, "run_macro", name="vol_reset", value="").status_code == 200
    assert wait_for(lambda: fake.keys == ["KEY_VOLDOWN", "KEY_VOLDOWN", "KEY_VOLUP"])
    assert call(client, "run_macro", name="missing", value="").status_code == 422


def test_play_over_http_reports_evidence(client, fake):
    r = call(client, "play", query="jazz music", prefer="long")
    assert r.status_code == 200 and r.json()["ok"] is True
    report = json.loads(r.json()["result"])
    assert report["status"] == "partly_verified" and report["playing"]["id"] == "Dx5qFachd3A"
    assert wait_for(lambda: fake.youtube.playing == "Dx5qFachd3A")
    # a failed outcome is still HTTP 200: the answer is the report
    fake.apps[:] = [a for a in fake.apps if a["name"] != "YouTube"]
    r = call(client, "play", query="relaxing piano")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert json.loads(r.json()["result"])["failed"]["kind"] == "app_not_installed"


def test_play_youtube_runs_a_saved_all_in_one_macro(client, fake):
    call(client, "save_macro", name="youtube_search", description="fixed", steps=[{"key": "KEY_HOME"}])
    assert call(client, "play_youtube", video="https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3").status_code == 200
    assert wait_for(lambda: fake.keys == ["KEY_HOME"])


def test_hub_runs_before_any_tv_is_paired_and_picks_one_up(fake, tmp_path):
    args = {"port": fake.ws_port, "info_port": fake.info_port, "dial_port": fake.info_port,
            "upnp_port": fake.info_port, "secure": False}
    with TestClient(create_app(access_key=KEY, config_path=tmp_path / "tv.json", tv_args=args)) as c:
        assert len(c.get("/api/tools", headers=AUTH).json()["tools"]) > 20
        r = call(c, "set_volume", level=5)
        assert r.status_code == 200 and json.loads(r.json()["result"])["failed"]["kind"] == "no_tv_paired"
        assert c.get("/api/status", headers=AUTH).status_code == 503
        assert c.post("/api/key", json={"key": "KEY_UP"}, headers=AUTH).status_code == 503
        TVConfig(host="127.0.0.1", mac=fake.mac, token=fake.token, label="Den").save(tmp_path / "tv.json")
        r = call(c, "set_volume", level=5)  # paired from another window: no restart needed
        assert json.loads(r.json()["result"])["ok"] and fake.volume == 5
        assert c.get("/api/status", headers=AUTH).json()["power"] == "on"


def test_twitch_and_skip_ad(client, fake):
    fake.apps.append({"appId": "3202203026841", "name": "Twitch", "app_type": 2})
    assert call(client, "open_twitch", channel="bad name!").status_code == 422
    assert call(client, "open_twitch", channel="ohnepixel").status_code == 200
    assert wait_for(lambda: "3202203026841" in fake.launched)
    n = len(fake.keys)
    assert call(client, "skip_ad").status_code == 200
    assert wait_for(lambda: fake.keys[n:] == ["KEY_ENTER"])


@pytest.mark.parametrize("raw,want", [
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/shorts/abcdefghijk", "abcdefghijk"),
    ("  lofi hip hop  ", "lofi hip hop"),
])
def test_youtube_query(raw, want):
    assert youtube_query(raw) == want
