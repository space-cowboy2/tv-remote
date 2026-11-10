"""Volume and mute through the TV's UPnP media renderer (port 9197 on Samsung).

The remote-control channel can only press Vol+/Vol-/Mute and never says what
the level is. The renderer's RenderingControl service does: GetVolume,
SetVolume (0-100), GetMute, SetMute. That gives absolute volume ("set it to
15") and a read-back to confirm a change. It needs no pairing and is LAN-only.

Checked on a 2022 Q60B (GetVolume/GetMute answer; SetVolume range 0-100).
TVs without the service simply report it as unavailable.
"""

from __future__ import annotations

import http.client
import re
from xml.sax.saxutils import escape

SERVICE = "urn:schemas-upnp-org:service:RenderingControl:1"
DEFAULT_CONTROL = "/upnp/control/RenderingControl1"

_SERVICE_RE = re.compile(
    r"<service>.*?<serviceType>\s*" + re.escape(SERVICE) + r"\s*</serviceType>.*?<controlURL>\s*([^<\s]+)\s*</controlURL>",
    re.DOTALL)


class UPnPError(Exception):
    """The renderer is missing, unreachable, or refused the action."""


def find_control_url(description: str) -> str | None:
    """controlURL of RenderingControl in a device description, or None."""
    m = _SERVICE_RE.search(description)
    if not m:
        return None
    url = m.group(1)
    return url if url.startswith("/") else "/" + url


def envelope(action: str, args: dict[str, object]) -> bytes:
    body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{SERVICE}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()


def read_value(xml: str, name: str) -> str | None:
    m = re.search(rf"<{name}(?:\s[^>]*)?>\s*([^<]*?)\s*</{name}>", xml)
    return m.group(1) if m else None


class Renderer:
    """Blocking client; call it through asyncio.to_thread."""

    def __init__(self, host: str, port: int = 9197, timeout: float = 3.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self._control: str | None = None

    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, str]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        except (OSError, http.client.HTTPException) as e:
            raise UPnPError(f"renderer at {self.host}:{self.port} did not answer: {e}") from e
        finally:
            conn.close()

    def control_url(self) -> str:
        if self._control is None:
            status, text = self._request("GET", "/dmr")
            if status != 200:
                raise UPnPError(f"no media renderer description (HTTP {status})")
            self._control = find_control_url(text) or DEFAULT_CONTROL
        return self._control

    def call(self, action: str, **args: object) -> str:
        status, text = self._request("POST", self.control_url(), envelope(action, {"InstanceID": 0, **args}), {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{SERVICE}#{action}"',
        })
        if status != 200:
            raise UPnPError(f"{action} was refused (HTTP {status})")
        return text

    def get_volume(self) -> int:
        value = read_value(self.call("GetVolume", Channel="Master"), "CurrentVolume")
        if value is None or not value.lstrip("-").isdigit():
            raise UPnPError("GetVolume gave no level")
        return int(value)

    def set_volume(self, level: int) -> None:
        self.call("SetVolume", Channel="Master", DesiredVolume=max(0, min(100, int(level))))

    def get_mute(self) -> bool:
        value = read_value(self.call("GetMute", Channel="Master"), "CurrentMute")
        if value is None:
            raise UPnPError("GetMute gave no state")
        return value.strip().lower() in ("1", "true", "yes")

    def set_mute(self, muted: bool) -> None:
        self.call("SetMute", Channel="Master", DesiredMute=1 if muted else 0)
