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
| Play a specific YouTube video | `ed.apps.launch` deep link `v=<id>` (Claude finds the id with web search) | Expected to work; confirm on this TV |
| Open a specific Twitch channel | Twitch on Tizen ignores deep links, so the hub launches Twitch and runs a blind search key sequence | Depends on the Twitch app layout; calibrate once (below) |
| Anything needing to read the screen ("is he live?", "pick the 3rd result") | not possible | — |

## Calibrating key sequences (macros)

Built-in macro `twitch_channel` (in `tvremote/assistant.py`): launch Twitch, wait 7 s,
LEFT (open side menu), UP ×6 (to Search), OK, type the channel + submit, wait 3 s, DOWN, OK.
If the Twitch app on the TV needs different presses, override it without changing code in
`~/.config/tvremote/macros.json` and restart the hub:

```json
{
  "twitch_channel": {
    "steps": [
      {"launch": "3202203026841"}, {"wait": 7},
      {"key": "KEY_LEFT"}, {"key": "KEY_UP", "repeat": 6}, {"key": "KEY_ENTER"}, {"wait": 1.5},
      {"text": "{value}", "submit": true}, {"wait": 3},
      {"key": "KEY_DOWN"}, {"key": "KEY_ENTER"}
    ]
  }
}
```

Step types: `launch` (app id), `wait` (seconds, ≤ 15), `key` (+ optional `repeat`),
`text` (`{value}` is replaced by the channel; `submit` presses the keyboard's search).

## Cost and limits

Each request is a few small Claude calls (roughly a cent or two; more when it searches the web).
The hub allows 20 requests per minute per client. Requests run one at a time, and the last six
exchanges (within 10 minutes) are kept so follow-ups like "louder" work.
