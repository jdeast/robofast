# A telescope with no enclosure

A classroom or backyard telescope on a tripod has no dome, no roof and
no safety monitor. Its enclosure config says so in plain words:

```json
{
  "name": "classroom",
  "enclosure": "none",
  "listen": "127.0.0.1:8100",
  "telescopes": {"t1": {"telescope": "127.0.0.1:11111/0", "camera": "127.0.0.1:11111/0"}},
  "images": {"data_root": "data", "site": "classroom"},
  "plan": {"type": "calibration", "biases": 1}
}
```

and the supervisor and the sequencer run as for any enclosure
(`robofast-supervisor --config classroom.json`, then
`robofast-sequencer --config classroom.json`).

## What the supervisor does then

There is nothing to open or close, and nothing reads the weather, so the
supervisor's one safety action is the lease (`robofast/safety.py`, "No
enclosure"):

* It grants a lease only while an **operator is on duty** (the observer
  console's `duty`, docs/console.md; `attended_only` is the floor and
  cannot be lowered). The operator judges the sky: the supervisor's
  `reason` says so instead of a weather verdict.
* It revokes the lease, so the sequencer aborts the exposure and **parks**,
  when the operator goes off duty, when the duty is not renewed
  (`duty_ttl`, then `duty_grace`), when the lease is not renewed, and when
  the supervisor stops.
* If the sequencer cannot park (it died, or its cleanup does not finish),
  its lease is revoked and not released: the supervisor then parks the
  telescopes itself, with a critical alarm: at once when the lease lapsed
  (not renewed: the sequencer is silent), after `safety.park_wait / 2` for
  the sequencer's own cleanup otherwise. Nothing closes over them, so no one
  else would.
* A lease is granted only once every telescope reports parked, and a dead
  sequencer's lease is recovered only once every telescope reports parked
  (with an enclosure: once it is closed).
* Calibration frames (biases, darks) need no lease, as everywhere: the plan
  above takes its bias with nobody on duty.

## What is refused

Each is an error naming the key, before any device is contacted:

* `enclosure` other than `"none"` (leave it out for an enclosure);
* `devices.dome`, `devices.safetymonitor`, `devices.observingconditions` or
  `devices.heartbeat` with `"enclosure": "none"`;
* no telescope;
* `safety.heartbeat_interval`, `safety.fault_action`,
  `safety.park_before_close`, `safety.attended_only: false` and
  `safety.allow_unattended_without_watchdog`, which need an enclosure;
* a configured mode whose `enclosure` is `watch` (the built-in `shadow` is
  left out), and any mode that may observe with nobody on duty.

If the supervisor dies, nothing can close: the sequencer's lease lapses (its
client revokes a lease it cannot renew), and it aborts and parks.

Without `"enclosure": "none"`, a config with no `devices.dome` or
`devices.safetymonitor` is refused as before, naming the key.
