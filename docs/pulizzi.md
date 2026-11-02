# Calibration lamps on the FLWO 60-inch (Pulizzi power switch)

TRES, the fiber-fed echelle spectrograph on FLWO's 60-inch (Tillinghast)
telescope, is calibrated with thorium-argon (ThAr) and incandescent lamps.
They are switched as outlets of a **Pulizzi** (Pulizzi Engineering, later
Eaton) remote power controller on a serial line. robofast serves those
outlets as a standard Alpaca **Switch**
(`robofast_core/devices/pulizzi_server.py`), so the sequencer switches the
lamps itself for `ARC` and `LAMP_FLAT` requests, with the device owner's
fencing, a simulator and ConformU like every other device.

What follows comes from reading FLWO's legacy 60-inch control software
(the observers' TCS window; with thanks to FLWO) and the Pulizzi IPC34xx
manual. The program that actually talks to the unit is not in the copy of
the legacy software robofast was built from, so part of the protocol is
taken from the manual and from that program's log output, and marked as
such below. Nothing here has been run against the real unit yet: see
"Confirm on the mountain".

## 1. How the legacy software switches the lamps

* The TCS window has a `lamp` verb: `lamp thar|thar2|inc|inc2 on|off`.
  Each becomes an outlet command for a separate small program, which the
  window starts with itself and which owns the serial line to the unit.
* The window waits until that program reports itself idle, hands it the
  command (`N` or `F` and a two-digit outlet number: `N02` switches outlet
  2 on, `F02` off), and waits until it is idle again (polling every 0.2 s,
  with no time limit).
* Lamp to outlet:

  | legacy verb | name here | outlet |
  |---|---|---|
  | `lamp thar` | `ThAr1` | 2 |
  | `lamp thar2` | `ThAr2` | 7 |
  | `lamp inc` | `Incand` | 4 |
  | `lamp inc2` | `Incand2` | 1 (moved from 8; the old value is commented out) |

* At startup and when the window quits it switches ThAr (outlet 2) and the
  incandescent lamp (outlet 4) **off**; ThAr2 and Incand2 are not touched.
* The external program's log shows what it sends: `@@@@` (6 bytes with its
  line ending) and `DX0` (5 bytes) when it starts, then `F02`, `F04`, ...
  (5 bytes each); after each it prints the eight outlet states it read
  back. In that log outlets 3 and 5 are on and everything else off: they
  power something other than the lamps.

## 2. Protocol

From the Pulizzi IPC34xx manual ("Programmers Guide"), unless marked
*inferred*:

| Item | Value |
|---|---|
| Line | RS-232, 9600 baud, 8 data bits, no parity, 1 stop bit, no flow control; DTR must be high |
| Characters | ASCII, upper case only; a command ends with CR (LF is ignored); no echo |
| Login | send the unit name (factory `@@@@`; the legacy program sends `@@@@`); the unit answers `IPC ONLINE!` (or `IPC@@@@`, depending on the manual's section and the firmware: robofast accepts any line starting `IPC`) |
| Bad input | anything that is not a command answers `INVALID` and **ends the session** (log in again) |
| Status | `DX0` (unit 0): one line per outlet, `OUTLET   2   OFF ( UNIT#0 J2 )`, plus the watchdog and remote status (their format is not given) |
| Switch | `Nxx` / `Fxx`, `xx` = `01`..`08`: one outlet on / off; answer `DONE` (followed by a status listing if "auto event" is on) |
| Timing | allow at least 0.5 s for an answer; retry once, then log in again |
| Line ending | *inferred*: the legacy program wrote 5 bytes for `F02`, so it sends CR LF; robofast sends CR only (the manual says CR is required, LF ignored) |
| Model | *inferred*: the legacy program's `DX0`/`Nxx`/`Fxx` are the IPC34xx command set; the model and firmware at FLWO are not recorded |

robofast sends **only** the unit name, `DX0`, `Nxx` and `Fxx`
(`robofast_core/devices/pulizzi.py`, an allowlist with a test). In
particular it never sends the commands that switch every outlet (`A0n`,
`A1n`, `S0n`, `S1n`), reboot an outlet (`SR0`), arm the unit's watchdog
(`WE0`: it reboots the unit when communication stops), or change its name,
password or power-on behaviour (`ADn`, `PWn`, `ISn`). After every switch it
reads the status back and reports an error if the outlet is not in the
asked state: the unit's front-panel switches (per-outlet manual and a
remote-disable switch) override remote commands.

