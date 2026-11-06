# robofast design

Status: draft, 2026-10-30. Decision: build robofast's own core on ASCOM Alpaca,
starting from the `robofast` prototype, and borrow ideas from existing
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
  roof and mounts, the FLWO 60-inch's DFM TCS and TRES front end) are small Python Alpaca servers. Each has hard timeouts,
  soft limits and a simulator mode, and must pass ConformU in CI.
- **Non-Alpaca instruments** (the spectrograph, iodine cell, exposure meter)
  follow the same pattern: one owning service, a small typed HTTP API, a
  simulator. Not a parallel framework.
  KiwiSpec is the first (`robofast/devices/kiwispec_service.py`,
  `docs/kiwispec.md`): it alone talks to the spectrograph PC's legacy server
  and SI camera server, runs one job at a time with every step bounded,
  terminates on the flux record's counts (the FAU guide frames' flux, not
  the unused exposure meter) and reports the flux-weighted midpoint, and gives the sequencer's cleanup a bounded abort that never
  raises. Like the Aqawan server, it refuses anything but a verified
  simulator without `allow_hardware` in the config and `--hardware`
  (below, "The hardware gate").
- **The guider** (`robofast/guider/`, `docs/guider.md`) is one such
  service per guide camera, run on the computer the camera is plugged into
  so frames never cross the network in the loop: camera, centroider,
  PI(D) controller, a measured device-to-pixel calibration, and a
  tip/tilt (Starlight Xpress AO, PI E-518) or mount offsets, with
  offloading to the mount and fiber acquisition. Every real backend is
  behind the same `allow_hardware` + `--hardware` gate.
- **PlaneWave CDK700s under PWI** (MINERVA) have robofast's own Alpaca
  Telescope + Focuser + Rotator server (`robofast/devices/pwi_*`,
  `docs/pwi.md`) over PWI's HTTP API, PWI4 or the legacy PWI2, with
  simulators of both and ConformU in CI: altitude limits and a limit watch
  enforced in the server, motors enabled only on explicit command,
  homing only on explicit, opted-in command (one axis at a time on PWI4),
  moves refused while unhomed without an engineering override, the M3 port as the `SelectPort` Action, and `robofast-pwi-jog`
  for bounded moves in a first hardware session. Behind the hardware gate.
- Vendor extras that Alpaca lacks (PWI4 M3 port selection, fans, pointing
  models) use Alpaca `Action()` or a typed side channel per device client.
