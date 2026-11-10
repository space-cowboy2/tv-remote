# Changelog

## 0.2.0 (2026-11-10)

Conversational control: the agent tools do whole jobs and answer with evidence. See [docs/CONVERSATION.md](docs/CONVERSATION.md).

- `play`: one call from "what" to "started". Picks a video by searching YouTube from the hub, wakes the TV, restarts YouTube, opens its search box and confirms it through the TV's keyboard announcements, types, starts the first result. Bounded retries (three tries, 170 s), one request per TV at a time, repeats within 30 s ignored. Other services: opens the app and says the search was not done.
- Reports instead of "sent": `verified`, `not_verified`, `failed.kind`, `needs_user`. Playback is never claimed.
- `set_volume` (absolute, relative, mute) and `set_power` with read-back; `playback`, `open_app` by name, `switch_input`, `search_youtube`, `get_state`.
- Timers in the hub (`schedule`, `cancel_schedule`), kept across restarts.
- Several TVs: `tv-<name>.json` next to `tv.json`, an active TV, `use_tv`, `find_tvs`, `pair_tv` (pair from the conversation while someone is at the TV). The hub starts with no TV paired and picks one up without a restart.
- `guide.md`: the model's handbook, served with the tool list.
- TV client: power state from three signals (no blind `KEY_POWER`), app state through DIAL when the REST app endpoints hang (2022 Q60B), UPnP volume, keyboard events, launch acknowledgment, app close.
- Discovery finds Samsungs that ignore the Samsung-specific search (Q60B) and lists other brands as not controllable. The search leaves from every local address, because a PC with a VPN (Tailscale) otherwise searches the wrong network.
- Waking: the hub notes the address its own network has for the TV (`lan_mac` in `tv.json`) and wakes that one as well as the Wi-Fi address the TV reports, so a TV on a cable wakes too; wake-ups leave from every local address. Pairing retries a stalled status port.
- Claude plugin 0.2.0 in `claude-plugin/`: local hub by default, `get_guide`, structured errors, UTF-8 on Windows.
- Windows hub: `deploy/windows/install-hub.ps1`; file logging when there is no console. `-Pair <address>` pairs in the same run, `-Gateway <folder>` points a duet-gateway's `tv` upstream at the installation (`tvremote link-gateway`).
- `tvremote selftest [--full]`, `tvremote selftest --host <address>` (read-only, before pairing) and `tvremote tool <name> <json>` for live checks.
- Optional playback check (`playback_check`, off by default): after `play`, the hub can ask the TV's YouTube
  app what is playing through YouTube's own remote link, read-only, and report the video as confirmed.
  Tested against a stand-in only; not yet run on a real TV.
- Tests: 187, all on a simulated TV with a YouTube screen model and virtual time.

## 0.1.0 (2026-11-05)

First release, built and verified against a 42" 2024 Samsung OLED with a Mac Mini hub.

### Step 1: TV connection
- Async client for the Tizen local API: token pairing, key click/press/release, hold, text input, cursor, apps, launch, power state, Wake-on-LAN power on, power off, automatic reconnect.
- `tvremote` CLI and a simulated TV for tests.
- Discovery via SSDP with mDNS fallback, `--interface` pinning, and a clear message when macOS Local Network permission blocks it.
- Real-TV fixes: `modelName` "0" falls back to `model`; double-HTML-escaped TV name unescaped; pairing waits 60 s.

### Step 2: Home hub
- FastAPI hub (`tvremote-hub`): REST + WebSocket API, access-key sign-in with rate limit, security headers, input validation, cached app list, serialized TV commands.
- Boot services: systemd unit and launchd agent.

### Step 3: Remote apps
- Installable web app: Remote, Apps, Keyboard tabs; hold-to-repeat; haptics; desktop shortcuts; light/dark; offline app shell. Browser-tested on phone and desktop viewports.
- 2024 firmware app list: probes known app IDs over REST (`/api/v2/applications/<id>`), launches via REST POST.
- Native SwiftUI iPhone app (`ios/`): Remote, Apps, Keyboard, More (numbers, colors, HDMI); WebSocket with REST fallback; key in the Keychain.

### Step 4: Production
- Tailscale HTTPS access (`tailscale serve`); hub bound to localhost; Secure cookies; trusted proxy setting.
- Session cookie derived from the key (HMAC) instead of containing it; `tvremote hub-key --rotate`.
- macOS `TVRemoteHub.app` wrapper so Local Network permission applies to the launchd hub.
- CI on Linux and macOS, Python 3.11–3.13, plus browser tests.
- Documentation: setup, architecture, decisions, API, operations, security.
