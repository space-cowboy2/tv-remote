#!/usr/bin/env python3
"""MCP server (stdio) that lets Claude Code control the TV through the hub.

It exposes the hub's own TV tools (power, keys, text, apps, YouTube, Twitch,
macros, notes), so Claude Code runs on your Claude subscription and the hub
needs no Anthropic API key. Standard library only.

Config (environment):
  TVREMOTE_HUB_URL       default https://dannys-mac-mini.tail24f732.ts.net
  TVREMOTE_HUB_KEY       the hub access key, or
  TVREMOTE_HUB_KEY_FILE  file holding it (default ~/.config/tvremote/hub.key)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://dannys-mac-mini.tail24f732.ts.net"
SNAPSHOT = Path(__file__).with_name("tools.json")
PROTOCOL = "2025-06-18"
VERSION = "0.1.0"

INSTRUCTIONS = """Controls Danny's Samsung TV (2024 Tizen OLED) through his hub.
- The hub can't see the screen: tools report what was sent, not what the TV shows. Say so when it matters.
- Call get_notes first in a session: it holds what's been learned about this TV and Danny's preferences.
- YouTube: always play_youtube (native in-app search). Never cast. For a vague ask ("play some jazz"),
  search_youtube first, pick one long mix or live stream, and play_youtube its id. Twitch: open_twitch (uncalibrated).
- If the TV is off and the request needs it on, set_power on first.
- When Danny says what the screen actually did, fix the macro with save_macro and remember lasting facts."""

EXTRA_TOOLS = [{
    "name": "get_notes",
    "description": "What has been learned about this TV and the owner's preferences (notes.md on the hub). "
                   "Read this before acting on anything beyond a single key press.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}]


class HubError(Exception):
    pass


class Hub:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        env = dict(os.environ if env is None else env)
        self.url = env.get("TVREMOTE_HUB_URL", DEFAULT_URL).rstrip("/")
        self.key = env.get("TVREMOTE_HUB_KEY", "").strip()
        self.key_file = Path(env.get("TVREMOTE_HUB_KEY_FILE") or Path.home() / ".config/tvremote/hub.key").expanduser()

    def _key(self) -> str:
        if self.key:
            return self.key
        try:
            key = self.key_file.read_text().strip()
        except OSError:
            key = ""
        if not key:
            raise HubError(f"No hub key. Put it in {self.key_file} (chmod 600) or set TVREMOTE_HUB_KEY.")
        return key

    def request(self, method: str, path: str, body: Any = None, timeout: float = 15) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._key()}", "Content-Type": "application/json",
            "User-Agent": "tvremote-claude-plugin/" + VERSION})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", "")
            except Exception:
                detail = ""
            hints = {401: "hub rejected the key (was it rotated?)", 503: "TV is off or unreachable",
                     504: "TV did not wake (enable Power On with Mobile)"}
            raise HubError(f"hub error {e.code}: {detail or hints.get(e.code, e.reason)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise HubError(f"can't reach the hub at {self.url} ({reason}). Is Tailscale on?") from None


def to_mcp(tool: dict[str, Any]) -> dict[str, Any]:
    return {"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}


class Server:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    def tools(self) -> list[dict[str, Any]]:
        try:
            hub_tools = self.hub.request("GET", "/api/tools")["tools"]
        except (HubError, KeyError, TypeError):
            hub_tools = json.loads(SNAPSHOT.read_text())  # hub down: still list tools, calls will explain
        return [to_mcp(t) for t in hub_tools] + EXTRA_TOOLS

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            if name == "get_notes":
                return self.hub.request("GET", "/api/tools").get("notes") or "(no notes yet)", False
            # Macros and play_youtube take ~25 s; give long sequences room.
            res = self.hub.request("POST", f"/api/tools/{name}", args or {}, timeout=180)
            return str(res.get("result", res)), False
        except HubError as e:
            return str(e), True

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
            text, err = self.call(params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": err}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
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
            out = server.handle(msg) if isinstance(msg, dict) else None
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
