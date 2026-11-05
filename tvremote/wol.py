"""Wake-on-LAN, used to turn the TV on from standby.

The TV must have "Power On with Mobile" (Settings > General > Network >
Expert Settings) enabled, and must be on the same LAN as the sender.
"""

from __future__ import annotations

import re
import socket

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}$")


def magic_packet(mac: str) -> bytes:
    if not _MAC_RE.match(mac):
        raise ValueError(f"invalid MAC address: {mac!r}")
    raw = bytes.fromhex(re.sub(r"[:-]", "", mac))
    return b"\xff" * 6 + raw * 16


def send_wol(mac: str, broadcast: str = "255.255.255.255", port: int = 9, repeat: int = 3) -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeat):
            s.sendto(packet, (broadcast, port))
