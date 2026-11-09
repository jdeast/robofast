# Camera cooling

robofast keeps each imaging camera cooled by itself: nothing outside
robofast (a launcher script, an operator before the night) has to switch a
cooler on. The code is `robofast/cooling.py`; the sequencer drives it.

## Configuration

The setpoint is a property of the camera, so cooling is part of the
camera's own config: the telescope's `camera` entry in the enclosure
config, written as `{"address": ..., "cooling": {...}}` instead of a bare
address (`robofast.config.camera_entry`; `robofast/sequencer_service.py`;
`sites/minerva/aqawan1.json`):

```json
"t1": {"telescope": "...",
       "camera": {"address": "host:port/N",
                  "cooling": {"setpoint": -50.0, "tolerance": 0.75, "settle_timeout": 900}}}
```

| Key | Default | Meaning |
|---|---|---|
| `setpoint` | (required) | CCD setpoint, C |
| `tolerance` | 1.0 | the CCD is settled within this of the setpoint, C |
| `settle_timeout` | 900 | how long after the cooler was set a light frame may wait for the CCD to settle, s |
| `restart_interval` | 300 | a cooler found off is turned back on at most once per this, s |
| `poll` | 5 | temperature poll while waiting to settle, s |

A bare number is a setpoint with the defaults. A camera given as a bare
address, or without a `cooling` entry, has its cooler left alone.

There is one place for it. To migrate a config that had `cooling` beside
the camera (`telescopes.<name>.cooling`, before 2026-11-09), move the entry
unchanged into the camera:

```json
"t1": {"camera": "host:port/N", "cooling": {...}}                 (old)
"t1": {"camera": {"address": "host:port/N", "cooling": {...}}}    (the camera's own)
```

A key left beside the camera is not read: the sequencer refuses it as it
does any telescope key it does not know (`unknown keys ['cooling']`), and
the rehearsal warns about any key of a real telescope's site entry it
cannot interpret.

MINERVA: T1 (Andor) -50 C, T2 (Apogee) +5 C -- the legacy `imager_t1.ini` /
`imager_t2.ini` `SETTEMP` (T2's is legacy's odd choice, kept as is) -- with
`tolerance` 0.75 C, legacy's `MAXTEMPERROR` (`max_temp_error`). These live
only in `sites/minerva/aqawan1.json`; the reference catalog
`sites/minerva/instruments.json` does not repeat T1's and T2's (it keeps
only the out-of-service T3's and T4's).

## One path

One function sets a cooler, `cooling.set_cooling`: `SetCCDTemperature` =
setpoint, then `CoolerOn` = true. It runs:

* **Whenever robofast connects the camera afresh.** The camera's
  `DeviceOwner` runs its `on_connect` hook after every new connection:
  the first command of the night, a command after a timeout dropped the
  driver, and after every recovery rung (`reconnect`, `restart`,
  `powercycle` all reconnect; docs/recovery.md). This matters: MINERVA
  T1's Andor ASCOM driver switches the cooler **off** whenever it is
  connected again (`Connected` false -> true), so a camera recovered by a
  driver restart would otherwise warm up silently. The hook is bounded by
  the command timeout on its own (it never eats the command's time) and its
  failure is logged (`on_connect_failed`), never the command's.
* **At startup**, once the operating mode lets the sequencer command (not
  in `shadow`): the hook is installed before the startup cleanup, and a
  cooled camera the cleanup did not connect is touched, so it starts
  cooling at once rather than at its first frame.
* **When the cooler is found off before an exposure**: turned back on with
  an alarm ("cooler found off; turning it back on"), at most once per
  `restart_interval`; within it, a `cooler_off` warning event.

A camera that cannot set its temperature -- `CanSetCCDTemperature` false,
or `SetCCDTemperature`/`CoolerOn` answering NotImplemented -- raises
`Unsupported` from `set_cooling`; robofast logs it once
(`cooling_unsupported`) and leaves that camera alone for the night.

## Before exposures

Before each exposure the sequencer reads `CoolerOn` and `CCDTemperature`.
A **light** frame whose CCD is outside the tolerance (with the cooler on)
waits for it, polling, but never past `settle_timeout` after the cooler
was last set. After that the frame is taken anyway: a `cooling_not_settled`
warning event (once, until the CCD settles again), `COOLOK = F` in the
header and `ccd_settled: false` in the sequencer's frame record. Cooling
never stops the night and never fails an exposure: a camera that cannot
answer the check fails at `StartExposure`, as before.

## FITS headers

| Keyword | Source |
|---|---|
| `CCD-TEMP` | `CCDTemperature` after the exposure [C] |
| `SET-TEMP` | `SetCCDTemperature` after the exposure [C] |
| `COOLERON` | `CoolerOn` after the exposure |
| `COOLPOW` | `CoolerPower` [%] |
| `COOLOK` | (cooled cameras) the CCD was within tolerance of its setpoint before the exposure |

## Events

`cooling_set` (`why`: `connected`, `startup`, `cooler found off`),
`cooling_unsupported`, `cooling_not_settled`, `cooling_waited`,
`cooler_off`, `cooling_check_failed`, `cooling_start_failed`; the owner's
`on_connect_failed`. Sequencer events carry the telescope.

## In the rehearsal

The rehearsal (`python -m robofast.testing.rehearsal`) takes each real
camera's cooling from the same place production does: the camera's own
`cooling` entry (`telescopes.<name>.camera.cooling`) in the site's enclosure config, `--site-config FILE`
(default `sites/minerva/aqawan1.json`: T1 -50 C, T2 +5 C). There is no
command-line setpoint (the former `--cooling` is refused with a pointer
here): to change a setpoint, change the config. A real camera without
a `cooling` entry has its cooler left alone. A launcher no longer has to
switch the coolers on before the rehearsal.

## Simulation

`robofast.testing.fakes.FakeCamera` models the cooler: `CCDTemperature`
heads for the setpoint while the cooler is on (no colder than `coldest`)
and for `ambient` while it is off, at `cooling_rate` C/s (None: at once);
`can_cool = False` makes `CanSetCCDTemperature` false; and
`cooler_off_on_connect = True` models the Andor driver (the cooler goes
off when `Connected` goes false -> true, in process or over Alpaca).
`tests/core/test_cooling.py` covers the hook, startup, recovery,
Unsupported, the tolerance warning and a cooler found off mid-night;
`tests/e2e/test_rehearsal.py` runs the rehearsal with `cooling` over Alpaca.
