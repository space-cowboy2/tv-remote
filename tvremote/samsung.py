"""Async client for the local remote-control API of Samsung Tizen TVs (2016+,
including the 2024 S90D/S95D OLEDs).

Protocol summary
- Control channel: wss://<tv>:8002/api/v2/channels/samsung.remote.control
  ?name=<base64 client name>&token=<token>
- First connection shows an Allow/Deny prompt on the TV. On Allow the TV sends
  `ms.channel.connect` with a token; reuse it to skip the prompt next time.
- Device info (incl. MAC and PowerState): http://<tv>:8001/api/v2/
- App state: REST on 8001 where the firmware answers it, DIAL on 8080
  (`/ws/apps/YouTube`) for the apps that register there.
- Volume and mute level: UPnP renderer on 9197 (see upnp.py).
- Power on from standby: Wake-on-LAN (see wol.py).

What the TV never tells us is what is on screen or whether a video is
playing. Everything here that says "state" is one of the signals above.

The TV uses a self-signed certificate, so certificate verification is off for
this LAN-only connection. Never expose the TV's ports to the internet; the hub
is the only thing that should talk to the TV.
"""

from __future__ import annotations

import asyncio
import base64
import html
import http.client
import json
import logging
import re
import ssl
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from .apps import DIAL_NAMES, KNOWN_APPS
from .keys import normalize_key
from .upnp import Renderer, UPnPError
from .wol import send_wol

log = logging.getLogger(__name__)

IME_OPEN_EVENTS = ("ms.remote.imeStart",)
IME_TEXT_EVENTS = ("ms.remote.imeUpdate",)
IME_CLOSE_EVENTS = ("ms.remote.imeDone", "ms.remote.imeEnd")


class TVError(Exception):
    """Base error for TV communication."""


class PairingDenied(TVError):
    """The user pressed Deny on the TV, or the token was rejected."""


class PairingTimeout(TVError):
    """Nobody answered the Allow prompt on the TV in time."""


class TVUnreachable(TVError):
    """The TV is off, asleep, or not on this network."""


