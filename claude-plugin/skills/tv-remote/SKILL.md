---
name: tv-remote
description: Control Danny's Samsung TV (power, volume, keys, apps, YouTube videos, Twitch channels, saved macros). Use whenever the user asks to do something on the TV.
---

# TV remote

The `tv-remote` MCP tools drive the TV through the hub on the Mac Mini. The hub can't see the
screen: every tool reports what it sent, not what the TV shows.

1. Unless it's a single key press, call `get_notes` first. It holds what's been learned about this
   TV and Danny's preferences, and it beats anything below.
2. If the TV is off (`get_status`) and the request needs it on, `set_power` on first.
3. Pick the tool:
   - Volume, mute, navigation, playback, inputs: `send_keys` (use `repeat`, e.g. KEY_VOLDOWN x5).
   - Open an app: `list_apps` for the id, then `launch_app`.
   - A YouTube video: `play_youtube` with a URL, id, or title. Never cast; it opens Samsung Multi View.
   - A vague YouTube ask ("play some jazz", "something chill"): `search_youtube` once (for music,
     "<genre> music mix" or "<genre> radio live"), pick one result that is live or over an hour
     from a popular channel, then `play_youtube` its id. Don't ask which one; say what you picked.
   - A Twitch channel: `open_twitch` with the login name. Its key sequence is not calibrated yet.
   - Skip a YouTube ad: `skip_ad`, only when asked (it pauses the video if no ad is showing).
   - Anything else: `launch_app`, then `send_keys` / `type_text`, with `wait` while apps load.
4. Reply in one short sentence: what you did, and what Danny should check on screen.

## Learning

When Danny says what the screen actually did ("it stopped on the search box", "needs one more
Up"), fix the sequence: `list_macros`, adjust the steps, `save_macro` under the same name. Save
lasting facts and preferences with `remember` (one short line). Both persist on the hub and are
shared with the phone and web apps.

## Errors

- "can't reach the hub": Tailscale is off on this computer, or the hub is down.
- "hub rejected the key": the key in `~/.config/tvremote/hub.key` is out of date.
- "TV is off or unreachable" / "did not wake": turn it on by hand, or enable Power On with Mobile.
