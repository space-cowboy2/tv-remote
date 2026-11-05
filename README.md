# TV Remote

Control a Samsung TV (2016+ Tizen, built for the 2024 S90D OLED) from any device.

Status: all four steps built: TV connection, home hub, web app, and secure access from anywhere over Tailscale.

## Before you start (on the TV)

1. Connect the TV to your home Wi-Fi or Ethernet.
2. Settings > General > Network > Expert Settings > turn on **Power On with Mobile** (needed to turn the TV on remotely).
3. Settings > General > External Device Manager > Device Connection Manager > set Access Notification to **First Time Only**.

## Try it (on a computer on the same network as the TV)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
tvremote discover                 # prints the TV's IP
tvremote pair --host <TV IP>      # press Allow on the TV
tvremote key volup volup mute
tvremote apps
tvremote launch 3201907018807     # Netflix
tvremote off
tvremote on
```

**macOS:** the first network call asks for Local Network access. Allow it (System Settings > Privacy & Security > Local Network) for your terminal, and later for the hub's Python, or discovery fails with "No route to host". You can always skip discovery with `pair --host <TV IP>`. If a VPN like Tailscale grabs multicast, use `tvremote discover --interface <this computer's LAN IP>`.

Pairing info (IP, MAC, token) is saved to `~/.config/tvremote/tv.json` with owner-only permissions.

## Run the home hub

The hub is an always-on service (Raspberry Pi, Mac Mini, or any always-on computer on the same network as the TV) that keeps the TV connection and exposes a signed-in API for the remote app.

```bash
tvremote pair --host <TV IP>      # once
TVREMOTE_BIND=0.0.0.0 tvremote-hub
```

On first start it generates an access key and saves it to `~/.config/tvremote/hub.key`. Clients sign in with that key (`POST /api/login`) or send it as `Authorization: Bearer <key>`.

API (all except `/api/health` and `/api/login` need the key):

| Method | Path | Body |
|---|---|---|
| GET | `/api/health` | |
| POST | `/api/login` | `{"key": "..."}` (5 failed tries per 5 min per client) |
| GET | `/api/status` | |
| GET | `/api/keys` | |
| POST | `/api/key` | `{"key": "KEY_VOLUP", "cmd": "Click"}` |
| POST | `/api/text` | `{"text": "...", "submit": false}` |
| GET | `/api/apps` | `?refresh=true` to bypass the 10 min cache |
| POST | `/api/launch` | `{"app_id": "3201907018807"}` |
| POST | `/api/power` | `{"state": "on"}` or `{"state": "off"}` |
| WS | `/api/ws` | send `{"key": "KEY_UP", "id": 1}` or `{"text": "..."}` |

### Run the hub on boot

- **Raspberry Pi / Linux:** `deploy/tvremote-hub.service` (systemd). Create a `tvremote` user, install to `/opt/tvremote` with a venv, pair with `TVREMOTE_CONFIG=/var/lib/tvremote/tv.json`, then `sudo cp deploy/tvremote-hub.service /etc/systemd/system/ && sudo systemctl enable --now tvremote-hub`.
- **Mac (recommended, used for the Mac Mini):** run `deploy/macos/build-app.sh` (optionally pass the venv path). It builds a tiny `~/Applications/TVRemoteHub.app` wrapper and installs the launchd agent. The wrapper is needed because macOS only grants Local Network access to apps: a bare Python started by launchd gets "No route to host" (errno 65) to the TV even after you click Allow. Click **Allow** when macOS asks about TV Remote Hub. Turn off sleep in System Settings > Energy. (`deploy/com.tvremote.hub.plist` is the plain agent for reference; it hits that errno 65 issue.)

## Access from anywhere (Tailscale)

The hub listens on `127.0.0.1` only. Tailscale publishes it over HTTPS to devices signed in to your tailnet, so nothing is exposed to the internet and no router ports are opened.

1. Install Tailscale on the hub and on every phone, tablet, and computer that should control the TV, all signed in to the same account.
2. In the Tailscale admin console, enable **MagicDNS** and **HTTPS certificates** (DNS page).
3. On the hub: `tailscale serve --bg 8080`
4. Open `https://<hub name>.<tailnet>.ts.net` on any device (shown by `tailscale serve status`), sign in with the access key, and **Add to Home Screen**.

