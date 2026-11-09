import pytest
from fastapi.testclient import TestClient

from tvremote import tools as tools_mod
from tvremote.hub import create_app
from tvremote.samsung import SamsungTV
from tvremote.tools import youtube_query

from test_hub import AUTH, KEY, fake, wait_for  # noqa: F401  (fake is a fixture)


@pytest.fixture
def client(fake, tmp_path, monkeypatch):
    async def no_sleep(self, s):
        pass
    monkeypatch.setattr(tools_mod.Tools, "sleep", no_sleep)
    tv = SamsungTV("127.0.0.1", port=fake.ws_port, info_port=fake.info_port, secure=False, mac=fake.mac)
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
    assert names == {"get_status", "set_power", "list_apps", "launch_app", "play_youtube", "run_macro", "list_macros",
                     "save_macro", "remember", "skip_ad", "open_twitch", "send_keys", "type_text", "wait"}
    assert all(hasattr(tools_mod.Tools, "t_" + n) for n in names)
    assert body["notes"] == ""


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


def test_play_youtube_runs_editable_macro(client, fake):
    assert call(client, "play_youtube", video="https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3").status_code == 200
    assert wait_for(lambda: "111299001912" in fake.launched and fake.keys[-1] == "KEY_ENTER")
    # a recalibrated macro replaces the default
    call(client, "save_macro", name="youtube_search", description="fixed", steps=[{"key": "KEY_HOME"}])
    n = len(fake.keys)
    call(client, "play_youtube", video="lofi")
    assert wait_for(lambda: fake.keys[n:] == ["KEY_HOME"])


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
