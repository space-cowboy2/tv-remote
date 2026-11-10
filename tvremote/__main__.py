"""Command-line tool for testing the TV connection.

  python -m tvremote discover
  python -m tvremote pair --host 192.168.1.50
  python -m tvremote pair --host 192.168.1.51 --label Bedroom   # a second TV
  python -m tvremote selftest                  # what does this TV answer? (read-only)
  python -m tvremote selftest --full           # also plays something and checks every step
  python -m tvremote tool play '{"query": "jazz music", "prefer": "long"}'   # any agent tool
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

from .config import TVConfig, default_path, extra_path, load_all
from .discovery import DiscoveryError, discover, scan
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
        devices = await scan(args.timeout, args.interface)
        for d in devices:
            what = "Samsung TV" if d["kind"] == "samsung" else "other device (not controllable)"
            print(f"{d['host']:<16} {what:<32} {d['name'] or '?'}  {d['model']}")
        if any(d["kind"] == "samsung" for d in devices):
            return 0
        try:
            hosts = await discover(args.timeout, args.interface)
        except DiscoveryError as e:
            print(e, file=sys.stderr)
            return 2
        print("\n".join(hosts) if hosts else "No Samsung TVs found. Is this computer on the same network?")
        return 0 if hosts else 1

    if args.cmd == "pair":
        main_path = default_path()
        current = TVConfig.load(main_path)
        path = main_path
        if current and current.host != args.host and not args.default:
            if not args.label:
                sys.exit(f"{main_path} already holds {current.label or current.model or current.host}. "
                         "Add --label <name> to pair this one as another TV, or --default to replace it.")
            path = extra_path(args.label, main_path)
        cfg = TVConfig(host=args.host, name=args.name, label=args.label or (current.label if current and path == main_path else None))
        tv = SamsungTV(cfg.host, name=cfg.name, on_token=lambda t: setattr(cfg, "token", t))
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
        cfg.label = cfg.label or SamsungTV.clean_name(device.get("name"))
        print(f"Paired. Saved to {cfg.save(path)}. A running hub picks it up by itself.")
        return 0

    if args.cmd in ("selftest", "tool"):
        from .selftest import full_run, probe, render
        from .tools import Store, Tools, load_handles

        tools = Tools(load_handles(default_path()), store=Store(default_path().parent), config_path=default_path())
        try:
            if args.cmd == "tool":
                try:
                    out = await tools.call(args.name, json.loads(args.args))
                except ValueError as e:
                    sys.exit(f"bad arguments: {e}")
                print(out)
                return 0 if out.ok else 1
            if not tools.tvs:
                sys.exit(f"Not paired yet. Run: python -m tvremote pair --host <TV IP>  (config: {default_path()})")
            failed = False
            for h in tools.tvs.values():
                if args.tv and args.tv not in (h.slug, h.label):
                    continue
                checks = await probe(h)
                print(render(f"LIVE read-only check of {h.title} at {h.tv.host}", checks))
                failed = failed or any(c["ok"] is False for c in checks)
                if args.full:
                    print(f"\nLIVE full run on {h.title} (watch the screen: the hub cannot)")
                    for s in await full_run(tools, h, args.query, args.prefer):
                        print(f"\n== {s['step']}: {s['tool']} {json.dumps(s['args'])}\n{json.dumps(s['report'], indent=1, ensure_ascii=False)}")
                        failed = failed or s["report"].get("ok") is False
            return 1 if failed else 0
        finally:
            await tools.stop()
            for h in tools.tvs.values():
                await h.tv.close()

    if args.cmd == "hub-key":
        key_path = default_path().parent / "hub.key"
        if args.rotate:
            key_path.unlink(missing_ok=True)
        from .hub import load_or_create_key
        load_or_create_key(default_path())
        print(f"Access key file: {key_path}")
        if args.rotate:
            print("New key created. Restart the hub; every device must sign in again.")
        return 0

    cfg = _require_config()
    tv = _tv_from_config(cfg)
    try:
        if args.cmd == "status":
            print(json.dumps({"power": await tv.power_state(), **cfg.__dict__, "token": "***" if cfg.token else None,
                              "other_tvs": [slug for slug, _, _ in load_all()][1:]}, indent=2))
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
    pr.add_argument("--name", default="TV Remote", help="how this hub appears in the TV's device list")
    pr.add_argument("--label", help="what people call this TV, e.g. Bedroom (needed for a second TV)")
    pr.add_argument("--default", action="store_true", help="replace the current default TV with this one")
    st = sub.add_parser("selftest", help="check what the real TV answers; --full also plays something")
    st.add_argument("--full", action="store_true", help="also change volume by one and back, play, pause, resume")
    st.add_argument("--query", default="jazz music", help="what --full plays on YouTube")
    st.add_argument("--prefer", default="long", choices=["auto", "long", "live"])
    st.add_argument("--tv", help="only this TV (id or label)")
    tl = sub.add_parser("tool", help="run one agent tool without the hub, e.g. tool get_state")
    tl.add_argument("name")
    tl.add_argument("args", nargs="?", default="{}", help="its arguments as JSON")
    sub.add_parser("status", help="show power state and saved settings")
    k = sub.add_parser("key", help="press one or more keys, e.g. volup home enter")
    k.add_argument("keys", nargs="+")
    t = sub.add_parser("text", help="type into the on-screen keyboard")
    t.add_argument("text")
    t.add_argument("--submit", action="store_true")
    sub.add_parser("apps", help="list installed apps")
    la = sub.add_parser("launch", help="open an app by id")
    la.add_argument("app_id")
    hk = sub.add_parser("hub-key", help="show where the hub access key is, or --rotate it")
    hk.add_argument("--rotate", action="store_true")
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
