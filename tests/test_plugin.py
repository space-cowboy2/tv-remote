"""The Claude plugin's stdio MCP server against a real hub process and a simulated TV."""

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

from tvremote import tools as tools_mod
from tvremote.fake import FakeTV
from tvremote.hub import create_app
from tvremote.samsung import SamsungTV

from sim import Internet

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "claude-plugin"
SERVER = PLUGIN / "server" / "tv_mcp.py"
KEY = "test-access-key"


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_snapshots_match_the_hub():
    """The plugin lists tools from its own copy when the hub is down; keep the copies exact."""
    assert (PLUGIN / "server" / "tools.json").read_text(encoding="utf-8") == (ROOT / "tvremote" / "tools.json").read_text(encoding="utf-8"), \
        "copy tvremote/tools.json to claude-plugin/server/tools.json"
    assert (PLUGIN / "server" / "guide.md").read_text(encoding="utf-8") == (ROOT / "tvremote" / "guide.md").read_text(encoding="utf-8"), \
        "copy tvremote/guide.md to claude-plugin/server/guide.md"


def test_manifests_are_valid_json():
    for p in [ROOT / ".claude-plugin/marketplace.json", PLUGIN / ".claude-plugin/plugin.json", PLUGIN / ".mcp.json"]:
        json.loads(p.read_text())


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A hub served over real HTTP, with its simulated TV on a virtual clock."""
    loop = asyncio.new_event_loop()
    fake = loop.run_until_complete(FakeTV().start())
    threading.Thread(target=loop.run_forever, daemon=True).start()
    net = Internet()
    fake.youtube.catalog = {v["id"]: v["title"] for v in net.videos}

    async def jump(self, seconds):
        await asyncio.sleep(0.02)
        fake.clock += seconds
        await asyncio.sleep(0.005)
    monkeypatch.setattr(tools_mod.Tools, "sleep", jump)
    monkeypatch.setattr(tools_mod.youtube, "search", net.search)
    monkeypatch.setattr(tools_mod.youtube, "lookup", net.lookup)
    tv = SamsungTV("127.0.0.1", port=fake.ws_port, info_port=fake.info_port, dial_port=fake.info_port,
                   upnp_port=fake.info_port, secure=False, mac=fake.mac)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(tv=tv, access_key=KEY, config_path=tmp_path / "tv.json")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    assert wait_for(lambda: server.started, 10)
    fake.url = f"http://127.0.0.1:{port}"
    yield fake
    server.should_exit = True
    t.join(5)
    asyncio.run_coroutine_threadsafe(fake.stop(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def mcp(env: dict[str, str], *msgs: dict) -> list[dict]:
    lines = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs)
    out = subprocess.run([sys.executable, str(SERVER)], input=lines.encode("utf-8"), capture_output=True,
                         env={**os.environ, **env}, timeout=120, check=True).stdout
    return [json.loads(line) for line in out.decode("utf-8").splitlines()]


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
INITED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def call(i, name, **args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}


def test_a_whole_conversation_through_the_plugin(hub, tmp_path):
    keyfile = tmp_path / "hub.key"
    keyfile.write_text(KEY + "\n")
    env = {"TVREMOTE_HUB_URL": hub.url, "TVREMOTE_HUB_KEY_FILE": str(keyfile)}
    init, tools, guide, play, quieter, bad = mcp(
        env, INIT, INITED, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        call(3, "get_guide"), call(4, "play", query="jazz music", prefer="long"),
        call(5, "set_volume", change=-3), call(6, "set_volume", level=500))
    assert init["result"]["serverInfo"]["name"] == "tv-remote"
    # Short enough to survive behind a gateway that puts its own instructions first.
    assert "get_guide" in init["result"]["instructions"] and len(init["result"]["instructions"]) < 500
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"play", "set_volume", "get_state", "schedule", "get_guide"} <= names
    assert all("inputSchema" in t for t in tools["result"]["tools"])
    text = guide["result"]["content"][0]["text"]
    assert "## Saying what happened" in text and "This household's notes" in text
    report = json.loads(play["result"]["content"][0]["text"])
    assert play["result"]["isError"] is False and report["playing"]["id"] == "Dx5qFachd3A"
    assert hub.youtube.playing == "Dx5qFachd3A"
    assert json.loads(quieter["result"]["content"][0]["text"])["status"] == "verified" and hub.volume == 9
    refused = json.loads(bad["result"]["content"][0]["text"])
    assert bad["result"]["isError"] and refused["failed"]["kind"] == "bad_request" and "level" in refused["failed"]["why"]


def test_text_that_is_not_ascii_survives_the_pipe(hub):
    env = {"TVREMOTE_HUB_URL": hub.url, "TVREMOTE_HUB_KEY": KEY}
    (_, saved, guide) = mcp(env, INIT, call(2, "remember", note="Björk and 坂本龍一 in the evening"), call(3, "get_guide"))
    assert saved["result"]["isError"] is False
    assert "Björk and 坂本龍一 in the evening" in guide["result"]["content"][0]["text"]


def test_a_wrong_key_and_a_hub_that_is_down(hub):
    (_, r) = mcp({"TVREMOTE_HUB_URL": hub.url, "TVREMOTE_HUB_KEY": "wrong"}, INIT, call(2, "get_state"))
    assert r["result"]["isError"] and json.loads(r["result"]["content"][0]["text"])["failed"]["kind"] == "hub_rejected_key"
    # Hub unreachable: the tools are still listed, and a call says what is wrong and that nothing was sent.
    env = {"TVREMOTE_HUB_URL": "http://127.0.0.1:9", "TVREMOTE_HUB_KEY": KEY}
    (_, tools, r, guide) = mcp(env, INIT, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                               call(4, "play", query="jazz"), call(5, "get_guide"))
    assert len(tools["result"]["tools"]) == len(tools_mod.SCHEMAS) + 1
    report = json.loads(r["result"]["content"][0]["text"])
    assert r["result"]["isError"] and report["failed"]["kind"] == "hub_unreachable" and report["failed"]["needs_user"]
    assert "Nothing was sent to the TV" in report["failed"]["hint"]
    assert "## Which TV" in guide["result"]["content"][0]["text"] and "not answering" in guide["result"]["content"][0]["text"]


def test_a_missing_key_file(tmp_path):
    env = {"TVREMOTE_HUB_URL": "http://127.0.0.1:9", "TVREMOTE_HUB_KEY": "", "TVREMOTE_HUB_KEY_FILE": str(tmp_path / "none")}
    (_, r, unknown) = mcp(env, INIT, call(2, "get_state"), {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert json.loads(r["result"]["content"][0]["text"])["failed"]["kind"] == "no_hub_key"
    assert unknown["error"]["code"] == -32601
