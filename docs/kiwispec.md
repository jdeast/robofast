# KiwiSpec: the legacy protocol and the robofast service

KiwiSpec, MINERVA's fiber-fed spectrograph, is controlled from a Windows PC
next to it ("the spectrograph PC"). robofast reaches it the way legacy
MINERVA did, through two TCP services on that PC, and adds one owning
service on the Linux side (design.md 3.1: "one owning service, a small typed
HTTP API, a simulator"):

| robofast module | What it is |
|---|---|
| `robofast/devices/kiwispec_client.py` | async clients for both legacy services: allowlisted commands, hard timeouts, typed replies, bounded retries |
| `robofast/devices/kiwispec_service.py` | the single owner: HTTP/JSON API, one job at a time, count termination and the flux-weighted midpoint, the raw-file rewrite, SI Image restarts, the hardware-safety gate, and `KiwiSpecRemote` (the abort hook for the sequencer's cleanup) |
| `robofast/devices/kiwispec_flux.py` | the flux record from the FAU guide frames: the per-telescope log and the summed flux-weighted midpoint |
| `robofast/devices/kiwispec_fits.py` | the verified in-place rewrite of SI Image's raw file into legal FITS |
| `robofast/devices/kiwispec_aux.py` | the chiller (telemetry), and `AuxRouted`: the heater, iodine stage, backlight and pressures through robofast's own Alpaca server instead of the legacy one (`"aux"`; docs/kiwispec_aux.md) |
| `robofast/devices/kiwispec_aux_server.py` | that Alpaca server: the auxiliary devices driven directly over their serial ports, by URL (docs/kiwispec_aux.md) |
| `robofast/guider/flux_report.py` | each guider's report of its fiber flux to the service (`POST /flux`) |
| `robofast/devices/kiwispec_sim.py` | a TCP simulator of the spectrograph PC (both protocols, legacy quirks included, SI Image's raw files), with fault injection |
| `tests/core/test_kiwispec.py`, `test_kiwispec_flux.py`, `test_guider_fiber_flux.py` | the fast-tier tests, all against the simulators on loopback |
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
| `start_si_image` / `kill_si_image` | `None` | `success` | 10 s | start or `taskkill` the SI Image camera program (killing it stops the CCD cooler until it is started again) |

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
| data (id 131) | `>IBBiHH` + payload: error code, data type, byte length | type 2007 "Done" carries `>H` function number; 2002 status (16 big-endian float32s: [0] CCD, [1] backplate temperature, C); 2006 image header; 2009 camera parameters |
| image (id 132) | `>IBBiHHHHHHII` + pixels | only for `RetrieveImage`, unused |

Functions legacy uses: `SetAcquisitionMode` 1034 (`>B` 0 = single frame),
`SetAcquisitionType` 1036 (`>H` 1, `>B` 0 light / 1 dark), `SetExposureTime`
1035 (`>I` ms), `Acquire` 1037 (`>HHHBB` 4, 1, 0, 73, 0), 
`TerminateAcquisition` 1018, `SetReadoutMode` 1042 (`>B`; legacy 3),
`SetCCDFormatParameters` 1043 (`>6i`; legacy 50, 2090, 1, 0, 2052, 1),
`SetCooler` 1046 (`>B`), `GetStatusFromCamera` 1011 (binary floats, above;
never parsed by legacy). The client waits for the ack, then for a Done (or the
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
| backlight on/off | **yes** (an LED disk on an arm: a Dynamixel servo swings it in over the fiber input and out again) | LED | allowed; with the legacy exposure meter in use, the service pauses it first (the backlight would saturate it) |
| slit-flat LED on/off | | **yes** (PDU outlet) | allowed |
| iodine cell heater on/off, set point | | **yes** (heater, through a Thorlabs controller; on/off is a toggle) | allowed |
| exposure meter pause/resume | | **yes** (PMT high voltage, PDU outlet) | allowed; sent only with `flux_source: "expmeter"` (the meter is not used at MINERVA) |
| SI: exposure (`Acquire`), `TerminateAcquisition` | **yes** (camera shutter) | | allowed |
| SI: cooler on/off, readout mode, CCD format | | cooler | allowed (cooler only ever turned **on**) |
| `kill_si_image` / `start_si_image` | | **stops/starts the CCD cooler** | allowed: only to restart a wedged SI Image, kill then start at once ("Restarting SI Image") |
| ThAr / flat lamp on/off | | | not possible: the handlers do not exist (forbidden: they kill the listener) |
| vacuum: pump, pump valve, vent valve | **valves** | **pump** | not on this protocol (legacy `spectrograph.py` switches them on the bench PDU directly: `vent`, `pump`, `hold`); the server closes the pump valve by itself on a suspected pump failure. Not in robofast |
| `save_image`, `write_header*` | | | allowed; edit files on the PC |
| rewrite the raw file as legal FITS | | | the service, on the data directory's mount, verified before it replaces the raw file ("The raw file") |
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
- Both refuse anything but a verified simulator unless constructed with
  `allow_hardware=True` ([the hardware gate](design.md#the-hardware-gate):
  loopback alone is not enough, since an SSH tunnel to the spectrograph PC
  is loopback too), and re-verify before every connection.

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
- **No automatic power cycle** of the iodine stage, no recursion, no
  `sys.exit()` in a thread, no lock held across an error, no 32-minute
  lock wait (`recover_server`'s `si_lock.acquire(1920)` -- which, in
  Python 2, is a blocking acquire with no timeout at all). SI Image is
  restarted only as "Restarting SI Image" says: kill, then start at once.
- **The raw file is rewritten once, verified.** Legacy's `fix_fits`
  rewrote the raw spectrum in place (`clobber=True`) with no check; robofast
  writes the legal file next to it, verifies it and only then replaces the
  raw file ("The raw file").
- **The file name is sent again just before `save_image`**: a server that
  restarted mid-exposure has forgotten it.
- **The CCD setpoint** (`ccd_setpoint`, default -80 C) and SI's
  "Operational Temp" (`ccd_operational_temp`, -75 C: SI's warning and popup
  threshold) are sent at every connection to SI Image, with the readout mode
  and format: SI function 1045 "Set any Configuration Parameter" (SI Image
  manual 6.3.15; 1044 sets only Readout & Format parameters, hence its
  Error 6003). They go to the camera's RAM, in effect until SI Image
  initializes the camera again (it reads them back from the EEPROM, which
  keeps -90 and -85 and is never written), so robofast re-sends them each
  time. Read back with function 1048 (the 32 Readout & Format and 32
  Configuration parameters; CP14 the setpoint, CP15 Operational Temp, in
  tenths of a kelvin).
- **A warm CCD refuses the exposure** (outside `ccd_temp_min..ccd_temp_max`,
  default -95..-78 C).
- **Backlight:** with the legacy exposure meter in use, it is paused before
  the backlight goes on and resumed after (legacy had this commented out).
- **No exposure meter.** The flux record comes from the FAU guide frames
  (next section).
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

### The flux record: FAU guide frames, not the exposure meter

**The legacy exposure meter (a PMT) is not used.** `flux_source` (default
`"fau"`) chooses where a science frame's flux record comes from:

| `flux_source` | Flux record | Count termination (`target_counts`) |
|---|---|---|
| `fau` (MINERVA) | the FAU guide frames the guiders report (`POST /flux`) | on the summed FAU counts (relative units, below) |
| `expmeter` | the legacy exposure meter's running total | on the meter's counts |
| `none` | none | ignored, with a warning |

With `fau` or `none` the service never resets, reads or pauses the meter
(and `/status` does not read it). An exposure runs its fixed `exptime`
unless a `target_counts` is reached first. When there is no flux record
for a frame -- no guider reported, or the reports cover less than
`fau_min_coverage` (default half) of the exposure, or the meter failed --
the frame's `flux_midpoint_*` are `null`, its `midpoint_source` is
`geometric`, and the barycentric correction uses the middle of the
exposure (`robofast/spectra.py`; the sidecar says which).

#### Flux from the FAU guide frames

Each telescope's fiber-acquisition unit (FAU) images the fiber's entrance
on its guide camera. The backlight lights the fiber from behind, so the
fiber's position on each guide camera is known (`POST /locate` on the
guider, `docs/guider.md`). With `guide.fiber_flux` on, the guider sums the
counts inside the fiber's footprint in every guide frame (a
`fiber_aperture` radius around the fiber pixel, less the median of an
annulus: `robofast.guider.loop.fiber_counts`), and its `flux_report`
(`robofast/guider/flux_report.py`) sends them every `interval`
seconds to this service:

    POST /flux  {"telescope": "T1", "frames": [[t_start, t_end, counts], ...]}

Times are Unix seconds, UTC (the guider and the service are both on NTP);
`telescope` is the service's name for it. A report that fails is kept
(bounded) and resent; nothing in the guide loop waits for it. The service
keeps each telescope's frames for `fau_max_age` (4 h); `/status`
`flux_reports` shows when each telescope last reported.

All four telescopes feed the fiber at the same time, and a spectrum's
flux is the sum over the telescopes in its `telescopes`
(`robofast/devices/kiwispec_flux.py`, `fau_flux`). Each frame's counts
arrive at a constant rate over the frame; a gap between frames of at most
`fau_max_gap` (10 s) is bridged (each frame's rate holds until halfway to
its neighbour), a longer gap is left uncovered. Then

    counts = Σ_tel ∫ R_tel(t) dt,   t_mid = Σ_tel ∫ t R_tel(t) dt / counts,   over [start, end]

(tested against analytic midpoints for constant, linear and exponential
rates, and for two telescopes). With `target_counts`, the running sum is
checked every `expmeter_poll` s and the exposure terminated once it is
reached (`ended_by: "counts"`); since guiders report every couple of
seconds, termination lags the light by about one report interval. The
midpoint is computed after the readout, so the last reports are in. The
result has `fau_counts` and, per telescope, `fau` (`counts`, `frames`,
`coverage`); the header `FAUFLUX` and `FLUXSRC`.

**A relative flux.** MINERVA's FAU is a beamsplitter: the guide camera sees
a fixed fraction of the light reaching the fiber's region, not the light
that actually couples into the fiber (losses from focus, seeing and
centring do not show). The FAU counts are a relative flux scale: right for
weighting the midpoint in time, and for a `target_counts` calibrated in
these same units (per telescope, per camera gain and exposure), but not a
measure of the light in the spectrum. With offset guiding the fiber must
be inside the guide window for its counts to be measured.

#### The legacy exposure meter (`flux_source: "expmeter"`)

During a science frame the service resets the total, then polls
`get_expmeter_total` every `expmeter_poll` s. If the total reaches
`target_counts` it sends `TerminateAcquisition` (the frame is still read
out and kept; `ended_by: "counts"`); otherwise the exposure runs for
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

### The raw file

SI Image writes a file that is not legal FITS (the header says `SIMPLE =
F`, and standard readers refuse it), so the raw file's header is
rewritten. There is **one copy at rest**: the legal file replaces the raw
one, and only after it is verified (`robofast/devices/kiwispec_fits.py`,
`standardize`). With `raw_fits: {"dir": ...}` (the spectrograph PC's data
directory as this host mounts it; the file is `<dir>/<night>/<file>`), after
each frame's header is written the service:

1. waits (up to `raw_fits.wait`) for the file to appear, reads it and
   decodes its 16-bit pixels;
2. writes the legal file next to it under a temporary name (same
   directory, so the rename is atomic), with FITS `CHECKSUM`/`DATASUM` and
   `RAWSHA` (the SHA-256 of the pixels), and fsyncs it; `DATE-OBS` still in
   the PC's local time (SI Image's own comment) is converted to UTC with
   `pc_utc_offset_hours`;
3. re-opens it: valid FITS, checksums good, the pixel array identical
   (shape, every value, SHA-256) to the raw file's;
4. checks that the raw file did not change meanwhile (size, mtime,
   SHA-256);
5. `os.replace`s the raw file with it and fsyncs the directory.

On any failure the temporary file is deleted, the raw file is left exactly
as it was, and `/status` raises an alarm (`raw file ... not rewritten (left
untouched)`); the frame's result says why (`fits.error`). A file that is
already legal is left alone; one whose header was never written (SI
Image's `PARAMnn` keywords still in it) is refused, as legacy did. The
result's `fits` (`pixel_sha256`, `datasum`, `raw_sha256`) goes into the
spectrum's sidecar. The form of the illegal file is taken from legacy
`fix_fits.py`; it has not yet been checked against a raw file from the
current camera software.

### Restarting SI Image

SI Image sometimes wedges (legacy's recovery note: "click OK on KIWISPEC-PC
and restart"). robofast may restart it, but must start it again **as soon
as possible**: killing it stops the CCD cooler, and the CCD's temperature
stability matters. So the restart is always the pair, back to back:
`kill_si_image`, then at once `start_si_image` (retried until
`camera_restart_timeout`), then reconnect and turn the cooler on. It runs
after `camera_restart_after` (2) camera failures in a row (a failed
exposure whose camera connection was dropped), while the job still holds
the spectrograph, or on `POST /restart_camera`. `camera_restart: false`
turns the automatic restart off. A restart that cannot start SI Image again
raises an alarm ("start it by hand"). `/status` `camera_restarts` lists the
recent ones, with how long each took. The next exposure still checks the
CCD temperature, so a frame is never taken before the CCD is back in range.

### Vacuum

The spectrograph is, for now, isolated from its pump: the pump side is at
atmosphere, and the gauges read correctly. `max_spec_pressure` (default
0.006 mbar, legacy) is the threshold, and `pressure_alarm` says what a
pressure above it is: `"alarm"` (default; `/status` `alarms`, so the
sequencer marks the spectrograph down) or `"warning"` (`/status`
`warnings` only). MINERVA's example config runs with `"warning"`. The pump
pressure is read for telemetry; it raises nothing. robofast does not
switch the pump or its valves.

### Other hardware

* **The backlight** (a fiber back-illuminator). A Dynamixel servo flips an
  arm into the fiber beam at the spectrograph end; the arm carries a disk
  of LEDs that lights the science fiber from behind. At each telescope's
  FAU (fiber acquisition unit) that light leaves the fiber, passes the 2 %
  side of the beamsplitter (heading away from the camera), goes into a
  collimator, hits a corner cube, comes back through the same lens to a
  focus, through the 98 % side of the beamsplitter and a V-band filter,
  onto the ZWO guide camera, which is confocal with the fiber. The spot
  marks where on the guide chip a star must be placed to go down the
  fiber: it is the guiders' fiber-position calibration (`POST /locate`,
  docs/guider.md), not a flat lamp. Two coupled controls, the arm (in the
  beam / out) and the LED (on / off): the LED lights only with the arm in,
  and the arm goes out only after the LED is off; the LED is turned off
  (and the arm swung out) 60 s after it was turned on, because it heats
  the spectrograph. `backlight_on` / `backlight_off` on the legacy server,
  `POST /backlight` here; with `"aux"`, the aux server's backlight Switch
  (docs/kiwispec_aux.md: no Alpaca device type fits it). The simulator
  models the arm (`backlight_arm`). `"backlight": false` in the config
  says there is no working backlight: no frame sends "backlight off" and
  `POST /backlight` is refused (409), and the guiders use the fiber
  positions in their configs. MINERVA runs that way while the servo is
  silent: the legacy server then answers `backlight_off` with `fail`
  (seen 2026-11-05), which would fail every frame.
* **The iodine stage** is the iodine cell selector: it moves the iodine
  cell into (`in`) and out of (`out`) the light path, and also carries the
  slit-flat lamp (`flat`), which is dead. `flat` stays among the named
  positions, but is unavailable: the service refuses it (a slit flat, or
  `configure` `iodine: "flat"`) unless the site's config sets
  `slit_flat_led`, and the aux server lists it in `stage.disabled`.
* **The iodine cell heater**, through a Thorlabs controller behind the
  legacy server: on/off (`POST /configure` `heater`; the last commanded
  state is `/status` `heater_on`), set point, and the cell's temperature
  (`/status` readings `heater_temp`, `heater_setpoint`).
* **The chiller** (an Oasis 3): telemetry only. Its driver is in the aux
  Alpaca server (docs/kiwispec_aux.md); the service reads it with `"aux":
  {"chiller": true}`. `chiller: {"kind": "sim"}` is the simulator;
  `{"kind": "oasis3"}` names it and reports, as a warning, to read it
  through the aux server. A reading (`temp`, `setpoint`, `flow`, `fault`)
  is in `/status` readings `chiller`; a temperature off its set point by
  more than `tolerance` is a warning, a fault an alarm.
* **Direct control** (docs/kiwispec_aux.md): with `"aux"` in the config the
  heater, the iodine stage, the backlight, the pressures and the chiller
  go to robofast's own Alpaca server, which drives their serial ports
  itself (through a serial-to-TCP bridge on the spectrograph PC), and not
  to the legacy server.
* **FITS header:** every frame gets `I2TEMPA` and `I2TEMPS` (the cell's
  temperature and set point) and the stage's `I2POSAF`/`I2POSAS`/`I2POSSS`;
  a failed read is written `UNKNOWN`, never left out.
* **The spectrograph PC's clock** is MST without daylight saving
  (`pc_utc_offset_hours: -7`); SI Image stamps `DATE-OBS` in it.

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
| SI Image wedged (takes commands, never answers) | each exposure fails within its bound; after `camera_restart_after` failures SI Image is killed and started again at once, and the next exposure works |
| raw file damaged, or changed during the rewrite | the raw file is left untouched; `/status` alarms |
| camera hung during an abort | `/abort` returns after `abort_timeout` with `ended: false`; the job is cancelled, the camera connection dropped, the claim released |

### Night names and times

The raw file is named `n<YYYYMMDD>.<object>.<NNNN>.fits`. The sequencer
sends the night (`"night"` in `expose` and `calibrate`), from its clock, so
the raw file, its sidecar's directory and every other night name agree
(`robofast/clock.py`, `docs/rv_night.md`). Without it the service uses
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
| **B stars through iodine** | at night, a scheduled request, once a night by default | `SPECTRUM` requests with `extra_params.iodine: true` and `extra_params.cadence` (`max_per_night: 1` in `pool: "bstars"`, so one B star a night whichever it is; raise it for more), `BSTAR-*` in `sites/minerva/requests.example.json`, made from the real "B stars" sheet by `robofast.sites.minerva.target_sheet` (`docs/minerva_sheet.md`) | a rapidly rotating B star (nearly featureless) observed with the cell in, one per night: `mkschedule` put one at the start of the night, the scheduler weighted B stars up as the night ran out ("weight for 1 Bstar per night") and down once one was observed |
| **Templates** | at night, one-shot requests, good conditions | `SPECTRUM` with `extra_params.iodine: false` (the cell out), no cadence: complete once taken (`TEMPLATE-*`) | targets observed with `i2` false (`template` targets in the schedule) |
| **Slit flats** | only if the slit-flat LED works (it doesn't: dead since 2023, JDE 2026-11-01) | `slit_flat_led: true` in the KiwiSpec config, then `{"kind": "slitflat", "exptime": 1, "n": 11}` in the calibration plan. **Off by default** | `specCalib` stopped them on 2023-06-12: "Slit flat lamp is dead, skipping slit flats" |
| ThAr, fiber flats | never | refused: by the KiwiSpec service (not in `lamp_frames`) and by the request API (an `ARC` or `LAMP_FLAT` configuration is refused with the reason, since no MINERVA telescope lists lamps; a lamp in `extra_params` is refused everywhere) | never taken: the lamp calls were commented out, and the server's handlers do not exist |

The science spectra themselves are taken with the cell in (the service's
`default_iodine: "in"`, or `extra_params.iodine: true`). The B stars'
names and exposure times in the example are illustrative: the real list
is legacy's "B stars" sheet, which is not in the public repository
(`docs/minerva_sheet.md` reads it). The
nightly biases and darks take about 65 minutes with the 22 s readout
(the calibration unit's `TimeoutStartSec` is 2 h).

## 5. Spectra in the sequencer

KiwiSpec is an instrument type, `kiwispec`
(`robofast/devices/kiwispec_instrument.py`, registered by the devices
package's built-in plug-in; docs/plugins.md): it takes `SPECTRUM` requests
with its own `extra_params` `iodine` and `target_counts`, its calibration
frames are the service's, and an enclosure config's
`"spectrograph": {"type": "kiwispec", "url": ...}` builds the sequencer's
`KiwiSpecRemote`. The sequencer itself knows only the neutral
`SpectrographHook` and its errors (`KiwiSpecBusy` is a
`SpectrographBusy`, every other `KiwiSpecError` a `SpectrographError`).

Done: `docs/rv_night.md`. In short, a `SPECTRUM` request is fed by every
free telescope it allows (all four feed the fiber at once). Each one
acquires the target, locates the fiber with the backlight (`POST
/backlight` through its guider) and guides, and its guider reports the
fiber flux (`POST /flux`). The leading telescope takes the spectrum (`POST
/expose` with the telescopes that are guiding), and a telescope that drops
out is reported in the spectrum's sidecar, not silently included. Every
cleanup calls `KiwiSpecRemote.abort` before parking. Calibrations (`POST
/calibrate`) run in the closed-roof calibration plan. robofast's metadata
goes into a sidecar file; the raw frame is made legal FITS once, verified
("The raw file"), and its checksums are in the sidecar.

## 6. Questions for JDE

Answered (JDE, 2026-10-31 to 2026-11-02), and how robofast follows:

1. ~~The exposure meter~~ **Not used.** Exposures run their fixed time
   unless a flux record says otherwise; the flux record is the FAU guide
   frames ("The flux record"); `flux_source: "expmeter"` keeps the meter's
   code for a spectrograph that has one.
2. ~~The chiller~~ **Part of KiwiSpec**: an Oasis 3, for telemetry
   ("Other hardware"). Its interface is still to be found (below).
3. ~~ThAr and fiber flat lamps~~ **None** used robotically: they were only
   ever fed by hand, and there is no way to switch over; KiwiSpec is iodine
   only (section 4), and no MINERVA plan asks for them
   (`test_no_minerva_plan_expects_lamp_frames`). The slit-flat LED is dead
   (legacy, 2023), so `slit_flat_led` stays false until it is repaired.
4. **Iodine stage:** its speed and homing time? Should the service ever
   power-cycle the stage by itself (legacy did, after two failed moves)?
   (The positions are answered: the live server's values are correct, and
   robofast reads them from it; see "The iodine stage's positions".)
5. **Does a second connection to SI Image work?** (`GetStatusFromCamera`
   is answered: 16 big-endian float32s, [0] the CCD and [1] the backplate
   temperature; read on the hardware 2026-11-05.)
6. **After `TerminateAcquisition`,** does SI Image read out and keep the
   frame (as the simulator assumes), and is the file at `C:/IMAGES/I`
   complete when `Acquire`'s Done arrives? Is the SI header's `TIME` the
   shutter open/close, so the midpoint can be referred to it?
7. ~~CCD temperature limits~~ **Answered:** the setpoint is -80 C (JDE
   2026-11-05; the cooler holds about -80.5 at full power with the chiller
   at 21 C), set by robofast at every connection ("The CCD setpoint"); an
   exposure is refused outside -95..-78. ~~The pressure alarm~~ **Answered:** 0.006 mbar,
   configurable, and downgradable to a warning while the spectrograph is
   isolated from its pump ("Vacuum").
8. ~~The PC's clock~~ **MST without daylight saving** (`pc_utc_offset_hours`).
9. ~~Restarting SI Image~~ **robofast may restart it when it wedges, and
   must start it again as soon as possible** ("Restarting SI Image").
10. ~~Raw files~~ **The camera software's file is not legal FITS, so its
    header is rewritten; no second copy is kept.** robofast rewrites it in
    place, verified ("The raw file").
11. ~~Which telescopes feed the fiber~~ **All four, simultaneously.**

Still open:

* The auxiliary devices' protocols as robofast drives them directly
  (docs/kiwispec_aux.md, "To confirm on the hardware"), above all the
  iodine stage's `counts_per_mm`.
* The **raw file's** exact form: confirm with one raw file from the
  current SI Image that `kiwispec_fits.read_raw` decodes it (the form is
  taken from legacy `fix_fits.py`), and the data directory's mount on the
  control computer (`raw_fits.dir`).
* The **FAU flux scale**: each FAU's beamsplitter fraction and camera gain,
  if `target_counts` is to mean the same on every telescope.
* Questions 4 to 7 above.
