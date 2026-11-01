# The FLWO 60-inch's atmospheric dispersion corrector (ADC)

The 60-inch (Tillinghast) telescope has an atmospheric dispersion
corrector: two prisms in the beam, each turned by its own stepper motor.
It is part of the telescope and feeds both of its instruments, TRES and
FAST, so robofast serves it as a telescope device, independent of the
instrument (`robofast_core/devices/adc_server.py`). The instrument can
change only an input: the wavelength band of the optional refraction model
and, if ever needed, a position-angle offset.

What follows comes from reading FLWO's legacy ADC daemon (`adc_d`, part of
the 60-inch TCS software "telshell", T. Groner; with thanks to FLWO) and
its variants. The tracking math is ported from it and checked against it
with test vectors; the control logic is new. Nothing here has run against
the real motors yet: see "Confirm on the mountain".

## 1. What the legacy daemon does

From its source unless marked *inferred*.

* **Start:** opens the motors' serial port (9600 baud, 8N1), sends a
  single byte 3 (Ctrl-C: a controller reset), waits 15 s, then commands
  both motors to step 0 ("home") and writes `Reset` to its position file.
  It does not run the homing routine at start: it assumes the prisms were
  left at home (*inferred* from the commands; the reset presumably zeroes
  the step counters where the motors stand).
* **Input:** every 2 s it reads the telescope's hour angle, declination
  and zenith distance from the TCS software's shared status block, which
  the TCS window's status reader fills from TCSGalil's `#25` (and `#26` for
  the mount mode). The status reader marks the mount "slewing" while
  TCSGalil reports slew mode, and also while the dome rotates.
* **When it moves:** only when the parallactic angle has changed by 2 deg
  or more since the last time it looked (and, inside, by more than 1 deg
  in whole degrees). If the mount is slewing it waits up to 1.2 s, then
  skips the update.
* **On/off:** the TCS window's `adc_on` and `adc_off` verbs create and
  delete a flag file the daemon checks before each move. With the flag it
  moves the prisms; without it, it runs each motor's homing routine (and
  leaves them there).
* **The move:** parallactic angle `pa` from HA/Dec (SOFA `iauHd2pa`, the
  latitude hard-coded at 31.69 deg); a deflection `defl` from the zenith
  distance by a step table (section 3); prism angles A = `90 - pa - defl`,
  B = `270 - pa + defl` (mod 360); each converted to steps from that
  motor's home and sent as an absolute move, then a position query. It
  waits 2 s after every command and only logs the replies.
* **Stowed:** a zenith distance of exactly 0 with the mount not slewing
  is taken as "stowed at the zenith": no move.
* **Output:** a one-line text file with the last parallactic angle, the
  two prism angles, the hour angle and the deflection, read by the TCS
  window.
