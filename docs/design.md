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
  roof and mounts) are small Python Alpaca servers. Each has hard timeouts,
  soft limits and a simulator mode, and must pass ConformU in CI.
- **Non-Alpaca instruments** (the spectrograph, iodine cell, exposure meter)
  follow the same pattern: one owning service, a small typed HTTP API, a
  simulator. Not a parallel framework.
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
per telescope (section 3.5).

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

**Evening start, morning end.** The sequencer process runs one night.
`deploy/robofast-sequencer@.timer` starts its unit every afternoon; the
unit takes evening calibrations (closed) while the Sun is up, waits with
`python -m robofast_core.when --sun-below -6 --config <enclosure.json>`
(astropy, the site from the config; at once if it is already dark, e.g.
after a restart), then runs the night. The dispatch scheduler ends the
night once the Sun is above its `max_sun_alt` and rising; on that clean
exit `deploy/robofast-calibrations@.service` takes the morning
calibrations.

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

The telescope layer hides the mount type. Pointing models live with the
mount software where it has them (PWI4 for the CDK700s, per M3 port), and in
robofast's telescope layer for mounts that don't. The sequencer never sees a
model.

### 3.7 Planning and the request API

- **Requests** are modelled the LCO OCS way
  (`robofast_core/request_model.py`): RequestGroup (who, priority, ToO or
  not) → Request (windows, allowed telescopes, cadence) → Configuration (one
  target, one instrument type, optional tighter constraints, a status) →
  InstrumentConfig (exposure time, count, filter). Status per
  configuration: PENDING, ATTEMPTED, COMPLETED, FAILED (after
  `max_attempts` failed attempts; interruptions do not count), EXPIRED,
  CANCELED. A request with a cadence (`min_interval`, `max_per_night`) is
  *standing*: it returns to PENDING after each visit, as MINERVA's RV
  targets do. Requests load from JSON files, validated with errors that
  name the entry; `from_legacy_csv` converts a legacy MINERVA target
  spreadsheet export. An InstrumentConfig may name a `filter` and an M3
  `port`; a Configuration may set `allow_partial`. A small authenticated REST API (accept, validate,
  report, cancel) comes next; a TOM Toolkit facility module can sit on it
  later.
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
- Configured as the enclosure's plan entry, `{"type": "dispatch", "site":
  ..., "requests": ..., "telescopes": {...limits...}, ...}`
  (`sites/minerva/dispatch.example.json`).
- Still to come: a look-ahead scheduler if dispatch proves myopic.

#### 3.7.1 Targets of opportunity

A target of opportunity (ToO) can arrive at any time and may have to
interrupt an observation in progress, on one telescope, without disturbing
the others or the enclosure's lease. A Rubin alert followed up within
minutes is the motivating case.

**The request.** A ToO is a request group with a `too` entry: a
`deadline` (latest start), a `priority_class` (an integer ≥ 1; normal
requests are class 0) and an interrupt `policy`. The request's
`telescopes` says which telescopes may take it. It arrives through
`DispatchScheduler.submit_too()` (in process) or as a JSON file renamed
into the plan's `too_dropbox` directory (moved to `accepted/` or to
`rejected/` with an `.error` file saying why). The REST API (section 3.7)
will call `submit_too()` too. ToOs are kept in the scheduler's state file,
so a restart does not lose one.

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
  (RTS2).
- Alarms (email or chat) for: quarantine, give-up, shutter not reaching the
  commanded state, safety supervisor or heartbeat failures.
- Status and control go through the API, never through files.

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
  Only the request API faces outward.

### 3.10 Configuration

Per-site YAML: which Alpaca endpoint each device is, enclosure membership,
timeouts, limits, and policies (the first is `sites/minerva/weather.yaml`).
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
  tests plus the scenario suite. Python 3.12-3.14.
- **Simulator tier:** the same scenarios against the official ASCOM
  OmniSimulators (pinned, checksum-verified).
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

## 7. Open decisions

1. **Roll-off roof versus an unparked telescope.** If the telescope can't
   be parked within `park_wait`, does the roof close anyway? The prototype
   closes anyway: rain on the optics is worse than an unlikely roof contact.
   Needs confirmation per enclosure (mechanical clearance).
2. The defaults in section 6.
3. Where the legacy `robofast/` package's useful parts (YAML configs, HAL
   knowledge for Aqawan, APC PDU, IRF90, Paramount, DFM) move, and when
   `robofast_core` becomes `robofast`.
4. Temperatures the CDK700 focus model needs when PWI4 doesn't report them.

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
9. Dispatch scheduler, request model and target-of-opportunity interrupts
   (built; sections 3.7, 3.7.1), with the night's operations around them
   (built: idle release, closed-roof calibrations, evening start and
   morning end, filter and M3-port selection, partial re-takes, the nightly
   plan record with replay); next the request API; then MEarth (collision
   checker).
10. **Done:** several telescopes per enclosure (section 3.5) and the
    production sequencer entry point with its plan-provider interface.

## 9. Borrowed ideas

| From | What |
|---|---|
| pyobs | typed device interfaces; state stamped with time and maximum age; in-process vs networked transport behind one interface; a simulator per device; planning separated from execution; constraint-based scheduling |
| Rockit | enclosure hardware heartbeat; "unsafe if any unsafe or all unknown" with per-sensor staleness; safe-for-N-minutes reopen hold; JSON nightly plans; PWI4 client practice |
| POCS | state machine defined as data; states tagged safe in any weather; fail-closed stale weather; simulator-backed tests |
| RTS2 | declared blocking between operations; value registry; dispatch selector with time-dependent bonus |
| LCO OCS / TOM Toolkit | request and status model; facility interface |
| ASCOM | Alpaca protocol, OmniSim, ConformU, ASCOM Remote |
