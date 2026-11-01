# robofast design

Status: draft, 2026-10-30. Decision: build robofast's own core on ASCOM Alpaca,
starting from the `robofast_core` prototype, and borrow ideas from existing
frameworks rather than build on one of them.

## 1. Goals

robofast is open-source software that runs a robotic observatory unattended,
night after night, and fails safe. Targets, in order:

1. MINERVA (two Aqawan roll-off enclosures, CDK700 telescopes, a fiber-fed
   spectrograph and imagers).
2. MEarth North and South (several telescopes under one roof; North's
   telescopes can collide).
3. Eventually FLWO 48" and 60", and Tierras.

Requirements that drive the design:

- **Bad weather always wins.** Whatever else is happening or broken, an
  unsafe or unknown weather state closes the enclosure, quickly, without
  asking anything else for permission.
- **No two programs ever command the same device.**
- **Every wait is bounded.** A hung device costs one timeout, never a night.
- **Recovery is automatic.** After the weather clears, observing resumes
  with a fresh plan, not with whatever was in flight when it stopped.
- **Everything runs against simulators in CI** before it touches hardware.
- **Adding a site is configuration plus device servers,** not a fork.

Non-goals for now: data reduction pipelines, a web portal, multi-site
network scheduling.

## 2. Why a new core

The legacy MINERVA code (Python 2, threads, flag files) loses nights and
occasionally leaves hardware unsafe when weather interrupts observing. Its
failure patterns, which this design exists to remove:

- control authority held in flag files with no owner;
- threads whose errors surface only on `join`, so one telescope's error ends
  the night;
- locks not released on error; recursion used for retries;
- no timeouts on I/O;
- sentinel return values (`False`, `'out of bounds'`, -99) in place of
  exceptions, and failure paths that default to "proceed";
- weather staleness limits computed once, so frozen data is trusted.

We evaluated two existing frameworks hands-on, on the same four scenarios:

| Scenario | Rockit | pyobs | robofast prototype |
|---|---|---|---|
| S1: weather unsafe mid-exposure | closes and aborts, but the mount keeps tracking | aborts, parks, closes | aborts, parks, closes (4.2 s) |
| S2: feed errors or hangs | closes | closes (errors ~2 s, hangs ~33 s) | closes (4-6 s) |
| S2: feed frozen while reporting "safe" | closes | trusted; observing continues | closes |
| S3: reopen after recovery | never reopens that night; stale actions resume | nothing reopens | reopens after the hold with a fresh plan |
| S4: a device or its daemon hangs or dies | hung dome still reported open | dead roof module: nothing closes, no alarm | bounded by timeouts; closes |
| Second client moves the telescope mid-exposure | not tested | accepted | refused by design |

Both frameworks' failures are architectural (no device ownership, no single
safety authority), so fixing them means rewriting their cores. We borrow
their good ideas instead (section 9).

## 3. Architecture

```
          request API (submit / status / cancel; OCS-shaped)
                              |
                         scheduler  ---- nightly plan (JSON)
                              |
          +-------------------+-------------------+
          |                                       |
   sequencer (enclosure A)                 sequencer (enclosure B)
     |  holds an open lease                  |
     |  grants: telescopes, cameras, ...     |
     |                                       |
     |        safety supervisor (separate process; the only
     |          one allowed to open or close; fails closed)
     |             |  weather   |  enclosures   |  heartbeat
     v             v            v               v
  device owners  ------------  ASCOM Alpaca over HTTP  ------------
     |                |                |               |
  telescope (PWI4)  cameras   enclosure server   weather server
  via ASCOM Remote  (vendor   (robofast-written) (robofast-written)
  on Windows        drivers)
```

### 3.1 Device layer: Alpaca everywhere

- Every device is reached through **ASCOM Alpaca** (HTTP/JSON). That gives
  one protocol, off-the-shelf simulators (OmniSim) for CI, and a conformance
  checker (ConformU) for any server we write.
- **Windows-only drivers** (PWI4, vendor camera drivers) are exposed by
  ASCOM Remote, or a native Alpaca server, on a minimal per-telescope Windows
  PC. MaxIm is not in the control path.
