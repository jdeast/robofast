# MINERVA site configuration

Configuration for MINERVA at Mt. Hopkins (FLWO ridge). Aqawan 1 holds T1 and
T2, the only telescopes in service. T3 is disabled (legacy `control.py`: "T3
disabled for bad collimation"). T4 and Aqawan 2 are down. T3 and T4 appear
only in `instruments.json`, with `"enabled": false`.

| File | Read by |
|---|---|
| `aqawan1.json` | `supervisor_service`, `sequencer_service` (`robofast_core/config.py`) |
| `autofocus.example.json` | merged into `aqawan1.json` (`AutofocusSetup`, `TriggerConfig`) |
| `dispatch.example.json`, `requests.example.json`, `too.example.json` | the dispatch scheduler (`plan_from_config`) |
| `calibration.example.json`, `targets.example.json` | `plan_from_config`, `load_plan` |
| `request_api.example.json` | `request_api` |
| `guider_t1.example.json`, `guider_t2.example.json` | `guider.service` (`GuiderServiceConfig`) |
| `kiwispec.example.json` | `devices.kiwispec_service` (`KiwiSpecConfig`) |
| `sky.example.json` | the dispatch plan's `vetting` entry (`vetting.Vetter`; docs/sky.md) |
| `weather.yaml` | `weather_bridge` |
| `instruments.json` | reference catalog: every legacy value, including those no module reads yet |

`tests/core/test_site_minerva.py` loads every file here through its real
loader and checks the copies of a value against `instruments.json`.

Every device address is a `127.0.0.1` placeholder. The legacy files' `HOST`,
`IP`, `SERVER_IP`, PDU, SNMP and credential keys are not copied.

## Where the values come from

All sources are in
[MinervaCollaboration/minerva-control](https://github.com/MinervaCollaboration/minerva-control),
`config/` (as of 2026-10-31) unless another path is given. "N" is the
telescope number. `[S] KEY` is key `KEY` in section `[S]`. The legacy
per-port sections use M3 port numbers as keys (`0` is "unknown").

### Site and enclosures

| Value | Here | Legacy source |
|---|---|---|
| latitude 31.680407, longitude -110.878977 (deg E), elevation 2316 m | `aqawan1.json` `site`; `dispatch.example.json` `site`; `weather.yaml` `site`; `instruments.json` `site` | `site_mtHopkins.ini` LATITUDE, LONGITUDE, ELEVATION |
| Aqawan 1 = T1, T2; Aqawan 2 = T3, T4 | `instruments.json` `enclosures` | `aqawan_1.ini`, `aqawan_2.ini` TELIDS |

### Telescopes (`instruments.json` `telescopes.tN`, unless noted)

| Value | Here | Legacy source |
|---|---|---|
| aperture 700 mm, focal length 4560 mm (f/6.5) | `aperture`, `focal_length`; `aqawan1.json` `images.telescopes`; `autofocus.example.json` `f_ratio` | `minerva_library/control.py` FITS header (APTDIA, FOCALLEN); not in any `.ini` |
| altitude limits 21-83 deg | `min_alt`, `max_alt`; `dispatch.example.json` `telescopes.tN` | `scheduler.ini` MINALT, MAXALT; `telescope_N.ini` HORIZON = 21 |
| acquisition refuses alt > 85 deg | `acquire_max_alt` | `minerva_library/cdk700.py` `acquireTarget` (hard-coded) |
| hour-angle limits | none (dispatch default -12..+12 h) | legacy had none |
| Sun altitude -12 deg, Moon separation 10 deg | `dispatch.example.json` | `scheduler.ini` HORIZON, MINMOONSEP |
| park alt 25, az 0 | `park` | `cdk700.py` `park()` defaults (not in any `.ini`). robofast parks with ASCOM `Park`, so set this in PWI4. |
| M3 ports: T1 imager 1, FAU 2; T2 imager 2, FAU 1; T3 imager 2, FAU 1; T4 imager 1, FAU 2 | `ports`; `imager.port`, `fau.port` | `telescope_N.ini` `[PORT]` IMAGER, FAU; `imager_tN.ini` PORT (they agree) |
| rotator offsets per port (deg) | `rotator_offset` | `telescope_N.ini` `[ROTATOROFFSET]` 1, 2 |
| focus range per port | `focus.min`, `focus.max`; `autofocus.example.json` `min_position`, `max_position` | `telescope_N.ini` `[MINFOCUS]`, `[MAXFOCUS]` |
| default focus and offset per port | `focus.default`, `focus.offset` | `telescope_N.ini` `[DEFAULT_FOCUS]`, `[FOCUS_OFFSET]` |
| focus model F0, C0, T0, C1, dT0, C2, G0 | `focus.model`; C0 and T0 in `autofocus.example.json` `model` | `telescope_N.ini` `[F0]`...`[G0]`, port 1 (port 2 is identical) |

**Rotator offset convention** (`cdk700.py` `solveRotatorPosition`,
`rotatorMovePA`): mechanical rotator angle = parallactic angle + offset -
sky PA, mod 360, in degrees. So sky PA = parallactic angle + offset -
mechanical angle (`control.py` `fauguide`). Legacy requests PA 0 unless the
target names `pa`. robofast does not move the rotator. PWI4 derotates, and
`aqawan1.json`'s `position_angle` 0 assumes PWI4 holds the imager at PA 0
as legacy did. The offsets go into PWI4's rotator set-up, or into robofast
once it commands the rotator.

**Focus model** (`cdk700.py` `guessFocus`): focus = F0 + C0 (T_M1 - T0) +
C1 (dT - dT0) + C2 (sin(alt) - G0). T_M1 is the primary mirror's temperature
(C), dT = T_M1 - T_ambient, and alt is the mount's altitude. Focus is in PWI
focuser units (microns, so `um_per_step` is 1). robofast's model
(`docs/autofocus.md` section 6) takes the C0 term on the focuser's
temperature until T_M1 is in the registry. The other terms wait for their
inputs.

### Cameras (`instruments.json` `telescopes.tN.imager`, `.fau`)

| Value | Here | Legacy source |
|---|---|---|
| imager plate scale: T1 0.61, T2 0.679, T3/T4 0.61 arcsec/px (unbinned) | `imager.plate_scale`; `aqawan1.json` `images.telescopes.tN.plate_scale`; `autofocus.example.json` `optics.pixel_scale` | `imager_tN.ini` PLATESCALE |
| imager pixel size: Andor 13.5 um, Aspen CG230 15 um | `imager.pixel_size`; `autofocus.example.json` `optics.pixel_size` | derived: PLATESCALE x 4560 mm / 206265 (13.49, 15.01 um), which matches the sensors (e2v CCD42-40, CCD230-42) |
| imager detector 2048 x 2048 | `imager.detector` | `imager_tN.ini` X1..X2, Y1..Y2 (0..2047) |
| imager make, model, serial | `imager.manufacturer`, `.model`, `.serial` | `imager_tN.ini` MANUFACTURER, MODEL, SERIAL; `imager_server_tN.ini` CAMERADRIVER |
| binning 1x1 | `binning` | `imager_tN.ini`, `fau_tN.ini` XBIN, YBIN |
| cooler setpoint, tolerance | `imager.setpoint`, `.max_temp_error` | `imager_tN.ini` SETTEMP, MAXTEMPERROR (MAXCOOLING is read but never used by `imager.py`; not copied) |
| bias level, saturation (ADU) | `bias_level`, `saturation` | `imager_tN.ini`, `fau_tN.ini` BIASLEVEL, SATURATION |
| gain, read noise | `null`: unknown | in no `.ini`. `ascomcam.py` tries to read a GAIN key that no file has. Measure them. |
| pointing centre (0-based px) | `imager.pointing_center` | `imager_tN.ini` XCENTER, YCENTER |
| filters (name: wheel slot) | `imager.filters`; names in `dispatch.example.json` `filters` | `imager_tN.ini` `[FILTERS]` |
| imager position angle 0, no flip | `imager.position_angle`, `.flip`; `aqawan1.json` `images.telescopes` | legacy WCS (`cdk700.py`, CD = scale x [[-cos PA, sin PA], [sin PA, cos PA]]) at the default PA 0. robofast's `Optics` has the same convention (`fits_writer.py`). |
| FAU: ZWO ASI174, 1936 x 1216, 5.86 um, 0.265 arcsec/px | `fau.*`; `autofocus.example.json` `overrides."fau/-"`; `guider_tN.example.json` `guide.plate_scale` | `fau_tN.ini` MODEL, X1..X2/Y1..Y2 (0..1935, 0..1215), `[Tel_params]` platescale. Pixel size derived as above. |
| FAU rotation 0 deg | `fau.rotangle` | `fau_tN.ini` `[Tel_params]` rotangle |
| fiber position (px) | `fau.fiber`; `guider_tN.example.json` `guide.target` | `fau_tN.ini` XFIBER, YFIBER (T1, T3: backlit 2018-05-31; T2: comment says 2016-12-20; T4: 2021-04-09) |
| FAU centre, acquisition tolerance (arcsec) | `fau.center`, `fau.acquisition_tolerance` | `fau_tN.ini` XCENTER, YCENTER, ACQUISITION_TOLERANCE |
| guide gains kp 0.5, ki 0.1, deadband 0 | `guider_tN.example.json` `guide.gains` | `fau_tN.ini` `[Loop_params]` KPx, KIx, Deadband (slow loop). Imax, Corr_max, the fast loop and `[Bp_arcsec]` have no robofast equivalent. |

**Pixel positions** (fiber, centres) are 0-based (x = column, y = row) in
the FITS frame as the legacy software wrote and read it (`imager.py`
`getGuideStar`: OpenCV moments on `pyfits.getdata`). robofast's ZWO backend
reads the camera directly, so confirm the orientation with `POST /locate`
before trusting them.

### T2 AO (Starlight Xpress) and the guider's calibration matrices

The guider's calibration is a 2x2 matrix `M` with d(pixels) = M @
d(device units) (`robofast_core/guider/calibration.py`). The legacy model
(`minerva_library/ao.py`) is tip = -(E cos r - N sin r)/a and tilt = (E sin
r + N cos r)/a, with a = ARCSEC_PER_STEP and r = ROTOFFSET.
`calibration.from_sx_legacy(a, r, plate_scale)` converts it.

| Value | Here | Legacy source |
|---|---|---|
| a = 0.025 arcsec/step, r = 45 deg -> M = [[0.066708, -0.066708], [0.066708, 0.066708]] px/step | `guider_t2.example.json` `_legacy_seed_calibrations.tiptilt` (not loaded); `instruments.json` `t2.tiptilt` | `ao_t2.ini` PLATESCALE = 40 and ROTOFFSET = 45, read the way `ao.py` used PLATESCALE before 2019 (steps per arcsec; see conflicts) |
| travel limits +/-500 steps | `_tiptilt_uncommissioned.limits` | `ao_t2.ini` XMIN..YMAX |
| serial COM9, 9600 baud | `_tiptilt_uncommissioned` | `ao_t2.ini` PORT, BAUDRATE |
| mount M = inv(sky_matrix(0.265, 0, +1)) = [[-3.773585, 0], [0, 3.773585]] px per arcsec (E, N) | `guider_tN.example.json` `_legacy_seed_calibrations.mount` (not loaded) | `fau_tN.ini` `[Tel_params]` platescale and rotangle, with the FAU derotated to PA 0 |

Both seeds are unverified starting points to compare with `POST /calibrate`.
They are not loaded. The T2 AO stays out of the live config: legacy never
used it (`fau_t2.ini` USETIPTILT = False, and `imager_server_t2.ini` has
`#AOINI`).

### KiwiSpec (`kiwispec.example.json`)

| Value | Legacy source |
|---|---|
| ports 44445 (spectrograph), 2055 (SI camera) | `spectrograph.ini` SERVER_PORT, CAMERA_PORT |
| iodine stage positions: **read from the live server**, not configured (see below) | the live `spectrograph_server.ini` `[I2POSITIONS]` on the spectrograph PC |
| iodine heater 55.0 C +/- 0.501 | `spectrograph.ini` I2SETTEMP, I2TEMPTOL |
| SI format (50, 2090, 1, 0, 2052, 1), readout mode 3 | `spectrograph.ini` `[SI_SETTINGS]` |

**The iodine stage's positions** (JDE, 2026-10-31). The live
`spectrograph_server.ini` on the spectrograph PC is authoritative. The two
legacy files disagree (the client's `spectrograph.ini` says in 145, flat
88, out 55; the server's says in 11, flat 68, out 102) because the stage's
home sensor once died and the motor and sensor were swapped at the wire
level, which flipped the positions. So robofast hard-codes neither: the
KiwiSpec service moves the stage **by name** (the server resolves the
name with its own live ini) and learns the millimetres from the server's
own `i2stage_get_pos` replies (`/status` `stage_positions_live`).
`kiwispec.example.json` therefore has `"stage_positions": null`. Set it
only as a cross-check, and only with values **confirmed on the
spectrograph PC** before any hardware use: a configured value that
disagrees with the live server raises an alarm (`docs/kiwispec.md`).

### Weather (`weather.yaml`)

Its limits and their sources are commented in the file itself
(`minerva_library/env.py`, `domeControl.py`).

## Conflicts and unknowns, for JDE

1. **FAU cameras.** Settled (JDE, 2026-10-31): ZWO ASI174 on every FAU.
   (History: the FAU cameras were originally of another make and were
   replaced by the ASI174; robofast carries nothing of the old cameras.)
2. **T2 AO step size.** Never calibrated (JDE, 2026-10-31): commissioning
   will measure it (`docs/guider.md`, Commissioning). The legacy
   candidates, for comparison only: `ao_t2.ini` has no ARCSEC_PER_STEP, so today's
   `ao.py` cannot load it. It has PLATESCALE = 40, commented "steps/pixel",
   which the pre-2019 `ao.py` multiplied by arcseconds (so steps per
   arcsec). The candidates are 1/40 = 0.025 arcsec/step (how the code used
   it, **chosen** for the seed), 0.265/40 = 0.0066 arcsec/step (the comment,
   with today's FAU scale; the file dates from 2016, before the ASI174),
   and `ao_mred.ini`'s 0.128 arcsec/step with ROTOFFSET 214.7, which
   `docs/guider.md` (Commissioning) cites for T2 although it belongs to the
   MRED telescope's unit. ROTOFFSET 45 is also unmeasured. Measure them.
   The AO is disabled.
3. **T2 imager make.** `imager_t2.ini` (MANUFACTURER = Apogee, MODEL =
   CG230) and `imager_server_t2.ini` (ASCOM.Apogee.Camera) disagree with
   the earlier robofast autofocus example ("FLI CG230"). Chosen: Apogee
   Aspen CG230.
4. **T2 imager cooling.** `imager_t2.ini` SETTEMP = +5 C (MAXCOOLING 40)
   against -50 C on the Andors. Confirmed correct (JDE, 2026-10-31).
5. **Iodine stage positions.** Explained (JDE, 2026-10-31): the live
   server's values are correct; the client's `spectrograph.ini` (145/88/55)
   and the server's `spectrograph_server.ini` (11/68/102) disagree because
   the stage's motor and sensor were swapped at the wire level after its
   home sensor died, flipping the positions. robofast reads the positions
   from the live server (above, "The iodine stage's positions") and
   hard-codes neither.
6. **Maximum altitude.** `scheduler.ini` MAXALT = 83, but
   `cdk700.acquireTarget` refuses only above 85. Chosen: 83 for dispatch;
   85 is recorded as `acquire_max_alt`.
7. **T1 focus floor.** `telescope_1.ini` MINFOCUS is 455 on port 1 (the
   imager) and 100 everywhere else. Kept: 455 for T1's imager, 100 for its
   FAU (`overrides."fau/-"`).
8. **Default focus against the model.** `telescope_2.ini` DEFAULT_FOCUS
   4000 / 8559 ("changed on march 28th 2018") is far from the model's F0 =
   16500. T1's DEFAULT_FOCUS is 25000 against F0 = 23850. robofast uses
   neither (only the C0 term plus the learned zero point). They are recorded
   only.
9. **Focus model comment.** `telescope_2.ini`-`telescope_4.ini` write the
   gravity term as `C2*(G0-G0)`, a typo. The code uses sin(alt) - G0.
10. **Gain and read noise** are in no legacy file. `null` until measured.
11. **Aperture, focal length and pixel sizes** are in no `.ini`. Aperture
    and focal length come from the legacy FITS header code. Pixel sizes are
    derived from PLATESCALE and the focal length, and match the sensors.
12. **Park position** (alt 25, az 0) is a code default, not configuration.
    robofast parks through ASCOM `Park`, i.e. PWI4's park position: set it
    there.
13. **Rotator offsets** have no robofast consumer yet: robofast never moves
    the rotator. `position_angle` 0 is right only if PWI4 derotates the
    imager to PA 0 with these offsets. T2 port 1 changed 2025-11-14 (270.1 ->
    151.377). T4 port 2 has a history of re-measurements (latest 136.68,
    2026-03-03).
14. **Fiber pixel orientation**: see "Pixel positions" above. T2's live
    XFIBER/YFIBER carry the same "2016-12-20" comment as the commented-out
    older values, so their date is unknown.
15. **Imager pointing centre**: T1's (984, 1048) is not the chip centre.
    robofast's approximate WCS puts CRPIX at the chip centre, about 40 px
    (25 arcsec) off for T1.
16. **Filter names** are legacy names (`gp`, `rp`, ...). The Alpaca filter
    wheel's `Names` must use the same names. T1 has R and no B. T2-T4 have B
    and no R, in a different order.
17. **M3 port selection**: the port numbers are known, but no telescope has
    a `port_action` (the PWI4 Alpaca Action name is not established), so
    `dispatch.example.json` lists no `ports`.
18. **Guide parameters** with no legacy source keep the robofast defaults:
    `fiber_radius` 3 px, `capture_radius` 8 px, `roi_size`, exposure times.

## Photometry and target vetting (`sky.example.json`)

Per port: geometry for vetting and an efficiency chain or a measured zero
point (`PortPhotometry`, docs/sky.md section 4). Values whose key ends in
`_ESTIMATED` are guesses, to be replaced by measurements.

| Value | Here | Source |
|---|---|---|
| aperture 700 mm, central obstruction 0.47 | `photometry.aperture`, `.obstruction` | legacy FITS APTDIA; CDK700 obstruction (PlaneWave: 47 % of the diameter) |
| mirrors 0.73 (3 aluminium surfaces x 0.9), optics 0.9, corrector 0.95, filter 0.85, QE 0.6 (FAU, ASI174 over Gaia G) / 0.85 (imager) | `efficiency.*_ESTIMATED` | ESTIMATED (typical values; not measured) |
| FAU beamsplitter 0.02 (the fiber gets the other 0.98) | `efficiency.beamsplitter` | JDE (2026-10-31); `PortPhotometry.with_fraction("beamsplitter", 0.98)` gives the fiber side for exposure-meter and S/N estimates |
| **T2 FAU zero point G = 19.70 +- 0.10 for 1 ADU/s** | `t2/fau` `photometry.zero_point` | MEASURED 2026-10-31 by robofast's solver on the legacy autofocus frames of night n20260423 (`n20260423.T2.FAU.autofocus.0325`, `0399`, `0671`-`0673`; 5 frames, 3-10 Gaia stars each, 4-arcsec-radius apertures, frame-to-frame rms 0.10 mag, star-to-star 0.3-0.6 mag; airmass not corrected). ADU are the stored 16-bit values (the ASI174's 12-bit counts x 16); its e-/ADU is unknown. The chain above estimates 19.66 for 1 e-/s, i.e. ~1 e- per stored ADU if the chain is right. |
| T2 FAU plate scale 0.2655 arcsec/px, **mirror-imaged** (east right with north up), PA 99-106 deg on that night | `t2/fau` `plate_scale` | MEASURED by the same solves (the legacy 0.265 is within 0.2 %); the PA follows the field rotation (ROTPOS 190.824 fixed) |

The frames' header RA/Dec are the mount's apparent (JNow) coordinates: solving
them needs a precession to ICRS first (0.15-0.4 deg in 2026).
