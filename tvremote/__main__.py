"""Command-line tool for testing the TV connection.

  python -m tvremote discover
  python -m tvremote pair --host 192.168.1.50
  python -m tvremote key volup
  python -m tvremote text "stranger things"
  python -m tvremote apps
  python -m tvremote launch 3201907018807      # Netflix
  python -m tvremote on | off | status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import TVConfig, default_path
from .discovery import DiscoveryError, discover
from .keys import is_valid_key, normalize_key
from .samsung import SamsungTV, TVError


def _tv_from_config(cfg: TVConfig) -> SamsungTV:
    def save_token(token: str) -> None:
        cfg.token = token
        cfg.save()

    return SamsungTV(cfg.host, name=cfg.name, token=cfg.token, mac=cfg.mac, on_token=save_token)


def _require_config() -> TVConfig:
    cfg = TVConfig.load()
    if not cfg:
        sys.exit(f"Not paired yet. Run: python -m tvremote pair --host <TV IP>  (config: {default_path()})")
    return cfg


async def run(args: argparse.Namespace) -> int:
    if args.cmd == "discover":
        try:
            hosts = await discover(args.timeout, args.interface)
        except DiscoveryError as e:
            print(e, file=sys.stderr)
            return 2
        print("\n".join(hosts) if hosts else "No Samsung TVs found. Is this computer on the same Wi-Fi?")
        return 0 if hosts else 1

    if args.cmd == "pair":
        cfg = TVConfig(host=args.host, name=args.name)
        tv = _tv_from_config(cfg)
        info = await tv.info()
        device = info.get("device") or {}
        cfg.mac = device.get("wifiMac")
        model = device.get("modelName")
        # Some 2024 models report modelName "0"; fall back to model/name.
        if not model or model == "0":
            model = device.get("model") or device.get("name")
        cfg.model = model
        print(f"Found {SamsungTV.clean_name(device.get('name'))} ({cfg.model}). Press Allow on the TV within 60 seconds...")
        tv.pair_timeout = 60
        await tv.connect()
        await tv.close()
        path = cfg.save()
        print(f"Paired. Saved to {path}")
        return 0

    cfg = _require_config()
    tv = _tv_from_config(cfg)
    try:
        if args.cmd == "status":
            print(json.dumps({"power": await tv.power_state(), **cfg.__dict__, "token": "***" if cfg.token else None}, indent=2))
        elif args.cmd == "key":
            for k in args.keys:
                if not is_valid_key(k):
                    print(f"warning: {normalize_key(k)} is not a known key, sending anyway", file=sys.stderr)
                await tv.send_key(k)
                await asyncio.sleep(0.15)
        elif args.cmd == "text":
            await tv.send_text(args.text, submit=args.submit)
        elif args.cmd == "apps":
            for app in sorted(await tv.list_apps(), key=lambda a: a["name"] or ""):
                print(f"{app['id']:<24} {app['name']}")
        elif args.cmd == "launch":
            await tv.launch_app(args.app_id)
        elif args.cmd == "on":
            ok = await tv.power_on()
            print("TV is on" if ok else "TV did not wake. Check 'Power On with Mobile' is enabled.")
            return 0 if ok else 1
        elif args.cmd == "off":
            await tv.power_off()
    finally:
        await tv.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tvremote", description="Control a Samsung TV on your network.")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="find TVs on the network")
    d.add_argument("--timeout", type=float, default=3.0)
    d.add_argument("--interface", help="this computer's LAN IP, to avoid VPN routes (e.g. Tailscale)")
    pr = sub.add_parser("pair", help="pair with a TV (press Allow on the TV)")
    pr.add_argument("--host", required=True)
    pr.add_argument("--name", default="TV Remote")
    sub.add_parser("status", help="show power state and saved settings")
    k = sub.add_parser("key", help="press one or more keys, e.g. volup home enter")
    k.add_argument("keys", nargs="+")
    t = sub.add_parser("text", help="type into the on-screen keyboard")
    t.add_argument("text")
    t.add_argument("--submit", action="store_true")
    sub.add_parser("apps", help="list installed apps")
    la = sub.add_parser("launch", help="open an app by id")
    la.add_argument("app_id")
    sub.add_parser("on", help="turn the TV on")
    sub.add_parser("off", help="turn the TV off")
    args = p.parse_args(argv)
    try:
        sys.exit(asyncio.run(run(args)))
    except TVError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