* **Variants:** the production daemon and `adc_d_swap_noncongruent` differ
  only in logging; `adc_d_swap_congruent` (and a test copy) give prism A
  the target the production daemon gives prism B and vice versa
  (`swap_prisms` in robofast's config). Which one runs on the mountain is
  to be confirmed; the production one is the default.

## 2. Protocol (`robofast_core/devices/adc_mdrive.py`)

| Item | Value |
|---|---|
| Line | RS-232, 9600 baud, 8 data bits, no parity, 1 stop bit |
| Addressing | two motors, `A` and `B`, on the one line; every command starts with the motor's name |
| Terminator | line feed |
| `<m>ma <n>` | move to absolute step `n` from home (0..3999) |
| `<m>pr p` | print the position (steps) |
| `<m>cl HO` | run the homing routine (the legacy daemon waits 8 s for A, 12 s for B, sending nothing) |
| Ctrl-C (byte 3) | controller reset (legacy, at start; **robofast never sends it**) |
| Scale | 11.1111 steps per degree of prism rotation (4000 a turn) |
| Controller | *inferred*: an IMS (Schneider) MDrive in "party" mode, `HO` a homing subroutine stored in each motor that leaves the counter at 0 |
| Replies | *unknown*: the legacy daemon only copies them to its log. robofast takes the first bare-integer line after `pr p` and treats a line starting with `?` as an error |

robofast sends only `ma`, `pr p` and `cl HO`, one exchange at a time,
each bounded; a move outside 0..3999 steps is refused before anything is
sent. There is no stop command (the legacy daemon has none), so a move is
bounded by `move_timeout` and watched for stalls instead.

## 3. The math (`robofast_core/devices/adc_math.py`)

1. **Parallactic angle and zenith distance** from the mount's altitude and
   azimuth (the TCSGO server's `DeviceState`): ERFA `ae2hd` then `hd2pa`
   (the IAU SOFA routines, as in the legacy daemon), with the site's
   latitude. Alt/az are used rather than RA/Dec because the Telescope
   reports J2000 RA/Dec, while alt/az are the mount's own topocentric
   position. East of the meridian the angle is negative.
2. **Deflection** (deg), how far each prism turns from the null setting:
   * `legacy_table` (default): the legacy table, first match on the zenith
     distance `zd`:

     | zd above | 67 | 63 | 58 | 53 | 48 | 43 | 38 | 28 | 18 | 8 | 4 | 2 | else |
     |---|---|---|---|---|---|---|---|---|---|---|---|---|---|
     | defl | 80 | 55 | 38 | 32 | 27 | 23 | 20 | 16 | 10 | 7 | 4 | 2 | 0 |

   * `refraction`: two equal prisms turned `+defl` and `-defl` from
     opposition disperse as `D sin(defl)` along their bisector, so
     `sin(defl) = R(zd) / D`, with `R` the atmosphere's dispersion between
     the instrument's `blue_um` and `red_um` from ERFA's refraction
     constants (`refco`: `R = dA tan z + dB tan^3 z`; the classic
     treatment is Filippenko 1982, PASP 94, 715) and `D`
     (`max_dispersion_arcsec`) the ADC's full correction. Between 390 and
     910 nm at the mountain `R` is about 1.43" tan z; the legacy table
     implies `D` of 3.4" to 4.1" for zd above 20 deg, so the default
     3.8" is a fit to it, not a measurement. Capped at `max_deflection`
     (80, the legacy maximum).
3. **Motor angles from home** (the only thing the motors see): with the
   homes at legacy prism angles 202.5 (A) and 22.5 (B) the legacy chain
   reduces to

   `A = 112.5 + pa + defl`, `B = 112.5 + pa - defl` (deg, mod 360)

   (with `swap_prisms`, `292.5 + pa - defl` and `292.5 + pa + defl`).
   Home is a null setting (A = B), so turning the pair to home is
   **neutral** whatever the parallactic angle. Steps =
   `round(11.1111 * angle)`, half away from zero, as in C.

**Test vectors.** `tests/core/data/adc_legacy_vectors.json` holds 231
cases computed by the legacy functions themselves (compiled unchanged in a
small harness with SOFA's `iauHd2pa`): hour angle and declination strings
as the status reader writes them, the zenith distance, and the legacy
parallactic angle, deflection, prism angles, motor angles and steps,
including every table edge and both sides of the meridian north of the
zenith. The port reproduces all of them exactly (steps included), and the
closed form above agrees with the legacy chain
(`tests/core/test_adc.py`).

### What the port fixes

* **Zenith-distance changes alone never moved the prisms.** The legacy
  update is triggered only by a 2 deg change in parallactic angle. Low in
  the east or west the zenith distance changes much faster than the
  parallactic angle (at alt 50 az 120 to alt 40 az 118 the deflection goes
  from 20 to 27 deg while the angle moves 0.7 deg), so the deflection
  could stay at its old value for a long time. robofast recomputes both
  targets every update and moves when either motor is off by more than
  `deadband`.
* **The first update and `adc_on` did nothing until the angle moved.**
  After a start, the inner threshold is initialised to the current angle,
  so the first call never moves; and the flag file is only looked at when
  the angle has moved 2 deg, so `adc_on` (and `adc_off`) took effect only
  then. In robofast `Tracking` takes effect at the next read.
* **A slew could leave the prisms set for the previous target.** The
  outer threshold is updated even when the update is skipped because the
  mount is slewing, so a reading taken late in a slew within 2 deg of the
  final angle suppresses the update for the new target. robofast holds
  during the slew and for `settle_after_slew` after it, then moves.
* **Near the zenith it chased the parallactic angle for nothing.** With
  no deflection the two prisms cancel at any common angle, but the legacy
  daemon still turned them as the angle swung through the zenith. robofast
  does not move while the pair is at a null and no deflection is wanted.
* Harmless: `adj_deg` wraps only once (a prism target can come out
  negative, e.g. -80 for 280), but the step conversion gives the same
  steps either way; the "north" and "south" branches of the target
  formula are identical; the hard-coded latitude (31.69) is 0.01 deg off.

## 4. Device mapping

One Alpaca server, three standard devices:

* **`switch/0` (ISwitchV2): the tracking service.** Switch 0, `Tracking`,
  is the legacy `adc_on`/`adc_off` and the only writable switch.
  Read-only: `Settled`, `InputFresh`, `Fault`, `Referenced`, and the
  values `ParallacticAngle`, `ZenithDistance`, `Deflection`, `PrismA`,
  `PrismB` (deg). Actions: `Status` (JSON), `Home`, `Neutral`,
  `Instrument`.
* **`rotator/0`, `rotator/1` (IRotatorV4): prism A, prism B.**
  `MechanicalPosition` is the motor's angle from home; manual moves
  (`MoveMechanical`, `MoveAbsolute`, `Move`) when tracking is off; `Sync`
  and `Reverse` are kept in memory; `Halt` is not implemented.

Why this shape: a prism is one rotating axis with an angle, which is
exactly what an ASCOM Rotator is, so any Alpaca client (the console, a
FITS header reader, ConformU) can read and move each prism with no custom
API. But tracking is a service over *both* prisms and the telescope, not a
property of either axis, and its only command is on/off: an ASCOM Switch
gives that a standard, scriptable control (`SetSwitch`), plus the state
the sequencer and the headers need as plain switch values, with Actions
for the rare operations (homing, neutral). A single custom device would
have needed a robofast-only client; two Rotators alone would have left the
tracking loop with no standard handle. Everything stays in one process
because both motors share one serial line.

## 5. Behaviour and safety

* **Hardware gate:** a real port needs `"allow_hardware": true` in the
  config **and** `--hardware`; shipped false.
* **Starting, connecting, reading and stopping never move a motor.**
  Startup only reads the two positions (no reset, unlike the legacy
  daemon); stopping sends nothing (a move in progress completes).
* **Homing only on request** (`Action("Home")`: A, then B, with the legacy
  waits; refused while tracking). Until the motors are homed every move
  and `Tracking` is refused, unless `trust_counter` (off in the site
  config) accepts the controllers' counters at startup.
* **Bounded moves:** targets are 0..3999 steps; a move that has not
  arrived within `move_timeout`, or whose position stops changing for
  `stall_time` short of its target, is a **fault**: `Fault` true, the
  reason in `Status` and the log, and tracking holds (no further moves)
  until `Tracking` is written true again.
* **Stale input:** the telescope is read every `update_interval` (2 s, the
  legacy cadence). A failed read, or a position that is not a number or out
  of range, is no input; after `stale_after` without a good read
  `InputFresh` goes false. Either way the prisms **hold** where they are:
  nothing is computed from garbage, and nothing moves until good input
  returns.
* **Slews:** hold while the Telescope reports `Slewing` and for
  `settle_after_slew` after, then move once to the new target.
* **Park:** with the Telescope `AtPark`, turn the prisms to neutral once
  (`park_action: "neutral"`, default) or hold (`"hold"`, closer to the
  legacy daemon, which holds when stowed at the zenith).
* **Low altitude:** the legacy table saturates at 80 deg above zd 67;
  beyond `max_zenith_distance` (75; the TCS refuses slews below 20 deg
  altitude) robofast holds rather than trust the input.
* **Tracking off:** turns both prisms to home (neutral), as the legacy
  `adc_off` did, but by an absolute move to step 0, not a homing run
  (`neutral_on_off`). Writing off when already off does nothing.
* **`Settled`** is true only when tracking, with fresh input read *after*
  the last `Tracking` write, no hold, no fault, no move running, and both
  motors within `deadband` of their targets. Writing `Tracking` true again
  is how a client asks "re-read the telescope now and tell me when you are
  there".

## 6. Sequencer and headers

A telescope entry's `adc` (the switch's address, `host:port/0`) gives the
sequencer an ADC:

* After every slew for light frames, once on target: `SetSwitch(0, true)`,
  then wait for `GetSwitch(1)` (`Settled`), bounded by
  `adc_settle_timeout` (60 s). If it fails or times out the event
  `adc_not_settled` is a warning and the frames go ahead: the ADC sharpens
  a spectrum, it does not make it possible.
* Dark, bias and lamp frames do not touch it.
* Every cleanup (end of night, weather, an operator stop) writes
  `Tracking` false: prisms neutral.
* Light frames get `ADCTRACK`, `ADCSETTL`, `ADCDEFL`, `ADCANGA`, `ADCANGB`
  in their headers (`docs/data.md`).

## 7. Running and testing

```
python -m robofast_core.devices.adc_server --config sites/flwo60/adc.json --simulate
```

`sites/flwo60/adc.json` (listens on 11180; reads the TCSGO server on
11140; hardware off); `flwo60.json`'s `tillinghast.adc` points the
sequencer at it. The simulator (`adc_sim.py`) models both motors on one
line, echo and prompt, homing, moving through the steps in between, and
stuck, short and silent motors. `tests/core/test_adc.py` covers the math
against the legacy vectors, the client, every hold and fault case, the
feed against the TCSGO server on its simulator, and the sequencer with
headers; `scripts/conformu_adc.py` runs ConformU against both Rotators
and the Switch in CI.

## 8. Confirm on the mountain

1. Which daemon runs (production or `swap_congruent`; the legacy startup
   script starts an ADC program under another name, whose source is not
   in the copy read here), and that it matches the copy read here (the
   steps per degree, home angles and table).
2. The controller: make and model (MDrive in party mode?), its reply
   format to `pr p` (echo, prompt, line ending), what an error looks like,
   and whether it keeps its step counter while robofast restarts (if so,
   `trust_counter` could be true).
3. What `HO` does: a home switch or a hard stop, which direction, how long
   (the legacy waits are 8 and 12 s), and that it leaves the counter at 0.
4. Motor speed (to set `move_timeout` and `stall_time`), and whether the
   prisms may turn more than a full turn (cables): robofast, like the
   legacy daemon, only moves within 0..3999 steps, so crossing the wrap
   is a long move the other way.
5. Is home really a null (no dispersion with both motors at step 0)? Check
   on a star near the horizon: the spectrum's blue and red ends should
   meet in the fiber with tracking on, and separate along the vertical
   with the prisms at neutral.
6. The sense of the parallactic angle: with tracking on at a large zenith
   distance, does the correction reduce the blue/red offset (if it doubles
   it, the sign of `pa` or of `defl` is flipped for this telescope;
   `pa_offset` of 180 or `swap_prisms` would be the fix).
7. Whether FAST uses the same prism settings as TRES (the legacy daemon
   makes no difference), and FAST's band if the `refraction` model is used.
8. The ADC's full correction `D`, if the `refraction` model is wanted: from
   the prism glass and angle, or from star trails at two zenith distances.
9. Does the legacy TCS window, or anything else, read the position file,
   and does the observer need it while robofast drives the ADC.
10. Whether the ADC position matters for the TRES calibration lamps (does
    lamp light pass through it?); robofast leaves it where it is for lamp
    frames.
