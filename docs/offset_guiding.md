# Offset guiding

On a fiber spectrograph the guider normally guides on the target itself.
What the guide camera sees of a target on the fiber depends on the
instrument (`on_target` in its guiding model):

- **a mirror round the fiber** (`annulus`; TRES): the fiber takes the core
  of the star, so the guide camera sees only the light that *spills* round
  the fiber hole, as a ring. The **annulus** centroider (`docs/guider.md`)
  finds the star's centre from that ring;
- **a beamsplitter** (`beamsplitter`; MINERVA's FAU): a beamsplitter in
  front of the fiber sends a small fraction of all the light (about 2 % by
  design) to the guide camera. There is no hole: the camera sees the whole
  star, and a plain centroider (the windowed centre of mass) guides on it.

For a faint target that light is too weak to centroid, or to centroid
often enough. **Offset guiding** guides on a brighter star nearby instead.
It holds that star at the pixel that puts the target on the fiber.

Everything here runs in the simulators only. Nothing has guided a real
telescope by an offset star yet.

## How it works

```
setpoint = fiber + S^-1 @ (east, north)
```

- `fiber` is the fiber's pixel on the guide camera, located with the
  backlight or configured.
- `(east, north)` is the guide star's position relative to the target, in
  arcsec on the sky, at the time of the observation.
- `S` is the guide camera's sky matrix: its plate scale, PA and parity, in
  the PA calibration's FITS convention (`docs/rotcal.md`,
  `calibration.fits_sky_matrix`).

The guide loop holds the guide star at `setpoint`. Whatever moves both
stars together (seeing tilt, drift, the mount) is corrected, so the target
stays on the fiber. The controller is the same, and so are the tip/tilt,
the offloading and the loss and re-acquisition logic. Because the guide
star is clear of the fiber hole, the centroider is a plain one: the
windowed centre of mass replaces `annulus`.

Acquisition looks for the **guide star**, not the target. The catalog
identifier or the acquisition cascade (`docs/acquisition.md`) is given the
guide star's coordinates, and its predicted pixel is the setpoint.

`robofast_core/guider/offset.py` handles the geometry. It also covers the
effects that change the offset:

