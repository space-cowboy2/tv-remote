"""Samsung Tizen remote key codes, grouped for UIs and validation."""

from __future__ import annotations

KEY_GROUPS: dict[str, list[str]] = {
    "power": ["KEY_POWER"],
    "navigation": [
        "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_ENTER",
        "KEY_RETURN", "KEY_HOME", "KEY_MENU", "KEY_EXIT", "KEY_INFO",
        "KEY_TOOLS", "KEY_GUIDE", "KEY_SOURCE",
    ],
    "volume": ["KEY_VOLUP", "KEY_VOLDOWN", "KEY_MUTE"],
    "channel": ["KEY_CHUP", "KEY_CHDOWN", "KEY_PRECH", "KEY_CH_LIST"],
    "playback": [
        "KEY_PLAY", "KEY_PAUSE", "KEY_STOP", "KEY_REWIND", "KEY_FF",
        "KEY_REC",
    ],
    "numbers": [f"KEY_{n}" for n in range(10)],
    "color": ["KEY_RED", "KEY_GREEN", "KEY_YELLOW", "KEY_CYAN"],
    "input": ["KEY_HDMI", "KEY_HDMI1", "KEY_HDMI2", "KEY_HDMI3", "KEY_HDMI4", "KEY_TV"],
    "other": ["KEY_CAPTION", "KEY_PICTURE_SIZE", "KEY_AMBIENT"],
}

ALL_KEYS: frozenset[str] = frozenset(k for group in KEY_GROUPS.values() for k in group)


def normalize_key(key: str) -> str:
    """Accept 'volup', 'VOLUP' or 'KEY_VOLUP' and return 'KEY_VOLUP'."""
    k = key.strip().upper()
    if not k.startswith("KEY_"):
        k = "KEY_" + k
    return k


def is_valid_key(key: str) -> bool:
    """True for known keys. Unknown KEY_* names are allowed by the client but
    flagged here so callers (like the hub API) can reject typos."""
    return normalize_key(key) in ALL_KEYS
