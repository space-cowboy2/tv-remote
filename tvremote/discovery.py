"""Find Samsung TVs on the LAN with SSDP, falling back to mDNS (_samsungmsf._tcp).

Not every Samsung answers the Samsung-specific search target: a 2022 Q60B
stays silent for it and only answers as a DIAL device and a media renderer.
So the search asks for all three and recognizes a Samsung by its SERVER
header. And a computer with a VPN sends multicast out of the wrong
network unless told otherwise, so the search goes out of every local address. scan() also reports the other TVs it saw (for example a Sony), so the
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


def local_addresses() -> list[str]:
    """This computer's own IPv4 addresses, one per network it is on."""
    found: set[str] = set()
    try:
        found.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    try:  # the address of the default route; on Linux the host name often resolves to loopback only
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent
            found.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127.") and a != "0.0.0.0")


def _search_from(address: str | None, timeout: float) -> list[tuple[bytes, str]]:
    """Send the searches out of one local address (None: wherever the system sends multicast)
    and collect (response, sender) pairs until the timeout."""
    out: list[tuple[bytes, str]] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if address:
            s.bind((address, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        s.settimeout(timeout)
        for target in SEARCH_TARGETS:
            s.sendto(_search_message(target), SSDP_ADDR)
        try:
            while True:
                data, addr = s.recvfrom(65507)
                out.append((data, addr[0]))
        except socket.timeout:
            pass
    return out


def _ssdp_scan(timeout: float, interface: str | None) -> list[dict[str, str]]:
    """Every device that answered one of SEARCH_TARGETS: host, server, st, location.

    A computer on several networks (a VPN such as Tailscale, a virtual switch)
    sends multicast out of only one of them, and it is often not the one the TV
    is on. So unless one address is given, the search leaves from each of this
    computer's addresses at the same time."""
    from concurrent.futures import ThreadPoolExecutor

    addresses: list[str | None] = [interface] if interface else [None, *local_addresses()]
    with ThreadPoolExecutor(max_workers=len(addresses)) as pool:
        jobs = [pool.submit(_search_from, a, timeout) for a in addresses]
    errors: list[OSError] = []
    found: dict[str, dict[str, str]] = {}
    for job in jobs:
        try:
            answers = job.result()
        except OSError as e:  # one network being down or closed to us must not hide the others
            errors.append(e)
            continue
        for data, sender in answers:
            headers = parse_headers(data)
            host = parse_response(data) or sender
            seen = found.setdefault(host, {"host": host, "server": "", "st": "", "location": ""})
            seen["server"] = seen["server"] or headers.get("server", "")
            seen["location"] = seen["location"] or headers.get("location", "")
            seen["st"] = (seen["st"] + " " + headers.get("st", "")).strip()
    if errors and len(errors) == len(jobs):
        raise errors[0]
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
