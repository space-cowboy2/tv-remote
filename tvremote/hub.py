"""Home hub: an always-on HTTP/WebSocket API in front of the TV.

Run:  tvremote-hub            (or: python -m tvremote.hub)

The hub starts even when no TV is paired yet: the agent tools then answer that
plainly, and a TV paired later (`tvremote pair`, or the pair_tv tool) is picked
up without a restart. More TVs live next to tv.json as tv-<name>.json.

Environment
  TVREMOTE_CONFIG     path to tv.json from `tvremote pair` (default ~/.config/tvremote/tv.json)
  TVREMOTE_HUB_KEY    access key clients must present. If unset, one is generated
                      on first start and stored next to tv.json (hub.key, mode 600).
  TVREMOTE_BIND       host to listen on (default 127.0.0.1; use 0.0.0.0 for the LAN)
  TVREMOTE_PORT       port (default 8080)
  TVREMOTE_WEB_DIR    folder of the web app to serve at / (optional)
  TVREMOTE_SECURE_COOKIE  1 to always mark the sign-in cookie Secure (set when
                      served over HTTPS, e.g. behind `tailscale serve`)
  TVREMOTE_TRUSTED_PROXIES  proxies whose X-Forwarded-* headers are trusted
                      (default 127.0.0.1, which covers `tailscale serve`)
  TVREMOTE_LOG_FILE   write the log to this file instead of the console
                      (automatic, as hub.log next to tv.json, when there is no console)
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import sys
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import default_path
from .keys import KEY_GROUPS, is_valid_key, normalize_key
from .samsung import SamsungTV, TVError, TVUnreachable
from .tools import Fail, Store, Tools, TVHandle, UnknownTool, load_handles

log = logging.getLogger("tvremote.hub")
COOKIE = "tvremote_session"


# ------------------------------------------------------------------ access key

def load_or_create_key(config_path: Path) -> str:
    env = os.environ.get("TVREMOTE_HUB_KEY")
    if env:
        return env
    key_path = config_path.parent / "hub.key"
    if key_path.exists():
        return key_path.read_text().strip()
    key = secrets.token_urlsafe(24)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    log.warning("Generated hub access key, saved to %s", key_path)
    return key


class LoginLimiter:
    """At most `limit` failed logins per client per `window` seconds."""

    def __init__(self, limit: int = 5, window: float = 300) -> None:
        self.limit, self.window = limit, window
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, client: str) -> bool:
        q = self.failures[client]
        now = time.monotonic()
        while q and now - q[0] > self.window:
            q.popleft()
        return len(q) >= self.limit

    def fail(self, client: str) -> None:
        self.failures[client].append(time.monotonic())


# --------------------------------------------------------------------- models

class KeyBody(BaseModel):
    key: str = Field(max_length=40)
    cmd: Literal["Click", "Press", "Release"] = "Click"


class TextBody(BaseModel):
    text: str = Field(max_length=500)
    submit: bool = False


class LaunchBody(BaseModel):
    app_id: str = Field(max_length=64, pattern=r"^[A-Za-z0-9._-]+$")


class PowerBody(BaseModel):
    state: Literal["on", "off"]


class LoginBody(BaseModel):
    key: str = Field(max_length=200)


# ------------------------------------------------------------------------ app

def create_app(tv: SamsungTV | None = None, access_key: str | None = None,
               config_path: Path | None = None, web_dir: str | None = None,
               tv_args: dict[str, Any] | None = None) -> FastAPI:
    """tv: use exactly this TV (tests, embedding). Without it the hub serves the
    TVs saved next to config_path, including ones paired while it runs."""
    config_path = config_path or default_path()
    store = Store(config_path.parent)
    if tv is not None:
        tools = Tools([TVHandle("tv", "TV", tv)], store=store)
    else:
        tools = Tools(load_handles(config_path, **(tv_args or {})), store=store, config_path=config_path,
                      tv_args=tv_args)
        if not tools.tvs:
            log.warning("No paired TV in %s yet. Pair one with `tvremote pair --host <TV IP>`.", config_path)
    key = access_key or load_or_create_key(config_path)
    limiter = LoginLimiter()
    apps_cache: dict[str, Any] = {"at": 0.0, "apps": []}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for h in tools.tvs.values():
            try:
                await h.tv.connect()
            except TVError as e:
                log.warning("%s not connected at startup (%s); will retry on first command", h.title, e)
        tools.start()
        yield
        await tools.stop()
        for h in tools.tvs.values():
            await h.tv.close()

    app = FastAPI(title="TV Remote Hub", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    # The cookie holds a value derived from the key, never the key itself.
    # Rotating the key signs every device out.
    session_value = hmac.new(key.encode(), b"tvremote-session-v1", "sha256").hexdigest()
    force_secure = os.environ.get("TVREMOTE_SECURE_COOKIE") == "1"

    def check(provided: str | None) -> bool:
        return bool(provided) and hmac.compare_digest(provided.encode(), key.encode())

    def authorized(request: Request | WebSocket) -> bool:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and check(auth[7:].strip()):
            return True
        cookie = request.cookies.get(COOKIE)
        return bool(cookie) and hmac.compare_digest(cookie.encode(), session_value.encode())

    def require_auth(request: Request) -> None:
        if not authorized(request):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in required")

    @app.exception_handler(TVUnreachable)
    async def _unreachable(_: Request, e: TVUnreachable) -> JSONResponse:
        return JSONResponse({"detail": "TV is off or unreachable", "error": str(e)}, status_code=503)

    @app.exception_handler(TVError)
    async def _tv_error(_: Request, e: TVError) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=502)

    @app.exception_handler(Fail)
    async def _fail(_: Request, e: Fail) -> JSONResponse:
        return JSONResponse({"detail": e.message, "kind": e.kind}, status_code=503)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # -- auth
    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, bool]:
        client = request.client.host if request.client else "?"
        if limiter.blocked(client):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts, try later")
        if not check(body.key):
            limiter.fail(client)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong access key")
        response.set_cookie(COOKIE, session_value, httponly=True, samesite="strict",
                            secure=force_secure or request.url.scheme == "https",
                            max_age=60 * 60 * 24 * 365)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # -- public
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    # -- protected (the remote-control routes act on the default TV)
    auth = [Depends(require_auth)]

    @app.get("/api/status", dependencies=auth)
    async def get_status() -> dict[str, Any]:
        return {"power": await tools.tv.power_state(), "connected": tools.tv.connected, "host": tools.tv.host}

    @app.get("/api/keys", dependencies=auth)
    async def get_keys() -> dict[str, list[str]]:
        return KEY_GROUPS

    @app.post("/api/key", dependencies=auth)
    async def press(body: KeyBody) -> dict[str, bool]:
        if not is_valid_key(body.key):
            raise HTTPException(422, f"unknown key {normalize_key(body.key)}")
        async with tools.lock:
            await tools.tv.send_key(body.key, body.cmd)
        return {"ok": True}

    @app.post("/api/text", dependencies=auth)
    async def text(body: TextBody) -> dict[str, bool]:
        async with tools.lock:
            await tools.tv.send_text(body.text, submit=body.submit)
        return {"ok": True}

    @app.get("/api/apps", dependencies=auth)
    async def apps(refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or time.monotonic() - apps_cache["at"] > 600 or not apps_cache["apps"]:
            async with tools.lock:
                apps_cache["apps"] = sorted(await tools.tv.list_apps(), key=lambda a: (a["name"] or "").lower())
            apps_cache["at"] = time.monotonic()
        return apps_cache["apps"]

    @app.post("/api/launch", dependencies=auth)
    async def launch(body: LaunchBody) -> dict[str, bool]:
        async with tools.lock:
            await tools.tv.launch_app(body.app_id)
        return {"ok": True}

    @app.post("/api/power", dependencies=auth)
    async def power(body: PowerBody) -> dict[str, Any]:
        if body.state == "on":
            ok = await tools.tv.power_on()
            if not ok:
                raise HTTPException(504, "TV did not wake. Check 'Power On with Mobile' is enabled.")
        else:
            async with tools.lock:
                await tools.tv.power_off()
        return {"ok": True, "power": await tools.tv.power_state()}

    # -- agent tools (Claude plugin, Duet): the contract in tools.json, with the guide
    #    that tells a model how to use them and the household's notes
    @app.get("/api/tools", dependencies=auth)
    async def list_tools() -> dict[str, Any]:
        return tools.list()

    @app.post("/api/tools/{name}", dependencies=auth)
    async def call_tool(name: str, request: Request) -> dict[str, Any]:
        try:
            args = await request.json() if await request.body() else {}
        except ValueError:
            raise HTTPException(422, "body must be JSON")
        if not isinstance(args, dict):
            raise HTTPException(422, "body must be a JSON object")
        try:
            result = await tools.call(name, args)
            return {"result": str(result), "ok": result.ok}
        except UnknownTool:
            raise HTTPException(404, f"unknown tool {name}")
        except ValueError as e:
            raise HTTPException(422, str(e))

    # -- low-latency channel for button presses: send {"key": "KEY_UP"} or {"text": "..."}
    @app.websocket("/api/ws")
    async def ws(socket: WebSocket) -> None:
        if not authorized(socket):
            await socket.close(code=4401)
            return
        await socket.accept()
        try:
            while True:
                msg = await socket.receive_json()
                try:
                    if "key" in msg:
                        if not is_valid_key(str(msg["key"])):
                            raise ValueError(f"unknown key {msg['key']}")
                        cmd = msg.get("cmd", "Click")
                        if cmd not in ("Click", "Press", "Release"):
                            raise ValueError("invalid cmd")
                        async with tools.lock:
                            await tools.tv.send_key(str(msg["key"]), cmd)
                    elif "text" in msg:
                        async with tools.lock:
                            await tools.tv.send_text(str(msg["text"])[:500], bool(msg.get("submit")))
                    else:
                        raise ValueError("expected 'key' or 'text'")
                    await socket.send_json({"ok": True, "id": msg.get("id")})
                except (TVError, ValueError, Fail) as e:
                    await socket.send_json({"ok": False, "id": msg.get("id"), "error": str(e)})
        except WebSocketDisconnect:
            pass

    web = web_dir or os.environ.get("TVREMOTE_WEB_DIR")
    if web and Path(web).is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


def setup_logging() -> bool:
    """Log to the console, or to a file when there is none. Returns True when
    logging went to a file. A hub started without a window (pythonw on Windows)
    has no console, so it writes hub.log next to tv.json; TVREMOTE_LOG_FILE picks
    another place. Lines hold requests and outcomes, never keys or tokens."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    log_file = os.environ.get("TVREMOTE_LOG_FILE") or (str(default_path().parent / "hub.log") if sys.stderr is None else "")
    if not log_file:
        logging.basicConfig(level=logging.INFO, format=fmt)
        return False
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[handler])
    return True


def main() -> None:
    import uvicorn

    to_file = setup_logging()
    app = create_app()
    uvicorn.run(
        app,
        host=os.environ.get("TVREMOTE_BIND", "127.0.0.1"),
        port=int(os.environ.get("TVREMOTE_PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("TVREMOTE_TRUSTED_PROXIES", "127.0.0.1"),
        log_level="info",
        **({"log_config": None} if to_file else {}),  # uvicorn's own config writes to the console
    )


if __name__ == "__main__":
    main()
