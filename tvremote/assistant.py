"""Plain-language TV commands ("open ohnepixel on Twitch", "volume down 5")
handled by Claude with tool use over the hub's own TV actions.

The hub cannot see the screen. Every tool reports what it sent, never what the
TV shows, and Claude is told to say so when a step can't be confirmed.

Configuration (all next to tv.json, normally ~/.config/tvremote/)
  ANTHROPIC_API_KEY or anthropic.key (mode 600)
  TVREMOTE_CLAUDE_MODEL   model id (default claude-opus-5-5)
  macros.json   key sequences Claude can run and save, see docs/ASSISTANT.md
  notes.md      what Claude has learned about this TV; loaded into every request
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
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

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Key sequences for things the TV's APIs can't do directly. "{value}" is
# replaced by the argument. Claude can add or fix these with save_macro; they
# persist in macros.json. Timings were verified on Danny's TV 2026-11-06.
DEFAULT_MACROS: dict[str, dict[str, Any]] = {
    "youtube_search": {
        "description": "Play a YouTube video natively: open YouTube, search for the title, pick the first result. "
                       "The TV ignores YouTube deep links, so this is THE way to play a specific video.",
        "steps": [
            {"key": "KEY_HOME"}, {"wait": 3},
            {"launch": YOUTUBE}, {"wait": 8},
            {"key": "KEY_LEFT"}, {"wait": 1.5},
            {"key": "KEY_UP"}, {"wait": 1},
            {"key": "KEY_ENTER"}, {"wait": 2.5},
            {"text": "{value}", "submit": True}, {"wait": 4},
            {"key": "KEY_RIGHT"}, {"wait": 1},
            {"key": "KEY_ENTER"},
        ],
    },
    "twitch_channel": {
        "description": "Open a Twitch channel by searching for it in the Twitch app (not yet verified on this TV).",
        "steps": [
            {"key": "KEY_HOME"}, {"wait": 3},
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

DEFAULT_NOTES = """\
- Samsung 42" 2024 OLED (Tizen). The hub cannot see the screen.
- YouTube: deep links (ed.apps.launch, DIAL payloads) start the app but drop the video. Use the
  youtube_search macro (play_youtube does this). Verified 2026-11-06.
- The owner does NOT want casting (YouTube Lounge / Smart View): it triggers Samsung Multi View
  (split screen, no sound). Never cast. KEY_MULTI_VIEW toggles Multi View if it ever appears.
