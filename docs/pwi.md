# PlaneWave PWI server (CDK700 mounts)

robofast's Alpaca server for a PlaneWave CDK700 driven by PlaneWave's PWI:
`robofast/devices/pwi_server.py` (Telescope + Focuser + Rotator),
over `pwi_client.py` (two backends), with simulators of both PWI HTTP APIs
(`pwi_sim.py`) and a small-move tool for the first hardware session
(`pwi_jog.py`, `robofast-pwi-jog`). ConformU runs against the server on both
simulators in CI (`scripts/conformu_pwi.py`).

| Backend | Program | HTTP | Reply |
|---|---|---|---|
| `pwi4` | PWI4 (`PWI4.exe`) | `GET /<device>/<command>?k=v`, port 8220 | `key=value` lines |
| `pwi2` | PWI2 (`PWI.exe`, MINERVA today) | `GET /?device=<d>&cmd=<c>&k=v`, port 44444 | `<system>` XML |

Sources: the legacy MINERVA driver, `MinervaCollaboration/minerva-control`
`minerva_library/cdk700.py` (PWI2; what MINERVA has used for years), and
its PWI4 port, `minerva_library/pwi4.py` and `docs/pwi4.md` (whose endpoint
list was checked against PlaneWave's reference client and the PWI4 4.1.4
binary). No PlaneWave code is copied here. Both APIs answer every command
with the whole status, so every command is also a status read.

## Devices

All three are device number 0 on one server (`/api/v1/telescope/0`,
`/api/v1/focuser/0`, `/api/v1/rotator/0`).

* **Telescope** (ITelescopeV4, alt-az, J2000): `SlewToCoordinatesAsync`
  (tracking on), `SlewToAltAzAsync` (tracking off), `AbortSlew`, `Park`
  (alt/az slew to `park_alt`/`park_az`, then tracking off), `Unpark`,
  `FindHome` (only with `allow_find_home`; see "Homing"), `Tracking`, `PulseGuide` (as a PWI RA/Dec offset of
  `guide_rate` arcsec/s, RA in coordinate arcsec). No sync, no MoveAxis.
* **Focuser** (absolute, microns): the focuser **on the current M3 port**.
  `Move` refused outside [`focus_min`, `focus_max`] or while the M3 moves.
* **Rotator**: the rotator on the current M3 port, mechanical degrees
  (`Sync`/`Reverse` only change `Position`, in memory). With PWI4 it reads
  only while the M3 is on `rotator_status_port` (below).

Actions on the telescope:

| Action | Parameters | Does |
|---|---|---|
| `SelectPort` | `"1"`, `"2"` or a `ports` name (`"imager"`, `"fau"`) | M3 to that port; waits until PWI reports it (`m3_timeout` in all; then `m3 stop` and an error). A goto PWI ignored (the reported port unchanged after `m3_resend_after` s) is sent again, at most `m3_retries` times. The sequencer's `port_action`. |
| `Offset` | `{"east": E, "north": N}` arcsec on the sky | RA/Dec offset (RA `E / cos Dec`); tracking; at most `max_offset` |
| `MountConnect` | | PWI connects to the mount controller (moves nothing) |
| `MotorsEnable` / `MotorsDisable` | | both axis motors |
| `FindHome` | `"both"`, `"0"` (azimuth) or `"1"` (altitude) | home; only with `allow_find_home`; one axis needs PWI4 4.1.7+ |
| `Stop` | | mount stop; works without `Connected` |
| `Status` | | JSON: PWI's typed status and the server's state |

## Command mapping

