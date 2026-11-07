# Decisions and gotchas

What we learned building this against a real 2024 Samsung OLED and a Mac Mini hub, and why the code looks the way it does.

## Local API instead of IR or the cloud

The 2024 Tizen TVs have a local remote-control API (WebSocket on 8002, REST on 8001), so no IR blaster or HDMI-CEC hardware is needed. We use it instead of the SmartThings cloud API because it is faster, works without internet, and supports every remote key, text input and app launch. The TV uses a self-signed certificate, so the client skips certificate verification; this is acceptable only because that traffic never leaves the LAN.

## A hub instead of talking to the TV from each device

Browsers cannot open `wss://` to a self-signed TV, every device would need its own pairing prompt and token, and the TV must never be exposed off the LAN. One always-on hub holds one pairing, one persistent connection, and gives every device the same small authenticated API.

## Mac Mini as the hub

It is always on (sleep disabled, auto restart after power loss), on the same Wi-Fi as the TV, and already runs Tailscale. A Raspberry Pi works too (`deploy/tvremote-hub.service`).

## 2024 firmware does not list apps

The `ed.installedApp.get` WebSocket event (used by older models) gets no answer on 2024 firmware. Instead, `GET http://<tv>:8001/api/v2/applications/<id>` returns `{id, name, running, version, visible}` for an installed app and 404 otherwise. So `list_apps()` tries the event for 2.5 s, then probes the catalog in `tvremote/apps.py` (6 requests in parallel, duplicates by name removed). Launching uses `POST` to the same URL, with the WebSocket `ed.apps.launch` event as fallback. To add an app, add its ID to `KNOWN_APPS`. On the real TV this found 12 apps, and launching YouTube worked.

## Other firmware quirks

- `modelName` in `/api/v2/` is `"0"` on this TV; we fall back to `model` (`24_PTM_QD_T09`) or `name`.
- The device name arrives HTML-escaped, sometimes twice (`42&amp;quot; OLED`); `SamsungTV.clean_name` unescapes repeatedly.
- The first pairing needs someone to press **Allow** on the TV; `tvremote pair` waits 60 s. If the device was ever denied, delete it under Device Connection Manager > Device List.
- Power on from full standby needs **Power On with Mobile** enabled on the TV (Wake-on-LAN).

## macOS Local Network permission and the .app wrapper

macOS (Sonoma and later) blocks LAN traffic from apps without Local Network permission, failing with `EHOSTUNREACH` / errno 65 ("No route to host").

- From Terminal, allowing Local Network for Terminal fixes it.
- For the hub under launchd, allowing it did **not** help: macOS attributes the traffic to the bare Python binary, which has no app identity, so the permission never applies (tests showed Homebrew and system Python both failing while `/usr/bin/curl` worked).
- Fix: `deploy/macos/build-app.sh` builds `~/Applications/TVRemoteHub.app` with an `Info.plist` (`CFBundleIdentifier com.tvremote.hub`, `NSLocalNetworkUsageDescription`, `NSBonjourServices`) and an ad-hoc signed C launcher (`launcher.c`). launchd runs the launcher, which spawns the hub as a child; the child inherits the app as its responsible process, so the "TV Remote Hub" Local Network permission applies. The launcher forwards SIGTERM/SIGINT/SIGHUP and exits with the child's status so `KeepAlive` restarts work. Rebuilding re-signs the app, which may make macOS ask again.

## Tailscale for access from anywhere

`tailscale serve --bg 8080` gives the hub a real HTTPS certificate at `https://<hub>.<tailnet>.ts.net` with no port forwarding and no public exposure. HTTPS also lets the web app install to the home screen and use Secure cookies. The hub trusts `X-Forwarded-*` only from `127.0.0.1` (`TVREMOTE_TRUSTED_PROXIES`) so rate limiting sees real client IPs.

It also gets around two common home-network problems:

- **Wired PC isolated from the Wi-Fi.** Some routers keep Ethernet and Wi-Fi apart; over Tailscale a wired PC still reaches the hub directly.
- **Address overlap.** Some home networks use addresses inside Tailscale's `100.64.0.0/10`. That still works as long as the hub does not accept subnet routes or use an exit node, so the route to the TV stays on Wi-Fi. Check with `route get <TV IP>` (interface must be Wi-Fi, not `utun`). Tailscale also adds a competing multicast route, which is why discovery accepts `--interface`.

## Sign-in design

A single long random access key (generated on first start, stored mode 600) is simple to hand to each device once and easy to rotate. The browser cookie is `HMAC(key, "tvremote-session-v1")`, so a stolen cookie never reveals the key, and rotating the key invalidates every cookie.

## Web app without a framework

Plain HTML/CSS/JS keeps the app small, fast to load over Tailscale, dependency free, and served directly by the hub with no build step. A WebSocket keeps button latency low; HTTP is the fallback.
