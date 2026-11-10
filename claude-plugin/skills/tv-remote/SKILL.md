---
name: tv-remote
description: Run the household's Samsung TV from ordinary requests - find and play something, power, volume, mute, pause and seek, apps, inputs, timers, follow-ups like "quieter" or "the other TV". Use whenever the user wants something to happen on a TV.
---

# TV remote

The `tv-remote` MCP tools reach the TV through the TV Remote hub. You work out what the person
wants; the tools do whole jobs and report what the TV confirmed.

1. Once per conversation, before the first TV action, call `get_guide`. It returns the hub's own
   guide for handling requests plus this household's notes (preferences, quirks of this TV). It
   is the reference; what follows is only the outline.
2. Use the tool that does the whole job: `play` (find and start something; turns the TV on by
   itself), `set_volume`, `set_power`, `playback`, `open_app`, `switch_input`, `schedule`.
   `get_state` answers "what's on / is it on / which TV" and gives follow-ups their context.
3. Choose sensible defaults and act. Ask only when a report says `needs_user`, or two TVs are
   equally likely.
4. Answer in one or two short sentences from the report: what was done, what the TV confirmed,
   and what could not be confirmed. The TV reports no playback state, so never state that
   something is playing as a fact. If a call failed, say what failed.
5. Never buy, rent, subscribe or change accounts on the TV unless asked for exactly that.

If the tools answer `hub_unreachable`, the hub is not running on its computer (or this computer
cannot reach it); say so instead of retrying.
