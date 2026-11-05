# TV Remote

Control a Samsung TV (2016+ Tizen, built for the 2024 S90D OLED) from any device.

Status: **Steps 1–2 of 4 done:** TV connection library + CLI, and the home hub API. Web app and remote access come next.

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
- **Mac:** `deploy/com.tvremote.hub.plist` (launchd). Replace `YOU` with your username, copy to `~/Library/LaunchAgents/`, and run `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tvremote.hub.plist`. Turn off sleep in System Settings > Energy.

## Develop without a TV

```bash
pip install -e '.[dev]'
pytest                            # runs against a simulated TV
python -m tvremote.fake           # standalone simulated TV on ports 8001/8002 (no TLS)
```

## Security notes

- The TV's API uses a self-signed certificate, so the client skips certificate checks. This is only safe on your home LAN. Never forward the TV's ports (8001/8002) to the internet; remote access will go through the hub over Tailscale (step 4).
- The pairing token grants full control of the TV. Keep `tv.json` private.
