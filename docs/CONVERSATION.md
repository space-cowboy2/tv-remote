# Running the TV from a conversation

"Play jazz on YouTube", "make it quieter", "turn it off in half an hour", "use the other TV".
This page explains how such requests are carried out, what is checked, and what the hub
cannot know.

## Who does what

```
Claude (understands the request) ──MCP──► plugin (claude-plugin/) ──HTTP──► hub /api/tools ──LAN──► TV
```

- **The model interprets.** There is no phrase list anywhere. The model decides the outcome
  (something playing, quieter, off later) and calls tools. `tvremote/guide.md` is its
  handbook: it is served with the tool list and through the plugin's `get_guide` tool, along
  with the household's notes (`remember`).
- **The hub carries out whole jobs** and answers with evidence. One call to `play` does
  everything from choosing a video to starting it; the model never has to sequence key
  presses.
- **The TV supplies the evidence.** The hub reads every signal a Samsung TV offers and
  reports only what those signals show.

## Tools

| Tool | Does | Confirmed by |
|---|---|---|
| `play` | Picks one video (YouTube search on the internet), wakes the TV, restarts YouTube, opens its search box, types, starts the first result | Power state, app state, the TV's keyboard announcements and text echo. Playback only with the playback check on (below) |
| `search_youtube` | Lists videos (id, title, channel, length, live) so the model can choose by a constraint | Nothing touches the TV |
| `set_volume` | Absolute level, relative change, mute on/off | Level and mute read back from the TV (UPnP) |
| `set_power` | On / off | The TV's own PowerState |
| `playback` | Pause, resume, stop, rewind, forward | Nothing: keys are sent, the TV reports no playback state |
| `open_app` | Opens an app by the name people use | App state (REST or DIAL) where the TV gives one |
| `switch_input` | HDMI 1-4, TV, source list | Nothing: the TV does not report its input |
| `schedule`, `cancel_schedule` | Runs a tool later; timers live in the hub and survive a restart | The scheduled tool's own report, kept in `get_state` history |
| `get_state` | Power, volume, mute, YouTube running, active TV, what was last started, timers, recent actions | Read from the TV, plus the hub's own record |
| `find_tvs`, `pair_tv`, `use_tv` | Other TVs: what is on the network, pairing, switching | Pairing is confirmed by the TV accepting the hub |
| `playback_check` | Switches the read-only playback check on or off for a TV. Off by default | Kept in the hub's saved state |
| `send_keys`, `type_text`, `run_macro`, `save_macro`, `list_macros`, `wait`, `skip_ad`, `open_twitch`, `remember` | The plain remote, saved key sequences, notes | Sent only |

Old names (`get_status`, `launch_app`, `play_youtube`) still answer.

## Reports

Outcome tools answer with JSON:

```json
{"ok": true, "status": "partly_verified",
 "summary": "Started Jazz Music - Relaxing Cafe Mix on YouTube; playback itself is not confirmed.",
 "tv": "Living Room (QN55Q60B)",
 "verified": ["The TV is on (it reports PowerState on).", "YouTube is running (the TV reports it started).",
              "YouTube's search box opened (the TV announced its on-screen keyboard).",
              "The TV's search box holds the text 'Dx5qFachd3A'."],
 "not_verified": ["That the video is actually playing: the first search result was selected, and this TV reports no playback state."],
 "playing": {"id": "Dx5qFachd3A", "title": "Jazz Music - Relaxing Cafe Mix", "channel": "Cafe BGM", "length": "3:02:11", "live": false}}
```

`status` is `verified`, `partly_verified`, `sent_not_verified`, `partial` (part of the request
was not done, see `not_done`) or `failed`. A failure carries `failed.kind`, `failed.why`,
`failed.needs_user` (only a person can fix it: press Allow, pair a TV, power) and often a
`hint`. The hub never reports success because a command was sent.

Failure kinds: `no_tv_paired`, `unknown_tv`, `tv_unreachable`, `tv_did_not_wake`,
`tv_did_not_turn_off`, `tv_is_off`, `pairing_needed`, `pairing_denied`, `app_not_installed`,
`unknown_app`, `app_did_not_open`, `app_closed`, `search_box_not_found`, `nothing_found`,
`youtube_search_failed`, `volume_not_set`, `volume_level_unreadable`, `mute_not_set`,
`no_known_way`, `busy`, `timeout`, `unsupported`, `tv_error`. The plugin adds
`hub_unreachable`, `hub_rejected_key`, `no_hub_key`, `bad_request`.

## How `play` works

1. **Choose.** Search YouTube from the hub (`tvremote/youtube.py`, no API key), filtered by
   `prefer` (`long` for music and background, `live` for radio and channels), skipping Shorts
   and anything in `exclude` or recently started (`avoid_recent`). One video is settled on
   before the TV is touched.
2. **Decide what to type.** The TV's YouTube app can only be driven through its search box,
   which plays the first result. The hub checks which text brings the chosen video up first:
   its id, else its title. If neither does, the report says the TV may pick another upload.
3. **Wake** the TV if needed and wait for the TV to say it is on. `KEY_POWER` is a toggle, so
   it is only sent when the TV itself reports standby; otherwise Wake-on-LAN.
