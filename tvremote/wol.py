"""Wake-on-LAN, used to turn the TV on when it is off.

The TV must have "Power On with Mobile" (Settings > General > Network >
Expert Settings) enabled, and must be on the same LAN as the sender.

Two things a real home network taught this module:
- A TV on a network cable only wakes for its cable address, and the TV itself
  reports just its Wi-Fi address. lan_mac() reads the address this computer
  actually talks to (its neighbour table), which is right either way.
- A computer on several networks (a VPN such as Tailscale) sends a broadcast
  out of only one of them. send_wol() sends from every local address.
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}$")
_MAC_IN_TEXT = re.compile(r"(?<![0-9A-Fa-f:-])((?:[0-9A-Fa-f]{1,2}[:-]){5}[0-9A-Fa-f]{1,2})(?![0-9A-Fa-f:-])")


def magic_packet(mac: str) -> bytes:
    if not _MAC_RE.match(mac):
        raise ValueError(f"invalid MAC address: {mac!r}")
    raw = bytes.fromhex(re.sub(r"[:-]", "", mac))
    return b"\xff" * 6 + raw * 16


def _local_addresses() -> list[str]:
    from .discovery import local_addresses
    return local_addresses()


def send_wol(mac: str, broadcast: str = "255.255.255.255", port: int = 9, repeat: int = 3) -> None:
    packet = magic_packet(mac)
    sent, error = 0, None
    for source in [None, *_local_addresses()]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                if source:
                    s.bind((source, 0))
                for _ in range(repeat):
                    s.sendto(packet, (broadcast, port))
                sent += 1
        except OSError as e:  # one network being down must not stop the others
            error = e
    if not sent and error:
        raise error


def parse_neighbours(text: str, host: str) -> str | None:
    """The hardware address listed for `host` in the output of `arp` or in /proc/net/arp, as aa:bb:cc:dd:ee:ff."""
    for line in text.splitlines():
        if not re.search(rf"(?<![\d.]){re.escape(host)}(?![\d.])", line):
            continue
        m = _MAC_IN_TEXT.search(line)
        if not m:
            continue
        mac = ":".join(part.zfill(2) for part in re.split(r"[:-]", m.group(1).lower()))
        if mac not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
            return mac
    return None


def lan_mac(host: str) -> str | None:
    """The hardware address this computer has for `host` right now, or None.

    Only meaningful just after talking to the host (the entry expires within
    minutes) and only for a host on the same network."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/net/arp", encoding="ascii", errors="replace") as f:
                return parse_neighbours(f.read(), host)
        args = ["arp", "-a", host] if sys.platform == "win32" else ["arp", "-n", host]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash under pythonw
        out = subprocess.run(args, capture_output=True, timeout=4, creationflags=flags).stdout
        return parse_neighbours(out.decode("ascii", "replace"), host)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
