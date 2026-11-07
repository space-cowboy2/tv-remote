# TV Remote plugin for Claude Code

Control the TV from Claude Code ("turn the volume down 5", "play <YouTube link>", "open
ohnepixel on Twitch", `/tv-remote:tv mute`). Claude Code is the brain, so it runs on your Claude
subscription; the hub needs no Anthropic API key for this.

It adds:
- an MCP server (`server/tv_mcp.py`, Python standard library only) exposing the hub's TV tools:
  `get_status`, `set_power`, `send_keys`, `type_text`, `list_apps`, `launch_app`, `play_youtube`,
  `open_twitch`, `skip_ad`, `run_macro`, `list_macros`, `save_macro`, `remember`, `wait`, `get_notes`;
- a `tv-remote` skill that tells Claude how to use them (native YouTube search, never cast,
  calibrate macros from what you see);
- a `/tv-remote:tv <request>` command.

Notes and macros live on the hub, so what Claude Code learns is shared with the Ask tab in the
phone and web apps.

## Install (any computer on your tailnet with Python 3.9+)

1. Put the hub key on that computer (skip on the Mac Mini, it's already there):
   ```bash
   mkdir -p ~/.config/tvremote && pbpaste > ~/.config/tvremote/hub.key && chmod 600 ~/.config/tvremote/hub.key
   ```
2. In Claude Code:
   ```
   /plugin marketplace add space-cowboy2/tv-remote
   /plugin install tv-remote@tv-remote
   ```
   (or `/plugin marketplace add ~/Projects/tv-remote-repo` from a local clone), then restart Claude Code.
3. Check: `/mcp` shows `plugin:tv-remote:tv-remote` connected. Ask "what's the TV doing?".

Windows: if `python3` isn't on PATH, install Python from python.org (tick "Add to PATH") or
change `command` in `.mcp.json` to `py`.

## Settings (environment variables)

| Variable | Default |
|---|---|
| `TVREMOTE_HUB_URL` | `https://dannys-mac-mini.tail24f732.ts.net` |
| `TVREMOTE_HUB_KEY` | read from the key file |
| `TVREMOTE_HUB_KEY_FILE` | `~/.config/tvremote/hub.key` |

## How it works

`GET /api/tools` on the hub returns the tool schemas (the same ones the hub's own assistant uses,
from `tvremote/assistant.py`) plus `notes.md`; `POST /api/tools/<name>` runs one. If the hub
can't be reached when Claude Code starts, the server lists tools from `server/tools.json` and each
call explains the problem (usually Tailscale is off).

After changing `TOOLS` in `tvremote/assistant.py`, regenerate the snapshot (a test checks it):
```bash
python -c "import json; from tvremote.assistant import TOOLS; json.dump([{k: t[k] for k in ('name','description','input_schema')} for t in TOOLS], open('claude-plugin/server/tools.json','w'), indent=1)"
```
