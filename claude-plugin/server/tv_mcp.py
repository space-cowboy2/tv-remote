#!/usr/bin/env python3
"""MCP server (stdio) that lets Claude run the TV through the TV Remote hub.

It passes the hub's own tools straight through, so whatever the hub can do is
what Claude sees, and adds get_guide: the hub's guide for handling requests
plus the household's notes. Standard library only; works on Windows, macOS
and Linux.

Config (environment):
  TVREMOTE_HUB_URL       default http://127.0.0.1:8080 (the hub on this computer)
  TVREMOTE_HUB_KEY       the hub access key, or
  TVREMOTE_HUB_KEY_FILE  file holding it (default ~/.config/tvremote/hub.key)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8080"
HERE = Path(__file__).parent
SNAPSHOT, GUIDE_SNAPSHOT = HERE / "tools.json", HERE / "guide.md"
PROTOCOL = "2025-06-18"
VERSION = "0.2.0"
CALL_TIMEOUT = 180  # the hub gives its longest tool (play) 170 s

# Kept very short on purpose: hosts cut long server instructions, and a gateway in front of this server adds its
# own before these. Everything else is in the guide (get_guide).
INSTRUCTIONS = """Runs the household's Samsung TV(s) through a local hub. Each tool does a whole job and answers with a report: what the TV confirmed, what was only sent, what failed.
- Call get_guide before the first TV action in a conversation. It says how to handle requests and holds this household's preferences.
- The TV reports no playback state: never say something is playing as a fact, and never report a failed call as done."""

EXTRA_TOOLS = [{
    "name": "get_guide",
    "description": "How to handle TV requests with these tools (choosing what to play, follow-ups, which TV, what "
                   "to say about results, when to ask) plus this household's saved notes and preferences. Read it "
                   "once per conversation before the first TV action.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}]


class HubError(Exception):
    def __init__(self, kind: str, why: str, hint: str = "", needs_user: bool = True) -> None:
        super().__init__(why)
        self.kind, self.why, self.hint, self.needs_user = kind, why, hint, needs_user

    def report(self) -> str:
        failed = {"kind": self.kind, "why": self.why, "needs_user": self.needs_user}
        if self.hint:
            failed["hint"] = self.hint
        return json.dumps({"ok": False, "status": "failed", "summary": self.why, "failed": failed})


class Hub:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = dict(os.environ if env is None else env)
        self.url = (env.get("TVREMOTE_HUB_URL") or DEFAULT_URL).rstrip("/")
        self.key = env.get("TVREMOTE_HUB_KEY", "").strip()
        self.key_file = Path(env.get("TVREMOTE_HUB_KEY_FILE") or Path.home() / ".config/tvremote/hub.key").expanduser()
        # A hub on this computer is never behind a proxy, whatever the system proxy settings say.
        local = urllib.parse.urlparse(self.url).hostname in ("127.0.0.1", "localhost", "::1")
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open if local else urllib.request.urlopen

    def _key(self) -> str:
        if self.key:
            return self.key
        try:
            key = self.key_file.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        if not key:
            raise HubError("no_hub_key", f"There is no hub access key at {self.key_file}.",
                           "The hub writes that file when it first starts on this computer. For a hub on another "
                           "computer, copy its key there or set TVREMOTE_HUB_KEY.")
        return key

    def request(self, method: str, path: str, body: Any = None, timeout: float = 15) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._key()}", "Content-Type": "application/json",
            "User-Agent": "tvremote-claude-plugin/" + VERSION})
        try:
            with self._open(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = str(json.loads(e.read()).get("detail", ""))
            except Exception:
                detail = ""
            if e.code == 401:
                raise HubError("hub_rejected_key", "The hub rejected the access key.",
                               f"The key in {self.key_file} is out of date (was it rotated?).") from None
            if e.code in (404, 422):
                raise HubError("bad_request", detail or f"The hub refused the call (HTTP {e.code}).",
                               needs_user=False) from None
            raise HubError("hub_error", f"The hub answered HTTP {e.code}: {detail or e.reason}", needs_user=False) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise HubError("hub_unreachable", f"The TV hub at {self.url} is not answering ({reason}).",
                           "It has to be running on its computer (`tvremote-hub`; on Windows the scheduled task "
                           "\"TV Remote Hub\"). Nothing was sent to the TV.") from None
        except ValueError:
            raise HubError("hub_error", f"{self.url} answered, but not as a TV hub.", needs_user=False) from None


def to_mcp(tool: dict[str, Any]) -> dict[str, Any]:
    return {"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}


class Server:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def tools(self) -> list[dict[str, Any]]:
        try:
            hub_tools = self.hub.request("GET", "/api/tools", timeout=5)["tools"]
        except (HubError, KeyError, TypeError):
            # Hub down: still list the tools, so a call can explain what is wrong.
            hub_tools = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        return [to_mcp(t) for t in hub_tools] + EXTRA_TOOLS

    def guide(self) -> tuple[str, bool]:
        try:
            got = self.hub.request("GET", "/api/tools", timeout=5)
            guide, notes, problem = got.get("guide") or "", got.get("notes") or "", ""
        except HubError as e:
            guide, notes, problem = "", "", f"\n\n## Right now\n\n{e.why} {e.hint}".rstrip()
        guide = guide or GUIDE_SNAPSHOT.read_text(encoding="utf-8")
        notes = notes.strip() or "(none saved yet)"
        return f"{guide.rstrip()}\n\n## This household's notes\n\n{notes}{problem}\n", False

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        if name in ("get_guide", "get_notes"):
            return self.guide()
        try:
            res = self.hub.request("POST", f"/api/tools/{name}", args or {}, timeout=CALL_TIMEOUT)
        except HubError as e:
            return e.report(), True
        return str(res.get("result", res)) if isinstance(res, dict) else str(res), False

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification
            return None
        params = msg.get("params") or {}
        if method == "initialize":
            result: Any = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "tv-remote", "version": VERSION},
                           "instructions": INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.tools()}
        elif method == "tools/call":
            text, err = self.call(str(params.get("name", "")), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": err}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    # MCP is UTF-8 on the wire. Windows would otherwise decode stdin with the
    # system code page and garble any title or search that is not plain ASCII.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    server = Server(Hub())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                out = server.handle(msg) if isinstance(msg, dict) else None
            except Exception as e:  # one bad call must not end the session
                out = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                       "error": {"code": -32603, "message": f"internal error: {type(e).__name__}"}}
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
