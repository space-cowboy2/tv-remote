# Operations

## Everyday commands (on a Mac hub)

```bash
launchctl print gui/$(id -u)/com.tvremote.hub | grep -E "state|pid"   # is the hub running?
launchctl kickstart -k gui/$(id -u)/com.tvremote.hub                 # restart it
tail -f ~/Library/Logs/tvremote-hub.log                                # logs
tailscale serve status                                                 # HTTPS URL
cd ~/Projects/tv-remote && . .venv/bin/activate && tvremote status    # TV state from the CLI
```

On a Windows hub (PowerShell; `$py = "$env:LOCALAPPDATA\tvremote\venv\Scripts\python.exe"`):

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/health            # is the hub running?
Get-Content $env:USERPROFILE\.config\tvremote\hub.log -Tail 30  # logs
& $py -m tvremote selftest                                    # what the TV answers right now
cd $env:USERPROFILE\tv-remote; git pull; powershell -ExecutionPolicy Bypass -File deploy\windows\install-hub.ps1   # update and restart
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
tvremote pair --host <TV IP>   # press Allow on the TV
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
| Claude says the TV hub is not answering | The hub is not running on its computer. Windows: run `deploy\windows\install-hub.ps1` again and read `hub.log`. |
| Claude has no TV tools at all (behind duet-gateway) | The gateway could not start the TV's MCP server. Its `guide` tool and `npm run doctor` give the reason. `install-hub.ps1 -Gateway <gateway folder>` sets the right paths; restart the gateway afterwards. |
| `play` fails with `search_box_not_found` | The keys that open YouTube's search do not fit this TV's layout. Watch the screen during one run, then save the right steps as `youtube_open_search` (`save_macro`). |
| `tvremote discover` lists the TV as "other device" or not at all | Some models (2022 Q60B) only answer as a DIAL device; they are still recognized by their SERVER header. If nothing is listed, pass the address with `--host`. |
| Pairing: "no answer to the pairing prompt" | Nobody pressed Allow within 60 s, or the device is in the TV's Device List as denied. Delete it and retry. |
| Phone: page won't load | Tailscale not connected on the phone, or `tailscale serve` was reset. Check `tailscale serve status` on the hub. |
| Phone: "Please sign in" every time | The key was rotated, or the browser blocks cookies for the site. Sign in again with the current key. |
| "Too many attempts, try later" | 5 wrong keys in 5 minutes from that device; wait 5 minutes. |
| Hub keeps restarting | Look at the log. Common: venv moved or deleted (re-run `build-app.sh` with the right venv), or `tv.json` missing (pair again). |
| TV unreachable only when a Tailscale setting changes | Your home LAN overlaps Tailscale's 100.64.0.0/10. Turn off accept-routes and exit nodes on the hub; `route get <TV IP>` must show the Wi-Fi interface. |
| Wired PC can't open the LAN address | Your router isolates wired devices from the Wi-Fi. Use the Tailscale URL on the PC. |