class _NoAnswer(OSError):
    """The TV accepted the connection but did not answer in time."""


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
        upnp_port: int = 9197,
        mac: str | None = None,
        lan_mac: str | None = None,
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
        self.upnp_port = upnp_port
        self.mac = mac
        self.lan_mac = lan_mac
        self.on_token = on_token
        self.pair_timeout = pair_timeout
        self.request_timeout = request_timeout

        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._waiters: dict[str, list[asyncio.Future[Any]]] = {}
        self._lock = asyncio.Lock()
        self._renderer = Renderer(host, upnp_port, timeout=min(request_timeout, 3.0))

        # Feedback the TV volunteers on the control channel. A text field opening
        # on the TV (imeStart) is the only sign that a search box really opened.
        self.events: deque[tuple[float, str]] = deque(maxlen=100)
        self.ime_open = False
        self.ime_text: str | None = None
        self.ime_seen = False  # this TV has sent at least one keyboard event
        self.ime_changes = 0   # bumps on every keyboard event, so callers can spot a new one

        # None = not tried yet. False = the app endpoints on 8001 hung (seen on a
        # 2022 Q60B, where one hung request also stalled the whole port for a while).
        self.rest_apps_ok: bool | None = None
        self.power_source = "none"  # which signal the last power_state() used
        self.sleep: Callable[[float], Any] = asyncio.sleep  # tests replace this

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
            self.ime_open = False
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
                if not isinstance(msg, dict):
                    continue
                event = msg.get("event")
                self._note_event(event, msg)
                for fut in self._waiters.pop(event, []):
                    if not fut.done():
                        fut.set_result(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.ime_open = False
            self._fail_waiters(TVUnreachable("connection to TV lost"))

    def _note_event(self, event: Any, msg: dict[str, Any]) -> None:
        if not isinstance(event, str):
            return
        self.events.append((time.time(), event))
        if event in IME_OPEN_EVENTS:
            self.ime_open, self.ime_text = True, ""
        elif event in IME_TEXT_EVENTS:
            self.ime_open = True
            self.ime_text = self._ime_payload(msg.get("data"))
        elif event in IME_CLOSE_EVENTS:
            self.ime_open = False
        else:
            return
        self.ime_seen = True
        self.ime_changes += 1

    @staticmethod
    def _ime_payload(data: Any) -> str | None:
        """imeUpdate carries the field's text, base64-encoded."""
        if not isinstance(data, str):
            return None
        try:
            return base64.b64decode(data, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return data

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

    # ---------------------------------------------------------------------- http

    def _http(self, method: str, port: int, path: str, *, timeout: float | None = None,
              body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        """One plain-HTTP request to the TV. Raises _NoAnswer when the TV took
        the connection and then stayed silent, OSError when it can't be reached."""
        conn = http.client.HTTPConnection(self.host, port, timeout=timeout or self.request_timeout)
        try:
            conn.connect()
            try:
                conn.request(method, path, body=body, headers=headers or {})
                r = conn.getresponse()
                return r.status, r.read()
            except TimeoutError as e:
                raise _NoAnswer(f"no answer from {self.host}:{port}{path}") from e
            except http.client.HTTPException as e:
                raise OSError(f"bad answer from {self.host}:{port}: {e}") from e
        finally:
            conn.close()

    def _rest(self, method: str, path: str, timeout: float | None = None) -> tuple[int, dict[str, Any] | None]:
        code, body = self._http(method, self.info_port, path, timeout=timeout)
        try:
            data = json.loads(body) if body else None
        except ValueError:
            data = None
        return code, data

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
            await self.sleep(0.3)  # let the keyboard take the text before it is told to finish
            await self.submit_text()

    async def submit_text(self) -> None:
        """Press the on-screen keyboard's Done/Search."""
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

    # ---------------------------------------------------------------------- apps

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
        # One careful probe first: on firmware where the app endpoints hang, a
        # burst of them takes port 8001 down with it.
        first_id, first_name = next(iter(KNOWN_APPS.items()))
        first = await self.app_status(first_id, timeout=2.5)
        if self.rest_apps_ok is False:
            return await self._dial_known_apps()
        sem = asyncio.Semaphore(4)

        async def probe(app_id: str, fallback_name: str) -> dict[str, Any] | None:
            if app_id == first_id:
                data = first
            else:
                async with sem:
                    data = await self.app_status(app_id, timeout=2.5)
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

    async def _dial_known_apps(self) -> list[dict[str, Any]]:
        """Last resort when neither the app list event nor REST works: the few
        apps the TV registers with DIAL."""
        apps, names = [], set()
        for app_id, dial_name in DIAL_NAMES.items():
            if dial_name in names:
                continue
            state = await self.dial_state(dial_name)
            if state:
                names.add(dial_name)
                apps.append({"id": app_id, "name": KNOWN_APPS.get(app_id, dial_name), "type": None,
                             "running": state["state"] == "running"})
        return apps

    async def app_status(self, app_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        """{id, name, running, visible, version}, or None if the app is not
        installed or this TV does not answer app queries (see rest_apps_ok)."""
        if self.rest_apps_ok is False:
            return None
        try:
            code, data = await asyncio.to_thread(self._rest, "GET", f"/api/v2/applications/{app_id}", timeout)
        except _NoAnswer:
            log.warning("TV %s does not answer app status over REST; not asking again", self.host)
            self.rest_apps_ok = False
            return None
        except OSError as e:
            raise TVUnreachable(f"cannot reach TV at {self.host}: {e}") from e
        self.rest_apps_ok = True
        return data if code == 200 and isinstance(data, dict) else None

    async def dial_state(self, dial_name: str) -> dict[str, Any] | None:
        """{state: running|stopped|hidden, screen_id} from the TV's DIAL
        server, or None if the TV has no such DIAL app or doesn't answer."""
        try:
            code, body = await asyncio.to_thread(
                self._http, "GET", self.dial_port, f"/ws/apps/{dial_name}", timeout=min(self.request_timeout, 3.0))
        except OSError:
            return None
        text = body.decode("utf-8", "replace")
        m = re.search(r"<state>\s*([A-Za-z]+)\s*</state>", text)
        if code != 200 or not m:
            return None
        screen = re.search(r"<screenId>\s*([^<\s]+)\s*</screenId>", text)
        return {"state": m.group(1).lower(), "screen_id": screen.group(1) if screen else None}

    async def app_state(self, app_id: str) -> str:
        """Best available answer to "is this app up?": visible (the TV says it is
        on screen), running (up, possibly in the background), stopped, missing
        (not installed) or unknown (this TV gives no signal for this app)."""
        data = await self.app_status(app_id, timeout=2.5)
        if data is not None:
            if data.get("visible"):
                return "visible"
            return "running" if data.get("running") else "stopped"
        dial_name = DIAL_NAMES.get(app_id)
        if dial_name:
            state = await self.dial_state(dial_name)
            if state:
                return "running" if state["state"] == "running" else "stopped"
        return "missing" if self.rest_apps_ok else "unknown"

    async def launch_app(self, app_id: str, deep_link: bool = True, meta_tag: str = "") -> str:
        """Open an app. Uses the REST endpoint (works on all recent firmware),
        falling back to the WebSocket launch event. Returns how it was sent:
        "rest", "accepted" (the TV acknowledged the launch event) or "sent"."""
        if self.rest_apps_ok is not False:
            try:
                code, _ = await asyncio.to_thread(self._rest, "POST", f"/api/v2/applications/{app_id}", 4.0)
                if 200 <= code < 300:
                    return "rest"
                if code == 404:
                    raise TVError(f"app {app_id} is not installed")
            except _NoAnswer:
                self.rest_apps_ok = False
            except OSError:
                pass
        payload = {
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
        }
        try:
            reply = await self._request(payload, reply_event="ed.apps.launch", timeout=3.0)
        except TVUnreachable:
            raise
        except TVError:
            return "sent"  # older firmware launches without acknowledging
        data = reply.get("data")
        if data in (200, "200", True) or (isinstance(data, dict) and data.get("code") in (None, 200)):
            return "accepted"
        raise TVError(f"TV refused to launch app {app_id} ({data!r})")

    async def dial_launch(self, app_id: str, params: dict[str, str]) -> bool:
        """Open an app through the TV's DIAL server and hand it launch parameters,
        which the app receives as if they were in its own address. YouTube reads
        q=<words> and goes straight to the search results for them, running or
        not, with nothing typed. True if the TV accepted the request; what the
        app did with it cannot be seen from here."""
        dial_name = DIAL_NAMES.get(app_id)
        if not dial_name:
            return False
        body = urllib.parse.urlencode(params, quote_via=urllib.parse.quote).encode()
        try:
            code, _ = await asyncio.to_thread(
                self._http, "POST", self.dial_port, f"/ws/apps/{dial_name}", timeout=6.0, body=body,
                headers={"Content-Type": "text/plain; charset=utf-8", "Origin": "https://www.youtube.com"})
        except OSError:
            return False
        return 200 <= code < 300

    async def close_app(self, app_id: str) -> bool:
        """Ask the TV to quit an app. True if the TV accepted the request."""
        dial_name = DIAL_NAMES.get(app_id)
        if dial_name:
            try:
                code, _ = await asyncio.to_thread(
                    self._http, "DELETE", self.dial_port, f"/ws/apps/{dial_name}/run", timeout=3.0,
                    headers={"Origin": "https://www.youtube.com"})
                if 200 <= code < 300:
                    return True
            except OSError:
                pass
        if self.rest_apps_ok is not False:
            try:
                code, _ = await asyncio.to_thread(self._rest, "DELETE", f"/api/v2/applications/{app_id}", 3.0)
                return 200 <= code < 300
            except _NoAnswer:
                self.rest_apps_ok = False
            except OSError:
                pass
        return False

    # -------------------------------------------------------------------- volume

    async def volume(self) -> int | None:
        """Current level 0-100, or None when the TV has no readable volume."""
        try:
            return await asyncio.to_thread(self._renderer.get_volume)
        except UPnPError:
            return None

    async def set_volume(self, level: int) -> int | None:
        """Set the level and return what the TV reports afterwards (None if unsupported)."""
        try:
            await asyncio.to_thread(self._renderer.set_volume, level)
            return await asyncio.to_thread(self._renderer.get_volume)
        except UPnPError:
            return None

    async def muted(self) -> bool | None:
        try:
            return await asyncio.to_thread(self._renderer.get_mute)
        except UPnPError:
            return None

    async def set_mute(self, muted: bool) -> bool | None:
        try:
            await asyncio.to_thread(self._renderer.set_mute, muted)
            return await asyncio.to_thread(self._renderer.get_mute)
        except UPnPError:
            return None

    # --------------------------------------------------------------------- power

    async def info(self, timeout: float | None = None) -> dict[str, Any]:
        """Device info from the TV's REST endpoint. Raises TVUnreachable if off."""
        try:
            code, data = await asyncio.to_thread(self._rest, "GET", "/api/v2/", timeout)
        except OSError as e:
            raise TVUnreachable(f"TV info unavailable at {self.host}:{self.info_port}: {e}") from e
        if code != 200 or not isinstance(data, dict):
            raise TVUnreachable(f"TV info unavailable at {self.host}:{self.info_port} (HTTP {code})")
        return data

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

    async def power_state(self, quick: bool = False) -> str:
        """'on', 'standby' or 'off' (unreachable).

        The TV's own PowerState is the answer when port 8001 replies. That port
        can stall while the TV is plainly on, so before saying "off" this asks
        the DIAL and UPnP services too; power_source records which one answered.
        quick=True is for polling loops: one short attempt instead of two."""
        for _ in range(1 if quick else 2):
            try:
                data = await self.info(timeout=2.0 if quick else min(self.request_timeout, 3.0))
            except TVUnreachable:
                continue
            self.power_source = "tv"
            return str((data.get("device") or {}).get("PowerState", "on")).lower()
        dial, volume = await asyncio.gather(self.dial_state("YouTube"), self.volume())
        if dial or volume is not None:
            self.power_source = "services"
            return "on"
        self.power_source = "none"
        return "off"

    @property
    def wake_macs(self) -> list[str]:
        """Addresses a wake-up is sent to: the one this network sees first, then the one the TV reports."""
        out: list[str] = []
        for mac in (self.lan_mac, self.mac):
            if mac and mac.lower().replace("-", ":") not in [m.lower().replace("-", ":") for m in out]:
                out.append(mac)
        return out

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
        """Wake the TV. Returns True once it reports 'on'.

        KEY_POWER is a toggle, so it is only sent when the TV itself says it is
        in standby; in every other case the wake-up is Wake-on-LAN, which is
        harmless if the TV turns out to be on already."""
        state = await self.power_state()
        if state == "on":
            return True
        macs = self.wake_macs
        if not macs:
            raise TVError("MAC address unknown; run `pair` while the TV is on to save it")

        def wake() -> None:
            for mac in macs:
                send_wol(mac)

        if state == "standby":
            # Network standby: the API is up but the screen is off.
            try:
                await self.send_key("KEY_POWER")
            except TVError:
                wake()
        else:
            wake()
        # Time spent probing counts too: a TV that is off takes seconds per probe.
        waited = 0.0
        while waited < wait:
            await self.sleep(1)
            started = time.monotonic()
            if await self.power_state(quick=True) == "on":
                return True
            waited += 1 + (time.monotonic() - started)
            wake()
        return False

    async def wait_ready(self, wait: float = 12.0) -> bool:
        """After a wake-up the control channel lags the power state by a few
        seconds. True once it accepts a connection."""
        waited = 0.0
        while True:
            try:
                await self.connect()
                return True
            except (PairingDenied, PairingTimeout):
                raise
            except TVError:
                if waited >= wait:
                    return False
                await self.sleep(2)
                waited += 2

    async def power_off(self, wait: float = 0.0) -> bool:
        """Turn the TV off. With wait > 0, returns whether the TV stopped
        reporting 'on' within that many seconds."""
        if await self.power_state() != "on":
            await self.close()
            return True
        await self.send_key("KEY_POWER")
        off = wait <= 0
        waited = 0.0
        while not off and waited < wait:
            await self.sleep(1)
            started = time.monotonic()
            off = await self.power_state(quick=True) != "on"
            waited += 1 + (time.monotonic() - started)
        await self.close()
        return off
