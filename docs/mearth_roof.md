# The MEarth roll-off roofs

MEarth ran two arrays of small telescopes, each under one roll-off roof:
MEarth North at FLWO (Mt. Hopkins) and MEarth South at CTIO (Cerro
Tololo). The two roofs have different controllers. robofast drives the
**South** roof (`robofast_core/devices/mearth_roof_*`, site config in
`sites/mearth_south/`); the North roof is described below for later.

What follows is adapted from the MEarth observing system (J. Irwin, used
with permission): its roof and weather daemon, its power-failure scripts,
its configuration and its operator guides.

## MEarth South (CTIO)

### Hardware

- One roll-off section on rails, moved by a **chain drive** (a bicycle
  chain, delicate when frozen) from a motor on a **variable frequency drive
  (VFD)**. The VFD ramps up and down at the ends of travel and has an
  **over-current trip** (a jam, a crash into the end stop, large
  earthquakes); after a trip it must be reset on site before the motor
  turns again.
- **Limit switches** at both ends stop the drive by themselves. A second set
  of contacts reports them to the computer; those contacts are powered from
  the weather station's 15 V supply. Without it the roof still moves and
  stops in the right places, but the computer cannot see where it is.
- A **local/computer switch** at the roof controls. Left on local (after
  day work), the computer's commands do nothing; legacy's commonest cause of
  a failed evening open.
- A **roof interface box** between the control PC's parallel port and the
  drive.

### Protocol (parallel port)

| | |
|---|---|
| Command | one byte on the data lines, latched by the box on a strobe pulse: write data, ~1 ms, assert STROBE (control bit 0 set, the line goes low), ~1 ms, release, ~1 ms |
| Bytes | `o` (0x6F) drive open, `c` (0x63) drive closed, 0x00 stop |
| Open limit | status register `BUSY` (0x80) **set** |
| Closed limit | status register `PAPEROUT` (0x20) **clear** |
| Both | a switch or wiring fault |
| Neither | part-way, or the 15 V sense supply is off |
| Watchdog | **none**: the box holds the last byte as long as it has power |

Legacy wrote the byte, then watched the limits every half second: stop on
reaching the target limit, or stop and flag STUCK after 180 s each way.

### Failure modes and what robofast does

| Failure | Legacy | robofast |
|---|---|---|
| Left on local control | 180 s timeout, STUCK | the move never leaves its starting limit: a fault after `departure_timeout` (30 s) |
| VFD tripped / chain broken or frozen / roof jammed | 180 s timeout, STUCK | same timeout (`open_timeout`, `close_timeout`, 180 s); a latched fault |
| Roof moved by hand | showed UNKNOWN; a later close timed out to STUCK | neither limit is `shutterError` and a fault: the supervisor closes |
| Both limits made | showed FAULT | `shutterError`, fault, open refused |
| 15 V sense supply off | position unknown | as moved by hand; a close then times out (the drive still stops at the end) |
| Control software dies | the latched command finishes; an open roof stays open | the same: hence **attended only** |
| UPS on battery | an open became a close; a close was held past its timeout | not in this server; see "Power failure" |
| STUCK cleared | by commanding the roof where it already was | `Action("ClearFaults", note)`, only with the roof on its closed limit, logged; through the supervisor (privileged) |

`ShutterStatus` is the position and the fault is a separate axis (as for
the Aqawan, `docs/aqawan.md`): a roof that failed to open but sits on its
closed limit reads `shutterClosed` with a fault, so the supervisor alarms
and does not re-close; a fault anywhere else reads `shutterError` and the
supervisor closes. The fault channel is `Action("Faults")`; there is no
automatic clearing (legacy never cleared STUCK by itself).

### Why attended only

The design (`docs/design.md` section 3.3) lets an enclosure run unattended
only behind a hardware heartbeat that closes it when the software stops.
This roof has none (legacy had a spare "watchdog" bit, never wired, and a
"ping" routine marked not implemented). So `sites/mearth_south/mearth_south.json`
is `attended_only`: no lease, and no opening, unless an operator is on
duty. A watchdog in the interface box (close if no strobe for N seconds)
would lift this; it would need hardware work on site.

### Power failure

