# The guider

`robofast_core/guider/` is a generic guide-camera service: it keeps a star on
a spectrograph fiber (or any target pixel) by moving a tip/tilt mirror, the
mount, or both. One service runs **on the computer the guide camera is
plugged into**. Frames never leave that computer while guiding: centroids
are computed next to the camera, and only commands, corrections and
low-rate telemetry cross the network.

It is written for three setups, none of which robofast has driven yet:

| Telescope | Guide camera | Fine correction | Coarse / offload |
|---|---|---|---|
| MINERVA T2 | ZWO ASI (FAU) | Starlight Xpress AO (never commissioned) | mount offsets (PWI4) |
| MINERVA T1 (and others without AO) | ZWO ASI (FAU) | mount offsets | -- |
| FLWO 60" / TRES front end | Andor Zyla (tres-guider) | PI S-340 + E-518 | TCSGO `Offset` action |

Some FLWO hardware has only an analog guide camera; there is a camera hook
for a frame grabber (`FrameGrabberCamera`), not yet implemented.

Everything here has run only against simulators. Each hardware fact not
confirmed on the real device is marked *assumed* or listed under
[What is left](#what-is-left).

## Architecture

```
         sequencer (control computer)                guide computer (camera attached)
  +----------------------------------+        +-------------------------------------------+
  | observation with guide: true     |  HTTP  | guider service  (robofast_core.guider.     |
  |   GuiderRemote.start/check/stop  | -----> |                  service)                 |
  |   status -> value registry       | <----- |  Camera -> Centroider -> Controller ->    |
  |   (FITS headers)                 |  JSON  |  Calibration -> TipTilt     (every frame) |
  +----------------------------------+        |  Offloader -> mount offset  (occasional)  |
                                               |  telemetry, alarms, value registry        |
                                               +-----+-----------------+-----------------+-+
                                                     | USB             | serial/USB      | Alpaca HTTP
                                                ZWO/Alpaca camera   SX AO / PI E-518   telescope (Offset,
                                                                                         PulseGuide)
```

The parts are typed protocols (`robofast_core/guider/base.py`), so each
setup is a configuration:

- **Camera** (`camera.py`, `sim.py`): `expose(exposure, roi, gain) ->
  Frame`, with the window, the pixels and the time the exposure *ended*.
  Backends: the simulator; any Alpaca camera (the slow path: an HTTP round
  trip and a download per frame, ~180 ms per 64x48 window on OmniSim, for
  tests, commissioning and mount-only guiding at ~1 Hz); ZWO ASI through the
  ZWO SDK's Python binding `zwoasi` (optional import; video mode, so
  readout overlaps the next exposure; written against the SDK API, **not
  yet run on a camera**); a frame-grabber hook.
- **Centroider** (`centroid.py`, numpy only; the good ideas of tres-guider's
  `centroid.py` and `annulus.py` without scipy, OpenCV or SExtractor):
  windowed, iterated centre of mass (default; ~0.004 px rms on a bright
  star); a three-point Gaussian/parabolic peak fit; a least-squares Gaussian
  (Gauss-Newton); and the **annulus** centroider for a star on the fiber
  hole: tres-guider's star-plus-hole model with the hole fixed at its
  located position and the seeing width fixed once measured on a star clear
  of the hole (a free width is degenerate with the amplitude when the core
  is in the hole), falling back to a centre of mass with the hole *and its
  mirror image* masked. On a 3-pixel hole and a 4-pixel FWHM star a plain
  centre of mass is off by ~0.8 px rms; the annulus fit by ~0.01 px.
