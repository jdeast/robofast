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
- **Park before reopen.** With a telescope configured, no lease is granted
  until it reports `AtPark` (a read-only check), so nothing reopens on a
  telescope that a crash or a failed cleanup left unparked.

### 3.4 Sequencer, one per enclosure

`robofast_core/sequencer.py`. Each "generation" of observing:

1. request an open lease and wait for the supervisor to open;
2. **re-plan from scratch** (never resume a stale action);
3. take grants for this generation only;
4. run the observation as one asyncio task;
5. on lease revocation: revoke the grants (fence), cancel the task, wait a
   bounded time for it to finish; if it doesn't, **quarantine** (never
   release the lease, so nothing reopens) and alarm;
6. clean up with fresh grants: abort the exposure, park, in parallel and
   bounded;
7. release the lease; only then may a new generation start.

At startup, before its first lease, the sequencer waits while a live lease
of its own holder name is outstanding (a crashed predecessor's, until it
lapses), cleans up (always: a crash can leave the telescope unparked even
with no lease outstanding), and then recovers the predecessor's lease if
there is one. Its name must be stable across restarts and unique per
enclosure; one process manager unit per enclosure keeps it so. If two
processes do share a name, the supervisor still never recovers a live lease,
recovery is idempotent, and they take turns, one lease at a time.

Repeated device errors back off, then give up for the night and alarm.

The sequencer's states (startup, calibrations, observing, weather hold,
shutdown) are an explicit graph defined as data, POCS-style, with states
tagged as safe to enter in any weather.

### 3.5 Several telescopes per enclosure

The enclosure sequencer arbitrates its telescopes: one lease per enclosure,
grants per telescope. MEarth North needs a **collision checker** that vets
every slew target and path against the other telescopes' positions before
the grant allows the move.

### 3.6 Telescopes and pointing models

The telescope layer hides the mount type. Pointing models live with the
mount software where it has them (PWI4 for the CDK700s, per M3 port), and in
robofast's telescope layer for mounts that don't. The sequencer never sees a
model.

### 3.7 Planning and the request API

- Requests are modelled the LCO OCS way (RequestGroup → Request →
  Configuration → InstrumentConfig, with status per configuration). A small
  authenticated REST API accepts, validates, reports and cancels requests.
  A TOM Toolkit facility module can sit on it later.
- The first scheduler is a **dispatch scheduler** (priority, a
  time-dependent bonus, and hard constraints: altitude, Moon separation,
  twilight), RTS2/POCS-style. Queues for targets of opportunity and manual
  observations come ahead of it.
- The night's plan is written as JSON, so it can be inspected and replayed.
- An interrupted observation is marked failed and the night re-planned.

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
timeouts, limits, and policies. A new site is a new config directory plus any
device servers it needs.

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
  (heartbeat closes).
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
| `error_backoff` | 300 s | wait after a device error |
| `max_consecutive_errors` | 3 | then give up for the night and alarm |
| `park_before_close` | off | roll-off roofs: park (bounded) before closing |
| `park_wait` | 60 s | bound on that park |

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
4. Site weather bridge (Alpaca SafetyMonitor + ObservingConditions) for
   MINERVA's real feeds.
5. Aqawan enclosure Alpaca server with a simulator mode, passing ConformU.
6. Windows bridge on one control PC: ASCOM Remote for the camera and PWI4;
   measure command latency and full-frame download time.
7. Shadow mode: robofast's supervisor runs read-only alongside the legacy
   system and logs what it *would* have done, for comparison.
8. One telescope, attended nights, then robotic; then the rest of MINERVA.
9. Request API and dispatch scheduler; then MEarth (collision checker).

## 9. Borrowed ideas

| From | What |
|---|---|
| pyobs | typed device interfaces; state stamped with time and maximum age; in-process vs networked transport behind one interface; a simulator per device; planning separated from execution; constraint-based scheduling |
| Rockit | enclosure hardware heartbeat; "unsafe if any unsafe or all unknown" with per-sensor staleness; safe-for-N-minutes reopen hold; JSON nightly plans; PWI4 client practice |
| POCS | state machine defined as data; states tagged safe in any weather; fail-closed stale weather; simulator-backed tests |
| RTS2 | declared blocking between operations; value registry; dispatch selector with time-dependent bonus |
| LCO OCS / TOM Toolkit | request and status model; facility interface |
| ASCOM | Alpaca protocol, OmniSim, ConformU, ASCOM Remote |
