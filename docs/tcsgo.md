# The DFM TCSGalil ("TCSGO") and the FLWO 60-inch

The FLWO 60-inch (Tillinghast) telescope, which feeds TRES, is run by a DFM
Engineering telescope control system, TCSGalil ("TCSGO"). robofast reaches
it through three modules in `robofast/devices/`:

- `tcsgo_client.py`: an asyncio client for TCSGalil's external command port
  (allowlist, hard timeouts, bounded retries, one command at a time);
- `tcsgo_sim.py`: a TCP simulator of that port (mount, focus, dome,
  shutters; fault injection), for tests and ConformU;
- `tcsgo_server.py`: an ASCOM Alpaca server over the client: `telescope/0`,
  `focuser/0` and, if enabled, `dome/0`. ConformU runs against it (backed by
  the simulator) in CI.

Sources: DFM's *TCSGalil Optimized 2.0 Operations Manual*, revision 1.3
(Appendix A, "External Commands"), and the TRES front-end code by J. Eastman
and J. Kansky (jdeast/tres-guider: `telescope.py`, `tcsserver.py`,
`tcsclient.py`, `dfmserver.py`), parts of which are adapted into robofast
(BSD-3) with Jan Kansky's agreement. Nothing here has been checked against
the real 60-inch TCS: robofast has only ever talked to the simulator.
Everything not in the manual is marked *assumed* or listed under
[Questions](#questions-for-jde).

## Transport and framing

- **Link.** TCSGalil listens for external commands on a serial port and/or a
  TCP port, each enabled in its Communications menu. The legacy TRES code
  used TCP. (A separate UDP link carries Galil sensor data to TCSGalil
  itself; it is not for clients.)
- **Connections.** The legacy clients open one TCP connection per command
  and close it. The relay (`tcsserver.py`) serializes them with a lock and
  keeps **one persistent connection** to TCSGalil, so TCSGalil takes many
  commands, one after another, on one connection. robofast opens one
  connection per command and sends one command at a time.
- **The relay is the TCS's only client.** When the new TRES front end was
  installed, the relay was added so that the front end (guiding offsets,
  focus) and the legacy TCS window could both command the TCS without
  colliding, and the TCS window was repointed from TCSGalil to the relay
  (JDE 2026-11-01). While either still runs, robofast must also go through
  the relay (its `tcs.host`/`tcs.port`), never straight to TCSGalil. Later,
  robofast can take the relay's place: a TCSGO-compatible port that
  serializes the legacy clients behind robofast's own ownership rules, so
  they move over one at a time.
- **Packets.** `#` + the command number + arguments, comma-separated, then
  `;` and a carriage return (0x0D): `#6,18.3457,35.3456,2000.0;\r`. All
  arguments are floating point; TCSGalil truncates where it needs integers.
  The CR is not required in practice: FLWO's legacy clients have long sent
  `;\n`, even `\n#26;\n`, straight to TCSGalil (the old status reader, the
  FAST guider). robofast sends the documented `;\r`.
- **Replies.** Only the read commands return data: `#v1,v2,...;`, ended by
  `;` with no newline. The status example in the manual is
  `#58,0,30,0,12,94,24,0;` (no command number echoed). Every other command
  is documented "Data returned: NONE". Production confirms it: the live
  relay reads a reply only for `#25`, `#26`, `#28`, `#32`, `#62` and `#64`
  over its one persistent connection. Any reply to a set command would be
  left unread and would desynchronise every later status read.
- **Errors.** The manual documents no error replies, no acknowledgement, and
  no behaviour for malformed or unknown commands. A command that cannot be
  carried out is visible only in status (for example the "target out of
  range" bit after `#6`). So robofast verifies the effect of a command by
  reading status, never by a reply.
- **Rate.** A legacy comment: "sending rapid fire commands seems to tank
  server (no more than 1 per second)", and "if status queries fail, reboot
  TCSGalil". Production queries much faster than that: the TCS window's
  status reader sends `#25`, `#26` and `#62` back to back every 2 s, and the
  TRES guider polls status every 0.3 s while it offsets. The FAST guider
  waits 1 s after each pulse ("slow things down"). The relay holds its lock
  for a whole `#61` pulse, so nothing else reaches TCSGalil during one.
  robofast keeps the conservative spacing: commands `min_interval` apart
  (0.5 s in the example config), with status cached for `status_max_age`.

## Coordinates, units and modes

- **RA** in decimal hours, **Dec** in decimal degrees, **equinox** in decimal
  years. `#6`/`#3`: an equinox of 0 means "the current display equinox"
  (set with `#22`). robofast always sends an explicit equinox (2000.0).
- **`#25` returns mean RA/Dec in the display equinox** (with the equinox),
  the apparent hour angle, airmass, zenith distance, azimuth, local sidereal
  time, UT (hours) and the date (decimal years). The server converts RA/Dec
  to J2000 (FK5, astropy) if the display equinox is not 2000.
- **Azimuth.** For the dome the manual says north is 0 and azimuth increases
  to the east. For `#25`'s mount azimuth it says nothing. robofast assumes
  the same convention, as the TRES guider's FITS headers do ("Deg E of N").
  Dome tracking slaves the dome to the mount inside TCSGalil, so the two
  very probably agree.
- **Offsets** (`#7`) are arcseconds on the sky, east = +RA, north = +Dec;
  TCSGalil divides the RA offset by cos(Dec). **robofast never sends `#7`.**
  FLWO's live TRES guider found it "broken on the DFM side" (its
  `dfm_telescope.py`, 2024) and offsets with a new `#6` target and chase
  instead (see "How robofast maps it to Alpaca").
- **Rates**: track rates in arcsec/s (positive HA rate is west, positive Dec
  rate north); guide and set (handpaddle) rates in arcsec/s; focus rates in
  percent of the maximum safe rate.
- **Focus** positions in microns, possibly negative (the manual's example
  reads -208).
- **Mount modes** (`#12`): 0 hold position, 1 follow target (open loop at the
  target's velocity: "turning on the drive"), 2 slew to target (fast, then
  Follow once within the capture threshold), 3 chase target (closed loop,
  continually; for moving targets). A new target (`#6`, `#7`, `#8`, `#9`,
  `#10`, `#57`) moves the mount only if it is already chasing; otherwise a
  `#12` is needed.
- **Park.** `#58` sends the mount to its park position, `#59` marks it
  unparked ("primarily an ASCOM convenience"), `#60` makes the current
  position the park position. The GUI also offers "Stow Mount at Zenith"
  (`#10`, then a slew). **There is no "parked" bit in the status.** FLWO's
  legacy software never uses `#58`. It stows at the zenith (`#10` + `#12,2`,
  dome to azimuth 235), and the live window then holds (`#12,0`) once the
  mount follows the zenith target. robofast does the same on the 60-inch.

## Commands

"Moves" marks commands that start, stop or redirect motion of the mount,
dome, shutters, focus or mirror doors. "robofast" says whether the client's
allowlist (`ALLOWED`) lets robofast send it. A ✓ under "reply" means a
query.

| # | Name | Arguments (units) | Reply | Moves | robofast |
|---|---|---|---|---|---|
| 2 | Initialize dome position | azimuth (deg; 360 = from the telescope) | | redefines | no |
| 3 | Initialize mount position (sync) | RA (h), Dec (deg), equinox | | redefines | no |
| 4 | Initialize instrument rotator | angle (deg) | | redefines | no |
| 5 | Initialize focus position | position (µm) | | redefines | no |
| 6 | Set target object | RA (h), Dec (deg), equinox | | target | yes |
| 7 | Offset target object | east ("), north (") | | target | no ("broken on the DFM side": offsets use `#6` + chase) |
| 8 | Target from library | object number | | target | no |
| 9 | Target from table | line (1-40) | | target | no |
| 10 | Target to zenith | | | target | yes (park_command "zenith", the 60-inch's) |
| 12 | Set mount mode | 0 hold, 1 follow, 2 slew, 3 chase | | **yes** | yes |
| 13 | Stop mount (hold) | | | **yes** | yes |
| 14 | Set track rates | HA, Dec, aux HA, aux Dec ("/s) | | rates | no |
| 15 | Guide rate | "/s | | config | no |
| 16 | Set rate | "/s | | config | no |
| 18 | Use cos(Dec) for the paddle | 1/0 | | config | no |
| 19 | Use rate corrections | 1/0 | | config | no |
| 20 | Dome mode | 0 manual, 1 track, 2 go to azimuth, 3 find home | | **yes** | yes (dome) |
| 22 | Display equinox | equinox | | config | no |
| 23 | Set table object | line, RA, Dec, equinox | | | no |
| 24 | Pointing coefficients | 17 terms | | model | no |
| 25 | Read mount coordinates | | ✓ 10 | | yes |
| 26 | Read TCS status | | ✓ 8 bytes | | yes |
| 27 | Move focus | position (µm) | | **yes** | yes (focuser) |
| 28 | Read point (pointing data) | | ✓ 6 | | no |
| 29 | Mirror doors | 1 open / 0 close | | **yes** | no (question) |
| 30 | Encoders for zenith | | | redefines | no |
| 31 | Encoders to defaults | | | redefines | no |
| 32 | Read target coordinates | | ✓ 10 | | yes |
| 35 | Dome stop rotation (to manual) | | | **yes** (stop) | yes (dome) |
| 36 | Dome upper shutter | 1 open / 0 close | | **yes** | yes (dome) |
| 37 | Initialize focus encoder (auto) | | | **yes** | no |
| 38 | Celestial trajectory | RA, Dec, equinox, rates, accelerations, start | | target | no |
| 39 | Mount trajectory | HA, Dec, rates, accelerations | | target | no |
| 40 | Dome stop upper shutter | | | **yes** (stop) | yes (dome) |
| 41 | Dome stop lower shutter | | | **yes** (stop) | yes (dome) |
| 42 | Pier side | 1 east / 0 west | | config | no |
| 43 | Dome azimuth target | azimuth (deg) | | target | yes (dome) |
| 44 | Dome lower shutter | 1 open / 0 close | | **yes** | yes (dome) |
| 50 | Start tracking | | | **yes** | yes |
| 51 | Tracking mode | 1 normal / 0 aux | | rates | no |
| 53 | Slew rate (paddle) | "/s | | config | no |
| 54, 55 | Slow / fast focus rate | % of max | | config | no |
| 56 | Initialize telescope alt/az | alt, az (deg) | | redefines | no |
| 57 | Target alt/az | alt, az (deg) | | target | no |
| 58 | Park mount | | | **yes** | yes (park_command "park"; not used on the 60-inch) |
| 59 | Unpark mount | | | state | yes |
| 60 | Set park position (here) | | | config | no |
| 61 | Pulse guide | direction 0 N, 1 S, 2 E, 3 W; duration (ms) | | **yes** | yes |
| 62 | Read dome azimuth | | ✓ 1 | | yes |
| 63 | Read rotator angle | | ✓ 1 | | no |
| 64 | Read focus position | | ✓ 1 (µm) | | yes |
| 66-70 | Pointing, rate, refraction, PEC corrections on/off | 1/0 | | model | no |
| 72 | *Stop focus* (undocumented; legacy code) | | | **yes** (stop) | no |
| 73 | *Dome continuous rotation* (undocumented; legacy code) | ±1 | | **yes** | no |

There are no commands 1, 11, 17, 21, 33, 34, 45-49, 52, 65 in the manual.

### Commands that move hardware or change TCS state

- **Move the mount:** `#12` (any mode but hold), `#50`, `#58`, `#61`, and
  `#7`/`#6`/`#8`/`#9`/`#10`/`#38`/`#39`/`#57` when the mount is already in
  chase mode (they move it at once). `#13` and `#12,0` stop it.
- **Move the dome:** `#20` (track, go to azimuth, find home), `#73`; `#35`
  stops it. `#20` works only with the front-panel AUTO DOME switch up.
- **Move the shutters:** `#36`, `#44`; `#40`, `#41` stop them.
- **Move the focus:** `#27`, `#37` (an automatic initialization run); `#72`
  stops it (undocumented).
- **Move the mirror doors:** `#29`.
- **Rewrite the TCS's idea of where things are** (never sent by robofast):
  `#2`, `#3`, `#4`, `#5`, `#30`, `#31`, `#56`.
- **Change configuration, the pointing model or rates** (never sent):
  `#14`-`#16`, `#18`, `#19`, `#22`-`#24`, `#42`, `#51`, `#53`-`#55`, `#60`,
  `#66`-`#70`.

### Commands the legacy 60-inch software sends

FLWO's legacy 60-inch control software (the observers' TCS window and its
status reader) talks to TCSGalil through the TRES relay. Reading it (with
thanks to FLWO) gives the commands robofast has to cover to replace it:

| # | Legacy use | robofast | Status |
|---|---|---|---|
| 3 | initialize mount position | never sent (sync belongs in TCSGalil's own GUI) | not needed |
| 6 | set target | `SlewToCoordinatesAsync` | covered |
| 10 | target zenith (stow; then `#12,0` hold once there; dome to 235) | `Park` with `park_command: "zenith"` (the flwo60 config), `dome_park_az: 235` | covered |
| 12 | 0 stop, 1 track, 2 slew | `#13`/`#12,0`, `Tracking`, `AbortSlew`, slews | covered |
| 15 | guide rate 2.0 "/s at startup, but only in the `ntcs_test` variant; commented out in the newest `ntcs.c` | not sent; `guide_rate: 2.0` in the config | open (question 9): the rate cannot be read back |
| 16 | set (paddle) rate | not sent | not needed (operator setting) |
| 18 | cos(Dec) for the paddle | not sent | not needed (question 9) |
| 20 | dome 0 manual, 1 track, 2 go to azimuth, 3 home | `Slaved`, `SlewToAzimuth`, `FindHome` | covered (`"dome": true`) |
| 25 | read coordinates | status | covered |
| 26 | read status | status, `Action("Status")`, `Action("Faults")` | covered |
| 43 | dome azimuth target | `SlewToAzimuth` | covered (`"dome": true`) |
| 57 | alt/az target | not allowed; no alt/az slews | gap (low): `SlewToAltAzAsync` = `#57` + `#12,2` |
| 62 | read dome azimuth | `Dome.Azimuth` | covered |

The legacy TCS window never sends `#7`. On-sky offsets come from the TRES
guider through the relay. Since 2024 the guider does not use `#7` either. It
sets a new target at the mount position plus the offset (`#6`), chases it
(`#12,3`), waits for capture, then follows (`#12,1`) and sets the old
target back. robofast's `Action("Offset")` does exactly this. FAST, the
60-inch's other instrument, has its own copy of the window and relay, and
its guider sends `#61` pulses straight to TCSGalil (question 15).
The rest of what that software controls is not in TCSGalil. The TRES
front end's calibration stage (`calstage` and its positions) is covered by
its own Alpaca server (`docs/tres_frontend.md`), and the calibration
lamps (`lamp`, on a Pulizzi power switch) by an Alpaca Switch server, with
`ARC` and `LAMP_FLAT` requests for the 60-inch (`docs/pulizzi.md`). The
atmospheric dispersion corrector (`adc_on`/`adc_off` and the legacy ADC
daemon, which took the telescope's position from the TCS window's status
reader) has its own Alpaca Rotator/Switch server that reads the position
from this server's Telescope instead, so it works with or without the
legacy software (`docs/adc.md`). The rest has no robofast service yet
(gaps, not built): the spectrograph bench (calibration box, fiber
selector, bench focus).

## Status (`#26`)

Eight unsigned bytes; bit 0 is the least significant.

| Byte | Bits 0-7 |
|---|---|
| 0 front panel | drives switch up, track, aux track, dome track, auto dome, amplifiers, -, - |
| 1 hand paddle | N, S, E, W, set, slew pressed, ASCOM pulse guiding, - |
| 2 limits | horizon, east, west, north, south travel limit, target out of range, approaching soft limit, reached soft limit |
| 3 dome | find-home mode, go-to-azimuth mode, track mode, rotating left, rotating right, at home, manual mode, - |
| 4 focus, rotator | focus moving in, moving out, at outer limit, at inner limit, rotator CCW, rotator CW, -, - |
| 5 comms, options | serial ready, TCP connected, use cos(Dec), use rate corrections, east-side pier, west-side pier, Galil data stale, - |
| 6 mount mode | chase, slew, follow, hold, target captured, -, -, - |
| 7 shutters | upper open, upper closed, lower open, lower closed, mirror doors open, mirror doors closed, -, - |

"Target captured" is 0 throughout a slew; when the mount reaches the target
it switches to follow and the bit becomes 1. In the manual's example
(`#58,0,30,0,12,94,24,0;`) byte 6 is 24: hold mode with the target
captured.

## Limits

- **Hardware travel limits** (east, west, north, south) and a **horizon
  limit**, as status bits. What clears them is not documented. They are
  understood to be trip wires that stop the mount before damage and lock
  out software control until an operator resets them. No FLWO client code
  reads or clears them (question 13).
- **Software limits**: "approaching" and "reached" soft limit bits, and the
  horizon check made before any motion: a target below the horizon sets
  "target out of range" and the mount switches to follow (does not slew).
- robofast adds its own `min_alt` check before `#6` (20° in the example
  config, the same as the legacy window's minimum elevation for alt/az moves), refuses slews with the drives or amplifiers switch down or the
  Galil data stale, and reports every limit bit in `Action("Faults")`.
- While any travel or horizon limit bit is set, robofast refuses every
  motion (slew, tracking on, park, shutter open) and never tries to clear
  it: the night needs an operator. The "soft limit" bits alone do not
  refuse (the TCS holds the mount itself).
- robofast has **no HA/Dec envelope of its own yet**: the 60-inch's
  hour-angle and declination limits are unknown (question 13), and the
  simulator's (HA +-6 h, Dec -40..89.5) are placeholders. Its own envelope,
  inside the trip wires, is to be added once they are known.

## How robofast maps it to Alpaca

Detailed in `tcsgo_server`'s module doc. In short:

- `SlewToCoordinatesAsync` = `#6` + `#12,2`, after robofast's checks, and
  only if `#26` does not report "target out of range" after the `#6`.
  Synchronous slews, sync, MoveAxis and alt/az slews are not offered.
- `Slewing` = mode "slew", or "chase" not yet captured (or a slew/park
  commanded within `slew_report_min`).
- `Tracking` = mode follow, chase or slew; setting it sends `#50` or `#13`.
- `AbortSlew` = `#12,1` (follow from here: stops the slew, keeps tracking),
  or `#13` while parking.
- `Park` with `park_command: "zenith"` (the 60-inch, as the legacy stow):
  `#10` + `#12,2`, then `#12,0` (hold) once the mount follows the captured
  zenith target within `park_tolerance` (bounded by `stow_timeout`). With
  `"dome": true` and `dome_park_az` set, also `#43` + `#20,2`.
  `park_command: "park"` sends `#58` instead. `Unpark` = `#59`. `AtPark` is
  true when the mount holds position within `park_tolerance` of
  (`park_alt`, `park_az`) and nothing has moved it since (the TCS has no
  parked bit). Any new motion command (slew, abort, tracking, offset)
  supersedes a pending stow, which then sends nothing more.
- `PulseGuide` = `#61` with the manual's directions (0 N, 1 S, 2 E, 3 W, the
  same as ASCOM). **Caution:** FLWO's FAST guider sends east as 3 and west as
  2 (commented "rev e-w also"). That probably compensates its camera's
  orientation. Nothing shows TCSGalil itself reversed, so robofast keeps
  the manual's mapping. Check E/W on the sky before trusting PulseGuide on
  the 60-inch. The simulator divides the RA motion by cos(Dec) only when
  `#18` is on.
- **Guider offsets:** `Action("Offset", '{"east": E, "north": N}')` in
  arcseconds, as the live TRES guider's `jog()` does it, never with `#7`:
  1. Read the target (`#32`, kept to restore) and the mount (`#25`).
  2. Set a new target at the mount position plus the offset (RA by
     E/cos Dec, in the `#25` equinox) with `#6`.
  3. Chase it (`#12,3`). The request returns here.
  4. In the background, once captured (or after `offset_capture_timeout`,
     10 s like the guider), follow (`#12,1`) and set the old target back
     with `#6`. A new target in follow mode does not move the mount.

  `Slewing` is true until capture. An offset sent while one is pending
  re-aims from the current mount position and keeps the original target.
  If the TCS flags the new target out of range, the old target is set back
  and the offset is refused. Bounded by `max_offset` (1000", the legacy
  `MAX_JOG`); refused unless tracking.
- `Action("Status")` (all flags plus coordinates) and `Action("Faults")`
  (drives, amplifiers, stale data, limits, out of range) return JSON.
- Focuser: `Position` = TCS microns − `focus_min`; `Move` = `#27`; no Halt.
- Dome (`"dome": true` only): shutters `#36`/`#44` in a configurable order,
  `SlewToAzimuth` = `#43` + `#20,2`, `FindHome` = `#20,3`, `Slaved` = `#20,1`
  (or `#35`), `AbortSlew` = `#35`, `#40`, `#41`.

### Safety

- The TCS must be a verified simulator unless the server config says
  `"allow_hardware": true` **and** the command line passes `--hardware`
  ([the hardware gate](design.md#the-hardware-gate): loopback alone is not
  enough, since an SSH tunnel puts the real TCS on 127.0.0.1). The address is resolved once and only that literal is
  contacted.
- The client sends nothing outside `ALLOWED`. A query is retried at most
  `max_attempts` times; a set command is re-sent only if its connection
  never opened, and `#61` (relative) never. `#7` is not in `ALLOWED`.
- Urgent commands (`#13`, `#35`, `#40`, `#41`, `#58`) go ahead of queued
  routine ones and cut short a query in flight (which is then re-queued).
- Since set commands have no reply, a TCS that accepts the connection and
  then ignores the command looks exactly like success. The server reads
  status back where it matters (after `#6`, for `Slewing`, `AtPark`,
  `ShutterStatus`), and the safety supervisor treats a dome that does not
  reach "closed" within `shutter_timeout` as an error.

## Notes on the legacy TRES code

Things found while reading `telescope.py` (jdeast/tres-guider), so they are
not carried over:

- `#18`, `#19`, `#36`, `#66` were sent without the comma (`#361;` instead of
  `#36,1;`), i.e. as commands 181, 191, 361, 661.
- "Actuate lower shutter" sent `#43` (set dome azimuth target) instead of
  `#44`.
- No carriage return after `;`, no socket timeout (a silent TCS hung the
  caller), and `sys.exit()` on a refused connection.
- `slew()` waited *while* the target was captured (inverted), referenced
  undefined names, and was marked untested.
- Focus rates were sent as fractions (0.01-1); the manual says percent.
- `#8` was sent without its object number.
- The `MAX_JOG` check on offsets logged an error but sent the offset anyway.

## Questions for JDE

JDE (2026-11-23): "we have access to the entirety of the code", so these
were answered from FLWO's own 60-inch code wherever it settles them. That
code is the legacy TCS window `tres-tcs` (`win.c`, `ntcs.c`, `rtcs.c`,
`sockhelp.c`), the live TRES guider `tres-guider` (`dfm_telescope.py`,
`tres_guider.py`) with its relay `tcsserver.py`, and the FAST guider
`fast-guider` (`comedi_1731.c`). It is summarised here, not copied. Nobody
has read TCSGalil's own source: only DFM has it. Each item is marked
**answered**, **partly** or **not settled**, and the "check" lines need no
motion: they only read status that the relay already polls.

1. **Does TCSGalil reply to set commands?** **Answered: no.** The live relay
   reads a reply only for the query commands, over one persistent
   connection, and an unread reply would desynchronise every later status
   read. The 2021 relay read after every command and would have hung; the
   rewrite stopped doing that. Every legacy client sends set commands
   without reading. *Not settled:* what TCSGalil does with an unknown or
   malformed command (no client tests it). The client already tolerates an
   unexpected reply and logs it.
2. **Is the CR required? Several commands per connection?** **Answered: CR
   not required; several commands per connection, yes.**
   - The old status reader, the `talkdfm` test tool and the FAST guider
     sent `;\n` (even `\n#26;\n`) straight to TCSGalil.
   - Replies end in `;` (the legacy reader's comment: "DFM needs 59").
   - The relay sends every client's commands down one persistent
     connection.

   *Not settled:* several commands in a single write, and whether the live
   relay passes robofast's `\r` through unchanged (read its source before
   the first real connection).
3. **Real rate limit.** **Partly.** There is no hard 1 command/s limit for
   queries. The status reader sends three queries back to back every 2 s;
   the TRES guider polls `#26` every 0.3 s and `#64` every 0.1 s. The
   "1 per second" is a single comment, and FAST waits 1 s after each pulse.
   robofast keeps `min_interval: 0.5` (conservative). Fast bursts of *set*
   commands are untested; TCSGalil's communication log (ASCOM Interfaces
   window) would show what a normal night sends.
4. **Is the 60-inch dome wired to the TCS?**
   - **Rotation: answered, yes.** The window moves the dome only with
     `#43` + `#20,2`, `#20,3`, `#20,0/1`, reads `#62`, and takes "dome
     moving" from `#26` byte 3. The old dome-controller serial code is not
     built.
   - **Shutters: partly, probably not.** No code sends `#36`/`#44`. The
     only slit control in the live window is a single open/close through a
     separate web relay (its button is disabled), so the shutter order is
     moot. Check: `#26` byte 7 while the observers open the slit.
   - **AUTO DOME:** `#20` needs the switch up (manual), and the window uses
     `#20` routinely, so it is normally up. Check: `#26` byte 0 bit 4.

   `"dome"` stays `false` until confirmed.
5. ~~Is there any watchdog~~ **Answered** (JDE, 2026-10-31): none he is
   aware of. So the 60-inch runs **attended only**: its supervisor
   (`safety.attended_only` in `sites/flwo60/flwo60.json`) grants no lease,
   and the dome never opens, unless an operator is on duty through the
   observer console, renewed while the console is open; an expired duty
   alarms and then closes (`docs/console.md`, "Operator duty"; design.md
   3.3).
6. **Park or stow?** **Answered: stow at zenith.** No FLWO code ever sends
   `#58`/`#59`/`#60`. The window's stow is `#10` + `#12,2` (and "Stow All"
   sends the dome to azimuth 235). The live window then waits for follow
   mode and holds (`#12,0`); it also has an "equatorial stow" (RA = LST,
   Dec 0). The flwo60 config now uses `park_command: "zenith"`
   (`park_alt` 90) and `dome_park_az: 235`. *Not settled:* where `#58`
   would park and whether it ends in hold. Nothing needs them now.
7. **Mirror doors (`#29`).** **Not settled.** No code sends `#29` or reads
   the door bits. Check: `#26` byte 7 bits 4/5. Both always 0 means no
   door sensors on the TCS. robofast keeps `#29` out of `ALLOWED`.
8. **Offsets.** **Answered: neither.** The live TRES guider (2024) gave up
   `#7` as "broken on the DFM side". It sets a new target at mount +
   offset with `#6`, chases (`#12,3`), waits for capture, follows
   (`#12,1`) and restores the target. Its 2021 version had used `#12,3` +
   `#7`, and a pulse-guide variant "doesn't seem to work". robofast now
   does the same and `#7` left `ALLOWED`. Accepted in production as smooth
   enough for TRES, whose fast loop is the tip/tilt mirror (the mount only
   takes offloads).
9. **Guide rate and cos(Dec).**
   - **Guide rate: not settled.** TCSGalil has no command to read it. The
     `ntcs_test` variant of the window sends 2.0"/s at startup; the newest
     `ntcs.c` has that line commented out, and which binary runs today
     differs from the repository. The manual recommends 3-10"/s and says
     TCSGalil restores its settings at restart.
   - **cos(Dec): answered, read it at run time** from `#26` byte 5 bit 2
     (`Action("Status")` reports it). The window assumes it off and only
     toggles it.

   The TRES guider never pulse-guides; FAST does, with east and west
   swapped relative to the manual (probably its camera orientation;
   robofast keeps the manual's directions).
10. **Focus range and direction.** **Partly.** The guider's focus sweeps
    stay within -1000…+1000 µm (default -100…+100), and the manual's
    examples read about -200 µm. So ±5000 is generous but harmless; the TCS
    reports its own focus limit bits. Direction, and `#72` (present only in
    uncalled client code): *not settled*. Check: log `#64` against `#26`
    byte 4 bits 0/1 during a guider focus sweep.
11. **Azimuth convention of `#25`.** **Partly: north 0, east 90**, as the
    TRES guider labels it in FITS headers and as the dome uses (the
    manual). Check: compare `#25` azimuth with the one computed from its own
    HA/Dec.
12. **Display equinox.** **Answered: J2000 (very probably).** The live
    guider's offsets take RA/Dec from `#25` (display equinox) and send them
    back with `#6` at equinox 2000. With an equinox of date every offload
    would jump about 20'. Check: field 4 of `#25` (or `TEPOCH` in any TRES
    header). The server converts either way.
13. **Limits.** **Partly.** The only limit in FLWO's client code is the
    window's 20° minimum elevation for alt/az moves, which matches
    robofast's `min_alt`. No client holds HA/Dec limits, or reads or clears
    the limit bits. The 60-inch's HA/Dec limits and the reset procedure for
    a tripped travel limit are *not settled* (TCSGalil's configuration, the
    mountain staff).
14. **TCS restarts.** **Partly.** The code expects them (guider comment:
    "if status queries fail, reboot TCSGalil, then reinitialize telescope").
    No client recovers: the status reader logs and carries on, and the
    guider exits. The 2021 relay never reconnected to TCSGalil. robofast
    reports a timeout and reconnects per command. It never restarts or
    reinitialises anything (`#3`/`#30`/`#56` are excluded). How often it
    still happens: *not settled*.
15. **Who else talks to the TCS.** **Partly.** Besides the TRES window and
    guider through the relay:
    - FAST (the 60-inch's other instrument) runs its own copy of the window
      and status reader, with its own relay.
    - The FAST guider sends `#61` straight to TCSGalil, bypassing any relay
      (2022 code).
    - A `talkdfm` test tool queries TCSGalil directly.
    - A site script that publishes telescope state may poll it (not
      readable).
    - TCSGalil's own ASCOM Telescope/Focuser/Dome and serial port may have
      clients (`#26` byte 5 bits 0/1 and byte 1 bit 6 hint at them).

    Check: TCSGalil's ASCOM Interfaces window and communication log.
