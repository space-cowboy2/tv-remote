"""A simulated Samsung TV for tests and development without real hardware.

It speaks the same protocols the hub uses: the remote-control WebSocket, the
REST info and app endpoints, the DIAL app state, and the UPnP volume service.
It also carries a small model of the YouTube app's screens (home, side menu,
search, results, player) so a whole "play this" key sequence can be followed
to the video that would end up playing. That model is our reading of the real
app, good for testing the hub's logic and its recovery paths; only the real
TV can confirm the key sequence itself.

Run standalone:  python -m tvremote.fake   (ws on 8002, info on 8001, no TLS)
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import Server, ServerConnection, serve

from .apps import DIAL_NAMES, YOUTUBE_ID

GUIDE = ["search", "home", "shorts", "subscriptions", "library"]


@dataclass
class FakeYouTube:
    """The YouTube TV app as a state machine driven by remote keys."""

    load_time: float = 5.0         # seconds after a cold start before keys register
    account_picker: bool = False   # cold start opens "Who's watching?" first
    catalog: dict[str, str] = field(default_factory=lambda: {
        "jfKfPfyJRdk": "lofi hip hop radio - beats to relax/study to",
        "Dx5qFachd3A": "Jazz Music - Relaxing Cafe Mix",
        "dQw4w9WgXcQ": "Rick Astley - Never Gonna Give You Up",
    })
    home_feed: str = "HOMEfeed001"  # what plays if OK is pressed on the home screen
    state: str = "closed"   # closed | loading | picker | browse | guide | search | results | results_focus | player
    guide_index: int = 1
    text: str = ""
    results: list[str] = field(default_factory=list)
    playing: str | None = None
    paused: bool = False
    position: float = 0.0
    ready_at: float = 0.0
    dropped: int = 0        # keys or text that landed nowhere
    trail: list[str] = field(default_factory=list)

    def _go(self, state: str) -> None:
        self.state = state
        self.trail.append(state)

    def launch(self, now: float) -> None:
        if self.state == "closed":  # a running app comes back exactly as it was left
            self.playing, self.paused, self.position, self.text, self.results = None, False, 0.0, "", []
            self.ready_at = now + self.load_time
            self._go("loading")

    def close(self) -> None:
        self.playing = None
        self._go("closed")

    def _settle(self, now: float) -> None:
        if self.state == "loading" and now >= self.ready_at:
            self._go("picker" if self.account_picker else "browse")

    def key(self, key: str, now: float) -> str | None:
        """Apply one key. Returns "ime_open" when a text field takes focus."""
        self._settle(now)
        s = self.state
        if s in ("closed", "loading"):
            self.dropped += 1
        elif s == "picker":
            if key == "KEY_ENTER":
                self._go("browse")
        elif s == "browse":
            if key == "KEY_LEFT":
                self.guide_index = 1
                self._go("guide")
            elif key == "KEY_ENTER":
                self._play(self.home_feed)
        elif s == "guide":
            if key == "KEY_UP":
                self.guide_index = max(0, self.guide_index - 1)
            elif key == "KEY_DOWN":
                self.guide_index = min(len(GUIDE) - 1, self.guide_index + 1)
            elif key in ("KEY_RIGHT", "KEY_RETURN"):
                self._go("browse")
            elif key == "KEY_ENTER":
                if GUIDE[self.guide_index] == "search":
                    self.text = ""
                    self._go("search")
                    return "ime_open"
                self._go("browse")
        elif s == "search":
            if key == "KEY_RETURN":
                self._go("guide")
                return "ime_close"
        elif s == "results":
            if key == "KEY_RIGHT" and self.results:
                self._go("results_focus")
            elif key == "KEY_RETURN":
                self._go("browse")
        elif s == "results_focus":
            if key == "KEY_ENTER":
                self._play(self.results[0])
            elif key == "KEY_LEFT":
                self._go("results")
            elif key == "KEY_RETURN":
                self._go("browse")
        elif s == "player":
            if key == "KEY_ENTER":
                self.paused = not self.paused
            elif key == "KEY_PAUSE":
                self.paused = True
            elif key == "KEY_PLAY":
                self.paused = False
            elif key in ("KEY_LEFT", "KEY_REWIND"):
                self.position = max(0.0, self.position - 10)
            elif key in ("KEY_RIGHT", "KEY_FF"):
                self.position += 10
            elif key in ("KEY_RETURN", "KEY_STOP"):
                self.playing = None
                self._go("browse")
        return None

    def _play(self, video: str) -> None:
        self.playing, self.paused, self.position = video, False, 0.0
        self._go("player")

    def type(self, text: str, now: float) -> bool:
        self._settle(now)
        if self.state != "search":
            self.dropped += 1
            return False
        self.text = text
        return True

    def submit(self, now: float) -> bool:
        self._settle(now)
        if self.state != "search":
            return False
        q = self.text.strip()
        words = q.lower().split()
        self.results = [q] if q in self.catalog else [
            vid for vid, title in self.catalog.items() if words and all(w in title.lower() for w in words)]
        self._go("results")
        return True


@dataclass
class FakeTV:
    token: str = "fake-token-123"
    pairing: str = "allow"  # allow | deny | timeout | silent
    power: str = "on"
    mac: str = "AA:BB:CC:DD:EE:FF"
    name: str = "[TV] 42&amp;quot; OLED"
    apps: list[dict[str, Any]] = field(default_factory=lambda: [
        {"appId": "3201907018807", "name": "Netflix", "app_type": 2},
        {"appId": "111299001912", "name": "YouTube", "app_type": 2},
    ])
    received: list[dict[str, Any]] = field(default_factory=list)
    answers_installed_apps: bool = True  # 2024 firmware: False
    launched: list[str] = field(default_factory=list)
    clients: list[str] = field(default_factory=list)
    ws_port: int = 0
    info_port: int = 0

    # behavior switches for failure tests
    rest_apps: str = "ok"      # "hang": app endpoints accept the request and never answer (2022 Q60B)
    rest_info: str = "ok"      # "hang": port 8001 stalls for device info too
    hang_seconds: float = 4.0
    dial: bool = True
    upnp: bool = True
    ime_events: bool = True    # False: the TV never announces its on-screen keyboard
    launch_works: bool = True  # False: the TV accepts launches and nothing opens
    acks_ws_launch: bool = True
    volume: int = 12
    muted: bool = False

    running: set[str] = field(default_factory=set)
    foreground: str | None = None
    closed: list[str] = field(default_factory=list)
    clock: float = 0.0         # virtual seconds added by tests instead of sleeping
    youtube: FakeYouTube = field(default_factory=FakeYouTube)

    _server: Server | None = None
    _http: ThreadingHTTPServer | None = None
    _connections: set[ServerConnection] = field(default_factory=set)
    _t0: float = field(default_factory=time.monotonic)

    def now(self) -> float:
        return time.monotonic() - self._t0 + self.clock

    async def start(self, ws_port: int = 0, info_port: int = 0) -> "FakeTV":
        self._server = await serve(self._handle, "127.0.0.1", ws_port)
        self.ws_port = self._server.sockets[0].getsockname()[1]
        self._start_http(info_port)
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._http:
            self._http.shutdown()
            self._http.server_close()

    async def drop_connections(self) -> None:
        for c in list(self._connections):
            await c.close()

    @property
    def keys(self) -> list[str]:
        return [
            m["params"]["DataOfCmd"] for m in self.received
            if m.get("params", {}).get("TypeOfRemote") == "SendRemoteKey" and m["params"]["Cmd"] == "Click"
        ]

    # ------------------------------------------------------------------ apps
    def _installed(self, app_id: str) -> dict[str, Any] | None:
        return next((a for a in self.apps if a["appId"] == app_id), None)

    def launch(self, app_id: str) -> bool:
        if not self._installed(app_id):
            return False
        self.launched.append(app_id)
        if not self.launch_works:
            return True
        self.running.add(app_id)
        self.foreground = app_id
        if app_id == YOUTUBE_ID:
            self.youtube.launch(self.now())
        return True

    def close_app(self, app_id: str) -> bool:
        if not self._installed(app_id):
            return False
        self.closed.append(app_id)
        self.running.discard(app_id)
        if self.foreground == app_id:
            self.foreground = None
        if app_id == YOUTUBE_ID:
            self.youtube.close()
        return True

    # ------------------------------------------------------------- websocket
    async def _handle(self, ws: ServerConnection) -> None:
        q = parse_qs(urlparse(ws.request.path).query)
        name = base64.b64decode(q.get("name", [""])[0]).decode(errors="replace")
        token = q.get("token", [None])[0]
        self.clients.append(name)
        authorized = token == self.token
        if not authorized:
            if self.pairing == "deny":
                await ws.send(json.dumps({"event": "ms.channel.unauthorized"}))
                return
            if self.pairing == "timeout":
                await ws.send(json.dumps({"event": "ms.channel.timeOut"}))
                return
            if self.pairing == "silent":
                await ws.wait_closed()
                return
        data: dict[str, Any] = {"clients": [], "id": "x"}
        if not authorized:
            data["token"] = self.token
        await ws.send(json.dumps({"event": "ms.channel.connect", "data": data}))
        self._connections.add(ws)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                await self._react(ws, msg)
        except Exception:
            pass
        finally:
            self._connections.discard(ws)

    async def _emit(self, ws: ServerConnection, event: str, data: Any = None) -> None:
        if self.ime_events:
            await ws.send(json.dumps({"event": event, "data": data}))

    async def _react(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        params = msg.get("params", {})
        kind = params.get("TypeOfRemote")
        if kind == "SendRemoteKey" and params.get("Cmd") == "Click":
            await self._key(ws, params.get("DataOfCmd"))
        elif kind == "SendInputString" and self.power == "on" and self.foreground == YOUTUBE_ID:
            text = base64.b64decode(params.get("Cmd", "")).decode(errors="replace")
            if self.youtube.type(text, self.now()):
                await self._emit(ws, "ms.remote.imeUpdate", base64.b64encode(text.encode()).decode())
        elif kind == "SendInputEnd" and self.power == "on" and self.foreground == YOUTUBE_ID:
            if self.youtube.submit(self.now()):
                await self._emit(ws, "ms.remote.imeEnd")
        if params.get("event") == "ed.installedApp.get" and self.answers_installed_apps:
            await ws.send(json.dumps({"event": "ed.installedApp.get", "data": {"data": self.apps}}))
        if params.get("event") == "ed.apps.launch":
            ok = self.launch(str((params.get("data") or {}).get("appId")))
            if self.acks_ws_launch:
                await ws.send(json.dumps({"event": "ed.apps.launch", "data": 200 if ok else 404}))

    async def _key(self, ws: ServerConnection, key: str) -> None:
        if key == "KEY_POWER":
            self.power = "standby" if self.power == "on" else "on"
            return
        if self.power != "on":
            return
        if key == "KEY_VOLUP":
            self.volume = min(100, self.volume + 1)
        elif key == "KEY_VOLDOWN":
            self.volume = max(0, self.volume - 1)
        elif key == "KEY_MUTE":
            self.muted = not self.muted
        elif key == "KEY_HOME":
            if self.foreground == YOUTUBE_ID and self.youtube.state == "player":
                self.youtube.paused = True
            self.foreground = None
        elif self.foreground == YOUTUBE_ID:
            change = self.youtube.key(key, self.now())
            if change == "ime_open":
                await self._emit(ws, "ms.remote.imeStart", "")
            elif change == "ime_close":
                await self._emit(ws, "ms.remote.imeEnd")

    # ------------------------------------------------------------------ http
    def _start_http(self, port: int) -> None:
        tv = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the client gave up waiting

            def _json(self, code: int, data: Any = None) -> None:
                self._send(code, json.dumps(data).encode() if data is not None else b"")

            def _app_id(self) -> str:
                return self.path.rstrip("/").rsplit("/", 1)[-1]

            def _hang(self) -> None:
                time.sleep(tv.hang_seconds)
                self._json(404)

            def _dial(self, method: str) -> None:
                parts = self.path.strip("/").split("/")  # ws/apps/<Name>[/run]
                name = parts[2] if len(parts) > 2 else ""
                app_id = next((i for i, n in DIAL_NAMES.items() if n == name and tv._installed(i)), None)
                if not tv.dial or tv.power != "on" or not app_id:
                    return self._send(404)
                if method == "DELETE":
                    tv.close_app(app_id)
                    return self._send(200)
                state = "running" if app_id in tv.running else "stopped"
                xml = (f'<?xml version="1.0" encoding="UTF-8"?><service xmlns="urn:dial-multiscreen-org:schemas:dial" '
                       f'dialVer="2.2"><name>{name}</name><options allowStop="true"/><state>{state}</state>'
                       f'<link rel="run" href="run"/><additionalData><screenId>fake-screen-id</screenId>'
                       f"</additionalData></service>")
                self._send(200, xml.encode(), "text/xml")

            def _upnp(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode(errors="replace")
                action = (self.headers.get("SOAPACTION") or "").strip('"').rsplit("#", 1)[-1]
                out = ""
                if action == "GetVolume":
                    out = f"<CurrentVolume>{tv.volume}</CurrentVolume>"
                elif action == "GetMute":
                    out = f"<CurrentMute>{1 if tv.muted else 0}</CurrentMute>"
                elif action == "SetVolume":
                    m = re.search(r"<DesiredVolume>(\d+)</DesiredVolume>", body)
                    if not m:
                        return self._send(500)
                    tv.volume = max(0, min(100, int(m.group(1))))
                elif action == "SetMute":
                    m = re.search(r"<DesiredMute>(\w+)</DesiredMute>", body)
                    if not m:
                        return self._send(500)
                    tv.muted = m.group(1) in ("1", "true")
                else:
                    return self._send(401)
                xml = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                       f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">{out}'
                       f"</u:{action}Response></s:Body></s:Envelope>")
                self._send(200, xml.encode(), "text/xml")

            def do_GET(self) -> None:  # noqa: N802
                if tv.power == "off":
                    return self._json(404)
                if self.path.startswith("/ws/apps/"):
                    return self._dial("GET")
                if self.path == "/dmr":
                    if not tv.upnp or tv.power != "on":
                        return self._send(404)
                    xml = ("<root><device><serviceList><service>"
                           "<serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>"
                           "<controlURL>/upnp/control/RenderingControl1</controlURL>"
                           "</service></serviceList></device></root>")
                    return self._send(200, xml.encode(), "text/xml")
                if self.path.startswith("/api/v2/applications/"):
                    if tv.rest_apps == "hang":
                        return self._hang()
                    app = tv._installed(self._app_id())
                    if not app:
                        return self._json(404, {"messages": "Not found"})
                    return self._json(200, {"id": app["appId"], "name": app["name"],
                                            "running": app["appId"] in tv.running, "version": "1.0",
                                            "visible": tv.foreground == app["appId"] and tv.power == "on"})
                if self.path.rstrip("/") == "/api/v2":
                    if tv.rest_info == "hang":
                        return self._hang()
                    return self._json(200, {"device": {
                        "name": tv.name,
                        "modelName": "0",
                        "model": "24_PTM_QD_T09",
                        "wifiMac": tv.mac,
                        "PowerState": tv.power,
                        "TokenAuthSupport": "true",
                    }})
                self._json(404)

            def do_POST(self) -> None:  # noqa: N802
                if tv.power == "off":
                    return self._json(404)
                if self.path.startswith("/upnp/control/"):
                    if not tv.upnp or tv.power != "on":
                        return self._send(404)
                    return self._upnp()
                if not self.path.startswith("/api/v2/applications/"):
                    return self._json(404)
                if tv.rest_apps == "hang":
                    return self._hang()
                if not tv.launch(self._app_id()):
                    return self._json(404)
                self._json(200, True)

            def do_DELETE(self) -> None:  # noqa: N802
                if tv.power == "off":
                    return self._json(404)
                if self.path.startswith("/ws/apps/"):
                    return self._dial("DELETE")
                if not self.path.startswith("/api/v2/applications/"):
                    return self._json(404)
                if tv.rest_apps == "hang":
                    return self._hang()
                self._json(200 if tv.close_app(self._app_id()) else 404, True)

            def log_message(self, *a: object) -> None:
                pass

        self._http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._http.daemon_threads = True
        self.info_port = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever, daemon=True).start()


async def _main() -> None:
    tv = await FakeTV().start(ws_port=8002, info_port=8001)
    print(f"Fake TV running: ws://127.0.0.1:{tv.ws_port}  info http://127.0.0.1:{tv.info_port}/api/v2/")
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(_main())
