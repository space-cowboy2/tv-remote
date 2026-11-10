"""Simulated: power, volume, mute, playback, inputs, apps and timers."""

import asyncio
import time

import pytest

from sim import said


# ------------------------------------------------------------------ volume

async def test_set_an_exact_volume_and_read_it_back(rig):
    r = await rig.call("set_volume", level=20)
    assert rig.tv.volume == 20
    assert r["ok"] and r["status"] == "verified" and said(r, "Volume is 20 (was 12)")
    assert rig.tv.keys == []  # set directly, not by pressing Vol+ eight times


async def test_quieter_is_relative_to_the_current_level(rig):
    r = await rig.call("set_volume", change=-3)
    assert rig.tv.volume == 9 and "now 9 (was 12)" in r["summary"]
    assert (await rig.call("set_volume", change=-50))["ok"] and rig.tv.volume == 0  # stops at the floor


async def test_mute_sets_the_state_instead_of_toggling(rig):
    r = await rig.call("set_volume", mute=True)
    assert rig.tv.muted and said(r, "is muted")
    await rig.call("set_volume", mute=True)  # asked again: still muted
    assert rig.tv.muted
    r = await rig.call("set_volume", mute=False, level=8)
    assert not rig.tv.muted and rig.tv.volume == 8 and r["status"] == "verified"


async def test_volume_on_a_tv_without_a_readable_level(rig):
    rig.tv.upnp = False
    r = await rig.call("set_volume", change=2)
    assert rig.tv.keys == ["KEY_VOLUP", "KEY_VOLUP"]
    assert r["ok"] and r["status"] == "sent_not_verified" and said(r, "does not report its volume", "not_verified")
    r = await rig.call("set_volume", level=15)
    assert r["failed"]["kind"] == "volume_level_unreadable" and "change" in r["failed"]["hint"]
    r = await rig.call("set_volume", mute=True)
    assert r["status"] == "sent_not_verified" and said(r, "toggles", "not_verified")


async def test_volume_keys_can_be_forced_for_a_soundbar(rig):
    r = await rig.call("set_volume", change=-2, method="keys")
    assert rig.tv.keys == ["KEY_VOLDOWN", "KEY_VOLDOWN"] and r["status"] == "sent_not_verified"


async def test_volume_when_the_tv_is_off(rig):
    rig.tv.power = "standby"
    r = await rig.call("set_volume", level=10)
    assert r["failed"]["kind"] == "tv_is_off" and rig.tv.volume == 12


@pytest.mark.parametrize("args", [{}, {"level": 101}, {"level": 5, "change": 1}, {"change": 1.5}, {"mute": "yes"}])
async def test_volume_rejects_bad_arguments(rig, args):
    with pytest.raises(ValueError):
        await rig.tools.call("set_volume", args)


# ------------------------------------------------------------------- power

async def test_power_off_and_on_are_confirmed_by_the_tv(rig):
    r = await rig.call("set_power", state="off")
    assert rig.tv.power == "standby" and r["status"] == "verified"
    r = await rig.call("set_power", state="on")
    assert rig.tv.power == "on" and r["status"] == "verified" and said(r, "PowerState on")
    keys = len(rig.tv.keys)
    r = await rig.call("set_power", state="on")  # already on: nothing pressed
    assert r["status"] == "verified" and len(rig.tv.keys) == keys


async def test_power_state_survives_a_stalled_status_port(rig):
    """Seen on the Q60B: port 8001 stalls while the TV is plainly on."""
    rig.tv.rest_info, rig.tv.hang_seconds = "hang", 6.0
    r = await rig.call("set_power", state="on")
    assert r["ok"] and said(r, "app and volume services answer")
    assert "KEY_POWER" not in rig.tv.keys  # it must not toggle a running TV off


async def test_power_off_that_does_not_take(rig, monkeypatch):
    async def ignore_power(ws, key):
        return None
    monkeypatch.setattr(rig.tv, "_key", ignore_power)
    r = await rig.call("set_power", state="off")
    assert r["failed"]["kind"] == "tv_did_not_turn_off" and rig.tv.power == "on"


# ---------------------------------------------------------------- playback

async def test_pause_resume_and_seek_reach_the_player(rig):
    await rig.call("play", video="dQw4w9WgXcQ")
    r = await rig.call("playback", action="pause")
    assert rig.tv.youtube.paused and r["status"] == "sent_not_verified"
    assert said(r, "reports no playback state", "not_verified")
    await rig.call("playback", action="resume")
    assert not rig.tv.youtube.paused
    await rig.call("playback", action="forward", seconds=60)
    assert rig.tv.youtube.position == 60
    r = await rig.call("playback", action="rewind", seconds=15)  # "go back a little"
    assert rig.tv.youtube.position == 40 and "2 times" in r["summary"]
    state = await rig.call("get_state")
    assert state["tvs"][0]["last_started"]["title"].startswith("Rick Astley")


