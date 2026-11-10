# Architecture

TV Remote lets any phone, tablet or computer control a Samsung TV, at home or away, without opening anything to the internet.

```
 iPhone / iPad / PC / laptop                 Mac Mini (always on, home Wi-Fi)                Samsung TV
 ┌──────────────────────────┐   HTTPS over   ┌──────────────────────────────────┐   LAN    ┌──────────────────┐
 │ Web app (installable PWA)│   Tailscale    │ tailscale serve :443 ─► 127.0.0.1:8080        │ wss :8002 remote │
 │ iOS app (ios/)           │ ─────────────► │ TVRemoteHub.app ─► tvremote-hub   │ ───────► │ http :8001 REST  │
 │ CLI / scripts (Bearer)   │                │  (FastAPI: auth, API, WebSocket)  │  + WoL   │ (Tizen 2024)     │
 └──────────────────────────┘                └──────────────────────────────────┘          └──────────────────┘
```

## Components

| Part | Where | What it does |
|---|---|---|
| TV client | `tvremote/samsung.py` | Async client for the TV's local API: pairing (token), key presses (click/hold), text input, cursor, app list and launch, power state, power on (Wake-on-LAN) and off. Reconnects once automatically if the TV drops the socket. |
| App catalog | `tvremote/apps.py` | Known Tizen app IDs, used to build the app list on firmware that no longer reports installed apps (see [DECISIONS.md](DECISIONS.md#2024-firmware-does-not-list-apps)). |
| Discovery | `tvremote/discovery.py` | Finds TVs with SSDP, falls back to mDNS (`_samsungmsf._tcp`). Can pin multicast to one interface. |
| Wake-on-LAN | `tvremote/wol.py` | Magic packet to the TV's MAC. |
| Config | `tvremote/config.py` | `~/.config/tvremote/tv.json` (host, MAC, model, pairing token), written atomically with mode 600. |
| CLI | `tvremote/__main__.py` | `tvremote discover | pair | status | key | text | apps | launch | on | off | hub-key`. |
| Agent tools | `tvremote/tools.py`, `tools.json`, `guide.md` | What an assistant calls: whole jobs (`play`, `set_volume`, `set_power`, ...) that check the TV's signals and answer with evidence, timers, several TVs, notes and saved key sequences. See [CONVERSATION.md](CONVERSATION.md). |
| YouTube lookup | `tvremote/youtube.py` | Finds videos on the internet (no API key) so `play` settles on one video before touching the TV. |
| Volume | `tvremote/upnp.py` | Absolute volume and mute with read-back through the TV's UPnP renderer. |
| Claude plugin | `claude-plugin/` | Stdio MCP server that passes the hub's tools to Claude, plus a skill and a command. |
| Windows hub | `deploy/windows/install-hub.ps1` | Per-user install that starts the hub at sign-in. |
| Hub | `tvremote/hub.py` | FastAPI service (`tvremote-hub`). Holds one persistent TV connection, serializes commands, exposes the [HTTP/WebSocket API](API.md), checks the access key, serves the web app. |
| Web app | `web/` | Plain HTML/CSS/JS, no build step. Remote, Apps and Keyboard tabs; installable to the home screen; light and dark; offline app shell via service worker. |
| iOS app | `ios/` | Native iPhone client of the same hub API. See `ios/README.md`. |
| Simulated TV | `tvremote/fake.py` | Speaks the TV protocol (WebSocket + REST) for tests and demos; mimics 2024 firmware quirks. |
| Dev server | `tvremote/devserver.py` | Hub + web app + simulated TV on `localhost:8080` (key `dev`). |
| macOS wrapper | `deploy/macos/` | `TVRemoteHub.app`: a signed launcher so macOS Local Network permission applies to the hub. Installs the launchd agent. |
| Linux service | `deploy/tvremote-hub.service` | systemd unit for a Raspberry Pi or other Linux hub. |
| CI | `.github/workflows/ci.yml` | Unit tests on Linux and macOS (Python 3.11–3.13) and browser tests on every push and PR. |

## How a button press flows

1. You tap **Vol +** in the web app. It sends `{"key":"KEY_VOLUP","id":7}` on its open WebSocket to `/api/ws` (falls back to `POST /api/key` if the socket is down).
2. Tailscale delivers it over HTTPS to `tailscale serve` on the Mac Mini, which proxies to `127.0.0.1:8080`.
3. The hub checks the session cookie (or Bearer key), validates the key name, and sends `ms.remote.control` / `SendRemoteKey` to the TV over its persistent `wss://<tv>:8002` channel.
4. The hub replies `{"ok":true,"id":7}`; the app vibrates and moves on. Typical round trip on the LAN is tens of milliseconds.

Holding a button repeats it every 140 ms after 450 ms. Apps are launched with `POST http://<tv>:8001/api/v2/applications/<id>`; power on uses Wake-on-LAN (or `KEY_POWER` when the TV is in network standby) and polls until the TV reports `PowerState: on`.

## Security model

- **Nothing on the internet.** The hub binds to `127.0.0.1`; only `tailscale serve` can reach it, and only devices on your tailnet can reach that. No router ports are opened. The TV's own ports are never exposed beyond the LAN.
- **Two secrets, both on the hub only.** The TV pairing token (`tv.json`) and the hub access key (`hub.key`), both mode 600 in `~/.config/tvremote/`. Neither is in the repo.
- **Clients** sign in once with the access key. Browsers get an HttpOnly, SameSite=Strict, Secure cookie holding an HMAC derived from the key, never the key itself; rotating the key signs every device out. Scripts and the iOS app can send `Authorization: Bearer <key>`.
- Sign-in is rate-limited (5 failures per client per 5 minutes). Inputs are validated (known key names only, app IDs `[A-Za-z0-9._-]`, text ≤ 500 chars). Responses carry `nosniff`, `X-Frame-Options: DENY`, `no-store`.

See [SECURITY.md](../SECURITY.md) for reporting and rotation.

## Testing

- Conversational control is tested on a simulated TV with a model of YouTube's screens and virtual time; what was checked on real TVs is listed in [CONVERSATION.md](CONVERSATION.md#how-this-is-tested).
- `pytest`: tests against the simulated TV (pairing allow/deny/timeout, keys, hold, text, apps on old and 2024 firmware, launch, reconnect, power, discovery fallbacks, config permissions, hub auth, cookies, rate limit, WebSocket, headers).
- `tests/e2e/remote.spec.mjs`: 40 Playwright checks of the web app on an iPhone-sized and a desktop viewport, in light and dark (sign-in, wrong key, buttons over WebSocket, no horizontal scroll, apps, keyboard, power cycle, sign-out, no JS errors).
- Verified on the real TV (42" 2024 OLED, platform `24_PTM_QD_T09`) from the Mac Mini: pairing, keys, status, app list (12 apps found), app launch, and the full path through the Tailscale HTTPS URL.
