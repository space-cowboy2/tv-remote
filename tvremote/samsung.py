"""Async client for the local remote-control API of Samsung Tizen TVs (2016+,
including the 2024 S90D/S95D OLEDs).

Protocol summary
- Control channel: wss://<tv>:8002/api/v2/channels/samsung.remote.control
  ?name=<base64 client name>&token=<token>
- First connection shows an Allow/Deny prompt on the TV. On Allow the TV sends
  `ms.channel.connect` with a token; reuse it to skip the prompt next time.
- Device info (incl. MAC and PowerState): http://<tv>:8001/api/v2/
- Power on from standby: Wake-on-LAN (see wol.py).

The TV uses a self-signed certificate, so certificate verification is off for
this LAN-only connection. Never expose the TV's ports to the internet; the hub
is the only thing that should talk to the TV.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from .apps import KNOWN_APPS
from .keys import normalize_key
from .wol import send_wol

log = logging.getLogger(__name__)


class TVError(Exception):
    """Base error for TV communication."""


class PairingDenied(TVError):
    """The user pressed Deny on the TV, or the token was rejected."""


class PairingTimeout(TVError):
    """Nobody answered the Allow prompt on the TV in time."""


class TVUnreachable(TVError):
    """The TV is off, asleep, or not on this network."""


def _insecure_ssl() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class SamsungTV:
    def __init__(
        self,
        host: str,
        *,
        name: str = "TV Remote",
        token: str | None = None,
        port: int = 8002,
        secure: bool = True,
        info_port: int = 8001,
        dial_port: int = 8080,
        mac: str | None = None,
        on_token: Callable[[str], None] | None = None,
        pair_timeout: float = 30.0,
        request_timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.name = name
        self.token = token
        self.port = port
        self.secure = secure
        self.info_port = info_port
        self.dial_port = dial_port
        self.mac = mac
        self.on_token = on_token
        self.pair_timeout = pair_timeout
        self.request_timeout = request_timeout

        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._waiters: dict[str, list[asyncio.Future[Any]]] = {}
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- connection

    @property
    def url(self) -> str:
        scheme = "wss" if self.secure else "ws"
        name = base64.b64encode(self.name.encode()).decode()
        url = f"{scheme}://{self.host}:{self.port}/api/v2/channels/samsung.remote.control?name={name}"
        if self.token:
            url += f"&token={self.token}"
        return url

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._reader is not None and not self._reader.done()

    async def connect(self) -> None:
        """Open the control channel. On first use this blocks until someone
        presses Allow on the TV (up to pair_timeout seconds)."""
        async with self._lock:
            if self.connected:
                return
            await self._close_unlocked()
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(
                        self.url,
                        ssl=_insecure_ssl() if self.secure else None,
                        max_size=8 * 1024 * 1024,
                        open_timeout=self.request_timeout,
                        close_timeout=2,
                    ),
                    timeout=self.request_timeout + 1,
                )
            except (OSError, asyncio.TimeoutError, websockets.InvalidHandshake) as e:
                raise TVUnreachable(f"cannot reach TV at {self.host}: {e}") from e

            try:
                first = await asyncio.wait_for(self._recv_json(ws), timeout=self.pair_timeout)
            except asyncio.TimeoutError as e:
                await ws.close()
                raise PairingTimeout("no answer to the pairing prompt on the TV") from e
            except websockets.ConnectionClosed as e:
                raise PairingDenied("TV closed the connection during pairing") from e

            event = first.get("event")
            if event == "ms.channel.unauthorized":
                await ws.close()
                raise PairingDenied("pairing was denied on the TV")
            if event == "ms.channel.timeOut":
                await ws.close()
                raise PairingTimeout("pairing prompt timed out on the TV")
            if event != "ms.channel.connect":
                await ws.close()
                raise TVError(f"unexpected first event from TV: {event!r}")

            new_token = (first.get("data") or {}).get("token")
            if new_token and new_token != self.token:
                self.token = str(new_token)
                if self.on_token:
                    self.on_token(self.token)

            self._ws = ws
            self._reader = asyncio.create_task(self._read_loop(ws))
            log.info("connected to TV %s", self.host)

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._fail_waiters(TVUnreachable("connection closed"))

    async def __aenter__(self) -> "SamsungTV":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @staticmethod
    async def _recv_json(ws: ClientConnection) -> dict[str, Any]:
        raw = await ws.recv()
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}

    async def _read_loop(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                event = msg.get("event")
                for fut in self._waiters.pop(event, []):
                    if not fut.done():
                        fut.set_result(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._fail_waiters(TVUnreachable("connection to TV lost"))

    def _fail_waiters(self, exc: Exception) -> None:
        waiters, self._waiters = self._waiters, {}
        for futs in waiters.values():
            for fut in futs:
                if not fut.done():
                    fut.set_exception(exc)

    async def _send(self, payload: dict[str, Any]) -> None:
        """Send a message, reconnecting once if the channel dropped."""
        for attempt in (1, 2):
            await self.connect()
            assert self._ws is not None
            try:
                await self._ws.send(json.dumps(payload))
                return
            except websockets.ConnectionClosed:
                await self.close()
                if attempt == 2:
                    raise TVUnreachable("connection to TV lost")

    async def _request(self, payload: dict[str, Any], reply_event: str, timeout: float | None = None) -> dict[str, Any]:
        await self.connect()
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(reply_event, []).append(fut)
        await self._send(payload)
        try:
            return await asyncio.wait_for(fut, timeout=timeout or self.request_timeout)
        except asyncio.TimeoutError as e:
            waiters = self._waiters.get(reply_event, [])
            if fut in waiters:
                waiters.remove(fut)
            raise TVError(f"TV did not answer {reply_event!r}") from e

    # ------------------------------------------------------------------ commands

    async def send_key(self, key: str, cmd: str = "Click") -> None:
        """cmd is Click, Press (start holding) or Release."""
        if cmd not in ("Click", "Press", "Release"):
            raise ValueError(f"invalid cmd: {cmd!r}")
        await self._send({
            "method": "ms.remote.control",
            "params": {
                "Cmd": cmd,
                "DataOfCmd": normalize_key(key),
                "Option": "false",
                "TypeOfRemote": "SendRemoteKey",
            },
        })

    async def hold_key(self, key: str, seconds: float) -> None:
        await self.send_key(key, "Press")
        try:
            await asyncio.sleep(seconds)
        finally:
            await self.send_key(key, "Release")

    async def send_text(self, text: str, submit: bool = False) -> None:
        """Type into the TV's on-screen keyboard (it must be open)."""
        await self._send({
            "method": "ms.remote.control",
            "params": {
                "Cmd": base64.b64encode(text.encode()).decode(),
                "DataOfCmd": "base64",
                "TypeOfRemote": "SendInputString",
            },
        })
        if submit:
            await self._send({"method": "ms.remote.control", "params": {"TypeOfRemote": "SendInputEnd"}})

    async def move_cursor(self, dx: int, dy: int) -> None:
        """Move the pointer (used by the browser and some apps)."""
        await self._send({
            "method": "ms.remote.control",
            "params": {
                "Cmd": "Move",
                "Position": {"x": dx, "y": dy, "Time": "0"},
                "TypeOfRemote": "ProcessMouseDevice",
            },
        })

    async def list_apps(self) -> list[dict[str, Any]]:
        """Installed apps. Older firmware answers `ed.installedApp.get`; 2024
        firmware ignores it, so fall back to probing known app IDs over REST."""
        try:
            msg = await self._request(
                {"method": "ms.channel.emit", "params": {"event": "ed.installedApp.get", "to": "host"}},
                reply_event="ed.installedApp.get",
                timeout=2.5,
            )
            apps = ((msg.get("data") or {}).get("data")) or []
            if apps:
                return [{"id": a.get("appId"), "name": a.get("name"), "type": a.get("app_type")} for a in apps]
        except TVUnreachable:
            raise
        except TVError:
            log.info("TV ignored ed.installedApp.get; probing known apps over REST")
        return await self._probe_known_apps()

    async def _probe_known_apps(self) -> list[dict[str, Any]]:
        sem = asyncio.Semaphore(6)

        async def probe(app_id: str, fallback_name: str) -> dict[str, Any] | None:
            async with sem:
                data = await self.app_status(app_id)
            if not data:
                return None
            return {"id": app_id, "name": data.get("name") or fallback_name, "type": None,
                    "running": bool(data.get("running"))}

        found = await asyncio.gather(*(probe(i, n) for i, n in KNOWN_APPS.items()))
        apps, names = [], set()
        for a in found:
            if a and a["name"] not in names:  # skip duplicate IDs of the same app
                names.add(a["name"])
                apps.append(a)
        return apps

    def _rest(self, method: str, path: str) -> tuple[int, dict[str, Any] | None]:
        req = urllib.request.Request(f"http://{self.host}:{self.info_port}{path}", method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as r:
                body = r.read()
                try:
                    return r.status, json.loads(body) if body else None
                except ValueError:
                    return r.status, None
        except urllib.error.HTTPError as e:
            return e.code, None

    async def app_status(self, app_id: str) -> dict[str, Any] | None:
        """{id, name, running, visible, version} or None if not installed."""
        try:
            code, data = await asyncio.to_thread(self._rest, "GET", f"/api/v2/applications/{app_id}")
        except OSError as e:
            raise TVUnreachable(f"cannot reach TV at {self.host}: {e}") from e
        return data if code == 200 and isinstance(data, dict) else None

    async def launch_app(self, app_id: str, deep_link: bool = True, meta_tag: str = "") -> None:
        """Open an app. Uses the REST endpoint (works on all recent firmware),
        falling back to the WebSocket launch event."""
        try:
            code, _ = await asyncio.to_thread(self._rest, "POST", f"/api/v2/applications/{app_id}")
            if 200 <= code < 300:
                return
            if code == 404:
                raise TVError(f"app {app_id} is not installed")
        except OSError:
            pass
        await self._send({
            "method": "ms.channel.emit",
            "params": {
                "event": "ed.apps.launch",
                "to": "host",
                "data": {
                    "appId": app_id,
                    "action_type": "DEEP_LINK" if deep_link else "NATIVE_LAUNCH",
                    "metaTag": meta_tag,
                },
            },
        })

    def _dial(self, app_name: str, payload: str | None) -> int:
        """DIAL (the protocol phone casting uses): GET app state or POST a
        launch payload to http://TV:8080/ws/apps/<Name>. Samsung requires an
        Origin header on POST, otherwise it answers 403."""
        url = f"http://{self.host}:{self.dial_port}/ws/apps/{app_name}"
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            req = urllib.request.Request(url, data=payload.encode(), method="POST", headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Origin": "https://www.youtube.com",
            })
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    async def dial_launch(self, app_name: str, payload: str) -> None:
        """Launch a DIAL-registered app (e.g. "YouTube") with a payload such
        as "v=<video id>". This is what reliably opens a specific YouTube
        video on 2024 Tizen; ed.apps.launch deep links are ignored."""
        try:
            code = await asyncio.to_thread(self._dial, app_name, payload)
        except OSError as e:
            raise TVUnreachable(f"cannot reach TV at {self.host}: {e}") from e
        if code == 404:
            raise TVError(f"{app_name} is not available over DIAL on this TV")
        if not 200 <= code < 300:
            raise TVError(f"DIAL launch of {app_name} failed ({code})")

    async def open_deep_link(self, app_id: str, meta_tag: str) -> None:
        """Ask an app to open specific content (e.g. YouTube "v=<id>"). Sent
        over the WebSocket because the REST launch ignores parameters. Apps
        that don't support it just open normally or ignore it."""
        await self._send({
            "method": "ms.channel.emit",
            "params": {
                "event": "ed.apps.launch",
                "to": "host",
                "data": {"appId": app_id, "action_type": "DEEP_LINK", "metaTag": meta_tag},
            },
        })

    # --------------------------------------------------------------------- power

    async def info(self) -> dict[str, Any]:
        """Device info from the TV's REST endpoint. Raises TVUnreachable if off."""
        url = f"http://{self.host}:{self.info_port}/api/v2/"

        def fetch() -> dict[str, Any]:
            with urllib.request.urlopen(url, timeout=self.request_timeout) as r:
                return json.loads(r.read())

        try:
            return await asyncio.to_thread(fetch)
        except (OSError, ValueError) as e:
            raise TVUnreachable(f"TV info unavailable at {url}: {e}") from e

    @staticmethod
    def clean_name(raw: str | None) -> str:
        """TVs report names HTML-escaped, sometimes twice (42&amp;quot; OLED)."""
        name = raw or "TV"
        for _ in range(3):
            unescaped = html.unescape(name)
            if unescaped == name:
                break
            name = unescaped
        return name

    async def power_state(self) -> str:
        """'on', 'standby' or 'off' (unreachable)."""
        try:
            data = await self.info()
        except TVUnreachable:
            return "off"
        state = (data.get("device") or {}).get("PowerState", "on")
        return str(state).lower()

    async def mac_address(self) -> str | None:
        if self.mac:
            return self.mac
        try:
            data = await self.info()
        except TVUnreachable:
            return None
        self.mac = (data.get("device") or {}).get("wifiMac")
        return self.mac

    async def power_on(self, wait: float = 15.0) -> bool:
        """Wake the TV. Returns True once it reports 'on'."""
        state = await self.power_state()
        if state == "on":
            return True
        mac = self.mac
        if not mac:
            raise TVError("MAC address unknown; run `pair` while the TV is on to save it")
        if state == "standby":
            # Network standby: the API is up but the screen is off.
            try:
                await self.send_key("KEY_POWER")
            except TVError:
                send_wol(mac)
        else:
            send_wol(mac)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while loop.time() < deadline:
            await asyncio.sleep(1)
            if await self.power_state() == "on":
                return True
            send_wol(mac)
        return False

    async def power_off(self) -> None:
        if await self.power_state() == "on":
            await self.send_key("KEY_POWER")
        await self.close()
