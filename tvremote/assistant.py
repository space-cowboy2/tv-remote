"""Plain-language TV commands ("open ohnepixel on Twitch", "volume down 5")
handled by Claude with tool use over the hub's own TV actions.

The hub cannot see the screen. Every tool reports what it sent, never what the
TV shows, and Claude is told to say so when a step can't be confirmed.

Configuration
  ANTHROPIC_API_KEY or ~/.config/tvremote/anthropic.key (mode 600)
  TVREMOTE_CLAUDE_MODEL   model id (default claude-opus-5-5)
  ~/.config/tvremote/macros.json   optional key sequences, see docs/ASSISTANT.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

from .apps import KNOWN_APPS
from .keys import ALL_KEYS, normalize_key
from .samsung import SamsungTV, TVError

log = logging.getLogger("tvremote.assistant")

DEFAULT_MODEL = "claude-opus-5-5"
YOUTUBE = "111299001912"
TWITCH = "3202203026841"

# Key sequences for apps that ignore deep links. "{value}" is replaced by the
# argument (e.g. the channel name). Override or add in macros.json.
DEFAULT_MACROS: dict[str, dict[str, Any]] = {
    "twitch_channel": {
        "description": "Open a Twitch channel by searching for it in the Twitch app.",
        "steps": [
            {"launch": TWITCH}, {"wait": 7},
            {"key": "KEY_LEFT"}, {"wait": 0.8},
            {"key": "KEY_UP", "repeat": 6}, {"wait": 0.5},
            {"key": "KEY_ENTER"}, {"wait": 1.5},
            {"text": "{value}", "submit": True}, {"wait": 3},
            {"key": "KEY_DOWN"}, {"wait": 0.5},
            {"key": "KEY_ENTER"},
        ],
    },
}

SYSTEM = """You control a Samsung Tizen smart TV (2024, 42" OLED) for its owner through \
the tools below. The owner types or speaks short requests from a phone remote.

How to work:
- Act right away with the fewest tool calls. Don't ask questions unless the request is truly ambiguous.
- You cannot see the TV screen. Tools report what was sent, not what is shown. If a step \
can't be confirmed (blind key sequences, deep links some apps ignore), say so briefly.
- If the TV is off and the request needs it on, power it on first.
- For YouTube videos, use web search to find the video id when the user doesn't give one, \
then play_youtube. For Twitch channels use open_twitch.
- For anything else, launch the app and, if needed, navigate with send_keys.
- Reply in one or two short plain sentences: what you did, and anything the owner should check. No markdown."""


def _tool(name: str, description: str, props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required if required is not None else list(props),
            "additionalProperties": False,
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool("get_status", "TV power state (on, standby, off) and the hub's connection state.", {}),
    _tool("set_power", "Turn the TV on (Wake-on-LAN) or off.",
          {"state": {"type": "string", "enum": ["on", "off"]}}),
    _tool("list_apps", "Installed apps with their ids.", {}),
    _tool("launch_app", "Open an installed app by id (from list_apps).", {"app_id": {"type": "string"}}),
    _tool("play_youtube", "Open the YouTube app straight to a video by its 11-character video id.",
          {"video_id": {"type": "string"}}),
    _tool("open_twitch", "Open a Twitch channel (streamer login name, e.g. 'ohnepixel'). "
          "Twitch on this TV ignores direct links, so this runs a blind search key sequence; "
          "the result can't be confirmed.", {"channel": {"type": "string"}}),
    _tool("send_keys", "Press remote keys in order. Valid keys: " + ", ".join(sorted(ALL_KEYS)) +
          ". Use repeat for e.g. several volume steps.",
          {"keys": {"type": "array", "items": {"type": "object", "properties": {
              "key": {"type": "string"}, "repeat": {"type": "integer"}},
              "required": ["key", "repeat"], "additionalProperties": False}},
           "delay_ms": {"type": "integer", "description": "Pause between presses (default 300)."}}),
    _tool("type_text", "Type into the TV's currently focused text field (a search box must be open). "
          "submit presses the keyboard's done/search.",
          {"text": {"type": "string"}, "submit": {"type": "boolean"}}),
    _tool("wait", "Pause, e.g. while an app loads (max 15 s).", {"seconds": {"type": "number"}}),
]
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}


def load_api_key(config_dir: Path) -> str | None:
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env.strip()
    p = config_dir / "anthropic.key"
    if p.exists():
        key = p.read_text().strip()
        return key or None
    return None


def load_macros(config_dir: Path) -> dict[str, dict[str, Any]]:
    macros = dict(DEFAULT_MACROS)
    p = config_dir / "macros.json"
    if p.exists():
        try:
            macros.update(json.loads(p.read_text()))
        except (ValueError, OSError) as e:
            log.warning("ignoring %s: %s", p, e)
    return macros


class TVActions:
    """The tool implementations. Each returns a short string for Claude."""

    def __init__(self, tv: SamsungTV, lock: asyncio.Lock, macros: dict[str, dict[str, Any]],
                 list_apps: Callable[[], Awaitable[list[dict[str, Any]]]]) -> None:
        self.tv, self.lock, self.macros, self._list_apps = tv, lock, macros, list_apps
        self.log: list[str] = []

    async def run(self, name: str, args: dict[str, Any]) -> str:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            raise ValueError(f"unknown tool {name}")
        result = await fn(**args)
        self.log.append(f"{name}: {result}")
        return result

    async def t_get_status(self) -> str:
        return json.dumps({"power": await self.tv.power_state(), "connected": self.tv.connected})

    async def t_set_power(self, state: str) -> str:
        if state == "on":
            if not await self.tv.power_on():
                return "TV did not wake (Power On with Mobile may be off)"
            return "TV is on"
        async with self.lock:
            await self.tv.power_off()
        return "TV turned off"

    async def t_list_apps(self) -> str:
        apps = await self._list_apps()
        return json.dumps([{"id": a["id"], "name": a["name"]} for a in apps])

    async def t_launch_app(self, app_id: str) -> str:
        async with self.lock:
            await self.tv.launch_app(app_id)
        return f"launched {KNOWN_APPS.get(app_id, app_id)}"

    async def t_play_youtube(self, video_id: str) -> str:
        if not (len(video_id) == 11 and all(c.isalnum() or c in "-_" for c in video_id)):
            return "invalid video id"
        async with self.lock:
            await self.tv.dial_launch("YouTube", f"v={video_id}")
        return f"YouTube accepted video {video_id}"

    async def t_open_twitch(self, channel: str) -> str:
        channel = channel.strip().lstrip("@").lower()
        if not channel or not all(c.isalnum() or c == "_" for c in channel) or len(channel) > 25:
            return "invalid channel name"
        return await self.run_macro("twitch_channel", channel)

    async def run_macro(self, name: str, value: str = "") -> str:
        macro = self.macros.get(name)
        if not macro:
            return f"no macro {name}"
        async with self.lock:
            for step in macro["steps"]:
                if "wait" in step:
                    await asyncio.sleep(min(float(step["wait"]), 15))
                elif "launch" in step:
                    await self.tv.launch_app(step["launch"])
                elif "key" in step:
                    for _ in range(max(1, min(int(step.get("repeat", 1)), 50))):
                        await self.tv.send_key(step["key"])
                        await asyncio.sleep(0.25)
                elif "text" in step:
                    await self.tv.send_text(step["text"].replace("{value}", value), submit=bool(step.get("submit")))
        return f"ran the {name} key sequence for '{value}' (not visually confirmed)"

    async def t_send_keys(self, keys: list[dict[str, Any]], delay_ms: int = 300) -> str:
        names = [normalize_key(k["key"]) for k in keys]
        bad = [k for k in names if k not in ALL_KEYS]
        if bad:
            return f"unknown keys: {', '.join(bad)}"
        total = 0
        delay = max(0.05, min(delay_ms, 3000) / 1000)
        async with self.lock:
            for k, n in zip(names, keys):
                for _ in range(max(1, min(int(n.get("repeat", 1)), 50))):
                    if total >= 100:
                        return f"stopped after {total} presses"
                    await self.tv.send_key(k)
                    total += 1
                    await asyncio.sleep(delay)
        return f"pressed {total} keys"

    async def t_type_text(self, text: str, submit: bool = False) -> str:
        async with self.lock:
            await self.tv.send_text(text[:500], submit=submit)
        return "typed"

    async def t_wait(self, seconds: float) -> str:
        await asyncio.sleep(max(0, min(seconds, 15)))
        return "waited"


class Assistant:
    """Runs one request through Claude. Keeps a short text-only history so
    follow-ups like "louder" or "no, the other one" work."""

    MAX_TURNS = 12

    def __init__(self, actions_factory: Callable[[], TVActions], api_key: str,
                 model: str | None = None, client: Any = None) -> None:
        if client is None:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=api_key, timeout=60, max_retries=2)
        self.client = client
        self.model = model or os.environ.get("TVREMOTE_CLAUDE_MODEL") or DEFAULT_MODEL
        self.actions_factory = actions_factory
        self.history: deque[tuple[float, str, str]] = deque(maxlen=6)
        self.lock = asyncio.Lock()

    def _recent(self) -> list[dict[str, Any]]:
        cutoff = time.time() - 600
        msgs: list[dict[str, Any]] = []
        for at, req, reply in self.history:
            if at >= cutoff:
                msgs += [{"role": "user", "content": req}, {"role": "assistant", "content": reply}]
        return msgs

    async def handle(self, text: str) -> dict[str, Any]:
        async with self.lock:
            actions = self.actions_factory()
            messages = self._recent() + [{"role": "user", "content": text}]
            reply = ""
            for _ in range(self.MAX_TURNS):
                resp = await self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=SYSTEM,
                    tools=[*TOOLS, WEB_SEARCH],
                    messages=messages,
                    output_config={"effort": "low"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
                if resp.stop_reason == "refusal":
                    reply = "Claude declined that request."
                    break
                messages.append({"role": "assistant", "content": resp.content})
                reply = " ".join(b.text for b in resp.content if b.type == "text").strip() or reply
                if resp.stop_reason == "pause_turn":
                    continue
                calls = [b for b in resp.content if b.type == "tool_use"]
                if resp.stop_reason != "tool_use" or not calls:
                    break
                results = []
                for call in calls:
                    try:
                        out, err = await actions.run(call.name, dict(call.input)), False
                    except TVError as e:
                        out, err = f"TV error: {e}", True
                    except (TypeError, ValueError) as e:
                        out, err = f"bad input: {e}", True
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": out, "is_error": err})
                messages.append({"role": "user", "content": results})
            else:
                reply = reply or "Stopped after too many steps."
            reply = reply or "Done."
            self.history.append((time.time(), text, reply))
            return {"reply": reply, "actions": actions.log}