| Operation | PWI4 request | PWI2 request | Confirmed? |
|---|---|---|---|
| status | `/status` | `/?cmd=getsystem` | both: legacy |
| mount connect | `/mount/connect` | `device=mount&cmd=connect` | both: legacy |
| enable motors | `/mount/enable?axis=0`, `axis=1` | `device=mount&cmd=enable` | both: legacy (`axis=-1` rejected by PWI4 4.1.4) |
| disable motors | `/mount/disable?axis=0`, `axis=1` | `device=mount&cmd=disable` | both: legacy |
| find home (both axes) | `/mount/find_home` | `device=mount&cmd=findhome` | both: legacy |
| find home (one axis) | `/mount/findhome?axis=0\|1` | (none: PWI2 homes both) | PWI4 changelog, 4.1.7 beta 3 ("Add axis parameter to /mount/findhome"); **path spelling and values unconfirmed** |
| goto J2000 | `/mount/goto_ra_dec_j2000?ra_hours=&dec_degs=` (decimal) | `device=mount&cmd=move&ra2000=&dec2000=` (decimal hours/deg) | PWI4: legacy; PWI2: legacy (docstring says decimal or sexagesimal) |
| goto alt/az | `/mount/goto_alt_az?alt_degs=&az_degs=` | `device=mount&cmd=move&alt=&azm=` | both: legacy (park) |
| RA/Dec offset | `/mount/offset?ra_add_arcsec=&dec_add_arcsec=` | `device=mount&cmd=move&incrementra=&incrementdec=` | legacy; **sign conventions not confirmed on the sky** |
| stop | `/mount/stop` | `device=mount&cmd=stop` | both: legacy |
| azimuth wrap range | `/mount/set_axis0_wrap_range_min?degs=` (status `mount.axis0_wrap_range_min_degs`, `mount.axis0.position_degs`) | (none) | PWI4 changelog; **path, parameter and status keys unconfirmed** (see "Azimuth wrap") |
| load pointing model | (none: robofast sends PWI4 no model request) | `device=mount&cmd=setmodel&filename=` | PWI2: legacy (`cdk700.startPWI`) |
| tracking on/off | `/mount/tracking_on`, `/tracking_off` | `cmd=trackingon` / `trackingoff` | both: legacy |
| M3 port | `/m3/goto?port=1\|2`, `/m3/stop` | `device=m3&cmd=select&port=`, `cmd=stop` | both: legacy |
| focuser goto/stop | `/focuser/goto?target=`, `/focuser/stop` (GUI-selected focuser) | `device=focuserN&cmd=move&position=`, `cmd=stop` | legacy |
| rotator goto/stop | `/rotator/goto_mech?degs=`, `/rotator/stop` (GUI-selected rotator) | `device=rotatorN&cmd=move&position=`, `cmd=stop` | legacy |

