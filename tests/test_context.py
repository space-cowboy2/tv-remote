"""Simulated: which TV, what is known, follow-ups, pairing and restarts."""

import json

import pytest

from tvremote.config import TVConfig, extra_path, load_all, slugify
from tvremote.fake import FakeTV
from tvremote.tools import Store, Tools, load_handles

from sim import Rig, said


async def test_get_state_gives_what_follow_ups_need(rig):
    await rig.call("play", query="jazz music", prefer="long")
    s = await rig.call("get_state")
    tv = s["tvs"][0]
    assert tv["power"] == "on" and tv["volume"] == 12 and tv["muted"] is False and tv["youtube_app"] == "running"
    assert tv["last_started"]["id"] == "Dx5qFachd3A" and "not what is on screen" in tv["last_started"]["note"]
    assert tv["can_read"] == {"volume": True, "app_state": True, "search_box_opening": True, "playback": False}
    assert s["active_tv"] == "living-room" and s["recent"][-1]["tool"] == "play"
    assert (await rig.call("get_status"))["tvs"]  # the old name still answers


async def test_the_next_episode_follow_up(rig):
    """The model reads the last title from get_state, finds the next one, plays it."""
    await rig.call("play", video="showEP00004")
    last = (await rig.call("get_state"))["tvs"][0]["last_started"]["title"]
    assert last == "Great Show Episode 4"
    found = await rig.call("search_youtube", query="Great Show Episode 5")
    r = await rig.call("play", video=found["suggested"])
    assert rig.tv.youtube.playing == "showEP00005" and r["ok"]


async def test_use_the_other_tv(rig):
    bedroom = await rig.add_tv("bedroom", "Bedroom", volume=30)
    r = await rig.call("use_tv", tv="other")
    assert r["active_tv"] == "bedroom" and r["state"]["volume"] == 30
    await rig.call("set_volume", change=-5)  # "make it quieter" now means the bedroom TV
    assert bedroom.volume == 25 and rig.tv.volume == 12
    await rig.call("set_volume", level=3, tv="living room")  # named: goes there, and it becomes the active one
    assert rig.tv.volume == 3
    await rig.call("playback", action="pause")
    assert rig.tv.keys == ["KEY_PAUSE"] and bedroom.keys == []


async def test_a_tv_that_is_not_paired_is_named_as_such(rig):
    rig.tools.scan = fake_scan([{"host": "10.0.0.9", "kind": "other", "name": "Media Room TV", "maker": "Sony", "model": "XBR-75X900F"}])
    await rig.call("find_tvs")
    r = await rig.call("set_volume", level=5, tv="media room")
    assert r["failed"]["kind"] == "unknown_tv"
    assert "Living Room" in r["failed"]["why"] and "Media Room TV (Sony, not controllable" in r["failed"]["why"]
    assert rig.tv.volume == 12  # never quietly applied to a different TV
    r = await rig.call("use_tv", tv="other")
    assert r["failed"]["kind"] == "unknown_tv" and not r["failed"]["needs_user"]


async def test_two_matching_tvs_need_the_person_to_choose(rig):
    await rig.add_tv("bedroom-1", "Bedroom Left")
    await rig.add_tv("bedroom-2", "Bedroom Right")
    r = await rig.call("set_power", state="off", tv="bedroom")
    assert r["failed"]["kind"] == "unknown_tv" and r["failed"]["needs_user"]
    assert all(f.power == "on" for f in rig.fakes)


def fake_scan(devices):
    async def scan(timeout=3.0, interface=None):
        return [dict(d) for d in devices]
    return scan


async def test_find_tvs_sorts_what_is_on_the_network(rig):
    rig.tools.scan = fake_scan([
        {"host": "127.0.0.1", "kind": "samsung", "name": "Living Room", "maker": "Samsung", "model": "QN55Q60B"},
        {"host": "10.0.0.8", "kind": "samsung", "name": "Bedroom", "maker": "Samsung", "model": "QN43"},
        {"host": "10.0.0.9", "kind": "other", "name": "Media Room TV", "maker": "Sony", "model": "XBR"},
    ])
    found = (await rig.call("find_tvs"))["found"]
    assert found[0]["paired"] and found[0]["id"] == "living-room"
    assert not found[1]["paired"] and "pair_tv" in found[1]["to_add"]
    assert "cannot control" in found[2]["note"]
    assert (await rig.call("get_state"))["seen_but_not_controllable"][0]["maker"] == "Sony"