async def test_next_has_no_key_until_one_is_taught(rig):
    r = await rig.call("playback", action="next")
    assert r["failed"]["kind"] == "no_known_way" and "playback_next" in r["failed"]["hint"]
    await rig.call("save_macro", name="playback_next", description="", steps=[{"key": "KEY_CHUP"}])
    r = await rig.call("playback", action="next")
    assert r["ok"] and "KEY_CHUP" in rig.tv.received[-1]["params"]["DataOfCmd"]


async def test_playback_when_the_tv_is_off(rig):
    rig.tv.power = "standby"
    assert (await rig.call("playback", action="pause"))["failed"]["kind"] == "tv_is_off"


# ----------------------------------------------------------- apps and inputs

async def test_open_an_app_by_the_name_people_use(rig):
    r = await rig.call("open_app", app="netflix")
    assert rig.tv.foreground == "3201907018807" and r["status"] == "verified"
    rig.tv.power = "standby"
    r = await rig.call("open_app", app="YouTube")  # wakes the TV first
    assert rig.tv.power == "on" and rig.tv.foreground == "111299001912"


async def test_open_an_app_that_is_not_there(rig):
    r = await rig.call("open_app", app="prime")  # a known app, not installed here
    assert r["failed"]["kind"] == "app_not_installed" and "Prime Video" in r["failed"]["why"]
    r = await rig.call("open_app", app="frobnicator")
    assert r["failed"]["kind"] == "unknown_app" and "Netflix" in r["failed"]["why"]


async def test_old_launch_app_still_works(rig):
    r = await rig.call("launch_app", app_id="3201907018807")
    assert r["ok"] and rig.tv.foreground == "3201907018807"


async def test_switch_input_says_it_cannot_confirm(rig):
    r = await rig.call("switch_input", input="hdmi2")
    assert rig.tv.keys == ["KEY_HDMI2"] and r["status"] == "partly_verified"  # only "the TV is on" is confirmed
    assert said(r, "does not report which input", "not_verified")
    with pytest.raises(ValueError):
        await rig.tools.call("switch_input", {"input": "scart"})


# ------------------------------------------------------------------ timers

class Clock:
    """Timers wait on this instead of real minutes."""

    def __init__(self):
        self.waits = []
        self.go = asyncio.Event()

    async def sleep(self, seconds):
        self.waits.append(seconds)
        await self.go.wait()


async def test_turn_it_off_in_half_an_hour(rig):
    clock = Clock()
    rig.tools.timer_sleep = clock.sleep
    r = await rig.call("schedule", in_minutes=30, tool="set_power", args={"state": "off"}, note="sleep timer")
    assert r["ok"] and r["args"] == {"state": "off", "tv": "living-room"}
    state = await rig.call("get_state")
    assert state["timers"][0]["does"] == "set_power" and 29 < state["timers"][0]["in_minutes"] <= 30
    assert rig.tv.power == "on"
    await asyncio.sleep(0.05)
    assert 1790 < clock.waits[0] <= 1800
    clock.go.set()
    for _ in range(200):
        if rig.tv.power == "standby" and not rig.tools.ctx["timers"]:
            break
        await asyncio.sleep(0.02)
    assert rig.tv.power == "standby" and rig.tools.ctx["timers"] == []
    assert rig.tools.ctx["history"][-1]["summary"].startswith("timer ran: sleep timer")


async def test_cancelled_timer_never_runs(rig):
    clock = Clock()
    rig.tools.timer_sleep = clock.sleep
    made = await rig.call("schedule", in_minutes=10, tool="playback", args={"action": "pause"})
    r = await rig.call("cancel_schedule", id=made["timer"])
    assert r["cancelled"] == [made["timer"]]
    clock.go.set()
    await asyncio.sleep(0.1)
    assert rig.tv.keys == [] and (await rig.call("get_state"))["timers"] == []


async def test_timers_survive_a_hub_restart_and_stale_ones_are_dropped(rig):
    clock = Clock()
    rig.tools.timer_sleep = clock.sleep
    await rig.call("schedule", in_minutes=30, tool="set_volume", args={"level": 5})
    await rig.tools.stop()  # the hub goes down with the timer pending
    saved = rig.tools.ctx["timers"]
    assert len(saved) == 1
    tools = rig.restart()
    tools.timer_sleep = clock.sleep
    tools.start()
    assert len(tools.ctx["timers"]) == 1
    clock.go.set()
    for _ in range(200):
        if rig.tv.volume == 5:
            break
        await asyncio.sleep(0.02)
    assert rig.tv.volume == 5
    # one that came due while the hub was down for an hour is not run late
    tools.ctx["timers"] = [{"id": "t1", "due": time.time() - 3600, "tool": "set_power", "args": {"state": "off"}, "note": ""}]
    tools.start()
    await asyncio.sleep(0.1)
    assert rig.tv.power == "on" and tools.ctx["timers"] == []
    assert "missed" in tools.ctx["history"][-1]["summary"]


