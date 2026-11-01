# KiwiSpec: the legacy protocol and the robofast service

KiwiSpec, MINERVA's fiber-fed spectrograph, is controlled from a Windows PC
next to it ("the spectrograph PC"). robofast reaches it the way legacy
MINERVA did, through two TCP services on that PC, and adds one owning
service on the Linux side (design.md 3.1: "one owning service, a small typed
HTTP API, a simulator"):

| robofast module | What it is |
|---|---|
| `robofast_core/devices/kiwispec_client.py` | async clients for both legacy services: allowlisted commands, hard timeouts, typed replies, bounded retries |
| `robofast_core/devices/kiwispec_service.py` | the single owner: HTTP/JSON API, one job at a time, exposure-meter termination and the flux-weighted midpoint, the hardware-safety gate, and `KiwiSpecRemote` (the abort hook for the sequencer's cleanup) |
| `robofast_core/devices/kiwispec_sim.py` | a TCP simulator of the spectrograph PC (both protocols, legacy quirks included), with fault injection |
| `tests/core/test_kiwispec.py` | the fast-tier tests, all against the simulator on loopback |
| `deploy/kiwispec.example.json` | an example config (loopback) |

Everything below about the legacy system comes from its code
(MinervaCollaboration/minerva-control, `minerva_library/spectrograph.py`
(the Linux-side client), `spectrograph_server.py` (the server on the
spectrograph PC), `si/` (the camera protocol), `control.py` and
`rv_control.py` (how a spectrum is taken)). None of it has been checked
against the running hardware; the open questions are at the end.

## 1. The two services on the spectrograph PC

### 1.1 `spectrograph_server.py` (ASCII, port 44445)

`SERVER_PORT` in `config/spectrograph.ini`; the server binds `HOST`/`PORT`
from `config/spectrograph_server.ini`.

**Framing.** One command per TCP connection. The client connects (legacy
timeout 3 s), sends `"<command> <argument>"` in one `sendall` (5 s), and
reads one `recv(1024)` with a per-command timeout; the server replies and
closes. Commands without an argument send the literal `None`. The server:

- is **single-threaded**: `run_server` accepts, handles and answers one
  connection at a time, with a listen backlog of 1. A slow command (homing,
  the stage power cycle) blocks every other command, including the exposure
  meter's total.
- reads each connection once (`recv(1024)`, 3 s timeout). If the read times
  out or is empty, it **closes its listening socket and calls `run_server()`
  again, recursively** (a brief "connection refused", and after ~1000 such
  events, Python's recursion limit would kill it).
- parses `repr()` of the bytes, `.strip("'")`, then `split(None, 1)`. So a
  command must have exactly two tokens (else `fail`); an argument containing
  `'` makes `repr` use double quotes and mangles the command token; a
  backslash or non-printable byte is escaped; and the argument's **leading
  whitespace is dropped** (this matters for header chunks, section 3).
- answers `success`, `success <fields...>`, or `fail` (no newline). An empty
  reply is how the client sees a server that closed without answering.
- has **no exception handling around dispatch**. A command routed to a
  method that does not exist, or a handler that raises, kills the listener
  (the process lives on: its pressure-logging threads keep running, so it
  looks alive) until someone restarts it.

**Commands the server dispatches** (`process_command`), with the legacy
client's reply timeout:

| Command | Argument | Reply | Legacy timeout | Notes |
|---|---|---|---|---|
| `cell_heater_on` / `cell_heater_off` | `None` | `success` | 10 s | iodine cell heater (serial, `I2Heater`); reads status first because the heater's `ens` is a **toggle** |
| `cell_heater_temp` | `None` | `success <T> C` | 10 s | |
| `cell_heater_set_temp` | `<T>` | `success <T> C` | 10 s | heater `tset=` |
| `cell_heater_get_set_temp` | `None` | `success <T> C` | 10 s | |
| `get_spec_pressure` | `None` | `success <mbar>` | 5 s | gauge controller `RDCG2` |
| `get_pump_pressure` | `None` | `success <mbar>` | 5 s | gauge controller `RDCG1` |
| `i2stage_connect` | `None` | `success`/`fail` | 30 s | **power-cycles the stage** on the bench PDU (10 s off, 10 s on), connects (Thorlabs APT), homes, moves `out` then `in`. Takes longer than the legacy client's 30 s |
| `i2stage_disconnect` | `None` | `success` | 10 s | APT clean-up (not doing it can crash python.exe in the APT DLL) |
| `i2stage_home` | `None` | `success`/`fail` | 10 s | moves to 0 then homes; **blocks** for the whole move. Legacy re-sent it for up to 60 s |
| `i2stage_get_pos` | `None` | `success <mm> <name>` | 10 s | `<name>` is the configured position equal to `round(mm, 1)`, else `unknown` |
| `i2stage_move` | `in`/`flat`/`out` | `success`/`fail` | 10 s | **starts** the move on a thread and answers at once; arrival must be polled |
| `i2stage_movef` | `<mm>` | `success`/`fail` | 10 s | 0-150 mm, else `fail`; a non-number raises (kills the listener) |
| `led_turn_on` / `led_turn_off` | `None` | `success` | 10 s | slit-flat LED on a PDU outlet; also logs the lamp's on-time (`FLATFILE`) |
| `time_tracker_check` | `<logfile>` | `success <seconds>` | 10 s | seconds since the lamp was last turned on; **raises (kills the listener) when the lamp is off or its log is missing** |
| `backlight_on` | `None` | `success` | 10 s | Dynamixel servo swings the backlight over the fiber (-45°), LED on (`n8`); a server thread turns it off after 60 s |
| `backlight_off` | `None` | `success` | 10 s | LED off (`f8`), servo to -90°, torque off |
| `get_expmeter_total` | `None` | `success <counts>`/`fail` | 10 s | the running total kept by the exposure-meter thread; `fail` if that thread never ran |
| `reset_expmeter_total` | `None` | `success` | 10 s | |
| `stop_log_expmeter` | `None` | `success`/`fail` | 30 s | pause the meter (for the backlight): waits for its high voltage to go off (forever, if the meter thread is stuck); `fail` if the thread never ran |
| `start_log_expmeter` | `None` | `success` | 10 s | resume |
| `get_index` | `None` | `success <n>` | 5 s | number of `*.fits` in tonight's data directory + 1 |
| `set_file_name` | `<name>` | `success` | 30 s | one token; stored as `<data path>\<night>\<name>` (in memory only: a restart forgets it) |
| `save_image` | `None` | `success`/`fail` | 30 s | `shutil.move("C:/IMAGES/I", file_name)`: the camera's last frame into place |
| `write_header` | `<text>` | `success` | 3 s | appends to a header buffer |
| `write_header_done` | `<text>` | `success`/`fail` | 10 s | appends, parses the buffer as JSON `{key: value or [value, comment]}`, **updates the raw FITS file in place**, and converts the SI `PARAMnn` keys (also: `DATE-OBS` + 7 h, local to UTC) |
| `start_si_image` / `kill_si_image` | `None` | `success` | 10 s | start or `taskkill` the SI Image camera program (killing it stops the CCD cooler for ~15 s) |

**Dispatched but not defined** (each raises `AttributeError` and kills the
listener): `thar_turn_on`, `thar_turn_off`, `flat_turn_on`, `flat_turn_off`,
`expose`, `set_camera_param`, `set_data_path`, `get_status`,
`get_fits_header`, `get_filter_name`, `set_binning`, `set_size`. The legacy
client still has methods that send several of them (`thar_turn_on`,
`get_status`, `set_binning`, ...). **Not dispatched** (always `fail`):
`safe_close`, `backlight_power_cycle` (also sent without an argument),
`fiber_*` (MRED only), `update_dynapower*` (commented out).

**Background threads on the server.** Pressure logging every 0.5 s (and, on
a pump failure with the pump valve open, the server **closes the pump valve
itself**); log-path rollover. The exposure-meter thread (`logexpmeter`) and
the chiller logging are **disabled** in `__main__` ("*** EXPMETER disabled
***"), so in the code as it stands `get_expmeter_total` answers `fail`.
When it runs, `logexpmeter` powers the meter's PDU outlet, sets one reading
per second (`P`), turns on the PMT high voltage (`V`), sets the shutter
trigger (`O`), and adds each 4-byte reading to the total; a reading above
`MAXSAFECOUNT` (3e7) shuts the meter down for good (until a restart).

### 1.2 The SI Image camera server (binary, port 2055)

`CAMERA_PORT` in `config/spectrograph.ini`. This is the vendor's "SI Image
SGL E" program (`C:/Program Files/SI Image SGL Rev E/`), not MINERVA code.
Legacy speaks to it with the `si/` package (`SIClient`, `Imager`). Packets
are big-endian:

| Packet | Layout | Notes |
|---|---|---|
| header | `>IBB` length, id, camera id (1) | |
| command (id 128) | `>IBBHH` + params: length, 128, 1, function, param length | |
| ack (id 129) | `>IBBH` + accept | sent for every command except `TerminateAcquisition` |
| data (id 131) | `>IBBiHH` + payload: error code, data type, byte length | type 2007 "Done" carries `>H` function number; 2002 status text; 2006 image header; 2009 camera parameters |
| image (id 132) | `>IBBiHHHHHHII` + pixels | only for `RetrieveImage`, unused |

Functions legacy uses: `SetAcquisitionMode` 1034 (`>B` 0 = single frame),
`SetAcquisitionType` 1036 (`>H` 1, `>B` 0 light / 1 dark), `SetExposureTime`
1035 (`>I` ms), `Acquire` 1037 (`>HHHBB` 4, 1, 0, 73, 0), 
`TerminateAcquisition` 1018, `SetReadoutMode` 1042 (`>B`; legacy 3),
`SetCCDFormatParameters` 1043 (`>6i`; legacy 50, 2090, 1, 0, 2052, 1),
`SetCooler` 1046 (`>B`), `GetStatusFromCamera` 1011 (status text; never
parsed by legacy). The client waits for the ack, then for a Done (or the
expected data packet). `Acquire`'s Done comes after the exposure **and the
readout**; the frame is then in `C:/IMAGES/I` on the PC, and the
spectrograph server's `save_image` moves it into place.

**How legacy took a spectrum** (`control.takeSpectrum` ->
`spectrograph.take_image` -> `expose_with_timeout` -> `expose`): take
`spectrograph_lock`; `spec_equipment_check` (backlight off; for science:
heater set point and on, iodine stage in/out, wait up to 10 s for the cell
temperature; ThAr: stage `out`, LED off; slit flat: stage `flat`, LED on;
fiber flat: stage per `i2`; bias/dark: stage `in`, LED off; each stage move
polled for 60 s, re-sent, then `i2stage_disconnect` + `i2stage_connect` --
a power cycle -- up to twice); `get_index`; `set_file_name`; a thread runs
`expose` (SI: mode 0, type, time, `Acquire`, wait for Done) with a timeout
of exptime + 120 s; `save_image`; build the header from every subsystem;
`write_header` in 800-character chunks and `write_header_done`; then
`fix_fits` on Linux, in a thread. The telescopes feeding the fiber
(`tele_list`) are used for acquisition, guiding and
`record_observation`; the header lists all telescopes. Readout: 22 s
(`specCalib`'s `ro_time`; 21.6 s in `spec_calib_time`).

## 2. Hardware actions

Every action the protocols can cause, and whether it moves or powers
something. robofast's client sends only the allowlisted ones
(`kiwispec_client.COMMANDS`); the rest are refused before anything is sent
(`FORBIDDEN`).

| Action | Moves | Powers | robofast |
|---|---|---|---|
| iodine stage to a named position / to mm (`i2stage_move`, `i2stage_movef`) | **yes** (Thorlabs stage, up to 150 mm) | | allowed; arrival polled (stopped -- two consecutive readings agree -- and at the position the live server names as the one asked for, or within `stage_tolerance` of a millimetre target), bounded by `stage_timeout` |
| home the iodine stage (`i2stage_home`) | **yes** | | allowed (client only; the service does not home on its own) |
| reconnect the stage (`i2stage_connect`) | **yes** (home, out, in) | **yes** (PDU outlet off/on) | allowed in the client; **the service never sends it** (legacy did, automatically, after a stuck move) |
| `i2stage_disconnect` | | | allowed (client only) |
| backlight on/off | **yes** (Dynamixel servo over the fiber input) | LED | allowed; the service pauses the exposure meter first (the backlight would saturate it) |
| slit-flat LED on/off | | **yes** (PDU outlet) | allowed |
| iodine cell heater on/off, set point | | **yes** (heater; a toggle) | allowed |
| exposure meter pause/resume | | **yes** (PMT high voltage, PDU outlet) | allowed |
| SI: exposure (`Acquire`), `TerminateAcquisition` | **yes** (camera shutter) | | allowed |
| SI: cooler on/off, readout mode, CCD format | | cooler | allowed (cooler only ever turned **on**) |
| `kill_si_image` / `start_si_image` | | **stops/starts the CCD cooler** | forbidden (operator) |
| ThAr / flat lamp on/off | | | not possible: the handlers do not exist (forbidden: they kill the listener) |
| vacuum: pump, pump valve, vent valve | **valves** | **pump** | not on this protocol (legacy `spectrograph.py` switches them on the bench PDU directly: `vent`, `pump`, `hold`); the server closes the pump valve by itself on a suspected pump failure. Not in robofast |
| `save_image`, `write_header*` | | | allowed; edit files on the PC |
| reads (temperatures, pressures, stage position, meter total, index, camera status) | | | allowed; the only commands retried |

## 3. The robofast side

### Client (`kiwispec_client.py`)

- `SpecServerClient`: one command at a time (an `asyncio.Lock`, always
  released by `async with`), each on its own connection; every attempt is
  bounded by connect + send + reply timeouts, and the whole call by an
  overall timeout. Replies are parsed into types (`StagePosition`, floats);
  `fail` raises `KiwiSpecFailed`, noise raises `KiwiSpecProtocolError`, no
  connection `KiwiSpecUnreachable` (nothing was sent). Only read-only
  commands are retried (`max_attempts`, a loop, no recursion); `fail` is
  never retried. Arguments are validated (stage range, names, and text the
  server's `repr()` would mangle). Headers are sent in chunks of at most 800
  characters that never begin with whitespace.
- `SICameraClient`: one persistent connection with a reader task that
  routes acks, Done packets (by function number) and data packets; every
  command is bounded; `TerminateAcquisition` goes on the same connection
  while an `Acquire` is outstanding; a dropped connection fails everything
  waiting at once.
- Both refuse a non-loopback address unless constructed with
  `allow_hardware=True`.

### Service (`kiwispec_service.py`)

The API is in the module doc. In short: `GET /status`, `POST /configure`,
`POST /expose` (science, thar, slitflat, fiberflat, dark, bias), `POST
/calibrate` (a list of frames as one job), `GET /jobs/{id}?wait=S`, `POST
/abort`, `POST /stop`, `POST /backlight`. One job at a time (a claim taken
with no await between the check and the take, released in `finally`;
waiting requests queue, see "Sharing the spectrograph"); every step has a
bound and every frame an overall bound. It differs from legacy on purpose:

- **Biases and darks are dark frames.** Legacy computed the exposure type but
  never passed it (`expose_with_timeout(exptime, expmeter)` leaves `exptype`
  at 1), so its biases and darks were light exposures.
- **Exposure times are not truncated.** Legacy did `int(float(exptime))`.
- **No automatic power cycle** of the iodine stage, no `kill_si_image`, no
  recursion, no `sys.exit()` in a thread, no lock held across an error, no
  32-minute lock wait (`recover_server`'s `si_lock.acquire(1920)` -- which,
  in Python 2, is a blocking acquire with no timeout at all).
- **Raw files are never rewritten on Linux.** Legacy's `fix_fits` rewrites
  the raw spectrum in place (`clobber=True`); robofast writes its keywords
  through the server's `write_header` only (the server's own in-place update
  is legacy behaviour that robofast cannot change).
- **The file name is sent again just before `save_image`**: a server that
  restarted mid-exposure has forgotten it.
- **A warm CCD refuses the exposure** (outside `ccd_temp_min..ccd_temp_max`,
  default -95..-85 C).
- **Backlight:** the exposure meter is paused before the backlight goes on
  and resumed after (legacy had this commented out).
- **Telescopes feeding the fiber** are part of a science request
  (`telescopes`), checked against the config, and written to the header
  (`FIBFEED`) and the result; calibrations take none.

**Lamps.** KiwiSpec has none to switch: "There are no ThAr lamps or a way
to robotically switch over. It's all iodine." (JDE, 2026-10-31). The
service keeps the generic `thar` and `fiberflat` kinds (they set up the
stage as legacy did and expose; no lamp is controlled), but only for a
spectrograph that lists them in `lamp_frames`. MINERVA's config lists
none, so the service refuses them (400), and `GET /status` reports the
kinds it accepts (`frame_kinds`). The slit-flat LED is the only lamp the
protocol can switch; `slit_flat_led` (default false) gates slit flats and
`/configure`'s `led: true`. See "Calibration" below.

### The iodine stage's positions: the live server is authoritative

The two legacy files disagree on where the named positions are: the
client's `spectrograph.ini` says in 145, flat 88, out 55 mm; the server's
`spectrograph_server.ini` says in 11, flat 68, out 102. JDE (2026-10-31):
the stage's home sensor once died, and the motor and sensor were swapped
at the wire level, which flipped the positions; the **live** values on the
spectrograph PC are correct. So the service hard-codes neither:

- It moves the stage **by name** (`i2stage_move in`): the server resolves
  the name with its own live ini.
- It decides arrival from the server's own reply: the stage has stopped
  (two consecutive readings agree) and `i2stage_get_pos` names the
  position asked for. It records the millimetres the server reports for
  each name (`/status` `stage_positions_live`).
- `stage_positions` in the service config is optional (default: none) and
  only a cross-check. If a configured value disagrees with the live server
  by more than `stage_tolerance`, `/status` raises an alarm saying the
  server is authoritative. Set it only with values confirmed on the
  spectrograph PC, before any hardware use (`sites/minerva/README.md`).

### Sharing the spectrograph between enclosures

KiwiSpec is fed by telescopes in both MINERVA enclosures, so two
sequencers (one per enclosure) may want it at once. The service is the one
owner of the spectrograph, and it arbitrates:

- **One claim at a time.** A job, a `configure` or a `backlight` holds the
  spectrograph until it ends (released in `finally`, also on abort,
  failure or cancellation).
- **A bounded, first-come first-served queue.** `expose`, `calibrate`,
  `configure` and `backlight` take `"wait": S` and `"client": name`. While
  the spectrograph is held, the request waits up to `S` s (capped by
  `max_queue_wait`, default 1800 s) in a FIFO queue of at most `max_queue`
  (default 4) requests; when the holder releases it, the first live waiter
  gets it at once. A request without `wait` is answered at once, and
  nobody jumps the queue (a request without `wait` is refused while others
  wait). A waiter whose connection goes away does not keep the
  spectrograph. `/status` lists the queue (`what`, `client`, `waiting`);
  job views carry their `client`.
- **Busy is not an error.** When the wait runs out (or the queue is full)
  the answer is 409 with `"busy": true`, and `KiwiSpecRemote` raises
  `KiwiSpecBusy`. The sequencer reports the spectrum `interrupted` (the
  dispatch scheduler re-queues it) and counts it neither as the
  telescope's error nor as a spectrograph fault (`docs/rv_night.md`).
  Other refusals (a warm CCD) and failures are spectrograph faults.
- **The sequencer's wait.** Each sequencer's `KiwiSpecRemote` sends its
  name as `client` and waits `spectrograph.queue_wait` (default
  `sequencer.spectrograph_queue_wait`, 600 s: a long RV exposure on the
  other enclosure, plus readout) while its telescopes stay on target and
  guiding.
- **Policy.** First come, first served: no enclosure has priority, and a
  running exposure is never preempted by the other enclosure (only its own
  sequencer, an operator or a cleanup aborts it). A ToO on one enclosure
  therefore waits for the other's spectrum in progress, bounded as above.
  `/abort` aborts whatever job is running, so each sequencer aborts only
  the job it started, and at startup only a job fed by its own telescopes.

Not done: the fiber backlight (`POST /backlight` from the guider) does not
yet send `wait` -- while the other enclosure exposes it is refused at once
and the guider guides on the configured fiber position (as legacy did);
and the two enclosures' dispatch schedulers do not see each other's
spectra in advance (each finds out from the queue).

### Exposure-meter termination and the flux-weighted midpoint

During a light frame that uses the meter (science), the service resets the
total, then polls `get_expmeter_total` every `expmeter_poll` s. If the total
reaches `target_counts` it sends `TerminateAcquisition` (the frame is still
read out and kept; `ended_by: "counts"`); otherwise the exposure runs for
`exptime`. After the shutter closes it waits one meter period and reads once
more, so the counts up to the close have reached the total.

The meter's total is a step function that lags the light by up to one
reading period `P` (half on average), so each sample time is moved back by
`P/2`. Counts between consecutive samples are taken to arrive uniformly
between them, and only those inside the exposure `[0, T]` count:

    t_mid = Σ_i Δc_i · (a_i + b_i)/2  /  Σ_i Δc_i,   [a_i, b_i] = [t_{i-1}, t_i] ∩ [0, T]

(`flux_weighted_midpoint`; exact for a rate constant between samples, and
tested against analytic results for constant, linear and exponential
rates.) Expected accuracy: a fraction of `P` + `expmeter_poll` (legacy: 1 s
each). The result gives the midpoint in seconds after the start, UTC and JD
(header `FLUXMID`, which legacy always wrote as `UNKNOWN`), and the counts
(`EXPFLUX`). The start time is the moment `Acquire` is acknowledged; the SI
header's own `TIME` keyword is more exact (question 6). If the meter does not
answer (`fail`, as with the legacy thread disabled), or its total goes
backwards (a server restart), the frame is time-terminated with a warning and
no midpoint.

### Faults the simulator injects, and what the service does

| Fault | Result (tested) |
|---|---|
| server hangs (reads, never answers) | each call fails within its bound; the job fails; the spectrograph is free again |
| garbled reply | reads retried, then reported in `/status`; moves never retried |
| undefined command / lamp-off `time_tracker_check` | the simulator's listener dies, as the real one does; robofast never sends these |
| CCD temperature out of range | the exposure is refused before `Acquire`; `/status` alarms |
| iodine stage stuck | the job fails after `stage_timeout`; nothing is exposed |
| spectrograph server restart mid-exposure | the camera finishes the frame; it is saved once the server is back (within `save_timeout`); the meter is reported lost |
| camera restart mid-exposure | the job fails at once; the next exposure reconnects |
| camera hung during an abort | `/abort` returns after `abort_timeout` with `ended: false`; the job is cancelled, the camera connection dropped, the claim released |

### Night names and times

The raw file is named `n<YYYYMMDD>.<object>.<NNNN>.fits`. The sequencer
sends the night (`"night"` in `expose` and `calibrate`), from its clock, so
the raw file, its sidecar's directory and every other night name agree
(`robofast_core/clock.py`, `docs/rv_night.md`). Without it the service uses
the legacy label, `n` + the UTC date. Exposure start times come from the
service's host clock (NTP-disciplined; `docs/deploy.md`); a simulation may
give it a `clock` (`{"file": "clock.json"}`).

## 4. Calibration: iodine only

KiwiSpec measures radial velocities against the iodine cell's absorption
lines, imprinted on the star light: the cell is the wavelength reference,
and the instrumental profile is modelled from the same exposure. There is
no ThAr lamp, no fiber-flat lamp and no way to switch the fiber over to
one (JDE, 2026-10-31). robofast's calibration plan for MINERVA follows the
legacy practice (`minerva-control`: `control.specCalib`, `takeSlitFlat`,
`spec_equipment_check`, `rv_control.doSpectra` and `mkschedule`,
`scheduler.py`'s B-star weighting, `targetlist.py`'s `bstar.csv`):

| What | When | How (robofast) | Legacy |
|---|---|---|---|
| **Biases** | each calibration run (evening and morning), roof closed | `sites/minerva/calibration.example.json` `spectrograph`: `{"kind": "bias", "n": 11}`; true dark frames, stage `in` | `specCalib(nbias=11)` (taken as light frames: section 3) |
| **Darks** | same | `{"kind": "dark", "exptime": 300, "n": 11}` | `specCalib(ndark=11, darkexptime=300)` |
| **B stars through iodine** | at night, a scheduled request, once a night by default | `SPECTRUM` requests with `extra_params.iodine: true` and `extra_params.cadence` (`max_per_night: 1`; raise it, or add `min_interval`, for more), `BSTAR-*` in `sites/minerva/requests.example.json` | a rapidly rotating B star (nearly featureless) observed with the cell in, one per night: `mkschedule` put one at the start of the night, the scheduler weighted B stars up as the night ran out ("weight for 1 Bstar per night") and down once one was observed |
| **Templates** | at night, one-shot requests, good conditions | `SPECTRUM` with `extra_params.iodine: false` (the cell out), no cadence: complete once taken (`TEMPLATE-*`) | targets observed with `i2` false (`template` targets in the schedule) |
| **Slit flats** | only if the slit-flat LED works | `slit_flat_led: true` in the KiwiSpec config, then `{"kind": "slitflat", "exptime": 1, "n": 11}` in the calibration plan. **Off by default** | `specCalib` stopped them on 2023-06-12: "Slit flat lamp is dead, skipping slit flats" |
| ThAr, fiber flats | never | refused: by the KiwiSpec service (not in `lamp_frames`) and by the request API (an `ARC` or `LAMP_FLAT` configuration is refused with the reason, since no MINERVA telescope lists lamps; a lamp in `extra_params` is refused everywhere) | never taken: the lamp calls were commented out, and the server's handlers do not exist |

The science spectra themselves are taken with the cell in (the service's
`default_iodine: "in"`, or `extra_params.iodine: true`). The B stars'
names and exposure times in the example are illustrative: the real list
is legacy's "B stars" sheet, which is not in the public repository. The
nightly biases and darks take about 65 minutes with the 22 s readout
(the calibration unit's `TimeoutStartSec` is 2 h).

## 5. Spectra in the sequencer

Done: `docs/rv_night.md`. In short, a `SPECTRUM` request is fed by every
free telescope it allows. Each one acquires the target, locates the fiber
with the backlight (`POST /backlight` through its guider) and guides. The
leading telescope takes the spectrum (`POST /expose` with the telescopes
that are guiding), and a telescope that drops out is reported in the
spectrum's sidecar, not silently included. Every cleanup calls
`KiwiSpecRemote.abort` before parking. Calibrations (`POST /calibrate`)
run in the closed-roof calibration plan. Robofast's metadata goes into a
sidecar file; the raw frame is never rewritten (question 10 below:
robofast does not rely on `write_header_done`'s in-place update for
anything it records itself).

## 6. Questions for JDE

1. **The exposure meter is disabled** in `spectrograph_server.py`'s
   `__main__` ("*** EXPMETER disabled ***"). Is it in use at all now? If it
   is to be used: is the reading period still 1 s, is `MAXSAFECOUNT` (3e7
   counts per reading) right, and does the PMT see light only while the
   camera shutter is open (the `O` "shutter trigger"), or all the time?
2. **Is the chiller** (also disabled) still part of KiwiSpec?
3. ~~ThAr and fiber flat lamps~~ **Answered** (JDE, 2026-10-31): there
   are none, and no way to switch over: KiwiSpec is iodine only (section
   4). Still open: does the slit-flat LED work (legacy: "Slit flat lamp is
   dead", 2023)? Until it is confirmed, `slit_flat_led` stays false.
4. **Iodine stage:** its speed and homing time? Should the service ever
   power-cycle the stage by itself (legacy did, after two failed moves)?
   (The positions are answered: the live server's values are correct, and
   robofast reads them from it; see "The iodine stage's positions".)
5. **Does a second connection to SI Image work**, and what does
   `GetStatusFromCamera` return (format, CCD temperature field)? The
   simulator assumes `name,value` lines.
6. **After `TerminateAcquisition`,** does SI Image read out and keep the
   frame (as the simulator assumes), and is the file at `C:/IMAGES/I`
   complete when `Acquire`'s Done arrives? Is the SI header's `TIME` the
   shutter open/close, so the midpoint can be referred to it?
7. **CCD temperature limits** to refuse an exposure (assumed -95..-85 C,
   from PARAM62/PARAM63), and the pressure alarm (legacy 0.006 mbar)?
8. **`write_header_done` adds 7 hours to `DATE-OBS`** (local to UTC). Is the
   spectrograph PC still on MST without daylight saving?
9. **Recovery:** when SI Image wedges ("click OK on KIWISPEC-PC and restart
   mainNew.py"), should robofast ever restart it (`kill_si_image` /
   `start_si_image`, which stops the cooler), or always page a human?
10. **Raw files:** may the service rely on the server's in-place
    `write_header_done`, or should robofast keep its keywords in a sidecar
    and leave the raw file exactly as the camera wrote it?
11. **Which telescopes feed the fiber** now (the example config lists T1-T4)?