- **Devices we write servers for** (enclosures, PDUs, site weather, MEarth's
  roof and mounts, the FLWO 60-inch's DFM TCS) are small Python Alpaca servers. Each has hard timeouts,
  soft limits and a simulator mode, and must pass ConformU in CI.
- **Non-Alpaca instruments** (the spectrograph, iodine cell, exposure meter)
  follow the same pattern: one owning service, a small typed HTTP API, a
  simulator. Not a parallel framework.
  KiwiSpec is the first (`robofast_core/devices/kiwispec_service.py`,
  `docs/kiwispec.md`): it alone talks to the spectrograph PC's legacy server
  and SI camera server, runs one job at a time with every step bounded,
  terminates on exposure-meter counts and reports the flux-weighted
  midpoint, and gives the sequencer's cleanup a bounded abort that never
  raises. Like the Aqawan server, it refuses a non-loopback PC without
  `allow_hardware` in the config and `--hardware`.
- **The guider** (`robofast_core/guider/`, `docs/guider.md`) is one such
  service per guide camera, run on the computer the camera is plugged into
  so frames never cross the network in the loop: camera, centroider,
  PI(D) controller, a measured device-to-pixel calibration, and a
  tip/tilt (Starlight Xpress AO, PI E-518) or mount offsets, with
  offloading to the mount and fiber acquisition. Every real backend is
  behind the same `allow_hardware` + `--hardware` gate.
- Vendor extras that Alpaca lacks (PWI4 M3 port selection, fans, pointing
  models) use Alpaca `Action()` or a typed side channel per device client.
- **robofast ships its own small asyncio Alpaca client.** alpyca serializes
  every request in a process behind one lock with a fixed 5 s timeout, so a
  hung device delays the safety supervisor's close. Our client has per-call
  timeouts and no shared lock.

### 3.2 Device owners and grants

A `DeviceOwner` is the only path to one device (`robofast_core/owner.py`).

- Components never hold a device, only a revocable `Grant`.
- The owner checks the grant immediately before each command goes on the
  wire, so a cancelled task cannot send anything after it was fenced off.
- Every command has a timeout; a timeout or device error is an exception,
  never a sentinel value.
- Some members are **privileged**: only the safety supervisor's grant may
  call `OpenShutter`/`CloseShutter`.
- Every command and outcome is recorded in a timeline, which the tests
  assert on.
- Operations that conflict ("camera integrating blocks telescope move") are
  declared as data, RTS2-style, instead of ad-hoc locks.

### 3.3 Safety supervisor

The only component allowed to open or close an enclosure
(`robofast_core/safety.py`). Policy:

- The enclosure is open only while (a) the weather has been **continuously
  safe for the reopen hold**, and (b) a live sequencer holds an unexpired
  **open lease**.
- Anything else closes: unsafe weather, a reading that errors or times out,
  data older than the staleness limit, no successful read within the limit,
  an unrenewed lease, or no lease.
- With several weather sources: unsafe if any is unsafe, or if all are
  unknown; each source has its own staleness limit.
- It never asks before closing. It revokes the lease (telling the sequencer
  to stop and clean up) and closes in the same step.
- Two independent loops: weather polling and enclosure enforcement. The
  enforcement loop runs its own staleness check, so a stuck weather loop
  still fails closed.
- **It runs as its own process**, small and separately supervised (systemd
  on Linux), so an observer crash cannot stop it.
- **Hardware heartbeat behind it.** Where the enclosure controller supports
  it (the Aqawan's does), the supervisor pings it; if the pings stop, the
  hardware closes on its own. That makes every software bug survivable.
- **A crashed sequencer's lease.** It expires and the enclosure closes, but
  it is never released, so nothing is granted until it is cleared. Only its
  restarted sequencer (`recover`, same holder name) or an operator (`clear`,
  logged with their name) can clear it, and only once it is no longer live
  and the enclosure is confirmed closed. The supervisor never clears one by
  itself, since it cannot know the telescope is safe; after
  `orphan_timeout` it alarms. A lease the sequencer quarantined is never
  recovered, only cleared.
- **Operators can close, never open.** `POST /close` (the observer
  console's enclosure stop, `docs/console.md`) revokes the lease, logged
  with the operator's name and reason; the sequencer cleans up as for any
  revocation and the enclosure closes. There is no operator open: opening
  happens only by the policy above.
- **Park before reopen.** No lease is granted until every telescope in the
  enclosure reports `AtPark` (read-only checks, all at once). One that is
  unparked or cannot be read raises an alarm naming it, and nothing is
  granted, so nothing reopens on a telescope that a crash or a failed
  cleanup left unparked. A dead mount therefore keeps the enclosure closed
  for its neighbours too, until it is fixed or removed from the config.

### 3.4 Sequencer, one per enclosure

`robofast_core/sequencer.py`. Each "generation" of observing:

1. request an open lease and wait for the supervisor to open;
2. take grants for this generation only, per telescope;
3. observe as one asyncio task (one child task per telescope, section
   3.5), each **re-planning from scratch** before every observation (never
   resume a stale action);
4. on lease revocation: revoke the grants (fence), cancel the task, wait a
   bounded time for it to finish; if it doesn't, **quarantine** (never
   release the lease, so nothing reopens) and alarm;
5. clean up with fresh grants: abort every exposure, park every telescope,
   all in parallel and bounded;
6. release the lease; only then may a new generation start.

At startup, before its first lease, the sequencer waits while a live lease
of its own holder name is outstanding (a crashed predecessor's, until it
lapses), cleans up every telescope (always: a crash can leave one unparked
even with no lease outstanding), and then recovers the predecessor's lease
if there is one. Its name must be stable across restarts and unique per
enclosure; one process manager unit per enclosure keeps it so. If two
processes do share a name, the supervisor still never recovers a live lease,
recovery is idempotent, and they take turns, one lease at a time.

Repeated device errors back off, then give up for the night and alarm,
per telescope (section 3.5). Before that, a device with a recovery policy
climbs its recovery ladder -- reconnect, restart the vendor software,
power-cycle its PDU outlet (`docs/recovery.md`) -- and the telescope gives
up for the night once the ladder is used up.

**Plans.** The sequencer asks a *plan provider* for each telescope's next
observation whenever that telescope is free, and reports how each ended
(`robofast_core/plan.py`):

```python
class PlanProvider(Protocol):
    async def next_observation(self, telescope: str) -> Observation | None: ...
    def report(self, telescope: str, obs: Observation, outcome: str, frames: int) -> None: ...
```

`None` means nothing more for that telescope tonight; an `Idle(seconds)`
means nothing to do now (the telescope stops tracking, stays where it is,
and asks again after `seconds` or when woken by a preemption); outcomes are
`complete`, `interrupted` (the enclosure closed, the sequencer stopped, or
a target of opportunity preempted it) and `failed` (a device error or a
refused slew). A plan may also offer `peek(telescope)`: the same answer
without handing it out or changing anything. The dispatch scheduler
(section 3.7) implements it all; there is also a static target list and a
calibration-only plan (bias and dark frames). The night ends, and the
process exits, when every telescope's plan is done or it gave up.

**Idle telescopes release the enclosure.** A short idle (a target about to
rise, a gap between visits) keeps the enclosure open: the telescope stops
tracking, so it cannot track into a limit, and stays where it is, ready to
slew; closing and reopening would cost the shutter travel twice and risk a
reopen hold. Once *every* telescope of the generation has been idle for
`idle_release_after` (15 min), the generation ends cleanly: the telescopes
park, the lease is released, the supervisor closes.

**The closed phase.** Before every lease the sequencer asks the plan,
holding no lease, what each telescope would do now (`peek`; a plan without
it is asked `next_observation` and given the answer back as `interrupted`
with no frames, so nothing stale is acted on later). If any telescope has
light work it requests a lease (the reopen hold applies as usual). If the
only work is dark frames (`light=False`: biases, darks), it takes them in a
*closed generation*: grants and cleanup as usual, no lease, enclosure
closed, telescopes parked. Otherwise it waits out the shortest `Idle`
(woken early by a preemption request, e.g. a ToO) and asks again. So a
calibration plan never opens the enclosure, and the sequencer started at
dusk opens only once something is observable. Dark frames a plan hands out
during an open generation are taken there.

**Filters and M3 ports.** An observation may name a `filter` and a `port`.
The filter is looked up by name in the telescope's optional Alpaca
FilterWheel (`Names`) and the wheel moved to it, bounded by
`filter_timeout`; the port is selected with the telescope's Alpaca
`Action(port_action, port)` (configurable per telescope: PWI4 on the
CDK700s), bounded by `port_timeout`. Both happen before the exposures,
while the enclosure opens. A telescope without a filter wheel or a
`port_action`, or a filter its wheel lacks, fails that observation (an
ordinary error of that telescope) rather than observe through the wrong
one; the dispatch scheduler already keeps such requests off it (section
3.7).

**Autofocus.** An observation of `kind="autofocus"` (or a request
configuration of type `AUTO_FOCUS`) focuses its telescope on its field,
through its filter and port: the lease, the slew, the grants (the focuser's
is fenced with the others) and the cleanup are the usual ones
(`robofast_core/autofocus.py`, `docs/autofocus.md`). The default method takes
two exposures far out on one known side of focus, where the star's size is
set by the optics and not the seeing, and computes the focus from a
calibrated V-curve slope; a donut method needs one, a sweep nine. A focus
that cannot be measured is reported `failed` without a backoff (the
telescope observes on at its old focus); one cut short is undone before the
telescope's next light observation. `autofocus_triggers` inserts them at
the start of the night, after a temperature change, or when the image
pipeline's FWHM degrades. The temperature model only predicts where to
start; each measured focus re-anchors it.

**Evening start, morning end.** The sequencer process runs one night.
`deploy/robofast-sequencer@.timer` starts its unit every afternoon; the
unit takes evening calibrations (closed) while the Sun is up, waits with
`python -m robofast_core.when --sun-below -6 --config <enclosure.json>`
(astropy, the site from the config; at once if it is already dark, e.g.
after a restart), then runs the night. The dispatch scheduler ends the
night once the Sun is above its `max_sun_alt` and rising; on that clean
exit `deploy/robofast-calibrations@.service` takes the morning
calibrations.

**Guiding.** An observation with `guide` (a request configuration's
`"guide": true`) starts the telescope's guider once on target, which must
reach `guiding` within its acquisition timeout; each exposure first checks
that the guider has not failed; the observation's end stops it. Cleanup
and a telescope's error path stop the guider *before* aborting the slew
and parking. A guider failure is that telescope's ordinary error. The
guider is a separate service reached over HTTP (section 3.1,
`docs/guider.md`); per telescope, `guider` in the enclosure config.

**Saving frames.** With an `images` entry in the enclosure config, every
frame is saved (`robofast_core/imaging.py`, `docs/data.md`). The image is
downloaded right after `ImageReady`, by the Alpaca ImageBytes transfer with
JSON as the fallback, with a timeout sized by the image. This happens in
the telescope's task and under its generation grant, because the camera
holds only one image and the next exposure discards it. A `now-after-readout`
preemption therefore waits for the download as well. Building the header and
writing the FITS file run in a bounded background task per telescope, which
overlaps the next exposure; if the writer falls `queue_size` frames behind,
the next exposure waits. An observation is reported `complete` only once its
frames are on disk, with their paths, so the request API's data endpoint
returns real files. If a write fails, the pipeline raises an alarm and that
telescope gives up for the night. Frames are never dropped silently.

**Entry point.** `python -m robofast_core.sequencer_service --config
<enclosure.json>` builds the owners, the lease client and the plan from the
enclosure's config file, which the supervisor service reads too
(`robofast_core/config.py`; `sites/minerva/aqawan1.json` is MINERVA's
Aqawan 1 with T1 and T2, on placeholder addresses). A telescope entry may
add `filterwheel` (an Alpaca address) and `port_action`. SIGTERM cleans up and
releases the lease; exit status 3 means "needs an operator" (quarantined, or
a telescope gave up) and is not restarted. The config is JSON for now; YAML
is read when PyYAML is installed.

The sequencer's states (startup, calibrations, observing, weather hold,
shutdown) are an explicit graph defined as data, POCS-style, with states
tagged as safe to enter in any weather.

### 3.5 Several telescopes per enclosure

The enclosure sequencer arbitrates its telescopes: one lease per enclosure,
grants per telescope (`TelescopeUnit`: a telescope and its camera). MINERVA's
Aqawan 1 holds T1 and T2, which cannot collide; MEarth North's telescopes
can.

- **Each telescope observes in its own task**, children of the generation's
  task in an asyncio `TaskGroup`. A telescope task catches its own errors
  (device errors, timeouts, refused slews, plan errors, bugs), so one
  telescope's failure never cancels its siblings: the legacy bug where one
  telescope's exception ended the night.
- **Backoff and give-up are per telescope.** After an error the telescope
  aborts its exposure and parks (bounded, with its fenced generation
  grants), waits `error_backoff` while the others keep observing, and after
  `max_consecutive_errors` in a row gives up for the night (alarm) while the
  others carry on. When no telescope is left working, the generation ends,
  the lease is released and the enclosure closes; a telescope still in
  backoff then waits a full `error_backoff` with the enclosure closed, so a
  dead camera cannot cycle the roof.
- **Quarantine stays per generation:** one lease, one fence over every
  telescope's grants, one bounded cancel. Cleanup aborts every camera and
  parks every telescope in parallel; so does the startup recovery.
- **Supervisor:** no lease unless every telescope reports `AtPark`
  (section 3.3); on a roll-off roof, `park_before_close` parks them all in
  parallel within `park_wait`, then closes regardless.
- **Collision checking hook.** Before every slew the telescope task asks a
  `SlewGuard` (default: allow). MEarth North's **collision checker** will be
  one: it vets each target and path against the other telescopes'
  positions and raises `SlewDenied`, which counts as that telescope's
  error. Parking is never vetted (cleanup must always be able to park), so
  MEarth also needs a park order or park positions that cannot collide.

### 3.6 Telescopes and pointing models

The telescope layer hides the mount type. robofast keeps its own pointing
model per telescope and M3 port (`robofast_core/pointmodel.py`,
`docs/pointing_model.md`): built from plate solves by a pointing run (a
`POINTING_MODEL` request) and from every solved frame, terms chosen by
significance and cross-validated, applied to every slew (optionally with
the vendor's model switched off), inverted for the FITS headers, with a
rolling local correction and a nightly drift alarm. Where the mount
software's own model is kept (PWI4 on the CDK700s, for now), robofast's
can run disabled, measuring only. Each port's image PA and parity against
the rotator's mechanical angle is measured from plate solves and saved
(`robofast_core/rotcal.py`, `docs/rotcal.md`); the frame pipeline's WCS,
the plate solver's hint and the guider's mount matrix read it.

Where a telescope really points is measured, not assumed (`docs/sky.md`):
robofast has its own star catalog (Gaia DR3 tiles cached on disk, a
bundled bright-star list, the WDS), a near-blind plate solver that every
light frame, acquisition and pointing-model run can use, and TPOINT-style
pointing-error terms (`robofast_core/pointing.py`) that the sky simulator
uses as the mount's "true" errors and that a pointing-model fit recovers.
Solved frames carry their WCS and a photometric zero point, for throughput
monitoring.

Focus is robofast's own (section 3.4, `docs/autofocus.md`): per telescope,
M3 port and filter, a calibrated V-curve and a temperature model whose zero
point follows each measured focus, kept in a JSON store. Focuser units and
direction are configuration (`um_per_step`, `focal_plane_factor`, `side`),
and the calibration checks them against the optics, since the TCSGO manual
may be wrong about the 60-inch's focus.

### 3.7 Planning and the request API

- **Requests** are written in the LCO OCS request language, with its field
  names, nesting and units (`robofast_core/request_model.py`;
  `docs/request_language.md` field by field), so OCS clients such as TOM
  Toolkit's `OCSFacility` work unchanged: RequestGroup (`proposal`,
  `ipp_value`, `observation_type`) → Request (`windows`, `location`) →
  Configuration (`type`, `instrument_type`, a `target` in degrees, optional
  tighter `constraints`, a status) → InstrumentConfig (`exposure_time`,
  `exposure_count`, `optical_elements.filter`). robofast's own fields live
  in the `extra_params` of their level: a request's cadence, several
  telescopes and `max_attempts`; a configuration's `allow_partial`; an
  instrument config's M3 `port`, `iodine` and exposure-meter
  `target_counts`; altitude, Sun and hour-angle constraints; the ToO
  deadline, policy and class. The previous robofast JSON still loads for
  one release (converted on load). Status per
  configuration: PENDING, ATTEMPTED, COMPLETED, FAILED (after
  `max_attempts` failed attempts; interruptions do not count), EXPIRED,
  CANCELED; requests and groups report the OCS `state`. A request with a
  cadence (`extra_params.cadence`: `min_interval`, `max_per_night`) is
  *standing*: it returns to PENDING after each visit, as MINERVA's RV
  targets do. Requests load from JSON files, validated with errors that
  name the entry; `from_legacy_csv` converts a legacy MINERVA target
  spreadsheet export. An InstrumentConfig may name a `filter` and an M3
  `port`; a Configuration may set `allow_partial`.
- The scheduler is a **dispatch scheduler** (`robofast_core/dispatch.py`),
  RTS2/POCS-style: whenever a telescope is free it evaluates every request
  for that telescope *now*. Hard constraints first: the telescope's
  instruments and the request's allowed telescopes; time windows (the
  whole visit must fit); cadence and nightly maximum; Sun altitude
  (twilight); and, at the start, middle and end of each configuration's
  part of the visit, altitude within the telescope's limits, airmass, hour
  angle limits and Moon separation. A configuration's constraints can only
  tighten the telescope's and site's. **An exception while evaluating a
  request excludes that request and is logged; a NaN fails its
  constraint**: nothing defaults to observable (the legacy scheduler read
  but never applied its altitude and Moon limits, and gave a target whose
  window computation raised a 100-year window). Then the score:

  ```
  score = priority
        + w_setting * max(0, 1 - t_left / setting_horizon)
        + w_airmass * airmass_at_transit / airmass_now
        + w_cadence * min(1, max(0, (t_since_last - min_interval) / min_interval))
  ```

  `t_left` is the time until the target is lost (below its altitude limit,
  past its hour-angle limit, its window's end, or a ToO's deadline); all
  three bonuses lie in [0, 1], weights default to 1 and `setting_horizon`
  to 2 h. Ranking is by priority class (ToOs above normal requests), then
  score, then id, so decisions are reproducible. Positions come from
  astropy, vectorized over all requests (one decision over a few hundred
  configurations takes tens of milliseconds), with IERS downloads turned
  off so a decision never waits on the network.
