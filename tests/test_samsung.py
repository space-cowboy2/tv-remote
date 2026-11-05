import asyncio
import base64
import json
import os

import pytest

from tvremote import PairingDenied, PairingTimeout, SamsungTV, TVUnreachable
from tvremote.config import TVConfig
from tvremote.discovery import parse_response
from tvremote.fake import FakeTV
from tvremote.keys import is_valid_key, normalize_key
from tvremote.wol import magic_packet


@pytest.fixture
async def fake():
    tv = await FakeTV().start()
    yield tv
    await tv.stop()


def client(fake: FakeTV, **kw) -> SamsungTV:
    return SamsungTV("127.0.0.1", port=fake.ws_port, info_port=fake.info_port, secure=False, **kw)


async def test_first_pairing_saves_token(fake):
    saved = []
    async with client(fake, on_token=saved.append) as tv:
        assert tv.token == "fake-token-123"
    assert saved == ["fake-token-123"]
    assert fake.clients == ["TV Remote"]


async def test_reuses_token_without_reprompt(fake):
    fake.pairing = "deny"  # any unauthenticated attempt would be denied
    saved = []
    async with client(fake, token="fake-token-123", on_token=saved.append):
        pass
    assert saved == []


@pytest.mark.parametrize("mode,exc", [("deny", PairingDenied), ("timeout", PairingTimeout)])
async def test_pairing_failures(fake, mode, exc):
    fake.pairing = mode
    with pytest.raises(exc):
        await client(fake).connect()


async def test_pairing_no_answer_times_out(fake):
    fake.pairing = "silent"
    with pytest.raises(PairingTimeout):
        await client(fake, pair_timeout=0.3).connect()


async def test_unreachable():
    tv = SamsungTV("127.0.0.1", port=1, secure=False, request_timeout=0.5)
    with pytest.raises(TVUnreachable):
        await tv.connect()


async def test_send_keys(fake):
    async with client(fake) as tv:
        await tv.send_key("volup")
        await tv.send_key("KEY_HOME")
        await asyncio.sleep(0.05)
    assert fake.keys == ["KEY_VOLUP", "KEY_HOME"]


async def test_hold_key(fake):
    async with client(fake) as tv:
        await tv.hold_key("KEY_VOLUP", 0.05)
        await asyncio.sleep(0.05)
    cmds = [m["params"]["Cmd"] for m in fake.received]
    assert cmds == ["Press", "Release"]


async def test_send_text(fake):
    async with client(fake) as tv:
        await tv.send_text("héllo", submit=True)
        await asyncio.sleep(0.05)
    first, second = fake.received
    assert base64.b64decode(first["params"]["Cmd"]).decode() == "héllo"
    assert second["params"]["TypeOfRemote"] == "SendInputEnd"


async def test_list_and_launch_apps(fake):
    async with client(fake) as tv:
        apps = await tv.list_apps()
        assert {a["name"] for a in apps} == {"Netflix", "YouTube"}
        await tv.launch_app("3201907018807")
        await asyncio.sleep(0.05)
    assert fake.launched == ["3201907018807"]


async def test_reconnects_after_drop(fake):
    async with client(fake) as tv:
        await tv.send_key("KEY_1")
        await asyncio.sleep(0.05)
        await fake.drop_connections()
        await asyncio.sleep(0.1)
        await tv.send_key("KEY_2")
        await asyncio.sleep(0.05)
    assert fake.keys == ["KEY_1", "KEY_2"]


async def test_power_state_and_off(fake):
    tv = client(fake)
    assert await tv.power_state() == "on"
    assert await tv.mac_address() == "AA:BB:CC:DD:EE:FF"
    await tv.power_off()
    await asyncio.sleep(0.05)
    assert fake.power == "standby"


async def test_power_on_from_standby(fake):
    fake.power = "standby"
    tv = client(fake, mac=fake.mac)
    assert await tv.power_on(wait=3)
    await tv.close()