To stop publishing: `tailscale serve reset`. The launchd/systemd files already set `TVREMOTE_BIND=127.0.0.1` and `TVREMOTE_SECURE_COOKIE=1` for this setup. If you also want plain LAN access without Tailscale, set `TVREMOTE_BIND=0.0.0.0` and remove `TVREMOTE_SECURE_COOKIE` (less secure: the key travels unencrypted on your Wi-Fi).

**Address overlap:** Tailscale uses 100.64.0.0/10. If your home LAN is also in that range (some ISP routers do this), keep "Use Tailscale subnets" / accept-routes and exit nodes **off** on the hub so traffic to the TV stays on the LAN. Check with `route get <TV IP>` (Mac) or `ip route get <TV IP>` (Linux): the interface must be your Wi-Fi/Ethernet, not `utun`/`tailscale0`.

## Access key

- Show where it lives: `tvremote hub-key`
- Copy it on a Mac: `pbcopy < ~/.config/tvremote/hub.key`
- Rotate (signs every device out): `tvremote hub-key --rotate`, then restart the hub (`launchctl kickstart -k gui/$(id -u)/com.tvremote.hub`).

Devices stay signed in for a year with an HttpOnly cookie derived from the key (the key itself is never stored in the browser). Five wrong keys from one device lock it out for 5 minutes.

## Troubleshooting

| Symptom | Fix |
|---|---|
| App says "TV off or unreachable" | TV unplugged, or not on Wi-Fi. Check `tvremote status` on the hub. |
| Power on does nothing | Enable TV Settings > General > Network > Expert Settings > **Power On with Mobile**. |
| Pairing times out / denied | TV Settings > General > External Device Manager > Device Connection Manager > Device List: delete "TV Remote", then `tvremote pair` again. |
| "No route to host" on Mac | CLI: allow Local Network for Terminal. Hub: install with `deploy/macos/build-app.sh` and turn on **TV Remote Hub** in System Settings > Privacy & Security > Local Network, then `launchctl kickstart -k gui/$(id -u)/com.tvremote.hub`. |
| Phone can't load the app | Is Tailscale connected on the phone? Check `tailscale serve status` on the hub. |
| Hub logs | Mac: `~/Library/Logs/tvremote-hub.log`. Linux: `journalctl -u tvremote-hub`. |
| TV IP changed | Reserve the TV's IP in your router (DHCP reservation), then `tvremote pair --host <new IP>`. |

## The remote app

The hub serves the app from `web/` (set `TVREMOTE_WEB_DIR`, already set in the service files). Open the Tailscale HTTPS address (above) on any phone, tablet or computer, sign in with the access key once, and use **Add to Home Screen** to install it like an app.

- Remote: power, D-pad, back/home/settings, volume and channel (hold to repeat), mute, source, media, numbers, color keys
- Apps: tap to launch any installed TV app
- Keyboard: type into TV search boxes
- Desktop shortcuts: arrows, Enter, Backspace (back), Esc (home), +/- (volume), M (mute), Space (play), 0–9

## Develop without a TV

```bash
pip install -e '.[dev]'
pytest                            # runs against a simulated TV
python -m tvremote.fake           # standalone simulated TV on ports 8001/8002 (no TLS)
python -m tvremote.devserver      # hub + web app + simulated TV at http://localhost:8080 (key: dev)
node tests/e2e/remote.spec.mjs    # browser tests (needs devserver running and Playwright)
```

CI (`.github/workflows/ci.yml`) runs the unit tests on Linux and macOS with Python 3.11–3.13, plus the browser tests, on every push and pull request.

## Security notes

- The TV's API uses a self-signed certificate, so the client skips certificate checks. This is only safe on your home LAN. Never forward the TV's ports (8001/8002) to the internet; remote access goes through the hub over Tailscale.
- The hub listens on localhost only and is reachable solely through Tailscale HTTPS. It sends `nosniff`, `DENY` framing and `no-store` headers, validates every key and app ID, and rate-limits sign-in.
- The pairing token grants full control of the TV. Keep `tv.json` private.