- **Controller** (`controller.py`): below.
- **TipTilt** (`tiptilt.py`): `move_by(da, db)` and `home()` in the
  device's own units, with limits. Starlight Xpress AO (serial), PI E-518
  (GCS through `pipython`, optional import), and `MountOffset` (the
  robofast Alpaca telescope's `Action("Offset")` or `PulseGuide`). Each has
  a protocol-faithful simulator.
- **Calibration** (`calibration.py`): a measured 2x2 matrix per device and
  port.
- **Offloader** (`offload.py`): moves the tip/tilt's mean displacement to
  the mount.

`loop.py` holds the loop (`Guider`): fiber location, acquisition, guiding,
loss and re-acquisition, telemetry and alarms. `service.py` is the HTTP
service and `GuiderRemote`, the sequencer's client.

## The loop and its latency budget

Each frame: expose the window (`roi_size`, default 48 px) -> centroid ->
controller -> calibration -> device command. The loop records, per frame,
the **latency from the end of the exposure to the moment the correction
went on the wire**; `GET /status` reports its median, 95th percentile and
maximum, and the loop rate.

| Step | Budget | Notes |
|---|---|---|
| exposure | 2-50 ms | sets the loop rate; the star's flux decides |
| readout + transfer to the guide computer | ~1-5 ms (ZWO ROI, USB 3, video mode) | *assumed*; overlaps the next exposure in video mode |
| centroid (48x48 window) | 0.3-2 ms | measured: windowed COM 0.4 ms, three-point peak 0.3 ms, annulus fit 1.1 ms, Gaussian fit 2 ms (numpy) |
| controller + calibration | < 0.1 ms | |
| command: PI E-518 `MOV` (USB) | ~1 ms | *assumed*; settling ~1 ms (S-340) |
| command: SX AO at 9600 baud | **~8 ms per axis** | 7 characters + 1 reply character at ~1 ms each, plus the mirror's travel; two axes ~17 ms. This, not the camera, limits an SX AO loop to ~30-50 Hz |
| command: mount `Offset` over Alpaca HTTP | 10-50 ms + the mount's response (~0.5 s) | mount guiding: ~1 Hz at most |

**Measured in the simulator** (`test_latency_on_the_real_clock`, real
clock, 5 ms exposures, 32x32 window, windowed COM, simulated tip/tilt) on
the development machine: from exposure end to command sent **1.0 ms
median, 1.1 ms 95th percentile, 1.3 ms maximum**, of which ~0.65 ms is the
simulator rendering the frame (standing in for readout) and **0.34 ms
median (0.38 ms 95th percentile) is the guider's own processing** (frame
delivered to command sent: centroid, controller, calibration, command).
The loop ran at ~150 Hz on 5 ms exposures. The software adds a third of a
millisecond; the budget is the camera and the device link. `GET /status`
reports both (`latency_*` and `processing_*`).

The loop is sequential: the next exposure starts after the command has
been sent. With a slow link (SX AO) the loop rate therefore drops (17 ms of
serial time at 50 Hz exposures gives ~27 Hz). Overlapping the next
exposure with the command (video mode does this for the ZWO) is a later
optimisation.

## Closed-loop performance in the simulator

`tests/core/test_guider_loop.py` runs the real loop on a simulated star in
simulated time: 1 px rms of seeing image motion (power spectrum flat below
a 1 Hz knee, falling as f^-11/3 above), a 0.5 px wind-shake line at 8 Hz
and a 0.05 px/s tracking drift; a tip/tilt with a 2 ms response; photon and
read noise. The residual is the *true* distance of the star from the target,
sampled every millisecond over 2 s after 1 s of settling (open loop:
2.8 px rms):

| frame rate | kp 0.2 | kp 0.5 | kp 0.8 |
|---|---|---|---|
| 25 Hz | 0.93 | 0.74 | 0.68 |
| 100 Hz | 0.55 | 0.37 | 0.26 |
| 400 Hz | 0.24 | 0.11 | 0.08 |

(px rms; the test asserts the ordering and the 400 Hz level.) With the SX
AO's ~17 ms command time at 50 Hz exposures (the loop then runs at 27 Hz)
the residual is 0.62 px at kp 0.5, against 0.53 px with an instant device
(5 s runs). 1000 Hz gives 0.06 px: the centroid noise floor of this star.

## The controller

`Controller` (`controller.py`) is a two-axis PI(D) whose output is the
image motion to apply this frame, in pixels. A tip/tilt mirror (or the
mount) holds its position, so the actuator integrates the corrections: `kp`
is the integrator gain of a classic tip/tilt loop (0 < kp < 1; 0.3-0.6 is
typical), `ki` adds a second integration that removes a steady drift with
no lag, `kd` damps.

Fixed relative to the legacy guiders: both MINERVA's `fauguide` and
tres-guider's `tres.py` called `PID.setPoint()` every frame, and
`setPoint()` zeroed the integrator and the derivative memory, so both ran
as proportional-only loops. Here moving the target (`POST /target`, or
offset guiding) keeps all state, and the derivative acts on the
measurement, so a target step gives no derivative kick. Only a new
acquisition resets it.

