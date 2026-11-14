"""Key grids of apps that draw their own on-screen keyboard.

Samsung's text message (SendInputString) only feeds the TV's system keyboard.
YouTube's search draws its own grid and ignores it, and the remote channel has
no letter keys, so the only way to put text there without submitting a search
is to walk the cursor over the grid with the arrow keys and press OK on each
key. This module knows the grid and plans that walk. It does no I/O.

The YouTube layout and every move rule below were read from YouTube's TV web
app (youtube.com/tv, the app Tizen runs) on 2026-11-13:

- Left and Right move inside a row. Right stops at the row's last key. Left
  from the first key leaves the keyboard (into the suggestions), so the walk
  never does that.
- Up and Down keep the column in the four character rows. Up stops at the top
  row. The walk never moves onto a row that has no key in that column.
- Down from any key of the fourth row lands on SPACE. Up from the bottom row
  returns to the fourth-row key the cursor came down from.
- OK on the page key swaps letters and symbols; the cursor stays on that key.
- OK on any other key leaves the cursor where it is.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

LEFT, RIGHT, UP, DOWN, OK = "KEY_LEFT", "KEY_RIGHT", "KEY_UP", "KEY_DOWN", "KEY_ENTER"
BACKSPACE, PAGE, LANGUAGE, SPACE, CLEAR, SEARCH = "BACKSPACE", "PAGE", "LANGUAGE", "SPACE", "CLEAR", "SEARCH"

_BOTTOM = (SPACE, CLEAR, SEARCH)
YOUTUBE_PAGES: tuple[tuple[tuple[str, ...], ...], ...] = (
    (tuple("abcdefg") + (BACKSPACE,), tuple("hijklmn") + (PAGE,), tuple("opqrstu") + (LANGUAGE,),
     tuple("vwxyz-'"), _BOTTOM),
    (tuple("123&#()") + (BACKSPACE,), tuple("456@!?:") + (PAGE,), tuple('789.-_"'),
     tuple("0/$%+[]"), _BOTTOM),
)
# From the search screen's side menu entry, its suggestions or any key: onto the
# top-right key. Every press past an edge does nothing, so extra ones are safe.
YOUTUBE_HOME: tuple[tuple[str, int], ...] = ((RIGHT, 9), (UP, 4), (RIGHT, 8))
YOUTUBE_HOME_AT = (0, 7)


@dataclass(frozen=True)
class Cursor:
    """Where the cursor is: page, row, column, and the fourth-row column that Up
    from the bottom row returns to."""

    page: int = 0
    row: int = 0
    col: int = 7
    back: int = 0

    def key(self) -> str:
        return YOUTUBE_PAGES[self.page][self.row][self.col]

    def press(self, key: str) -> "Cursor | None":
        """The cursor after one remote key, or None for a move the walk must not
        make (it would leave the keyboard or land somewhere not established)."""
        rows = YOUTUBE_PAGES[self.page]
        last = len(rows) - 1
        p, r, c, b = self.page, self.row, self.col, self.back
        if key == LEFT:
            return Cursor(p, r, c - 1, b) if c > 0 else None
        if key == RIGHT:
            return Cursor(p, r, c + 1, b) if c < len(rows[r]) - 1 else None
        if key == UP:
            if r == last:
                return Cursor(p, last - 1, b, b)
            return Cursor(p, r - 1, c, b) if r > 0 and c < len(rows[r - 1]) else None
        if key == DOWN:
            if r == last:
                return None
            if r == last - 1:
                return Cursor(p, last, 0, c)
            return Cursor(p, r + 1, c, b) if c < len(rows[r + 1]) else None
        if key == OK:
            return Cursor(1 - p, r, c, b) if self.key() == PAGE else self
        return None


@dataclass(frozen=True)
class Walk:
    keys: list[str]   # remote keys, in order, starting from the home position
    typed: str        # the text those keys put in the box
    skipped: str      # characters the grid has no key for, in order of appearance


def _label(ch: str) -> str | None:
    if ch == " ":
        return SPACE
    ch = ch.lower()
    return ch if len(ch) == 1 and any(ch in row for page in YOUTUBE_PAGES for row in page[:-1]) else None


def _path(start: Cursor, label: str) -> tuple[list[str], Cursor]:
    """Fewest keys from start to having the cursor on `label`."""
    seen, queue = {start}, deque([(start, [])])
    while queue:
        cur, keys = queue.popleft()
        if cur.key() == label:
            return keys, cur
        for k in (LEFT, RIGHT, UP, DOWN, OK):
            if k == OK and cur.key() != PAGE:
                continue  # OK anywhere else would type something
            nxt = cur.press(k)
            if nxt is not None and nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, keys + [k]))
    raise ValueError(f"no key {label!r} on this keyboard")


def plan(text: str, *, clear: bool = False, submit: bool = False) -> Walk:
    """The key presses that type `text` on YouTube's search keyboard, starting
    with the moves that bring the cursor to a known key. clear presses CLEAR
    first; submit presses SEARCH last. Capitals are typed as lower case."""
    keys = [k for k, n in YOUTUBE_HOME for _ in range(n)]
    cur = Cursor(0, *YOUTUBE_HOME_AT, 0)
    typed, skipped = [], []

    def go(label: str) -> None:
        nonlocal cur
        steps, cur = _path(cur, label)
        keys.extend(steps + [OK])
        cur = cur.press(OK) or cur

    if clear:
        go(CLEAR)
    for ch in text:
        label = _label(ch)
        if label is None:
            if ch not in skipped:
                skipped.append(ch)
            continue
        go(label)
        typed.append(" " if label == SPACE else label)
    if cur.page:
        go(PAGE)  # leave the letters showing, where the next walk expects to start
    if submit:
        go(SEARCH)
    return Walk(keys, "".join(typed), "".join(skipped))


def replay(keys: list[str], text: str = "") -> str:
    """What a key sequence from plan() leaves in the box, by the same rules.
    Used by the simulated TV and the tests."""
    cur, out, i = None, list(text), 0
    home = [k for k, n in YOUTUBE_HOME for _ in range(n)]
    if keys[:len(home)] == home:
        cur, i = Cursor(0, *YOUTUBE_HOME_AT, 0), len(home)
    if cur is None:
        raise ValueError("the sequence does not start with the homing moves")
    for k in keys[i:]:
        if k == OK:
            label = cur.key()
            if label == CLEAR:
                out.clear()
            elif label == BACKSPACE:
                out[-1:] = []
            elif label == SPACE:
                out.append(" ")
            elif label not in (PAGE, LANGUAGE, SEARCH):
                out.append(label)
        nxt = cur.press(k)
        if nxt is None:
            raise ValueError(f"{k} from {cur.key()!r} leaves the keyboard")
        cur = nxt
    return "".join(out)