## 3. The Alpaca device

`switch/0` (ISwitchV2): one boolean switch per outlet listed in the
config (`sites/flwo60/pulizzi.json`), in order.

* An outlet with `"lamp": true` is writable. Any other listed outlet is
  read-only (`SetSwitch` raises `MethodNotImplemented`); the shipped
  config lists only the four lamps, so outlets 3, 5, 6 and 8 are not
  served at all and robofast never touches them.
* `Action("Status")`: JSON with every listed outlet (`id`, `outlet`,
  `name`, `lamp`, `on`; for lamps `max_on`, `on_for`, `remaining` and the
  lamp hours below), the lamps the max-on limit switched off (`tripped`),
  the unit's other status lines, the monitor's last error, and the
  lamp-hours file and its last error (`hours_file`, `hours_error`).
* `Action("LampsOff")`: every lamp off. Anyone may call it.
* `Action("LampReplaced", '{"lamp": "ThAr1", "by": "who", "note": "...",
  "hours": 0}')`: an **operator** records a new lamp (below). It switches
  nothing. `lamp` and `by` are required; `hours` is what the new lamp has
  already burned (default 0).

### Lamp hours

ThAr and incandescent lamps age as they burn, and are replaced from their
accumulated hours, so the server keeps them
(`robofast_core/devices/lamp_hours.py`).

* **Counted from what the unit reports**, not from commands: every status
  the server reads (its `poll_interval` monitor, and the read-back after
  every switch) is counted, so a lamp switched on by hand, by the legacy
  software while both coexist, or found on at startup counts from when
  the server first sees it on until it sees it off. A start is counted
  when a lamp is first seen on.
* Per lamp, by name: `hours` and `cycles` (starts) over the lamp
  position's lifetime; `since_replaced` (`hours`, `cycles`, `at`, `by`:
  the lamp now installed; `at` null means since tracking began, the
  earlier hours unknown); `rated_hours` (config, optional); `life_used`
  (`since_replaced.hours / rated_hours`); `life_warning` (`"near"` from
  90 %, `"past"` from 100 %, logged once each as a warning and an error).
  **Hours never stop a lamp from being switched on**: replacing it is the
  observers' decision.
* `Action("LampReplaced")` restarts the `since_replaced` counts and keeps
  the lifetime totals; the old lamp's hours, starts, who recorded it (with
  the client id) and the note are appended to the file's `replacements`
  and logged. For the lamps installed when tracking starts, record each
  once with the hours it is believed to have (`hours`).
* **The file** (`hours_file`): JSON, default
  `lamp-hours/<name>.json` under the data directory (`$ROBOFAST_DATA_DIR`,
  else `~/.cache/robofast/data`: on the site computer set
  `ROBOFAST_DATA_DIR`, or an absolute `hours_file`, to a directory that is
  kept and backed up); a relative `hours_file` is taken from the data
  directory. Written atomically (temporary file, fsync, rename) on every
  on/off, on a replacement, at a clean stop and every `hours_flush` s
  (default 60) while a lamp burns, so a crash loses at most that much. A
  restart that finds a lamp still on that the file says was on does not
  count a new start; the time the server was down is not counted.
