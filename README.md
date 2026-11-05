# TV Remote

Control a Samsung TV (2016+ Tizen, built for the 2024 S90D OLED) from any device.

Status: **Step 1 of 4: TV connection library + CLI.** Hub, web app, and remote access come next.

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

Pairing info (IP, MAC, token) is saved to `~/.config/tvremote/tv.json` with owner-only permissions.

## Develop without a TV

```bash
pip install -e '.[dev]'
pytest                            # runs against a simulated TV
python -m tvremote.fake           # standalone simulated TV on ports 8001/8002 (no TLS)
```

## Security notes

- The TV's API uses a self-signed certificate, so the client skips certificate checks. This is only safe on your home LAN. Never forward the TV's ports (8001/8002) to the internet; remote access will go through the hub over Tailscale (step 4).
- The pairing token grants full control of the TV. Keep `tv.json` private.
