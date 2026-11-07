# TV Remote

Control a Samsung TV from any phone, tablet or computer, at home or away. Built for a 42" 2024 Samsung OLED (Tizen), and works with most Samsung smart TVs from 2016 on.

- **Full remote:** power, arrows and OK, back, home, settings, volume and channel (hold to repeat), mute, input, playback, numbers, color keys, HDMI inputs
- **Apps:** one-tap launch of Netflix, YouTube, Prime Video and other installed apps
- **Keyboard:** type into TV search boxes from your phone
- **Everywhere:** an installable web app for any device, plus a native iPhone app
- **Private:** no cloud and no open ports. Devices reach the home hub only through your Tailscale network, over HTTPS, with an access key.

```
phone / PC ──HTTPS (Tailscale)──► Mac Mini hub ──LAN──► Samsung TV
```

## Quick start

On an always-on computer on the same Wi-Fi as the TV (the hub):

```bash
git clone https://github.com/space-cowboy2/tv-remote.git && cd tv-remote
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
tvremote pair --host <TV IP>                 # press Allow on the TV
deploy/macos/build-app.sh                    # macOS: install the hub service (Linux: see docs/SETUP.md)
tailscale serve --bg 8080                    # publish it over HTTPS to your tailnet
pbcopy < ~/.config/tvremote/hub.key          # the access key for signing in
```

Then on each device: connect Tailscale, open `https://<hub>.<tailnet>.ts.net`, sign in, and Add to Home Screen.

Full walkthrough, including TV settings: **[docs/SETUP.md](docs/SETUP.md)**.

## Claude Code

Control the TV from Claude Code with the bundled plugin ([claude-plugin/README.md](claude-plugin/README.md)):

```
/plugin install tv-remote --marketplace space-cowboy2/tv-remote
```

## Documentation

| Doc | What's in it |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Current deployment, step-by-step install on the Mac Mini (or a Raspberry Pi), Tailscale, adding devices |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, how a button press flows, security model, tests |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Why it's built this way, and the gotchas: 2024 firmware app list, macOS Local Network and the `.app` wrapper, Tailscale address overlap |
| [docs/ASSISTANT.md](docs/ASSISTANT.md) | Ask Claude: plain-language commands, API key setup, what works (and what does not) for Twitch/YouTube, calibrating key sequences |
| [docs/API.md](docs/API.md) | Hub HTTP and WebSocket API, with curl examples |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Restart, update, rotate the key, re-pair, uninstall, troubleshooting |
| [ios/README.md](ios/README.md) | Build and install the native iPhone app |
| [SECURITY.md](SECURITY.md) | Secrets, what's exposed, key rotation |
| [CHANGELOG.md](CHANGELOG.md) | What was built, in order |

## Repository layout

```
tvremote/        Python package: TV client, CLI, hub (FastAPI), discovery, Wake-on-LAN, simulated TV
web/             Web app served by the hub (HTML/CSS/JS, installable, no build step)
ios/             Native SwiftUI iPhone app (XcodeGen project)
claude-plugin/   Claude Code plugin: MCP server, skill and /tv command over the hub API
deploy/macos/    TVRemoteHub.app wrapper + launchd installer (Mac hub)
deploy/          systemd unit (Linux hub), plain launchd plist (reference)
tests/           pytest suite against a simulated TV; tests/e2e browser tests (Playwright)
docs/            Documentation
```

## CLI

```bash
tvremote discover                 # find TVs (SSDP, then mDNS)
tvremote pair --host <IP>         # pair; saves ~/.config/tvremote/tv.json
tvremote status                   # power state and saved settings (token masked)
tvremote key volup volup mute     # press keys (with or without KEY_ prefix)
tvremote text "stranger things" --submit
tvremote apps                     # list apps
tvremote launch 111299001912      # open YouTube
tvremote on | off
tvremote hub-key [--rotate]       # show or rotate the hub access key
```

## Develop without a TV

```bash
pip install -e '.[dev]'
pytest                            # 44 tests against a simulated TV
python -m tvremote.devserver      # hub + web app + simulated TV at http://localhost:8080 (key: dev)
node tests/e2e/remote.spec.mjs    # 40 browser checks (with devserver running; needs Playwright)
```

CI runs all of this on every push and pull request (`.github/workflows/ci.yml`).