async def test_context_survives_a_hub_restart(rig):
    await rig.add_tv("bedroom", "Bedroom")
    await rig.call("use_tv", tv="bedroom")
    await rig.call("play", video="dQw4w9WgXcQ", tv="living-room")
    await rig.call("remember", note="Start music at volume 10")
    tools = rig.restart()
    assert tools.ctx["active"] == "living-room"
    assert tools.ctx["playing"]["living-room"]["id"] == "dQw4w9WgXcQ"
    assert tools.ctx["signals"]["living-room"]["keyboard_events"] is True
    assert tools.list()["notes"] == "- Start music at volume 10\n"
    assert "Running the TV from a conversation" in tools.list()["guide"]


async def test_a_damaged_context_file_does_not_stop_the_hub(tmp_path):
    (tmp_path / "context.json").write_text("{not json")
    (tmp_path / "macros.json").write_text("[]")
    r = Rig(tmp_path)
    await r.add_tv()
    try:
        assert (await r.call("set_volume", level=7))["ok"]
        assert "youtube_open_search" in (await r.call("list_macros"))
    finally:
        await r.close()


# ----------------------------------------------------------------- pairing

@pytest.fixture
async def unpaired(tmp_path):
    """A hub with nothing paired, and a TV on the network that could be."""
    fake = await FakeTV().start()
    args = {"port": fake.ws_port, "info_port": fake.info_port, "dial_port": fake.info_port,
            "upnp_port": fake.info_port, "secure": False}
    tools = Tools([], store=Store(tmp_path), config_path=tmp_path / "tv.json", tv_args=args)
    yield tools, fake, tmp_path
    await tools.stop()
    for h in tools.tvs.values():
        await h.tv.close()
    await fake.stop()


async def test_with_no_tv_paired_every_tool_says_so(unpaired):
    tools, _, _ = unpaired
    for name, args in [("get_state", {}), ("play", {"query": "jazz"}), ("set_volume", {"level": 5}),
                       ("set_power", {"state": "on"}), ("send_keys", {"keys": [{"key": "up"}]})]:
        r = json.loads(await tools.call(name, args))
        assert not r["ok"] and r["failed"]["kind"] == "no_tv_paired" and r["failed"]["needs_user"], name


async def test_pairing_from_the_conversation(unpaired):
    tools, fake, folder = unpaired
    r = json.loads(await tools.call("pair_tv", {"host": "127.0.0.1", "label": "Living Room"}))
    assert r["ok"] and r["status"] == "verified" and r["id"] == "living-room"
    saved = TVConfig.load(folder / "tv.json")
    assert saved.token == fake.token and saved.mac == fake.mac and saved.label == "Living Room"
    r = json.loads(await tools.call("set_volume", {"level": 9}))  # usable at once, no restart
    assert r["ok"] and fake.volume == 9
    assert fake.token not in json.dumps(r) + json.dumps(json.loads(await tools.call("get_state", {})))


async def test_pairing_notes_the_address_a_wake_up_needs_and_learns_it_later_too(unpaired):
    tools, fake, folder = unpaired
    tools.lan_mac = lambda host: "a0:d7:f3:01:02:03"
    await tools.call("pair_tv", {"host": "127.0.0.1", "label": "Living Room"})
    saved = TVConfig.load(folder / "tv.json")
    assert saved.lan_mac == "a0:d7:f3:01:02:03" and saved.mac == fake.mac
    assert tools.tvs["living-room"].tv.wake_macs == ["a0:d7:f3:01:02:03", fake.mac]
    # A TV saved without it (paired by an older version) gets it the first time a new hub sees it on.
    saved.lan_mac = None
    saved.save(folder / "tv.json")
    later = Tools(load_handles(folder / "tv.json", **tools.tv_args), store=Store(folder), config_path=folder / "tv.json",
                  tv_args=tools.tv_args)
    try:
        assert later.tvs["living-room"].tv.lan_mac is None
        later.lan_mac = lambda host: "a0:d7:f3:0a:0b:0c"
        r = json.loads(await later.call("set_power", {"state": "on"}))
        assert r["ok"] and TVConfig.load(folder / "tv.json").lan_mac == "a0:d7:f3:0a:0b:0c"
        assert TVConfig.load(folder / "tv.json").token == fake.token   # nothing else in the file changed
    finally:
        await later.stop()
        for h in later.tvs.values():
            await h.tv.close()