| Effect | Size (60" offset) | Handling |
|---|---|---|
| Proper motion | up to arcsec over years | Both stars are moved to the date of observation (`catalog.propagate`). An explicit offset moves with the target. |
| Differential refraction | ~0.1" at 30 deg altitude, ~0.02" at 60 deg | Computed from ERFA's `refco` constants: along the vertical `(A sec^2 z + 3B tan^2 z sec^2 z) x` the vertical offset, across it `A x` the offset. It is logged and applied, unless the guider config's `offset_guide.refraction` is false. It needs `site`, which the dispatch scheduler sends. |
| Field rotation | depends on the port | The setpoint is recomputed every `offset_guide.update` seconds (default 10). `field_rotation: "none"` is for an equatorial mount (the 60-inch) or a derotated port (MINERVA's FAU rotator runs in derotate mode). `"altaz"` with `sense` is for an alt-az port without a derotator: the PA then follows the change in parallactic angle since the start. |
| Plate scale, PA errors | 0.1 % gives 0.06", 0.1 deg gives 0.1" | These are systematic, so prefer close guide stars. |

Each update moves the setpoint without resetting the controller, as
`POST /target` does. A target step therefore gives no derivative kick.

## Asking for it

A request configuration does it through `guiding_config.extra_params`:

```json
"guiding_config": {"mode": "ON",
                   "extra_params": {"guide": "offset",
                                    "offset_star": {"east": 25.0, "north": -10.0}}}
```

- **`guide`** is `"annulus"`, the default (guide on the target itself),
  `"offset"`, or `"auto"`: the dispatch scheduler applies the
  recommendation (below). That needs the target's magnitude in the guide
  camera's band, **`target_mag`** in the same `extra_params`.
- **`offset_star`** is one of:
  - `"auto"`, the default: the guider picks the star from its catalog, as
    described below;
  - `{"east", "north"}`: the guide star's position relative to the target,
    in arcsec on the sky with east as a true angle. It is fixed relative to
    the target;
  - `{"ra", "dec", "pmra"?, "pmdec"?, "epoch"?, "g"?}`: deg ICRS, mas/yr
    and Julian year (default 2000.0), with Gaia G optional.

The dispatch scheduler copies these settings into the observation's
`guide_offset`, together with the target's astrometry and the site. Its
decision record carries them (`"guide": {"on": "offset", ...}`). The
sequencer passes them to the guider's `POST /start` as `offset_guide`. A
static plan can set `guide_offset` on an observation directly.

### MINERVA's sheet

`acquisition_offset_north` and `acquisition_offset_east` map to offset
guiding (`docs/minerva_sheet.md`). Legacy's offset is **the target minus
the guide star**, in arcsec on the sky (no cos dec), so `offset_star` is
`{"east": -east, "north": -north}`. Either column non-zero turns it on.
Legacy needed both to be non-zero, so a pure north or east offset was
silently ignored.

Each offset is checked against the star catalog when the sheet is
imported: a star of sensible brightness must be at the target plus the
offset, proper motion applied to both, or the row is refused (or flagged)
with where the nearest star is (`docs/minerva_sheet.md`, "Acquisition
offsets"). The check is `check_offset_star` in
`robofast_core/guider/offset.py`, for any explicit `{"east", "north"}`.

## When to offset guide

`robofast_core/guider/recommend.py` predicts the error of each method from
photon statistics and recommends one, with the reason. Its inputs are:

- the target's magnitude;
- the sky (mag/arcsec^2);
- the seeing (FWHM);
- the instrument's `GuideModel`: aperture, fiber diameter, guide camera
  plate scale, read noise, dark, throughput (QE included), the longest
  guide exposure, the loop's `kp`, the zero point of the guide band, how
  the camera sees the target (`on_target`, `light_fraction`) and the
  slowest acceptable correction rate (`min_rate`).

**On the target, `annulus`.** The spill of a Gaussian star of rms `s`
centred on a fiber of radius `r` is `exp(-r^2/2s^2)` of its light. Its
centroid error per axis is `annulus_factor x sqrt(s^2 + r^2) / SNR`.

**On the target, `beamsplitter`.** The whole star, with `light_fraction`
of the electrons an off-fiber star of the same magnitude gives (1 when, as
at MINERVA, the whole guide field is behind the beamsplitter and
`throughput` already includes it). Its centroid error per axis is
`com_factor x s / SNR`. Unlike the spill, it does not grow with the
seeing.

**Offset.** The guide star's centroid error is `com_factor x s / SNR`.
The offset's systematic error is added in quadrature:

```
separation x scale_error,  separation x pa_error,  refraction_residual,  flexure
```

**Both.** A floor of `centroid_floor` pixels is added in quadrature. A
loop of gain `kp` turns the centroid error into a tracking error of
`sqrt(2 kp/(2 - kp))` times it, rms in both axes. A centroider does not
find a star whose SNR is below `min_snr`. For `auto` the star is unknown,
so its `star_g` and `separation` are assumed; a given star's `g` replaces
the assumed one.

**Cadence.** Each method's achievable correction rate is `1/t`, with `t`
the shortest exposure (up to `exposure`) at which its star is found and
its error is within `max_error`. A method slower than `min_rate` (Hz; 0:
no requirement) does not work: a target seen only in long exposures
cannot be guided on often enough to follow the image motion.

**The choice.**

1. On the target, if it works (found, within `max_error`, at least
   `min_rate`) and is no worse than `prefer_annulus` times the offset's
   error. Guiding on the target needs no second star and has no systematic
   error.
2. Otherwise offset, if it works. So offset guiding is chosen only when the
   target is too faint to see at the light the guide camera gets of it,
   or too faint to guide on often enough; the reason says which.
3. Otherwise the lesser evil, and the reason says "neither works".

The method's key stays `annulus` (the request language's word for guiding
on the target, `docs/request_language.md`); the recommendation's
`on_target` and its reason say what the guider actually sees.

**Calibrated in the simulator** (`tests/core/test_guide_recommend.py`):

- The guider's own centroiders were run on simulated frames: a star on
  the fiber hole for annulus, a star in the clear for offset, with photon,
  sky and read noise. The windowed centre of mass follows `s / SNR`
  (`com_factor` 1). The annulus fit, which knows the seeing width, beats
  the ring estimate by about 3 (`annulus_factor` 0.3). Both fail to find
  the star near SNR 5 (`min_snr`).
- The test then runs the **closed guide loop** on simulated frames over a
  magnitude grid, for both methods. The predicted tracking error agrees
  with the true error of the target against the fiber to within a factor
  of 2. The model's "not found" is exactly where the guider cannot acquire
  or hold the star. The choice matches the method the simulated loop does
  better with.
- The same is run for a **beamsplitter**: the whole target at 2 % of the
  light, no hole (`Fiber.behind_beamsplitter` in the simulator), the
  windowed centre of mass. The predicted tracking error again agrees to
  within a factor of 2, and "not found" is where the guider cannot hold
  the star.

**The thresholds are per instrument**, in the dispatch config, under
`instruments.<name>.guiding`:

- MINERVA's KiwiSpec: `sites/minerva/dispatch.example.json`;
- TRES: `sites/flwo60/dispatch.example.json`.

The thresholds are `max_error` (KiwiSpec 0.3", TRES 0.25"), `min_snr` (5),
`min_rate` (KiwiSpec 0.1 Hz, proposed; TRES none) and `prefer_annulus`
(1.5). Each hardware value carries its source in the config's `_note`:

| Value | MINERVA (KiwiSpec, FAU) | TRES |
|---|---|---|
| `on_target` | `beamsplitter`: no fiber hole; ~2 % of the light by design | `annulus`: a mirror round the fiber |
| `fiber_diameter` | 2.27": the 50 um core at 22 um/arcsec (Swift et al. 2015, JATIS 1, 027002, sec. III.3) | 2.3": the medium fiber, 100 um at f/6 (Fűrész 2008, PhD thesis, sec. 2.1.3); the new front end has only this fiber (MEDIUM, R~44,000) |
| `plate_scale` | 0.265"/px, confirmed from the FAU frames' FITS WCS | 0.161"/px, tres-guider `config/zyla.ini` (Andor Zyla 4.2P) |
| `exposure` | 10 s, the longest (FAU frames use 5-15 s) | 1 s (assumed) |
| `read_noise` | 6 e-: ZWO ASI174MM datasheet at gain 0 (the legacy code runs gain 0); about 3.9 e-/ADU there. Datasheet estimates, consistent with the measured 0.9-1.5 ADU frame noise | 1.4 e- (assumed) |
| `throughput` | 0.0008, **provisional**, with `zero_point` 8.1e9 (V-band photons) and `light_fraction` 1: one star, T1, night n20230306, ~500 e-/s at V 8.93 in 5 s frames (1 e-/s at V ~15.7), FWHM 1.2-1.7". An effective 0.08 %, beamsplitter included, far below the 2 % design | 0.1 (assumed) |

With these, MINERVA guides on the target to about V 11.0 and offset
guides from 11.5 (the target is no longer seen in 10 s); with the assumed
offset star (G 10.5) the offset star itself needs ~5 s exposures. TRES
guides on the spill to about G 16.0 and offset guides from 16.5.

**For `guide: "auto"`** the dispatch scheduler applies the recommendation
when a visit starts:

- the sky is the model's dark sky, brightened by the Moon's illuminated
  fraction while it is up (`sky_full_moon`);
- the seeing is the plan's seeing source (docs/seeing.md): by default the
  guiders' live FWHM, the median of the last 30 minutes; with nothing
  recent, the model's `seeing`.

The decision record's `guide` entry carries the choice, the
recommendation (both predictions and the reason) and the `seeing` used
(its value, source, samples and age). Without a model or a `target_mag`,
`auto` guides on the target, and the record says why.

From the command line:

```
python -m robofast_core.guider.recommend --config sites/flwo60/dispatch.example.json --instrument tres --grid 10:18:1
python -m robofast_core.guider.recommend --config sites/minerva/dispatch.example.json --mag 13 --sky 19 --seeing 2.5 --json
```

## Choosing the guide star (`"auto"`)

`choose_guide_star` uses the guider's `catalog` (Gaia DR3 and the bright
stars, `docs/sky.md`). It searches the guide camera's field round the
target, at the date of observation. It keeps stars that:

- are brighter than `g_faint` and fainter than `g_bright`, because
  brighter stars saturate the guide exposure. The camera and exposure set
  this, so set it per guider;
- are at least `min_separation` arcsec from the target, which keeps them
  clear of the fiber hole and the target's spill;
- put the setpoint on the camera, with `edge_margin` pixels to spare;
- have no catalog star brighter than their own G + `isolation_dmag`
  within `isolation` arcsec.

Of these it takes the one with the smallest `separation /
separation_scale + max(0, G - g_good)`. Close keeps the systematic errors
small, and bright keeps the centroid noise small. The rejected candidates
are logged with the reason. If no star qualifies, the start is refused
(409) and the guider says why.

## The guider service

The guider's config takes these settings (all optional):

```json
"offset_guide": {"update": 10, "refraction": true, "field_rotation": "none",
                 "sense": 1, "wavelength": 0.65,
                 "auto": {"g_bright": 7.0, "g_faint": 14.5, "g_good": 11.0,
                          "min_separation": 8, "edge_margin": 40, "isolation": 6,
                          "isolation_dmag": 3, "separation_scale": 60}}
```

It also needs:

- the plate scale (`guide.plate_scale` or the PA calibration's);
- the port's PA, from `pa_calibration` or `offset_guide.pa` at start;
- the fiber pixel, configured or located.

The start is refused if any of them is missing.

`GET /status` reports:

- `guide_mode` (`"offset"` or `"target"`);
- `fiber`, the fiber pixel even while the setpoint is elsewhere;
- `offset_guide`: the star, the scale and PA, and `now`, the latest
  solution: the offset east/north, the altitude and parallactic angle, the
  refraction shift, the PA, and the pixel offset and setpoint.

A plain `POST /start` afterwards guides on the target at the fiber again.

## In the FITS headers

`GuiderRemote` copies these values into the sequencer's registry, so they
land in the headers of the frames:

| Keyword | Value |
|---|---|
| `GUIDMODE` | `offset` or `target` |
| `GSSOURCE` | `offset`, `coordinates` or `catalog` |
| `GSRA`, `GSDEC` | the guide star, deg ICRS, at its epoch |
| `GSMAG` | its Gaia G, when known |
| `GSID` | its Gaia DR3 source_id (`catalog`) |
| `GSOFFE`, `GSOFFN` | the guide star minus the target, arcsec, now |
| `GSPIXX`, `GSPIXY` | the setpoint (guide-camera pixels) |
| `GSDAR` | the differential refraction across the offset, arcsec |

`XSCIFIB`/`YSCIFIB` stay the fiber's pixel. The sequencer's `guiding`
event also records `guide_mode`, `guide_star` and `guide_offset`.

## Legacy

- **MINERVA (`minerva-control`).** `control.py` turned the sheet's offsets
  into pixels with the rotator PA, but used them only to *choose* which
  star `zwo.py` reported as the guide star, as "the star at brightest +
  offset". A scalar `np.linalg.norm` over all the stars made that choice
  always star 0, so the selection most likely never worked. The setpoint
  stayed on the fiber, so the chosen star, not the target, would have been
  guided onto the fiber. There was no proper motion, refraction or cos dec
  handling.
- **tres-guider.** It moved the setpoint by a pixel offset from the fiber,
  but searched for the star at the opposite sign and assumed `PA = 0`.

## Tests

`tests/core/test_offset_guiding.py` covers:

- **Geometry:** the pixel offset inverts the sky matrix, offsets are true
  angles, proper motion, refraction (size, direction, and the shrink
  across the vertical), and field rotation.
- **Choosing a star:** saturation, crowding, off-camera stars, and the
  trade between bright and close.
- **The guide loop, on a simulated TRES-like faint target with a fiber
  hole.** Guided by a star 25" east and 15" north, the target stays within
  0.25 px rms of the fiber while the image drifts ~5 px in 20 s. Guiding on
  the target itself fails to acquire. With field rotation of 0.05 deg/s
  the setpoint updates keep the target on the fiber, and a stale setpoint
  drags it off.
- **The HTTP service and its FITS values.**
