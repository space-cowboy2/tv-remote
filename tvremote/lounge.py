"""What the TV's YouTube app is playing, read through YouTube's own remote link.

A Samsung TV has no local way to say what is on screen. YouTube's app does
tell a phone that joins it as a remote ("lounge") which video is playing and
whether it is playing, paused or showing an ad. This module joins that way,
listens, and leaves again. That is the only evidence of playback there is.

It is read-only on purpose:
- it never sends a control command (no play, pause, queue or volume). The one
  request it may send, getNowPlaying, asks the app to repeat its state;
- it joins without a video, so nothing is cast and nothing is queued;
- it leaves as soon as it has its answer or the time is up.

Off unless the household switches it on for a TV (the playback_check tool).
The link is not a published API: YouTube can change it at any time. When it
does not answer, watch() raises LoungeError and the caller reports playback as
not confirmed, exactly as with the link off.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterator

API = "https://www.youtube.com/api/lounge"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
NAME = "TV Remote hub (status only)"
STATES = {"-1": "idle", "0": "ended", "1": "playing", "2": "paused", "3": "loading", "5": "cued", "1081": "ad"}
MOVING = ("playing", "ad")          # the picture is running
MAX_CHUNK = 200_000


class LoungeError(Exception):
    """The link could not be used (offline, refused, or its format changed)."""


def _post(url: str, form: dict[str, str], timeout: float, token: str = "") -> str:
    headers = {"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"}
    if token:
        headers["X-YouTube-LoungeId-Token"] = token
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise LoungeError(f"YouTube's link answered {e.code}") from e
    except (OSError, ValueError) as e:
        raise LoungeError(f"YouTube's link did not answer: {type(e).__name__}") from e


def token_for(screen_id: str, timeout: float = 6.0) -> str:
    """The short-lived token for one TV's YouTube app, from the screen id the TV itself hands out (DIAL)."""
    text = _post(f"{API}/pairing/get_lounge_token_batch", {"screen_ids": screen_id}, timeout)
    try:
        screens = json.loads(text)["screens"]
        return str(next(s["loungeToken"] for s in screens if s.get("loungeToken")))
    except (ValueError, KeyError, TypeError, StopIteration) as e:
        raise LoungeError("YouTube gave no token for this TV's YouTube app") from e


def parse_chunks(lines: Iterator[str]) -> Iterator[list[Any]]:
    """The link's framing: a line with a length, then that much JSON (a list of
    [number, [event, arguments...]]), possibly over several lines; repeated."""
    want, buf = 0, ""
    for line in lines:
        line = line.rstrip("\r\n")
        if not want:
            if line.strip().isdigit():
                want, buf = int(line), ""
            continue
        buf += line + "\n"
        if len(buf) < want:
            continue
        try:
            chunk = json.loads(buf)
        except ValueError:
            if len(buf) > want + MAX_CHUNK:   # the count is in units we can't be sure of; give up only when far past it
                raise LoungeError("YouTube's link sent something unreadable")
            continue
        want = 0
        if isinstance(chunk, list):
            yield chunk


class Status:
    """Folds the app's events into one picture of what is playing."""

    def __init__(self) -> None:
        self.sid = self.gsession = ""
        self.last_id = 0
        self.video: str | None = None
        self.state = "unknown"
        self.position: float | None = None
        self.duration: float | None = None
        self.heard = False            # the app has told us its state at least once
        self.gone = False             # the app left the link (closed, or the TV went off)

    def take(self, chunk: list[Any]) -> None:
        for item in chunk:
            if not (isinstance(item, list) and len(item) == 2 and isinstance(item[1], list) and item[1]):
                continue
            if isinstance(item[0], int):
                self.last_id = max(self.last_id, item[0])
            name, args = item[1][0], item[1][1:]
            data = args[0] if args and isinstance(args[0], dict) else {}
            if name == "c" and args:
                self.sid = str(args[0])
            elif name == "S" and args:
                self.gsession = str(args[0])
            elif name == "nowPlaying":
                self.heard = True
                self.video = str(data["videoId"]) if data.get("videoId") else None
                self._state(data)
                if not self.video:
                    self.state = "idle"
            elif name == "onStateChange":
                self.heard = True
                self._state(data)
            elif name == "onAdStateChange":
                self.heard = True
                if data.get("contentVideoId"):
                    self.video = str(data["contentVideoId"])
                if str(data.get("adState", "")) in ("1", "1081"):
                    self.state = "ad"
            elif name == "loungeScreenDisconnected":
                self.gone = True

    def _state(self, data: dict[str, Any]) -> None:
        if "state" in data:
            code = str(data["state"])
            self.state = STATES.get(code, f"unknown ({code[:6]})")
        for key, attr in (("currentTime", "position"), ("duration", "duration")):
            try:
                setattr(self, attr, round(float(data[key]), 1))
            except (KeyError, TypeError, ValueError):
                pass

    def settled(self, want: str | None) -> bool:
        """Enough to stop listening: the wanted video (or, with none wanted, any video) is running."""
        return self.state in MOVING and bool(self.video) and (not want or self.video == want)

    def as_dict(self) -> dict[str, Any]:
        return {"video": self.video, "state": self.state, "position": self.position, "duration": self.duration,
                "heard": self.heard}


