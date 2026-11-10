"""Simulated: the read-only playback check. With it off (the default) the hub
never asks the TV's YouTube app anything and never claims playback. With it on,
`play` reports what that app says: the video running, another video, an ad, or
nothing. The stand-in for YouTube's link is in sim.py; the link's own wire
format is tested in test_lounge.py, also against a stand-in."""

import pytest

from sim import said

JAZZ = "Dx5qFachd3A"


async def test_off_by_default_nothing_is_asked_and_nothing_is_claimed(rig):
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.youtube.playing == JAZZ
    assert r["status"] == "partly_verified" and said(r, "actually playing", "not_verified")
    assert rig.lounge.asked == [] and "on_tv" not in r
    state = (await rig.call("get_state"))["tvs"][0]
    assert state["playback_check"] == "off" and state["can_read"]["playback"] is False


async def test_switched_on_play_is_confirmed_by_the_tvs_youtube_app(rig):
    on = await rig.call("playback_check", on=True)
    assert on["ok"] and on["playback_check"] == "on" and "never starts" in on["summary"]
    r = await rig.call("play", query="jazz music", prefer="long")
    assert r["ok"] and r["status"] == "verified"
    assert r["summary"] == "Playing Jazz Music - Relaxing Cafe Mix on YouTube; the TV's YouTube app confirms it."
    assert said(r, "YouTube on the TV reports that it is playing this video, 3 s in.")
    assert not said(r, "actually playing", "not_verified")
    assert r["on_tv"] == {"video": JAZZ, "state": "playing", "position": 3.0}
    assert rig.lounge.asked == [("fake-screen-id", JAZZ)]          # asked once, for the chosen video
    state = (await rig.call("get_state"))["tvs"][0]
    assert state["playback_check"] == "on" and state["can_read"]["playback"] is True


async def test_a_different_video_on_screen_is_reported_as_that(rig):
    await rig.call("playback_check", on=True)
    rig.lounge.instead = "jazzOTHER02"
    r = await rig.call("play", query="jazz music", prefer="long")
    assert r["ok"] and r["status"] == "partly_verified"            # something from the search is playing; not the pick
    assert r["playing"]["id"] == "jazzOTHER02" and r["playing"]["title"] == "Smooth Jazz Music Night Mix"
    assert r["chosen"]["id"] == JAZZ
    assert said(r, "it is playing Smooth Jazz Music Night Mix, not the video the hub chose")
    assert "Smooth Jazz Music Night Mix" in r["summary"] and "confirms" not in r["summary"]
    # what the hub remembers as started is what is on, so "something else" avoids the right one
    assert (await rig.call("get_state"))["tvs"][0]["last_started"]["id"] == "jazzOTHER02"


async def test_an_exact_video_that_did_not_come_up_is_not_done(rig):
    await rig.call("playback_check", on=True)
    rig.lounge.instead = "jazzOTHER02"
    r = await rig.call("play", video="dQw4w9WgXcQ")
    assert r["ok"] is False and r["status"] == "partial"
    assert "Rick Astley" in r["not_done"] and "not the video asked for" in r["summary"]


async def test_an_ad_ahead_of_the_video_is_not_called_playing(rig):
    await rig.call("playback_check", on=True)
    rig.lounge.ad = True
    r = await rig.call("play", query="jazz music", prefer="long")
    assert r["status"] == "partly_verified" and "an ad was still running" in r["summary"]
    assert said(r, "an ad running ahead of this video") and said(r, "the video itself has started", "not_verified")


@pytest.mark.parametrize("setup, line", [
    (lambda rig: setattr(rig.lounge, "down", True), "could not ask YouTube on the TV (YouTube's link answered 503)"),
    (lambda rig: setattr(rig.lounge, "silent", True), "did not answer the playback check in time"),
    (lambda rig: setattr(rig.tv, "dial", False), "the TV gave no id for its YouTube app to ask"),
])
async def test_a_check_that_learns_nothing_leaves_the_answer_as_before(rig, setup, line):
    await rig.call("playback_check", on=True)
    setup(rig)
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.youtube.playing == JAZZ
    assert r["ok"] and r["status"] == "partly_verified" and "not confirmed" in r["summary"]
    assert said(r, line, "not_verified")


async def test_a_paused_or_empty_player_is_reported_as_such(rig, monkeypatch):
    await rig.call("playback_check", on=True)
    real = rig.lounge.watch

    async def paused(screen_id, want=None, seconds=20.0):
        rig.tv.youtube.paused = True
        return await real(screen_id, want, seconds)
    rig.tools.youtube_now = paused
    r = await rig.call("play", query="jazz music", prefer="long")
    assert r["status"] == "partly_verified" and said(r, f"reports 'paused' for video id {JAZZ}", "not_verified")

    async def empty(screen_id, want=None, seconds=20.0):
        rig.tv.youtube.playing = None
        return await real(screen_id, want, seconds)
    rig.tools.youtube_now = empty
    r = await rig.call("play", query="lofi hip hop radio", prefer="live")
    assert said(r, "reports 'idle' with no video loaded", "not_verified")


async def test_the_switch_is_kept_per_tv_and_over_a_restart(rig):
    await rig.add_tv("bedroom", "Bedroom")
    await rig.call("playback_check", on=True, tv="living-room")
    tvs = {t["id"]: t["playback_check"] for t in (await rig.call("get_state"))["tvs"]}
    assert tvs == {"living-room": "on", "bedroom": "off"}
    rig.restart()
    tvs = {t["id"]: t["playback_check"] for t in (await rig.call("get_state"))["tvs"]}
    assert tvs == {"living-room": "on", "bedroom": "off"}
    off = await rig.call("playback_check", on=False, tv="living-room")
    assert off["playback_check"] == "off" and "Nothing connects" not in off["summary"] and "nothing connects" in off["summary"]
    await rig.call("play", query="jazz music", prefer="long", tv="living-room")
    assert rig.lounge.asked == []


async def test_the_switch_rejects_bad_arguments(rig):
    with pytest.raises(ValueError):
        await rig.tools.call("playback_check", {})
    with pytest.raises(ValueError):
        await rig.tools.call("playback_check", {"on": "yes"})
    r = await rig.call("playback_check", on=True, tv="kitchen")
    assert r["ok"] is False and r["status"] == "failed"
