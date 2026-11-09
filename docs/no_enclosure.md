# A telescope with no enclosure

A classroom or backyard telescope on a tripod has no dome, no roof and no
weather station. robofast models that as two devices, so the safety
supervisor (`robofast/safety.py`) runs the same code as at an enclosure
(first proposed by Guven Gurkan, PR #125):

* **The sky**, an Alpaca Dome (`robofast-no-enclosure-server`,
  `robofast/devices/no_enclosure_server.py`): `OpenShutter` and
  `CloseShutter` succeed at once and the shutter state follows the
  command. Nothing moves.
* **The operator's duty**, an Alpaca SafetyMonitor in the same server:
  `IsSafe` is true only while an operator is on duty at the supervisor
  (the observer console's `duty` action, docs/console.md, "Operator duty"),
  and false when nobody is or the supervisor has not answered for
  `stale_after`. With no weather sensor, the operator on duty judges the
  sky.

The no-enclosure server's config:

```json
{"name": "classroom", "listen": "127.0.0.1:11130", "supervisor": "http://127.0.0.1:8100"}
```

and the enclosure config, which names it as both the dome and the safety
monitor:

```json
{
  "name": "classroom",
  "enclosure": "none",
  "listen": "127.0.0.1:8100",
  "devices": {"dome": "127.0.0.1:11130", "safetymonitor": "127.0.0.1:11130"},
  "telescopes": {"t1": {"telescope": "127.0.0.1:11111/0", "camera": "127.0.0.1:11111/0"}},
  "safety": {"park_before_close": true, "require_park_before_close": true,
             "reopen_hold": 0},
  "images": {"data_root": "data", "site": "classroom"},
  "plan": {"type": "calibration", "biases": 1}
}
```

Then run the three as for any enclosure:
`robofast-no-enclosure-server --config sky.json`,
`robofast-supervisor --config classroom.json`,
`robofast-sequencer --config classroom.json`. (`reopen_hold` is the time
the operator must have been on duty before the first lease; an enclosure's
20 minutes of continuously safe weather mean nothing here.)

## What the supervisor does then

Nothing in it is specific to this site; these are its ordinary rules on
these two devices:

* It grants a lease only while an **operator is on duty**: the site has no
  hardware heartbeat, so `attended_only` is the floor, and the safety
  monitor is unsafe without a duty too.
* It revokes the lease, so the sequencer aborts the exposure and **parks**,
  when the operator goes off duty, when the duty is not renewed
  (`duty_ttl`, then `duty_grace`), when the lease is not renewed, and when
  the supervisor stops.
* Before it "closes" the sky it **parks every telescope** itself
  (`park_before_close`), after giving the sequencer `park_wait / 2` for its
  own cleanup; so a sequencer that died leaves no telescope tracking. The
  park-before-close interlock (`require_park_before_close`) holds a routine
  close, parking again on every pass, until every telescope reports
  `AtPark`, and raises a critical alarm after `park_interlock_escalate`.
* A dead sequencer's lease is recovered (or cleared by an operator) only
  once the sky reads closed, so only after that park; and no lease is
  granted until every telescope reports `AtPark`.
* Calibration frames (biases, darks) need no lease, as everywhere: the plan
  above takes its bias with nobody on duty.

If the supervisor dies, nothing parks from its side: the sequencer's lease
lapses (its client revokes a lease it cannot renew), and it aborts and
parks.

## What is refused

With `"enclosure": "none"` the supervisor refuses, naming the key, before
any device is contacted:

* `enclosure` other than `"none"` (leave it out for an enclosure);
* `devices.observingconditions` or `devices.heartbeat`;
* no telescope;
* `safety.heartbeat_interval`, `safety.fault_action` and
  `safety.fault_auto_clear`, which need an enclosure;
* `safety.attended_only: false` and
  `safety.allow_unattended_without_watchdog`: nothing parks the telescopes
  if the supervisor dies, so the site stays attended only;
* `safety.park_before_close` or `safety.require_park_before_close` not
  true;
* a configured mode whose `enclosure` is `watch` (the built-in `shadow` is
  left out), and any mode that may observe with nobody on duty.

## Limits

* Going off duty also makes the safety monitor unsafe (within its
  `poll_interval` and the supervisor's), which the supervisor treats as
  unsafe weather: an emergency close, which the park interlock does not
  hold. The telescopes are still parked once (after the sequencer's
  `park_wait / 2`), but a mount that does not confirm `AtPark` then is an
  alarm, not retried; no lease is granted until it is parked. The operator
  has just gone off duty and is at the telescope.
* A sequencer that dies after its lease is granted but before the
  supervisor's next pass opens the sky (a fraction of `poll_interval`) may
  leave an unparked telescope under a sky that never read open: nothing is
  closed, so nothing parks. The orphaned lease alarms after
  `orphan_timeout`, and no new lease is granted until the telescope is
  parked. (The same holds at an enclosure, under its closed roof.)
* An operator who walks away without going off duty leaves the telescope
  observing for up to `duty_ttl + duty_grace` (300 + 300 s by default).
* The observer console shows the sky as the enclosure's shutter.