async def test_power_state_off_when_unreachable(fake):
    fake.power = "off"
    assert await client(fake).power_state() == "off"


def test_keys():
    assert normalize_key("volup") == "KEY_VOLUP"
    assert is_valid_key("key_home")
    assert not is_valid_key("KEY_NOPE")


def test_magic_packet():
    pkt = magic_packet("AA:BB:CC:DD:EE:FF")
    assert len(pkt) == 102 and pkt[:6] == b"\xff" * 6
    with pytest.raises(ValueError):
        magic_packet("nope")


def test_ssdp_parse():
    resp = b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.50:9197/dmr\r\nST: x\r\n\r\n"
    assert parse_response(resp) == "192.168.1.50"
    assert parse_response(b"HTTP/1.1 200 OK\r\n\r\n") is None


def test_config_roundtrip_is_private(tmp_path):
    p = tmp_path / "tv.json"
    TVConfig(host="1.2.3.4", token="secret", mac="AA:BB:CC:DD:EE:FF").save(p)
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    cfg = TVConfig.load(p)
    assert cfg and cfg.token == "secret"
    assert json.loads(p.read_text())["host"] == "1.2.3.4"


def test_discovery_blocked_network_gives_hint(monkeypatch):
    from tvremote import discovery

    def boom(*a, **k):
        raise OSError(65, "No route to host")

    monkeypatch.setattr(discovery, "_ssdp_blocking", boom)
    monkeypatch.setattr(discovery, "_mdns_blocking", boom)
    with pytest.raises(discovery.DiscoveryError, match="Local Network"):
        discovery._discover_blocking(0.1, None)


def test_discovery_falls_back_to_mdns(monkeypatch):
    from tvremote import discovery

    monkeypatch.setattr(discovery, "_ssdp_blocking", lambda *a: [])
    monkeypatch.setattr(discovery, "_mdns_blocking", lambda *a: ["192.168.1.9"])
    assert discovery._discover_blocking(0.1, None) == ["192.168.1.9"]


def test_pair_uses_model_when_modelname_is_zero(monkeypatch, tmp_path):
    import asyncio as aio
    from tvremote import __main__ as cli

    monkeypatch.setenv("TVREMOTE_CONFIG", str(tmp_path / "tv.json"))

    async def info(self):
        return {"device": {"modelName": "0", "model": "24_PTM_QD_T09", "wifiMac": "11:22:33:44:55:66"}}

    async def noop(self):
        return None

    monkeypatch.setattr(SamsungTV, "info", info)
    monkeypatch.setattr(SamsungTV, "connect", noop)
    monkeypatch.setattr(SamsungTV, "close", noop)
    args = cli.argparse.Namespace(cmd="pair", host="10.0.0.2", name="TV Remote")
    assert aio.run(cli.run(args)) == 0
    cfg = TVConfig.load(tmp_path / "tv.json")
    assert cfg.model == "24_PTM_QD_T09" and cfg.mac == "11:22:33:44:55:66"


def test_clean_name_unescapes_twice():
    assert SamsungTV.clean_name("[TV] 42&amp;quot; OLED") == '[TV] 42" OLED'
    assert SamsungTV.clean_name(None) == "TV"


async def test_apps_fallback_for_2024_firmware(fake):
    fake.answers_installed_apps = False
    fake.apps.append({"appId": "3201910019365", "name": "Prime Video", "app_type": 2})
    fake.apps.append({"appId": "11101200001", "name": "Netflix", "app_type": 2})  # duplicate old ID
    async with client(fake) as tv:
        apps = await tv.list_apps()
    assert sorted(a["name"] for a in apps) == ["Netflix", "Prime Video", "YouTube"]


async def test_launch_missing_app_errors(fake):
    from tvremote import TVError
    async with client(fake) as tv:
        with pytest.raises(TVError, match="not installed"):
            await tv.launch_app("999")