Nothing else can be sent (`PWI4_PATHS`, `PWI2_COMMANDS` in
`pwi_client.py`): no sync, no pointing-model save/add (and no load but
PWI2's `setmodel`, below), no jog, no
disconnect, no PWI process control, no focuser/rotator homing, no
derotation (`goto_field` / `derotatestart`; a later step).

Status fields read (`PWIStatus`): PWI4 `mount.is_connected`,
`mount.axis0/1.is_enabled` (az/alt), `mount.is_slewing`, `mount.is_tracking`,
`mount.altitude_degs`, `mount.azimuth_degs`, `mount.ra/dec_apparent_*`,
`mount.ra/dec_j2000_*`, `site.lmst_hours`, `m3.port` (0 = moving),
`focuser.*`, `rotator.mech_position_degs`; PWI2 `mount.connected`,
`azm_enabled`, `alt_enabled`, `moving`, `tracking`, `alt_radian`,
`azm_radian`, `ra_radian`, `dec_radian`, `ra_2000`, `dec_2000`,
`is_finding_home`, `encoders_have_been_set`, `*_motor_error_code`,
`status.lst`, `m3.port`, `m3.moving_rotate`, `focuserN.*`, `rotatorN.*`.
PWI4 also: `pwi4.version`, and `mount.axisN.is_homed` where present (PWI4
4.1.4 had none, and the changelog documents no homed status key; PWI4
4.1.2+ "remembers if [a CDK700 has] been homed since the last power
cycle", but 4.1.6 reports nothing of it over HTTP: see "The home record").

Quirks handled:

* **PWI2 `mount.moving` is `True` whenever tracking is off** (legacy
  `cdk700.isMoving`), so it means "slewing" only while tracking. For an
  untracked move the server also counts the mount as slewing while its
  position changes faster than `moving_speed` (deg/s) between reads.
* **PWI2 `mount.moving` can stick `True`** (MINERVA T1 after a PWI2
  restart, 2026-11-07: `True` while idle, and after a slew had arrived).
  So `moving` alone never decides a slew: a slew the server sent is
  complete once the mount is within `slew_tolerance` (default 0.1 deg) of
  its target (RA/Dec, or alt/az and mechanical azimuth) and not moving
  faster than `moving_speed`, whatever PWI reports; and when the stall
  timer runs out with PWI2 reporting `on_target` (what the legacy driver
  waited for), that too is a finished slew, not a stall. With no slew of
  ours under way, PWI2's `moving` without any change of position is not
  slewing. The simulator's `stuck_moving` (`--stuck-moving`) reproduces it.
* **PWI2 can report tracking on during an alt/az move, and tracking off
  then halts the move midway** (MINERVA T1, 2026-11-08: the rehearsal's
  end action stopped 15 deg short). A tracking off asked during an alt/az
  slew of ours is sent once the mount is on the target. PWI2's alt/az move
  and its home also leave the mount tracking (MINERVA T1, 2026-11-09:
  `Tracking` read True after a `SlewToAltAzAsync` sent with tracking off);
  ASCOM wants no tracking after `SlewToAltAz`, so the server sends tracking
  off once an alt/az slew of ours (or a home) is done. The simulator's
  `altaz_tracks` reproduces both.
* **PWI2 can report a tracked mount not moving for a moment after a
  goto** (night 1107; night1108: 4.6 s from goto to on target). A slew the
  server sent stays its slew (`Slewing` true, its target known to the limit
  watch) until the mount is on the target. A PWI2 that says the slew is done
  (or, untracked, cannot say) while the mount stands still off its target
  for `slew_arrive_timeout` (30 s) has failed the slew: the server stops
  the mount and `Slewing` raises the reason.
* **PWI2 decides `moving` from the mount's servo residuals.** A noisy mount
  (windshake, a poorly tuned servo) can read `moving` False and then True
  again on its target: legacy `cdk700.inPosition` waited 1 s after
  `moving` went False, read again, and logged "Telescope moving after it
  said it was done". So a PWI2 slew of ours is complete only when the mount
  is on its target and still **and** PWI2 has read done (`moving` False, or
  `on_target` True) on reads `slew_settle` (1 s) apart with no `moving`
  True between. Each `moving` True after a done read is logged (a warning
  with PWI2's rms residuals) and counted (`moving_flickers` in
  `Action("Status")`); a mount whose flag keeps flickering on its target is
  done after `slew_noisy_time` (10 s) on it, with a warning -- never a
  stall. A slew during which PWI2 reports a motor (drive) error code
  (`alt_motor_error_code`/`azm_motor_error_code` not 0) fails at once: the
  server stops the mount and `Slewing` raises the code and message, as
  legacy `inPosition` returned False on one. The simulator's
  `moving_flicker` and `motor_errors` reproduce both.

  **Tuning:** the threshold is PWI2's own setting "MAX RMS Encoder Error
  for Goto" (PWI2, Mount tab; legacy cdk700.py, JDE 2015-10-06). PWI2's HTTP
  API does not report it, so robofast cannot read it: `Action("Status")`
  reports what it is compared with, PWI2's `rms_error_arcsec` (`alt`, `az`;
  `mount.alt_rms_error_arcsec`, `mount.azm_rms_error_arcsec`). If
  "moving again after it read done" warnings or non-zero `moving_flickers`
  show up on still nights, raise that setting in PWI2 a little above the
  rms the mount shows when settled on a target (and note the value in the
  site's deployment notes); on windy nights some flicker is expected and
  only delays the end of a slew by up to `slew_noisy_time`.
* **PWI4 reports only the GUI-selected focuser and rotator** ("Select from
  M3" must be ticked in PWI4; nothing in the API can check it), and
  `rotator.mech_position_degs` is always Rotator index 0's.
* **PWI4 accepts a goto beyond its own axis limits** (HTTP 200, no error),
  decelerates to exactly the limit, and then reports `mount.is_slewing`
  (and `is_tracking`) true forever. The server watches every slew it sent:
  slewing while the mount moves less than `stall_move` deg in `stall_time`
  s is a failed slew. It sends stop, and `Slewing` raises the reason (the
  target is probably beyond PWI's own limit) until the next slew or
  `AbortSlew`. The jog tool stops such a move too.
* **PWI4 can ignore an M3 goto**: `/m3/goto?port=1` sent about 3 s after the
  M3 had arrived at port 2 answered 200, and the M3 never moved (still port 2
  after 120 s); sent again later it took 6 s. `SelectPort` re-sends a goto
  the M3 has not started on (see Actions). `m3.port` reads -1 while the M3
  moves, and may read -1 or stale just after PWI4 starts.
* **PWI4 disables an axis after a drive fault** (seen: "Axis 0 error: 29056
  Motor pulse usage too high (I2t)", a thermal overload after the motor had
  pushed against something for minutes). The server refuses motion with
  the axis named and any axis error fields PWI reports; a slew under way
  when it happens fails (`Slewing` raises). Find the cause before
  `MotorsEnable`.
* **PWI4 configuration keys are dotted** (`Axis0.HomePositionDegs`,
  `Axis1.NegativeSoftwareLimitDegs`); an undotted name is silently ignored.

## Pointing models (PWI2)

A restarted PWI2 loads `Default_Mount_Model.PXP`, not the model of the M3
port in use; legacy `cdk700.startPWI` sent
`device=mount&cmd=setmodel&filename=<the current port's model>` after
starting PWI. The server's optional `pointing_models` does the same:

```json
"pointing_models": {"1": "<port 1 model>.PXP", "2": "<port 2 model>.PXP"}
```

(keys: M3 port `1`/`2` or a `ports` name). Whenever PWI comes back (the
server's first read, a read after PWI did not answer, PWI reconnecting to
the mount) or the M3 port changes, and PWI reports another
`mount.pointing_model` than the current port's (file names compare
without directory or case), the server sends `setmodel` for it, once per
such event; a failure is logged and the next reconnect or port change
tries again. It moves nothing. PWI2 can take seconds to report the new
model (on MINERVA's T2, 2026-11-08, still the default model 3 s after
`setmodel` answered, the new one moments later), so if the answer still
shows the old model the server re-reads PWI every second in the background
(the limit watch and Alpaca requests do not wait) for up to
`model_verify_timeout` (default 15 s), and logs the outcome once: loaded,
with the time it took, or an error only once the whole wait has passed.
The `Status` action reports `pointing_model`, `pointing_model_wanted` and
every `model_loads` entry (port, file, error), added when its outcome is
known. PWI2 only: a PWI4 config with `pointing_models` is
refused (robofast sends PWI4 no model request; PWI4 keeps the model loaded
in its GUI).

## Homing

`/mount/find_home` under PWI4 4.1.6 homed both CDK700s from every start
position tried (17 of 17 runs on one, 11 of 11 on the other), in 33-58 s:

* The altitude home sensor is a single edge; its state tells the controller
  which way to go.
* Azimuth first drives to the NoWrap sensor (66 deg above home on one
  mount, 93 deg below on the other), then back to home, so expect up to
  about 100 deg of azimuth travel.
* PWI4 aborts an axis after 30 s of its own fine search if the sensor's
  hysteresis is wide (seen once): the axis stops away from home.
* Until the first home after a controller power cycle the encoder positions
  mean nothing (both axes read about 0 deg).

What robofast does:

* **Nothing homes implicitly**: not starting, not connecting, not enabling,
  not before a slew. The server's `FindHome` (Alpaca, and the `FindHome`
  Action) is refused (NotImplemented, `CanFindHome` false) unless the config
  says `"allow_find_home": true`; the MINERVA configs say false.
  The recovery ladder's `home` rung (docs/recovery.md) is a client of that
  `FindHome`: with `allow_find_home` false it is skipped as unavailable.
* The engineering path is `robofast-pwi-jog home --axis 0|1|both`: it asks
  first, prints the position while it runs, and sends stop on Ctrl-C, after
  `home_timeout` s, or (for one axis) if the other axis moves more than
  0.5 deg -- what a PWI4 that ignored the axis parameter would do.
* One axis at a time needs PWI4 4.1.7 or later (by `pwi4.version`; the
  axis parameter arrived in 4.1.7 beta 3, and a 4.1.7 beta 1 or 2 that
  ignored it would be caught by the other-axis watch). On PWI4 4.1.6 home
  both axes together. PWI2 homes both together.
* **Moves are refused while the mount is not homed**: slews, park,
  tracking on, offsets, pulses, and the jog's `altaz`/`radec`. Homed means
  PWI2 `encoders_have_been_set`, PWI4 `axisN.is_homed` where reported, or
  robofast's **home record** (below). The engineering override is
  `"allow_unhomed_moves": true` in the config **and** `--allow-unhomed` on
  the command line (server or jog); every such move is logged as a warning.

### The home record (PWI4)

PWI4 4.1.6's `/status` has no homed flag, so robofast keeps its own
evidence (`robofast/devices/pwi_homed.py`): a JSON file per telescope,
`$ROBOFAST_DATA_DIR/pwi-homed/<name>.json` (or `home_record` in the
config), shared by the server and the jog tool.

* **Written** only when a home robofast commanded (`robofast-pwi-jog home`,
  or the server's `FindHome`) ends with the mount still and each homed axis
  within `home_tolerance` (deg) of the config's `home_alt` / `home_az`:
  where PWI4 reports the mount after a good home. A home that ends
  elsewhere (an aborted fine search) is not recorded, and the jog prints
  where it ended. Without `home_alt`/`home_az` nothing is recorded (the
  server counts its own home until it stops, as before).
* **Dropped** (the file removed) as soon as the server or the jog sees PWI
  not connected to the mount (a PWI4 restart or a controller power cycle),
  both axes reading within 1 deg of 0 (the encoders after a power cycle), or
  a PWI version other than the one that homed. Reconnecting does not bring
  it back: home again.
* `robofast-pwi-jog forget-home` drops it by hand. What it cannot see: a
  controller power cycle while nothing of robofast's reads PWI, followed by
  a move away from 0 by another client. After any power cycle, home again
  or run `forget-home`.

So the operator's path after a power cycle is: `connect`, `enable`,
`home --axis both` (watching the webcam), and the record lets the server
and jogs move without any override. To set `home_alt`/`home_az` for a
mount, home it once, check it on the webcam, and copy the position the jog
prints.

The PWI version (PWI4 `pwi4.version`; PWI2 reports none) is logged by the
server at start and on any change, by the jog at its first read, and is in
the `Status` Action's JSON. The telescope's `DriverInfo` starts with it
(`PlaneWave PWI4 4.1.6; robofast pwi_server 0.1.0`), and the imaging
pipeline writes `DriverInfo` into each light frame's header as `MNTSWVER`
(docs/data.md).

## Safety

* **Hardware gate** (`robofast/hwgate.py`, design.md 3.1): without
  `"allow_hardware": true` in the config **and** `--hardware`, the server
  and the jog tool talk only to a verified simulator on loopback. A tunnel
  (`ssh -L 7101:...`) on 127.0.0.1 is refused before a byte is sent.
* **Nothing implicit**: starting, Alpaca `Connected` and every read only
  read PWI (and, with `pointing_models`, load the port's model: it moves
  nothing). Connecting PWI to the mount, enabling the motors and homing
  are explicit (`MountConnect`, `MotorsEnable`, `FindHome`).
* **Altitude limits** (`min_alt`, `max_alt`) are checked here before any
  goto (RA/Dec targets via PWI's sidereal time and the configured
  latitude, J2000 precessed to date). Motion also needs the mount connected,
  both motors enabled and no PWI2 drive error.
* **A stop is not instant**: from full slew speed (15 deg/s, with
  15 deg/s^2 deceleration configured in PWI4) a CDK700 coasted 6-8 deg after
  `/mount/stop` (measured 5.9 and 7.4 deg in azimuth and altitude on one
  mount, 7.2 and 8.1 on the other, PWI4 4.1.6). Anything that stops the
  mount at a limit must act that far before it. The config's
  `stop_distance` (default 8 deg) at `slew_speed` (default 15 deg/s) is that
  coast; at speed v robofast assumes `stop_distance * (v / slew_speed)^2`
  (constant deceleration) plus one `watch_interval` of travel.
* **Limit watch**: every `watch_interval` s the server sends `stop` if the
  mount is outside the limits by more than `limit_margin` and still moving
  further out (a tracked target setting), or if it is heading out fast
  enough that it would coast past `limit_margin` after a stop (motion
  commanded by something else). A slew the server itself sent to a target
  inside the limits is left to PWI's own deceleration unless the mount
  passes that target by more than `limit_margin`. It never commands motion.
  A read implying more than `glitch_speed` (default twice `slew_speed`) is
  a bad sample, not motion: it is skipped and logged, and the next read is
  measured from scratch (MINERVA T1 under PWI2 once reported 61.4 deg
  altitude mid-slew from near 40, and the watch stopped a good slew).
  Speeds come from the newest read and the read two before it (two
  intervals), each timed halfway between asking PWI and its answer, so the
  timing jitter of one read through an SSH tunnel does not make a speed;
  a speed between `slew_speed` and `glitch_speed` (no mount goes faster
  than `slew_speed`) coasts as `slew_speed`. With nothing of ours moving
  the mount (no slew, park or home), a read implying more than
  `track_speed_max` (default 1 deg/s; tracking is ~0.004) is suspect: the
  watch reads PWI again at once and acts only if that read confirms the
  motion (night 1107: PWI2 read 77.9 deg for one sample of a mount
  tracking at 63.7, "22 deg/s", and the watch stopped it). A slew the
  server sent toward a target inside the limits is predicted to come to
  rest no further than `goto_margin` (default 1 deg) past that target
  while it moves toward it: PWI decelerates onto it. Past the target, or
  moving away from it, or with no target of ours, the full coast counts,
  and a runaway is stopped as before. When
  the watch stops a slew the server sent, `Slewing` raises: the slew failed,
  and nobody mistakes where it stopped for the target.
  The jog tool applies the same coast to its own limit check.
* **The limit watch stands aside while the mount is not homed or is
  homing.** Until a CDK700 is homed its encoders count from wherever the
  controller powered up (both axes ~0 deg after a power cycle), and a home
  search runs in that frame before PWI resets it: the alt/az PWI reports
  then are not sky positions. On MINERVA T2 (2026-11-09) a findhome sent
  to PWI2 directly searched downward from 0-18 deg; the watch read that as
  below `min_alt` and sent `stop` four times, aborting every altitude home
  (with the server stopped the home finished at 45.85 deg). So the watch
  enforces no alt/az limits while PWI reports a home in progress (PWI2
  `is_finding_home`, whoever sent it), while a `FindHome` the server sent
  is running, or while the mount is not homed by the same test the moves
  use (PWI2 `encoders_have_been_set`; PWI4 `axisN.is_homed` where
  reported, else robofast's home record; unknown counts as not homed).
  PWI's own limit sensors and the controller protect the mount meanwhile.
  It logs one warning when it suspends and one when it resumes (an info
  line if the reason changes in between), and Action `Status` reports
  `watch_suspended` (the reason, or null). Speeds are measured afresh once
  it resumes. Robofast's own moves while unhomed stay refused, and nothing
  in the server interrupts PWI's home: a due pointing-model check
  (`setmodel`) also waits until the home is over. A homed mount is watched
  exactly as above.
* **Stalled slews**: see "Quirks handled" (PWI4 at its own limit). A slew
  to an alt/az target (and a park) must make progress toward it: at least
  `stall_move` deg in `stall_time` s on the axis furthest from its target,
  so a mount pushing against an obstruction and only creeping is stopped
  too. A park also fails after `park_timeout` s. A mount on its target
  (`slew_tolerance`) and still is a finished slew, never a stall (PWI2's
  stuck `moving`, above).
* **Azimuth wrap**: see below.
* **Stop** is always reachable: Alpaca `AbortSlew`, Action `Stop` (no
  connection needed), `robofast-pwi-jog stop`, and PWI's own GUI.
* Every PWI request has a timeout (`pwi.timeout`); nothing is retried,
  except an M3 goto PWI visibly did not start on (bounded, `m3_retries`).

## Azimuth wrap

A CDK700's azimuth axis turns more than 360 deg (MINERVA's PWI2 settings:
goto range -90 to 450 deg mechanical, hard limits -150 to 510), so every
azimuth has one or two mechanical angles. PWI4 picks the one inside its
axis-0 wrap range, [min, min + 360). With PWI4 4.1.6's default (min 0, as
PWI4 logs "WrapMin = 0 degs"), a park at azimuth 0 from the home position
(mechanical 281) drove 264 deg the long way, down toward mechanical 0; the
mount stopped at 17.3, still "slewing", and the next slew found azimuth
disabled by a drive overload. PWI2 had always parked there through
mechanical 360.

With `az_wrap_min`/`az_wrap_max` in the config (mechanical deg; the
MINERVA PWI4 configs say -90 and 450, PWI2's goto range), every goto
robofast sends (slews, park):

1. reads the mechanical azimuth (`mount.axis0.position_degs`) and PWI4's
   wrap range (`mount.axis0_wrap_range_min_degs`); if PWI4 reports either
   not, the goto is refused;
2. picks the target's mechanical angle inside [`az_wrap_min`,
   `az_wrap_max`] nearest the current one (the shortest legal path; a path
   between two angles inside the range stays inside it);
3. if PWI4's wrap range would not put it there, sets the wrap range
   minimum to that angle - 180 (`/mount/set_axis0_wrap_range_min?degs=`),
   reads it back, and refuses the goto if PWI4 did not take it;
4. sends the goto, and watches it make progress (stalled slews, above).

The limit watch stops a mount whose mechanical azimuth leaves the range (or
would coast out of it). The jog tool does not set the wrap range: it
refuses an alt/az jog that PWI4's current wrap range would send the long
way. Without `az_wrap_min`/`az_wrap_max` PWI chooses, as before.

## Config

`sites/minerva/remote/t1_pwi2.json` (and `t2_pwi2`, `t1_pwi4`, `t2_pwi4`):
`name`, `backend`, `latitude`, `longitude`, `elevation`, `min_alt`,
`max_alt`, `park_alt`, `park_az` are required (no defaults, design.md
3.10); with a focuser also `focus_min`, `focus_max`. Optional: `pwi`
(`host`, `port`, `timeout`, `connect_timeout`), `allow_hardware`, `listen`,
`focuser`, `rotator`, `rotator_status_port`, `ports`, `m3_timeout`,
`m3_resend_after`, `m3_retries`, `max_offset`, `guide_rate`,
`max_pulse_ms`, `limit_margin`, `watch_interval`, `stop_distance`,
`slew_speed`, `glitch_speed`, `track_speed_max`, `goto_margin`,
`slew_tolerance`, `stall_time`, `stall_move`, `moving_speed`,
`slew_report_min`, `slew_settle`, `slew_noisy_time` and
`slew_arrive_timeout` (PWI2), `park_tolerance`, `park_timeout`, `az_wrap_min`,
`az_wrap_max`, `status_max_age`, `request_timeout`,
`aperture_diameter`, `focal_length`, `max_jog_deg` (at most 10),
`jog_timeout`, `home_timeout`, `home_alt`, `home_az`, `home_tolerance`,
`home_record`, `allow_find_home`, `allow_unhomed_moves`,
`pointing_models` (PWI2), `discovery_port`.

    robofast-pwi-server --config sites/minerva/remote/t1_pwi4.json --hardware [--allow-unhomed]

## Simulators

    python -m robofast.devices.pwi_sim --api pwi4|pwi2 --port 0 [--ready]

One CDK700 model behind either API. Motion needs connect + enable (start
with `--ready` for connected, enabled and homed); slews at `--slew-rate`;
a goto puts azimuth at its mechanical angle in PWI4's wrap range,
[0, 360) by default, so a goto from 359 to 1 deg goes the long way (as
PWI4 does). Fault modes (`/robofast-sim/mode?mode=`):
`hang`, `slow`, `garble`, `http500`. The PWI4 behaviour seen on hardware is
modelled too: `--pwi-min-alt` (a goto below PWI's own limit stops there and
reports slewing forever), the axis-0 wrap range (`--wrap-min`; mechanical
azimuth is continuous and a goto ends inside the wrap range),
`CDK700Model.az_mech_min` (an azimuth obstruction), `m3.port` -1 while the
M3 moves, dropped M3 gotos (`CDK700Model.m3_drops`), a controller power cycle
(`CDK700Model.power_cycle`: disconnected, not homed, both axes at 0; with
`home_search` a home of the unhomed altitude axis first searches that many
degrees downward, then the encoders are set and the reading jumps to the
home, as T2's PWI2 did on 2026-11-09), and a
PWI restart (`CDK700Model.pwi_restart`, `GET /robofast-sim/restart`, or a
new simulator started with `--restarted`: disconnected, motors disabled,
not homed, `Default_Mount_Model.PXP` loaded). PWI2 `setmodel` replaces the
loaded model (`--model-file` at start; reported as `mount.pointing_model`).
Loopback only, registered with `hwgate` while serving.

## The jog tool

`robofast-pwi-jog --config CONFIG [--hardware] [--yes] COMMAND`

| Command | Does |
|---|---|
| `status [--watch S]` | read only (the default) |
| `stop` | mount stop, at once |
| `connect` | PWI connects to the mount controller |
| `enable` / `disable` | both axis motors (`enable` asks first) |
| `tracking-off` | tracking off |
| `home --axis 0\|1\|both` | find home (asks first; see "Homing"); records a home that ends at `home_alt`/`home_az` |
| `forget-home` | drop robofast's home record |
| `altaz --dalt D --daz D` | relative alt/az goto, each at most `max_jog_deg`; target inside the limits; tracking off; no azimuth move across 0/360; homed (or `--allow-unhomed`) |
| `radec --dra A --ddec A` | relative RA/Dec offset in arcsec while tracking, each at most `max_jog_deg` x 3600 |

A move prints its plan and waits for `yes` (or `--yes`), then prints the
position every `--poll` s. It sends **stop** on Ctrl-C, after
`jog_timeout`, if the mount moves more than planned + `--slack` deg from
where it started, if the altitude leaves the limits or heads out of them
fast enough to coast past them, or if PWI reports slewing while the mount
does not move (`stall_time`). Everything is
logged to `$ROBOFAST_DATA_DIR/pwi-jog/<name>.log` (or `--log`).

## PWI4 on the network

PWI4's HTTP API (port 8220) listens on the telescope PC's own 127.0.0.1
only (as on T1, PWI4 4.1.6). robofast reaches it without opening it to the
LAN: a second SSH hop through the telescope PC's own sshd (gateway, then
the telescope PC on port 22, forwarding to its 127.0.0.1:8220), with
`ProxyJump`. The `ssh` lines are in
[docs/remote.md](remote.md#pwi4-through-the-telescope-pcs-sshd).

The alternative: in PWI4's HTTP server settings allow connections from
other machines, and add a Windows Defender Firewall inbound rule on the
telescope PC for TCP 8220 allowing **only** the gateway's address (Scope:
remote IP address).

Either way, check through the tunnel from the control machine with
`robofast-pwi-jog --config ... --hardware status` (it prints PWI4's
version).

## First hardware session

Checklist (before any command that moves):

1. **Enclosure closed** (the mount moves; no sky needed). Nobody in it.
2. **Legacy users stopped**: the legacy control code (`mainNew` and
   anything else that talks to PWI on that telescope) is not running, so
   robofast is PWI's only client. Only one of PWI2 and PWI4 is running.
3. **Webcam on the telescope**, watched by someone who can see the mount.
4. **The PDU outlet** for the mount (and how to switch it off) is known.
5. **Abort at hand**: a second terminal with
   `robofast-pwi-jog --config ... --hardware stop` typed and ready; PWI's
   own Stop button as a second path.
6. The tunnel is up (`docs/remote.md`); PWI4's API reachable ("PWI4 on the
   network").
7. **Before any home**: compare PWI2's two mount settings files on the
   telescope PC (`settingsMount.xml` vs
   `settingsMount.20161219.badhome.xml`) and set PWI4's mount homing
   direction and limits to match the live PWI2 file. For T1 the live
   `settingsMount.xml` (read 2026-11-02) says:

   | PWI2 setting | Live value | 2016 "badhome" file |
   |---|---|---|
   | `homingMoveAzmCwWhenTrue` | True (azimuth homes clockwise) | False |
   | `homingMoveAltUpWhenTrue` | False (altitude homes down) | True |
   | azimuth home (encoder) | 4.90527 rad (281.05 deg) | |
   | altitude home (encoder) | 0.81423 rad (46.65 deg) | |
   | altitude limits: hard / goto / track | 20-90 / 20-86 / 20-87 deg | |
   | azimuth CW limit: hard / goto / track | 510 / 450 / 480 deg | |
   | azimuth CCW limit: hard / goto / track | -150 / -90 / -130 deg | |

   PWI4 was set to match (its keys are dotted: `Axis0.HomePositionDegs`,
   `Axis1.NegativeSoftwareLimitDegs`; an undotted key is silently
   ignored), and homing then worked from every start position ("Homing").
   With PWI4's home positions equal to PWI2's (the same encoder frame) PWI4
   loads PWI2's `.PXP` pointing-model files directly
   (`/mount/model/load?filename=`, from PWI4's own GUI or by hand: robofast
   does not send it). robofast's own limits stay tighter: `min_alt` 21, `max_alt` 83 (legacy
   scheduler); the jog refuses any azimuth move across 0/360 rather than
   modelling the -90..450 deg wrap.

Sequence (T1 on PWI4, through the 7141 tunnel):

    C=sites/minerva/remote/t1_pwi4.json
    J="robofast-pwi-jog --config $C --hardware"
    # 1. Read only.
    $J status                          # version, alt/az, conn, motors, homed
    $J status --watch 2                # leave running in a window
    # 2. Connect and enable (no motion).
    $J connect                         # only if conn 0
    $J enable                          # only if motors 00; asks yes
    $J tracking-off                    # only if track 1
    # 3. Tiny jogs BEFORE homing, to see which way each axis goes.
    $J --allow-unhomed altaz --dalt 0.2     # up 0.2 deg; asks yes; watch the webcam
    $J --allow-unhomed altaz --dalt -0.2    # back
    $J --allow-unhomed altaz --daz 0.2      # azimuth (away from 0/360)
    $J --allow-unhomed altaz --daz -0.2
    # 4. Settings checked (checklist 7)? Home (33-58 s; azimuth may first
    #    travel ~100 deg to its NoWrap sensor). Both axes on PWI4 4.1.6;
    #    one at a time (--axis 1, then --axis 0) needs PWI4 4.1.7+.
    $J home --axis both                # asks yes; stop at hand
    $J status                          # "home record: axes [0, 1]"
    # 5. Homed (recorded): jogs without the override.
    $J altaz --dalt 0.5
    $J stop                            # any time

Watch on the webcam that each move goes the right way and by about the
right amount, and that each home goes toward its sensor. Only then the
server (`robofast-pwi-server --config $C --hardware`) and Alpaca clients.
On PWI2 (T1 config `t1_pwi2.json`, tunnel 7101) the steps are the same
except that `home --axis both` is the only home.

## Measured on hardware

Two CDK700s under PWI4 4.1.6 (2026-11):

| What | Measured |
|---|---|
| Slew of 10-40 deg | 6-10 s; final error at most 2 arcsec |
| Repeatability (returning to a position) | 0.1-0.3 arcsec |
| Tracking servo error | 0.06-0.12 arcsec rms |
| Stop from full slew speed (15 deg/s, 15 deg/s^2) | coasts 6-8 deg ("Safety") |
| Home (`/mount/find_home`) | 33-58 s, from any start ("Homing") |
| M3 port 1 to 2 / 2 to 1 | 5.7-10.7 s / 6-41 s |
| Rotator (`/rotator/goto_mech?degs=`; the parameter is `degs`) | about 2.3 deg/s; ends 2-6 arcsec short of the target |
| `/status` latency | about 2 ms on the telescope PC (an SSH tunnel adds its round trip) |

## To check on hardware

* PWI2 accepts decimal `ra2000`/`dec2000`/`alt`/`azm` (legacy docstrings
  say so; legacy acquisitions sent decimals).
* What PWI2 does with an alt/az goto while tracking (legacy turned tracking
  on before an alt/az park, then off): robofast turns tracking off first.
* PWI2 `moving` after an untracked alt/az goto (we rely on position change).
* When PWI2's `moving` sticks `True` (seen after a restart), whether
  `on_target` still tells the truth, and whether PWI2's reported position
  keeps updating (night 1107's stall verdict saw it unchanged for 3.5 s
  while the controller's own read showed the mount tracking).
* Offset sign conventions (`incrementra` / `ra_add_arcsec` positive = RA
  increases) and the PulseGuide directions, with a star on a camera.
* Whether PWI4's `/mount/find_home` reports `is_slewing` while homing.
* The per-axis home request: `/mount/findhome?axis=0|1` (changelog
  spelling; the older both-axes command is `/mount/find_home`), axis 0 =
  azimuth, 1 = altitude (PWI4 4.1.7+). PWI4 4.1.6 reports no homed state
  over HTTP (hence the home record); whether a later PWI4 does.
* The PWI4 wrap-range request and status keys ("Azimuth wrap"): the path
  `/mount/set_axis0_wrap_range_min`, its parameter (`degs` assumed), and the
  keys `mount.axis0_wrap_range_min_degs` and `mount.axis0.position_degs`.
  Until they are confirmed on PWI4 4.1.6, gotos on a config with
  `az_wrap_min` are refused if PWI4 does not report them; nothing moves.
* The mechanical azimuth range that is really free of obstructions and
  cable limits on each mount (a goto toward mechanical 0 stopped at 17.3 on
  T1, inside PWI2's -90..450 goto range).
* Focuser positions (microns) and rotator mechanical angles match legacy.
