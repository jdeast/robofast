# Recovery ladder and the PDU

When a telescope's device fails, the sequencer makes the telescope safe
(abort, park) and then, if the telescope has a `recovery` entry for that
device, climbs its **recovery ladder** (`robofast_core/recovery.py`) before
the usual backoff. It replaces legacy `cdk700.recover()`, `killPWI` /
`restartPWI` and `powercycle`, which were called ad hoc.

## The ladder

Per device (`telescope`, `camera`, `focuser`, `rotator`, `filterwheel`), the
rungs listed in `steps`, in order:

1. `reconnect` -- drop the device's Alpaca client; the next command connects
   afresh (`DeviceOwner.reconnect`).
2. `restart` -- restart the vendor software with a hook: `{"command": [...]}`
   (must exit 0 within `step_timeout`; e.g. a service restart on the
   telescope's Windows PC over ssh) or `{"url": ..., "method": "POST"}`;
   then `settle` s and reconnect.
3. `powercycle` -- `Action("PowerCycle", "<switch>")` on the PDU's Alpaca
   Switch server (below), `boot_time` s, reconnect.

After each rung the device must answer (`AtPark`, `CameraState`, `Position`
read within `verify_timeout`), else the next rung is tried. Every rung is
bounded (`step_timeout` + `verify_timeout`) and raises an alarm (status
snapshot, console, notifier) saying what was tried and how it went. A
device that fails again within `escalate_window` (1 h) starts one rung above
the last that worked; when no rung is left the telescope **gives up for
the night** (`giving_up`, reason "recovery ladder exhausted"); the other
telescopes observe on. A recovered device gets another try regardless of
the consecutive-error count.

Power cycling is guarded: at most `max_powercycles` (1) a night per device
and one per `min_interval` (1800 s); only with the telescope parked
(`requires_parked`), the camera idle (`requires_camera_idle`) and, with
`camera_warm_above`, the CCD warmer than that -- the checks on the device
being cycled itself are skipped when it cannot answer. The PDU server adds
its own guards (below).

```json
"recovery": {
  "camera": {
    "steps": ["reconnect", "restart", "powercycle"],
    "restart": {"command": ["ssh", "t1-pc", "restart-camera-service"], "settle": 10},
    "powercycle": {"pdu": "127.0.0.1:11120", "switch": 0, "boot_time": 60},
    "camera_warm_above": -5, "max_powercycles": 1, "min_interval": 1800
  },
  "telescope": {"steps": ["reconnect", "restart"], "restart": {"command": ["..."]},
                "requires_parked": false}
}
```

## The PDU server

`python -m robofast_core.devices.pdu_server --config pdu.json [--hardware | --simulate]`
is an Alpaca **Switch** (ISwitchV2; ConformU-clean in CI,
`scripts/conformu_pdu.py`) for an APC switched PDU over SNMP
(`robofast_core/devices/snmp.py`: a small v1/v2c GET/SET client, no
dependency; `apc_pdu.py`: PowerNet-MIB `sPDUOutletCtl`, as legacy
`minerva_library/pdu.py`).

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
- The PDU address must be loopback unless the config says
  `allow_hardware: true` **and** the command line passes `--hardware`.
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

`tests/core/test_pdu.py` (BER round trip, client vs simulator incl. wrong
and read-only communities, credentials file mode, the hardware gate, the
Alpaca server incl. protected outlets and the cycle rate limit, a silent
PDU); `tests/core/test_recovery.py` (each rung, escalation and
exhaustion, the power-cycle guards, step bounds, a cycle through the real
server and simulator, and the sequencer recovering one telescope or giving
up on it while the other observes on).
