# Changelog

## Unreleased

### Vague YouTube asks
- `search_youtube` tool: top YouTube videos for a query, scraped from the public results page (no API key). Claude uses it to turn "play some jazz" into one long mix or live stream, then plays it natively with `play_youtube`.

### Claude Code plugin
- `claude-plugin/`: MCP server, skill and `/tv-remote:tv` command so Claude Code (on a Claude subscription) controls the TV. Install with `/plugin marketplace add space-cowboy2/tv-remote`. See claude-plugin/README.md.
- Hub: `GET /api/tools` and `POST /api/tools/{name}` run the assistant's TV tools directly, no Anthropic key needed.

### Ask Claude
- `POST /api/assistant`: plain-language TV commands run by Claude with tool use over hub actions (power, keys, text, apps, YouTube deep links, key-sequence macros, web search). Key from `ANTHROPIC_API_KEY` or `anthropic.key`. See docs/ASSISTANT.md.
- Ask tab in the web app (with mic where the browser supports speech) and the iOS app.
- Memory: `notes.md` and `macros.json` next to `tv.json`; Claude's `remember`, `save_macro`, `run_macro` tools.
- YouTube videos play natively through the `youtube_search` key-sequence macro (`POST /api/youtube`); every launch-with-parameter route drops the video on 2024 Tizen and casting triggers Multi View.
- `SamsungTV.open_deep_link()` sends `ed.apps.launch` with a meta tag over the WebSocket (ignored by YouTube on this TV; kept for other apps).

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
