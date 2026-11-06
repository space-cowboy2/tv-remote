"""Home hub: an always-on HTTP/WebSocket API in front of the TV.

Run:  tvremote-hub            (or: python -m tvremote.hub)

Environment
  TVREMOTE_CONFIG     path to tv.json from `tvremote pair` (default ~/.config/tvremote/tv.json)
  TVREMOTE_HUB_KEY    access key clients must present. If unset, one is generated
                      on first start and stored next to tv.json (hub.key, mode 600).
  TVREMOTE_BIND       host to listen on (default 127.0.0.1; use 0.0.0.0 for the LAN)
  TVREMOTE_PORT       port (default 8080)
  TVREMOTE_WEB_DIR    folder of the web app to serve at / (optional)
  TVREMOTE_SECURE_COOKIE  1 to always mark the sign-in cookie Secure (set when
                      served over HTTPS, e.g. behind `tailscale serve`)
  ANTHROPIC_API_KEY   enables /api/assistant (or put the key in anthropic.key
                      next to tv.json, mode 600). See docs/ASSISTANT.md.
  TVREMOTE_TRUSTED_PROXIES  proxies whose X-Forwarded-* headers are trusted
                      (default 127.0.0.1, which covers `tailscale serve`)
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .assistant import Assistant, TVActions, load_api_key, load_macros
from .config import TVConfig, default_path
from .keys import KEY_GROUPS, is_valid_key, normalize_key
from .samsung import SamsungTV, TVError, TVUnreachable

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


class AssistantBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class LoginBody(BaseModel):
    key: str = Field(max_length=200)


# ------------------------------------------------------------------------ app

def create_app(tv: SamsungTV | None = None, access_key: str | None = None,
               config_path: Path | None = None, web_dir: str | None = None,
               assistant_client: Any = None) -> FastAPI:
    config_path = config_path or default_path()
    if tv is None:
        cfg = TVConfig.load(config_path)
        if not cfg:
            raise SystemExit(f"No paired TV in {config_path}. Run `tvremote pair --host <TV IP>` first.")

        def save_token(token: str) -> None:
            cfg.token = token
            cfg.save(config_path)

        tv = SamsungTV(cfg.host, name=cfg.name, token=cfg.token, mac=cfg.mac, on_token=save_token)
    key = access_key or load_or_create_key(config_path)
    limiter = LoginLimiter()
    apps_cache: dict[str, Any] = {"at": 0.0, "apps": []}
    tv_lock = asyncio.Lock()
    assistant_limiter = LoginLimiter(limit=20, window=60)
    assistant: Assistant | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await tv.connect()
        except TVError as e:
            log.warning("TV not connected at startup (%s); will retry on first command", e)
        yield
        await tv.close()

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

    # -- protected
    auth = [Depends(require_auth)]

    @app.get("/api/status", dependencies=auth)
    async def get_status() -> dict[str, Any]:
        return {"power": await tv.power_state(), "connected": tv.connected, "host": tv.host}

    @app.get("/api/keys", dependencies=auth)
    async def get_keys() -> dict[str, list[str]]:
        return KEY_GROUPS

    @app.post("/api/key", dependencies=auth)
    async def press(body: KeyBody) -> dict[str, bool]:
        if not is_valid_key(body.key):
            raise HTTPException(422, f"unknown key {normalize_key(body.key)}")
        async with tv_lock:
            await tv.send_key(body.key, body.cmd)
        return {"ok": True}

    @app.post("/api/text", dependencies=auth)
    async def text(body: TextBody) -> dict[str, bool]:
        async with tv_lock:
            await tv.send_text(body.text, submit=body.submit)
        return {"ok": True}

    @app.get("/api/apps", dependencies=auth)
    async def apps(refresh: bool = False) -> list[dict[str, Any]]:
        if refresh or time.monotonic() - apps_cache["at"] > 600 or not apps_cache["apps"]:
            async with tv_lock:
                apps_cache["apps"] = sorted(await tv.list_apps(), key=lambda a: (a["name"] or "").lower())
            apps_cache["at"] = time.monotonic()
        return apps_cache["apps"]

    @app.post("/api/launch", dependencies=auth)
    async def launch(body: LaunchBody) -> dict[str, bool]:
        async with tv_lock:
            await tv.launch_app(body.app_id)
        return {"ok": True}

    @app.post("/api/power", dependencies=auth)
    async def power(body: PowerBody) -> dict[str, Any]:
        if body.state == "on":
            ok = await tv.power_on()
            if not ok:
                raise HTTPException(504, "TV did not wake. Check 'Power On with Mobile' is enabled.")
        else:
            async with tv_lock:
                await tv.power_off()
        return {"ok": True, "power": await tv.power_state()}

    async def cached_apps() -> list[dict[str, Any]]:
        return await apps(refresh=False)

    def get_assistant() -> Assistant:
        nonlocal assistant
        if assistant is None:
            api_key = load_api_key(config_path.parent)
            if not api_key and assistant_client is None:
                raise HTTPException(503, "Claude is not set up on the hub. Add an Anthropic API key (see docs/ASSISTANT.md).")
            macros = load_macros(config_path.parent)
            assistant = Assistant(lambda: TVActions(tv, tv_lock, macros, cached_apps),
                                  api_key=api_key or "", client=assistant_client)
        return assistant

    @app.get("/api/assistant", dependencies=auth)
    async def assistant_status() -> dict[str, Any]:
        return {"enabled": assistant_client is not None or bool(load_api_key(config_path.parent))}

    @app.post("/api/assistant", dependencies=auth)
    async def ask(body: AssistantBody, request: Request) -> dict[str, Any]:
        client = request.client.host if request.client else "?"
        if assistant_limiter.blocked(client):
            raise HTTPException(429, "too many requests, slow down")
        assistant_limiter.fail(client)  # counts every request
        bot = get_assistant()
        from anthropic import APIConnectionError, APIStatusError, AuthenticationError, RateLimitError
        try:
            return await bot.handle(body.text.strip())
        except AuthenticationError:
            raise HTTPException(503, "The hub's Anthropic API key was rejected.")
        except RateLimitError:
            raise HTTPException(429, "Claude is busy, try again in a moment.")
        except APIStatusError as e:
            log.warning("Claude API error %s: %s", e.status_code, e.message)
            raise HTTPException(502, f"Claude error ({e.status_code}).")
        except APIConnectionError:
            raise HTTPException(504, "Can't reach Claude from the hub.")

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
                        async with tv_lock:
                            await tv.send_key(str(msg["key"]), cmd)
                    elif "text" in msg:
                        async with tv_lock:
                            await tv.send_text(str(msg["text"])[:500], bool(msg.get("submit")))
                    else:
                        raise ValueError("expected 'key' or 'text'")
                    await socket.send_json({"ok": True, "id": msg.get("id")})
                except (TVError, ValueError) as e:
                    await socket.send_json({"ok": False, "id": msg.get("id"), "error": str(e)})
        except WebSocketDisconnect:
            pass

    web = web_dir or os.environ.get("TVREMOTE_WEB_DIR")
    if web and Path(web).is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    uvicorn.run(
        app,
        host=os.environ.get("TVREMOTE_BIND", "127.0.0.1"),
        port=int(os.environ.get("TVREMOTE_PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("TVREMOTE_TRUSTED_PROXIES", "127.0.0.1"),
        log_level="info",
    )


if __name__ == "__main__":
    main()
