# Ask Claude (plain-language commands)

The **Ask** tab in the web and iOS apps sends a request like "open ohnepixel on Twitch" or
"volume down 5" to the hub, which runs it through Claude (`claude-opus-5-5`, low effort) with
tool use over the hub's own TV actions: power, keys, text, app list/launch, YouTube deep links,
key sequences ("macros") and web search (to find things like a video id).

## Set up

1. Create an API key at https://console.anthropic.com (Settings → API keys).
2. On the hub (Mac Mini), save it where only your user can read it:
   ```bash
   pbpaste > ~/.config/tvremote/anthropic.key && chmod 600 ~/.config/tvremote/anthropic.key
   ```
   (or set `ANTHROPIC_API_KEY` in the launchd plist). The key never leaves the hub.
3. Install the new dependency and restart the hub:
   ```bash
   cd ~/Projects/tv-remote && .venv/bin/pip install -e . && launchctl kickstart -k gui/$(id -u)/com.tvremote.hub
   ```

`GET /api/assistant` returns `{"enabled": true}` once a key is found. Optional:
`TVREMOTE_CLAUDE_MODEL` picks another model.

## What works and what doesn't

The hub can't see the TV screen, so Claude only knows what it sent, not what the TV shows.

| Request | How | Reliability |
|---|---|---|
| Power, volume, mute, inputs, playback, navigation | key presses | Reliable |
| Open an app | REST launch | Reliable |
| Play a specific YouTube video (link, id or title) | `youtube_search` macro: Home, open YouTube, Left, Up, OK, type the title, submit, Right, OK. A link/id is turned into a title via YouTube's oEmbed endpoint. ~25 s. | Verified 2026-11-06 |
| Play something vague ("play some jazz") | `search_youtube` scrapes YouTube's public results page (no API key) for ids, titles, channels, lengths and live badges; Claude picks a long mix or live stream and plays it with `play_youtube` | Parser unit-tested; YouTube can change its page, in which case the tool says it found nothing |
| Open a specific Twitch channel | `twitch_channel` macro (same idea) | Not yet calibrated; tell Claude what the screen showed and it will fix the steps |
| Skip a YouTube ad | `skip_ad` presses OK, which hits YouTube's Skip button when it's showing | Only on request: with no ad on screen, OK pauses the video |
| Anything needing to read the screen ("is he live?", "pick the 3rd result") | not possible | — |

Dead ends on this TV (don't retry): YouTube deep links via `ed.apps.launch` (DEEP_LINK and
NATIVE_LAUNCH, app id or Tizen package id), REST launch with a JSON body, and DIAL
`POST /ws/apps/YouTube` payloads all start YouTube but drop the video. Casting through the
YouTube Lounge API does play the video, but the TV opens Samsung Multi View (split screen, no
sound) for casts, and Danny doesn't want casting. Ad skipping with screen feedback would need that
same Lounge connection, so it isn't offered.

## Memory

Claude keeps two files next to `tv.json` and reads them on every request:

- `notes.md`: lasting facts about the TV and preferences. Claude appends with its `remember` tool
  ("remember that I want captions on"); you can edit it by hand.
- `macros.json`: key sequences Claude can run (`run_macro`) and create or fix (`save_macro`).
  Built-in macros (`youtube_search`, `twitch_channel`) live in `tvremote/assistant.py`; the file
  only holds what Claude changed, and overrides built-ins by name.

Step types: `{"key": "KEY_X", "repeat": n}`, `{"wait": seconds}` (≤ 15), `{"launch": app_id}`,
`{"text": "...{value}...", "submit": true}`. `{value}` is the macro argument.

Calibrating: ask for the thing, watch the TV, then tell Claude what happened ("it stopped on the
search box", "it needs one more Up"). It adjusts the macro and saves it.

`POST /api/youtube {"video": "<url | id | title>"}` runs the YouTube routine without Claude.

## Cost and limits

Each request is a few small Claude calls (roughly a cent or two; more when it searches the web).
The hub allows 20 requests per minute per client. Requests run one at a time, and the last six
exchanges (within 10 minutes) are kept so follow-ups like "louder" work.
