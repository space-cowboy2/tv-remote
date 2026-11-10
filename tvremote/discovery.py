"""Find Samsung TVs on the LAN with SSDP, falling back to mDNS (_samsungmsf._tcp).

Not every Samsung answers the Samsung-specific search target: a 2022 Q60B
stays silent for it and only answers as a DIAL device and a media renderer.
So the search asks for all three and recognizes a Samsung by its SERVER
header. scan() also reports the other TVs it saw (for example a Sony), so the
hub can say "that one isn't supported" instead of "there is no other TV".

On macOS, Python needs Local Network permission (System Settings > Privacy &
Security > Local Network). Without it, sending to the network fails with
"No route to host" (errno 65).
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from urllib.parse import urlparse

log = logging.getLogger(__name__)

SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGET = "urn:samsung.com:device:RemoteControlReceiver:1"
DIAL_TARGET = "urn:dial-multiscreen-org:service:dial:1"
RENDERER_TARGET = "urn:schemas-upnp-org:device:MediaRenderer:1"
SEARCH_TARGETS = (SEARCH_TARGET, DIAL_TARGET, RENDERER_TARGET)
MDNS_TYPE = "_samsungmsf._tcp.local."

LOCAL_NETWORK_HINT = (
    "The network blocked discovery. On macOS, allow Local Network access for your "
    "terminal/Python in System Settings > Privacy & Security > Local Network, or "
    "pass the TV's IP directly with --host."
)


class DiscoveryError(Exception):
    pass


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


def parse_headers(data: bytes) -> dict[str, str]:
    """Header names (lower-cased) and values of an SSDP response."""
    headers: dict[str, str] = {}
    for line in data.decode(errors="replace").split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return headers


def _ssdp_scan(timeout: float, interface: str | None) -> list[dict[str, str]]:
    """Every device that answered one of SEARCH_TARGETS: host, server, st, location."""
    found: dict[str, dict[str, str]] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if interface:
            # Pin multicast to the LAN interface (VPNs like Tailscale can steal the route).
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
        s.settimeout(timeout)
        for target in SEARCH_TARGETS:
            s.sendto(_search_message(target), SSDP_ADDR)
        try:
            while True:
                data, addr = s.recvfrom(65507)
                headers = parse_headers(data)
                host = parse_response(data) or addr[0]
                seen = found.setdefault(host, {"host": host, "server": "", "st": "", "location": ""})
                seen["server"] = seen["server"] or headers.get("server", "")
                seen["location"] = seen["location"] or headers.get("location", "")
                seen["st"] = (seen["st"] + " " + headers.get("st", "")).strip()
        except socket.timeout:
            pass
    return list(found.values())


def is_samsung(device: dict[str, str]) -> bool:
    return "samsung" in device.get("server", "").lower() or SEARCH_TARGET in device.get("st", "")


def _ssdp_blocking(timeout: float, interface: str | None) -> list[str]:
    return [d["host"] for d in _ssdp_scan(timeout, interface) if is_samsung(d)]


def _describe(location: str, timeout: float = 2.0) -> dict[str, str]:
    """friendlyName, manufacturer and modelName from a UPnP device description."""
    import urllib.request

    if not location.startswith("http://"):
        return {}
    try:
        with urllib.request.urlopen(location, timeout=timeout) as r:
            text = r.read(200_000).decode("utf-8", "replace")
    except (OSError, ValueError):
        return {}
    out = {}
    for key in ("friendlyName", "manufacturer", "modelName"):
        m = re.search(rf"<{key}>\s*([^<]*?)\s*</{key}>", text)
        if m:
            out[key] = m.group(1)
    return out


def _scan_blocking(timeout: float, interface: str | None) -> list[dict[str, str]]:
    devices = []
    for d in _ssdp_scan(timeout, interface):
        info = _describe(d["location"]) if d["location"] else {}
        devices.append({
            "host": d["host"],
            "kind": "samsung" if is_samsung(d) or "samsung" in info.get("manufacturer", "").lower() else "other",
            "name": info.get("friendlyName", ""),
            "maker": info.get("manufacturer", ""),
            "model": info.get("modelName", ""),
        })
    return devices


def _mdns_blocking(timeout: float) -> list[str]:
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return []

    names: list[str] = []

    class Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            names.append(name)

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    import time

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, MDNS_TYPE, Listener())
        time.sleep(timeout)
        hosts: list[str] = []
        for name in names:
            info = zc.get_service_info(MDNS_TYPE, name, timeout=int(timeout * 1000))
            for addr in (info.parsed_addresses() if info else []):
                if ":" not in addr and addr not in hosts:
                    hosts.append(addr)
        return hosts
    finally:
        zc.close()


def _discover_blocking(timeout: float, interface: str | None) -> list[str]:
    errors: list[OSError] = []
    hosts: list[str] = []
    try:
        hosts = _ssdp_blocking(timeout, interface)
    except OSError as e:
        log.debug("SSDP failed: %s", e)
        errors.append(e)
    if not hosts:
        try:
            hosts = _mdns_blocking(timeout)
        except OSError as e:
            log.debug("mDNS failed: %s", e)
            errors.append(e)
    if not hosts and errors:
        raise DiscoveryError(f"{LOCAL_NETWORK_HINT} ({errors[0]})")
    return hosts


async def discover(timeout: float = 3.0, interface: str | None = None) -> list[str]:
    return await asyncio.to_thread(_discover_blocking, timeout, interface)


async def scan(timeout: float = 3.0, interface: str | None = None) -> list[dict[str, str]]:
    """TVs and other media devices that answered on the LAN, each as
    {host, kind: "samsung" | "other", name, maker, model}. Empty on a blocked network."""
    try:
        return await asyncio.to_thread(_scan_blocking, timeout, interface)
    except OSError as e:
        log.debug("scan failed: %s", e)
        return []