@pytest.mark.parametrize("args", [
    {"in_minutes": 5, "tool": "pair_tv", "args": {}},          # not something to run unattended
    {"in_minutes": 5, "tool": "save_macro", "args": {}},
    {"in_minutes": 0, "tool": "set_power", "args": {"state": "off"}},
    {"tool": "set_power", "args": {"state": "off"}},
])
async def test_schedule_rejects_what_it_should_not_run(rig, args):
    with pytest.raises(ValueError):
        await rig.tools.call("schedule", args)


# -------------------------------------------------------------- plain remote

async def test_plain_remote_tools_say_they_only_sent(rig):
    r = await rig.call("send_keys", keys=[{"key": "caption"}])
    assert rig.tv.keys == ["KEY_CAPTION"] and "Sent only" in r["text"]
    rig.tv.power = "off"
    await rig.tv.drop_connections()
    r = await rig.call("send_keys", keys=[{"key": "up"}])
    assert not r["ok"] and r["failed"]["kind"] == "tv_unreachable"


# ------------------------------------------------- waking a TV on a real network

def test_the_address_a_wake_up_goes_to_is_read_from_the_neighbour_table():
    from tvremote.wol import lan_mac, parse_neighbours

    windows = ("\r\nInterface: 100.110.24.18 --- 0x7\r\n  Internet Address      Physical Address      Type\r\n"
               "  100.110.24.1          4c-5e-0c-aa-bb-cc     dynamic\r\n  100.110.24.16         a0-d7-f3-01-02-03     dynamic\r\n"
               "  100.110.24.160        11-22-33-44-55-66     dynamic\r\n")
    assert parse_neighbours(windows, "100.110.24.16") == "a0:d7:f3:01:02:03"   # not .160, not the router
    mac_os = "? (192.168.1.50) at a0:d7:f3:1:2:3 on en0 ifscope [ethernet]\n"
    assert parse_neighbours(mac_os, "192.168.1.50") == "a0:d7:f3:01:02:03"      # macOS drops leading zeros
    linux = ("IP address       HW type     Flags       HW address            Mask     Device\n"
             "192.168.1.50     0x1         0x2         a0:d7:f3:01:02:03     *        eth0\n"
             "192.168.1.51     0x1         0x0         00:00:00:00:00:00     *        eth0\n")
    assert parse_neighbours(linux, "192.168.1.50") == "a0:d7:f3:01:02:03"
    assert parse_neighbours(linux, "192.168.1.51") is None                      # an entry that never resolved
    assert parse_neighbours("? (192.168.1.50) at (incomplete) on en0\n", "192.168.1.50") is None
    assert lan_mac("203.0.113.9") is None                                       # unknown host: None, never an error


def test_a_wake_up_leaves_from_every_local_address(monkeypatch):
    from tvremote import wol

    sent = []

    class Sock:
        def __init__(self, *a):
            self.source = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def bind(self, addr):
            if addr[0] == "100.111.6.22":
                raise OSError(10049, "cannot bind")   # one network refusing does not stop the others
            self.source = addr[0]

        def sendto(self, packet, addr):
            sent.append((self.source, addr, packet))

    monkeypatch.setattr(wol.socket, "socket", Sock)
    monkeypatch.setattr(wol, "_local_addresses", lambda: ["100.110.24.18", "100.111.6.22"])
    wol.send_wol("a0:d7:f3:01:02:03", repeat=1)
    assert [s[0] for s in sent] == [None, "100.110.24.18"]
    assert all(s[1] == ("255.255.255.255", 9) and s[2] == wol.magic_packet("a0-d7-f3-01-02-03") for s in sent)


async def test_a_tv_on_a_cable_is_woken_by_the_address_the_network_sees(rig, monkeypatch):
    """The TV reports its Wi-Fi address; on a cable only the other one wakes it."""
    cable = "a0:d7:f3:01:02:03"
    rig.tv.power = "off"
    rig.tools.tvs["living-room"].tv.lan_mac = cable
    sent = []

    def wake(mac):
        sent.append(mac)
        if mac == cable and sent.count(cable) == 2:
            rig.tv.power = "on"
    monkeypatch.setattr("tvremote.samsung.send_wol", wake)
    r = await rig.call("set_power", state="on")
    assert r["ok"] and r["status"] == "verified"
    assert sent[:2] == [cable, rig.tv.mac]   # both, the network's own first
