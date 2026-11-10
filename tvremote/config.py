"""Saved TV settings (host, MAC, pairing token). The file is readable only by
its owner because the token grants control of the TV.

One TV lives in tv.json (the default TV). Each further TV is paired into its
own tv-<slug>.json next to it, e.g. tv-bedroom.json."""

from __future__ import annotations

import json
import os
import re
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
    mac: str | None = None      # what the TV reports (its Wi-Fi address)
    lan_mac: str | None = None  # the address the hub's network sees; differs for a TV on a cable
    token: str | None = None
    name: str = "TV Remote"
    model: str | None = None
    label: str | None = None  # what people call this TV, e.g. "Bedroom"

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


def slugify(label: str) -> str:
    """"Living Room TV" -> "living-room-tv" (safe in a file name and a tool argument)."""
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "tv"


def extra_path(label: str, path: Path | None = None) -> Path:
    """Where a second (third, ...) TV is saved."""
    return (path or default_path()).with_name(f"tv-{slugify(label)}.json")


def load_all(path: Path | None = None) -> list[tuple[str, TVConfig, Path]]:
    """Every paired TV as (id, config, its file). The TV in tv.json comes first
    and is the default."""
    p = path or default_path()
    found: list[tuple[str, TVConfig, Path]] = []
    try:
        primary = TVConfig.load(p)
    except (ValueError, TypeError):
        primary = None
    if primary:
        found.append((slugify(primary.label or "tv"), primary, p))
    for extra in sorted(p.parent.glob("tv-*.json")):
        try:
            cfg = TVConfig.load(extra)
        except (ValueError, TypeError):
            continue
        slug = extra.stem[3:]
        if cfg and slug and slug not in {s for s, _, _ in found}:
            found.append((slug, cfg, extra))
    return found
