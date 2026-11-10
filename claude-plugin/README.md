# TV Remote plugin for Claude

Run the TV by asking: "put on some jazz", "quieter", "pause it", "turn it off in half an hour",
"use the other TV". Claude works out what you mean; the hub does it and reports what the TV
confirmed. How that works, and its limits: [docs/CONVERSATION.md](../docs/CONVERSATION.md).

It adds:
- an MCP server (`server/tv_mcp.py`, Python standard library only) that passes the hub's tools
  straight through (`play`, `set_volume`, `set_power`, `playback`, `open_app`, `get_state`,
  `schedule`, ...) and adds `get_guide`: the hub's handbook plus your household's notes;
- a `tv-remote` skill and a `/tv-remote:tv <request>` command for Claude Code.

## Install

The hub must be running (`docs/SETUP.md`). On the computer that runs it nothing needs
configuring: the server talks to `http://127.0.0.1:8080` and reads the access key the hub wrote
to `~/.config/tvremote/hub.key`.

Claude Code:
```
/plugin marketplace add space-cowboy2/tv-remote
/plugin install tv-remote@tv-remote
```

Any other MCP client (for example a gateway that serves Claude on your phone): run
`python <clone>/claude-plugin/server/tv_mcp.py` as a stdio server. On Windows the hub installer
copies the server to `%LOCALAPPDATA%\tvremote\plugin`, and `install-hub.ps1 -Gateway <folder>`
sets a duet-gateway's `tv` upstream to it.

Windows: if `python3` is not on PATH, change `command` in `.mcp.json` to `python` or `py`.

## Settings (environment variables)

| Variable | Default |
|---|---|
| `TVREMOTE_HUB_URL` | `http://127.0.0.1:8080` |
| `TVREMOTE_HUB_KEY` | read from the key file |
| `TVREMOTE_HUB_KEY_FILE` | `~/.config/tvremote/hub.key` |

For a hub on another computer, set `TVREMOTE_HUB_URL` to its address and copy its key.

## When the hub is down

The tools are still listed (from `server/tools.json`), and every call answers
`hub_unreachable` with what to do, saying that nothing was sent to the TV.

`server/tools.json` and `server/guide.md` are copies of the hub's `tvremote/tools.json` and
`tvremote/guide.md`; a test fails when they drift. After changing the hub's files:

```bash
cp tvremote/tools.json tvremote/guide.md claude-plugin/server/
```
