"""Claude Code plugin: hub /api/tools endpoints and the stdio MCP server."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from tvremote.assistant import TOOLS
from tvremote.fake import FakeTV
from tvremote.hub import create_app
from tvremote.samsung import SamsungTV

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "claude-plugin"
SERVER = PLUGIN / "server" / "tv_mcp.py"
KEY = "test-access-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def fake():
    loop = asyncio.new_event_loop()
    tv = loop.run_until_complete(FakeTV().start())
    threading.Thread(target=loop.run_forever, daemon=True).start()
    yield tv
    asyncio.run_coroutine_threadsafe(tv.stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def make_app(fake, tmp_path):
    tv = SamsungTV("127.0.0.1", port=fake.ws_port, info_port=fake.info_port, secure=False, mac=fake.mac)
    return create_app(tv=tv, access_key=KEY, config_path=tmp_path / "tv.json")


@pytest.fixture
def client(fake, tmp_path):
    with TestClient(make_app(fake, tmp_path)) as c:
        yield c


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_snapshot_matches_hub_tools():
    snap = json.loads((PLUGIN / "server" / "tools.json").read_text())
    live = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in TOOLS]
    assert snap == live, "regenerate claude-plugin/server/tools.json (see claude-plugin/README.md)"


def test_manifests_are_valid_json():
    for p in [ROOT / ".claude-plugin/marketplace.json", PLUGIN / ".claude-plugin/plugin.json", PLUGIN / ".mcp.json"]:
        json.loads(p.read_text())


def test_tools_need_auth(client):
    assert client.get("/api/tools").status_code == 401
    assert client.post("/api/tools/get_status", json={}).status_code == 401


def test_list_tools_includes_notes(client, tmp_path):
    (tmp_path / "notes.md").write_text("- captions on\n")
    body = client.get("/api/tools", headers=AUTH).json()
    assert {t["name"] for t in body["tools"]} == {t["name"] for t in TOOLS}
    assert "captions on" in body["notes"]


def test_call_tool(client, fake):
    r = client.post("/api/tools/send_keys", headers=AUTH,
                    json={"keys": [{"key": "volup", "repeat": 2}], "delay_ms": 50})
    assert r.status_code == 200 and r.json()["result"] == "pressed 2 keys"
    assert wait_for(lambda: fake.keys == ["KEY_VOLUP", "KEY_VOLUP"])


def test_call_tool_errors(client):
    assert client.post("/api/tools/nope", headers=AUTH, json={}).status_code == 404
    assert client.post("/api/tools/send_keys", headers=AUTH, json={"bogus": 1}).status_code == 422
    assert client.post("/api/tools/send_keys", headers=AUTH, json=[1]).status_code == 422


def test_remember_and_macros_persist(client, tmp_path):
    assert client.post("/api/tools/remember", headers=AUTH, json={"note": "likes subtitles"}).status_code == 200
    assert "likes subtitles" in (tmp_path / "notes.md").read_text()
    steps = [{"key": "KEY_HOME"}, {"wait": 0.1}]
    r = client.post("/api/tools/save_macro", headers=AUTH, json={"name": "go_home", "description": "home", "steps": steps})
    assert "saved" in r.json()["result"]
    assert "go_home" in json.loads((tmp_path / "macros.json").read_text())


# --------------------------------------------------------------- MCP server

@pytest.fixture
def hub_url(fake, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make_app(fake, tmp_path), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    assert wait_for(lambda: server.started, 10)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


def mcp(env: dict[str, str], *msgs: dict) -> list[dict]:
    lines = "".join(json.dumps(m) + "\n" for m in msgs)
    full_env = {**os.environ, **env}
    out = subprocess.run([sys.executable, str(SERVER)], input=lines, capture_output=True, text=True,
                         env=full_env, timeout=60, check=True).stdout
    return [json.loads(l) for l in out.splitlines()]


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
INITED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def test_mcp_end_to_end(hub_url, fake, tmp_path):
    keyfile = tmp_path / "hub.key"
    keyfile.write_text(KEY + "\n")
    env = {"TVREMOTE_HUB_URL": hub_url, "TVREMOTE_HUB_KEY_FILE": str(keyfile)}
    init, tools, call, notes = mcp(env, INIT, INITED,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "send_keys", "arguments": {"keys": [{"key": "KEY_MUTE", "repeat": 1}]}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_notes", "arguments": {}}})
    assert init["result"]["serverInfo"]["name"] == "tv-remote"
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"play_youtube", "open_twitch", "save_macro", "get_notes"} <= names
    assert all("inputSchema" in t for t in tools["result"]["tools"])
    assert call["result"] == {"content": [{"type": "text", "text": "pressed 1 keys"}], "isError": False}
    assert wait_for(lambda: fake.keys == ["KEY_MUTE"])
    assert "Samsung" in notes["result"]["content"][0]["text"]


def test_mcp_bad_key_and_offline_hub(hub_url, tmp_path):
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_status", "arguments": {}}}
    (_, r) = mcp({"TVREMOTE_HUB_URL": hub_url, "TVREMOTE_HUB_KEY": "wrong"}, INIT, call)
    assert r["result"]["isError"] and "401" in r["result"]["content"][0]["text"]
    # Hub unreachable: tools still listed (from the snapshot), calls explain why they fail.
    env = {"TVREMOTE_HUB_URL": "http://127.0.0.1:9", "TVREMOTE_HUB_KEY": KEY}
    (_, tools, r) = mcp(env, INIT, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, call)
    assert len(tools["result"]["tools"]) == len(TOOLS) + 1
    assert r["result"]["isError"] and "can't reach the hub" in r["result"]["content"][0]["text"]


def test_mcp_missing_key(tmp_path):
    env = {"TVREMOTE_HUB_URL": "http://127.0.0.1:9", "TVREMOTE_HUB_KEY": "",
           "TVREMOTE_HUB_KEY_FILE": str(tmp_path / "none")}
    (_, r) = mcp(env, INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "get_status", "arguments": {}}})
    assert "No hub key" in r["result"]["content"][0]["text"]
