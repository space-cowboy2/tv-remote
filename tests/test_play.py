"""Simulated: finding and starting content. Each test follows one request from
the tool call to the video the simulated TV ends up playing, and checks what
the report claims against what the hub could really have known."""

import pytest

from tvremote import fake as fake_mod
from tvremote import tools as tools_mod

from sim import said

JAZZ = "Dx5qFachd3A"


async def test_play_jazz_from_a_tv_that_is_on(rig):
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.youtube.playing == JAZZ              # the long mix, not the 60-second clip listed first
    assert r["ok"] and r["status"] == "partly_verified"
    assert r["playing"]["title"] == "Jazz Music - Relaxing Cafe Mix" and r["playing"]["id"] == JAZZ
    assert said(r, "The TV is on") and said(r, "YouTube is on screen") and said(r, "search box opened")
    assert said(r, f"holds the text '{JAZZ}'")
    assert said(r, "actually playing", "not_verified")  # never claimed
    assert "not confirmed" in r["summary"]
    assert rig.tv.youtube.dropped == 0                  # no key landed on a screen that was not ready


async def test_play_wakes_a_tv_in_standby_first(rig):
    rig.tv.power = "standby"
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.power == "on" and rig.tv.youtube.playing == JAZZ
    assert "was standby" in r["steps"][0] and said(r, "The TV is on")


async def test_play_wakes_a_tv_that_is_off_with_wake_on_lan(rig, monkeypatch):
    rig.tv.power = "off"
    sent = []

    def wake(mac):
        sent.append(mac)
        if len(sent) == 3:  # a TV takes a few seconds to come up
            rig.tv.power = "on"
    monkeypatch.setattr("tvremote.samsung.send_wol", wake)
    r = await rig.call("play", query="jazz music", prefer="long")
    assert sent and sent[0] == rig.tv.mac
    assert r["ok"] and rig.tv.youtube.playing == JAZZ
    assert "KEY_POWER" not in rig.tv.keys  # a toggle is never sent blind


async def test_play_reports_a_tv_that_will_not_wake(rig, monkeypatch):
    rig.tv.power = "off"
    monkeypatch.setattr("tvremote.samsung.send_wol", lambda mac: None)
    r = await rig.call("play", query="jazz music")
    assert not r["ok"] and r["status"] == "failed"
    assert r["failed"]["kind"] == "tv_did_not_wake" and r["failed"]["needs_user"]
    assert r["playing"]["id"]  # it still says what it had chosen
    assert rig.tv.launched == []


async def test_play_restarts_youtube_when_it_was_left_somewhere_else(rig):
    await rig.call("play", video="dQw4w9WgXcQ")
    assert rig.tv.youtube.playing == "dQw4w9WgXcQ"
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.closed and rig.tv.youtube.playing == JAZZ
    assert any("Closed YouTube first" in s for s in r["steps"])


async def test_play_gets_past_the_account_picker(rig):
    rig.tv.youtube.account_picker = True
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.youtube.playing == JAZZ and r["ok"]
    assert any("Try 1" in s for s in r["steps"]) and said(r, "search box opened")


async def test_play_recovers_from_a_slow_youtube_start(rig):
    rig.tv.youtube.load_time = 12.0  # longer than the first wait
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.youtube.playing == JAZZ and r["ok"]
    assert len([s for s in r["steps"] if s.startswith("Try")]) == 2  # the third, slower try worked
    assert len(rig.tv.closed) >= 1


async def test_play_fails_plainly_when_the_search_box_never_opens(rig, monkeypatch):
    await rig.call("play", video="dQw4w9WgXcQ")  # this TV is now known to announce its keyboard
    monkeypatch.setattr(fake_mod, "GUIDE", ["profile", "search", "home", "shorts", "library"])  # a different layout
    r = await rig.call("play", query="jazz music", prefer="long")
    assert not r["ok"] and r["failed"]["kind"] == "search_box_not_found"
    assert "youtube_open_search" in r["failed"]["hint"]
    assert rig.tv.youtube.playing is None  # closed again, nothing stray left running
    assert len([s for s in r["steps"] if s.startswith("Try")]) == 3
    # the owner watches the screen once and corrects the steps; play works again
    fixed = await rig.call("save_macro", name="youtube_open_search", description="search is the second entry",
                           steps=[{"key": "KEY_LEFT"}, {"wait": 1}, {"key": "KEY_ENTER"}])
    assert fixed["ok"]
    r = await rig.call("play", query="jazz music", prefer="long")
    assert r["ok"] and rig.tv.youtube.playing == JAZZ


