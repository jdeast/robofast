# Operating modes

An enclosure runs in one **operating mode** at a time: a named bundle of
policy that the safety supervisor, the sequencer, the observer console and
the request API all read from the supervisor and honor
(`robofast/modes.py`; design.md 3.10 (b)). The site config says which
modes exist and what each means, so "robotic at MINERVA, attended at the
60-inch" is configuration, never an `if site == ...` in core.

## The built-in modes

| Mode | Opens? | Sequencer | Enclosure | Requests | Operator on duty | Fault auto-clear |
|---|---|---|---|---|---|---|
| `robotic` | by its own policy | runs the plan | commanded | accepted | not needed | the `safety` entry's |
| `attended` | only with an operator on duty | runs the plan | commanded | accepted | needed; expiry closes | the `safety` entry's |
| `engineering` | only with an operator on duty | **manual control only** | commanded | **refused** | needed; expiry closes | off |
| `shadow` | **never** | logs what the plan would do | **watched**; closed only on an active safety risk | accepted | not needed | off |

* **robotic** -- unattended observing: the supervisor opens when the
  weather has been safe for the reopen hold and the sequencer holds a lease.
  Not offered on an enclosure with no hardware watchdog unless the site
  opts in (below).
* **attended** -- the same plan, but no lease (so no opening) unless an
  operator is on duty through the observer console (docs/console.md,
  "Operator duty"); a duty not renewed for `duty_ttl` raises a critical
  alarm, and `duty_grace` later the enclosure closes.
* **engineering** -- nothing autonomous: the plan is not asked (only peeked,
  to end the night when it has nothing more), so no observation, no
  calibration frame and no automatic opening. A telescope under the
  console's manual control runs its manual session, which may ask for the
  lease (an operator must be on duty for it to open). The supervisor does
  not clear enclosure faults by itself, so an engineer working on the
  controller is not surprised by a `ClearFaults`; the request API refuses
  new requests (503, `Retry-After`).
* **shadow** -- run robofast beside another control system (the legacy one)
  and compare: the sequencer peeks at its plan and logs what it would observe
  (`shadow_would` events: `observe a (expose, light)`, `idle (...)`), the
  supervisor logs when it would open or close (`shadow_would`, and
  `would_open` in its status). Nothing that moves is commanded: no lease,
  no generation, no startup park, no `OpenShutter`/`CloseShutter`, no
  automatic `ClearFaults`, no final close when the supervisor stops. The
  hardware heartbeat keeps being pinged (a ping moves nothing; stopping it
  would make the hardware close). An enclosure found open in shadow is a
  critical alarm, and robofast leaves it to whoever opened it -- **unless
  there is an active safety risk** (below; unknown weather is one), which
  it closes at once. The weather bridge's own `--shadow` flag (the bridge
  alone, serving nothing; docs/weather_bridge.md) is a different thing: to
  shadow with all of robofast, serve the bridge and put the enclosure in
  this mode.

### Shadow intervenes on an active safety risk

JDE (2026-11-02): "if there's an active safety risk, it should intervene
immediately." In shadow, when the enclosure is open (by anyone: the legacy
system, a person at the controller) and a condition that would close it in
the robotic mode is **active**, the supervisor closes it at once, exactly
as the robotic mode would (`park_before_close` included), raises a
critical alarm (`mode shadow intervened: closing the enclosure ... on
weather unsafe (...)`) and logs a `shadow_intervened` event; once it reads
closed it goes back to watching (`watching` with `after`). It never opens.
An active condition is:

* the weather verdict unsafe: the safety monitor's last successful
  `IsSafe` read said false (a read failure afterwards keeps it);
* the weather verdict **unknown** (JDE 2026-11-23, "yes, safety first"):
  the last weather read failed (the bridge unreachable, a timeout), the
  feed's data are older than `safety.stale_after`, or no read has succeeded
  for `stale_after`; the risk reads `weather unknown (...)`;
* an enclosure fault read from the fault channel (`safety.fault_action`).

**Startup grace.** A supervisor that has just started has not read the
weather yet. It gets `stale_after` (the same bound as a stale feed) from
its start to make its first successful read; inside that grace "no read
yet", and any failed reads, do not make shadow close, so a restart does
not close what another system has open merely because it has not read
anything yet. Still no successful read after the grace is unknown weather,
and shadow closes.

Not risks: an unreadable fault channel, an expired operator duty and the
reopen hold do **not** make shadow close.

**Only enforced conditions count.** The verdict is the weather bridge's
`IsSafe`, which only the sources and limits it *enforces* move: a sensor
still under validation, run log-only (e.g. a source with `enforce_rain:
false`, as every MINERVA rain sensor is today; docs/weather_bridge.md),
shows on the console and in the bridge's `would_close`, but leaves `IsSafe`
alone and so never makes shadow close.
Promote it to enforced once it has earned that.