Also: a continuous deadband (errors below `deadband` ignored, larger ones
reduced by it); the integrator clamped to `i_max`; anti-windup (no
integration further in a direction in which the output was clipped, by the
step or rate limit or by the device reporting a limit); limits on the
correction per frame (`max_step`, px) and per second (`max_rate`, px/s).
Unit tests cover a step, a ramp (P lags, PI does not), sinusoids of rising
frequency, setpoint changes, the deadband, the limits and anti-windup.

## Calibration

One matrix per device and port, measured:

    d(pixels) = M @ d(device units)

`POST /calibrate {"device": "tiptilt"|"mount", "step": ...}` (or
`calibration.measure`) homes the device, moves it by +/- `step` on each axis
and diagonally (9 positions), averages `frames` centroids at each, fits
`pixel = p0 + M @ position` by least squares and refuses a fit whose rms
residual exceeds a tenth of the motion of one step (seeing: average more,
or step further). The result is saved as
`<calibration_dir>/<device>__<port>.json` (atomic write) and loaded at
start.

The matrix replaces MINERVA's `ARCSEC_PER_STEP` and `ROTOFFSET`
(`config/ao_*.ini`) and tres-guider's `STEPS_PER_PIXEL_TIP/TILT`, `THETA`
and `FLIP`: scales, rotation, flip (`det M < 0`) and any non-orthogonality
all come out of the fit, so no sign convention has to be guessed.
`from_sx_legacy` and `from_tres_legacy` turn the old numbers into a matrix
to compare with a measurement. With a plate scale and position angle
(`sky_matrix`: arcsec east/north per pixel; PA of the detector's +y axis
east of north; parity for a mirrored field) a calibration also maps to the
sky.

The mount is calibrated the same way (its units are arcseconds of offset).
Its matrix depends on the port and, without a derotator, on the field
rotation: calibrate per port, and recalibrate if the rotator angle changes.

## Fiber acquisition