* **Failures never get in the way of switching**: a missing file starts
  every lamp at zero (a warning); an unreadable one is moved aside to
  `<file>.corrupt-<time>`, kept, and counting starts again from zero (an
  error, and `hours_error` says so until the server restarts); a failed
  write is logged, the hours kept in memory and the write retried at the
  next flush. Any error in the tracker is logged and the switch goes on.
* The simulator keeps its hours in memory only, unless `hours_file` is set
  explicitly; `--simulate` on a hardware config always does, so a
  simulator run never adds to the real lamps' hours.
* Not in the FITS headers: the lamp server has no path into the value
  registry (the sequencer's). A lamp frame could carry its lamp's hours
  from `Status` if the TRES pipeline wants them (question 13).

### Lamp safety (decided)

* **Starting never switches anything.** The server logs in and reads
  `DX0`; a lamp found on is left on (its max-on clock starts then). This
  differs from the legacy window, which switched two lamps off at startup:
  robofast's sequencer does that at its own startup cleanup instead (below).
* **Reading never switches** (`GetSwitch`, `Status`, connecting).
* **Max-on limit, in the server.** Each lamp has `max_on` seconds
  (shipped: 1800, a placeholder; at most 4 h). The server polls the unit
  every `poll_interval` (2 s), with or without a client, and switches off
  any lamp on for longer, logging an error and listing it under `tripped`.
  The clock starts when the server switches the lamp on, or first sees it
  on (switched on by hand, or found on at startup). Switching an
  already-on lamp on again does not restart it: switch it off first. This
  holds whatever happens to the sequencer, the console or the network.
* **A clean stop switches every lamp off** (SIGTERM or SIGINT;
  `lamps_off_on_stop`, default true): a lamp left burning overheats and
  ages, and once the server is gone nothing watches it. A crash cannot do
  this; then the sequencer's cleanup (below) and, once the server is
  restarted, its max-on limit catch the lamp.
* **Power loss.** What the unit does when its own power returns is the
  unit's setting (`ISn`): "default" restores the outlets as they were, so
  a lamp that was on comes back on. Setting the lamp outlets' power-on
  state to off (preset `000`) on the unit itself is recommended (question
  5); robofast never changes it.
* The hardware gate: `port.type: "serial"` needs `"allow_hardware": true`
  in the config **and** `--hardware` on the command line; the shipped
  config has `allow_hardware: false`.

## 4. Lamp frames in requests and the sequencer

**Requests.** An `ARC` or `LAMP_FLAT` configuration names its lamp in each
instrument config's `optical_elements.lamp` (`docs/request_language.md`):

```json
{"type": "ARC", "instrument_type": "tres",
 "target": {"type": "ICRS", "name": "ThAr", "ra": 0, "dec": 0},
 "instrument_configs": [{"exposure_time": 60, "exposure_count": 3,
                         "optical_elements": {"lamp": "ThAr1"}}]}
```

Lamp frames take no sky light: they are never guided, the target is only a
label, and the Sun, altitude and Moon limits do not apply.

* **The 60-inch** lists its lamps in its dispatch limits
  (`sites/flwo60/dispatch.example.json`: `"lamps": ["ThAr1", ...]`); the
  scheduler sends a lamp configuration only to a telescope whose `lamps`
  list that lamp.
* **MINERVA** lists none (KiwiSpec is iodine only), so the request API
  still refuses every `ARC` and `LAMP_FLAT` with the same reason as before
  (`request_model.NO_LAMPS`), now at submission (the enclosure has no
  lamps) rather than while parsing. A lamp asked for in `extra_params` is
  refused everywhere, with the right way to ask.

**Sequencer.** The telescope's `lamps` entry in the enclosure config
(`sites/flwo60/flwo60.json`; `robofast_core/lamps.py`) gives the switch's
address and, per lamp, its warm-up and its calibration-stage position. An
`ARC` (`kind="arc"`) or `LAMP_FLAT` (`kind="lamp_flat"`) observation then
runs, without a lease (the dome stays as it is and the telescope does not
move):

1. read the switch; refuse (observation failed, before any lamp is on) if
   warm-up plus the frames (exposure plus `frame_overhead` each) would
   exceed the lamp's `max_on`: split such a request;
2. select the calibration stage (the instrument config's `calstage`, else
   the lamp's; `docs/tres_frontend.md`) **before** any lamp is on;
3. switch any other lamp of the telescope off (one lamp's light at a time),
   then this lamp on (`lamp_on` in the timeline);
4. wait its warm-up;
5. take the frames with the shutter open (`IMAGETYP` `ARC` or `FLAT`),
   checking before each that the lamp is still on (a lamp switched off by
   hand or by its max-on limit fails the observation);
6. switch the lamp off (`lamp_off`).

**The lamp is switched off on every way out**: an error, a preemption, a
cancelled generation or a stopped sequencer. The switch-off does not use
the generation's grant (which a cancellation revokes first) but one of its
own that is only ever used to switch off, and it is not abandoned if the
task is cancelled again. If it fails, a critical `lamp_off_failed` event
is raised. Independently, **every sequencer cleanup** (after each
generation, after a telescope error, and at the sequencer's startup, which
covers a crashed predecessor) calls `LampsOff` on each telescope's switch.
The safety supervisor does not command the lamps (it commands only the
enclosure, design.md 3.3); a lamp left on is caught by the sequencer's
cleanup and, failing that, by the switch's own max-on limit.

## 5. Running and testing

```
python -m robofast_core.devices.pulizzi_server --config sites/flwo60/pulizzi.json --simulate
```

prints `{"url": ..., "port": "simulator"}`. Tests:
`tests/core/test_pulizzi.py` (the protocol client and simulator, the
server's gate and safety behaviour, requests and dispatch, the sequencer's
lamp frames and every switch-off path) and `tests/core/test_lamp_hours.py`
(the lamp hours, on a fake clock and in the server). ConformU:
`scripts/conformu_pulizzi.py` (`conformance` with switch writes, and
`alpacaprotocol`; in CI's `conformu` job).

## 6. Confirm on the mountain

Until each is checked, `allow_hardware` stays false.

1. The unit: model and firmware (its `?` menu, read by hand on a terminal;
   robofast never sends `?`), serial or network (`-NET`) variant, and the
   serial device and settings on the computer it is cabled to.
2. The unit name (`@@@@`?) and whether a password is set (robofast does not
   handle one).
3. The exact login answer and `DX0` listing on this firmware, and whether
   "auto event" (`AE0`) is on.
4. The outlet map (ThAr1 2, ThAr2 7, Incand 4, Incand2 1), and what
   outlets 3, 5, 6 and 8 power (3 and 5 were seen on). They must never be
   switched by robofast.
5. The power-on setting (`IS0`): set the lamp outlets to come up off.
6. That the unit's watchdog is disabled.
7. Each lamp's rated duty, a safe `max_on`, its warm-up time, and TRES's
   usual ThAr and flat sequences (count, exposure), so they fit in
   `max_on`.
8. Which calibration-stage position each lamp needs (`in`, unconfirmed;
   `docs/tres_frontend.md` question 4), and whether the bench's
   calibration box is also involved (question 10 there).
9. The transition: the legacy program holds the serial line while the TCS
   window runs, so only one of them can drive the unit at a time.
10. The positions of the front-panel switches (manual overrides, remote
    disable).
11. The `IMAGETYP` (and any lamp keyword) the TRES pipeline expects for ThAr
    and flat frames.
12. That the legacy verb `lamp thar` means ThAr1 (outlet 2), and whether
    ThAr2 and Incand2 are still installed.
13. Each installed lamp's rated life (`rated_hours`; the shipped values
    are placeholders), the hours it has already burned and when it was
    installed (recorded once with `LampReplaced`), where the lamp-hours
    file lives on the site computer, and whether the TRES pipeline wants
    a lamp's hours in its frames' headers.
