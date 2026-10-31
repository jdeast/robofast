# Demo: a whole simulated MINERVA RV night

One command runs a complete MINERVA night end to end. Everything runs on
your machine against simulators on loopback (127.0.0.1). Nothing contacts
hardware or the observatory network.

```sh
python -m pip install --group test              # once (pip >= 25.1)
python -m robofast_core.testing.rv_night --out /tmp/rv-night
```

It takes about two minutes. It prints the night's events as they happen,
then a summary of every spectrum, the request statuses at dawn, the roof
moves, and the result of every check. It exits with status 0 only if every
check passed. `--scale` sets the speed of the scheduler's clock (default
400 simulated seconds per real second). The CI job `e2e` runs the same
night (`python -m pytest -m e2e tests/e2e`) and uploads its output
directory as the `rv-night` artifact.

## What runs

| Piece | What it is |
|---|---|
| weather | the real weather bridge (`robofast_core.weather_bridge`, a child process) with the shipped `sites/minerva/weather.yaml`, reading a fake site Redis server and fake SAO weather pages |
| enclosure | the Aqawan PAC simulator and the real Alpaca Dome server (`aqawan_server`) |
| safety | the real supervisor (`supervisor_service`, a child process), with `sites/minerva/aqawan1.json`'s safety settings (park before close, the dome heartbeat) and shorter timings |
| T1, T2 | fake mounts served over Alpaca; simulated star-field imagers on simulated focusers (autofocus and imaging) |
| guiders | one real guider service per telescope (`robofast_core.guider.service`, child processes): simulated FAU camera, tip/tilt and backlight |
| KiwiSpec | the spectrograph PC simulator and the real KiwiSpec service (`kiwispec_service`, a child process) |
| sequencer | the real sequencer service (`sequencer_service.build` and `run`) on `aqawan1.json`. It runs three times, as `deploy/robofast-sequencer@.service` does: evening calibrations, the night (the dispatch plan of `dispatch.example.json`, with autofocus triggers and the console's `operator_control`), and morning calibrations |

The scheduler's clock starts in evening twilight on 2026-11-09 (new Moon)
and runs 400 times faster than real time. The Sun, the targets and dawn
therefore pass in about two minutes. Exposures, holds and timeouts run in
real seconds, shortened. FITS times and file names use the wall clock.

## The night

1. **Evening calibrations** with the roof closed and no lease: KiwiSpec
   biases, a dark, ThAr and a fiber flat through the spectrograph service.
   The biases and darks are true dark frames. The imagers take a bias and
   a dark.
2. **Twilight**: nothing is observable until the Sun is at -12 deg. The
   sequencer then asks for a lease, and the supervisor opens the roof
   (weather safe for longer than the reopen hold).
3. **Autofocus** on each telescope before its first spectrum.
4. **RV spectra**: six RV standards, each a `SPECTRUM` request for T1 and
   T2. One telescope leads, the other joins, and both locate the fiber
   with the backlight and guide. KiwiSpec exposes with the iodine cell in.
   One target ends on the exposure meter's counts.
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
   `operator_control`) for a few seconds and then resumes it.
9. **The imaging field**: an imaging request for T1 only, in a window in
   the middle of the night. T1 images it while T2 leads spectra alone.
10. **Dawn**: the scheduler ends the night once the Sun is above -12 deg
    and rising. The telescopes park, the lease is released and the
    supervisor closes the roof. Then the **morning calibrations** run.

## What to look at (in `--out`)

| File | What it shows |
|---|---|
| `sequencer-night.log` | the sequencer's timeline: `spectrum_session`, `spectrum_joined`, `guiding`, `spectrum_started` (with the telescopes feeding it), `spectrum_dropped`, `spectrum_done`, `cancel_requested`, `spectrograph_aborted`, `cleanup_done`, ... |
| `sequencer-evening.log`, `sequencer-morning.log` | the calibrations (`spectrograph_calibration_done`; no `observe_start`, so no lease) |
| `supervisor.log` | the supervisor: weather verdicts, the lease, `close_commanded`, the heartbeat |
| `plans/plan-2026-11-09.jsonl` | the nightly plan record (the simulated night): every decision with its score, `join`s, outcomes, the ToO, the preemptions. `python -m robofast_core.plan_record FILE` lists it; add `--replay` to re-run the decisions. The autofocus results are stamped with the wall clock, so they land in a second file named after today's night |
| `dispatch-state.json` | request statuses and the data pointers of every completed observation |
| `data/minerva/kiwispec/n*/` | one JSON sidecar per spectrum and calibration frame: the telescopes, who dropped out, guide statistics, the flux-weighted midpoint, `barycentric` (BJD_TDB and the RV correction), and the index `*.spectra.jsonl` (`docs/rv_night.md`) |
| `data/minerva/t1/n*/`, `data/minerva/t2/n*/` | FITS frames (autofocus, the imaging field, the imager calibrations) and the frame index (`docs/data.md`) |
| `aqawan1-control/holds.json`, `aqawan1-control/done/` | the console's commands and the holds |
| `sequencer-status.json` | the last status snapshot (what the request API and the console read) |
| `kiwispec.log`, `guider_t1.log`, `guider_t2.log`, `weather_bridge.log` | the services |

## The checks

`robofast_core.testing.rv_night.check` checks the following, and the demo
prints every check that failed:

* **ordering**: calibrations with the roof closed and no lease, autofocus
  before each telescope's first spectrum, at least three spectra fed by
  both telescopes, the imaging field on disk, every request (the ToO
  included) `COMPLETED` at dawn, the night ended by dawn, no telescope
  given up;
* **the interruptions**: the lease revoked for weather, the spectrum
  aborted and reported `interrupted`, then observed again; T2 dropped out
  of a spectrum that T1 finished; the console paused and resumed T2;
* **safety invariants**: the roof never opens while the weather is unsafe
  or within the reopen hold after it clears; no spectrum starts unless
  every telescope it lists is guiding; in every cleanup, every abort (the
  spectrograph and the cameras) comes before the first park; every close
  of the roll-off roof comes after every telescope has finished parking;
* **outputs**: a science sidecar for every spectrum, each with the
  barycentric correction, guide statistics and the flux-weighted midpoint;
  at least one spectrum ended on counts; the biases and darks on the
  spectrograph PC are dark frames; the plan record (with joins); the
  frame index.