- <a id="the-hardware-gate"></a>**The hardware gate.** A server that speaks
  a vendor protocol (PAC, TCSGO, the spectrograph PC, SNMP, PWI, a guide
  camera's Alpaca server, a legacy Redis) talks to real hardware only with
  `"allow_hardware": true` in its config **and** `--hardware` on its
  command line; shipped configs say false. Without both it talks only to a
  simulator it has **verified** itself (`robofast/hwgate.py`).
  Loopback is not evidence: `ssh -L 7100:pac:22004` puts the real PAC on
  127.0.0.1, as would socat, a VPN helper or a container port mapping. A
  simulator proves itself by holding a Linux abstract Unix socket
  `robofast-sim:<proto>:<port>` in the process that owns the listening
  socket (`hwgate.SimulatorRegistration`; OmniSim is recognised by its
  loaded assembly). The server checks from `/proc` -- which process holds
  the listening socket, and whether it holds the registration -- before the
  first byte goes out, so an unverified endpoint is sent nothing, not even
  a status query; the registration dies with the process, so it cannot go
  stale; and a client that connects per command re-verifies before every
  connection (`hwgate.SimulatorGuard`), so a tunnel that takes over a dead
  simulator's port is refused. Alternatives rejected: an in-band identity
  probe (it sends bytes to whatever answers, and binary or fixed-format
  protocols such as TCSGO's and the SI camera's have nowhere to put a
  marker) and a `"simulator": true` config flag (a claim, not a check: the
  loophole is exactly a config that says one thing while a tunnel says
  another). Limits, all failing closed: Linux only, same host, and only
  processes this user can inspect; anything else needs the gate. A serial
  or parallel port has no such ambiguity: its servers run their simulator
  in-process (`port.type: "sim"`) and gate every real device path.
- **robofast ships its own small asyncio Alpaca client.** alpyca serializes
  every request in a process behind one lock with a fixed 5 s timeout, so a
  hung device delays the safety supervisor's close. Our client has per-call
  timeouts and no shared lock.

### 3.2 Device owners and grants

A `DeviceOwner` is the only path to one device (`robofast/owner.py`).

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
(`robofast/safety.py`). Policy:

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
- **No heartbeat, no unattended running.** An enclosure without a hardware
  heartbeat device (the FLWO 60-inch dome has no watchdog, JDE 2026-10-31)
  is `attended_only` (automatically): no lease is granted, so it never
  opens, unless an operator is on duty. The operator checks in through the
  observer console (`duty`, audited), and the open console page renews the
  duty; one not renewed for `duty_ttl` raises a critical alarm, and
  `duty_grace` later the supervisor revokes the lease and closes. Going
  off duty closes an open enclosure. MINERVA (the Aqawan heartbeat) is
  unaffected (`docs/console.md`, "Operator duty").
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
- **No lease while closing.** Once the supervisor commits to a close (after
  park-before-close, right before `CloseShutter`), or sees the shutter
  closing, no lease is granted until the shutter reads closed: a lease
  granted then would let its sequencer unpark a telescope under a closing
  roll-off roof.
- **Enclosure faults** (a separate axis from the shutter position) alarm
  and block opening; a fault with the enclosure open closes it. Where
  configured (`fault_auto_clear`, MINERVA's Aqawan) the supervisor clears a
  fault itself, only with both shutters confirmed closed, never an E-stop,
  at most once an hour and alarming each time; a recurrence within the hour
  stops automatic clearing until an operator clears (`docs/aqawan.md`).

### 3.4 Sequencer, one per enclosure

`robofast/sequencer.py`. Each "generation" of observing:

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
(`robofast/plan.py`):

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
tracking, so it does not track toward a limit, and stays where it is, ready to
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
A telescope may also have a **calibration stage** (`calstage`: the FLWO
60-inch's TRES front end, served as an Alpaca FilterWheel whose `Names` are
its positions, `docs/tres_frontend.md`); an observation's `calstage` is
selected the same way, bounded by `calstage_timeout`, before the exposures
and before any lamp is switched on. A telescope with calibration **lamps**
(`lamps`: the 60-inch's TRES lamps on a Pulizzi switch,
`docs/pulizzi.md`) takes `ARC` and `LAMP_FLAT` observations without a
lease: calibration stage, lamp on, warm-up, frames, lamp off. The lamp is
switched off on every way out (error, preemption, cancellation, with a
grant of its own that a fence does not revoke), every cleanup switches all
lamps off, and the switch server itself turns off any lamp left on longer
than its `max_on`. MINERVA has no lamps: its requests for them are refused.
A telescope with an atmospheric dispersion corrector (`adc`: the 60-inch's,
served as an Alpaca Switch for tracking plus a Rotator per prism,
`docs/adc.md`) has its ADC tracking switched on once on target for light
frames, and the sequencer waits (bounded by `adc_settle_timeout`) for it
to report `Settled`; an ADC that does not settle is a warning, not a
failure. Every cleanup switches the tracking off (prisms neutral).

**Autofocus.** An observation of `kind="autofocus"` (or a request
configuration of type `AUTO_FOCUS`) focuses its telescope on its field,
through its filter and port: the lease, the slew, the grants (the focuser's
is fenced with the others) and the cleanup are the usual ones
(`robofast/autofocus.py`, `docs/autofocus.md`). The default method takes
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
`python -m robofast.when --sun-below -6 --config <enclosure.json>`
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
`docs/guider.md`); per telescope, `guider` in the enclosure config. A
configuration with `guiding_config.extra_params.guide: "offset"` is
*offset guided* (`docs/offset_guiding.md`): the guider holds a nearby
star (given, or chosen from Gaia) at the fiber plus that star's offset
from the target, with proper motion, differential refraction and field
rotation folded in, so a target too faint for annulus guiding still sits
on the fiber; the guide mode and star go into the FITS headers
(`GUIDMODE`, `GS*`) and the decision record. With `guide: "auto"` the
dispatch scheduler chooses annulus or offset guiding from the instrument's
guiding model (`instruments.<name>.guiding`:
`robofast/guider/recommend.py`, a photon-noise prediction of each
method's error checked against the guide loop in the simulator), and
records the recommendation and its reason. How the guide camera sees a
target on the fiber is per instrument (`on_target`): the spill round a
fiber hole in a mirror (`annulus`, TRES) or the whole star through a
beamsplitter (`beamsplitter`, MINERVA's FAU, JDE 2026-11-02: "the FAU has
no fiber hole"); offset guiding is chosen only when the target is too
faint to see at that light, or to correct at least `min_rate` times a
second. **Seeing** comes from a
configured, pluggable source (`robofast/seeing.py`,
`docs/seeing.md`; the dispatch plan's `seeing` entry): by default the
guiders' live FWHM (the median over the last 30 min, stale after 15),
falling back to the guiding model's value; the same estimate decides a
configuration's `max_seeing` constraint, and the decision record keeps
it (JDE 2026-11-02: "schedulers may use seeing as a selection criterion,
too").

**Saving frames.** With an `images` entry in the enclosure config, every
frame is saved (`robofast/imaging.py`, `docs/data.md`). The image is
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

**Entry point.** `python -m robofast.sequencer_service --config
<enclosure.json>` builds the owners, the lease client and the plan from the
enclosure's config file, which the supervisor service reads too
(`robofast/config.py`; `sites/minerva/aqawan1.json` is MINERVA's
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
Aqawan 1 holds T1 and T2, which the site has judged cannot collide, so their
slews are not vetted against each other; MEarth North's telescopes can collide.

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
  parallel within `park_wait`, then closes regardless -- unless the
  enclosure sets `require_park_before_close` (a roof that can hit a
  telescope): then a *routine* close (lease released or expired, an
  operator's stop, a mode switch, a duty expiry) waits until every
  telescope reports `AtPark`, re-parking each pass, alarming, and raising a
  critical alarm after `park_interlock_escalate`; it never closes on an
  unparked telescope. An *emergency* close (weather unsafe or unknown, an
  enclosure fault, a shutter error, the console's EMERGENCY CLOSE, the
  supervisor stopping) still closes after `park_wait`, parked or not
  (`robofast/safety.py`, "Closing on unparked telescopes").
- **Collision checking hook.** Before every slew the telescope task asks a
  `SlewGuard` (default: allow). MEarth North's **collision checker** will be
  one: it vets each target and path against the other telescopes'
  positions and raises `SlewDenied`, which counts as that telescope's
  error. Parking is never vetted (cleanup must always be able to park), so
  MEarth also needs a park order or park positions the site has verified do not collide.

### 3.6 Telescopes and pointing models

The telescope layer hides the mount type. robofast keeps its own pointing
model per telescope and M3 port (`robofast/pointmodel.py`,
`docs/pointing_model.md`): built from plate solves by a pointing run (a
`POINTING_MODEL` request) and from every solved frame, terms chosen by
significance and cross-validated, applied to every slew (optionally with
the vendor's model switched off), inverted for the FITS headers, with a
rolling local correction and a nightly drift alarm. Where the mount
software's own model is kept (PWI4 on the CDK700s, for now), robofast's
can run disabled, measuring only. Each port's image PA and parity against
the rotator's mechanical angle is measured from plate solves and saved
(`robofast/rotcal.py`, `docs/rotcal.md`); the frame pipeline's WCS,
the plate solver's hint and the guider's mount matrix read it.

Where a telescope really points is measured, not assumed (`docs/sky.md`):
robofast has its own star catalog (Gaia DR3 tiles cached on disk, a
bright-star list fetched once from Zenodo, the WDS; or, where Gaia's
non-commercial licence does not fit, UCAC4 or 2MASS tiles from IRSA with an
estimated G, chosen by `catalog.source`, and `catalog.allow_noncommercial:
false` to refuse every non-commercial file), a near-blind plate solver that every
light frame, acquisition and pointing-model run can use, and TPOINT-style
pointing-error terms (`robofast/pointing.py`) that the sky simulator
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
  names, nesting and units (`robofast/request_model.py`;
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
  targets do; a cadence `pool` shares `max_per_night` among requests (one
  B star a night, whichever), and a cadence `limit` says whether reaching
  it is `hard` (excluded) or `soft` (demoted below every other request, so
  taken only rather than idle: MINERVA's B stars, as legacy did). Requests load from JSON files, validated with errors that
  name the entry; a site plug-in's request importers convert legacy lists
  (a dispatch plan's `imports`: MINERVA's target spreadsheet export is
  `minerva.legacy_csv`), `robofast.sites.minerva.target_sheet` reads
  MINERVA's target and B-star lists from their Google Sheet (read only,
  with timestamped snapshots and a fallback to the newest;
  `docs/minerva_sheet.md`), and `robofast.sites.flwo60.catalogs` the FLWO 60-inch's
  telshell catalogs (`.mct` target lists, `.off` offset patterns; `.sec`
  confidential lists described but never decrypted) into an intermediate
  form shaped like the proposed target model, request groups for the
  60-inch, and back into `.mct` for the legacy software
  (`docs/catalogs.md`; rules ported from FLWO's telshell, T. Groner). An InstrumentConfig may name a `filter` and an M3
  `port`; a Configuration may set `allow_partial`.
- The scheduler is a **dispatch scheduler** (`robofast/dispatch.py`),
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
- **The nightly plan record** (`robofast/plan_record.py`, plan entry
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
  robofast.plan_record FILE --replay` rebuilds the scheduler, replays
  the night's calls at their recorded times and checks that it makes the
  same choices (a test does exactly that).
- The **request API** (`robofast/request_api.py`) is the only
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
  **Authentication** (`robofast/api_auth.py`): a token per client,
  stored only as a salted HMAC-SHA256 in a tokens file outside any
  repository, compared in constant time; scopes `read`, `submit`, `too`,
  `operator` (the observer console's actions, section 3.8), `admin`; a client sees and cancels only its own groups; per-token and
  per-address (failed attempts) rate limits; every POST is written to an
  append-only audit log. No default tokens: `python -m
  robofast.request_api tokens add --name ... --scope ...` prints a
  new one once. The HTTP layer is plain asyncio (no new dependency):
  bounded request line, headers and body, timeouts, a connection limit, no
  chunked bodies, JSON errors without stack traces.
  **Intake.** The API never writes the scheduler's state. A submission or
  cancellation is a file renamed into the plan's drop box, taken in by the
  scheduler (in the sequencer process) in order as each file arrives (the API rings the
  scheduler's doorbell after each, `robofast.doorbell`: no polling)
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
  yet: time accounting per proposal, non-sidereal targets other than
  comets and minor planets (`docs/moving_targets.md`), a
  transparency constraint, dither/mosaic expansion, an archive
  (`max_seeing` is honored: `docs/seeing.md`).
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
  (RTS2; `robofast/registry.py`). Values are named `<scope>.<name>`,
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
  (`robofast/notifier/`, `docs/notifier.md`), its own process, polls
  the same sources through the console's aggregator and sends them by
  email, chat webhook or a command (to page an observer): per-sink minimum
  level, deduplication, rate limits, escalation until acknowledged on the
  console, a daily digest. The safety processes do not know it exists.
- **The observer console** (`robofast/console/`, `docs/console.md`):
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
- Cancellation is never lost and a stop never hangs
  (`robofast/cancel.py`). A `task.cancel()` landing inside an httpx
  request can be lost (httpx 0.28 / anyio 4: the request returns normally),
  so every httpx request runs through `isolated()`, in a task of its own,
  and the caller's cancel always arrives. Each loop stopped by a cancel also
  ends on a stop flag (or on no longer being the current task), and its
  stopper waits for it with a bound (`stop_task()`), passing its own
  cancellation on: never `suppress(BaseException)` around an unbounded
  `await task`. The supervisor's final close goes out even if it is
  cancelled again while stopping, and its heartbeat never pings after it
  has stopped.
- Long-running processes keep bounded in-memory state: the timeline is a
  ring buffer (`timeline_size`), and so is the alarm list.
- No message bus (pyobs's XMPP and Rockit's Pyro were both costs in the
  evaluations).
- Alpaca has no authentication: device traffic stays on an isolated network.
  Only the request API and the observer console face outward (both on
  loopback behind a TLS proxy, with tokens).
- Deployment on a control host (users and groups, units, layout, secrets,
  firewall, time sync, backups, upgrades and rollback): `docs/deploy.md`.

### 3.10 Configuration, modes and site code

JDE asked (2026-11-01): what lets robofast operate in different modes at
different sites, and add site- or instrument-specific sequences? Three
layers, so that adding a site is configuration plus device servers (section
1), never a fork and never an `if site == ...` in core.

**(a) Config: every site value lives under `sites/<site>/`.** Per-site JSON
or YAML says which Alpaca endpoint each device is, enclosure membership,
timeouts, limits and policies (`sites/minerva/aqawan1.json`,
`sites/minerva/weather.yaml`, `sites/flwo60/`, `sites/mearth_south/`).
`sites/minerva/README.md` maps every MINERVA value to its legacy
minerva-control `config/*.ini` source and lists the conflicts between them.
The rules:

- Core (`robofast/`, outside `robofast/sites/`) has no site
  names and no site numbers: no coordinates, elevations, apertures, port
  geometry, altitude limits, telescope or instrument names, help text.
- A site, telescope or instrument value that is missing is an error that
  names the key and the file, never another site's default (the error
  comes from `robofast.config.require_keys` or the config's own
  `from_dict`). Generic device tuning (timeouts, poll rates, retry counts)
  may keep defaults. Values derived from site values are derived, not
  assumed: the Sun's refraction uses the standard atmosphere at the
  configured elevation unless a pressure and temperature are configured
  (`docs/weather_bridge.md`).
- A driver for specific hardware (`robofast/devices/`: the Aqawan
  PAC, the KiwiSpec PC, the TCSGalil, the TRES front end, ...) may name its
  own hardware; the core modules that use it (sequencer, plan, dispatch,
  request intake, console, safety, imaging, photometry, vetting, Sun,
  weather) may not. Per-site advice goes in config too: MINERVA's "observe
  a B star through the iodine cell" for a refused lamp calibration is the
  enclosure config's `help.no_lamps`, so MEarth South's refusal does not
  carry it.
- `tests/core/test_no_site_values.py` enforces this on the AST of core
  (docstrings and comments may mention sites). What core still names is an
  explicit allowlist, each entry tagged with the step below that removes it;
  the test fails when an entry goes stale, so the list only shrinks.

**(b) Modes: one operating mode per enclosure (step 3, done;
docs/modes.md).** An enclosure's `mode` is a named policy bundle defined in
the site config: the built-in `robotic`, `attended`, `engineering` and
`shadow`, which a site may change, remove or add to (`modes`). A mode says
whether the supervisor may open at all, what the sequencer does (run the
plan; manual control only; log what it would do), whether the enclosure is
commanded or only watched, whether the request API takes requests, and the
operator-duty checks (needed or not, whether an expired duty closes or only
alarms, its ttl and grace) and automatic fault clearing; the weather,
fault, shutter, lease, park and heartbeat checks are enforced in every mode
that may open. The supervisor holds the mode (saved across restarts; a
missing or unreadable file means the config's default: `robotic` at
MINERVA, `attended` at the 60-inch and MEarth South) and the sequencer, the observer console
and the request API read it from its status; every switch (console or
`python -m robofast.modes`) is logged with who, when and why and
raised as a notice, never opens by itself (a less safe mode takes effect
through the normal lease path), and closes at once when the live lease may
not go on under the new mode. The active mode is in the FITS headers
(`OBSMODE`) and the plan record. `safety.attended_only` (section 3.3) is
now the enclosure's floor (no mode lowers it: the built-in `robotic` is not
offered without a heartbeat). Running unattended with no watchdog anyway is
the site's explicit choice (JDE 2026-11-02;
`safety.allow_unattended_without_watchdog`, docs/modes.md): it lifts the
floor and robofast warns loudly wherever the enclosure shows (the log at
start, on every switch and periodically, the switch notice, the status,
the console's enclosure panel and alarm list, the morning report). MEarth
South opts in; the 60-inch does not. The enclosure's `shadow` mode is the
documented way to shadow a legacy system with all of robofast; it commands
nothing that moves except one thing: an enclosure found open while an
enforced close condition is active (unsafe weather verdict, an enclosure
fault) is closed at once, with a critical alarm (the mode's `intervene`,
JDE 2026-11-02: "if there's an active safety risk, it should intervene
immediately"); the weather bridge's `--shadow` stays the bridge's own
flag.

**(c) Sequences and plug-ins: site code without forking core (steps 2
and 4, done).** Two mechanisms, for two kinds of difference:

- *Sequences* (step 4, docs/sequences.md): a site's or instrument's
  routine that is only an ordering of existing operations (calibration
  stage, filter, port, ADC, lamp on/off, focus, wait, camera frames, the
  spectrograph's own calibration frames) is a named step list in the
  enclosure config (`sequences`), run as an observation of kind
  `sequence` from a static or calibration plan: e.g. the TRES ThAr and
  flat sets (`sites/flwo60/flwo60.json`). Each step type is an entry of a
  registry (`robofast.sequences.SEQUENCE_STEPS`), so a plug-in adds
  steps. A sequence runs inside the sequencer's generation and grants like
  any observation; its `cleanup` (lamps off, the stage out, ...) always
  runs after its steps, with grants of its own, also when it is cancelled
  (every lamp it switched on goes off first). Whole sequences are checked
  when the config is loaded (unknown steps, devices and lamps the
  telescope lacks, frame types the instrument does not take, lamp frames
  with no lamp on, a lamp left on or kept on past its `max_on`) and again
  against the devices before the first step. A small library of generic
  **templates** (`arc_set`, `lamp_flat_set`, `bias_set`, `dark_set`; a
  registry, `SEQUENCE_TEMPLATES`) is instantiated by a site with its lamp,
  exposure time, count, warm-up and stage positions, and a site's own
  `sequence_templates` override them by name (JDE: "lamp calibrations are
  general enough we may want to offer a handful of generic sequences"; the
  observers' lamp scripts become site sequences later). OCS requests keep
  their parametric `arc` and `lamp_flat` kinds, and an `ARC` or `LAMP_FLAT`
  may instead name a site sequence made from a template
  (`extra_params.sequence`, listed in the telescope's dispatch
  `sequences`): it runs with the request's exposure time and count
  substituted, checked again (the lamps' `max_on`) at submission and
  before anything moves. MINERVA's iodine B-star calibration stays
  a `SPECTRUM` with `extra_params.iodine` (it needs a target and guiding,
  which a sequence's steps do not add).
- *Plug-ins* (step 2; docs/plugins.md): what a site needs that is new
  code (plan types, observation kinds, instrument handlers such as
  KiwiSpec, TRES and the imager, weather sources, guider hardware, recovery
  rungs) registers in a registry keyed by name, and an installed package
  can add entries through the `robofast.plugins` entry-point group. An
  observation kind is one handler object (`robofast/kinds.py`) that
  owns its validation, its request mapping and its execution, instead of
  the former if-chain across the plan's kind list, the sequencer, the
  request model and dispatch. The registries that existed already
  (weather `SOURCE_TYPES`, notifier `SINK_TYPES`) move onto the same
  mechanism; Alpaca `Action` names move into config.

The plan, approved by JDE 2026-11-01:

1. **Done:** site values out of core: the console's manual-slew limits
   from the telescope's config (refused when none are configured); the
   Sun's refraction from the site's elevation; required keys instead of
   another site's defaults in the device servers, dispatch, photometry,
   vetting, the guider's pulse guiding and the catalog CLI; neutral lamp
   messages with per-enclosure help; the guard test.
2. **Done:** registries and `robofast.plugins` entry points (plan types,
   observation kinds, instrument handlers, guider hardware, weather
   sources, request importers), KiwiSpec decoupled from the sequencer,
   Action names in config, the legacy importers moved under
   `robofast/sites/<site>/`. (The recovery ladder's rungs stay a
   fixed list until a site needs another.) **2a**
   (docs/plugins.md): the plug-in loader (`robofast.plugins`: entry
   points and a config's `plugins`), plan types, observation kinds (one
   handler per kind: `robofast/kinds.py`), instrument types
   (`robofast/instruments.py`: imager, camera_spectrograph, KiwiSpec;
   each dispatch config declares its instruments' types, `/instruments`
   and request intake follow them), KiwiSpec decoupled from the sequencer
   (neutral `SpectrographBusy`/`SpectrographError`, `spectrograph.type`),
   weather sources (MINERVA's adapters loaded from its weather config) and
   notifier sinks.
   **2b**: guider hardware registries (`robofast/guider/hardware.py`;
   KiwiSpec's backlight registered by `robofast.devices`), ADC
   switches found by name (`robofast/adc.py`), Action names in config
   with robofast's servers' names as defaults, the console's spectrograph
   panel described by the instrument type (`status_fields`); the legacy
   importers as site code: `robofast/sites/minerva/` (the MINERVA
   plug-in: weather pages, the legacy target CSV as request importer
   `minerva.legacy_csv`; the target Sheet, the legacy focus model, the
   legacy AO calibration) and `robofast/sites/flwo60/` (the telshell
   catalogs and their command line, tres-guider's calibration); a legacy
   frame's port from its file name is the throughput config's
   `file_ports`. The guard now also refuses a module named after a site
   outside `sites/` and `devices/`, and its allowlist is empty.
3. **Done:** enclosure operating modes, (b) above (docs/modes.md).
4. **Done:** config-defined sequences, (c) above (docs/sequences.md).

A new site is a new config directory, plus any device servers it needs,
plus (after step 2) any plug-ins it needs in its own package.

Site code that is not configuration (a new weather source type for the
bridge, `robofast.weather_sources.SOURCE_TYPES`; a new plan type,
`robofast.plan.PLAN_TYPES`; an observation kind or an instrument type)
plugs in through the Python entry-point group `robofast.plugins`: a site
package declares an entry there in its own `pyproject.toml`, pointing at a
function that registers its entries, and robofast runs it the first time a
registry is used, so a site never has to patch the core. Code that is not
installed is listed in the config that needs it (`plugins`). A name
registered twice is refused, naming both origins (docs/plugins.md).

robofast is packaged from `pyproject.toml` (`pip install .`): only
`robofast` is installed, with a `robofast-*` console command for each
long-running service and device server. The earlier, Windows-era
`robofast/` code was removed on 2026-11-04 (git tag `legacy-robofast`).

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
  (`robofast/testing/starfield.py`: the pupil image, with its central
  obstruction, convolved with the seeing; jitter, noise, subframes) with
  known truth, or on the real sky (`robofast/testing/skysim.py`:
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
| evening start | Sun below -6° | `robofast.when` in the sequencer unit (`deploy/`) |
| `night_rollover_hour` | 12 | plan record: earlier local times belong to the previous night |
| `record_top_n` | 5 | runners-up and rejected candidates recorded per decision |
| `park_before_close` | off | roll-off roofs: park every telescope (bounded, in parallel) before closing |
| `park_wait` | 60 s | bound on that park |
| `require_park_before_close` | off | the park-before-close interlock: a routine close waits until every telescope reports `AtPark`; emergencies still close after `park_wait` (needs `park_before_close`) |
| `park_interlock_escalate` | 300 s | a routine close held back by the interlock this long raises a critical alarm (the enclosure stays open) |
| `heartbeat_interval` | none | supervisor ping period; at most `heartbeat_timeout`/4, or the config is rejected |
| `heartbeat_timeout` | none | the enclosure's own heartbeat timeout (Aqawan PAC: 60 s, confirmed); required with `heartbeat_interval` |
| `fault_action` | none | the dome's read-only fault channel (Aqawan: `Faults`); any fault alarms and blocks opening, and a fault with the enclosure not closed closes it |
| `clear_faults_action`, `heartbeat_action` | `ClearFaults`, `Heartbeat` | the dome's clear-faults action (privileged: the supervisor's alone) and the heartbeat device's ping; the defaults are robofast's own servers' names |
| `fault_auto_clear` | off (MINERVA: on) | clear a fault automatically, only with the enclosure closed, never an E-stop; a recurrence stops it until an operator clears (`docs/aqawan.md`) |
| `fault_auto_clear_interval`, `fault_auto_clear_settle` | 1 h, 15 s | at most one automatic clear per interval; a fault still there `settle` after one counts as a recurrence |
| `attended_only` | true without a heartbeat device | the enclosure's floor: no mode opens without an operator on duty (section 3.3, docs/modes.md); FLWO 60-inch and MEarth South: on; MINERVA: off |
| `mode` | none: `robotic` (`attended` without a heartbeat) | the operating mode started in when its mode file is missing or unreadable; the shipped sites: `attended` (docs/modes.md) |
| `duty_ttl`, `duty_grace` | 300 s, 300 s | an operator's duty not renewed this long raises a critical alarm; after the grace, the supervisor closes |
| `shutter_unreadable_grace` | 15 s | while open is wanted, a shutter unreadable this long revokes the lease and closes (shutterError does so at once) |
| `shutter_error_backoff` | 30 min | no reopen after a shutter error, so a broken shutter is not cycled |
| `min_alt`, `max_alt` | 21°, 83° | per-telescope altitude limits (legacy MINALT, MAXALT) |
| `max_sun_alt` | -12° | observe light frames only with the Sun below this (legacy HORIZON) |
| `min_moon_sep` | 10° | Moon separation limit (legacy MINMOONSEP) |
| `w_setting`, `w_airmass`, `w_cadence` | 1, 1, 1 | dispatch score weights (section 3.7) |
| `setting_horizon` | 2 h | a target lost within this gets a setting bonus |
| `idle_poll` | 60 s | with nothing observable, ask again this often |
| `too_poll` | 10 s | re-check waiting ToOs this often (the drop box is taken in as files arrive: `robofast.doorbell`) |
| `doorbell_listen` | `127.0.0.1:0` | the drop box's doorbell; set an interface and port when the request API runs on another host |
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

1. ~~**Roll-off roof versus an unparked telescope.**~~ **Answered** (JDE,
   2026-11-02): an emergency close may close the roof on unparked
   telescopes, but some roofs can hit some telescopes, so a routine close
   there must wait for every telescope to park. Per enclosure:
   `require_park_before_close` (section 3.5). Which enclosures need it is
   still a per-site mechanical-clearance question; no shipped site config
   sets it yet.
2. The defaults in section 6.
3. Which of the legacy `robofast/` package's useful parts (YAML configs, HAL
   knowledge for Aqawan, APC PDU, IRF90, Paramount, DFM) still need porting.
   The package itself was removed on 2026-11-04 and the core renamed from
   `robofast_core` to `robofast`; the old tree is git tag `legacy-robofast`.
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
   (`robofast/devices/`: PAC client, PAC simulator, Dome server;
   ConformU runs in CI). Hardware facts not yet confirmed on a real PAC are
   listed in `aqawan_pac`'s and `aqawan_sim`'s module docs; the confirmed
   ones and the fault policy are in `docs/aqawan.md`.
6. Windows bridge on one control PC: ASCOM Remote for the camera and PWI4;
   measure command latency and full-frame download time. The mount: the PWI
   server (built, simulators only; `docs/pwi.md`), PWI2 now and PWI4 after
   its cutover; next, a first attended small-move session on T1 through a
   tunnel (`docs/pwi.md`, "First hardware session").
7. Shadow mode: robofast's supervisor runs read-only alongside the legacy
   system and logs what it *would* have done, for comparison. Off-site
   engineering through an SSH tunnel (built: `docs/remote.md`,
   `sites/minerva/remote/`): the hardware gate no longer takes loopback
   for a simulator (section 3.1), a read-only Aqawan server for a first
   connection beside the legacy dome daemon, engineering-mode configs.
8. One telescope, attended nights, then robotic; then the rest of MINERVA.
9. Dispatch scheduler, request model, target-of-opportunity interrupts
   and the authenticated request API (built; sections 3.7, 3.7.1), with
   the night's operations around them (built: idle release, closed-roof
   calibrations, evening start and morning end, filter and M3-port
   selection, partial re-takes, the nightly plan record with replay);
   next a TOM Toolkit facility module (`docs/tom_facility.md`) and an
   alert-broker client; then MEarth (collision checker). MINERVA's
   target and B-star lists come from their Google Sheet as request groups
   (built: `robofast/sites/minerva/target_sheet.py`,
   `docs/minerva_sheet.md`; next, the service-account key on the control
   host and a timer running `export`).
10. **Done:** several telescopes per enclosure (section 3.5) and the
    production sequencer entry point with its plan-provider interface.
11. KiwiSpec spectrograph service, its clients for the legacy spectrograph
    PC and a protocol-faithful simulator (built; `docs/kiwispec.md`, with
    open questions); next, spectra in the sequencer (`docs/kiwispec.md`
    section 4), then the service against the real PC, attended. Its
    auxiliary devices (iodine heater, iodine stage, backlight, vacuum
    gauges, chiller) are also driven directly, over serial URLs, as an
    Alpaca Switch/FilterWheel server passing ConformU in
    CI (built: `robofast/devices/kiwispec_aux_server.py`,
    `docs/kiwispec_aux.md`; next, a serial bridge on the PC and the
    hardware checks listed there).
12. FLWO 60" / TRES: an Alpaca Telescope + Focuser (+ Dome, if the TCS
    drives it) server for the 60-inch's DFM TCSGalil ("TCSGO"), with a
    protocol-faithful simulator, passing ConformU in CI, and the
    weather-unsafe scenario on the `sites/flwo60/` config (built:
    `robofast/devices/tcsgo_*`; protocol, hardware-moving commands
    and open questions in `docs/tcsgo.md`). The TRES front end's
    calibration stage (a PI C-663 stage controller) is served as an Alpaca
    FilterWheel (named positions: `out`, `sky`, `science`, ...) and Focuser
    (absolute microns), with a simulator, ConformU in CI, an optional
    Redis shim for the legacy TCS window, and the sequencer selecting an
    observation's `calstage` (built: `robofast/devices/tres_frontend_*`,
    `docs/tres_frontend.md`, hardware locked until confirmed). The
    calibration lamps (a Pulizzi power controller) are served as an Alpaca
    Switch with a simulator, a max-on limit and lamps off on a clean stop,
    ConformU in CI, and `ARC`/`LAMP_FLAT` requests and sequencer lamp frames
    for the 60-inch only, and each lamp's burning hours and starts kept
    from what the unit reports, with a replacement record and rated-life
    warnings (built: `robofast/devices/pulizzi*`, `lamp_hours.py`,
    `robofast/lamps.py`, `docs/pulizzi.md`, hardware locked until
    confirmed). The 60-inch's atmospheric dispersion corrector (two prism
    motors on one serial line; it serves TRES and FAST alike) is served as
    two Alpaca Rotators (the prisms) and a Switch (tracking on/off and its
    state), following the telescope from the TCSGO server with the legacy
    daemon's math (checked against its own test vectors), holding on stale
    input, during slews and beyond its zenith-distance limit, neutral at
    park and when off, with a simulator, ConformU in CI, the sequencer
    turning it on before light frames and its state in the headers (built:
    `robofast/devices/adc_*`, `docs/adc.md`, hardware locked until
    confirmed). The TRES catalogs import as request groups and export
    back for telshell (built: `robofast/sites/flwo60/catalogs/`,
    `docs/catalogs.md`; the target database waits for the live catalog
    directory). Next: answers to those questions, a TRES
    camera/spectrograph service, then a supervised test on the real TCS.
    MEarth South: an Alpaca Dome server for its roll-off roof (a
    parallel-port interface box, no watchdog, so attended only), with a
    simulator of its faults, passing ConformU in CI, and the
    `sites/mearth_south/` config, hardware locked until confirmed on site
    (`docs/mearth_roof.md`, which also describes the North roof).
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
    acquisition, offset guiding (`docs/offset_guiding.md`; MINERVA's sheet
    offsets checked against the star catalog on import), and the
    sequencer hook. Next: the ZWO backend on a real
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

17. The sky (built; `docs/sky.md`): the Gaia DR3 tile cache and downloaded
    bright stars, a sky simulator with pointing errors, the near-blind
    plate solver, solved WCS and zero points in every frame. Swappable
    catalog source (`gaia`, `ucac4`, `2mass`; a registry) and a switch
    that refuses non-commercial data, for commercial users (docs/sky.md,
    "Licences"). Next: target
    vetting and catalog-based acquisition, the per-port zero points
    measured on real frames, a pointing-model builder.

18. Site configuration, modes and site code (section 3.10): step 1
    (site values out of core, the guard test) and step 2 (the plug-in
    loader; plan, observation-kind, instrument, weather-source, guider
    hardware and request-importer registries; KiwiSpec out of the
    sequencer; Action names in config; the legacy importers under
    `robofast/sites/`) done; step 3 (operating modes, docs/modes.md)
    and step 4 (config-defined sequences, docs/sequences.md) done.

## 9. Borrowed ideas

| From | What |
|---|---|
| pyobs | typed device interfaces; state stamped with time and maximum age; in-process vs networked transport behind one interface; a simulator per device; planning separated from execution; constraint-based scheduling |
| Rockit | enclosure hardware heartbeat; "unsafe if any unsafe or all unknown" with per-sensor staleness; safe-for-N-minutes reopen hold; JSON nightly plans; PWI4 client practice |
| POCS | state machine defined as data; states tagged safe in any weather; fail-closed stale weather; simulator-backed tests |
| RTS2 | declared blocking between operations; value registry; dispatch selector with time-dependent bonus |
| LCO OCS / TOM Toolkit | request and status model; facility interface |
| ASCOM | Alpaca protocol, OmniSim, ConformU, ASCOM Remote |
| FLWO telshell (T. Groner) | the TRES catalog formats and their reading rules (`docs/catalogs.md`) |