4. **Restart YouTube** so it starts from its home screen, and wait for the TV to report it up.
5. **Open the search box** with the saved sequence `youtube_open_search`, and wait for the TV
   to announce its on-screen keyboard. No announcement means the box did not open, so the hub
   tries again: the same keys where the app now stands (gets past "Who's watching?" and slow
   starts), then a clean start with a longer wait. After three tries it closes YouTube and
   fails with `search_box_not_found`.
6. **Type, submit, start the first result** (`youtube_pick_first`), and confirm the typed text
   through the TV's echo.

Limits: one request per TV at a time (a second one waits, then answers `busy`); the same
request inside 30 seconds is not run twice; everything stops at 170 seconds with `timeout`.

A TV that never announces its keyboard gets all three tries once; after that the hub does one
try and says the search box is unconfirmed.

## The playback check (optional, off by default)

The TV cannot say what is playing, but its YouTube app can: it tells a phone that joins it as
a remote which video is on and whether it is playing, paused or showing an ad. With
`playback_check` switched on for a TV, `play` ends by joining that way
(`tvremote/lounge.py`), listening for up to 20 seconds, and leaving again. The report then
says what the app said:

- the chosen video is running: `status: verified`, "Playing ...; the TV's YouTube app confirms it";
- another video is running: the report names that one as `playing`, keeps the hub's pick as
  `chosen`, and for an exact video request answers `partial`;
- an ad, a pause, nothing loaded, or no answer: said as that, and playback stays unconfirmed.

What it does and does not do:

- It is read-only. It joins without a video, so nothing is cast or queued, and it sends no
  play, pause, queue or volume command. The one request it may send asks the app to repeat
  its state. `tests/test_lounge.py` asserts what goes over the wire.
- It is one join and one leave per `play`. Nothing connects at any other time, and with the
  check off nothing connects at all.
- It goes through YouTube's servers with an id the TV hands out on the local network. It is
  not a published API and can stop working; then the report says the check could not ask.
- A TV that reacts badly to a remote joining (some Samsung sets open Multi View when a phone
  casts) should keep it off: `playback_check` with `on: false`.

**Status: built and tested against a stand-in only.** It has not been run against a real TV.
Whether the TV shows a notice when the hub joins, and whether the app announces its state
the way the client expects, is only known after a first run with someone watching the screen.

## What the hub cannot do, and says so

- **See the screen or playback.** No Samsung local API reports what is playing. Unless the
  playback check is on, `play` is never `verified`; the model is told to say "started, can't
  confirm it's playing".
- **Search inside Netflix, Prime Video and other apps.** Their keyboards are their own. `play`
  with another `service` opens the app and reports the search as not done. A sequence saved as
  `<app>_search` is used if the owner has worked one out.
- **Next / previous, captions, sound and picture settings.** There is no reliable key.
  `playback next` fails with `no_known_way` unless `playback_next` was saved.
- **Other brands.** `find_tvs` lists them as not controllable.
- **Purchases and account changes.** The guide forbids them unless asked for explicitly.
- **Casting.** The hub never casts. The playback check joins YouTube's remote link to listen, and starts nothing.

## Acceptance criteria

A request counts as handled when:

1. The model needed no step-by-step instructions and asked nothing it could decide itself.
2. Dependent actions ran in a working order (wake, open, search, start; play before volume
   before timer).
3. Every claim in the answer is backed by a `verified` line, and everything else is named as
   unconfirmed.
4. Foreseeable failures were retried within bounds and then reported with their kind, never
   as success.
5. The same request sent twice did not act twice.
6. Nothing was bought, and nothing outside the request changed.

## How this is tested

Simulated, on every run (`pytest`, a simulated TV with a model of YouTube's screens and a
clock that jumps instead of waiting):

| Area | File |
|---|---|
| Play: TV on, standby, off; stale app; account picker; slow start; wrong layout; no keyboard announcements; app missing; launch ignored; hanging endpoints; YouTube unreachable; exact link; title fallback; duplicates; "something else"; other services; deadline; busy | `tests/test_play.py` |
| Volume, mute, power, playback, apps, inputs, timers and their failures | `tests/test_controls.py` |
| State, follow-ups, several TVs, pairing, restarts, damaged files, no secrets in answers | `tests/test_context.py` |
| YouTube page reading, choice rules | `tests/test_youtube.py` |
| The playback check: off by default and silent; confirmed, another video, an ad, a pause, no answer, link down; kept per TV and over a restart | `tests/test_playback_check.py` |
| The read-only link's wire handling against a stand-in server: what is sent, that it leaves, that it gives up in time | `tests/test_lounge.py` |
| The plugin end to end over real HTTP and stdio | `tests/test_plugin.py` |

The simulation checks the hub's logic. It cannot show that a real TV reacts the same way.

Live, on the real TV:

```
tvremote selftest --host IP   # read-only, before pairing: which signals this TV answers
tvremote selftest             # the same for the paired TV, plus the remote-control channel and the app list
tvremote selftest --full      # volume down and up one, play, pause, resume; prints every report
tvremote now-playing --host IP   # one read-only question to the TV's YouTube app: what is on? (the playback check's link)
```

Even live, a person has to look at the screen once to confirm that the right video came up,
because the TV cannot tell the hub. The playback check can stand in for that look only after
it has itself been watched working once on that TV.
