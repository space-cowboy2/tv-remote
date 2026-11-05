"""Saved TV settings (host, MAC, pairing token). The file is readable only by
its owner because the token grants control of the TV."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def default_path() -> Path:
    env = os.environ.get("TVREMOTE_CONFIG")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "tvremote" / "tv.json"


@dataclass
class TVConfig:
    host: str
    mac: str | None = None
    token: str | None = None
    name: str = "TV Remote"
    model: str | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "TVConfig | None":
        p = path or default_path()
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self, path: Path | None = None) -> Path:
        p = path or default_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(tmp, p)
        return p