async def test_pairing_when_nobody_presses_allow(unpaired):
    tools, fake, folder = unpaired
    fake.pairing = "timeout"
    r = json.loads(await tools.call("pair_tv", {"host": "127.0.0.1"}))
    assert r["failed"]["kind"] == "pairing_needed" and r["failed"]["needs_user"]
    assert not (folder / "tv.json").exists() and tools.tvs == {}
    fake.pairing = "deny"
    r = json.loads(await tools.call("pair_tv", {"host": "127.0.0.1"}))
    assert r["failed"]["kind"] == "pairing_denied"


async def test_pairing_a_second_tv_keeps_the_first_as_default(unpaired):
    tools, fake, folder = unpaired
    await tools.call("pair_tv", {"host": "127.0.0.1", "label": "Living Room"})
    other = await FakeTV(token="other-token").start()
    try:
        tools.tv_args = {**tools.tv_args, "port": other.ws_port, "info_port": other.info_port,
                         "dial_port": other.info_port, "upnp_port": other.info_port}
        # same address as the first in this simulation, so the hub sees a re-pair of that TV
        r = json.loads(await tools.call("pair_tv", {"host": "127.0.0.1", "label": "Living Room"}))
        assert r["ok"] and list(tools.tvs) == ["living-room"]
        assert TVConfig.load(folder / "tv.json").token == "other-token"
    finally:
        await other.stop()


async def test_pairing_refuses_addresses_outside_the_home(unpaired):
    tools, _, _ = unpaired
    for host in ("8.8.8.8", "example.com", "", "192.168.1.5; rm -rf"):
        with pytest.raises(ValueError):
            await tools.call("pair_tv", {"host": host})


async def test_pairing_an_address_with_no_tv(unpaired):
    tools, fake, _ = unpaired
    fake.power = "off"
    r = json.loads(await tools.call("pair_tv", {"host": "127.0.0.1"}))
    assert r["failed"]["kind"] == "tv_unreachable"


async def test_a_tv_paired_from_the_command_line_is_picked_up(unpaired):
    tools, fake, folder = unpaired
    TVConfig(host="127.0.0.1", mac=fake.mac, token=fake.token, label="Den").save(folder / "tv.json")
    r = json.loads(await tools.call("set_volume", {"level": 4}))
    assert r["ok"] and fake.volume == 4 and list(tools.tvs) == ["den"]


# ------------------------------------------------------------ saved TV files

def test_saved_tvs_load_in_order_with_ids(tmp_path):
    main = tmp_path / "tv.json"
    TVConfig(host="10.0.0.2", label="Living Room TV").save(main)
    TVConfig(host="10.0.0.3", label="Bedroom").save(extra_path("Bedroom", main))
    (tmp_path / "tv-broken.json").write_text("{nope")
    found = load_all(main)
    assert [(slug, cfg.host, path.name) for slug, cfg, path in found] == [
        ("living-room-tv", "10.0.0.2", "tv.json"), ("bedroom", "10.0.0.3", "tv-bedroom.json")]
    assert slugify("  Déjà Vu / TV #2 ") == "d-j-vu-tv-2" and slugify("!!!") == "tv"
    assert load_all(tmp_path / "missing" / "tv.json") == []


async def test_reports_never_carry_the_pairing_token(rig):
    token = rig.tv.token
    for name, args in [("get_state", {}), ("play", {"query": "jazz music"}), ("find_tvs", {}), ("list_macros", {})]:
        rig.tools.scan = fake_scan([])
        assert token not in str(await rig.tools.call(name, args))
    assert said(await rig.call("set_power", state="on"), "The TV is on")
