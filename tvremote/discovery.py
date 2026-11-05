"""Find Samsung TVs on the LAN with SSDP."""

from __future__ import annotations

import asyncio
import re
import socket
from urllib.parse import urlparse

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGET = "urn:samsung.com:device:RemoteControlReceiver:1"


def _search_message(st: str = SEARCH_TARGET, mx: int = 2) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode()


def parse_response(data: bytes) -> str | None:
    """Return the TV's IP from an SSDP response, or None."""
    text = data.decode(errors="replace")
    m = re.search(r"^LOCATION:\s*(\S+)", text, re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    return urlparse(m.group(1)).hostname


def _discover_blocking(timeout: float) -> list[str]:
    found: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(timeout)
        s.sendto(_search_message(), SSDP_ADDR)
        try:
            while True:
                data, addr = s.recvfrom(65507)
                host = parse_response(data) or addr[0]
                if host not in found:
                    found.append(host)
        except socket.timeout:
            pass
    return found


async def discover(timeout: float = 3.0) -> list[str]:
    return await asyncio.to_thread(_discover_blocking, timeout)
