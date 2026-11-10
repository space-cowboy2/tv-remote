"""The live self-test's own logic, run against the simulated TV."""

from __future__ import annotations

from tvremote import selftest, youtube
from tvremote.tools import TVHandle


def by_check(checks):
    return {c["check"]: c for c in checks}


async def test_probe_of_an_unpaired_tv_reads_and_changes_nothing(rig, monkeypatch):
    fake = await rig.add_tv()
    monkeypatch.setattr(youtube, "search", lambda q, prefer="auto": [{"id": "x", "title": "Jazz Music"}])
    tv = rig.client(fake, token=False)
    before = (fake.volume, fake.muted, list(fake.keys), list(fake.clients))
    try:
        checks = by_check(await selftest.probe(TVHandle("probe", "TV", tv), app_endpoint=False))
    finally:
        await tv.close()
    assert checks["power"]["ok"] is True
    assert checks["remote-control channel"] == {"check": "remote-control channel", "ok": None, "detail": "not paired yet (no saved token)"}
    assert checks["YouTube state over the app endpoint"]["ok"] is None  # left out on purpose
    assert checks["YouTube state over DIAL"]["ok"] is True
    assert checks["volume and mute read-back"]["detail"] == f"volume {fake.volume}, muted {fake.muted}"
    assert checks["YouTube search from this computer"]["ok"] is True
    # no pairing prompt, no key, no change
    assert (fake.volume, fake.muted, list(fake.keys), list(fake.clients)) == before


async def test_probe_of_a_tv_that_is_off_stops_early(rig):
    fake = await rig.add_tv()
    fake.power = "standby"
    tv = rig.client(fake)
    try:
        checks = await selftest.probe(TVHandle("probe", "TV", tv))
    finally:
        await tv.close()
    assert [c["check"] for c in checks][-1] == "everything else"
    assert "FAIL" in selftest.render("t", checks) and "skipped: the TV is not on" in selftest.render("t", checks)


async def test_full_run_reports_each_step_and_puts_the_volume_back(rig):
    fake = await rig.add_tv()
    start = fake.volume
    steps = await selftest.full_run(rig.tools, rig.tools.tvs["living-room"], "jazz music", "long")
    names = [s["tool"] for s in steps]
    assert names == ["get_state", "set_volume", "set_volume", "play", "playback", "playback", "get_state"]
    assert fake.volume == start
    play = steps[3]["report"]
    assert play["ok"] and play["status"] == "partly_verified"  # never "verified": playback cannot be confirmed


def test_link_gateway_sets_only_the_tv_upstream(tmp_path):
    import json

    import pytest

    from tvremote.gatewaylink import LinkError, console_python, link

    plugin = tmp_path / "plugin" / "tv_mcp.py"
    plugin.parent.mkdir()
    plugin.write_text("# server")
    config = tmp_path / "config.json"
    original = {"port": 8787, "passphraseHash": "scrypt$aa$bb", "notes": ["Café first"], "upstreams": [
        {"prefix": "tv", "enabled": False, "command": "python", "args": ["D:\\old\\tv_mcp.py"], "allow": ["play"]},
        {"prefix": "money", "command": "node", "args": ["server.mjs"], "allow": ["status"]}], "claude": {"runnerToken": "t"}}
    config.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

    done = link(config, plugin, python="C:\\venv\\Scripts\\python.exe")
    assert done == {"changed": True, "added": False, "was_off": True}
    now = json.loads(config.read_text(encoding="utf-8"))
    assert now["upstreams"][0] == {"prefix": "tv", "command": "C:\\venv\\Scripts\\python.exe", "args": [str(plugin)], "allow": ["play"]}
    # everything else is untouched, including text that is not plain ASCII
    assert {k: v for k, v in now.items() if k != "upstreams"} == {k: v for k, v in original.items() if k != "upstreams"}
    assert now["upstreams"][1] == original["upstreams"][1]
    assert "Café" in config.read_text(encoding="utf-8") and not list(tmp_path.glob("*.tmp"))
    # a second run changes nothing
    stamp = config.read_bytes()
    assert link(config, plugin, python="C:\\venv\\Scripts\\python.exe")["changed"] is False
    assert config.read_bytes() == stamp

    # no tv entry yet: one is added in front; a file written with a byte-order mark still reads
    config.write_text("\ufeff" + json.dumps({"upstreams": [original["upstreams"][1]]}), encoding="utf-8")
    assert link(config, plugin, python="py")["added"] is True
    assert [u["prefix"] for u in json.loads(config.read_text(encoding="utf-8"))["upstreams"]] == ["tv", "money"]

    # broken or foreign files are left alone, and the error never quotes the file
    for text in ('{"passphraseHash": "secret-hash" oops', "[1, 2]", '{"upstreams": {}}'):
        config.write_text(text, encoding="utf-8")
        with pytest.raises(LinkError) as err:
            link(config, plugin)
        assert "secret-hash" not in str(err.value) and config.read_text(encoding="utf-8") == text
    with pytest.raises(LinkError):
        link(config, tmp_path / "missing.py")
    assert console_python("C:\\venv\\Scripts\\pythonw.exe").endswith("python.exe")
    assert not console_python("C:\\venv\\Scripts\\pythonw.exe").endswith("pythonw.exe")
