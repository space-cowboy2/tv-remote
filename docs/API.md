# Hub API

Base URL: `https://dannys-mac-mini.tail24f732.ts.net` (or `http://127.0.0.1:8080` on the hub).

## Authentication

Every endpoint except `/api/health` and `/api/login` needs one of:

- `Authorization: Bearer <access key>` (CLI, scripts, iOS app; also accepted on the WebSocket handshake), or
- the `tvremote_session` cookie set by `POST /api/login` (web app). It holds an HMAC derived from the key, is HttpOnly, SameSite=Strict, Secure behind HTTPS, and lasts a year.

Wrong keys: `401`. More than 5 failed logins from one client in 5 minutes: `429`.

## Errors

| Status | Meaning |
|---|---|
| 401 | Not signed in |
| 422 | Invalid input (unknown key name, bad app ID, text too long) |
| 429 | Too many failed sign-ins |
| 502 | TV returned an error (e.g. app not installed) |
| 503 | TV is off or unreachable (`{"detail": "TV is off or unreachable", "error": "..."}`) |
| 504 | TV did not wake up (check Power On with Mobile) |

## Endpoints

| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/api/health` | | `{"ok": true, "version": "0.1.0"}` |
| POST | `/api/login` | `{"key": "..."}` | `{"ok": true}` + cookie |
| POST | `/api/logout` | | `{"ok": true}`, clears cookie |
| GET | `/api/status` | | `{"power": "on" \| "standby" \| "off", "connected": bool, "host": "..."}` |
| GET | `/api/keys` | | Key names grouped (`power`, `navigation`, `volume`, `channel`, `playback`, `numbers`, `color`, `input`, `other`) |
| POST | `/api/key` | `{"key": "KEY_VOLUP", "cmd": "Click" \| "Press" \| "Release"}` | `{"ok": true}`. `volup` and `KEY_VOLUP` are both accepted. |
| POST | `/api/text` | `{"text": "...", "submit": false}` (≤ 500 chars) | `{"ok": true}`. A TV text field must be focused. |
| GET | `/api/apps` | `?refresh=true` skips the 10-minute cache | `[{"id", "name", "type", "running"?}]` sorted by name |
| POST | `/api/launch` | `{"app_id": "3201907018807"}` | `{"ok": true}` |
| POST | `/api/power` | `{"state": "on" \| "off"}` | `{"ok": true, "power": "on"}` |
| GET | `/api/assistant` | | `{"enabled": bool}`: whether an Anthropic API key is set on the hub |
| POST | `/api/assistant` | `{"text": "open ohnepixel on Twitch"}` (≤ 500 chars) | `{"reply": "...", "actions": ["open_twitch: ..."]}`. 503 if Claude isn't set up, 429 over 20/min. See [ASSISTANT.md](ASSISTANT.md). |
| WS | `/api/ws` | see below | |

## WebSocket `/api/ws`

Low-latency channel for button presses. Authenticate with the cookie or a Bearer header on the handshake (unauthenticated sockets are closed with code `4401`).

Send:
```json
{"key": "KEY_UP", "cmd": "Click", "id": 1}
{"text": "stranger things", "submit": true, "id": 2}
```
Receive:
```json
{"ok": true, "id": 1}
{"ok": false, "id": 2, "error": "connection to TV lost"}
```

## Examples

```bash
KEY=$(cat ~/.config/tvremote/hub.key); H="Authorization: Bearer $KEY"; U=https://dannys-mac-mini.tail24f732.ts.net
curl -s -H "$H" $U/api/status
curl -s -H "$H" -H 'content-type: application/json' -d '{"key":"KEY_MUTE"}' $U/api/key
curl -s -H "$H" -H 'content-type: application/json' -d '{"app_id":"111299001912"}' $U/api/launch   # YouTube
curl -s -H "$H" -H 'content-type: application/json' -d '{"state":"off"}' $U/api/power
```

Common app IDs are in `tvremote/apps.py` (Netflix `3201907018807`, YouTube `111299001912`, Prime Video `3201910019365`, Disney+ `3201901017640`).
