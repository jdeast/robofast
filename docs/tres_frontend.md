# The TRES front end on the FLWO 60-inch

TRES, the fiber-fed echelle spectrograph on FLWO's 60-inch (Tillinghast)
telescope, has a front end at the telescope: the fiber head, a guide
camera with a tip/tilt stage, and a **calibration stage** ("calstage") that
puts calibration light into, or blocks, the science and sky fibers.
robofast serves the calibration stage as standard Alpaca devices
(`robofast_core/devices/tres_frontend_server.py`), so the sequencer drives
it directly, with leases, the device owner's fencing, a simulator and
ConformU like every other device, and no message broker.

What follows comes from reading the TRES front-end software by J. Eastman
and J. Kansky (jdeast/tres-guider, relicensed BSD-3 with their agreement;
the robofast code is adapted from it with credit) and FLWO's legacy 60-inch
control software (the observers' TCS window), with thanks to FLWO. Nothing
here has been run against the real front end yet: see "Confirm on the
mountain".

## 1. What drives each mechanism

| Mechanism | Hardware and protocol (from the code) | robofast |
|---|---|---|
| Calibration stage | PI **L-505** linear stage on a PI **C-663** Mercury Step controller. The front end connects to it as a serial port (the controller's USB, 115200 baud; an older version found it by its USB serial number) and drives it with PI's GCS command set through PI's `pipython`: on connect it switches the servo on and **references to the negative limit** (`FNL`); a move is `MOV 1 <mm>`, waited for with `waitontarget`; travel limits come from the controller (`TMN?`/`TMX?`). Named positions (`out`, `sky`, `science`) are millimetre values in its config. | this server: `filterwheel/0` + `focuser/0` |
| Calibration-stage power | an outlet on the front end's own APC switched PDU (SNMP), which also powers the tip/tilt controller, the guide camera, the front-end computer and the star projector. The front end can power-cycle the stage. | robofast's APC PDU server (`robofast_core/devices/pdu_server.py`) can serve it; not configured here yet |
| Calibration lamps (ThAr, incandescent) | **not** the front end: the legacy TCS window switches them itself, on a Pulizzi power switch, through a separate program | the lamp Switch server (`robofast_core/devices/pulizzi_server.py`, `docs/pulizzi.md`); `ARC`/`LAMP_FLAT` requests on the 60-inch |
| Tip/tilt | PI S-340 platform on an E-518 controller (GCS) | the guider service (`docs/guider.md`), which already has an E-518 backend |
| Star projector | three Zaber axes (X, Y, rotation; the rotation's homing is noted as broken), an artificial star for daytime guider tests | not served (an engineering tool; a Focuser per axis if it is wanted) |
| Guider / spectrograph focus motors | none in the front-end code. The legacy TCS window's old focus steppers are dead code (its `focus` verb moves nothing) | nothing to serve |

**How calstage commands arrive today.** The legacy TCS window's
`calstage <name>` (names `in`, `out`, `sky`, `science`, `block_sky`,
`block_science`) reaches the front end through a site Redis server: one
build of the window `SET`s the key `calstage` to the name; the newest
`PUBLISH`es `{"set_cal_stage": "<name>"}` on the channel `guider_commands`
and then polls `GET calstage_status` until it equals the name (up to 60 s).
`calstage position <mm>` `SET`s `calstage` to the number (0-25 checked by
the window). The public front-end code has no Redis listener for these (its
Redis use is commented out), so the production front end's handling, and
the exact strings it writes to `calstage_status`, are inferred from what
the window waits for.

## 2. Device mapping

* **`filterwheel/0` (IFilterWheelV3): the stage by named position.** A
  calibration stage with a fixed set of named positions is exactly what
  the FilterWheel interface describes: `Names`, an integer `Position`
  that reads -1 while moving, and moves by setting it. The sequencer
  already selects filter-wheel positions by name, OmniSim and ConformU
  cover the interface, and every Alpaca client understands it. A Switch was
  considered and rejected: one boolean switch per position allows
  impossible states ("sky" and "science" both on) and has no notion of
  motion, and one multi-valued switch loses the names.
* **`focuser/0` (IFocuserV4, absolute): the same stage in microns.** The
  legacy `calstage position <mm>` needs arbitrary positions; the Focuser is
  ASCOM's standard absolute positioner (`Move`, `Position`, `IsMoving`,
  `Halt`). `Position` is microns from the negative travel limit; `MaxStep`
  is the controller's travel. Off with `"focuser": false`.
* **Two Actions**, only for what ASCOM has no member for:
  `Action("Reference")` (the reference move, needed once after the
  controller powers up) and `Action("Status")` (JSON: position in mm and
  name, motion, referencing, servo, travel, controller identity, the last
  failed move).

`Position` on the FilterWheel is an `InvalidOperation` error when the stage
is not referenced, or rests between named positions (after an absolute
move, a failed move, or a hand move); setting a named position works from
anywhere. A position whose millimetre value is `null` in the config (not yet
confirmed) is listed in `Names` but refused.

## 3. Protocol (PI GCS 2.0 subset; `robofast_core/devices/pi_gcs.py`)

One command per line (LF). Setting commands have no reply: robofast reads
`ERR?` after each (the last error code, cleared by the read) and raises on a
nonzero code. Queries answer `1=<value>`; multi-line answers end every line
but the last with a space. `#5` (the single byte 0x05) answers a hex motion
mask.

| Sent | Meaning | Moves |
|---|---|---|
| `*IDN?`, `POS? 1`, `ONT? 1`, `FRF? 1`, `SVO? 1`, `TMN? 1`, `TMX? 1`, `ERR?`, `#5` | identity, position (mm), on target, referenced, servo, travel, last error, moving | no |
| `SVO 1 1` | servo on (only before a move, if off) | no (energises) |
| `MOV 1 <mm>` | absolute move | **yes** |
| `FNL 1` | reference move to the negative limit | **yes** |
| `HLT 1` | stop (the controller then reports error 10, read and cleared) | stops |

Nothing else can be sent: the client has an allowlist, so it cannot change
controller parameters or write to its memory. Error codes used: 5 (move
refused: not referenced or servo off), 7 (position out of limits), 10
(stopped by command).

## 4. Safety

* `port.type: "serial"` (a real controller) needs `"allow_hardware": true`
  in the config **and** `--hardware` on the command line; the shipped
  `sites/flwo60/tres_frontend.json` has it false. `"sim"` (or
  `--simulate`) runs the simulated controller
  (`robofast_core/devices/tres_frontend_sim.py`).
* **Starting, connecting to, reading and stopping the server never move the
  stage.** Unlike the legacy front end, robofast does not reference on
  connect: referencing is an explicit `Action("Reference")`, and until then
  every move is refused. Stopping sends nothing; a move in progress
  completes on the controller.
* Every exchange is bounded (`port.timeout`), and every move by
  `move_timeout` (the reference by `reference_timeout`): one still running
  then is halted and reported. A move that ends off target is reported
  (`Status`, the log, and `Position` reading as an error) and does not block
  the next.
* The **safety supervisor does not touch the stage**: no position is safer
  than another for the enclosure, and the stage cannot hurt the telescope or
  dome. A fault of the stage is the observation's error, not an enclosure
  fault. (If FLWO wants it left in a particular position at the end of the
  night, that is a sequencer cleanup step; see the questions.)

## 5. Sequencer and requests

* Enclosure config: a telescope's `calstage` is the server's FilterWheel
  (`sites/flwo60/flwo60.json`: `"calstage": "127.0.0.1:11160/0"`). The
  sequencer takes a grant on it with the telescope's other devices, so a
  revoked lease fences it.
* An observation's `calstage` (a position name) is selected before its
  exposures, after the M3 port and filter, bounded by the sequencer's
  `calstage_timeout` (60 s, the legacy window's wait); the timeline records
  `calstage_selected`. A telescope without a calstage, or a name its stage
  does not list, fails the observation rather than expose through the wrong
  path.
* Requests: an instrument config's `optical_elements.calstage`
  (`docs/request_language.md`). The dispatch scheduler sends it only to a
  telescope whose limits list the position in `calstage`.
* **Lamps.** A TRES ThAr or flat is "stage to the calibration position,
  lamp on, expose, lamp off" (`docs/pulizzi.md`): an `ARC` or `LAMP_FLAT`
  selects the instrument config's `calstage`, or else the lamp's own
  position from the telescope's `lamps` entry, before the lamp is switched
  on, so lamp light reaches the fibers only with the stage in place.

## 6. Redis transition shim (optional, off by default)

While the legacy TCS window still issues `calstage` commands, the server
can keep the legacy Redis keys working
(`robofast_core/devices/calstage_redis.py`; config `legacy_redis`):

* **Mirror**: `calstage_status` set to the stage's state when it changes
  (the position's name, `moving`, the position in mm such as `12.500`, or
  `unreferenced`), and `calstage` set to the name of each position reached,
  so the window's wait succeeds and the key holds the last position.
* **Commands** (`accept_commands: true` only): a *change* of `calstage` to a
  configured name or a number moves the stage through the same checks as an
  Alpaca request. The value found at startup is a baseline, never acted on.
  Anything else is logged and ignored. The window's `PUBLISH` path is not
  followed (it needs a subscription); if the live window publishes, either
  add that or keep the window on the `SET` build during the transition.
* The client sends only `SELECT`, `GET` and `SET`, and only for those two
  keys. A non-loopback Redis needs `allow_hardware` and `--hardware`, like
  the stage. Redis errors never affect the stage.
* Delete the shim when the legacy window stops commanding the stage.

robofast itself never needs Redis.

## 7. Running and testing

```
python -m robofast_core.devices.tres_frontend_server --config sites/flwo60/tres_frontend.json --simulate
```

prints `{"url": ..., "port": "simulator"}`. Tests:
`tests/core/test_tres_frontend.py` (GCS client and simulator, the server,
the shim against an in-process fake Redis, the sequencer and requests).
ConformU: `scripts/conformu_tres_frontend.py` (FilterWheel and Focuser,
`conformance` and `alpacaprotocol`; in CI's `conformu` job).

## 8. Confirm on the mountain

Until each is checked, `allow_hardware` stays false.

1. Which front-end code runs in production (newer than the public 2021
   copy), and does it still drive the stage through `pipython` with the
   commands above? Read its calstage code before enabling hardware.
2. Controller: `*IDN?` (model, firmware), GCS 2.0, axis identifier `1`,
   `#5` supported, `ERR?` cleared on read, error 10 after `HLT`.
3. The serial device on the front-end computer, USB or RS-232, baud.
4. The millimetre value of every named position. The 2021 public config
   says out 0, sky 10, science 20; the 2019 one said science 0, sky 10,
   out 20. `in`, `block_sky` and `block_science` are unknown. What each
   does optically (which fiber gets lamp light, which is blocked).
5. The travel (`TMN?`/`TMX?`), and that the reference move to the negative
   limit is safe from every position.
6. Does the controller stay powered and referenced from night to night, or
   must the evening start reference it? Who may power-cycle it (its PDU
   outlet)?
7. Speed: how long a full-travel move takes (`move_timeout`).
8. Which build of the legacy TCS window is live (`SET` or `PUBLISH` +
   poll), what the production front end writes to `calstage_status`
   (strings, case, during motion, for an absolute position), and whether it
   consumes or clears `calstage`.
9. Does anything else read or write the calstage keys?
10. Is the spectrograph bench's calibration box still used, or does all
    calibration light go through the front-end stage?
11. That the front end switches no lamps (all through the Pulizzi switch),
    and which outlets they are.
12. Is the star projector still used? Is there any motorised focus in the
    front end?
13. Should the stage be left in a particular position at the end of the
    night or when the dome closes?
14. The TRES FITS headers: the front end recorded the stage position (name
    and mm) and the controller; does the CCD header writer need these from
    robofast?
