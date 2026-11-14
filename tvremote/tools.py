"""Agent tools on top of the TV client: the contract served at /api/tools and used by
the Claude plugin and Duet. Notes, macros and context live next to tv.json so they
survive restarts and are shared by every client.

Two kinds of tools:
- outcome tools (play, open_app, set_volume, set_power, playback, ...) do the whole
  job in the right order, check every signal the TV offers, retry what can be
  retried, and answer with a report: what was confirmed, what was only sent, and
  what failed. The hub cannot see the screen, and no Samsung signal says whether a
  video is playing, so a report never claims playback; it says how far the
  evidence goes.
- remote tools (send_keys, type_text, run_macro, ...) press exactly what they are
  told and say so.

Which words map to which tool is the model's job, guided by guide.md. Nothing in
here matches phrases.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from . import discovery, keyboards, lounge, youtube
from .apps import APP_ALIASES, KNOWN_APPS, YOUTUBE_ID
from .config import TVConfig, extra_path, load_all, slugify
from .keys import is_valid_key, normalize_key
from .samsung import PairingDenied, PairingTimeout, SamsungTV, TVError, TVUnreachable
from .wol import lan_mac

SCHEMAS: list[dict[str, Any]] = json.loads((Path(__file__).with_name("tools.json")).read_text(encoding="utf-8"))
GUIDE: str = (Path(__file__).with_name("guide.md")).read_text(encoding="utf-8")
YOUTUBE, TWITCH = YOUTUBE_ID, "3202203026841"
NAME_RE = re.compile(r"^[a-z0-9_]{2,40}$")
MAX_STEPS, MAX_REPEAT, MAX_WAIT = 60, 50, 15.0
PLAY_DEADLINE = 170.0      # the plugin gives a call 180 s, the gateway 200 s
WAKE_SECONDS = 40.0
REPEAT_GUARD_SECONDS = 30  # the same play request inside this window is not run twice
PLAYBACK_CHECK_SECONDS = 20.0  # how long the read-only YouTube link listens for the video to be running
HISTORY = 20
SCHEDULABLE = {"set_power", "set_volume", "playback", "play", "open_app", "send_keys", "run_macro", "switch_input"}
PLAYBACK_KEYS = {"pause": "KEY_PAUSE", "resume": "KEY_PLAY", "stop": "KEY_STOP",
                 "forward": "KEY_FF", "rewind": "KEY_REWIND"}
SEEK_STEP = 10  # seconds one FF/REW press moves in most apps; uncalibrated
GRID_KEY_DELAY_MS = 200   # between cursor keys when spelling on an app's own keyboard
GRID_MAX_SECONDS = 150.0  # a walk longer than this would outlast the caller's patience (plugin: 180 s)
GRID_TITLE_CHARS = 60     # play spells at most this much of a title; enough to bring the video up first
LINK_SETTLE = (5.0, 12.0)  # seconds for YouTube to show a linked search: already running, cold start
INPUT_KEYS = {"hdmi": "KEY_HDMI", "hdmi1": "KEY_HDMI1", "hdmi2": "KEY_HDMI2", "hdmi3": "KEY_HDMI3",
              "hdmi4": "KEY_HDMI4", "tv": "KEY_TV", "source": "KEY_SOURCE"}

# Key sequences for what the TV's APIs can't do directly. Each is a default that
# save_macro can replace once someone has watched what the screen does.
DEFAULT_MACROS: dict[str, dict[str, Any]] = {
    "youtube_open_search": {
        "description": "play: from YouTube's home screen to its open search box (side menu, up to Search, OK). "
                       "This order worked on a 2024 Samsung OLED on 2026-11-06.",
        "steps": [{"key": "KEY_LEFT"}, {"wait": 1.5}, {"key": "KEY_UP"}, {"wait": 1}, {"key": "KEY_ENTER"}],
    },
    "youtube_pick_first": {
        "description": "play: after the search text is submitted, move to the first result and start it.",
        "steps": [{"key": "KEY_RIGHT"}, {"wait": 1}, {"key": "KEY_ENTER"}],
    },
    "youtube_search": {
        "description": "The old all-in-one YouTube sequence. play no longer uses it unless you save your own "
                       "version under this name; then play runs yours instead of its own steps.",
        "steps": [{"launch": YOUTUBE}, {"wait": 8}, {"key": "KEY_LEFT"}, {"wait": 1.5}, {"key": "KEY_UP"}, {"wait": 1},
                  {"key": "KEY_ENTER"}, {"wait": 2.5}, {"text": "{value}", "submit": True}, {"wait": 4},
                  {"key": "KEY_RIGHT"}, {"wait": 1}, {"key": "KEY_ENTER"}],
    },
    "twitch_search": {
        "description": "open_twitch: open Twitch, open search, type {value}, open the first channel. Uncalibrated.",
        "steps": [{"launch": TWITCH}, {"wait": 8}, {"key": "KEY_LEFT"}, {"key": "KEY_UP", "repeat": 6},
                  {"key": "KEY_ENTER"}, {"wait": 2}, {"text": "{value}", "submit": True}, {"wait": 4},
                  {"key": "KEY_DOWN"}, {"key": "KEY_ENTER"}],
    },
}


class UnknownTool(Exception):
    pass


class Fail(Exception):
    """An outcome that could not be reached. kind is a stable label; needs_user
    means only the owner can fix it (press Allow, plug the TV in, pair a TV)."""

    def __init__(self, kind: str, message: str, *, needs_user: bool = False, hint: str = "") -> None:
        super().__init__(message)
        self.kind, self.message, self.needs_user, self.hint = kind, message, needs_user, hint


class Result(str):
    """A tool's answer. ok is False when the requested outcome was not reached."""

    ok = True

    @classmethod
    def of(cls, text: str, ok: bool = True) -> "Result":
        r = cls(text)
        r.ok = ok
        return r


class Report:
    """Collects the evidence for one outcome and renders the answer."""

    def __init__(self, tv: str) -> None:
        self.tv = tv
        self.verified: list[str] = []
        self.unverified: list[str] = []
        self.steps: list[str] = []
        self.extra: dict[str, Any] = {}

    def render(self, summary: str, *, goal_confirmed: bool, fail: Fail | None = None,
               partial: str = "") -> Result:
        if fail:
            status = "failed"
        elif partial:
            status = "partial"
        elif goal_confirmed:
            status = "verified"
        elif self.verified:
            status = "partly_verified"
        else:
            status = "sent_not_verified"
        out: dict[str, Any] = {"ok": not fail and not partial, "status": status, "summary": summary, "tv": self.tv}
        if fail:
            out["failed"] = {"kind": fail.kind, "why": fail.message, "needs_user": fail.needs_user}
            if fail.hint:
                out["failed"]["hint"] = fail.hint
        if partial:
            out["not_done"] = partial
        if self.verified:
            out["verified"] = list(dict.fromkeys(self.verified))
        if self.unverified:
            out["not_verified"] = list(dict.fromkeys(self.unverified))
        if self.steps:
            out["steps"] = self.steps
        out.update(self.extra)
        return Result.of(json.dumps(out, ensure_ascii=False), bool(out["ok"]))


