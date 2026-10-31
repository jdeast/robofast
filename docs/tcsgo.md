# The DFM TCSGalil ("TCSGO") and the FLWO 60-inch

The FLWO 60-inch (Tillinghast) telescope, which feeds TRES, is run by a DFM
Engineering telescope control system, TCSGalil ("TCSGO"). robofast reaches
it through three modules in `robofast_core/devices/`:

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
- **Connections.** The legacy client opened one TCP connection per command,
  read the reply, and closed it. Its relay (`tcsserver.py`) serialized
  clients with a lock. robofast does the same: one connection per command,
  one command at a time.
- **Packets.** `#` + the command number + arguments, comma-separated, then
  `;` and a carriage return (0x0D): `#6,18.3457,35.3456,2000.0;\r`. All
  arguments are floating point; TCSGalil truncates where it needs integers.
- **Replies.** Only the read commands return data: `#v1,v2,...;`. The status
  example in the manual is `#58,0,30,0,12,94,24,0;` (no command number
  echoed). Every other command is documented "Data returned: NONE".
- **Errors.** The manual documents no error replies, no acknowledgement, and
  no behaviour for malformed or unknown commands. A command that cannot be
  carried out is visible only in status (for example the "target out of
  range" bit after `#6`). So robofast verifies the effect of a command by
  reading status, never by a reply.
- **Rate.** A legacy comment: "sending rapid fire commands seems to tank
  server (no more than 1 per second)", and "if status queries fail, reboot
  TCSGalil". robofast spaces commands `min_interval` apart (0.5 s in the
  example config) and caches status for `status_max_age`.

## Coordinates, units and modes

- **RA** in decimal hours, **Dec** in decimal degrees, **equinox** in decimal
  years. `#6`/`#3`: an equinox of 0 means "the current display equinox"
  (set with `#22`). robofast always sends an explicit equinox (2000.0).
- **`#25` returns mean RA/Dec in the display equinox** (with the equinox),
  the apparent hour angle, airmass, zenith distance, azimuth, local sidereal
  time, UT (hours) and the date (decimal years). The server converts RA/Dec
  to J2000 (FK5, astropy) if the display equinox is not 2000.
- **Azimuth.** For the dome the manual says north is 0 and azimuth increases
  to the east. For `#25`'s mount azimuth it says nothing; robofast *assumes*
  the same convention.
- **Offsets** (`#7`) are arcseconds on the sky, east = +RA, north = +Dec;
  TCSGalil divides the RA offset by cos(Dec).
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
  (`#10`, then a slew). **There is no "parked" bit in the status.**

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
| 7 | Offset target object | east ("), north (") | | target | yes (Offset action) |
| 8 | Target from library | object number | | target | no |
| 9 | Target from table | line (1-40) | | target | no |
| 10 | Target to zenith | | | target | yes (park_command "zenith") |
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
| 58 | Park mount | | | **yes** | yes |
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
  limit**, as status bits. What clears them is not documented.
- **Software limits**: "approaching" and "reached" soft limit bits, and the
  horizon check made before any motion: a target below the horizon sets
  "target out of range" and the mount switches to follow (does not slew).
- robofast adds its own `min_alt` check before `#6` (20° in the example
  config), refuses slews with the drives or amplifiers switch down or the
  Galil data stale, and reports every limit bit in `Action("Faults")`.

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
- `Park` = `#58` (or `#10` + `#12,2` with `park_command: "zenith"`);
  `Unpark` = `#59`. `AtPark` is true when the mount holds position within
  `park_tolerance` of (`park_alt`, `park_az`) and nothing has moved it since
  (the TCS has no parked bit).
- `PulseGuide` = `#61` (same direction numbering as ASCOM). The simulator
  divides the RA motion by cos(Dec) only when `#18` is on.
- **Guider offsets:** `Action("Offset", '{"east": E, "north": N}')` in
  arcseconds = `#7,E,N`, then `#12,2` (`offset_mode`) unless chasing. Bounded
  by `max_offset` (1000", the legacy `MAX_JOG`); refused unless tracking.
- `Action("Status")` (all flags plus coordinates) and `Action("Faults")`
  (drives, amplifiers, stale data, limits, out of range) return JSON.
- Focuser: `Position` = TCS microns − `focus_min`; `Move` = `#27`; no Halt.
- Dome (`"dome": true` only): shutters `#36`/`#44` in a configurable order,
  `SlewToAzimuth` = `#43` + `#20,2`, `FindHome` = `#20,3`, `Slaved` = `#20,1`
  (or `#35`), `AbortSlew` = `#35`, `#40`, `#41`.

### Safety

- The TCS address must be loopback (the simulator) unless the server config
  says `"allow_hardware": true` **and** the command line passes
  `--hardware`. The address is resolved once and only that literal is
  contacted.
- The client sends nothing outside `ALLOWED`. A query is retried at most
  `max_attempts` times; a set command is re-sent only if its connection
  never opened, and `#7` and `#61` (relative) never.
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

1. **Does TCSGalil reply to set commands** (any acknowledgement, echo or
   error text), and what does it do with an unknown or malformed command?
   The client tolerates a reply but does not need one.
2. **Is the carriage return required**, and does TCSGalil accept several
   commands on one connection? (The legacy client sent no CR and used one
   connection per command.)
3. **The real command rate limit.** Is "no more than 1 per second" real? The
   example config spaces commands 0.5 s apart.
4. **Is the 60-inch dome (rotation and shutters) wired to the TCS?** If so,
   is the shutter order constrained (upper before lower when opening, lower
   before upper when closing, as configured)? Does `#20` need the AUTO DOME
   switch up in normal operation?
5. **Is there any watchdog** that closes the dome if robofast (or its
   supervisor) dies? The TCS has no heartbeat command, unlike the Aqawan.
6. **Park or stow?** Should robofast park with `#58` (where is the 60-inch's
   park position?) or stow at zenith (`#10` + slew)? What alt/az should
   `AtPark` check? Does `#58` end in hold mode?
7. **Mirror doors** (`#29`): does the 60-inch have them under TCS control,
   and should robofast open them at the start of the night and close them
   with the dome?
8. **Offsets:** after `#7` in follow mode, should robofast send `#12,2`
   (slew to target, as now) or `#12,3` (chase)? Does a small `#7` while
   chasing move smoothly enough for TRES guiding?
9. **Guide rate:** what is the 60-inch's guide rate (`#15`), and is "use
   cos(Dec)" (`#18`) on? `PulseGuide` and `GuideRateRightAscension` assume
   5"/s and no cos(Dec).
10. **Focus range and direction:** the 60-inch's usable focus range in
    microns (`focus_min`/`focus_max`, now ±5000), and is "moving in"
    decreasing microns? Is there a documented focus stop (`#72` is not in
    the manual)?
11. **Azimuth convention of `#25`:** north = 0, east = 90 (assumed)?
12. **Display equinox:** what is it set to on the 60-inch (J2000, or the
    current date)? The server converts either way.
13. **Limits:** the 60-inch's horizon/HA/Dec limits, how a tripped travel
    limit is cleared, and what `min_alt` robofast should enforce.
14. **TCS restarts:** the legacy note says failed status queries meant
    restarting TCSGalil. Does that still happen, and should robofast alarm
    (it now reports a timeout) or attempt anything?
15. **Who else talks to the TCS** while robofast runs (the TRES guider, the
    observer's GUI, an ASCOM client)? TCSGalil also advertises itself as
    ASCOM Telescope/Focuser/Dome; would talking to that be preferable?
