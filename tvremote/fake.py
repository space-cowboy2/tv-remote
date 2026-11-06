"""A simulated Samsung TV for tests and development without real hardware.

Run standalone:  python -m tvremote.fake   (ws on 8002, info on 8001, no TLS)
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import Server, ServerConnection, serve


@dataclass
class FakeTV:
    token: str = "fake-token-123"
    pairing: str = "allow"  # allow | deny | timeout | silent
    power: str = "on"
    mac: str = "AA:BB:CC:DD:EE:FF"
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
    _server: Server | None = None
    _http: ThreadingHTTPServer | None = None
    _connections: set[ServerConnection] = field(default_factory=set)

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

    async def _react(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        params = msg.get("params", {})
        if params.get("DataOfCmd") == "KEY_POWER" and params.get("Cmd") == "Click":
            self.power = "standby" if self.power == "on" else "on"
        if params.get("event") == "ed.apps.launch":
            d = params.get("data") or {}
            self.launched.append(f'{d.get("appId")}#{d.get("metaTag", "")}')
        if params.get("event") == "ed.installedApp.get" and self.answers_installed_apps:
            await ws.send(json.dumps({"event": "ed.installedApp.get", "data": {"data": self.apps}}))

    # ------------------------------------------------------------------ http
    def _start_http(self, port: int) -> None:
        tv = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, code: int, data: Any = None) -> None:
                body = json.dumps(data).encode() if data is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _app(self) -> dict[str, Any] | None:
                app_id = self.path.rstrip("/").rsplit("/", 1)[-1]
                return next((a for a in tv.apps if a["appId"] == app_id), None)

            def do_GET(self) -> None:  # noqa: N802
                if tv.power == "off":
                    return self._json(404)
                if self.path.startswith("/api/v2/applications/"):
                    app = self._app()
                    if not app:
                        return self._json(404, {"messages": "Not found"})
                    return self._json(200, {"id": app["appId"], "name": app["name"], "running": False,
                                            "version": "1.0", "visible": False})
                if self.path.rstrip("/") == "/api/v2":
                    return self._json(200, {"device": {
                        "name": "[TV] 42&amp;quot; OLED",
                        "modelName": "0",
                        "model": "24_PTM_QD_T09",
                        "wifiMac": tv.mac,
                        "PowerState": tv.power,
                        "TokenAuthSupport": "true",
                    }})
                self._json(404)

            def do_POST(self) -> None:  # noqa: N802
                if tv.power == "off" or not self.path.startswith("/api/v2/applications/"):
                    return self._json(404)
                app = self._app()
                if not app:
                    return self._json(404)
                tv.launched.append(app["appId"])
                self._json(200, True)

            def log_message(self, *a: object) -> None:
                pass

        self._http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.info_port = self._http.server_address[1]
        threading.Thread(target=self._http.serve_forever, daemon=True).start()


async def _main() -> None:
    tv = await FakeTV().start(ws_port=8002, info_port=8001)
    print(f"Fake TV running: ws://127.0.0.1:{tv.ws_port}  info http://127.0.0.1:{tv.info_port}/api/v2/")
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(_main())