async def test_play_on_a_tv_that_never_announces_its_keyboard(rig):
    """What the 2024 OLED does: YouTube draws its own keyboard, so the TV announces
    nothing and its text message lands nowhere. The hub spells the title with the
    cursor instead."""
    rig.tv.ime_events = False
    rig.tv.youtube.own_keyboard = True
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.youtube.playing == JAZZ
    assert rig.tv.youtube.text == "jazz music - relaxing cafe mix"
    assert r["ok"] and r["status"] == "partly_verified"
    assert said(r, "search box opened", "not_verified") and not said(r, "search box opened")
    assert said(r, "spelled key by key", "not_verified") and any("Spelled" in s for s in r["steps"])
    assert rig.tv.dial_launches == []  # the link is off unless switched on
    launches = len(rig.tv.launched)
    r = await rig.call("play", query="relaxing piano", prefer="long")  # learned: one try from now on
    assert len(rig.tv.launched) == launches + 1 and rig.tv.youtube.playing == "relaxPIANO1"


async def test_play_when_youtube_is_not_installed(rig):
    rig.tv.apps = [a for a in rig.tv.apps if a["name"] != "YouTube"]
    r = await rig.call("play", query="jazz music")
    assert r["failed"]["kind"] == "app_not_installed" and not r["ok"]


async def test_play_when_the_tv_accepts_the_launch_and_nothing_opens(rig):
    rig.tv.launch_works = False
    r = await rig.call("play", query="jazz music")
    assert r["failed"]["kind"] == "app_did_not_open"
    assert len(rig.tv.launched) == 2  # one retry, then stop
    assert rig.tv.keys == []          # no keys pressed into whatever is on screen


async def test_play_on_firmware_whose_app_endpoints_hang(rig):
    """The 2022 Q60B: port 8001 accepts app requests and never answers."""
    rig.tv.rest_apps, rig.tv.hang_seconds = "hang", 6.0
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.youtube.playing == JAZZ and r["ok"]
    assert said(r, "YouTube is running")  # from DIAL, which only knows running or stopped
    assert rig.tools.tv.rest_apps_ok is False


async def test_play_when_youtube_cannot_be_searched_from_the_hub(rig):
    rig.net.down = True
    r = await rig.call("play", query="jazz music relaxing")
    assert r["ok"] and rig.tv.youtube.playing == JAZZ  # the TV's own first result for the words
    assert r["playing"]["id"] is None
    assert said(r, "no specific video was chosen", "not_verified")
    assert "first YouTube result for 'jazz music relaxing'" in r["summary"]


async def test_play_an_exact_video_from_a_link(rig):
    r = await rig.call("play", video="https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3")
    assert rig.tv.youtube.playing == "dQw4w9WgXcQ"
    assert r["playing"]["title"].startswith("Rick Astley")


async def test_play_types_the_title_when_the_id_does_not_find_the_video(rig):
    rig.net.unpinned.add("jfKfPfyJRdk")  # seen live: YouTube does not return some streams for their id
    r = await rig.call("play", query="lofi hip hop", prefer="live")
    assert rig.tv.youtube.playing == "jfKfPfyJRdk"
    assert said(r, "holds the text 'lofi hip hop radio - beats to relax study to'")


async def test_play_treats_a_plain_eleven_letter_word_as_words(rig):
    rig.net.videos.append({"id": "abcDEF12345", "title": "Rachmaninov Piano Concerto", "channel": "Hall",
                           "length": "35:00", "seconds": 2100, "live": False})
    rig.tv.youtube.catalog["abcDEF12345"] = "Rachmaninov Piano Concerto"
    r = await rig.call("play", video="Rachmaninov")
    assert r["playing"]["id"] == "abcDEF12345" and rig.tv.youtube.playing == "abcDEF12345"


