"""Simulated: getting text onto the TV with one type_text call.

Two kinds of TV are modelled. The default one has a system keyboard that takes
the TV's text message and echoes it. The "2024" one is what the 42" OLED did on
2026-11-13: YouTube draws its own key grid, takes no text message, announces
nothing, and accepts a search handed over as a launch parameter."""

import pytest

from tvremote.apps import YOUTUBE_ID

NETFLIX = "3201907018807"
JAZZ = "Dx5qFachd3A"
OPEN_SEARCH = [{"key": "KEY_LEFT"}, {"key": "KEY_UP"}, {"key": "KEY_ENTER"}]


def make_2024(rig):
    rig.tv.ime_events = False
    rig.tv.youtube_link = True
    rig.tv.youtube.own_keyboard = True


async def youtube_ready(rig, search: bool = False):
    rig.tv.launch(YOUTUBE_ID)
    rig.tv.clock += 6
    if search:
        await rig.call("send_keys", keys=OPEN_SEARCH, delay_ms=0)
        assert rig.tv.youtube.state == "search"
    return len(rig.tv.keys)


async def test_system_keyboard_takes_the_text_and_the_echo_is_reported(rig):
    await youtube_ready(rig, search=True)
    r = await rig.call("type_text", text="jazz music", submit=True)
    assert r["ok"] and "echoed" in r["text"] and "submitted" in r["text"]
    assert rig.tv.youtube.text == "jazz music" and rig.tv.youtube.state == "results"
    assert rig.tv.dial_launches == []


async def test_submit_walks_the_keyboard_and_presses_search(rig):
    make_2024(rig)
    await youtube_ready(rig, search=True)
    r = await rig.call("type_text", text="relaxing piano", submit=True, key_delay_ms=50)
    assert r["ok"] and "then SEARCH" in r["text"]
    assert rig.tv.youtube.text == "relaxing piano" and rig.tv.youtube.state == "results_focus"
    assert rig.tv.dial_launches == []                    # the link is off unless switched on


async def test_youtube_link_is_a_per_tv_switch(rig):
    r = await rig.call("youtube_link", on=True)
    assert r["ok"] and r["youtube_link"] == "on" and "Multi View" in r["summary"]
    assert rig.restart().ctx["signals"]["living-room"]["youtube_link"] is True   # kept over a restart
    r = await rig.call("youtube_link", on=False)
    assert r["youtube_link"] == "off"
    with pytest.raises(ValueError):
        await rig.tools.call("youtube_link", {"on": "yes"})


async def test_youtube_search_by_link_from_any_screen(rig):
    make_2024(rig)
    await rig.call("youtube_link", on=True)
    sent = await youtube_ready(rig)                      # on YouTube's home screen, search not open
    r = await rig.call("type_text", text="relaxing jazz", submit=True)
    assert r["ok"] and "by link" in r["text"]
    assert rig.tv.dial_launches == [{"q": "relaxing jazz"}]
    assert rig.tv.youtube.text == "relaxing jazz" and rig.tv.youtube.state == "results_focus"
    assert rig.tv.keys[sent:] == []                      # not one key pressed


async def test_type_only_walks_youtubes_own_keyboard(rig):
    make_2024(rig)
    sent = await youtube_ready(rig, search=True)
    r = await rig.call("type_text", text="Jazz 42", key_delay_ms=50)
    assert r["ok"] and "Spelled 'jazz 42'" in r["text"] and "lower case" in r["text"]
    assert rig.tv.youtube.text == "jazz 42"
    assert rig.tv.youtube.state == "search"              # left in the box, not searched
    assert rig.tv.dial_launches == []
    assert set(rig.tv.keys[sent:]) <= {"KEY_LEFT", "KEY_RIGHT", "KEY_UP", "KEY_DOWN", "KEY_ENTER"}
    # a second call adds to the box; clear starts it over
    await rig.call("type_text", text=" mix", key_delay_ms=50)
    assert rig.tv.youtube.text == "jazz 42 mix"
    r = await rig.call("type_text", text="lofi", clear=True, key_delay_ms=50)
    assert rig.tv.youtube.text == "lofi" and "after CLEAR" in r["text"]


async def test_characters_without_a_key_are_left_out_and_named(rig):
    make_2024(rig)
    await youtube_ready(rig, search=True)
    r = await rig.call("type_text", text="café, déjà", key_delay_ms=50)
    assert r["ok"] and rig.tv.youtube.text == "caf dj"
    assert "Left out" in r["text"] and "é" in r["text"]
    r = await rig.call("type_text", text="é", key_delay_ms=50)
    assert not r["ok"] and "no key" in r["text"] and rig.tv.youtube.text == "caf dj"


async def test_no_false_success_when_the_text_has_nowhere_to_go(rig):
    make_2024(rig)
    rig.tv.launch(NETFLIX)
    r = await rig.call("type_text", text="stranger things", submit=True)
    assert not r["ok"] and r["text"].startswith("Not typed")
    assert rig.tv.keys == [] and rig.tv.dial_launches == []
    # and on the home screen of a TV whose keyboard is simply not open
    rig.tv.foreground = None
    r = await rig.call("type_text", text="hello")
    assert not r["ok"]


async def test_a_walk_that_would_take_too_long_is_refused(rig):
    make_2024(rig)
    sent = await youtube_ready(rig, search=True)
    r = await rig.call("type_text", text="z a " * 60, key_delay_ms=2000)
    assert not r["ok"] and "longer than" in r["text"] and rig.tv.keys[sent:] == []


@pytest.mark.parametrize("args", [{"text": ""}, {"text": "x" * 501}, {"text": "x", "key_delay_ms": 5}, {}])
async def test_type_text_rejects_bad_arguments(rig, args):
    with pytest.raises(ValueError):
        await rig.tools.call("type_text", args)


async def test_play_uses_the_link_and_presses_ok_once(rig):
    make_2024(rig)
    await rig.call("youtube_link", on=True)
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.youtube.playing == JAZZ and r["ok"]
    assert rig.tv.dial_launches == [{"q": JAZZ}]
    assert rig.tv.keys == ["KEY_ENTER"]                  # no menu walking, no typing
    assert any("by link" in s for s in r["steps"])
    assert any("first result" in s for s in r["not_verified"])
    assert rig.tv.closed == []                           # YouTube is not restarted for it


async def test_play_by_link_replaces_what_was_playing(rig):
    make_2024(rig)
    await rig.call("youtube_link", on=True)
    await rig.call("play", video="dQw4w9WgXcQ")
    r = await rig.call("play", query="relaxing piano", prefer="long")
    assert r["ok"] and rig.tv.youtube.playing == "relaxPIANO1"
    assert rig.tv.keys == ["KEY_ENTER", "KEY_ENTER"]


async def test_play_falls_back_to_keys_on_a_tv_that_refuses_the_link(rig):
    await rig.call("youtube_link", on=True)                         # switched on, but this TV's DIAL says no
    r = await rig.call("play", query="jazz music", prefer="long")
    assert rig.tv.youtube.playing == JAZZ and r["ok"]
    assert rig.tv.dial_launches == [] and "KEY_LEFT" in rig.tv.keys


async def test_play_without_the_link_spells_the_title_on_the_2024_tv(rig):
    make_2024(rig)
    r = await rig.call("play", video="dQw4w9WgXcQ")
    assert r["ok"] and rig.tv.youtube.playing == "dQw4w9WgXcQ"
    assert rig.tv.youtube.text == "rick astley - never gonna give you up"   # the title: an id has capitals
    assert rig.tv.dial_launches == [] and rig.tv.keys[-1] == "KEY_ENTER"