class Store:
    """notes.md, macros.json and context.json in one folder; writes are atomic."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.notes_path = folder / "notes.md"
        self.macros_path = folder / "macros.json"
        self.context_path = folder / "context.json"

    def notes(self) -> str:
        try:
            return self.notes_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def remember(self, note: str) -> None:
        self._write(self.notes_path, self.notes() + f"- {note}\n")

    def saved_macros(self) -> dict[str, Any]:
        """Only what save_macro wrote (the owner's calibrations)."""
        try:
            saved = json.loads(self.macros_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return saved if isinstance(saved, dict) else {}

    def macros(self) -> dict[str, Any]:
        return {**DEFAULT_MACROS, **self.saved_macros()}

    def save_macro(self, name: str, macro: dict[str, Any]) -> None:
        saved = self.saved_macros()
        saved[name] = macro
        self._write(self.macros_path, json.dumps(saved, indent=2))

    def context(self) -> dict[str, Any]:
        try:
            ctx = json.loads(self.context_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return ctx if isinstance(ctx, dict) else {}

    def save_context(self, ctx: dict[str, Any]) -> None:
        try:
            self._write(self.context_path, json.dumps(ctx, indent=1))
        except OSError:
            pass  # context is a convenience; never fail a TV action over it

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


class TVHandle:
    """One paired TV: its client, its locks and what the hub knows about it."""

    def __init__(self, slug: str, label: str, tv: SamsungTV, lock: asyncio.Lock | None = None,
                 model: str | None = None) -> None:
        self.slug, self.label, self.tv, self.model = slug, label, tv, model
        self.lock = lock or asyncio.Lock()  # one message to the TV at a time
        self.seq = asyncio.Lock()           # one multi-step action at a time
        self.doing: str | None = None
        self.apps: list[dict[str, Any]] = []
        self.apps_at = 0.0
        self.save_lan_mac: Any = None  # set for a saved TV: writes the address the network sees to its file
        self.lan_mac_checked = False

    @property
    def title(self) -> str:
        return self.label if not self.model or self.model in self.label else f"{self.label} ({self.model})"


def handle_from_config(slug: str, cfg: TVConfig, path: Path, **tv_args: Any) -> TVHandle:
    """A TVHandle for a saved TV; a renewed pairing token is written back to its file."""

    def save_token(token: str) -> None:
        cfg.token = token
        cfg.save(path)

    def save_lan_mac(mac: str) -> None:
        cfg.lan_mac = mac
        cfg.save(path)

    tv = SamsungTV(cfg.host, name=cfg.name, token=cfg.token, mac=cfg.mac, lan_mac=cfg.lan_mac, on_token=save_token, **tv_args)
    h = TVHandle(slug, cfg.label or cfg.model or "TV", tv, model=cfg.model)
    h.save_lan_mac = save_lan_mac
    return h


def load_handles(config_path: Path, **tv_args: Any) -> list[TVHandle]:
    """Every TV paired on this machine: tv.json (the default) and tv-<name>.json."""
    return [handle_from_config(slug, cfg, path, **tv_args) for slug, cfg, path in load_all(config_path)]


def _key(k: Any) -> str:
    if not isinstance(k, str) or not is_valid_key(k):
        raise ValueError(f"unknown key {k!r}")
    return normalize_key(k)


def _count(n: Any, hi: int) -> int:
    if n is None:
        return 1
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= hi:
        raise ValueError(f"repeat must be 1..{hi}")
    return n


def check_steps(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"steps must be a list of 1..{MAX_STEPS} steps")
    for s in steps:
        if not isinstance(s, dict) or len({"key", "wait", "launch", "text"} & s.keys()) != 1:
            raise ValueError(f"each step needs exactly one of key, wait, launch, text: {s!r}")
        if "key" in s:
            _key(s["key"]); _count(s.get("repeat"), MAX_REPEAT)
        elif "wait" in s and not (isinstance(s["wait"], (int, float)) and 0 <= s["wait"] <= MAX_WAIT):
            raise ValueError(f"wait must be 0..{MAX_WAIT} s")
        elif "launch" in s and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(s["launch"])):
            raise ValueError("bad app id")
        elif "text" in s and not (isinstance(s["text"], str) and len(s["text"]) <= 500):
            raise ValueError("text must be a string up to 500 chars")
    return steps


def youtube_query(video: str) -> str:
    """URL or id -> the 11-char id; else the words."""
    v = video.strip()
    return youtube.extract_video_id(v) or v


def _norm(text: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9+]+", " ", str(text).lower()).split())


def _number(a: dict[str, Any], name: str, lo: float, hi: float, *, whole: bool = False) -> float | None:
    v = a.get(name)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi or (whole and v != int(v)):
        raise ValueError(f"{name} must be {'a whole number ' if whole else ''}{lo:g}..{hi:g}")
    return v


class Tools:
    def __init__(self, tvs: "SamsungTV | list[TVHandle]", lock: asyncio.Lock | None = None,
                 store: Store | None = None, config_path: Path | None = None,
                 tv_args: dict[str, Any] | None = None) -> None:
        if isinstance(tvs, SamsungTV):  # the single-TV form the hub started with
            tvs = [TVHandle("tv", "TV", tvs, lock)]
        self.tvs: dict[str, TVHandle] = {h.slug: h for h in tvs}
        self.store = store or Store(Path("."))
        # Where paired TVs are saved. None: this hub was handed its TV and reads no files.
        self.config_path = config_path
        self.tv_args = tv_args or {}  # extra SamsungTV arguments (tests point these at the simulated TV)
        self._loaded_at = 0.0
        self.ctx: dict[str, Any] = {"active": None, "playing": {}, "history": [], "timers": [], "signals": {},
                                    **self.store.context()}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._seen: list[dict[str, str]] = []  # other devices from the last network scan
        # Replaceable so tests need neither the internet nor multicast.
        self.yt_search = youtube.search
        self.yt_lookup = youtube.lookup
        self.scan = discovery.scan
        self.lan_mac = lan_mac
        self.youtube_now = lounge.watch
        for h in self.tvs.values():
            h.tv.sleep = self.sleep

    def reload(self, force: bool = False) -> None:
        """Pick up TVs paired since the hub started (`tvremote pair` in another window)."""
        if not self.config_path or (not force and time.monotonic() - self._loaded_at < 5):
            return
        self._loaded_at = time.monotonic()
        try:
            found = load_handles(self.config_path, **self.tv_args)
        except (OSError, ValueError, TypeError):
            return
        hosts = {h.tv.host for h in self.tvs.values()}
        for h in found:
            if h.slug not in self.tvs and h.tv.host not in hosts:
                h.tv.sleep = self.sleep
                self.tvs[h.slug] = h

    # tv and lock of the default TV, as the single-TV code and tests know them
    @property
    def tv(self) -> SamsungTV:
        return self._default().tv

    @property
    def lock(self) -> asyncio.Lock:
        return self._default().lock

    async def sleep(self, seconds: float) -> None:  # a method so tests can patch it
        await asyncio.sleep(seconds)

    async def timer_sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def list(self) -> dict[str, Any]:
        return {"tools": SCHEMAS, "notes": self.store.notes(), "guide": GUIDE}

    async def call(self, name: str, a: dict[str, Any]) -> Result:
        """Run a tool. Bad arguments raise ValueError; everything that can go wrong
        with a TV comes back as a failed report, never as an exception."""
        fn = getattr(self, "t_" + name, None) if re.fullmatch(r"[a-z_]{2,30}", name) else None
        if fn is None:
            raise UnknownTool(name)
        try:
            out = await fn(a)
        except (Fail, TVError) as e:
            fail = self._as_fail(e)
            self._log(name, None, fail.message, "failed")
            return Report("").render(fail.message, goal_confirmed=False, fail=fail)
        return out if isinstance(out, Result) else Result.of(str(out))

    @staticmethod
    def _as_fail(e: Exception) -> Fail:
        if isinstance(e, Fail):
            return e
        if isinstance(e, (PairingDenied, PairingTimeout)):
            return Fail("pairing_needed", f"The TV did not accept the hub's connection ({e}).", needs_user=True,
                        hint="An Allow prompt may be showing on the TV: press Allow. If the TV keeps refusing, "
                             "pair again with pair_tv while someone is at the TV.")
        if isinstance(e, TVUnreachable):
            return Fail("tv_unreachable", f"The TV is off or not answering ({e}).",
                        hint="If it should be on, set_power on wakes it; get_state shows what it reports.")
        return Fail("tv_error", str(e))

    # ------------------------------------------------------------------ helpers

    def _default(self) -> TVHandle:
        if not self.tvs:
            self.reload(force=True)  # one small file read; worth it on every call until a TV exists
        if not self.tvs:
            raise Fail("no_tv_paired", "No TV is paired with the hub yet.", needs_user=True,
                       hint="find_tvs lists the TVs on the network; pair_tv connects one while someone is at "
                            "the TV to press Allow.")
        return next(iter(self.tvs.values()))

    def _active(self) -> TVHandle:
        return self.tvs.get(self.ctx.get("active") or "") or self._default()

    def _tv(self, a: dict[str, Any], *, activate: bool = True) -> TVHandle:
        """The TV a call is about: the one named in `tv`, else the one last used."""
        self._default()
        want = a.get("tv")
        if want in (None, ""):
            return self._active()
        w = _norm(want)
        if w != "other" and not self._match(w):
            self.reload()
        if w == "other":
            rest = [h for h in self.tvs.values() if h is not self._active()]
            if len(rest) == 1:
                h = rest[0]
            else:
                raise Fail("unknown_tv", self._tv_choices("There is no single other TV."), needs_user=len(rest) != 0)
        else:
            close = self._match(w)
            if len(close) != 1:
                raise Fail("unknown_tv", self._tv_choices(f"No single paired TV matches {str(want)[:40]!r}."),
                           needs_user=len(close) > 1)
            h = close[0]
        if activate and self.ctx.get("active") != h.slug:
            self.ctx["active"] = h.slug
            self._save()
        return h

    def _match(self, w: str) -> list[TVHandle]:
        exact = [h for h in self.tvs.values() if w in (h.slug, _norm(h.slug), _norm(h.label), _norm(h.model or ""))]
        return exact or [h for h in self.tvs.values()
                         if w and (w in _norm(h.label) or w in _norm(h.model or "") or w in _norm(h.slug))]

    def _tv_choices(self, lead: str) -> str:
        paired = ", ".join(f"{h.slug} = {h.title}" for h in self.tvs.values())
        seen = "; ".join(f"{d.get('name') or d.get('model') or d['host']} ({d.get('maker') or d['kind']}, not controllable by this hub)"
                         for d in self._seen if d["kind"] != "samsung") if self._seen else ""
        return f"{lead} Paired TVs: {paired}." + (f" Also on the network: {seen}." if seen else "")

    def _save(self) -> None:
        self.ctx["history"] = self.ctx.get("history", [])[-HISTORY:]
        self.store.save_context(self.ctx)

    def _log(self, tool: str, h: TVHandle | None, summary: str, status: str) -> None:
        self.ctx.setdefault("history", []).append(
            {"at": int(time.time()), "tool": tool, "tv": h.slug if h else None, "status": status, "summary": summary[:200]})
        self._save()

    @asynccontextmanager
    async def _busy(self, h: TVHandle, what: str, wait: float = 30.0):
        """One multi-step action per TV at a time, so key sequences never interleave."""
        waited = 0.0
        while h.seq.locked():
            if waited >= wait:
                raise Fail("busy", f"The TV is in the middle of something else ({h.doing or 'another request'}). "
                                   "Wait for it to finish, then try again.")
            await self.sleep(0.5)
            waited += 0.5
        await h.seq.acquire()
        h.doing = what
        try:
            yield
        finally:
            h.doing = None
            h.seq.release()

    async def _run(self, tool: str, a: dict[str, Any], body: Any, *, busy: str | None = None,
                   busy_wait: float = 30.0, deadline: float = 120.0) -> Result:
        """Run an outcome tool: resolve the TV, hold its sequence lock, and turn
        every foreseeable failure into a report instead of an exception."""
        h: TVHandle | None = None
        rep = Report("")
        try:
            h = self._tv(a)
            rep.tv = h.title
            async with asyncio.timeout(deadline):
                if busy:
                    async with self._busy(h, busy, busy_wait):
                        out = await body(h, rep)
                else:
                    out = await body(h, rep)
            self._log(tool, h, json.loads(out).get("summary", ""), json.loads(out).get("status", ""))
            return out
        except TimeoutError:
            fail = Fail("timeout", f"Gave up after {deadline:g} s; the steps listed were done before that.")
        except (Fail, TVError) as e:
            fail = self._as_fail(e)
        out = rep.render(fail.message, goal_confirmed=False, fail=fail)
        self._log(tool, h, fail.message, "failed")
        return out

    async def _keys(self, key: str, repeat: int = 1, delay: float = 0.3, h: TVHandle | None = None) -> None:
        h = h or self._active()
        for i in range(repeat):
            async with h.lock:
                await h.tv.send_key(key)
            if i < repeat - 1:
                await self.sleep(delay)

    async def _steps(self, steps: list[dict[str, Any]], value: str = "", h: TVHandle | None = None) -> None:
        h = h or self._active()
        for s in steps:
            if "key" in s:
                await self._keys(_key(s["key"]), _count(s.get("repeat"), MAX_REPEAT), h=h)
            elif "wait" in s:
                await self.sleep(float(s["wait"]))
            elif "launch" in s:
                async with h.lock:
                    await h.tv.launch_app(str(s["launch"]))
            else:
                async with h.lock:
                    await h.tv.send_text(s["text"].replace("{value}", value)[:500], bool(s.get("submit")))
            await self.sleep(0.3)

    def _signals(self, h: TVHandle) -> dict[str, Any]:
        return self.ctx.setdefault("signals", {}).setdefault(h.slug, {})

    def _keyboard_known(self, h: TVHandle) -> bool:
        """Has this TV ever announced its on-screen keyboard? If so, silence means
        the search box did not open. keyboard_events is True once an announcement
        was seen, False after a whole run of tries stayed silent, absent before."""
        if h.tv.ime_seen and not self._signals(h).get("keyboard_events"):
            self._signals(h)["keyboard_events"] = True
            self._save()
        return bool(self._signals(h).get("keyboard_events"))

    async def _wait_keyboard(self, h: TVHandle, mark: int, seconds: float) -> bool:
        waited = 0.0
        while True:
            if h.tv.ime_open and h.tv.ime_changes > mark:
                self._keyboard_known(h)
                return True
            if waited >= seconds:
                return False
            await self.sleep(0.5)
            waited += 0.5

    async def _wait_text(self, h: TVHandle, text: str, seconds: float) -> bool:
        waited = 0.0
        while True:
            if h.tv.ime_text == text:
                self._keyboard_known(h)
                return True
            if waited >= seconds:
                return False
            await self.sleep(0.5)
            waited += 0.5

    async def _ensure_on(self, h: TVHandle, rep: Report) -> None:
        tv = h.tv
        state = await tv.power_state()
        woke = state != "on"
        if woke:
            rep.steps.append(f"The TV was {state}; sent a wake-up.")
            try:
                ok = await tv.power_on(wait=WAKE_SECONDS)
            except (TVUnreachable, PairingDenied, PairingTimeout):
                raise
            except TVError as e:
                raise Fail("tv_did_not_wake", str(e), needs_user=True) from e
            if not ok:
                raise Fail("tv_did_not_wake", f"The TV did not turn on within {WAKE_SECONDS:g} s of the wake-up.",
                           needs_user=True,
                           hint="Check that it has power and is on the network, and that Power On with Mobile is on "
                                "(Settings > General > Network > Expert Settings).")
        rep.verified.append("The TV is on (it reports PowerState on)." if tv.power_source == "tv" else
                            "The TV is on (its app and volume services answer, though its status port did not).")
        await self._learn_lan_mac(h)
        if not await tv.wait_ready(12.0 if woke else 4.0):
            raise Fail("tv_unreachable", "The TV is on, but its remote-control channel does not accept a connection.",
                       hint="This usually clears within a minute of the TV waking up. Try again.")
        if woke:
            await self.sleep(4)  # the home screen ignores launches for a moment after waking

    async def _learn_lan_mac(self, h: TVHandle) -> None:
        """Once per run, while the TV is on: note the address this network has for it. That is the one a
        wake-up must go to, and for a TV on a cable it is not the one the TV reports."""
        if h.lan_mac_checked:
            return
        h.lan_mac_checked = True
        try:
            mac = await asyncio.to_thread(self.lan_mac, h.tv.host)
            if mac and mac != h.tv.lan_mac:
                h.tv.lan_mac = mac
                if h.save_lan_mac:
                    h.save_lan_mac(mac)
        except Exception:  # a wake-up address is a nicety here; never fail the request over it
            pass

    async def _apps(self, h: TVHandle, refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or not h.apps or time.monotonic() - h.apps_at > 600:
            async with h.lock:
                h.apps = await h.tv.list_apps()
            h.apps_at = time.monotonic()
        return h.apps

    async def _resolve_app(self, h: TVHandle, name: str) -> tuple[str, str]:
        """(app id, app name) for what someone called an app."""
        raw = name.strip()
        if re.fullmatch(r"\d{8,}|[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+", raw):
            return raw, KNOWN_APPS.get(raw, raw)
        want = _norm(APP_ALIASES.get(_norm(raw), raw))
        if not want:
            raise ValueError("app is required")
        try:
            installed = [(str(a["id"]), str(a["name"])) for a in await self._apps(h) if a.get("id") and a.get("name")]
        except TVUnreachable:
            raise
        except TVError:
            installed = []
        known = list(KNOWN_APPS.items())
        for pool in (installed, known):
            for test in (lambda n: n == want, lambda n: n.startswith(want), lambda n: want in n):
                hits = sorted({(i, n) for i, n in pool if test(_norm(n))}, key=lambda x: len(x[1]))
                if hits:
                    return hits[0]
        names = ", ".join(sorted({n for _, n in installed})) or "unknown (the TV did not list its apps)"
        raise Fail("unknown_app", f"No app on this TV matches {raw[:40]!r}. Installed: {names}.")

    async def _open_app(self, h: TVHandle, app_id: str, name: str, rep: Report, *, fresh: bool = False) -> str:
        """Launch an app and wait for the TV to report it up. Returns the app
        state the TV gave: visible, running, or unknown (no signal on this TV)."""
        tv = h.tv
        before = await tv.app_state(app_id)
        if before == "missing":
            raise Fail("app_not_installed", f"{name} is not installed on this TV.")
        if fresh and before in ("visible", "running"):
            if await tv.close_app(app_id):
                waited = 0.0
                while waited < 6 and await tv.app_state(app_id) in ("visible", "running"):
                    await self.sleep(1)
                    waited += 1
                rep.steps.append(f"Closed {name} first so it starts from its home screen.")
            else:
                await self._keys("KEY_HOME", h=h)
                await self.sleep(2)
        state = "unknown"
        for attempt in (1, 2):
            async with h.lock:
                try:
                    how = await tv.launch_app(app_id)
                except TVUnreachable:
                    raise
                except TVError as e:
                    if "not installed" in str(e):
                        raise Fail("app_not_installed", f"{name} is not installed on this TV.") from e
                    raise
            waited = 0.0
            while waited < 12:
                await self.sleep(1)
                waited += 1
                state = await tv.app_state(app_id)
                if state in ("visible", "running", "unknown"):
                    break
            if state in ("visible", "running", "unknown"):
                break
            rep.steps.append(f"{name} did not come up within 12 s of launch {attempt}.")
        if state == "visible":
            rep.verified.append(f"{name} is on screen (the TV reports it visible).")
        elif state == "running":
            rep.verified.append(f"{name} is running (the TV reports it started).")
        elif state == "unknown":
            rep.unverified.append(f"{name} was launched ({how}), but this TV gives no signal to confirm it opened.")
        else:
            raise Fail("app_did_not_open", f"{name} was launched twice and the TV still reports it as not running.")
        return state

    # ------------------------------------------------------------ state and TVs

    async def _tv_state(self, h: TVHandle) -> dict[str, Any]:
        tv = h.tv
        power = await tv.power_state()
        st: dict[str, Any] = {"id": h.slug, "name": h.title, "power": power, "connected": tv.connected}
        if tv.power_source == "services":
            st["power_note"] = "from its app/volume services; its status port did not answer"
        if power == "on":
            vol, mute, yt = await asyncio.gather(tv.volume(), tv.muted(), tv.dial_state("YouTube"))
            if vol is not None:
                st["volume"] = vol
            if mute is not None:
                st["muted"] = mute
            if yt:
                st["youtube_app"] = yt["state"]
            st["can_read"] = {"volume": vol is not None, "app_state": bool(yt) or bool(tv.rest_apps_ok),
                              "search_box_opening": self._keyboard_known(h),
                              "playback": bool(self._signals(h).get("playback_check"))}
        playing = self.ctx.get("playing", {}).get(h.slug)
        if playing:
            st["last_started"] = {**{k: v for k, v in playing.items() if k != "at"},
                                  "minutes_ago": round((time.time() - playing.get("at", 0)) / 60, 1),
                                  "note": "what the hub last started here, not what is on screen now"}
        st["playback_check"] = "on" if self._signals(h).get("playback_check") else "off"
        if h.doing:
            st["busy_with"] = h.doing
        return st

    async def t_get_state(self, a: dict[str, Any]) -> str:
        try:
            self._default()
        except Fail as f:
            return Result.of(json.dumps({"ok": False, "tvs": [], "failed": {"kind": f.kind, "why": f.message,
                                                                           "needs_user": True, "hint": f.hint}}), False)
        only = self._tv(a, activate=False) if a.get("tv") else None
        tvs = await asyncio.gather(*(self._tv_state(h) for h in ([only] if only else self.tvs.values())))
        now = time.time()
        out = {
            "tvs": list(tvs),
            "active_tv": self._active().slug,
            "timers": [{"id": t["id"], "in_minutes": round(max(0.0, t["due"] - now) / 60, 1), "does": t["tool"],
                        "args": t["args"], "note": t.get("note", "")} for t in self.ctx.get("timers", [])],
            "recent": [{"minutes_ago": round((now - e["at"]) / 60, 1), **{k: e[k] for k in ("tool", "tv", "status", "summary")}}
                       for e in self.ctx.get("history", [])[-6:]],
        }
        others = [d for d in self._seen if d["kind"] != "samsung"]
        if others:
            out["seen_but_not_controllable"] = others
        return json.dumps(out, ensure_ascii=False)

    async def t_get_status(self, a: dict[str, Any]) -> str:
        return await self.t_get_state(a)

    async def t_use_tv(self, a: dict[str, Any]) -> str:
        if not a.get("tv"):
            raise ValueError("tv is required")
        try:
            h = self._tv(a)
        except Fail as f:
            return Report("").render(f.message, goal_confirmed=False, fail=f)
        return json.dumps({"ok": True, "active_tv": h.slug, "state": await self._tv_state(h)}, ensure_ascii=False)

    async def t_find_tvs(self, a: dict[str, Any]) -> str:
        self.reload(force=True)
        devices = await self.scan(3.0)
        paired = {h.tv.host: h for h in self.tvs.values()}
        self._seen = [d for d in devices if d["host"] not in paired]
        out = []
        for d in devices:
            h = paired.get(d["host"])
            entry = {**d, "paired": bool(h)}
            if h:
                entry["id"] = h.slug
            elif d["kind"] == "samsung":
                entry["to_add"] = f"pair_tv with host {d['host']}, while someone is at that TV to press Allow"
            else:
                entry["note"] = "not a Samsung TV; this hub cannot control it"
            out.append(entry)
        missing = [{"id": h.slug, "name": h.title, "host": h.tv.host, "paired": True, "note": "did not answer the scan (off, or on another network segment)"}
                   for host, h in paired.items() if host not in {d["host"] for d in devices}]
        return json.dumps({"found": out + missing}, ensure_ascii=False)

    async def t_pair_tv(self, a: dict[str, Any]) -> str:
        host = str(a.get("host") or "").strip()
        label = " ".join(str(a.get("label") or "").split())[:40]
        try:
            local = not ipaddress.ip_address(host).is_global
        except ValueError:
            local = False
        if not local:
            raise ValueError("host must be the TV's address on the home network, e.g. 192.168.1.50 (see find_tvs)")
        if not self.config_path:
            raise Fail("unsupported", "This hub was started with a fixed TV and cannot pair another.")
        self.reload(force=True)
        known = next((h for h in self.tvs.values() if h.tv.host == host), None)
        rep = Report(label or host)
        cfg = TVConfig(host=host, label=label or (known.label if known else None))
        got: dict[str, str] = {}
        tv = SamsungTV(host, name=cfg.name, on_token=lambda t: got.update(token=t), pair_timeout=45.0, **self.tv_args)
        try:
            device = None
            for attempt in range(3):  # the status port of some models stalls for seconds at a time
                try:
                    device = (await tv.info(timeout=3.0)).get("device") or {}
                    break
                except TVUnreachable as e:
                    if attempt == 2:
                        raise Fail("tv_unreachable", f"Nothing answers as a Samsung TV at {host}.",
                                   hint="The TV must be on and on this network. find_tvs shows what is there.") from e
                    await self.sleep(1)
            model = device.get("modelName")
            if not model or model == "0":  # some 2024 models report "0"
                model = device.get("model") or device.get("name")
            cfg.mac, cfg.model = device.get("wifiMac"), model
            cfg.label = cfg.label or SamsungTV.clean_name(device.get("name")) or model
            rep.tv = f"{cfg.label} ({model})" if model and model not in str(cfg.label) else str(cfg.label)
            rep.verified.append(f"A Samsung TV answers at {host} ({model}).")
            try:
                await tv.connect()
            except PairingTimeout as e:
                raise Fail("pairing_needed", "The TV showed its Allow prompt and nobody pressed Allow in time.",
                           needs_user=True, hint="Call pair_tv again when someone is at the TV with its remote.") from e
            except PairingDenied as e:
                raise Fail("pairing_denied", "The TV refused the connection (Deny was pressed, or this hub is blocked).",
                           needs_user=True,
                           hint="On the TV: Settings > General > External Device Manager > Device Connection Manager "
                                "> Device List, remove or allow 'TV Remote', then pair again.") from e
        finally:
            await tv.close()
        cfg.token = got.get("token") or cfg.token
        try:
            cfg.lan_mac = await asyncio.to_thread(self.lan_mac, host)
        except Exception:
            cfg.lan_mac = None
        primary = TVConfig.load(self.config_path)
        if known:
            path = next((p for s, _, p in load_all(self.config_path) if s == known.slug), self.config_path)
            slug = known.slug
        elif primary is None:
            path, slug = self.config_path, slugify(cfg.label or "tv")
        else:
            slug = slugify(cfg.label or "tv")
            n = 2
            while slug in self.tvs:
                slug, n = f"{slugify(cfg.label or 'tv')[:36]}-{n}", n + 1
            path = extra_path(slug, self.config_path)
        cfg.save(path)
        old = self.tvs.pop(slug, None)
        if old:
            await old.tv.close()
        h = handle_from_config(slug, cfg, path, **self.tv_args)
        h.tv.sleep = self.sleep
        self.tvs[slug] = h
        rep.verified.append("The TV accepted the hub and its pairing is saved.")
        self._log("pair_tv", h, f"Paired {h.title}", "verified")
        rep.extra["id"] = slug
        return rep.render(f"Paired with {h.title}. It can be controlled now.", goal_confirmed=True)

    # --------------------------------------------------------------------- power

    async def t_set_power(self, a: dict[str, Any]) -> str:
        state = a.get("state")
        if state not in ("on", "off"):
            raise ValueError("state must be on or off")

        async def body(h: TVHandle, rep: Report) -> Result:
            if state == "on":
                await self._ensure_on(h, rep)
                return rep.render("The TV is on.", goal_confirmed=True)
            async with h.lock:
                off = await h.tv.power_off(wait=12.0)
            self.ctx.get("playing", {}).pop(h.slug, None)
            if off:
                rep.verified.append("The TV no longer reports being on.")
                return rep.render("The TV is off.", goal_confirmed=True)
            raise Fail("tv_did_not_turn_off", "Power was pressed, but the TV still reports being on after 12 s.")

        return await self._run("set_power", a, body, busy=f"turning the TV {state}", deadline=90)

    # -------------------------------------------------------------------- volume

    async def t_set_volume(self, a: dict[str, Any]) -> str:
        level = _number(a, "level", 0, 100, whole=True)
        change = _number(a, "change", -50, 50, whole=True)
        mute = a.get("mute")
        if mute is not None and not isinstance(mute, bool):
            raise ValueError("mute must be true or false")
        if level is None and not change and mute is None:
            raise ValueError("give level (0-100), change (+/- steps) or mute (true/false)")
        if level is not None and change:
            raise ValueError("give level or change, not both")
        use_keys = a.get("method") == "keys"

        async def body(h: TVHandle, rep: Report) -> Result:
            tv = h.tv
            if await tv.power_state() != "on":
                raise Fail("tv_is_off", "The TV is off, so its volume can't be changed.",
                           hint="Turn it on first (set_power on) if that is what the request implies.")
            current = None if use_keys else await tv.volume()
            said, confirmed = [], True
            if level is not None or change:
                if current is not None:
                    target = int(level) if level is not None else max(0, min(100, current + int(change or 0)))
                    after = await tv.set_volume(target)
                    if after == target:
                        rep.verified.append(f"Volume is {after} (was {current}); the TV reports the new level.")
                        said.append(f"Volume is now {after} (was {current}).")
                    else:
                        raise Fail("volume_not_set", f"Asked for volume {target}, but the TV reports {after}.")
                elif change:
                    key = "KEY_VOLUP" if change > 0 else "KEY_VOLDOWN"
                    await self._keys(key, abs(int(change)), 0.25, h=h)
                    rep.unverified.append(f"Pressed {key} x{abs(int(change))}; this TV does not report its volume level.")
                    said.append(f"Pressed volume {'up' if change > 0 else 'down'} {abs(int(change))} times.")
                    confirmed = False
                else:
                    raise Fail("volume_level_unreadable",
                               "This TV does not report or accept an exact volume level.",
                               hint="Use change (+/- steps) instead, e.g. change=-5.")
            if mute is not None:
                was = None if use_keys else await tv.muted()
                if was is not None:
                    now_muted = was if was == mute else await tv.set_mute(mute)
                    if now_muted != mute:
                        raise Fail("mute_not_set", f"Asked for mute={mute}, but the TV reports mute={now_muted}.")
                    rep.verified.append(f"The TV reports it is {'muted' if mute else 'not muted'}.")
                    said.append("Muted." if mute else "Unmuted.")
                else:
                    await self._keys("KEY_MUTE", h=h)
                    rep.unverified.append("Pressed Mute, which toggles; this TV does not report whether it is muted now.")
                    said.append("Pressed Mute.")
                    confirmed = False
            return rep.render(" ".join(said), goal_confirmed=confirmed)

        return await self._run("set_volume", a, body, busy="changing the volume", deadline=60)

    # -------------------------------------------------------------------- apps

    async def t_list_apps(self, a: dict[str, Any]) -> str:
        h = self._tv(a)
        apps = await self._apps(h, refresh=True)
        return json.dumps(sorted(apps, key=lambda x: (x.get("name") or "").lower()))

    async def t_open_app(self, a: dict[str, Any]) -> str:
        name = str(a.get("app") or a.get("app_id") or "").strip()
        if not name:
            raise ValueError("app is required")

        async def body(h: TVHandle, rep: Report) -> Result:
            await self._ensure_on(h, rep)
            app_id, app_name = await self._resolve_app(h, name)
            state = await self._open_app(h, app_id, app_name, rep)
            self.ctx.setdefault("playing", {})[h.slug] = {"service": app_name, "what": "the app itself", "at": time.time()}
            return rep.render(f"{app_name} is open." if state != "unknown" else f"Launched {app_name}.",
                              goal_confirmed=state in ("visible", "running"))

        return await self._run("open_app", a, body, busy=f"opening {name[:30]}", deadline=110)

    async def t_launch_app(self, a: dict[str, Any]) -> str:
        app_id = str(a.get("app_id", ""))
        check_steps([{"launch": app_id}])
        return await self.t_open_app({**a, "app": app_id})

    async def t_switch_input(self, a: dict[str, Any]) -> str:
        key = INPUT_KEYS.get(_norm(a.get("input", "")).replace(" ", ""))
        if not key:
            raise ValueError("input must be one of " + ", ".join(INPUT_KEYS))

        async def body(h: TVHandle, rep: Report) -> Result:
            await self._ensure_on(h, rep)
            await self._keys(key, h=h)
            rep.unverified.append(f"Pressed {key}; the TV does not report which input it is showing.")
            return rep.render(f"Pressed the {a['input']} input key.", goal_confirmed=False)

        return await self._run("switch_input", a, body, busy="switching input", deadline=90)

    # ------------------------------------------------------------------- playing

    async def t_search_youtube(self, a: dict[str, Any]) -> str:
        query = " ".join(str(a.get("query", "")).split())
        prefer = a.get("prefer") or "auto"
        if not query:
            raise ValueError("query is required")
        if prefer not in youtube.PREFER:
            raise ValueError("prefer must be auto, long or live")
        try:
            results = await asyncio.to_thread(self.yt_search, query, prefer)
        except youtube.YouTubeError as e:
            return Result.of(json.dumps({"ok": False, "failed": {"kind": "youtube_search_failed", "why": str(e),
                                                                "needs_user": False,
                                                                "hint": "play still works: give it the words as query and it types them on the TV."}}), False)
        best = youtube.pick(results, prefer)
        return json.dumps({"ok": True, "results": [{k: v for k, v in r.items() if k != "seconds"} for r in results],
                           "suggested": best["id"] if best else None}, ensure_ascii=False)

    async def _resolve_youtube(self, a: dict[str, Any], h: TVHandle, rep: Report) -> dict[str, Any]:
        """Settle on one video and on the text to type on the TV for it."""
        given = str(a.get("video") or "").strip()
        query = " ".join(str(a.get("query") or "").split())
        prefer = a.get("prefer") or "auto"
        video_id = youtube.extract_video_id(given) if given else None
        target: dict[str, Any] | None = None
        results: list[dict[str, Any]] = []
        if video_id:
            target = await asyncio.to_thread(self.yt_lookup, video_id)
            if target is None and "/" not in given and "." not in given and not query:
                query, video_id = given, None  # an 11-letter word, not an id
            elif target is None:
                target = {"id": video_id, "title": None, "channel": None, "length": None, "live": False}
                rep.unverified.append("Could not look the video up on YouTube, so its title is unknown.")
        if not video_id:
            query = query or given
            exclude = {str(x) for x in a.get("exclude") or []}
            if a.get("avoid_recent"):
                exclude |= {e["video"] for e in self.ctx.get("history", []) if e.get("video")}
                last = self.ctx.get("playing", {}).get(h.slug, {}).get("id")
                if last:
                    exclude.add(last)
            try:
                results = await asyncio.to_thread(self.yt_search, query, prefer)
            except youtube.YouTubeError as e:
                rep.unverified.append(f"YouTube could not be searched from the hub ({e}), so no specific video was "
                                      f"chosen in advance: the TV plays its own first result for {query!r}.")
                return {"id": None, "title": None, "type": query, "query": query}
            target = youtube.pick(results, prefer, exclude)
            if not target:
                raise Fail("nothing_found", f"YouTube has no videos for {query!r}.", hint="Try other words.")
        assert target is not None
        # What to type on the TV. The id is exact when YouTube's search returns the
        # video for it, which it does for most videos but not for every live stream.
        typed = target["id"]
        try:
            first = await asyncio.to_thread(self.yt_search, typed, "auto")
            if not first or first[0]["id"] != target["id"]:
                title = " ".join(re.sub(r"[^\w\s'&-]", " ", target.get("title") or "").split())[:80]
                by_title = await asyncio.to_thread(self.yt_search, title, "auto") if title else []
                if by_title and by_title[0]["id"] == target["id"]:
                    typed = title
                elif title:
                    typed = title
                    rep.unverified.append("YouTube's search does not put this exact video first for its id or its "
                                          "title, so the TV's first result may be a different upload.")
        except youtube.YouTubeError:
            pass
        out = {k: target.get(k) for k in ("id", "title", "channel", "length", "live")}
        out.update(type=typed, query=query or None)
        if results:
            out["others"] = [{k: r.get(k) for k in ("id", "title", "channel", "length", "live")}
                             for r in results if r["id"] != target["id"]][:4]
        return out

    async def _walk(self, h: TVHandle, walk: keyboards.Walk, delay_ms: int = GRID_KEY_DELAY_MS) -> None:
        """Send a planned cursor walk, one remote key at a time."""
        for key in walk.keys:
            async with h.lock:
                await h.tv.send_key(key)
            await self.sleep(delay_ms / 1000)

    async def _youtube_by_link(self, h: TVHandle, target: dict[str, Any], rep: Report) -> bool:
        """Hand YouTube the search as a launch parameter, then press OK on the first
        result, which the app focuses by itself. Nothing is typed. Only for a TV
        where the household switched the link on (youtube_link): the launch goes
        through DIAL, which a Samsung TV can treat like a phone cast and answer by
        opening Multi View. False when the link is off or the TV did not take it;
        the caller then opens the search box with keys instead."""
        tv = h.tv
        if not self._signals(h).get("youtube_link"):
            return False
        before = await tv.app_state(YOUTUBE)
        if before == "missing" or not await tv.dial_launch(YOUTUBE, {"q": target["type"]}):
            return False
        state, waited = "stopped", 0.0
        while waited < 15:
            await self.sleep(1)
            waited += 1
            state = await tv.app_state(YOUTUBE)
            if state in ("visible", "running"):
                break
        if state not in ("visible", "running"):
            rep.steps.append("The TV accepted YouTube's search link, but YouTube did not come up; using keys instead.")
            return False
        rep.verified.append("YouTube is on screen (the TV reports it visible)." if state == "visible"
                            else "YouTube is running (the TV reports it started).")
        rep.steps.append(f"Opened YouTube's results for {target['type']!r} by link; nothing was typed.")
        await self.sleep(LINK_SETTLE[before not in ("visible", "running")])  # results load; nothing reports when
        await self._keys("KEY_ENTER", h=h)
        rep.unverified.append("That the first result was the one started: the TV accepted the search link and OK "
                              "was pressed, but the hub can't see the results.")
        await self.sleep(2)
        if await tv.app_state(YOUTUBE) == "stopped":
            raise Fail("app_closed", "YouTube is no longer running after the result was selected.")
        return True

    async def _youtube_on_tv(self, h: TVHandle, target: dict[str, Any], rep: Report) -> None:
        """Open YouTube, search for target["type"] and start the first result."""
        tv, macros = h.tv, self.store.macros()
        if "youtube_search" in self.store.saved_macros():
            await self._steps(check_steps(macros["youtube_search"]["steps"]), target["type"], h)
            rep.unverified.append("Ran your saved youtube_search key sequence; none of its steps can be confirmed.")
            return
        if await self._youtube_by_link(h, target, rep):
            return
        # No link on this TV: open the search box with keys and type into it.
        # Tries: a clean start; the same keys again where the app now stands (gets
        # past a "Who's watching?" screen or a slow start); a clean start with a
        # longer wait. A TV that stayed silent through a whole run before gets one.
        silent_tv = self._signals(h).get("keyboard_events") is False and not tv.ime_seen
        opened = False
        for attempt, settle in ((1, 8.0),) if silent_tv else ((1, 8.0), (2, 0.0), (3, 14.0)):
            if attempt != 2:
                await self._open_app(h, YOUTUBE, "YouTube", rep, fresh=True)
                await self.sleep(settle)  # the home screen loads; nothing reports when it is ready
            else:
                await self.sleep(3)
            mark = tv.ime_changes
            await self._steps(check_steps(macros["youtube_open_search"]["steps"]), h=h)
            opened = await self._wait_keyboard(h, mark, 5.0)
            if opened:
                break
            if not silent_tv:
                rep.steps.append(f"Try {attempt}: no sign that YouTube's search box opened.")
        if opened:
            rep.verified.append("YouTube's search box opened (the TV announced its on-screen keyboard).")
        elif self._keyboard_known(h):
            if await tv.close_app(YOUTUBE):  # don't leave whatever the stray keys started
                rep.steps.append("Closed YouTube again so nothing unintended keeps playing.")
            raise Fail("search_box_not_found", "YouTube opened, but its search box did not open in three tries.",
                       hint="The key steps may not fit this TV's YouTube layout. Have someone watch the screen "
                            "during one run, then correct them with save_macro under the name youtube_open_search.")
        else:
            if not silent_tv:
                self._signals(h)["keyboard_events"] = False
                self._save()
            rep.unverified.append("That YouTube's search box opened: the keys for it were pressed, but this TV has "
                                  "never announced its on-screen keyboard to the hub. If the wrong thing comes up, "
                                  "the steps saved as youtube_open_search may not fit this TV.")
        async with h.lock:
            await tv.send_text(target["type"])
        echoed = await self._wait_text(h, target["type"], 3.0)
        if echoed:
            rep.verified.append(f"The TV's search box holds the text {target['type']!r}.")
        elif opened:
            rep.unverified.append("The search text was sent; the TV did not echo it back.")
        if echoed or opened:
            async with h.lock:
                await tv.submit_text()
            await self.sleep(4)  # results load
            await self._steps(check_steps(macros["youtube_pick_first"]["steps"]), h=h)
        else:
            # No keyboard announced and no echo: this YouTube draws its own keyboard,
            # which takes nothing from the TV's text message. Spell it with the cursor.
            # An id can't be spelled there (no capitals), so the title is.
            words = (target.get("title") or target["type"])[:GRID_TITLE_CHARS]
            walk = keyboards.plan(words, clear=True, submit=True)
            await self._walk(h, walk)
            rep.steps.append(f"Spelled {walk.typed!r} on YouTube's own keyboard with the cursor "
                             f"({len(walk.keys)} key presses) and pressed SEARCH.")
            rep.unverified.append("That the text reached YouTube's search box: it was spelled key by key on the app's "
                                  "own keyboard, which reports nothing back.")
            await self.sleep(5)  # results load, and the app moves to the first one by itself
            await self._keys("KEY_ENTER", h=h)
        await self.sleep(2)
        if await tv.app_state(YOUTUBE) == "stopped":
            raise Fail("app_closed", "YouTube is no longer running after the result was selected.")

    async def t_play(self, a: dict[str, Any]) -> str:
        service = str(a.get("service") or "youtube").strip()
        prefer = a.get("prefer") or "auto"
        if prefer not in youtube.PREFER:
            raise ValueError("prefer must be auto, long or live")
        if not (a.get("query") or a.get("video")):
            raise ValueError("give query (what to find) or video (a YouTube link or id)")
        if a.get("exclude") is not None and not isinstance(a["exclude"], list):
            raise ValueError("exclude must be a list of video ids")
        if _norm(APP_ALIASES.get(_norm(service), service)) != "youtube":
            return await self._play_elsewhere(a, service)
        wanted = _norm(a.get("video") or a.get("query"))

        async def body(h: TVHandle, rep: Report) -> Result:
            last = self.ctx.get("playing", {}).get(h.slug) or {}
            if last.get("asked") == wanted and time.time() - last.get("at", 0) < REPEAT_GUARD_SECONDS and not a.get("avoid_recent"):
                rep.extra["playing"] = {k: last.get(k) for k in ("id", "title", "channel", "length", "live")}
                rep.unverified.append("Not repeated: the same request was carried out on this TV "
                                      f"{int(time.time() - last['at'])} s ago.")
                return rep.render(f"Already started {last.get('title') or wanted!r} a moment ago.", goal_confirmed=False)
            target = await self._resolve_youtube(a, h, rep)
            rep.extra["playing"] = {k: target.get(k) for k in ("id", "title", "channel", "length", "live")}
            if target.get("others"):
                rep.extra["other_matches"] = target["others"]
            await self._ensure_on(h, rep)
            await self._youtube_on_tv(h, target, rep)
            confirmed, summary, not_done = await self._confirm_playing(h, target, rep, exact=bool(a.get("video")))
            self.ctx.setdefault("playing", {})[h.slug] = {
                "service": "YouTube", "id": target.get("id"), "title": target.get("title"), "channel": target.get("channel"),
                "length": target.get("length"), "live": target.get("live"), "asked": wanted, "at": time.time()}
            if target.get("id"):
                self.ctx["history"].append({"at": int(time.time()), "tool": "played", "tv": h.slug, "status": "",
                                            "summary": target.get("title") or "", "video": target["id"]})
            what = target.get("title") or f"the first YouTube result for {target['type']!r}"
            return rep.render(summary or f"Started {what} on YouTube; playback itself is not confirmed.",
                              goal_confirmed=confirmed, partial=not_done)

        return await self._run("play", a, body, busy=f"starting {wanted[:40]!r} on YouTube", busy_wait=60,
                               deadline=PLAY_DEADLINE)

    async def _confirm_playing(self, h: TVHandle, target: dict[str, Any], rep: Report, *,
                               exact: bool) -> tuple[bool, str, str]:
        """Is the video really running? Only the TV's YouTube app knows, and it is asked only
        when the read-only link is switched on for this TV (playback_check). Returns
        (confirmed, summary or "", what was not done or ""); the report gets the evidence."""
        doubt = "That the video is actually playing: "
        if not self._signals(h).get("playback_check"):
            rep.unverified.append(doubt + "the first search result was selected, and this TV reports no playback state.")
            return False, "", ""
        screen = (await h.tv.dial_state("YouTube") or {}).get("screen_id")
        if not screen:
            rep.unverified.append(doubt + "the playback check is on, but the TV gave no id for its YouTube app to ask.")
            return False, "", ""
        want = target.get("id")
        try:
            now = await self.youtube_now(screen, want, PLAYBACK_CHECK_SECONDS)
        except lounge.LoungeError as e:
            rep.unverified.append(doubt + f"the playback check could not ask YouTube on the TV ({e}).")
            return False, "", ""
        video, state = now.get("video"), now.get("state")
        rep.extra["on_tv"] = {"video": video, "state": state, "position": now.get("position")}
        what = target.get("title") or f"the first YouTube result for {target['type']!r}"
        if video and state in lounge.MOVING and want and video != want:
            # The first result on the TV was not the video the hub had settled on. Say what is on instead.
            actual = await asyncio.to_thread(self.yt_lookup, video) or {}
            rep.extra["chosen"] = rep.extra.get("playing")
            target.update({"id": video, "title": actual.get("title"), "channel": actual.get("channel"),
                           "length": actual.get("length"), "live": bool(actual.get("live"))})
            rep.extra["playing"] = {k: target.get(k) for k in ("id", "title", "channel", "length", "live")}
            named = actual.get("title") or f"another video (id {video})"
            rep.verified.append(f"YouTube on the TV reports that it is playing {named}, not the video the hub chose.")
            if exact:
                return False, f"Started the first result, but it is {named}, not the video asked for.", \
                    f"Playing the exact video asked for ({what})."
            return False, f"YouTube on the TV is playing {named}; the hub had chosen {what}.", ""
        if video and state == "playing":
            at = f", {int(now['position'])} s in" if now.get("position") else ""
            rep.verified.append(f"YouTube on the TV reports that it is playing {'this video' if want else f'video id {video}'}{at}.")
            return True, f"Playing {what} on YouTube; the TV's YouTube app confirms it.", ""
        if video and state == "ad":
            rep.verified.append("YouTube on the TV reports an ad running ahead of this video.")
            rep.unverified.append("That the video itself has started: the ad was still running when the check ended.")
            return False, f"Started {what} on YouTube; an ad was still running when the check ended.", ""
        if not now.get("heard"):
            rep.unverified.append(doubt + "YouTube on the TV did not answer the playback check in time.")
        else:
            rep.unverified.append(doubt + f"YouTube on the TV reports {str(state)!r}"
                                  + (f" for video id {video}." if video else " with no video loaded."))
        return False, "", ""

    async def _play_elsewhere(self, a: dict[str, Any], service: str) -> Result:
        """Any app other than YouTube: open it; search inside it only with a saved sequence."""
        query = " ".join(str(a.get("query") or "").split())

        async def body(h: TVHandle, rep: Report) -> Result:
            await self._ensure_on(h, rep)
            app_id, name = await self._resolve_app(h, service)
            macro_name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") + "_search"
            macro = self.store.saved_macros().get(macro_name)
            state = await self._open_app(h, app_id, name, rep, fresh=bool(macro))
            self.ctx.setdefault("playing", {})[h.slug] = {"service": name, "what": query or "the app itself", "at": time.time()}
            if macro:
                await self.sleep(8)
                await self._steps(check_steps(macro["steps"]), query, h)
                rep.unverified.append(f"Ran your saved {macro_name} key sequence for {query!r}; what it selected can't be confirmed.")
                return rep.render(f"Opened {name} and ran its saved search sequence for {query!r}.", goal_confirmed=False)
            return rep.render(
                f"{name} is {'open' if state != 'unknown' else 'launched'}, but nothing was searched or selected in it.",
                goal_confirmed=False,
                partial=f"Finding {query!r} inside {name}: the hub can only search inside YouTube. Other apps use their "
                        f"own on-screen keyboards that the hub cannot type into or see. A key sequence saved as "
                        f"{macro_name} (save_macro) would be used here.")

        return await self._run("play", a, body, busy=f"opening {service[:30]}", busy_wait=60, deadline=PLAY_DEADLINE)

    async def t_play_youtube(self, a: dict[str, Any]) -> str:
        if not str(a.get("video", "")).strip():
            raise ValueError("video is required")
        return await self.t_play({**a, "service": "youtube"})

    async def t_playback(self, a: dict[str, Any]) -> str:
        action = a.get("action")
        seconds = _number(a, "seconds", 1, 3600)
        if action not in (*PLAYBACK_KEYS, "next", "previous"):
            raise ValueError("action must be one of pause, resume, stop, rewind, forward, next, previous")

        async def body(h: TVHandle, rep: Report) -> Result:
            if await h.tv.power_state() != "on":
                raise Fail("tv_is_off", "The TV is off, so there is nothing to control.")
            saved = self.store.saved_macros().get(f"playback_{action}")
            if saved:
                await self._steps(check_steps(saved["steps"]), str(int(seconds or SEEK_STEP)), h)
                rep.unverified.append(f"Ran your saved playback_{action} key sequence; the TV reports no playback state.")
                return rep.render(f"Ran the saved {action} sequence.", goal_confirmed=False)
            if action in ("next", "previous"):
                raise Fail("no_known_way", f"No remote key reliably skips to the {action} item in TV apps.",
                           hint="On YouTube, start the next thing by name with play. If the owner finds keys that work "
                                f"in an app, save them as the macro playback_{action}.")
            presses = max(1, min(30, round((seconds or SEEK_STEP) / SEEK_STEP))) if action in ("rewind", "forward") else 1
            await self._keys(PLAYBACK_KEYS[action], presses, 0.35, h=h)
            playing = self.ctx.get("playing", {}).get(h.slug)
            if playing and action in ("pause", "resume", "stop"):
                playing["believed"] = {"pause": "paused", "resume": "playing", "stop": "stopped"}[action]
            rep.unverified.append(f"Pressed {PLAYBACK_KEYS[action]} x{presses}; the TV reports no playback state"
                                  + (f", and one press is assumed to move about {SEEK_STEP} s." if presses > 1 or seconds else "."))
            return rep.render(f"Pressed {action}" + (f" {presses} times" if presses > 1 else "") + ".", goal_confirmed=False)

        return await self._run("playback", a, body, busy=f"playback {action}", deadline=60)

    async def t_skip_ad(self, a: dict[str, Any]) -> str:
        await self._keys("KEY_ENTER", h=self._tv(a))
        return "Pressed OK (skips the ad if its Skip button was showing; otherwise it paused)."

    async def t_open_twitch(self, a: dict[str, Any]) -> str:
        ch = str(a.get("channel", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{2,25}", ch):
            raise ValueError("channel must be a Twitch login name")
        await self.t_run_macro({**a, "name": "twitch_search", "value": ch})
        return f"Searched Twitch for {ch} (macro twitch_search, uncalibrated). Check the TV."

    # --------------------------------------------------------------------- timers

    def start(self) -> None:
        """Re-arm timers saved before a restart. Call once the event loop runs."""
        for t in list(self.ctx.get("timers", [])):
            if t["id"] not in self._timers:
                self._timers[t["id"]] = asyncio.create_task(self._fire(t))

    async def stop(self) -> None:
        for task in self._timers.values():
            task.cancel()
        self._timers.clear()

    async def _fire(self, t: dict[str, Any]) -> None:
        late = time.time() - t["due"]
        if late <= 300:  # a timer found more than 5 minutes overdue after a restart is dropped
            await self.timer_sleep(max(0.0, -late))
            try:
                out = await self.call(t["tool"], dict(t["args"]))
                status = "timer ran" if out.ok else "timer failed"
            except Exception as e:  # a timer must never take the hub down
                status = f"timer failed: {type(e).__name__}"
        else:
            status = "timer missed (hub was down)"
        self.ctx["timers"] = [x for x in self.ctx.get("timers", []) if x["id"] != t["id"]]
        self._timers.pop(t["id"], None)
        self._log(t["tool"], self.tvs.get(t["args"].get("tv") or ""), f"{status}: {t.get('note') or t['tool']}", status)

    async def t_schedule(self, a: dict[str, Any]) -> str:
        minutes = _number(a, "in_minutes", 0.1, 24 * 60)
        tool, args = a.get("tool"), a.get("args") or {}
        if minutes is None:
            raise ValueError("in_minutes is required")
        if tool not in SCHEDULABLE:
            raise ValueError("tool must be one of " + ", ".join(sorted(SCHEDULABLE)))
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        if len(self.ctx.get("timers", [])) >= 10:
            raise ValueError("10 timers are already pending; cancel one first")
        try:
            args = {**args, "tv": self._tv(args).slug}  # pin the TV meant now, not the one active later
        except Fail as f:
            return Report("").render(f.message, goal_confirmed=False, fail=f)
        t = {"id": f"t{int(time.time() * 1000) % 100000}", "due": time.time() + minutes * 60, "tool": tool, "args": args,
             "note": " ".join(str(a.get("note", "")).split())[:120]}
        self.ctx.setdefault("timers", []).append(t)
        self._save()
        self._timers[t["id"]] = asyncio.create_task(self._fire(t))
        return json.dumps({"ok": True, "timer": t["id"], "runs_in_minutes": minutes, "does": tool, "args": args,
                           "note": "Held in the hub; it survives a hub restart but not the PC being off at that time."})

    async def t_cancel_schedule(self, a: dict[str, Any]) -> str:
        which = a.get("id")
        gone = [t for t in self.ctx.get("timers", []) if which in (None, "", "all", t["id"])]
        for t in gone:
            task = self._timers.pop(t["id"], None)
            if task:
                task.cancel()
        self.ctx["timers"] = [t for t in self.ctx.get("timers", []) if t not in gone]
        self._save()
        return json.dumps({"ok": bool(gone), "cancelled": [t["id"] for t in gone]})

    # ---------------------------------------------------------- the plain remote

    async def t_send_keys(self, a: dict[str, Any]) -> str:
        keys = a.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError("keys must be a non-empty list")
        delay = a.get("delay_ms", 300)
        if not isinstance(delay, int) or not 0 <= delay <= 5000:
            raise ValueError("delay_ms must be 0..5000")
        plan = [(_key((k or {}).get("key")), _count((k or {}).get("repeat"), MAX_REPEAT)) for k in keys]
        h = self._tv(a)
        sent = []
        async with self._busy(h, "pressing keys"):
            for key, n in plan:
                await self._keys(key, n, delay / 1000, h=h)
                sent.append(f"{key} x{n}")
                await self.sleep(delay / 1000)
        return "Sent " + ", ".join(sent) + ". (Sent only: the hub can't see what the TV did with them.)"

    async def t_type_text(self, a: dict[str, Any]) -> Result:
        """Three ways to get text onto the TV, tried in this order: the TV's own
        keyboard (confirmed by its echo), YouTube's search link (submit), and a
        cursor walk over YouTube's key grid (type only). Anything else is
        reported as not typed rather than as sent."""
        text = a.get("text")
        if not isinstance(text, str) or not text or len(text) > 500:
            raise ValueError("text must be a non-empty string up to 500 chars")
        submit, clear = bool(a.get("submit")), bool(a.get("clear"))
        delay = a.get("key_delay_ms", GRID_KEY_DELAY_MS)
        if not isinstance(delay, int) or not 50 <= delay <= 2000:
            raise ValueError("key_delay_ms must be 50..2000")
        h = self._tv(a)
        tv = h.tv
        try:
            async with self._busy(h, "typing"):
                async with h.lock:
                    await tv.send_text(text)
                echoed = await self._wait_text(h, text, 1.5)
                if echoed or tv.ime_open:
                    if submit:
                        await self.sleep(0.3)
                        async with h.lock:
                            await tv.submit_text()
                    how = "the TV echoed it back" if echoed else "its keyboard is open, but it did not echo the text"
                    return Result.of(f"Typed {len(text)} characters into the TV's keyboard ({how})"
                                     + (" and submitted." if submit else "."))
                state = await tv.app_state(YOUTUBE)
                if state != "visible" and not (state == "running" and tv.rest_apps_ok is False):
                    return Result.of(
                        "Not typed. The TV's own keyboard is not open (it did not take the text), and the app on screen "
                        "is not one whose keyboard the hub knows. Only YouTube's is known so far.", False)
                if submit and self._signals(h).get("youtube_link") and await tv.dial_launch(YOUTUBE, {"q": text}):
                    return Result.of(f"Searched YouTube for {text!r} by link; nothing had to be typed. "
                                     "(Sent and accepted by the TV: the hub can't see the results.)")
                walk = keyboards.plan(text, clear=clear, submit=submit)
                if not walk.typed:
                    return Result.of(f"Not typed: YouTube's keyboard has no key for any of {walk.skipped!r}.", False)
                if len(walk.keys) * delay / 1000 > GRID_MAX_SECONDS:
                    return Result.of(f"Not typed: {len(walk.keys)} key presses at {delay} ms each is longer than "
                                     f"{GRID_MAX_SECONDS:g} s. Send shorter text or a smaller key_delay_ms.", False)
                await self._walk(h, walk, delay)
        except (Fail, TVError) as e:
            return Result.of("Not typed. " + self._as_fail(e).message, False)
        out = (f"Spelled {walk.typed!r} on YouTube's search keyboard with {len(walk.keys)} key presses"
               + (", after CLEAR" if clear else "") + (", then SEARCH" if submit else "") + ".")
        if walk.skipped:
            out += f" Left out {walk.skipped!r}: that keyboard has no key for them."
        if walk.typed != text and not walk.skipped:
            out += " Capitals were typed as lower case."
        return Result.of(out + " (Sent only: this assumes YouTube's search screen was showing with its letters page, "
                               "and the hub can't see the box.)")

    async def t_wait(self, a: dict[str, Any]) -> str:
        s = a.get("seconds")
        if not isinstance(s, (int, float)) or not 0 <= s <= MAX_WAIT:
            raise ValueError(f"seconds must be 0..{MAX_WAIT}")
        await self.sleep(float(s))
        return f"Waited {s} s."

    async def t_playback_check(self, a: dict[str, Any]) -> str:
        if not isinstance(a.get("on"), bool):
            raise ValueError("on must be true or false")
        try:
            h = self._tv(a, activate=False)
        except Fail as f:
            return Report("").render(f.message, goal_confirmed=False, fail=f)
        self._signals(h)["playback_check"] = a["on"]
        self._save()
        self._log("playback_check", h, "on" if a["on"] else "off", "")
        return json.dumps({"ok": True, "tv": h.title, "playback_check": "on" if a["on"] else "off", "summary": (
            "On: after play, the hub asks this TV's YouTube app what is playing and reports it. It never starts, "
            "queues or changes anything there." if a["on"] else
            "Off: nothing connects to this TV's YouTube app; play reports playback as not confirmed.")}, ensure_ascii=False)

    async def t_youtube_link(self, a: dict[str, Any]) -> str:
        if not isinstance(a.get("on"), bool):
            raise ValueError("on must be true or false")
        try:
            h = self._tv(a, activate=False)
        except Fail as f:
            return Report("").render(f.message, goal_confirmed=False, fail=f)
        self._signals(h)["youtube_link"] = a["on"]
        self._save()
        self._log("youtube_link", h, "on" if a["on"] else "off", "")
        return json.dumps({"ok": True, "tv": h.title, "youtube_link": "on" if a["on"] else "off", "summary": (
            "On: play, and type_text with submit, hand YouTube the search as a launch link: instant, nothing typed. "
            "This TV may open Multi View (split screen) when the link arrives, unless its Autorun Multi View Casting "
            "setting is off." if a["on"] else
            "Off: searches are spelled on YouTube's own keyboard with the cursor, about a second per character.")},
            ensure_ascii=False)

    async def t_remember(self, a: dict[str, Any]) -> str:
        note = " ".join(str(a.get("note", "")).split())
        if not note or len(note) > 300:
            raise ValueError("note must be one line, 1..300 chars")
        self.store.remember(note)
        return "Noted."

    async def t_list_macros(self, a: dict[str, Any]) -> str:
        return json.dumps(self.store.macros(), indent=1)

    async def t_save_macro(self, a: dict[str, Any]) -> str:
        name = a.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValueError("name must match ^[a-z0-9_]{2,40}$")
        self.store.save_macro(name, {"description": str(a.get("description", ""))[:300],
                                     "steps": check_steps(a.get("steps"))})
        return f"Saved macro {name}."

    async def t_run_macro(self, a: dict[str, Any]) -> str:
        m = self.store.macros().get(str(a.get("name")))
        if not m:
            raise ValueError(f"no macro {a.get('name')!r}; see list_macros")
        h = self._tv(a)
        async with self._busy(h, f"running macro {a['name']}"):
            await self._steps(check_steps(m["steps"]), str(a.get("value") or ""), h)
        return f"Ran macro {a['name']}. The hub can't see the screen; check the TV."
