"""The cursor walk over YouTube's own search keyboard: pure planning, no TV.

The layout and move rules were read from YouTube's TV web app; replay() applies
the same rules, so these tests show the plan is consistent with them, not that
the rules still match the app."""

import pytest

from tvremote.keyboards import DOWN, LEFT, OK, RIGHT, UP, YOUTUBE_HOME, Cursor, plan, replay

HOME = [k for k, n in YOUTUBE_HOME for _ in range(n)]


@pytest.mark.parametrize("text", ["jazz", "relaxing jazz music mix", "a", "zzz", "lofi 24/7", "it's 3-2 (live) & more",
                                  "x_y \"z\" 100% $5+ [hd]: ok?!"])
def test_plan_types_what_was_asked(text):
    walk = plan(text)
    assert walk.typed == text and walk.skipped == ""
    assert replay(walk.keys) == text
    assert walk.keys[:len(HOME)] == HOME


def test_plan_starts_from_the_top_right_key_and_takes_the_short_way():
    assert plan("g").keys[len(HOME):] == [LEFT, OK]              # one left of backspace
    assert plan("n").keys[len(HOME):] == [LEFT, DOWN, OK]
    assert plan("gg").keys[len(HOME):] == [LEFT, OK, OK]         # a repeated letter is one more OK


def test_space_is_reached_through_the_fourth_row_and_the_cursor_comes_back():
    keys = plan("z z").keys[len(HOME):]
    # backspace -> z: three left and three down, in some order. Space: down. Back up lands on z again.
    assert sorted(keys[:6]) == sorted([LEFT] * 3 + [DOWN] * 3)
    assert keys[6:] == [OK, DOWN, OK, UP, OK]


def test_digits_use_the_symbols_page_and_the_letters_are_left_showing():
    walk = plan("a1")
    assert replay(walk.keys) == "a1"
    cur = Cursor()
    for k in walk.keys[len(HOME):]:
        cur = cur.press(k)
    assert cur.page == 0


def test_capitals_are_lowered_and_keyless_characters_are_reported():
    walk = plan("Héllo, Wörld")
    assert walk.typed == "hllo wrld" and walk.skipped == "é,ö"
    assert replay(walk.keys) == "hllo wrld"


def test_clear_and_submit():
    walk = plan("jazz", clear=True, submit=True)
    assert replay(walk.keys, "old text") == "jazz"
    assert plan("jazz").keys != walk.keys


def test_the_walk_never_makes_a_move_the_app_would_take_elsewhere():
    """No Left from the first column (suggestions), no Down from the bottom row
    (results), no move onto a row with no key in that column."""
    cur = Cursor()
    for k in plan("the quick brown fox 0-9 v's \"q\" [ok]", clear=True, submit=True).keys[len(HOME):]:
        nxt = cur.press(k)
        assert nxt is not None, f"{k} from {cur.key()}"
        cur = nxt


def test_cursor_edges():
    assert Cursor(0, 0, 0).press(LEFT) is None
    assert Cursor(0, 0, 7).press(RIGHT) is None
    assert Cursor(0, 2, 7).press(DOWN) is None            # nothing under the language key
    assert Cursor(0, 3, 4).press(DOWN) == Cursor(0, 4, 0, 4)
    assert Cursor(0, 4, 2, 4).press(UP) == Cursor(0, 3, 4, 4)
    assert Cursor(0, 1, 7).press(OK) == Cursor(1, 1, 7)   # the page key swaps pages in place