The knob is the mode's `intervene` (true for the built-in `shadow`; a site
may set `"modes": {"shadow": {"intervene": false}}` to watch only). A mode
that commands its enclosure closes on these conditions anyway.

## The policy knobs

Every mode is a `ModePolicy`:

| Key | Values | Meaning |
|---|---|---|
| `open` | bool | the supervisor may grant a lease (and so open) at all |
| `observe` | `plan`, `manual`, `shadow` | what the sequencer asks of its plan (`robofast.modes.ModeGate`) |
| `enclosure` | `command`, `watch` | `watch`: the supervisor reads the enclosure and commands nothing; needs `open` false |
| `requests` | bool | the request API accepts submissions and ToOs |
| `attended_only` | bool | no lease unless an operator is on duty |
| `duty_expiry` | `close`, `alarm` | an expired duty closes after `duty_grace` (`close`), or only raises the critical alarm (`alarm`) |
| `duty_ttl`, `duty_grace` | seconds or null | null: the `safety` entry's |
| `intervene` | bool | a `watch` mode closes an open enclosure on an active safety risk, unknown weather included (above); default true for `shadow`, false otherwise |
| `fault_auto_clear` | bool or null | the supervisor's automatic fault clearing (docs/aqawan.md); null: the `safety` entry's; true needs `safety.fault_action` |
| `description` | text | shown on the console |

`observe: shadow` and `enclosure: watch` need `open: false`.

### Which checks are configurable

JDE asked that "the checks should be configurable". Per mode: the
operator-duty checks (`attended_only`, `duty_expiry`, `duty_ttl`,
`duty_grace`) and automatic fault clearing. Not per mode, enforced in every
mode that may open:

