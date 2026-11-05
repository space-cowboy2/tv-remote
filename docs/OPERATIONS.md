# Operations

## Everyday commands (on the Mac Mini)

```bash
launchctl print gui/$(id -u)/com.tvremote.hub | grep -E "state|pid"   # is the hub running?
launchctl kickstart -k gui/$(id -u)/com.tvremote.hub                 # restart it
tail -f ~/Library/Logs/tvremote-hub.log                                # logs
tailscale serve status                                                 # HTTPS URL
cd ~/Projects/tv-remote && . .venv/bin/activate && tvremote status    # TV state from the CLI
```

## Update to a new version

```bash
cd ~/Projects/tv-remote && git pull && . .venv/bin/activate && pip install -e .
launchctl kickstart -k gui/$(id -u)/com.tvremote.hub
```

Only if `deploy/macos/` changed, re-run `deploy/macos/build-app.sh ~/Projects/tv-remote/.venv` (rebuilding re-signs the app; macOS may ask for Local Network again). Web app changes show up on the next page load; the service worker refreshes the cached copy.

## Access key

```bash
tvremote hub-key                       # where the key file is
pbcopy < ~/.config/tvremote/hub.key    # copy it to sign in a new device
tvremote hub-key --rotate              # new key; then restart the hub
```

Rotate if a device is lost or the key was shared. Rotation signs out every browser immediately; the iOS app and scripts need the new key entered.

## Re-pair the TV

Needed after a TV factory reset, or if the TV starts asking again / denying.

```bash
tvremote pair --host 192.168.1.50   # press Allow on the TV
launchctl kickstart -k gui/$(id -u)/com.tvremote.hub
```

If it times out or is denied: on the TV, Settings > General > External Device Manager > Device Connection Manager > Device List, delete **TV Remote**, and try again.

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.tvremote.hub
tailscale serve reset
# then delete ~/Library/LaunchAgents/com.tvremote.hub.plist, ~/Applications/TVRemoteHub.app, ~/.config/tvremote
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| App shows "TV off" / "TV is off or unreachable" | TV unplugged or off Wi-Fi, or its IP changed. `tvremote status`; if the IP changed, reserve it in the router and re-pair. |
| Hub log: `[Errno 65] No route to host` | macOS Local Network permission. Turn on **TV Remote Hub** in Privacy & Security > Local Network, then kickstart. If the hub was started without the app wrapper, run `deploy/macos/build-app.sh`. |
| CLI: "The network blocked discovery" | Allow Local Network for Terminal, or skip discovery with `--host`. With Tailscale on, try `tvremote discover --interface <Mac's Wi-Fi IP>`. |
| Power on does nothing | Enable Power On with Mobile on the TV. The TV must have been off for less than its deep-sleep window, and on the same LAN as the hub. |
| Apps list is empty or missing an app | 2024 firmware does not report apps; the hub probes `tvremote/apps.py`. Add the app's ID there. Tap Refresh in the app (the list is cached 10 minutes). |
| Pairing: "no answer to the pairing prompt" | Nobody pressed Allow within 60 s, or the device is in the TV's Device List as denied. Delete it and retry. |
| Phone: page won't load | Tailscale not connected on the phone, or `tailscale serve` was reset. Check `tailscale serve status` on the Mac Mini. |
| Phone: "Please sign in" every time | The key was rotated, or the browser blocks cookies for the site. Sign in again with the current key. |
| "Too many attempts, try later" | 5 wrong keys in 5 minutes from that device; wait 5 minutes. |
| Hub keeps restarting | Look at the log. Common: venv moved or deleted (re-run `build-app.sh` with the right venv), or `tv.json` missing (pair again). |
| TV unreachable only when a Tailscale setting changes | The home LAN overlaps Tailscale's 100.64.0.0/10. Turn off accept-routes and exit nodes on the Mac Mini; `route get 192.168.1.50` must show the Wi-Fi interface. |
| Wired PC can't open the LAN address | The wired network is isolated from the Wi-Fi. Use the Tailscale URL on the PC. |
