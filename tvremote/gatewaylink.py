"""Point a duet-gateway at this hub's MCP server.

duet-gateway (github.com/space-cowboy2/duet-gateway) starts the TV's MCP server
itself, from the "tv" entry under "upstreams" in its config.json. This sets
that entry to this installation: the Python that runs the hub, by full path,
and the given tv_mcp.py. Everything else in the file is left as it is, and the
file is never printed: it holds the gateway's secrets.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


class LinkError(Exception):
    pass


def console_python(executable: str | None = None) -> str:
    """The interpreter with a console: an MCP server talks over stdin and stdout, which pythonw.exe does not have."""
    exe = str(executable or sys.executable)
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    return exe


def link(config: Path, plugin: Path, python: str | None = None) -> dict[str, Any]:
    """Set the gateway's "tv" upstream. Returns {"changed": bool, "added": bool, "was_off": bool}."""
    if not plugin.is_file():
        raise LinkError(f"{plugin} is not there.")
    try:
        raw = json.loads(config.read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise LinkError(f"{config} cannot be read ({e.strerror or type(e).__name__}).") from None
    except ValueError:
        raise LinkError(f"{config} is not valid JSON; nothing was changed.") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("upstreams", []), list):
        raise LinkError(f"{config} does not look like a duet-gateway config; nothing was changed.")
    ups = raw.setdefault("upstreams", [])
    entry = next((u for u in ups if isinstance(u, dict) and u.get("prefix") == "tv"), None)
    added = entry is None
    if entry is None:
        entry = {"prefix": "tv"}
        ups.insert(0, entry)
    want = {"command": python or console_python(), "args": [str(plugin)]}
    was_off = entry.get("enabled") is False
    changed = added or was_off or any(entry.get(k) != v for k, v in want.items())
    if changed:
        entry.update(want)
        entry.pop("enabled", None)
        tmp = config.with_name(config.name + ".tmp")
        try:
            tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, config)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise LinkError(f"{config} cannot be written ({e.strerror or type(e).__name__}).") from None
    return {"changed": changed, "added": added, "was_off": was_off}