The legacy South UPS script set a power-fail flag (refusing opens, turning
an open into a close, holding a close latched past its timeout) and locked
the roof closed; the North one also ran an endless close loop. In robofast
the supervisor is the only one that opens or closes, so a UPS-on-battery
signal should reach it as an unsafe condition (a weather-bridge source, or
a SafetyMonitor of its own), not this server. Not built yet. Whether the
South drive and interface box are on a UPS is not known.

### Weather (legacy South limits)

From the South configuration; ported to `sites/mearth_south/weather.yaml`
where the weather bridge can express them. Close at the trigger, reopen
below the reset, and legacy held each alert 20 minutes after it cleared
(`reopen_hold` 1200 s here):

| Quantity | Close | Reopen |
|---|---|---|
| Temperature | < -15 C or > 35 C | same |
| Ambient minus dew point | < 3 C | same (not expressible yet) |
| Humidity | > 80 % (winter; summer up to 85 %) | < 75 % |
| Wind (mean) | > 35 km/h | < 30 km/h |
| Wind (gust) | > 40 km/h | < 35 km/h |
| Rain intensity (WXT520) | > 0.55 mm/h (the sensor reads up to ~0.2 in wind) | ~0 |
| Hail intensity | > 0 | 0 |
| Cloud sensor rain flag | any | |
| Sky minus ambient | > -31 C (plus a humidity correction) | < -34 C |
| Sun | above -1 deg (east) / +5 deg (west) | |
| No weather data | 60 s | |
| No cloud data | 30 s (then the sensor was power cycled) | |

The site's station (a Vaisala WXT520) and cloud sensor were read over
serial ports by the roof daemon itself. robofast has no reader for either
yet; the weather config's station source is a required placeholder, so the
bridge is unsafe until one exists.

### Confirm on site before `allow_hardware`

The server refuses a real port until the roof config says
`"allow_hardware": true` and `--hardware` is passed. Before that:

1. Which computer the interface box is cabled to now, and its parallel
   port device. robofast must run there (or the box moves to a PC with a
   port).
2. The command bytes and strobe: `o`, `c`, 0x00, latched on the strobe;
   which edge latches; what the box does with any other byte; whether it
   latches garbage when the PC boots or the port is reset (and so whether
   the roof can move at boot).
3. The limit bits and polarity (`BUSY` set = open, `PAPEROUT` clear =
   closed) and what an unpowered sense supply reads.
4. Full-travel time each way, and how long the roof takes to leave a limit
   (to set `departure_timeout`, now 30 s, and the move timeouts, now 180 s).
5. Whether the VFD stops on the limit switches independently of the box
   (legacy guide: yes).
6. How local/computer mode can be read, if at all (it cannot through the
   port as legacy used it).
7. Whether the drive and the box are on a UPS, and what happens to the
   latch when the box loses power.
8. Telescope park positions that clear the closing roof (legacy parked at
   altitude 10, azimuth 190), and whether a roof can close over a mount
   that failed to park.
9. Whether a hardware watchdog can be added to the box (the only way to
   run unattended).

## MEarth North (FLWO), for later

Not driven by robofast yet.

- Two sections: a **main** roof section on three-phase mains (**not on a
  UPS**; it can only move once the generator is up) and an **end** section
  (on batteries). Close order matters for the seal: main first, then end.
- **No position feedback at all**: no limit switches reach the computer.
  The software's state is what it last commanded; the operator guide tells
  observers to trust only the webcams.
- Interface: originally four bits on a parallel port's data lines; later a
  serial relay board (9600 baud) taking the three bytes `254, 40, bits`.
  Bits: 0 main open, 1 end close, 2 main close, 3 end open; bit 4 was a
  watchdog output, never connected.
- Sequencing by time only: open = main open for 40 s (45 s before a safety
  change), then end open for 30 s, then all off; close = main close for
  60 s, then end close for 45 s, then all off.
- Power failure: if the main section was moving, the move was held (its
  timer restarted) until power returned; a close also drove the end. The
  UPS script ran an endless loop holding the close bits; it still named
  the old parallel port after the move to the relay board.
- Manual close: a grey box at the roof with a manual/computer toggle and
  buttons, main first then end.

A North driver would be the South server's structure with a timed
sequencer in place of the limit monitor, an open-loop `ShutterStatus` (with
a forced "is closed / is open" operator override), and the same attended
only policy. The cheapest real improvement would be limit switches wired
to the relay board's inputs, if it has any.
