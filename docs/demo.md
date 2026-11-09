# Demo: a whole simulated MINERVA RV night

One command runs a complete MINERVA night end to end. Everything runs on
your machine against simulators on loopback (127.0.0.1). Nothing contacts
hardware or the observatory network.

```sh
python -m pip install --group test              # once (pip >= 25.1)
python -m robofast.testing.rv_night --out /tmp/rv-night
```

It takes about four minutes. It prints the night's events as they happen,
then a summary of every spectrum, the request statuses at dawn, the roof
moves, and the result of every check. It exits with status 0 only if every
check passed. `--scale` sets the speed of the scheduler's clock (default
200 simulated seconds per real second; faster leaves a slow
machine too little night for every target). The CI job `e2e` runs the same
night (`python -m pytest -m e2e tests/e2e`) and uploads its output
directory as the `rv-night` artifact.

## What runs

| Piece | What it is |
|---|---|
| weather | the real weather bridge (`robofast.weather_bridge`, a child process) with the shipped `sites/minerva/weather.yaml`, reading a fake site Redis server and fake SAO weather pages |
| enclosure | the Aqawan PAC simulator and the real Alpaca Dome server (`aqawan_server`) |
| safety | the real supervisor (`supervisor_service`, a child process), with `sites/minerva/aqawan1.json`'s safety settings (park before close, the dome heartbeat) and shorter timings, in the `robotic` operating mode (the site's default is `attended`; docs/modes.md) |
| T1, T2 | fake mounts served over Alpaca; simulated star-field imagers on simulated focusers (autofocus and imaging) |
| guiders | one real guider service per telescope (`robofast.guider.service`, child processes): simulated FAU camera, tip/tilt and backlight |
| KiwiSpec | the spectrograph PC simulator and the real KiwiSpec service (`kiwispec_service`, a child process) |
| sequencer | the real sequencer service (`sequencer_service.build` and `run`) on `aqawan1.json`. It runs three times, as `deploy/robofast-sequencer@.service` does: evening calibrations, the night (the dispatch plan of `dispatch.example.json`, with autofocus triggers and the console's `operator_control`), and morning calibrations |

One simulated clock (`clock.json` in `--out`, `robofast/clock.py`)
gives every process its time: the scheduler, the sequencer (FITS times and
directories, sidecars, autofocus results, the plan record) and the KiwiSpec
service (raw file names, exposure start times). It runs at real speed for
the evening calibrations (from half an hour before the night), then from
evening twilight on 2026-11-09 (new Moon) 200 times faster than real time,
so the Sun, the targets and dawn pass in about four minutes, and at real
speed again for the morning calibrations. Exposures, holds and timeouts
run in real seconds, shortened. The whole night is `n20261109` everywhere.

## The night

1. **Evening calibrations** with the roof closed and no lease: KiwiSpec
   biases and a dark through the spectrograph service (KiwiSpec is iodine
   only: no ThAr or fiber flats; `docs/kiwispec.md`, section 4).
   The biases and darks are true dark frames. The imagers take a bias and
   a dark.
2. **Twilight**: nothing is observable until the Sun is at -12 deg. The
   sequencer then asks for a lease, and the supervisor opens the roof
   (weather safe for longer than the reopen hold).
3. **Autofocus** on each telescope before its first spectrum.
4. **RV spectra**: six `SPECTRUM` requests for T1 and T2: a B star
   through iodine (the instrumental-profile calibration), four RV
   standards with the iodine cell in, and a template with the cell out.
   One telescope leads, the other joins, and both locate the fiber with
   the backlight and guide; each guider reports its fiber flux to the
   KiwiSpec service, which sums it into each spectrum's flux-weighted
   midpoint. One target ends on those FAU counts. Each raw file is made
   legal FITS in place, verified (`docs/kiwispec.md`, "The raw file").
5. **Bad weather** during the second spectrum: humidity goes to 95 %, the
   bridge reports unsafe, and the supervisor revokes the lease. The
   sequencer aborts the spectrum, stops the guiders and parks. The
   supervisor closes the roof once the telescopes are parked. The weather
   clears, the reopen hold passes, the roof reopens, and the interrupted
   target is observed again.
6. **A ToO** (a `SPECTRUM` request group with a `too` entry) is dropped
   into the scheduler's drop box. It is taken next, ahead of every normal
   request.
7. **T2's mount fails** during a two-telescope spectrum. T2 drops out, is
   made safe and backs off. T1 finishes the spectrum alone. The fault
   clears, and T2 comes back.
8. **The observer console** pauses T2 (an operator hold through
   `operator_control`) for a few seconds and then resumes it; T1 leads
   spectra alone meanwhile (a held telescope is not reserved). Then it
   parks T2 alone at once (`park`: only T2's mount moves; the roof stays
   open and T1 goes on) and resumes it again.
9. **The imaging field**: an imaging request for T1 only, in a window in
   the middle of the night. T1 images it while T2 leads spectra alone.
10. **Dawn**: the scheduler ends the night once the Sun is above -12 deg
    and rising. The telescopes park, the lease is released and the
    supervisor closes the roof. Then the **morning calibrations** run.

## Watching it in the observer console

During the night the demo runs the observer console (`docs/console.md`)
on loopback against the night's services, and prints its address and
where its operator token is:

```
[    8.9 s] observer console: http://127.0.0.1:43707 (token in /tmp/rv-night/console-token.txt)
```

Open the address in a browser on the same machine (`--console-port 8400`
fixes the port; `--console-port -1` runs without it) and paste the token
(`demo`, scopes `read` and `operator`). You see the night as an observer
would: the alarms (the bad weather, T2's mount), the enclosure and the
lease, both telescopes, the guider thumbnails with the fiber and the
centroid while they guide, the spectrograph's jobs, the sky chart with the
RV standards rising and setting on the simulated clock, the plan, and the
merged log. The emergency-close button and manual control work too (the
first closes the simulated roof; nothing here touches hardware). Mid-night
the demo reads the console's status, sky, log and a guider image itself
and checks them (`console` in the checks).

## What to look at (in `--out`)

| File | What it shows |
|---|---|
| `sequencer-night.log` | the sequencer's timeline: `spectrum_session`, `spectrum_joined`, `guiding`, `spectrum_started` (with the telescopes feeding it), `spectrum_dropped`, `spectrum_done`, `cancel_requested`, `spectrograph_aborted`, `cleanup_done`, ... |
| `sequencer-evening.log`, `sequencer-morning.log` | the calibrations (`spectrograph_calibration_done`; no `observe_start`, so no lease) |
| `supervisor.log` | the supervisor: weather verdicts, the lease, `close_commanded`, the heartbeat |
| `plans/plan-2026-11-09.jsonl` | the nightly plan record: every decision with its score, `join`s, `held` entries, outcomes, the autofocus results, the ToO, the preemptions. `python -m robofast.plan_record FILE` lists it; add `--replay` to re-run the decisions |
| `dispatch-state.json` | request statuses and the data pointers of every completed observation |
| `clock.json` | the simulated clock's current phase (start, scale, origin) |
| `data/minerva/kiwispec/n*/` | one JSON sidecar per spectrum and calibration frame: the telescopes, who dropped out, guide statistics, the flux-weighted midpoint, `barycentric` (BJD_TDB and the RV correction), and the index `*.spectra.jsonl` (`docs/rv_night.md`) |
| `data/minerva/t1/n*/`, `data/minerva/t2/n*/` | FITS frames (autofocus, the imaging field, the imager calibrations) and the frame index (`docs/data.md`) |
| `aqawan1-control/holds.json`, `aqawan1-control/done/` | the console's commands and the holds |
| `console-audit.jsonl`, `console-token.txt` | the console's audit log; the demo operator's token |
| `sequencer-status.json` | the last status snapshot (what the request API and the console read) |
| `kiwispec.log`, `guider_t1.log`, `guider_t2.log`, `weather_bridge.log` | the services |

## The checks

`robofast.testing.rv_night.check` checks the following, and the demo
prints every check that failed:

* **ordering**: calibrations with the roof closed and no lease, autofocus
  before each telescope's first spectrum, at least three spectra fed by
  both telescopes, the imaging field on disk, every request (the ToO
  included) `COMPLETED` at dawn, the night ended by dawn, no telescope
  given up; one night name (`n20261109`) for every directory, file,
  sidecar, raw spectrum and plan record;
* **the interruptions**: the lease revoked for weather, the spectrum
  aborted and reported `interrupted`, then observed again; T2 dropped out
  of a spectrum that T1 finished; the console paused and resumed T2, and
  parked T2 alone (T2 parked, T1 not, no roof command meanwhile); the
  console answered with the night's status, sky targets, a log with the
  supervisor's and the sequencer's entries, and a guider image;
* **safety invariants (as checked by the demo)**: the roof never opens while the weather is unsafe
  or within the reopen hold after it clears; no spectrum starts unless
  every telescope it lists is guiding; in every cleanup, every abort (the
  spectrograph and the cameras) comes before the first park; every close
  of the roll-off roof comes after every telescope has finished parking;
* **outputs**: a science sidecar for every spectrum, each with the
  barycentric correction, guide statistics and the flux-weighted midpoint;
  at least one spectrum ended on counts; the biases and darks on the
  spectrograph PC are dark frames; the plan record (with joins); the
  frame index.

## Hardware rehearsal: real mounts and cameras, the enclosure closed

`robofast.testing.rehearsal` runs the same night **in real time** with,
for the telescopes you name, the **real mount** (its Alpaca Telescope, and
the Focuser and Rotator read for the FITS headers) and the **real science
camera** in place of the fakes. Everything else stays simulated as above:
the weather (forced safe), the Aqawan PAC simulator and Dome server (the
*simulated* roof opens; the real enclosure is never contacted), the
supervisor, the guiders (simulated FAU cameras that acquire and guide on
synthetic star fields wherever the real mount points) and KiwiSpec. A
telescope you do not name stays simulated. It is for an attended session
with the **enclosure closed**: it checks that the sequencer drives the real
mounts and cameras through a whole night (slews, tracking, exposures,
downloads, headers, the end of the night) without opening anything.

Start the real device servers first, yourself, as for any hardware session
(the PWI servers with `--hardware`, docs/pwi.md; the cameras behind ASCOM
Remote, docs/remote.md), and unpark the mounts. Then, for example:

```sh
python -m robofast.testing.rehearsal --out /tmp/rehearsal \
    --telescope t1=127.0.0.1:11121/0 --camera t1=127.0.0.1:7151/0 \
    --telescope t2=127.0.0.1:11122/0 --camera t2=127.0.0.1:7152/0 \
    --i-know-the-enclosure-is-closed --duration 2h --end-action leave
```

* **Endpoints**: every real telescope needs both `--telescope NAME=HOST:PORT/N`
  and `--camera NAME=HOST:PORT/N`. The Focuser and Rotator are device `N`
  on the telescope's server unless `--focuser`/`--rotator` say otherwise
  (`--rotator t1=none`: no rotator). It refuses to start without
  `--i-know-the-enclosure-is-closed`, with a parked mount (it never sends
  a real mount `Park` or `Unpark`), a busy camera or an endpoint that does
  not answer -- unless that telescope has a recovery policy (`--recovery`,
  below): then it gets its ladder once and, still failing, is dropped while
  the others observe on (it refuses when no real telescope is left). It logs, for each endpoint, whether it is a verified robofast
  simulator (`robofast/hwgate.py`) or real.
* **What it starts** is only the simulated night's services, each on a
  verified simulator and none with `--hardware`: nothing it starts can
  reach a real enclosure, PAC or PDU. The hardware gate is unchanged: the
  real device servers are yours, and the rehearsal only talks Alpaca to
  them.
* **The mount guard** (`robofast/testing/mount_guard.py`): the
  supervisor and the sequencer reach each real mount only through it.
  Reads pass. It forwards `SlewToCoordinatesAsync` only within 21..83 deg
  of altitude (robofast's MINERVA limits) at that moment, and
  `AbortSlew`, `Tracking` and `Connected`; robofast's `Park` becomes
  "abort the slew, tracking off, stay where you are" (`AtPark` then reads
  true until robofast's next `Unpark`); every other command is refused and
  reported, except `Action("Status")` and, for a telescope with
  `--recovery`, what its recovery ladder sends (`MountConnect`,
  `MotorsEnable`, `FindHome`; listed in the summary). `AtPark` reads the
  guard's flag only once the real mount has answered its own. The camera
  is used directly.
* **`--recovery NAME=FILE`** (opt-in, per real telescope): the JSON is a
  sequencer `recovery` entry (docs/recovery.md; no `powercycle`), e.g.
  `{"telescope": {"steps": ["reconnect", "restart", "home"], "restart":
  {"command": [...]}}}` with the site's PWI restart hook. In the night it
  is that telescope's recovery ladder in the sequencer, as in production
  (a PWI that dies is restarted, PWI reconnected to the mount, the mount
  homed if PWI says it is not; the PWI server reloads the port's pointing
  model with `pointing_models`). At the preflight a failing telescope gets
  the ladder once, then is read again, and is dropped (a WARNING, a
  problem in the checks) if it still fails.
* **Cooling from the site config** (`--site-config FILE`, default
  `sites/minerva/aqawan1.json`, the enclosure config production runs on):
  each real telescope's `cooling` entry there keeps its real camera cooled
  (docs/cooling.md), as in production: the cooler is set when the
  sequencer starts and whenever it connects or reconnects the camera (every
  recovery rung: MINERVA T1's Andor driver switches its cooler off on a
  reconnect), and turned back on, with an alarm, if found off. A real
  telescope without a `cooling` entry has its cooler left alone. There is
  no command-line setpoint (`--cooling` is refused): the config is the one
  source.
* **Real time.** The clock runs at real speed from now (the real mounts
  point by the real sidereal time). The plan is made at start: `SPECTRUM`
  requests (a B star through iodine, RV standards with iodine, a template
  without), enough for the duration, and `--images` short imaging requests
  per telescope (`--image-exptime`, two frames each), on the stars of
  `sites/minerva/requests.example.json` plus a few bright stars in the
  module (so that something is up at any sidereal time) that are within
  the limits during the rehearsal. It ends after `--duration` or at
  morning twilight, whichever is first (or once every request is done).
  Before evening twilight nothing is observed until the Sun is at -12 deg;
  `--ignore-twilight` observes at once, keeping targets 45 deg from the Sun
  (only with the enclosure closed).
* **No autofocus** on a real telescope (dark frames cannot be focused: a
  log line says so). The real camera takes light frames (the shutter opens
  inside the closed enclosure); nothing measures their content (no FWHM, no
  plate solving). The frames, the frame index and the spectrum sidecars are
  written as usual, the headers with the real mount's pointing.
* **The end** (also on Ctrl-C or an error): the guard is locked, then each
  real mount gets `--end-action`: `leave` (abort any slew, tracking off,
  where it is; the default) or `altaz:ALT,AZ` (tracking off, then a slew
  there, within the limits; it waits, up to `end_slew_timeout`, until the
  mount reads within 0.5 deg of ALT,AZ, not just `Slewing` false, and only
  then sends tracking off again: on PWI2 a tracking off mid-slew halts the
  slew. Not there in time is reported, tracking left as is). A `kill -9`
  skips it: stop the mounts yourself then.
* **`--kiwispec URL`** (opt-in): use an already-running KiwiSpec service
  (`robofast.devices.kiwispec_service`) -- the **real spectrograph** --
  instead of starting the simulated one. The guiders' flux reports and the
  sequencer's spectra go to it, so the spectra are taken by the real
  spectrograph (the enclosure closed: dark spectra, as intended). Before
  anything starts the rehearsal sends it `GET /status` and refuses if it
  does not answer; it logs that KiwiSpec is EXTERNAL. Nothing in the
  rehearsal sends it `/restart_camera` or `/backlight` (the guiders'
  backlight stays simulated).
* **`--catalog-guide`** (opt-in): the simulated FAU guide cameras show the
  **real catalog star field** around each target (`robofast.catalog`,
  rendered by `robofast.testing.skysim.GuiderSky` through the guider
  service's `sim.sky` section) instead of one synthetic star: the real
  FAU's sensor (ZWO ASI174, 1936 x 1216), plate scale and fiber position
  (`sites/minerva/guider_tN.example.json`), the target a few pixels off
  the fiber (a pointing error), full-frame acquisition as on the real FAU.
  `--catalog-source` (`gaia`, the default; `ucac4` or `2mass`, from IRSA)
  and `--catalog-dir` (default `$ROBOFAST_CATALOG_DIR`, else
  `~/.cache/robofast/catalog`) choose the catalog. Before anything starts
  it queries the catalog around every candidate target (downloading
  missing tiles, so the guiders never wait) and refuses, naming the
  target, if the catalog cannot answer; the guiders then read the cache
  only.

It prints the night as it goes (slews with the pointing error, frames,
spectra), then a summary: the spectra, the request statuses at the end,
every slew of a real mount (commanded position, the mount's position after
it, the pointing error and how long it took), any telescope dropped at the
preflight and every recovery (preflight and night: what each rung did), every frame (exposure time,
download time and rate, the header's pointing), anything the guard refused,
the simulated roof and the end action. The checks: no telescope dropped, spectra fed by every
real telescope, no autofocus on a real one, the night ended by itself,
nothing refused by the guard, every slew within the limits and ended
(one cut short by a telescope error is that error's), the safety
invariants above (never a spectrum unguided, every abort before a park,
every park before the roof closes), a sidecar with the barycentric
correction for every spectrum, every real frame on disk with the mount's
pointing within a degree of its target, one night name, and every real
mount not tracking after the end action. The tests
(`tests/e2e/test_rehearsal.py`, `tests/core/test_rehearsal_guard.py`;
recovery: `tests/e2e/test_rehearsal_recovery.py`,
`tests/core/test_pwi_recovery.py`, with PWI2 simulators) run it
against fake mounts and cameras served over Alpaca on loopback as if they
were real, with a faster clock (test only).