- A **visit** (a request's configurations in order, one observation per
  instrument config) is re-checked against the hard constraints between its
  observations and abandoned if it no longer passes or a ToO outranks it.
  `interrupted` re-queues the configuration (ATTEMPTED), `failed` counts an
  attempt. An interrupted or failed configuration is re-taken whole, unless
  it sets `allow_partial`: then the frames already taken are kept, and the
  next visit takes only the rest (the planned duration shrinks to match).
  Status, completed-visit history (for cadence and nightly counts),
  `allow_partial` progress and received ToOs persist in a JSON state file,
  written atomically after each change, so a restart neither repeats a
  completed one-shot nor breaks a cadence; a corrupt file is moved aside
  with an error.
- **Filters and ports are hard constraints.** A configuration naming an M3
  `port` is only for telescopes whose limits list it in `ports` (default
  none: a telescope that cannot select ports never gets one, rather than
  observe through the wrong port); one naming a `filter`, only for
  telescopes whose `filters`, if listed, include it. The sequencer selects
  them (section 3.4).
- **The nightly plan record** (`robofast_core/plan_record.py`, plan entry
  `"record": {"dir": ..., "night_rollover_hour": 12, "timezone": ...}`):
  every answer and report as one JSON line, one file per night (the local
  date of the evening; times before `night_rollover_hour` belong to the
  previous night; the site's mean solar time without a time zone). Each
  decision carries the chosen request and configuration, the score and its
  terms, the runners-up with their scores and the top rejected candidates
  with the constraints they failed; then idle answers, visits continued or
  abandoned, ToOs received or cancelled, expiries, preemptions and
  outcomes. The first line a process writes to a night's file is a snapshot
  of the scheduler (requests, state, limits, settings), so `python -m
  robofast_core.plan_record FILE --replay` rebuilds the scheduler, replays
  the night's calls at their recorded times and checks that it makes the
  same choices (a test does exactly that).
- The **request API** (`robofast_core/request_api.py`) is the only
  internet-facing surface; the supervisor's API and Alpaca stay on the
  control network. A small REST API over JSON, in its own process, on
  loopback behind a TLS reverse proxy (`deploy/` has nginx and Caddy
  examples; it refuses a non-loopback address without TLS unless told
  `--insecure-bind`). The OCS observation portal's endpoints under `/api`
  (`/api/v1`, the previous prefix, kept for one release): submit a request
  group (a RAPID_RESPONSE or TIME_CRITICAL one is a ToO), validate (the same checks, plus a dry run of the scheduler's hard
  constraints over the coming night on a coarse grid: which telescopes
  could start each request, when, and the commonest reasons why not), list
  and get groups with each configuration's status, cancel, the enclosures'
  status (weather verdict, shutter, lease and faults from the supervisor;
  each telescope's state and current observation from a status snapshot the
  sequencer writes), and data pointers for completed observations (paths
  only, from a configured template; no files served); and what an OCS
  client reads: `/api/instruments/`, `/api/profile/` (the configured
  proposals), `/api/requests/{id}` and its observation blocks,
  `/api/telescope_states/`, and the cadence expansion.
  **Authentication** (`robofast_core/api_auth.py`): a token per client,
  stored only as a salted HMAC-SHA256 in a tokens file outside any
  repository, compared in constant time; scopes `read`, `submit`, `too`,
  `operator` (the observer console's actions, section 3.8), `admin`; a client sees and cancels only its own groups; per-token and
  per-address (failed attempts) rate limits; every POST is written to an
  append-only audit log. No default tokens: `python -m
  robofast_core.request_api tokens add --name ... --scope ...` prints a
  new one once. The HTTP layer is plain asyncio (no new dependency):
  bounded request line, headers and body, timeouts, a connection limit, no
  chunked bodies, JSON errors without stack traces.
  **Intake.** The API never writes the scheduler's state. A submission or
  cancellation is a file renamed into the plan's drop box, taken in by the
  scheduler (in the sequencer process) in order every `too_poll` seconds
  through `submit()`, `submit_too()` and `cancel()`; status is read back
  from the scheduler's state file, which also keeps the groups submitted at
  run time and a record of each completed observation. So the scheduler
  stays the one source of truth, a submission made while the sequencer is
  down waits in the drop box, and the internet-facing process holds no
  channel into the control processes beyond that directory. A group is
  `QUEUED`, `ACCEPTED`, `REJECTED` (with the scheduler's reason) or
  `CANCELED` (withdrawn while queued). Canceling an accepted group cancels
  every configuration not yet finished; an observation in progress
  finishes, then its visit ends. A TOM uses TOM Toolkit's own
  `OCSFacility` with settings only (`docs/tom_facility.md`); not supported
  yet: time accounting per proposal, non-sidereal targets, seeing and
  transparency constraints, dither/mosaic expansion, an archive.
- Configured as the enclosure's plan entry, `{"type": "dispatch", "site":
  ..., "requests": ..., "telescopes": {...limits...}, ...}`
  (`sites/minerva/dispatch.example.json`).
- Still to come: a look-ahead scheduler if dispatch proves myopic.

#### 3.7.1 Targets of opportunity

A target of opportunity (ToO) can arrive at any time and may have to
interrupt an observation in progress, on one telescope, without disturbing
the others or the enclosure's lease. A Rubin alert followed up within
minutes is the motivating case.

**The request.** A ToO is a request group whose OCS `observation_type` is
`RAPID_RESPONSE` or `TIME_CRITICAL`, with `extra_params.too`: a `deadline`
(latest start; default the end of the last window), a `priority_class` (an
integer ≥ 1; normal requests are class 0; default 2 for RAPID_RESPONSE, 1
for TIME_CRITICAL) and an interrupt `policy` (default `now-after-readout`
for RAPID_RESPONSE, `next` for TIME_CRITICAL). The request's
`telescopes` says which telescopes may take it. It arrives through
`DispatchScheduler.submit_too()` (in process) or as a JSON file renamed
into the plan's `too_dropbox` directory (moved to `accepted/` or to
`rejected/` with an `.error` file saying why). The request API (section
3.7, `POST /api/requestgroups`, or `/api/toos`) writes into the same drop box. ToOs are kept in
the scheduler's state file, so a restart does not lose one.

**Eligibility.** A ToO is taken only when it passes the same hard
constraints as any request, on a telescope allowed to take it. Until then
it stays pending; past its deadline, unstarted, it is EXPIRED. The
scheduler re-checks waiting ToOs when one arrives and every `too_poll`
seconds (a target that rises, a Moon that sets, a telescope that frees up).

**Choosing what to interrupt.** For an observable ToO the scheduler wakes
an idle eligible telescope if there is one (nothing is interrupted).
Otherwise, for `now-after-readout` and `now`, it preempts the eligible
telescope whose current visit ranks lowest, if that is below the ToO's
class, and reserves the ToO for it for `reserve_timeout` (so another
telescope finishing first does not take it from under the one that was just
interrupted). An equal-class ToO never interrupts another: it is next.

**The policies,** carried out by `Sequencer.preempt(telescope, policy)`:

| Policy | Where the current observation stops | Measured on fakes |
|---|---|---|
| `next` | not at all: the ToO is the telescope's next observation | when the observation ends |
| `now-after-readout` | at the next exposure boundary: the exposure in progress finishes, is read out and counted; if none is in progress (slewing), at once | the rest of the exposure (0.66 s with 0.7 s left) |
| `now` | at once: cancel, abort the exposure and any slew | 1 ms to stopped; ToO's slew starts 60 ms after submission |

The interrupted observation is reported `interrupted`, so it is re-queued.
The telescope is not parked: the ToO is a slew away.

**Mechanism.** Each observation runs as its own child task of its
telescope's task, so one telescope's observation can be cancelled while the
others carry on under the same lease and grants. The stop is bounded and
follows the cleanup rules: cancel the task, wait at most
`cancel_ack_timeout`, then abort the exposure if the camera is not idle and
any slew, with that telescope's generation grants. A task that ignores
cancellation gets that telescope's grants revoked (fencing it alone) and the
telescope gives up for the night (alarm); a failed abort is that
telescope's error (abort, park, back off).

**Safety wins.** `preempt()` is refused unless a generation is running with
its lease unrevoked and the telescope is working in it (not in a weather
hold, a backoff, or given up). If the lease is revoked while a preemption is
in progress, the generation's fence makes the preemption's own commands
fail and its cancel reaches the telescope task; the generation's cleanup
(abort and park everything, with fresh grants) runs exactly as it would
have. A ToO that arrives during a weather hold waits; after the reopen the
telescopes ask the plan afresh and take it first, unless its deadline has
passed.

**Alert brokers, later.** No broker client yet, only the hook. A small
separate process will subscribe to a broker that filters the Rubin alert
stream (ANTARES, Fink, Lasair, ALeRCE, ...) or take triggers from a TOM
(TOM Toolkit's facility interface, via the REST API), apply the site's own
filter (brightness, declination, a human in the loop or not), and turn each
accepted alert into a ToO request group: coordinates, a deadline from the
science case, a class and a policy. It submits it through the REST API or
the drop box, and reads its status back for the broker or TOM. Broker
credentials and alert filtering stay out of the control processes; a
broker outage costs only ToOs, never the night.

### 3.8 State, telemetry and alarms

- Published state carries the time it was measured and a maximum age;
  consumers treat expired state as unknown (pyobs).
- A value registry collects device state for status pages and FITS headers
  (RTS2; `robofast_core/registry.py`). Values are named `<scope>.<name>`,
  where the scope is a telescope (`t1.ccd_temp`) or shared (`site`,
  `software`, `weather`, `enclosure`, `sky`). Each value carries its
  measurement time, maximum age, unit and FITS keyword. Publishers: the
  frame pipeline reads the telescope, focuser, rotator and filter at each
  exposure start and the camera at each exposure end, through the
  generation's own grants, so the fence covers those reads. While a
  telescope guides, its guider's state, residual, flux, FWHM, rate,
  latency, fiber pixel and tip/tilt position are copied in as
  `<telescope>.guide_*` (GUIDSTAT, GUIDRMS, ...). A background
  task reads the weather bridge's ObservingConditions (read-only), the
  shutter and weather verdict from the supervisor's status, and the Sun
  and Moon (astropy). The site and the software version (`git describe`)
  are static. The autofocus publishes its last result
  (`<tel>.autofocus.*`: `AFFOCUS`, `AFMETHOD`, `AFOK`, ...) and, with
  `images.measure_fwhm`, the pipeline each light frame's star FWHM
  (`<tel>.image.fwhm`, for the autofocus trigger). The header builder snapshots the registry at exposure start
  and end. A value past its maximum age, or one whose last read failed, is
  written undefined and marked `STALE` with its old value, never as
  current. The sequencer's status snapshot includes the registry.
- Frames are saved by the sequencer itself (section 3.4, `docs/data.md`):
  atomic, never overwritten, checksummed, UTC from the camera, and a
  lost frame stops its telescope with an alarm.
- Alarms (email or chat) for: quarantine, give-up, shutter not reaching the
  commanded state, safety supervisor or heartbeat failures. The observer
  console shows and acknowledges them; the notifier
  (`robofast_core/notifier/`, `docs/notifier.md`), its own process, polls
  the same sources through the console's aggregator and sends them by
  email, chat webhook or a command (to page an observer): per-sink minimum
  level, deduplication, rate limits, escalation until acknowledged on the
  console, a daily digest. The safety processes do not know it exists.
- **The observer console** (`robofast_core/console/`, `docs/console.md`):
  one web page, its own small process, for the observer on site (the FLWO
  60-inch runs as attended automation) and the people watching MINERVA.
  Live by server-sent events: weather (verdict, reasons, key values,
  would-close rain), enclosure (shutter, faults, lease, heartbeat, holds),
  each telescope (state, observation, alt/az, focus, guiding, last frame),
  the spectrograph (job, progress, exposure meter), the plan record and
  queue, alarms and the supervisor's timeline, all read from the services'
  status APIs and files. With an `operator` token an observer acknowledges
  alarms, pauses, resumes or stops a telescope or the enclosure, parks one
  telescope now (never the roof), clears a
  dead lease or the faults, and submits or cancels requests (through the
  request API's code); every action is authenticated, CSRF-protected,
  rate-limited and audited with who and why. Pause, resume and stop reach
  the sequencer only through a command directory that a plan wrapper in the
  sequencer process reads (`operator_control`; holds answer `Idle`, the
  observation in progress is preempted). It can ask the supervisor to
  close; it can never open or get around it.
- Status and control go through the APIs, never through files, with one
  deliberate exception: the request API reaches the dispatch scheduler only
  through its drop box (in) and state file (out), and the sequencer's
  status snapshot (`status_file`), so the internet-facing process has no
  network path into the control processes (section 3.7). The observer
  console's pause, resume and stop use the same pattern: a command
  directory in (`operator_control`), a holds file out.

### 3.9 Processes and communication

- Start with components in one asyncio process for tests; production splits
  the safety supervisor, each sequencer and the device servers into
  processes. The interfaces between them are plain HTTP/JSON (Alpaca for
  devices), so splitting is configuration, not a rewrite.
- systemd restarts them (`deploy/` has example units): the supervisor
  always, the sequencer on failure, except for "needs an operator"
  (quarantine, give-up). A crash-looping sequencer stops being restarted and
  the supervisor's orphan alarm takes over.
- Long-running processes keep bounded in-memory state: the timeline is a
  ring buffer (`timeline_size`), and so is the alarm list.
- No message bus (pyobs's XMPP and Rockit's Pyro were both costs in the
  evaluations).
- Alpaca has no authentication: device traffic stays on an isolated network.
  Only the request API and the observer console face outward (both on
  loopback behind a TLS proxy, with tokens).
- Deployment on a control host (users and groups, units, layout, secrets,
  firewall, time sync, backups, upgrades and rollback): `docs/deploy.md`.

### 3.10 Configuration

Per-site YAML: which Alpaca endpoint each device is, enclosure membership,
timeouts, limits, and policies (the first is `sites/minerva/weather.yaml`).
`sites/minerva/README.md` maps every MINERVA value to its legacy
minerva-control `config/*.ini` source and lists the conflicts between them.
A new site is a new config directory plus any device servers it needs.

## 4. Windows control PCs

Most vendor camera and mount software is Windows-first, and its Linux
support is often worse. So:

1. **Default:** a minimal Windows appliance per telescope (IoT Enterprise
   LTSC, so no feature updates and patches on our schedule), running only
   vendor drivers, PWI4 and ASCOM Remote; firewalled to the control network,
   with no internet access; rebuilt from a script plus an image.
2. **Opportunistic:** native, cross-platform drivers behind Alpaca where a
   device's Windows stack is the actual problem. Simple serial or USB
   command protocols (focuser/rotator controllers, PDUs, enclosures, weather)
   are good candidates. Each is tested against captured traffic and must pass
   ConformU before it touches hardware.
3. **Virtual machines** only for non-hardware services (scheduler, archive,
   database), not for PCs with USB cameras.

## 5. Testing and CI

- **Fast tier:** pure-Python fakes and an in-process weather server; unit
  tests plus the scenario suite. Python 3.12-3.14. Image-based features
  (autofocus, the guider) run on a synthetic star field
  (`robofast_core/testing/starfield.py`: the pupil image, with its central
  obstruction, convolved with the seeing; jitter, noise, subframes) with
  known truth, or on the real sky (`robofast_core/testing/skysim.py`:
  catalog stars through a mount with TPOINT-style pointing errors, sky
  brightness from the Sun and Moon, the port's zero point; fixture Gaia
  tiles, so no catalog is downloaded in CI).
- **Simulator tier:** the same scenarios against the official ASCOM
  OmniSimulators (pinned, checksum-verified), plus a real FITS frame
  written from OmniSim's camera and read back, and the full-frame download
  time in both transfer modes (published with the other timings), and the
  autofocus moving OmniSim's focuser.
- **Conformance:** ConformU against every Alpaca server robofast ships.
- **Scenario suite** (grows with each bug found in the field): weather unsafe
  mid-exposure; feed erroring, hanging, or frozen at "safe"; recovery and
  reopen; device hang or death; second-client interference; sequencer crash
  with the enclosure open, and its restart and recovery; supervisor crash
  (heartbeat closes); two telescopes in one enclosure (both abort, park and
  close; one's camera hangs or keeps failing while the other observes on; a
  refused slew; stale grants refused on each); targets of opportunity (each
  interrupt policy stops the right telescope at the right point while the
  other observes on; a ToO during a weather hold; one that never becomes
  observable and expires; preemption racing a lease revocation; repeated
  ToOs; an observation that ignores the preemption). OmniSim serves one telescope
  and camera, so the simulator tier runs a second OmniSim for the second
  pair.
- `master` requires the `test` gate check.

## 6. Defaults (proposed; to be confirmed)

| Setting | Default | Meaning |
|---|---|---|
| `poll_interval` | 5 s | supervisor weather and enforcement period |
| `read_timeout` | 5 s | per weather read |
| `stale_after` | 60 s | weather data or last good read older than this is unsafe |
| `reopen_hold` | 20 min | continuous safe time before (re)opening |
| `lease_ttl` | 60 s | an unrenewed lease is revoked and the enclosure closed |
| `lease_renew_interval` | 10 s | sequencer renewal period |
| `orphan_timeout` | 10 min | alarm if a revoked lease is still unreleased |
| `shutter_timeout` | 120 s | shutter move before re-command and alarm |
| `cancel_ack_timeout` | 10 s | wait for a cancelled task before quarantine |
| `error_backoff` | 300 s | a telescope's wait after a device error |
| `max_consecutive_errors` | 3 | then that telescope gives up for the night and alarms |
| `plan_timeout` | 30 s | bound on asking the plan for a telescope's next observation |
| `idle_release_after` | 15 min | every telescope idle this long: park, release the lease, close (section 3.4) |
| `filter_timeout`, `port_timeout` | 60 s, 60 s | bounds on a filter wheel move and an M3 port selection |
| evening start | Sun below -6° | `robofast_core.when` in the sequencer unit (`deploy/`) |
| `night_rollover_hour` | 12 | plan record: earlier local times belong to the previous night |
| `record_top_n` | 5 | runners-up and rejected candidates recorded per decision |
| `park_before_close` | off | roll-off roofs: park every telescope (bounded, in parallel) before closing |
| `park_wait` | 60 s | bound on that park |
| `heartbeat_interval` | none | supervisor ping period; at most `heartbeat_timeout`/4, or the config is rejected |
| `heartbeat_timeout` | none | the enclosure's own heartbeat timeout (Aqawan PAC: 60 s, confirmed); required with `heartbeat_interval` |
| `fault_action` | none | the dome's read-only fault channel (Aqawan: `Faults`); any fault alarms and blocks opening, and a fault with the enclosure not closed closes it |
| `shutter_unreadable_grace` | 15 s | while open is wanted, a shutter unreadable this long revokes the lease and closes (shutterError does so at once) |
| `shutter_error_backoff` | 30 min | no reopen after a shutter error, so a broken shutter is not cycled |
| `min_alt`, `max_alt` | 21°, 83° | per-telescope altitude limits (legacy MINALT, MAXALT) |
| `max_sun_alt` | -12° | observe light frames only with the Sun below this (legacy HORIZON) |
| `min_moon_sep` | 10° | Moon separation limit (legacy MINMOONSEP) |
| `w_setting`, `w_airmass`, `w_cadence` | 1, 1, 1 | dispatch score weights (section 3.7) |
| `setting_horizon` | 2 h | a target lost within this gets a setting bonus |
| `idle_poll` | 60 s | with nothing observable, ask again this often |
| `too_poll` | 10 s | re-check waiting ToOs and the drop box this often |
| `reserve_timeout` | 60 s | a ToO is held for the telescope it preempted this long |
| `images.queue_size` | 4 | frames per telescope waiting to be written before the next exposure waits (`docs/data.md`) |
| `images.imagebytes` | on | camera images by the Alpaca ImageBytes transfer (JSON fallback) |
| `images.download_timeout_base`, `download_min_rate` | 30 s, 2 MB/s | image download timeout: base + generous size / rate |
| `images.pointing_max_age`, `camera_max_age` | 60 s, 60 s | telescope and camera values go stale in headers after this |
| `images.weather_max_age` | 300 s | a weather value older than this is written as stale |
| `images.flush_timeout` | 120 s | bound on waiting for a telescope's frames to be written |
| `images.compress` | off | Rice-compress integer frames (`.fits.fz`) |
| `autofocus.method` | `vcurve` | two exposures on one side of focus; `donut` (one), `sweep` (nine) (`docs/autofocus.md`) |
| `autofocus.blur_ratio`, `far_ratio` | 6, 1.5 | the near point's blur over the worst seeing FWHM (`max_seeing` 4"); far over near offset |
| `autofocus.slope_tolerance` | 0.2 | a two-point slope this far off the calibration falls back to a sweep |
| `autofocus_triggers` | start of night; 1 C; 1.5x FWHM | when to focus; a failed autofocus waits `retry_after` (30 min) |

## 7. Open decisions

1. **Roll-off roof versus an unparked telescope.** If the telescope can't
   be parked within `park_wait`, does the roof close anyway? The prototype
   closes anyway: rain on the optics is worse than an unlikely roof contact.
   Needs confirmation per enclosure (mechanical clearance).
2. The defaults in section 6.
3. Where the legacy `robofast/` package's useful parts (YAML configs, HAL
   knowledge for Aqawan, APC PDU, IRF90, Paramount, DFM) move, and when
   `robofast_core` becomes `robofast`.
4. Temperatures the CDK700 focus model needs when PWI4 doesn't report them
   (the autofocus model uses the focuser's temperature until the primary's
   and the ambient are in the registry; `docs/autofocus.md` section 11).

## 8. Roadmap

Each step is a CI-tested deliverable.

1. **Done:** prototype of the owner, supervisor and sequencer, passing the
   scenario suite on fakes and OmniSim.
2. Own asyncio Alpaca client; drop alpyca.
3. Supervisor as its own process, with a heartbeat device and the
   "supervisor dies" scenario.
4. Site weather bridge (built; `docs/weather_bridge.md`; to be compared
   with the legacy system in its shadow mode). Alpaca SafetyMonitor +
   ObservingConditions for
   MINERVA's real feeds.
5. Aqawan enclosure Alpaca server with a simulator mode, passing ConformU
   (`robofast_core/devices/`: PAC client, PAC simulator, Dome server;
   ConformU runs in CI). Hardware facts not yet confirmed on a real PAC are
   listed in `aqawan_pac`'s and `aqawan_sim`'s module docs; the confirmed
   ones and the fault policy are in `docs/aqawan.md`.
6. Windows bridge on one control PC: ASCOM Remote for the camera and PWI4;
   measure command latency and full-frame download time.
7. Shadow mode: robofast's supervisor runs read-only alongside the legacy
   system and logs what it *would* have done, for comparison.
8. One telescope, attended nights, then robotic; then the rest of MINERVA.
9. Dispatch scheduler, request model, target-of-opportunity interrupts
   and the authenticated request API (built; sections 3.7, 3.7.1), with
   the night's operations around them (built: idle release, closed-roof
   calibrations, evening start and morning end, filter and M3-port
   selection, partial re-takes, the nightly plan record with replay);
   next a TOM Toolkit facility module (`docs/tom_facility.md`) and an
   alert-broker client; then MEarth (collision checker).
10. **Done:** several telescopes per enclosure (section 3.5) and the
    production sequencer entry point with its plan-provider interface.
11. KiwiSpec spectrograph service, its clients for the legacy spectrograph
    PC and a protocol-faithful simulator (built; `docs/kiwispec.md`, with
    open questions); next, spectra in the sequencer (`docs/kiwispec.md`
    section 4), then the service against the real PC, attended.
12. FLWO 60" / TRES: an Alpaca Telescope + Focuser (+ Dome, if the TCS
    drives it) server for the 60-inch's DFM TCSGalil ("TCSGO"), with a
    protocol-faithful simulator, passing ConformU in CI, and the
    weather-unsafe scenario on the `sites/flwo60/` config (built:
    `robofast_core/devices/tcsgo_*`; protocol, hardware-moving commands
    and open questions in `docs/tcsgo.md`). Next: answers to those
    questions, a TRES camera/spectrograph service, then a supervised
    test on the real TCS.
13. **Done:** the sequencer saves its frames (section 3.4, `docs/data.md`):
    ImageBytes download, the value registry (section 3.8), the FITS header
    builder and an atomic, never-overwriting writer with a nightly index;
    real file paths in the request API's data pointers. Next: measure
    full-frame download over ASCOM Remote on a control PC (step 6), the
    plate scales and position angles of each telescope, and a guider/FAU
    image path.
14. Guider (built, simulators only; `docs/guider.md`): a generic guide
    service next to the camera, with SX AO, PI E-518 and mount-offset
    backends and their simulators, measured calibrations, offloading, fiber
    acquisition, and the sequencer hook. Next: the ZWO backend on a real
    FAU camera, then T2's AO unit commissioned (attended, with JDE's
    approval), then the TRES front end (an Andor Zyla backend).
15. Autofocus (built; `docs/autofocus.md`): star measurement, the V-curve,
    donut and sweep methods with a calibration per telescope, port and
    filter, the temperature model, `AUTO_FOCUS` requests and the night's
    triggers, tested on a simulated star field and OmniSim's focuser. Next:
    the CDK700 optics and focuser units from PWI4, a calibration on the sky
    per port and filter, and the 60-inch's focus once TCSGO's units are
    confirmed.

16. Observer console (built; `docs/console.md`): live status for every
    part of an enclosure, alarms with acknowledgement, and the operator
    actions (pause, resume, stop, close, clear a dead lease or faults,
    requests and ToOs), authenticated and audited, tested to never open
    the enclosure. Alarm delivery (email, chat, a command to page the
    60-inch observer) is built (`docs/notifier.md`). Next: an attended
    night with both.

17. The sky (built; `docs/sky.md`): the Gaia DR3 tile cache and bundled
    bright stars, a sky simulator with pointing errors, the near-blind
    plate solver, solved WCS and zero points in every frame. Next: target
    vetting and catalog-based acquisition, the per-port zero points
    measured on real frames, a pointing-model builder.

## 9. Borrowed ideas

| From | What |
|---|---|
| pyobs | typed device interfaces; state stamped with time and maximum age; in-process vs networked transport behind one interface; a simulator per device; planning separated from execution; constraint-based scheduling |
| Rockit | enclosure hardware heartbeat; "unsafe if any unsafe or all unknown" with per-sensor staleness; safe-for-N-minutes reopen hold; JSON nightly plans; PWI4 client practice |
| POCS | state machine defined as data; states tagged safe in any weather; fail-closed stale weather; simulator-backed tests |
| RTS2 | declared blocking between operations; value registry; dispatch selector with time-dependent bonus |
| LCO OCS / TOM Toolkit | request and status model; facility interface |
| ASCOM | Alpaca protocol, OmniSim, ConformU, ASCOM Remote |