async def test_same_request_twice_is_not_run_twice(rig):
    await rig.call("play", query="jazz music", prefer="long")
    launches = len(rig.tv.launched)
    r = await rig.call("play", query="Jazz  Music", prefer="long")
    assert len(rig.tv.launched) == launches
    assert "Already started" in r["summary"] and said(r, "Not repeated", "not_verified")


async def test_something_else_picks_a_different_video(rig):
    await rig.call("play", query="jazz music", prefer="long")
    r = await rig.call("play", query="jazz music", prefer="long", avoid_recent=True)
    assert r["playing"]["id"] == "jazzOTHER02" and rig.tv.youtube.playing == "jazzOTHER02"


async def test_choosing_a_result_first_then_playing_it(rig):
    found = await rig.call("search_youtube", query="great show episode")
    assert [v["id"] for v in found["results"]] == ["showEP00004", "showEP00005"]
    assert all("seconds" not in v for v in found["results"])
    r = await rig.call("play", video="showEP00005")
    assert rig.tv.youtube.playing == "showEP00005" and r["playing"]["title"] == "Great Show Episode 5"


async def test_search_youtube_failure_is_a_report_not_a_crash(rig):
    rig.net.down = True
    r = await rig.call("search_youtube", query="anything")
    assert not r["ok"] and r["failed"]["kind"] == "youtube_search_failed"


async def test_nothing_found(rig):
    r = await rig.call("play", query="zzzz qqqq nothing")
    assert r["failed"]["kind"] == "nothing_found" and rig.tv.launched == []


async def test_another_service_opens_the_app_and_says_search_was_not_done(rig):
    r = await rig.call("play", query="Stranger Things", service="Netflix")
    assert rig.tv.foreground == "3201907018807"
    assert not r["ok"] and r["status"] == "partial"
    assert "Stranger Things" in r["not_done"] and "netflix_search" in r["not_done"]
    assert said(r, "Netflix is on screen")
    assert rig.tv.keys == []  # nothing pressed blindly inside Netflix


async def test_another_service_uses_a_saved_search_sequence(rig):
    await rig.call("save_macro", name="netflix_search", description="from watching the screen",
                   steps=[{"key": "KEY_LEFT"}, {"key": "KEY_UP"}, {"key": "KEY_ENTER"}, {"text": "{value}", "submit": True}])
    r = await rig.call("play", query="Stranger Things", service="netflix")
    assert r["ok"] and r["status"] == "partly_verified"
    assert rig.tv.keys == ["KEY_LEFT", "KEY_UP", "KEY_ENTER"]
    assert said(r, "netflix_search", "not_verified")


async def test_a_service_that_is_not_installed(rig):
    r = await rig.call("play", query="The Bear", service="hulu")
    assert r["failed"]["kind"] == "app_not_installed"


async def test_a_saved_all_in_one_youtube_macro_still_wins(rig):
    await rig.call("save_macro", name="youtube_search", description="mine", steps=[{"key": "KEY_HOME"}])
    r = await rig.call("play_youtube", video="lofi")
    assert rig.tv.keys == ["KEY_HOME"] and said(r, "saved youtube_search", "not_verified")


async def test_play_gives_up_at_its_deadline(rig, monkeypatch):
    monkeypatch.setattr(tools_mod, "PLAY_DEADLINE", 0.3)
    r = await rig.call("play", query="jazz music")
    assert r["failed"]["kind"] == "timeout" and not r["ok"]


async def test_a_second_request_waits_then_reports_busy(rig):
    handle = rig.tools.tvs["living-room"]
    await handle.seq.acquire()
    handle.doing = "starting 'jazz' on YouTube"
    try:
        r = await rig.call("set_volume", level=5)
    finally:
        handle.seq.release()
    assert r["failed"]["kind"] == "busy" and "starting 'jazz'" in r["failed"]["why"]
    assert rig.tv.volume == 12


@pytest.mark.parametrize("args", [{}, {"query": "x", "prefer": "short"}, {"query": "x", "exclude": "abc"}])
async def test_play_rejects_bad_arguments(rig, args):
    with pytest.raises(ValueError):
        await rig.tools.call("play", args)