1. **Locate the fiber**: `POST /locate` turns the backlight on (MINERVA:
   the KiwiSpec service's `POST /backlight`), takes one frame, turns it
   off, and centroids the brightest blob; that pixel becomes the target.
   Or configure `guide.target`.
2. **Acquire**: a wide frame (`acq_roi`, `acq_exposure`); the star nearest
   the target (or the brightest, `select`; or, with `select: "catalog"`,
   the star at the target's catalog position in a plate solve of the
   frame, which a near-equal neighbour cannot fool: docs/sky.md section 8).
   Farther than `capture_radius`
   pixels: offset the mount through its calibration, wait `coarse_settle`,
   repeat (up to `coarse_iterations`). A distance that grows is the legacy
   "acquisition not converging" alarm (usually a wrong mount calibration
   or rotator angle) and fails the attempt.
3. **Guide**: the fine loop in a `roi_size` window around the target. The
   annulus centroider learns the seeing FWHM from stars measured clear of
   the hole during acquisition (or takes `guide.fwhm`).
4. **Lost**: no valid star for `lost_frames` frames, flux below
   `lost_flux_fraction` of its running mean, or the star leaving the window
   -> state `lost`, alarm `star_lost`, back to step 2; after
   `max_reacquire` attempts, `failed`.

## Offloading

The `Offloader` watches the tip/tilt's mean position over `window` seconds
and, when it is past `threshold` of its range or every `period` seconds (if
it has wandered more than `min_pixels`), offsets the mount by the amount
that moves the star as much as that mean displacement does. It does not
move the tip/tilt back itself: as the mount moves, the fast loop follows
and walks the tip/tilt back to its centre with no jump in the image. It
then waits `settle` seconds before considering another. In the simulator a
1.8 px/s drift on a +/- 5 px tip/tilt is guided for 15 s with three or more
offloads and no limit alarm.

## Devices

### Starlight Xpress AO (MINERVA T2)

Legacy driver: `minerva_library/ao.py` in MinervaCollaboration/minerva-control
(and `config/ao_*.ini`); protocol from the SX AO-L handbook, p. 12. Serial
9600 8N1. `K` centres the mirror (reply `K`); `G` + `N`/`S`/`T`/`W` + five
digits moves that many steps (reply `G`, or `L` if it hit its mechanical
limit). Only these two commands are used. The legacy driver sent `\r\n`
after each command, which the handbook does not show: `terminator` is
configurable and defaults to none (*to confirm on the unit*). Which letter
moves which way on the detector does not matter: the calibration measures
it.

Fixed relative to the legacy driver: every read is bounded (it opened the
port with `timeout=None`, so a silent unit hung the guider); the position
tallies are updated only for moves the unit acknowledged, after the limit
check (it added the move first, so a refused or limited move left them
wrong); a move beyond the software range is clipped to it and reported to
the controller (anti-windup) and the offloader; an `L` reply marks the
tallies uncertain until the next `K`.

### PI S-340 + E-518 (FLWO 60" TRES)

Adapted from jdeast/tres-guider (J. Eastman, J. Kansky; relicensed BSD-3
for robofast with Jan Kansky's agreement): `tiptilt.py` (connect by USB
serial number, servo on, range from `qTMN`/`qTMX`, absolute `MOV` on axes
`A` and `B`, centre the middle of the range) and `calibrate_tiptilt.py`
(the matrix fit replaces its FLIP/THETA/steps-per-pixel least squares).
`pipython` is optional; `SimGCS` implements the same `GCSDevice` methods
(error 7 out of limits, error 5 with the servo off). tres-guider found that
`qPOS` did not seem to work, so positions are read once at connect and
otherwise tallied from the commanded moves (the stage is closed-loop). Calls
run in a worker thread with a timeout.

### Mount offsets (T1, and offloading)

`MountOffset` sends `Action("Offset", '{"east": E, "north": N}')`
(arcseconds on the sky; the TCSGO server's guider offset, `docs/tcsgo.md`)
or `PulseGuide` (direction and milliseconds at a configured guide rate;
east pulses lengthened by 1/cos(dec), the declination given at `start`).
Offsets beyond `max_offset` per axis are clipped and reported. For T1 the
mount is the fine device: the same loop at ~1 Hz with long exposures.

## The service

`python -m robofast_core.guider.service --config guider.json [--hardware]`
(`deploy/guider.example.json` runs entirely on simulators). The hardware
backends' packages are the `guider-hardware` dependency group
(`pip install --group guider-hardware`: pyserial, pipython, zwoasi, redis),
needed only on a guide computer with real devices. The API and the config
are in the module docstring. In short:

- `GET /status`, `GET /telemetry?n=`, `GET /values`;
- `GET /frame?max=N`: the last frame the camera took (the guide window, or
  an acquisition frame), binned on the guide computer to at most N pixels a
  side, compressed, with the target (fiber) and its radius, the last
  centroid and the guide window to draw over it. Display only: the
  observer console's guider thumbnails (docs/console.md, "Images");
- `POST /start` (`target`, `mode`, `exposure`, `dec`), `/stop`, `/mode`
  (`closed`, or `open`: measure only, for commissioning), `/target` (moves
  the setpoint, keeps the controller's state), `/gains`, `/roi`,
  `/exposure`, `/locate`, `/calibrate`.

**Telemetry**: state and reason; per frame (ring buffer): centroid, error,
correction, tip/tilt position, flux, FWHM, latency; over the window:
residual rms (pixels, and arcsec with `plate_scale`), loop rate, latency
statistics, flux, FWHM, range used, offloads, re-acquisitions.

**Value registry** (design.md 3.8): `<scope>.guide_state` (GUIDSTAT),
`guide_rms` (GUIDRMS), `guide_rms_arcsec` (GUIDRMSA), `guide_flux`,
`guide_fwhm`, `guide_rate`, `guide_latency`, `guide_exptime`, `fiber_x/y`
(XSCIFIB/YSCIFIB, as tres-guider), `tiptilt_a/b` (TTPOSA/B). The service
keeps its own registry; `GuiderRemote` copies them into the sequencer's
registry every `poll_interval` while guiding, so they land in the FITS
headers of the frames taken meanwhile (stale values are marked as such).

**Alarms**: `star_lost`, `tiptilt_limit`, `residual_high`
(`residual_alarm` px rms), `acquisition_failed`, `not_converging`,
`device_error`; repeats within `alarm_repeat` seconds are counted.

**Redis (optional, off by default)**: `"redis": {"host", "port",
"channel"}` publishes each telemetry summary in tres-guider's
`tracking_data` JSON shape (timestamp, x/y/north/east mispointing, dx, dy,
north, east, counts, fwhm, platescale, roi_x1..y2), from a worker thread so
a slow Redis never delays the loop. It only publishes: it never
subscribes, reads keys or acts on anything received.

## The sequencer

An observation (or a request configuration, `"guide": true` in the request
model) with `guide` is guided: after the slew, the sequencer calls the
telescope's guider `start(dec=...)`, which waits (bounded,
`acquire_timeout`) for `guiding`; before each exposure it calls `check()`,
which raises if the guider has failed; when the observation ends (complete,
failed, preempted or cancelled) it calls `stop()`. Every cleanup (and the
error path of a telescope) stops the guider **before** it aborts the slew
and parks. `stop()` is bounded and never raises. A guider that cannot
acquire, or fails mid-observation, is that telescope's ordinary error
(back off, give up after `max_consecutive_errors`). A telescope without a
guider fails an observation that asks to guide. Config: per telescope,
`"guider": "http://host:port"` or `{"url", "acquire_timeout", "timeout",
"poll_interval"}`.

## Safety

Every real device backend -- a serial port (SX AO), PI's USB or TCP, a ZWO
or frame-grabber camera, an Alpaca camera or telescope at a non-loopback
address -- is refused unless the config says `"allow_hardware": true`
**and** the command line passes `--hardware` (as the Aqawan and TCSGO
servers). Simulators and loopback devices never need it. The service's
tests check every backend against the gate.

## The simulator

`sim.py` (rendering with the shared `robofast_core/testing/starfield.py`,
written for autofocus): `SimWorld` holds the truth -- star position = nominal +
atmospheric jitter (`Jitter`: power-law spectrum with a knee, wind-shake
lines, drift) + tip/tilt matrix @ tip/tilt position + mount matrix @ mount
offset, each device with its own response (first-order lag after a dead
time). `SimCamera` renders the window with the star at its mean position
over the exposure (photon and read noise, the fiber hole or the backlit
fiber). On a `VirtualClock` (discrete-event: time jumps to the next wake-up
once every task is blocked) a closed-loop run of minutes takes seconds; on
the real clock the latency is the real compute. The SX AO and PI protocol
simulators plug into the same world (`sxao-sim`, `pi-sim` in the config).

## Commissioning T2's AO unit

**Needs JDE's explicit approval before any step that touches the unit,
the camera or the telescope.** The unit has never been commissioned.

1. On the bench (no telescope): confirm the serial settings and whether
   the unit wants a terminator; send `K` and one small `G` move per
   direction with `python -c` against `SXAO` (with `allow_hardware` and
   `--hardware`); record the replies and step size; set the software
   `limits` well inside the travel.
2. Confirm the ZWO camera backend (`zwoasi` installed, SDK library path)
   with the service in `open` mode and no tip/tilt.
3. On sky, in `open` mode: a bright star, `POST /calibrate {"device":
   "tiptilt", "step": ...}` at the FAU port; compare with
   the legacy seed in `sites/minerva/guider_t2.example.json`
   (`from_sx_legacy(0.025, 45, 0.265)` from `ao_t2.ini`; `ao_mred.ini`'s
   0.128 arcsec/step and 214.7 deg belong to the MRED unit -- see
   `sites/minerva/README.md`, conflict 2).
4. Closed loop at a low gain (kp 0.2, 10-20 Hz), then raise; watch
   `latency_*`, `residual_rms` and `range_fraction`; calibrate the mount
   (`"device": "mount"`) and enable offloading.
5. Fiber: `POST /locate` with the KiwiSpec backlight; acquire and guide on
   the hole with the annulus centroider.

## What is left

- Run the ZWO backend on a camera; pipelining (overlap the next exposure
  with the device command); the frame-grabber backend.
- Confirm the SX AO protocol details (terminator, step size, travel, the
  handbook's other commands) and the E-518's timing and `qPOS` behaviour on
  the hardware.
- An Andor Zyla backend for the TRES front end (tres-guider's `guider.py`).
- Mount calibration with field rotation (alt-az without a derotator), and
  the plate scale and PA of each guide camera.
- The dispatch scheduler does not yet keep `guide` requests off telescopes
  without a guider (such an observation fails on that telescope instead).
- Sky-fiber and offset guiding (a target pixel offset from the fiber) work
  through `POST /target`, but nothing in the request model asks for them.
