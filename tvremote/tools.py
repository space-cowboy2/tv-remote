"""Agent tools on top of the TV client: the contract served at /api/tools and used by
the Claude plugin and Duet. Notes and macros live next to tv.json so they survive
restarts and are shared by every client."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from .keys import is_valid_key, normalize_key
from .samsung import SamsungTV

SCHEMAS: list[dict[str, Any]] = json.loads((Path(__file__).with_name("tools.json")).read_text())
YOUTUBE, TWITCH = "111299001912", "3202203026841"
NAME_RE = re.compile(r"^[a-z0-9_]{2,40}$")
VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")
MAX_STEPS, MAX_REPEAT, MAX_WAIT = 60, 50, 15.0

# Starting points only: uncalibrated on this TV. Fix them with save_macro once someone
# watches what the screen does.
DEFAULT_MACROS: dict[str, dict[str, Any]] = {
    "youtube_search": {
        "description": "play_youtube: open YouTube, open search, type {value}, play the first result. Uncalibrated.",
        "steps": [{"launch": YOUTUBE}, {"wait": 8}, {"key": "KEY_LEFT"}, {"key": "KEY_UP", "repeat": 6},
                  {"key": "KEY_ENTER"}, {"wait": 2}, {"text": "{value}", "submit": True}, {"wait": 4},
                  {"key": "KEY_DOWN"}, {"key": "KEY_ENTER"}],
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


class Store:
    """notes.md and macros.json in one folder; writes are atomic."""

    def __init__(self, folder: Path) -> None:
        self.notes_path = folder / "notes.md"
        self.macros_path = folder / "macros.json"

    def notes(self) -> str:
        try:
            return self.notes_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def remember(self, note: str) -> None:
        self._write(self.notes_path, self.notes() + f"- {note}\n")

    def macros(self) -> dict[str, Any]:
        try:
            saved = json.loads(self.macros_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        return {**DEFAULT_MACROS, **saved}

    def save_macro(self, name: str, macro: dict[str, Any]) -> None:
        try:
            saved = json.loads(self.macros_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        saved[name] = macro
        self._write(self.macros_path, json.dumps(saved, indent=2))

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


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
    """URL or id -> the 11-char id (the TV search finds videos by id); else the words."""
    v = video.strip()
    m = VIDEO_ID_RE.search(v)
    return m.group(1) if m else v


class Tools:
    def __init__(self, tv: SamsungTV, lock: asyncio.Lock, store: Store) -> None:
        self.tv, self.lock, self.store = tv, lock, store

    async def sleep(self, seconds: float) -> None:  # a method so tests can patch it
        await asyncio.sleep(seconds)

    def list(self) -> dict[str, Any]:
        return {"tools": SCHEMAS, "notes": self.store.notes()}

    async def call(self, name: str, a: dict[str, Any]) -> str:
        fn = getattr(self, "t_" + name, None) if re.fullmatch(r"[a-z_]{2,30}", name) else None
        if fn is None:
            raise UnknownTool(name)
        return await fn(a)

    async def _keys(self, key: str, repeat: int = 1, delay: float = 0.3) -> None:
        for i in range(repeat):
            async with self.lock:
                await self.tv.send_key(key)
            if i < repeat - 1:
                await self.sleep(delay)

    async def _steps(self, steps: list[dict[str, Any]], value: str = "") -> None:
        for s in steps:
            if "key" in s:
                await self._keys(_key(s["key"]), _count(s.get("repeat"), MAX_REPEAT))
            elif "wait" in s:
                await self.sleep(float(s["wait"]))
            elif "launch" in s:
                async with self.lock:
                    await self.tv.launch_app(str(s["launch"]))
            else:
                async with self.lock:
                    await self.tv.send_text(s["text"].replace("{value}", value)[:500], bool(s.get("submit")))
            await self.sleep(0.3)

    # -- tools (name = t_<tool name>)
    async def t_get_status(self, a: dict[str, Any]) -> str:
        return json.dumps({"power": await self.tv.power_state(), "connected": self.tv.connected, "host": self.tv.host})

    async def t_set_power(self, a: dict[str, Any]) -> str:
        if a.get("state") == "on":
            return "TV is on." if await self.tv.power_on() else "TV did not wake (check Power On with Mobile)."
        if a.get("state") == "off":
            async with self.lock:
                await self.tv.power_off()
            return "Sent power off."
        raise ValueError("state must be on or off")

    async def t_list_apps(self, a: dict[str, Any]) -> str:
        async with self.lock:
            apps = await self.tv.list_apps()
        return json.dumps(sorted(apps, key=lambda x: (x.get("name") or "").lower()))

    async def t_launch_app(self, a: dict[str, Any]) -> str:
        app_id = str(a.get("app_id", ""))
        await self._steps(check_steps([{"launch": app_id}]))
        return f"Launched {app_id}."

    async def t_send_keys(self, a: dict[str, Any]) -> str:
        keys = a.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError("keys must be a non-empty list")
        delay = a.get("delay_ms", 300)
        if not isinstance(delay, int) or not 0 <= delay <= 5000:
            raise ValueError("delay_ms must be 0..5000")
        sent = []
        for k in keys:
            key, n = _key((k or {}).get("key")), _count((k or {}).get("repeat"), MAX_REPEAT)
            await self._keys(key, n, delay / 1000)
            sent.append(f"{key} x{n}")
            await self.sleep(delay / 1000)
        return "Sent " + ", ".join(sent) + "."

    async def t_type_text(self, a: dict[str, Any]) -> str:
        text = a.get("text")
        if not isinstance(text, str) or len(text) > 500:
            raise ValueError("text must be a string up to 500 chars")
        async with self.lock:
            await self.tv.send_text(text, bool(a.get("submit")))
        return f"Typed {len(text)} characters" + (" and submitted." if a.get("submit") else ".")

    async def t_wait(self, a: dict[str, Any]) -> str:
        s = a.get("seconds")
        if not isinstance(s, (int, float)) or not 0 <= s <= MAX_WAIT:
            raise ValueError(f"seconds must be 0..{MAX_WAIT}")
        await self.sleep(float(s))
        return f"Waited {s} s."

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
        await self._steps(check_steps(m["steps"]), str(a.get("value") or ""))
        return f"Ran macro {a['name']}. The hub can't see the screen; check the TV."

    async def t_play_youtube(self, a: dict[str, Any]) -> str:
        q = youtube_query(str(a.get("video", "")))
        if not q:
            raise ValueError("video is required")
        await self.t_run_macro({"name": "youtube_search", "value": q})
        return f"Searched YouTube for {q!r} and selected the first result (macro youtube_search). Check the TV."

    async def t_open_twitch(self, a: dict[str, Any]) -> str:
        ch = str(a.get("channel", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{2,25}", ch):
            raise ValueError("channel must be a Twitch login name")
        await self.t_run_macro({"name": "twitch_search", "value": ch})
        return f"Searched Twitch for {ch} (macro twitch_search, uncalibrated). Check the TV."

    async def t_skip_ad(self, a: dict[str, Any]) -> str:
        await self._keys("KEY_ENTER")
        return "Pressed OK (skips the ad if its Skip button was showing; otherwise it paused)."
