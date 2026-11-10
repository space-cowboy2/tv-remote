"""Finding TVs on the network (built responses, no multicast)."""

from tvremote import discovery as d

Q60B = (b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nLOCATION: http://10.0.0.16:9197/dmr\r\n"
        b"SERVER: Samsung-Linux/4.1, UPnP/1.0, Samsung_UPnP_SDK/1.0\r\n"
        b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n")
SONY = (b"HTTP/1.1 200 OK\r\nLOCATION: http://10.0.0.10:52323/dmr.xml\r\n"
        b"SERVER: Linux/3.10 UPnP/1.0 Sony-BDP/2.0\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n")


def test_headers_and_who_is_a_samsung():
    q, s = d.parse_headers(Q60B), d.parse_headers(SONY)
    assert q["location"] == "http://10.0.0.16:9197/dmr" and "Samsung" in q["server"]
    assert d.is_samsung({"server": q["server"], "st": q["st"]})          # by its SERVER header alone
    assert d.is_samsung({"server": "", "st": d.SEARCH_TARGET})          # or by answering the Samsung search
    assert not d.is_samsung({"server": s["server"], "st": s["st"]})
    assert d.parse_response(Q60B) == "10.0.0.16"


def test_search_asks_for_more_than_the_samsung_target():
    """A 2022 Q60B never answers the Samsung-specific search; it does answer these."""
    assert d.DIAL_TARGET in d.SEARCH_TARGETS and d.RENDERER_TARGET in d.SEARCH_TARGETS
    assert b"ST: " + d.DIAL_TARGET.encode() in d._search_message(d.DIAL_TARGET)


async def test_scan_labels_each_device(monkeypatch):
    monkeypatch.setattr(d, "_ssdp_scan", lambda timeout, interface: [
        {"host": "10.0.0.16", "server": "Samsung-Linux/4.1", "st": d.RENDERER_TARGET, "location": "http://10.0.0.16:9197/dmr"},
        {"host": "10.0.0.10", "server": "Linux Sony-BDP/2.0", "st": d.RENDERER_TARGET, "location": "http://10.0.0.10:52323/dmr.xml"},
        {"host": "10.0.0.30", "server": "", "st": d.DIAL_TARGET, "location": ""},
    ])
    described = {"http://10.0.0.16:9197/dmr": {"friendlyName": "Samsung Q60BA 55", "manufacturer": "Samsung Electronics", "modelName": "QN55Q60BAFXZA"},
                 "http://10.0.0.10:52323/dmr.xml": {"friendlyName": "Media Room TV", "manufacturer": "Sony Corporation", "modelName": "XBR-75X900F"}}
    monkeypatch.setattr(d, "_describe", lambda location, timeout=2.0: described.get(location, {}))
    found = await d.scan(0.1)
    assert found[0] == {"host": "10.0.0.16", "kind": "samsung", "name": "Samsung Q60BA 55",
                        "maker": "Samsung Electronics", "model": "QN55Q60BAFXZA"}
    assert found[1]["kind"] == "other" and found[1]["name"] == "Media Room TV"
    assert found[2] == {"host": "10.0.0.30", "kind": "other", "name": "", "maker": "", "model": ""}
    monkeypatch.setattr(d, "_ssdp_blocking", lambda timeout, interface: ["10.0.0.16"])
    assert await d.discover(0.1) == ["10.0.0.16"]


async def test_scan_on_a_blocked_network_is_empty_not_an_error(monkeypatch):
    def blocked(timeout, interface):
        raise OSError(65, "No route to host")
    monkeypatch.setattr(d, "_ssdp_scan", blocked)
    assert await d.scan(0.1) == []


def test_the_search_leaves_from_every_local_address(monkeypatch):
    """Seen live on a PC with Tailscale: the system's own choice of network found nothing,
    the search pinned to the home network's address found both TVs."""
    asked = []

    def search_from(address, timeout):
        asked.append(address)
        if address == "100.111.6.22":
            raise OSError(10051, "network unreachable")   # one network failing does not hide the others
        return [(Q60B, "10.0.0.16"), (SONY, "10.0.0.10")] if address == "10.0.0.18" else []

    monkeypatch.setattr(d, "local_addresses", lambda: ["10.0.0.18", "100.111.6.22"])
    monkeypatch.setattr(d, "_search_from", search_from)
    found = d._ssdp_scan(0.1, None)
    assert sorted(asked, key=str) == ["10.0.0.18", "100.111.6.22", None]
    assert [x["host"] for x in found] == ["10.0.0.16", "10.0.0.10"]
    assert d._ssdp_blocking(0.1, None) == ["10.0.0.16"]
    # one address given: only that one is used
    asked.clear()
    d._ssdp_scan(0.1, "10.0.0.18")
    assert asked == ["10.0.0.18"]


def test_a_search_that_fails_everywhere_is_an_error(monkeypatch):
    import pytest

    def blocked(address, timeout):
        raise OSError(65, "No route to host")
    monkeypatch.setattr(d, "local_addresses", lambda: ["10.0.0.18"])
    monkeypatch.setattr(d, "_search_from", blocked)
    with pytest.raises(OSError):
        d._ssdp_scan(0.1, None)


def test_local_addresses_and_a_real_search_do_not_raise():
    addresses = d.local_addresses()
    assert all(not a.startswith("127.") for a in addresses)
    assert isinstance(d._search_from(None, 0.05), list)   # nothing answers in a test run; it must just come back
