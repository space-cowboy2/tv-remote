# Setup

How to set it up for your own Samsung smart TV (Tizen, 2016 or newer) with an always-on Mac as the hub. For a Raspberry Pi or other Linux hub, see the end. Replace `<TV IP>` below with your TV's address.

## What you end up with

| Item | Value |
|---|---|
| TV | Any Samsung Tizen TV on your home Wi-Fi, with a reserved IP (`<TV IP>`) |
| Hub | An always-on Mac on the same network (sleep off, restart after power failure on), code in `~/Projects/tv-remote`, venv `~/Projects/tv-remote/.venv`, Python 3.11+ |
| Hub process | `~/Applications/TVRemoteHub.app` started by launchd agent `com.tvremote.hub`, listening on `127.0.0.1:8080` |
| Remote URL | `https://<hub>.<tailnet>.ts.net` (Tailscale serve, tailnet devices only) |
| Secrets | `~/.config/tvremote/tv.json` (TV token), `~/.config/tvremote/hub.key` (access key), both mode 600, never in git |
| Logs | `~/Library/Logs/tvremote-hub.log` |

## 1. Prepare the TV (once)

1. Connect it to the home Wi-Fi (same network as the hub). Reserve its IP in the router (DHCP reservation) so it never changes.
2. Settings > General > Network > Expert Settings > **Power On with Mobile**: On.
3. Settings > General > External Device Manager > Device Connection Manager > Access Notification: **First Time Only**.

## 2. Install on the hub (Mac)

```bash
# prerequisites: Homebrew, Python 3.11+, Xcode Command Line Tools
git clone https://github.com/space-cowboy2/tv-remote.git ~/Projects/tv-remote
cd ~/Projects/tv-remote
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

System Settings:
- Energy: prevent automatic sleeping; start up automatically after a power failure.
- Privacy & Security > Local Network: allow **Terminal** (for the CLI).

## 3. Pair with the TV

```bash
tvremote discover                         # finds the TV (or skip and use its IP)
tvremote pair --host <TV IP>              # press Allow on the TV within 60 s
tvremote key volup voldown                # TV volume should move
tvremote apps                             # lists installed apps
tvremote status
```

## 4. Install the hub service

```bash
deploy/macos/build-app.sh ~/Projects/tv-remote/.venv
```

This builds and signs `~/Applications/TVRemoteHub.app`, writes `~/Library/LaunchAgents/com.tvremote.hub.plist` (bind `127.0.0.1:8080`, Secure cookies, serves `web/`), and starts it. Click **Allow** when macOS asks to let **TV Remote Hub** find devices on the local network. If no prompt appears, turn it on in System Settings > Privacy & Security > Local Network, then:

```bash
launchctl kickstart -k gui/$(id -u)/com.tvremote.hub
curl -s localhost:8080/api/health
curl -s -H "Authorization: Bearer $(cat ~/.config/tvremote/hub.key)" localhost:8080/api/status   # power should be "on"
```

Why the app wrapper is needed: [DECISIONS.md](DECISIONS.md#macos-local-network-permission-and-the-app-wrapper).

## 5. Publish over Tailscale

1. Install Tailscale on the hub and sign in. In the admin console (DNS page) turn on **MagicDNS** and **HTTPS certificates**.
2. On the hub: `tailscale serve --bg 8080`, then `tailscale serve status` shows the HTTPS URL.
3. Keep **Use Tailscale subnets** (accept routes) and exit nodes **off** on the hub. Confirm `route get <TV IP>` uses the Wi-Fi interface, not `utun` (this matters most if your home network uses `100.x` addresses).

## 6. Set up each device

1. Install Tailscale and sign in with the same account.
2. Open `https://<hub>.<tailnet>.ts.net`.
3. Sign in with the access key (on a Mac hub: `pbcopy < ~/.config/tvremote/hub.key`, then paste it via Universal Clipboard or a password manager).
4. iPhone/iPad: Share > **Add to Home Screen**. Desktop Chrome/Edge: install icon in the address bar.

Or install the native iPhone app: see [ios/README.md](../ios/README.md).

## Windows hub instead

On an always-on Windows PC on the same network as the TV, with Python 3.11+ and git:

```powershell
git clone https://github.com/space-cowboy2/tv-remote.git $env:USERPROFILE\tv-remote
powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\tv-remote\deploy\windows\install-hub.ps1
```

The script installs the hub into `%LOCALAPPDATA%\tvremote\venv`, starts it without a window on `127.0.0.1:8080`, and registers it to start at every sign-in (no administrator rights). Settings, pairing and the access key are in `%USERPROFILE%\.config\tvremote`, the log in `hub.log` there. Clone into a folder only you can write to: the code runs as you.

Pair with the TV on, either from a terminal (`%LOCALAPPDATA%\tvremote\venv\Scripts\python -m tvremote pair --host <TV IP>`) or by asking Claude to pair it while you are at the TV (`pair_tv`). The running hub picks the TV up by itself.

To let Claude run the TV on that PC, point an MCP client at `claude-plugin\server\tv_mcp.py` in the clone (see [claude-plugin/README.md](../claude-plugin/README.md)).

## Raspberry Pi / Linux hub instead

```bash
sudo useradd -r -m -d /var/lib/tvremote tvremote
sudo git clone https://github.com/space-cowboy2/tv-remote.git /opt/tvremote
sudo python3 -m venv /opt/tvremote/.venv && sudo /opt/tvremote/.venv/bin/pip install -e /opt/tvremote
sudo -u tvremote env TVREMOTE_CONFIG=/var/lib/tvremote/tv.json /opt/tvremote/.venv/bin/tvremote pair --host <TV IP>
sudo cp /opt/tvremote/deploy/tvremote-hub.service /etc/systemd/system/
sudo systemctl enable --now tvremote-hub
sudo tailscale serve --bg 8080
```

The unit sets `TVREMOTE_BIND=0.0.0.0`; change it to `127.0.0.1` and add `Environment=TVREMOTE_SECURE_COOKIE=1` when using Tailscale serve.
