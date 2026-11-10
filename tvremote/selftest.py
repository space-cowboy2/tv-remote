"""Check a real TV: which signals it answers, and optionally a full run.

  tvremote selftest            read-only: nothing on the TV changes
  tvremote selftest --full     also changes things, then puts them back where it
                               can: volume down one and up one, plays a search on
                               YouTube, pauses and resumes it

Everything printed comes from the TV in front of you, so this is the live
counterpart of the simulated tests. It still cannot see the screen: a person
has to confirm what actually came up.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from . import youtube
from .apps import YOUTUBE_ID
from .samsung import TVError
from .tools import Tools, TVHandle


async def probe(h: TVHandle) -> list[dict[str, Any]]:
    """Read-only checks of one TV. Each: {check, ok, detail}."""
    tv = h.tv
    out: list[dict[str, Any]] = []

    def add(check: str, ok: bool | None, detail: str) -> None:
        out.append({"check": check, "ok": ok, "detail": detail})

    try:
        device = (await tv.info(timeout=3.0)).get("device") or {}
        add("status port (8001)", True, f"model {device.get('modelName') or device.get('model')}, "
                                        f"PowerState {device.get('PowerState', 'not reported')}")
    except TVError as e:
        add("status port (8001)", False, str(e))
    power = await tv.power_state()
    add("power", power == "on", f"{power} (answered by: {tv.power_source})")
    if power != "on":
        add("everything else", None, "skipped: the TV is not on")
        return out
    if tv.token:
        try:
            await tv.connect()
            add("remote-control channel", True, "connected with the saved pairing")
        except TVError as e:
            add("remote-control channel", False, f"{type(e).__name__}: {e}")
    else:
        add("remote-control channel", None, "not paired yet (no saved token)")
    try:
        status = await tv.app_status(YOUTUBE_ID, timeout=3.0)
        add("YouTube state over the app endpoint", status is not None,
            json.dumps({k: status.get(k) for k in ("running", "visible")}) if status else
            ("the endpoint accepts the request and never answers" if tv.rest_apps_ok is False else "no answer for this app"))
    except TVError as e:
        add("YouTube state over the app endpoint", False, str(e))
    dial = await tv.dial_state("YouTube")
    add("YouTube state over DIAL", dial is not None, dial["state"] if dial else "no DIAL answer")
    volume, muted = await tv.volume(), await tv.muted()
    add("volume and mute read-back", volume is not None, f"volume {volume}, muted {muted}")
    if tv.connected:
        try:
            apps = await tv.list_apps()
            add("installed apps", bool(apps), f"{len(apps)} found: " + ", ".join(sorted(a['name'] for a in apps)[:12]))
        except TVError as e:
            add("installed apps", False, str(e))
    add("keyboard announcements", tv.ime_seen or None,
        "seen" if tv.ime_seen else "none yet (they only come when a search box opens; --full shows it)")
    try:
        found = await asyncio.to_thread(youtube.search, "jazz music", "long")
        add("YouTube search from this computer", bool(found),
            f"{len(found)} results, first: {found[0]['title'][:60]!r}" if found else "no results")
    except youtube.YouTubeError as e:
        add("YouTube search from this computer", False, str(e))
    return out


async def full_run(tools: Tools, h: TVHandle, query: str, prefer: str) -> list[dict[str, Any]]:
    """The acceptance run on the real TV. Each: {step, report}."""
    out: list[dict[str, Any]] = []

    async def step(name: str, tool: str, **args: Any) -> dict[str, Any]:
        raw = await tools.call(tool, {**args, "tv": h.slug})
        try:
            report = json.loads(raw)
        except ValueError:
            report = {"text": str(raw)}
        out.append({"step": name, "tool": tool, "args": args, "report": report})
        return report if isinstance(report, dict) else {}

    await step("what is known before", "get_state")
    down = await step("volume down one", "set_volume", change=-1)
    if down.get("ok"):
        await step("volume back up one", "set_volume", change=1)
    played = await step(f"play {query!r}", "play", query=query, prefer=prefer)
    if played.get("ok"):
        await tools.sleep(15)
        await step("pause", "playback", action="pause")
        await tools.sleep(3)
        await step("resume", "playback", action="resume")
    await step("what is known after", "get_state")
    return out


def render(title: str, checks: list[dict[str, Any]]) -> str:
    marks = {True: "ok  ", False: "FAIL", None: "--  "}
    return "\n".join([title] + [f"  {marks[c['ok']]} {c['check']}: {c['detail']}" for c in checks])
