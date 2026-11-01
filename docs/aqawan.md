# The Aqawan enclosure: confirmed facts and policy

The Aqawan's Alpaca Dome server (`robofast_core/devices/aqawan_server.py`)
drives the enclosure's PAC controller. The wire protocol, and everything
about it that is still a guess, is described in the module docs of
`aqawan_pac.py` and `aqawan_sim.py`. This page records what the MINERVA team
has confirmed (JDE, 2026-10-31) and the policy built on it.

## Confirmed

- **Heartbeat timeout: 60 s.** The PAC closes on its own when the pings stop
  for 60 s, and refuses to open until they resume. The simulator defaults to
  60 s. The supervisor must ping at least four times per timeout (legacy:
  "ensure 4 heartbeats before timeout"), so `SafetyConfig` rejects a
  `heartbeat_interval` above `heartbeat_timeout / 4` and requires
  `heartbeat_timeout` whenever `heartbeat_interval` is set.
  `sites/minerva/aqawan1.json` pings every 10 s against 60 s.
- **Re-sending commands.** The legacy driver re-sent a command that got no
  reply "because it was necessary sometimes and didn't seem to hurt". The
  new client re-sends at most `max_attempts` times (3), only while nothing
  has come back, on the same connection, within one deadline. See the next
  section for why that is safe.
- **Doors do not matter for observing.** `interlock_doors` defaults to none;
  the option stays for a site that wants it.
- **Fault is a separate axis from shutter position.** "Closed and fault" is
  a different state from "open and fault", and only the second is dangerous.

## Why re-sends are safe

Every command the server sends names an absolute target (`OPEN_SHUTTER_n`,
`CLOSE_SEQUENTIAL`, `CLOSE_SHUTTERS`, `STOP`, `LIGHTS_OFF`), is a query
(`STATUS`, `GET_FAULTS`, `GET_ERRORS`), or restarts a timer (`HEARTBEAT`).
A duplicate asks for what is already happening. That the PAC ignores such a
duplicate is assumed (not yet seen on hardware); the simulator does, and a
test checks that a duplicated open does not restart the shutter's travel.

The one hazard: a slow PAC could execute a duplicate `OPEN_SHUTTER_n` after
a later close. That is no worse than any other unexpected motion. Once the
PAC has acknowledged the close (reports a shutter `CLOSING`), the server
reports the PAC's own state again, so the reopening shows up as opening and
the supervisor re-closes on its next poll. `RESET_PAC`, which is not
idempotent, is never re-sent (and the server never sends it).

## Faults

- `ShutterStatus` is the position: closed and faulted reads `shutterClosed`.
  A Fault or Error with a shutter open or part-way open reads `shutterError`.
  A shutter that is closing still reads `shutterClosing`; the supervisor
  re-commands a close that takes longer than its `shutter_timeout`.
- `Action("Faults")` is the read-only fault channel: JSON with `fault`,
  `error`, the PAC's fault and error text, and both shutter strings. The
  supervisor polls it each pass when `fault_action` is `"Faults"`. Any fault
  (or an unreadable channel) alarms once per change, revokes the lease and
  blocks opening. If the enclosure is not closed, a second, louder alarm is
  raised and the supervisor closes it. A fault while closed only alarms: no
  close is re-sent.
- `Action("ClearFaults", note)` sends `CLEAR_FAULTS` then `CLEAR_ERRORS`, is
  logged with the note, and is refused unless both shutters report
  `CLOSED`. It is privileged at the dome owner (`"Action:ClearFaults"` in
  `ENCLOSURE_MEMBERS`), so a sequencer cannot call it. An operator clears
  faults through the supervisor: `POST /faults/clear {"operator": ...,
  "reason": ...}` (the console's `clear_faults`).
- The fault channel also says `estop: true` when the PAC's fault or error
  text names an emergency stop (`Estop active`, `E-STOP`, `emergency stop`).

## Automatic fault clearing

JDE, 2026-10-31: "let's start there". The supervisor owns the policy, since
`ClearFaults` is privileged (`SafetyConfig.fault_auto_clear`, on in
`sites/minerva/aqawan1.json`):

| Rule | How |
|---|---|
| Only with both shutters confirmed closed | the fault channel's `shutters` are both `CLOSED` *and* `ShutterStatus` reads closed; the server refuses otherwise too. A fault with the enclosure open closes it first (above), and the clear comes after |
| Never an E-stop | `estop`, or fault text naming one: a **critical** alarm, no clear; an operator resets the E-stop and clears faults. The server also refuses an automatic clear (note `auto: ...`) of an E-stop |
| Never an emergency close latch | an operator's own close (`docs/console.md`); only an operator releases it |
| Not before the fault text has arrived | so an E-stop can be recognised |
| At most once per hour | `fault_auto_clear_interval`, 3600 s; every automatic clear raises an alarm (`automatic fault clear #n ...`) and a `fault_auto_clear` timeline event |
| A recurrence needs an operator | the fault is back within the interval of an automatic clear (or still there `fault_auto_clear_settle`, 15 s, after it): automatic clearing **stops**, with a critical alarm, until an operator clears faults through the console |

The supervisor's `GET /status` lists the conditions that need an operator
now under `critical` (the console raises them as critical alarms) and the
policy's state under `fault_auto_clear` (`clears`, `last_ago`,
`stopped`).

## After STOP or an E-stop mid-travel

What the PAC reports for a shutter stopped part-way is not known yet (the
simulator says `STOPPED`). The server does not depend on it: any shutter
string it does not recognise is `shutterError`, and the raw string is logged
(once per distinct value) so the real one can be learned. After `AbortSlew`
the server re-reads `STATUS` and logs what the PAC reports.
