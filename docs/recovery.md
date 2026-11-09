# Recovery ladder and the PDU

When a telescope's device fails, the sequencer makes the telescope safe
(abort, park) and then, if the telescope has a `recovery` entry for that
device, climbs its **recovery ladder** (`robofast/recovery.py`) before
the usual backoff. It replaces legacy `cdk700.recover()`, `killPWI` /
`restartPWI` and `powercycle`, which were called ad hoc.

## The ladder

Per device (`telescope`, `camera`, `focuser`, `rotator`, `filterwheel`,
`guider`), the rungs listed in `steps`, in order:

1. `reconnect` -- drop the device's Alpaca client; the next command connects
   afresh (`DeviceOwner.reconnect`).
2. `restart` -- restart the vendor software with a hook: `{"command": [...]}`
   (must exit 0 within `step_timeout`; e.g. a service restart on the
   telescope's Windows PC over ssh) or `{"url": ..., "method": "POST"}`;
   then `settle` s and reconnect (see "The restart hook").
3. `home` (telescope only) -- if robofast's PWI server (docs/pwi.md) reports
   the mount not homed (`Action("Status")`: `homed`), its Alpaca `FindHome`
   (unparking first and parking again after, if it was parked), then wait
   until `Slewing` reads false; bounded by `home_timeout` (300 s) instead
   of `step_timeout`. The server homes only with `allow_find_home: true`
   in its config: otherwise (`CanFindHome` false, or `FindHome` answering
   NotImplemented) the rung is **skipped** -- logged and alarmed as
   unavailable, never counted as a recovery -- and so is it on a telescope
   that is not a PWI server or that reports itself homed.
4. `powercycle` -- `Action("PowerCycle", "<switch>")` on the PDU's Alpaca
   Switch server (below), `boot_time` s, reconnect. A PDU server whose
   action is named differently: `powercycle.action`. A device that
   shares its power and USB with the mount says so (`requires_pwi_stop`,
   below).

On a telescope served by robofast's PWI server, `reconnect` and `restart`
also reconnect PWI to the mount: `Action("MountConnect")` if `Status` says
PWI is not connected, then `Action("MotorsEnable")` if both axes are
disabled -- the state of a PWI that was just started. An axis PWI disabled
while connected (a drive fault, e.g. PWI4's I2t) is left disabled: the
telescope is not usable and the ladder climbs on.

A camera with a `cooling` entry (docs/cooling.md) has its cooler set again
by every rung that reconnects it -- the camera owner's `on_connect` hook
runs on each fresh connection -- because MINERVA T1's Andor ASCOM driver
switches its cooler off whenever it is connected again.

After each rung the device must answer (`AtPark`, `CameraState`, `Position`
read within `verify_timeout`; the guider: `GET /status` answering and not
`failed`), else the next rung is tried. A PWI telescope
must also be usable: `Status` shows it connected with both axes enabled
and, when its steps include `home`, homed. A mount that answers but is not
usable fails the rung at once (no waiting out `verify_timeout`): after a
PWI restart the `restart` rung fails with "the mount is not homed" and
`home` comes next. Every rung is
bounded (`step_timeout` + `verify_timeout`) and raises an alarm (status
snapshot, console, notifier) saying what was tried and how it went. A
device that fails again within `escalate_window` (1 h) starts one rung above
the last that worked; when no rung is left the ladder is **escalated**
(below): an agent hook, if configured, may try to fix it, and a person is
always told. A telescope that is not brought back **gives up for the
night** (`giving_up`, reason "recovery ladder exhausted (...); escalation:
..."); the other telescopes observe on. A recovered device gets another try regardless of
the consecutive-error count.

Power cycling is guarded: at most `max_powercycles` (1) a night per device
and one per `min_interval` (1800 s); only with the telescope parked
(`requires_parked`), the camera idle (`requires_camera_idle`) and, with
`camera_warm_above`, the CCD warmer than that -- the checks on the device
being cycled itself are skipped when it cannot answer. The PDU server adds
its own guards (below).

A PWI that died or was restarted (MINERVA T1, 2026-11-06: Windows Task
Scheduler closed PWI2) is the ladder `["reconnect", "restart", "home"]`:
reconnect fails (PWI does not answer), the restart hook starts PWI again
and the rung reconnects PWI to the mount, the mount answers but is not
homed, `home` homes it, and the PWI server loads the M3 port's pointing
model again (`pointing_models`, docs/pwi.md).

```json
"recovery": {
  "camera": {
    "steps": ["reconnect", "restart", "powercycle"],
    "restart": {"command": ["ssh", "t1-pc", "restart-camera-service"], "settle": 10},
    "powercycle": {"pdu": "127.0.0.1:11120", "switch": 0, "boot_time": 60},
    "camera_warm_above": -5, "max_powercycles": 1, "min_interval": 1800
  },
  "telescope": {"steps": ["reconnect", "restart", "home"],
                "restart": {"command": ["..."], "settle": 5},
                "home_timeout": 300, "requires_parked": false}
}
```

## The guider

The guider (`robofast/guider/service.py` and its guide camera: MINERVA's
FAU cameras) is a device of the ladder. Any guider failure -- an
acquisition that times out (night1108: T2 gave up after three "not guiding
after 10 s" in a row, without trying anything), a guide camera that stops
answering, a guider that fails mid-observation -- is a `GuiderError`, and
with a `guider` policy it climbs the guider's ladder before it counts
towards giving up, exactly as the other devices (a recovered guider gets
another try; an exhausted ladder gives the telescope up). The rungs,
after legacy `imager.recover()` (disconnect and reconnect; quit, then kill
MaxIm; power-cycle):

- `reconnect`: `POST /reconnect` to the guider service -- it stops
  guiding, closes the guide camera and opens it again;
- `restart`: the hook restarts what serves the camera -- for an Alpaca
  guide camera its driver (e.g. the ASCOM Remote server on the
  telescope's PC: the counterpart of legacy's MaxIm restart), or the
  guider service itself -- and must exit 0 once it answers; then
  `reconnect`;
- `powercycle`: the guide camera's PDU outlet.

```json
"guider": {
  "steps": ["reconnect", "restart", "powercycle"],
  "restart": {"command": ["ssh", "t2-pc", "restart-fau-camera"], "settle": 10},
  "powercycle": {"pdu": "127.0.0.1:11120", "switch": 4, "boot_time": 30,
                 "requires_pwi_stop": true,
                 "pwi_stop": {"command": ["ssh", "t2-pc", "stop-pwi"]},
                 "pwi_start": {"command": ["ssh", "t2-pc", "start-pwi"], "settle": 10}}
}
```

## Power and USB shared with the mount (`requires_pwi_stop`)

MINERVA's cameras share a power and USB "black box" with the mount:
legacy `imager.recover()` shut the telescope down and killed PWI before
it power-cycled a camera, and started PWI and initialized the telescope
after. robofast makes that an explicit option of a camera's (or the
guider's) `powercycle` rung, never hidden coupling:
`"requires_pwi_stop": true` with `pwi_stop` and `pwi_start` hooks (the
`restart` hook's contract: `pwi_start` exits 0 once PWI answers). It needs
`requires_parked` (the default): the rung refuses unless the telescope is
parked. It then runs `pwi_stop`, cycles the outlet, waits `boot_time`, and
runs `pwi_start` -- also when the cycle failed -- then brings the mount
back as the telescope's ladder would (reconnect, `MountConnect`,
`MotorsEnable`, `home` if the PWI server allows it), and reconnects the
camera. A mount that does not come back is alarmed
(`recovery_mount_after_cycle`) and left to the telescope's own ladder at
its next command. Not allowed on the `telescope` policy itself (its PWI
restart is its `restart` rung).

## Escalation: an agent, then a person

The ladder's last step (`robofast/escalation.py`). When every rung has
failed, the sequencer opens an **incident** for the device:

1. With an `agent` hook configured (and the rate limits below allowing),
   it runs the hook **in the background**. The failing telescope is out of
   the plan meanwhile (snapshot state `escalated`; it waits like a
   telescope in an error backoff) and the other telescopes observe on --
   nothing waits for the agent. Its bound is `agent.timeout` (default
   1800 s); past it the hook's whole process group gets SIGTERM, then
   SIGKILL. If the hook exits 0, robofast **checks the device itself**
   (a fresh connection; the rung's own check: the device answers and a PWI
   mount is usable) -- the agent's word is never taken. Verified: the
   telescope rejoins the night (its ladder starts from the first rung
   again); anything else: it gives up for the night.
2. **Always**, the incident ends with **one summary for a person**: the
   error, every rung tried and how it went, the agent's own summary (or
   "no agent configured", "timed out after ... s", "not run: ..."), and
   the outcome -- *recovered* (the telescope rejoins) or *dropped for the
   night, the others observe on*. It is in the sequencer's status snapshot
   (`telescopes.<t>.incidents`), which the observer console shows as an
   alarm (the summary as its detail; level `critical` when dropped,
   `alarm` when recovered) and the alarm notifier sends like any other
   (docs/notifier.md): **one path**, no second way to page. Each
   incident's alarm text is its own (`t1 camera incident 1 (<UTC>): ...`),
   so the notifier sends it once; the agent starting is an event on the
   console, not a page.

Many hardware faults cannot be fixed remotely, by an agent or a person
(a dead power supply, a cable, a seized motor): dropping the telescope
and observing on with the others stays the fallback.

**Rate limits.** At most `max_per_night` (2) agent runs per device per
night (the sequencer runs one night per process). A fault that comes back
after the agent "fixed" it (the same device and error, numbers ignored)
is not escalated again: the telescope is dropped and the summary says so.
Each incident gets one summary; the notifier deduplicates by alarm id and
its own per-sink rate limits still apply.

```json
"recovery": {
  "escalate": {
    "agent": {"command": ["/usr/local/bin/robofast-agent-hook"], "timeout": 2700},
    "max_per_night": 2,
    "safe": ["The telescope's PC may be rebooted: nothing else runs on it."]
  },
  "camera": {"steps": ["reconnect", "restart", "powercycle"], "...": "..."},
  "telescope": {"steps": ["reconnect", "restart", "home"], "...": "...",
                "escalate": {"max_per_night": 1}}
}
```

`escalate` at the top of a telescope's `recovery` applies to all its
devices; a device's own `escalate` is merged over it (`"agent": null`
there: no agent for that device). Keys: `agent` (`command`: argv, no
shell, `command[0]` an absolute path; `timeout` s; `env`: extra
environment), `max_per_night` (2), `log_entries` (200: timeline entries
in the context), `safe` (site lines added to what the agent is told is
safe), `summary_max` (4000 characters), `workdir` (where the hook's
temporary directory is made; default the system's). Without `escalate`
there is no agent, and the person is told at once.

The rehearsal (`robofast.testing.rehearsal`) refuses an `agent`: it
would act outside the rehearsal's guards. An exhausted ladder there
still gets its summary.

### The agent hook

The hook lives with the site's deployment, **outside this repository**,
with whatever hosts and credentials it needs. Its contract:

- **Input.** A JSON context on **stdin** and in the file
  `$ROBOFAST_ESCALATION_CONTEXT` (mode 600, in a private temporary
  directory that is also the hook's working directory and is deleted
  after it exits): `incident`, `time`, `sequencer`, `telescope`,
  `device`, `device_name`, `error`, `tried` (`[{"rung", "outcome"}]`),
  `other_telescopes`, `timeout`, `safe` (what it may and may not do:
  robofast's lines -- the failing telescope is out of the plan and gets
  only reads until the hook exits; never command the other telescopes,
  the enclosure, the safety supervisor, the weather station or another
  device's outlet; do not move the telescope unless a site line allows
  it; when unsure, change nothing -- then the site's `safe` lines),
  `contract`, `timeline` (the telescope's recent commands and events) and
  `status` (the sequencer's status snapshot). Also in the environment:
  `ROBOFAST_ESCALATION_TELESCOPE`, `_DEVICE`, `_SUMMARY`; the rest is the
  sequencer's own environment plus `agent.env`.
- **What it may do.** Try a fix by its own means (restart a service on
  the telescope's PC, reconnect a driver, read logs ...) within `safe`.
- **Exit status.** 0: the device should be retried (robofast verifies it);
  anything else: it could not fix it. Past `timeout` it is killed, with
  its process group.
- **Summary.** A short human-readable summary -- the diagnosis, what it
  did, the result -- written to `$ROBOFAST_ESCALATION_SUMMARY`, or else
  its stdout (the last `summary_max` characters). It goes to a person
  verbatim: no secrets.

An example hook that runs a headless coding agent with a diagnosis
prompt (here Claude Code's non-interactive mode; any agent CLI that takes
a prompt and can be limited to a set of tools works the same way):

```bash
#!/bin/bash
# /usr/local/bin/robofast-agent-hook -- site deployment, not in robofast.
set -u
ctx="$ROBOFAST_ESCALATION_CONTEXT"
prompt="You are the on-call engineer for a robotic telescope. robofast's
recovery ladder failed for $(jq -r '.telescope + " " + .device' "$ctx").
The incident (error, every rung tried, recent log, status) is in $ctx;
read it, and obey its \"safe\" list exactly. Diagnose the fault using
the site's runbook (/etc/robofast/runbook.md) and the read-only tools you
have. If, and only if, the runbook's restart procedure for this device
applies, run it. Then write to $ROBOFAST_ESCALATION_SUMMARY a summary for
the observer (at most 20 lines): DIAGNOSIS, WHAT I DID, RESULT. Last line
exactly RETRY if the device should be tried again, else GIVE-UP."
claude -p "$prompt" \
  --allowedTools "Read,Bash(/usr/local/bin/site-restart-camera-service:*),Bash(jq:*)" \
  --max-turns 40 >/dev/null 2>&1
tail -n 1 "$ROBOFAST_ESCALATION_SUMMARY" 2>/dev/null | grep -qx RETRY
```

The agent's tools are the site's choice: give it the narrowest set that
can fix the faults the runbook covers (here one site restart script), and
nothing that moves hardware, opens the enclosure or switches a PDU. Its
API key comes from the sequencer's environment (e.g. a systemd
`LoadCredential=` file the hook reads), never from robofast's config.

## The restart hook

`restart.command` is run as given (no shell), with `step_timeout` as its
bound. The contract:

- it restarts the vendor software (for a PWI telescope: PWI on the
  telescope's PC) and **exits 0 only once that software answers again**
  (for PWI2: `GET /?cmd=getsystem` on its port returns a status); any
  other exit, or no exit within `step_timeout`, fails the rung;
- it does nothing else: connecting PWI to the mount, enabling, homing and
  the pointing model are robofast's (the rungs above and the PWI server);
- its output (the last 300 characters) goes into the rung's
  `recovery_restart` event; it must not print secrets.

Site hooks (MINERVA's goes through the site gateway to the telescope's
Windows PC: `taskkill` of `PWI.exe` and `ComACRServer.exe`, then
`schtasks /Run` of the PWI task, then poll the HTTP API) live in the
site's deployment, outside this repository, with their host names and
credentials. `tests/e2e/pwi2_restart.py` is the hook the tests use: it
restarts a PWI2 simulator process.

## In the rehearsal

`python -m robofast.testing.rehearsal --recovery t1=t1-recovery.json`
(one per real telescope; the JSON is a `recovery` entry as above, a
`guider` policy included, without `powercycle`: the rehearsal never
switches a PDU) gives the night's
sequencer that ladder, exactly as in production; the rehearsal's mount
guard forwards to that telescope what the ladder sends (`Action("Status")`,
`MountConnect`, `MotorsEnable`, `FindHome`) and still never a real `Park`
or `Unpark`. At the preflight a real telescope whose endpoints fail gets
its ladder once (through a guard of its own) and is read again: still
failing, it is **dropped** (a WARNING; a problem in the evaluation and the
summary) and the night goes on with the other telescopes. The rehearsal
refuses when a telescope without a recovery policy fails, or when no real
telescope is left.

## The PDU server

`python -m robofast.devices.pdu_server --config pdu.json [--hardware | --simulate]`
is an Alpaca **Switch** (ISwitchV2; ConformU-clean in CI,
`scripts/conformu_pdu.py`) for an APC switched PDU over SNMP
(`robofast/devices/snmp.py`: a small v1/v2c GET/SET client, no
dependency; `apc_pdu.py`: PowerNet-MIB `sPDUOutletCtl`, as legacy
`minerva_library/pdu.py`).

- `--status` prints the PDU and every outlet, `--check-write N` also sets
  outlet N to its own state (proves write access, switches nothing), then
  both exit. An APC drops a SET it will not accept without answering, so
  "reads work, SET times out" means the write community is wrong or
  read-only, or the PDU refuses writes while someone is logged in to its
  web or console interface (APC community access "Write"; "Write+" does
  not); the check says so.
- A PDU behind an SSH tunnel: `"relay": true` and `scripts/snmp_relay.py`
  on a site host (docs/remote.md).

- Only outlets listed in the config are switches. `writable: true` lets
  them be switched; anything the safety path needs (the enclosure
  controller, the weather station, the network, the supervisor's computer)
  is listed `writable: false` or not at all: `CanWrite` false, `SetSwitch`
  refused.
- `Action("PowerCycle", id)` only for `cycle: true` outlets, at most once
  per `min_cycle_interval` s each; `Action("Status")` lists every outlet.
- SNMP communities come only from `credentials`, a JSON file
  `{"read": ..., "write": ...}` readable by its owner alone (mode 600);
  never in the config, never logged.
- The PDU must be a verified simulator unless the config says
  `allow_hardware: true` **and** the command line passes `--hardware`
  ([the hardware gate](design.md#the-hardware-gate)).
  `--simulate` runs a simulated PDU (`APCPDUSim`) on loopback.

```json
{"name": "pdu-aqawan1", "listen": "127.0.0.1:11120",
 "pdu": {"host": "<pdu address>", "port": 161, "timeout": 2, "retries": 2},
 "credentials": "/etc/robofast/pdu-aqawan1.credentials.json",
 "allow_hardware": true,
 "outlets": [
   {"outlet": 3, "name": "t1 camera", "writable": true, "cycle": true},
   {"outlet": 8, "name": "aqawan PAC", "writable": false}],
 "min_cycle_interval": 1800}
```

The outlet map of MINERVA's PDUs is to be taken from the legacy
`pdu_*.ini` files and confirmed on site before `allow_hardware` is set.

## Tests

`tests/core/test_escalation.py` (an agent that fixes the device -- it
rejoins while the other telescope observed on, and the person gets the
summary; one that fails, one that exits 0 without fixing it -- robofast's
own check drops it; one that hangs -- killed with its process group at its
timeout; no agent configured; the same fault again and `max_per_night`;
the hook's input, environment and summary; the config merge and its
refusals; the incident as one console alarm);
`tests/core/test_pwi_recovery.py` (the `home` rung and the PWI reconnect
on a PWI2 simulator, a skipped `home`, `pointing_models`, the guard and
the rehearsal's preflight: recovered, dropped, refused);
`tests/e2e/test_rehearsal_recovery.py` (a PWI2 simulator killed mid-night
and brought back by the restart hook, reconnect, home and its model; a
telescope dropped at the preflight while the other observes the night);
`tests/core/test_pdu.py` (BER round trip, client vs simulator incl. wrong
and read-only communities, credentials file mode, the hardware gate, the
Alpaca server incl. protected outlets and the cycle rate limit, a silent
PDU); `tests/core/test_recovery.py` (each rung, escalation and
exhaustion, the power-cycle guards, step bounds, a cycle through the real
server and simulator, `requires_pwi_stop`, the guider's ladder on a
simulated guider service, and the sequencer recovering one telescope or
giving up on it while the other observes on).