def _listen(st: Status, token: str, want: str | None, until: float) -> None:
    """Read the app's events until the answer is in, the app leaves, or the time is up."""
    query = urllib.parse.urlencode({
        "name": NAME, "loungeIdToken": token, "SID": st.sid, "AID": st.last_id, "gsessionid": st.gsession,
        "device": "REMOTE_CONTROL", "app": "youtube-desktop", "VER": "8", "v": "2", "RID": "rpc", "CI": "0",
        "TYPE": "xmlhttp"})
    req = urllib.request.Request(f"{API}/bc/bind?{query}", headers={"User-Agent": UA, "X-YouTube-LoungeId-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=max(0.5, until - time.monotonic())) as r:
            def lines() -> Iterator[str]:
                while time.monotonic() < until:
                    raw = r.readline()
                    if not raw:
                        return
                    yield raw.decode("utf-8", "replace")
            for chunk in parse_chunks(lines()):
                st.take(chunk)
                if st.settled(want) or st.gone:
                    return
    except urllib.error.HTTPError as e:
        raise LoungeError(f"YouTube's link answered {e.code}") from e
    except TimeoutError:
        return                        # quiet until the deadline: whatever was heard so far stands
    except OSError as e:
        if "timed out" in str(e).lower():
            return
        raise LoungeError(f"YouTube's link broke off: {type(e).__name__}") from e


def watch_blocking(screen_id: str, want: str | None = None, seconds: float = 20.0) -> dict[str, Any]:
    """Join the TV's YouTube app as a silent remote and report what it plays:
    {video, state, position, duration, heard}. Waits up to `seconds` for the
    wanted video (or, with none wanted, any video) to be running, and returns
    the last state heard otherwise. One join, one leave."""
    until = time.monotonic() + seconds
    token = token_for(screen_id, timeout=min(6.0, seconds))
    st = Status()
    bind = f"{API}/bc/bind?" + urllib.parse.urlencode({"RID": 1, "VER": 8, "CVER": 1, "auth_failure_option": "send_error"})
    form = {"app": "web", "mdx-version": "3", "name": NAME, "id": str(uuid.uuid4()), "device": "REMOTE_CONTROL",
            "capabilities": "que,dsdtr,atp", "magnaKey": "cloudPairedDevice", "ui": "false", "theme": "cl",
            "deviceContext": "user_agent=dunno&window_width_points=&window_height_points=&os_name=android&ms=",
            "loungeIdToken": token}   # no "method": joining with one is how a video gets cast
    for chunk in parse_chunks(iter(_post(bind, form, min(8.0, seconds), token).splitlines())):
        st.take(chunk)
    if not (st.sid and st.gsession):
        raise LoungeError("YouTube's link did not open a session")
    session = {"SID": st.sid, "gsessionid": st.gsession, "VER": 8, "CVER": 1, "auth_failure_option": "send_error"}
    rid = 1

    def send(form: dict[str, str], timeout: float) -> None:
        nonlocal rid
        rid += 1
        _post(f"{API}/bc/bind?" + urllib.parse.urlencode({**session, "RID": rid}), form, timeout, token)

    try:
        asked = False
        for _ in range(8):            # each pass is one listen; the deadline normally ends it first
            if st.settled(want) or st.gone or time.monotonic() >= until:
                break
            began = time.monotonic()
            # The app normally announces its state to a remote that joins, so listen briefly at first.
            _listen(st, token, want, until if (asked or st.heard) else min(until, began + 4.0))
            if not st.heard and not asked and time.monotonic() < until:
                asked = True          # it said nothing: ask it to repeat its state (a question, not a control)
                send({"count": "1", "ofs": "1", "req0__sc": "getNowPlaying"}, 5.0)
            elif time.monotonic() - began < 0.5:
                time.sleep(0.5)       # the stream closed at once; don't hammer the link
    finally:
        try:                          # leave, so the TV does not keep a remote on its list
            send({"ui": "", "TYPE": "terminate",
                  "clientDisconnectReason": "MDX_SESSION_DISCONNECT_REASON_DISCONNECTED_BY_USER"}, 4.0)
        except LoungeError:
            pass
    return st.as_dict()


async def watch(screen_id: str, want: str | None = None, seconds: float = 20.0) -> dict[str, Any]:
    return await asyncio.to_thread(watch_blocking, screen_id, want, seconds)