* the weather verdict (the safety monitor's `IsSafe`) and its staleness;
* enclosure faults (a fault blocks opening and closes an open enclosure);
* the shutter's health (an error or an unreadable shutter closes and holds);
* the lease and its renewal, and the park check before a lease;
* the hardware heartbeat.

Turning one of these into "log only" would let an enclosure stay open in
rain or with a fault, so they stay fixed; which weather limits apply, and
whether rain is enforced, is the weather bridge's config
(docs/weather_bridge.md: `enforce_rain`, groups, limits), which serves every
enclosure on the site and does not know their modes.

## Configuring them

In the enclosure config (the file the supervisor and the sequencer read):

```json
{
  "mode": "attended",
  "modes": {
    "attended": {"duty_ttl": 600},
    "engineering": null,
    "commissioning": {"like": "attended", "requests": false,
                      "description": "first nights: an operator watches, no outside requests"}
  },
  "mode_file": "modes/aqawan1.json"
}
```

* `mode` -- the default: the mode the supervisor starts in when its mode
  file is missing or unreadable. **Make it a mode the site is content to
  wake up in.** MINERVA uses `robotic` (its Aqawan has a hardware
  heartbeat that closes it if the supervisor dies); FLWO 60-inch and
  MEarth South use `attended`: it cannot open without an operator on duty,
  and an open enclosure with nobody on duty closes. (`shadow` is not a safe
  default: it never closes an open enclosure.) A config without `mode`
  keeps the behavior from before modes: `robotic`, or `attended` on an
  attended-only enclosure.
* `modes` -- an entry for a built-in name changes only the fields it gives;
  a new name starts from `like` (a built-in or an earlier entry) or gives
  every one of `open`, `observe`, `requests`, `attended_only`; `null`
  removes a built-in; keys starting with `_` are comments.
* `mode_file` -- where the active mode is kept (relative: under the data
  directory, `$ROBOFAST_DATA_DIR`); default `modes/<name>.json`.

Every error names its path (`aqawan1.json: modes.night.observe: one of
plan, manual, shadow`), and the supervisor refuses to start on one.

### Attended-only enclosures: a floor

An enclosure without a hardware heartbeat has nothing that closes it if the
supervisor dies (the FLWO 60-inch dome, MEarth South's roof), so its
`safety.attended_only` is true (the default without a heartbeat). No mode
lowers that: every mode that may open there needs `attended_only` true and
`duty_expiry` `close`. The built-in `robotic` is simply not offered there,
and a site definition that breaks the rule is refused at load.

### Unattended without a watchdog: the site's choice

Whether that risk is worth taking is the site's decision, not robofast's
(JDE 2026-11-02: "some may decide it's worth the dome not closing (with a
loud warning) to operate unattended. mearth operated robotically for years
with its dome"). A site opts in explicitly in the enclosure's `safety`:

```json
"safety": {"allow_unattended_without_watchdog": true}
```

* `attended_only` then defaults to false: the floor is lifted and every
  mode, `robotic` included, is offered. The site's default `mode` is still
  its own choice (MEarth South starts `attended` and is switched to
  `robotic`).
* Without the opt-in nothing changes: no heartbeat means attended only, and
  `attended_only: false` without a heartbeat is **refused** at load, naming
  the opt-in (before, it was accepted with one log line).
* With a heartbeat the opt-in changes nothing; with `attended_only: true`
  given as well, the floor stays.

While it is set, robofast says so, loudly and repeatedly:

| Where | What |
|---|---|
| the supervisor's log, at start | a critical line in an unattended mode (a warning otherwise), and an `unwatched` timeline event |
| every switch into a mode that may open with nobody on duty | a critical log line, an `unwatched` event, and `NO WATCHDOG: ...` appended to the switch notice (the console and the notifier carry it for an hour) |
| the log, while it lasts | an `unwatched` event and a critical line every `safety.unwatched_warning_interval` s (default 900) |
| the supervisor's `GET /status` | `mode.unwatched`: `{"active", "text"}` (null without the opt-in) |
| the observer console | a red, bordered banner on the enclosure panel and a warning in the alarm list for as long as it lasts (an amber note in an attended mode) |
| the sequencer's status snapshot, the request API's `GET /status` | `unwatched`, `supervisor.mode.unwatched` |
| the morning report | a bold warning above everything else, with every `unwatched` event (JSON `unwatched`) |

| Site | Default | Offered | Watchdog |
|---|---|---|---|
| MINERVA (`sites/minerva/aqawan1.json`) | `robotic` | robotic, attended, engineering, shadow | the Aqawan's heartbeat |
| FLWO 60-inch (`sites/flwo60/flwo60.json`) | `attended` | attended, engineering, shadow | none; no opt-in |
| MEarth South (`sites/mearth_south/mearth_south.json`) | `attended` | robotic, attended, engineering, shadow | none; **opted in** |

## Switching

From the observer console (an operator token; the enclosure panel's *mode:*
buttons, which ask for a reason and a confirmation; audited), or from a
shell on the control network:

```
python -m robofast.modes --config sites/minerva/aqawan1.json show   # or robofast-mode
python -m robofast.modes --config sites/minerva/aqawan1.json \
    set robotic --operator jde --reason "unattended tonight"
```

Both go to the supervisor (`POST /mode {"operator", "mode", "reason"}`),
the one place the mode lives. Every switch is

* logged (a `mode` timeline event at warning level, with who, when, why and
  the mode before) and raised as a notice on the console and through the
  notifier for an hour;
* saved to the mode file (a failed write is an alarm: a restart would start
  in the default);
* **never an opening**: a switch to a less safe mode (shadow -> robotic,
  attended -> robotic) takes effect through the normal lease path: the
  sequencer asks for a lease and the supervisor grants it only with every
  check (safe weather for the reopen hold, no fault, the telescopes parked,
  an operator on duty if the mode needs one);
* **at once when safer**: if the live lease may not go on under the new
  mode -- it may not open, its `observe` differs (robotic -> engineering:
  the generation was chosen under other rules), or its duty check fails
  now (robotic -> attended with nobody on duty) -- the lease is revoked, so
  the sequencer stops, aborts and parks, and the enclosure closes, as for
  any revocation. A switch into a `watch` mode (shadow) while open finishes
  that close before it stops commanding.

A switch between modes that do the same work and may both open
(attended -> robotic) keeps the lease: the enclosure stays open.

## Where the mode shows

* the supervisor's `GET /status` (`mode`: name, description, policy, who
  switched to it, when and why, the default, whether the default was used
  and why, the modes defined, `would_open` for a mode that may not open);
* the observer console's enclosure panel, and the switch notice;
* the request API's `GET /status` (`supervisor.mode`);
* the sequencer's status snapshot (`mode`, `observe`) and its timeline
  (`mode_seen`, `shadow_would`, `startup_held`);
* every FITS header (`OBSMODE`, from the value registry's `enclosure.mode`);
* the dispatch scheduler's plan record: a `mode` entry on each change, and
  every entry made after it carries the `mode` it was made in.

## How each part honors it

* **The supervisor** (`robofast/safety.py`): `open` and the duty checks
  in `open_allowed`, so `request_open` grants nothing a mode forbids;
  `enclosure: watch` in its enclosure loop; `fault_auto_clear` per mode.
* **The sequencer** (`robofast/sequencer.py`) asks its plan through a
  `ModeGate`, which reads the supervisor's mode (at most every 2 s; the
  in-process supervisor, or `RemoteSupervisor.mode_status`): `plan` passes
  through; `manual` answers Idle to every telescope not under manual control
  (only peeking, so the night still ends when the plan has nothing more);
  `shadow` peeks and logs. At start, in shadow (or while the mode cannot be
  read), it holds back its startup cleanup, which would park the
  telescopes. A dark frame already exposing in a closed generation
  finishes; the next is not started. Idle telescopes ask again every
  `sequencer.mode_idle` (10 s).
* **The console** forwards switches and refuses manual control in shadow.
* **The request API** refuses submissions while the mode's `requests` is
  false; a supervisor that does not answer refuses nothing (intake only
  queues).