- Twitch: deep links are ignored; twitch_channel macro is a best guess, not yet calibrated.
"""

SYSTEM = """You control a Samsung Tizen smart TV (2024, 42" OLED) for its owner through \
the tools below. The owner types or speaks short requests from a phone remote.

How to work:
- Act right away with the fewest tool calls. Don't ask questions unless the request is truly ambiguous.
- You cannot see the TV screen. Tools report what was sent, not what is shown. If a step \
can't be confirmed (blind key sequences, deep links some apps ignore), say so briefly.
- If the TV is off and the request needs it on, power it on first.
- YouTube: if the owner names no specific video ("play some jazz", "put on something chill"), \
call search_youtube once with a good query (for music: "<genre> music mix" or "<genre> radio live"), \
pick one result that is live or over an hour long from a popular channel, and play_youtube its id. \
Don't ask which one. Say what you picked (title, channel). For Twitch channels use open_twitch.
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
    _tool("play_youtube", "Play a YouTube video in the TV's YouTube app by searching for it there "
          "(the TV ignores direct links). Pass a youtube.com/youtu.be URL, an 11-character video id, "
          "or the title / search words. Takes about 25 s.",
          {"video": {"type": "string"}}),
    _tool("search_youtube", "Search YouTube (on the internet, not on the TV) and return the top videos: "
          "id, title, channel, length, live, views. Use it to turn a vague ask ('play some jazz', "
          "'something chill') into one concrete video, then pass that video's id to play_youtube. "
          "For music or background asks, search for a long mix or live radio (e.g. 'jazz music mix', "
          "'jazz radio live') and pick a result that is live or over an hour, from a popular channel.",
          {"query": {"type": "string"}}),
    _tool("run_macro", "Run a saved key sequence by name (see the notes and list_macros). value replaces {value}.",
          {"name": {"type": "string"}, "value": {"type": "string"}}),
    _tool("list_macros", "Saved key sequences with their steps.", {}),
    _tool("save_macro", "Create or replace a key sequence so it is remembered. Steps: {\"key\": \"KEY_X\", "
          "\"repeat\": n} | {\"wait\": seconds} | {\"launch\": app_id} | {\"text\": \"...{value}...\", "
          "\"submit\": bool}.",
          {"name": {"type": "string", "pattern": "^[a-z0-9_]{2,40}$"},
           "description": {"type": "string"},
           "steps": {"type": "array", "items": {"type": "object", "properties": {
               "key": {"type": "string"}, "repeat": {"type": "integer"}, "wait": {"type": "number"},
               "launch": {"type": "string"}, "text": {"type": "string"}, "submit": {"type": "boolean"}},
               "required": [], "additionalProperties": False}}}),
    _tool("remember", "Append a lasting fact about this TV or the owner's preferences to the notes "
          "(one short line).", {"note": {"type": "string"}}),
    _tool("skip_ad", "Press OK once, which skips a YouTube ad when its Skip button is showing. Warning: if "
          "no ad is on screen this pauses the video. Only use when the owner asks.", {}),
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


class Memory:
    """Macros (macros.json) and notes (notes.md) in the config dir. Built-in
    defaults are merged under what the files hold, so files only need to
    contain what Claude changed."""

    def __init__(self, config_dir: Path) -> None:
        self.dir = config_dir
        self.macros: dict[str, dict[str, Any]] = {}
        self.notes = ""
        self.reload()

    def reload(self) -> None:
        self.macros = {k: dict(v) for k, v in DEFAULT_MACROS.items()}
        p = self.dir / "macros.json"
        if p.exists():
            try:
                self.macros.update(json.loads(p.read_text()))
            except (ValueError, OSError) as e:
                log.warning("ignoring %s: %s", p, e)
        n = self.dir / "notes.md"
        try:
            self.notes = n.read_text() if n.exists() else DEFAULT_NOTES
        except OSError:
            self.notes = DEFAULT_NOTES

    def save_macro(self, name: str, description: str, steps: list[dict[str, Any]]) -> None:
        self.macros[name] = {"description": description, "steps": steps}
        p = self.dir / "macros.json"
        existing: dict[str, Any] = {}
        if p.exists():
            try:
                existing = json.loads(p.read_text())
            except (ValueError, OSError):
                pass
        existing[name] = self.macros[name]
        self._write(p, json.dumps(existing, indent=2) + "\n")

    def remember(self, note: str) -> None:
        note = " ".join(note.split())[:300]
        if not note:
            return
        self.notes = (self.notes.rstrip("\n") + f"\n- {note}\n") if self.notes.strip() else f"- {note}\n"
        self._write(self.dir / "notes.md", self.notes)

    def _write(self, p: Path, content: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content)
        os.replace(tmp, p)


def youtube_title(video_id: str) -> str | None:
    """Video title via YouTube's public oEmbed endpoint (no API key)."""
    url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
        {"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.loads(r.read().decode()).get("title")
    except (OSError, ValueError):
        return None


YT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
YT_DATA_RE = re.compile(r"(?:var ytInitialData|window\[\"ytInitialData\"\])\s*=\s*(\{.*?\});\s*(?:</script>|var |window\[)",
                        re.DOTALL)


def _yt_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return str(node["simpleText"])
    return "".join(str(r.get("text", "")) for r in node.get("runs", []) if isinstance(r, dict))


def _yt_videos(node: Any, out: list[dict[str, Any]]) -> None:
    """Collect every videoRenderer in ytInitialData, in page order."""
    if isinstance(node, dict):
        vr = node.get("videoRenderer")
        if isinstance(vr, dict) and vr.get("videoId"):
            badges = json.dumps(vr.get("badges", [])) + json.dumps(vr.get("thumbnailOverlays", []))
            length = _yt_text(vr.get("lengthText"))
            out.append({
                "id": vr["videoId"],
                "title": _yt_text(vr.get("title")),
                "channel": _yt_text(vr.get("ownerText") or vr.get("longBylineText")),
                "length": length or None,
                "live": not length and ("LIVE" in badges or "BADGE_STYLE_TYPE_LIVE_NOW" in badges),
                "views": _yt_text(vr.get("viewCountText") or vr.get("shortViewCountText")) or None,
            })
            return
        for v in node.values():
            _yt_videos(v, out)
    elif isinstance(node, list):
        for v in node:
            _yt_videos(v, out)


def parse_youtube_results(html: str, limit: int = 8) -> list[dict[str, Any]]:
    m = YT_DATA_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    _yt_videos(data, out)
    seen: set[str] = set()
    uniq = [v for v in out if not (v["id"] in seen or seen.add(v["id"]))]
    return uniq[:limit]


def youtube_search(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Top YouTube video results for a query, scraped from the public results
    page (no API key). Shorts, channels and playlists are left out."""
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode(
        {"search_query": query, "hl": "en", "gl": "US"})
    req = urllib.request.Request(url, headers={
        "User-Agent": YT_UA, "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "CONSENT=YES+1; SOCS=CAI"})  # skip the EU consent page
    with urllib.request.urlopen(req, timeout=10) as r:
        html = r.read().decode("utf-8", "replace")
    return parse_youtube_results(html, limit)


def extract_video_id(text: str) -> str | None:
    text = text.strip()
    if VIDEO_ID_RE.match(text):
        return text
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", text)
    return m.group(1) if m else None


def search_words(title: str, limit: int = 6) -> str:
    """Trim a video title to something the TV search box handles well."""
    words = [w for w in re.sub(r"[^\w\s'&-]", " ", title).split() if w]
    return " ".join(words[:limit]) or title[:60]


class TVActions:
    """The tool implementations. Each returns a short string for Claude."""

    def __init__(self, tv: SamsungTV, lock: asyncio.Lock, memory: Memory,
                 list_apps: Callable[[], Awaitable[list[dict[str, Any]]]],
                 title_lookup: Callable[[str], str | None] | None = None) -> None:
        self.tv, self.lock, self.memory, self._list_apps = tv, lock, memory, list_apps
        self.title_lookup = title_lookup or (lambda vid: youtube_title(vid))
        self.log: list[str] = []

    @property
    def macros(self) -> dict[str, dict[str, Any]]:
        return self.memory.macros

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

    async def t_play_youtube(self, video: str) -> str:
        video_id = extract_video_id(video)
        query = video.strip()
        if video_id:
            title = await asyncio.to_thread(self.title_lookup, video_id)
            if not title:
                return "could not look up that video's title, so I can't search for it on the TV"
            query = search_words(title)
        else:
            query = search_words(query, limit=8)
        await self.run_macro("youtube_search", query)
        return f"searched YouTube on the TV for '{query}' and selected the first result (not visually confirmed)"

    async def t_search_youtube(self, query: str) -> str:
        query = " ".join(query.split())[:120]
        if not query:
            return "empty query"
        try:
            results = await asyncio.to_thread(youtube_search, query)
        except OSError as e:
            return f"YouTube search failed ({e}); try play_youtube with search words instead"
        if not results:
            return "no videos found; try other words"
        return json.dumps(results)

    async def t_run_macro(self, name: str, value: str = "") -> str:
        return await self.run_macro(name, value)

    async def t_list_macros(self) -> str:
        return json.dumps(self.macros)

    async def t_save_macro(self, name: str, description: str, steps: list[dict[str, Any]]) -> str:
        if not re.match(r"^[a-z0-9_]{2,40}$", name):
            return "bad macro name"
        clean: list[dict[str, Any]] = []
        for st in steps:
            st = {k: v for k, v in st.items() if v is not None}
            if "key" in st:
                k = normalize_key(str(st["key"]))
                if k not in ALL_KEYS and k != "KEY_MULTI_VIEW":
                    return f"unknown key {k}"
                clean.append({"key": k, **({"repeat": int(st["repeat"])} if st.get("repeat") else {})})
            elif "wait" in st:
                clean.append({"wait": max(0, min(float(st["wait"]), 15))})
            elif "launch" in st:
                clean.append({"launch": str(st["launch"])})
            elif "text" in st:
                clean.append({"text": str(st["text"])[:200], "submit": bool(st.get("submit", True))})
            else:
                return f"step {st} has none of key/wait/launch/text"
        if not clean:
            return "no steps"
        self.memory.save_macro(name, description[:200], clean)
        return f"saved macro {name} ({len(clean)} steps)"

    async def t_remember(self, note: str) -> str:
        self.memory.remember(note)
        return "noted"

    async def t_skip_ad(self) -> str:
        async with self.lock:
            await self.tv.send_key("KEY_ENTER")
        return "pressed OK (skips the ad if its button was showing; otherwise toggled pause)"

    async def t_open_twitch(self, channel: str) -> str:
        channel = channel.strip().lstrip("@").lower()
        if not channel or not all(c.isalnum() or c == "_" for c in channel) or len(channel) > 25:
            return "invalid channel name"
        return await self.run_macro("twitch_channel", channel)

    async def run_macro(self, name: str, value: str = "") -> str:
        macro = self.macros.get(name)
        if not macro:
            return f"no macro {name}; known: {', '.join(self.macros)}"
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
            system = SYSTEM + (actions.memory.notes or "(none yet)")
            messages = self._recent() + [{"role": "user", "content": text}]
            reply = ""
            for _ in range(self.MAX_TURNS):
                resp = await self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system,
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
