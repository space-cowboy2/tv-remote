# Security

## What is exposed

- **Nothing to the internet.** The hub listens on `127.0.0.1:8080`. `tailscale serve` publishes it over HTTPS only to devices signed in to the owner's tailnet. No router port forwarding.
- The TV's ports (8001, 8002) are used only on the home LAN, by the hub. Never forward them. The TV uses a self-signed certificate, so the hub does not verify it; this is acceptable only on the LAN.

## Secrets

| Secret | Location | Grants |
|---|---|---|
| TV pairing token | `~/.config/tvremote/tv.json` (mode 600) on the hub | Full control of the TV from the LAN |
| Hub access key | `~/.config/tvremote/hub.key` (mode 600) on the hub | Full control of the TV through the hub |

Neither is ever committed. `.gitignore` excludes `tv.json`, `hub.key` and `*.key`. The iOS app keeps the key in the Keychain (this device only, after first unlock). Browsers keep only an HMAC-derived session cookie (HttpOnly, SameSite=Strict, Secure), never the key.

## Protections in the hub

- Constant-time key and cookie comparison
- 5 failed sign-ins per client per 5 minutes, then `429`. Client IPs come from `X-Forwarded-For` only when sent by a trusted proxy (`127.0.0.1` by default)
- Input validation: known key names only, app IDs `[A-Za-z0-9._-]{1,64}`, text ≤ 500 chars
- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` on API responses
- API docs pages disabled

## If a device is lost or the key leaks

```bash
tvremote hub-key --rotate
launchctl kickstart -k gui/$(id -u)/com.tvremote.hub
```

Then remove the lost device from the Tailscale admin console. To also revoke TV access, delete **TV Remote** from the TV's Device Connection Manager > Device List and re-pair.
