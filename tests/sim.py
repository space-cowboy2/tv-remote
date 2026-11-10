"""Shared pieces for the scenario tests: a simulated TV, a stand-in for
YouTube's website, and a clock that jumps instead of waiting.

Everything here is simulation. It checks the hub's logic (order of steps,
evidence, retries, reports); it cannot show that a real TV reacts the same way.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tvremote.fake import FakeTV
from tvremote.samsung import SamsungTV
from tvremote.tools import Store, Tools, TVHandle
from tvremote.youtube import YouTubeError

VIDEOS = [
    {"id": "jazzSHORT01", "title": "Jazz Music in 60 Seconds", "channel": "Shorty", "length": "1:00", "seconds": 60, "live": False},
    {"id": "Dx5qFachd3A", "title": "Jazz Music - Relaxing Cafe Mix", "channel": "Cafe BGM", "length": "3:02:11", "seconds": 10931, "live": False},
    {"id": "jazzOTHER02", "title": "Smooth Jazz Music Night Mix", "channel": "Night Jazz", "length": "2:10:00", "seconds": 7800, "live": False},
    {"id": "JAZZlive001", "title": "Jazz Radio 24/7", "channel": "Jazz FM", "length": None, "seconds": None, "live": True},
    {"id": "jfKfPfyJRdk", "title": "lofi hip hop radio - beats to relax/study to", "channel": "Lofi Girl", "length": None, "seconds": None, "live": True},
    {"id": "dQw4w9WgXcQ", "title": "Rick Astley - Never Gonna Give You Up", "channel": "Rick Astley", "length": "3:33", "seconds": 213, "live": False},
    {"id": "relaxPIANO1", "title": "Relaxing Piano Music for Sleep", "channel": "Calm", "length": "2:00:00", "seconds": 7200, "live": False},
    {"id": "comedySPCL1", "title": "Stand Up Comedy Full Special", "channel": "Laughs", "length": "1:02:00", "seconds": 3720, "live": False},
    {"id": "showEP00004", "title": "Great Show Episode 4", "channel": "Great Show", "length": "42:00", "seconds": 2520, "live": False},
    {"id": "showEP00005", "title": "Great Show Episode 5", "channel": "Great Show", "length": "41:00", "seconds": 2460, "live": False},
]


class Internet:
    """YouTube's website as the hub sees it: search and lookup."""

    def __init__(self) -> None:
        self.videos = [dict(v) for v in VIDEOS]
        self.down = False
        self.unpinned: set[str] = set()  # ids that searching by id does not bring up first
        self.searches: list[tuple[str, str]] = []

    def search(self, query: str, prefer: str = "auto", limit: int = 10, timeout: float = 10.0) -> list[dict[str, Any]]:
        self.searches.append((query, prefer))
        if self.down:
            raise YouTubeError("offline")
        by_id = [v for v in self.videos if v["id"] == query and query not in self.unpinned]
        if by_id:
            return [dict(by_id[0])]
        words = query.lower().split()
        hits = [v for v in self.videos if words and all(w in (v["title"] + " " + v["channel"]).lower() for w in words)]
        if prefer == "live":
            hits = [v for v in hits if v["live"]] or hits
        elif prefer == "long":
            hits = [v for v in hits if v["live"] or (v["seconds"] or 0) >= 1200] or hits
        return [dict(v) for v in hits[:limit]]

    def lookup(self, video_id: str, timeout: float = 8.0) -> dict[str, Any] | None:
        if self.down:
            return None
        return next((dict(v) for v in self.videos if v["id"] == video_id), None)


class Rig:
    """One hub (Tools) wired to one or more simulated TVs."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.fakes: list[FakeTV] = []
        self.net = Internet()
        self.tools: Tools = None  # type: ignore[assignment]

    async def add_tv(self, slug: str = "living-room", label: str = "Living Room", **fake_args: Any) -> FakeTV:
        fake = await FakeTV(**fake_args).start()
        fake.youtube.catalog = {v["id"]: v["title"] for v in self.net.videos}
        self.fakes.append(fake)
        handle = TVHandle(slug, label, self.client(fake), model="QN55Q60B")
        if self.tools is None:
            self.tools = self.make_tools([handle])
        else:
            handle.tv.sleep = self.sleep
            self.tools.tvs[slug] = handle
        return fake

    def client(self, fake: FakeTV, token: bool = True) -> SamsungTV:
        return SamsungTV("127.0.0.1", port=fake.ws_port, info_port=fake.info_port, dial_port=fake.info_port,
                         upnp_port=fake.info_port, secure=False, mac=fake.mac, token=fake.token if token else None,
                         request_timeout=2.0)

    def make_tools(self, handles: list[TVHandle], **kw: Any) -> Tools:
        tools = Tools(handles, store=Store(self.folder), **kw)
        tools.sleep = self.sleep  # type: ignore[method-assign]
        for h in tools.tvs.values():
            h.tv.sleep = self.sleep
        tools.yt_search, tools.yt_lookup = self.net.search, self.net.lookup
        tools.lan_mac = lambda host: None  # the simulated TV is this computer: no neighbour entry
        return tools

    def restart(self) -> Tools:
        """A new hub process over the same saved files and the same TVs."""
        handles = [TVHandle(h.slug, h.label, self.client(f), model=h.model)
                   for h, f in zip(self.tools.tvs.values(), self.fakes)]
        self.tools = self.make_tools(handles)
        return self.tools

    async def sleep(self, seconds: float) -> None:
        """Virtual time: let messages in flight land, then jump every TV's clock."""
        await asyncio.sleep(0.02)
        for f in self.fakes:
            f.clock += seconds
        await asyncio.sleep(0.005)

    async def call(self, _tool: str, **args: Any) -> dict[str, Any]:
        out = await self.tools.call(_tool, args)
        await asyncio.sleep(0.03)  # the last key is still on its way to the simulated TV
        try:
            data = json.loads(out)
        except ValueError:
            data = {"text": str(out)}
        if not isinstance(data, dict):
            data = {"data": data}
        data.setdefault("ok", out.ok)
        return data

    @property
    def tv(self) -> FakeTV:
        return self.fakes[0]

    async def close(self) -> None:
        if self.tools:
            await self.tools.stop()
            for h in self.tools.tvs.values():
                await h.tv.close()
        for f in self.fakes:
            await f.stop()


def said(report: dict[str, Any], part: str, where: str = "verified") -> bool:
    return any(part in line for line in report.get(where, []))
