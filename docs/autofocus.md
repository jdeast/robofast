# robofast autofocus

Focusing a telescope by *computing* the focus from star sizes measured on
one known side of it, rather than by searching for the smallest star.
`robofast_core/autofocus.py` (the methods and the runner),
`robofast_core/starmeasure.py` (star sizes), `robofast_core/focusfit.py`
(the fits), `robofast_core/autofocus_plan.py` (when to focus), and the
sequencer's `kind="autofocus"` observations (design.md section 3.4).
Tests run on a simulated focuser and star field
(`robofast_core/testing/starfield.py`, `robofast_core/testing/focus_sim.py`).

## 1. Why it can be deterministic

Defocus by `dz` (microns, at the focal plane) turns a star into the image
of the pupil: a uniformly lit disk, or with a central obstruction an
annulus ("donut"), of outer diameter

    D = |dz| / N                     (N: focal ratio of the beam at the detector)

and its half-flux diameter (HFD) is

    HFD = k D,   k = sqrt((1 + eps^2) / 2)      (eps: obstruction / pupil diameter)

(half the light of an annulus lies inside radius `R sqrt((1 + eps^2)/2)`).
The focuser position `x` (steps) moves the focal plane by

    dz = (x - x_focus) * um_per_step * focal_plane_factor

`um_per_step` is the focuser's step size; `focal_plane_factor` is 1 when the
focuser moves the instrument (the CDK700 Nasmyth focusers) and larger when it
moves the secondary (about `m^2 + 1` for a secondary magnification `m`, as on
the FLWO 60-inch). In unbinned pixels of size `p`, the two arms of the
V-curve are straight lines of slope

    dHFD/dx = k * um_per_step * focal_plane_factor / (N p)    (FocusOptics.hfd_slope)
    dD/dx   =     um_per_step * focal_plane_factor / (N p)    (FocusOptics.donut_slope)

The seeing barely changes this far from focus. Convolving a large annulus
with a symmetric seeing profile spreads light inward and outward about
equally, so the half-flux radius and the outer edge's half-light point
stay where they were. The simulated HFD is within 0.5 % of `k D` once
`D >= 6 x FWHM` and within 0.2 % at `10 x`. The edge is within 0.3 % at
`D >= 6 x FWHM`. Near focus the seeing takes over and the curve rounds into
a hyperbola. So:

* points with `D >= 6 x` the worst expected seeing FWHM give the focus as
  `x - side * HFD / slope`, independent of the seeing;
* two such points on the same side also check the slope; the stars grow
  as you move away from focus, which tells you which side you are on.

**How close is close enough.** A focus error `e` steps leaves a blur of
`e * um_per_step * factor / (N p)` pixels. The tests require that blur to be
under a quarter of the seeing FWHM (`0.25 * FWHM_px * N * p` microns of
focal plane: 24 um at 1", 98 um at 4" for a CDK700 with 9 um pixels at
0.6"/px). For comparison, the diffraction depth of focus at f/6.5 is
about +-110 um.

## 2. Methods

`AutofocusConfig.method`; every run returns an `AutofocusResult` and leaves
the focuser at the new focus (or back where it started, on failure).

### V-curve (`vcurve`, the default): 2 exposures

1. Start from the predicted focus `x0`: the temperature model's, else the
   last measured focus, else the focuser's position.
2. Expose at `x0 + side * far` and `x0 + side * near` (in that order, so
   both are reached travelling toward focus). The offsets are

       near = blur_ratio * FWHM_max / donut_slope + max_start_error
       far  = far_ratio * near

   with `blur_ratio = 6` (the near blur is 6x the worst seeing's FWHM,
   `max_seeing`, default 4") and `far_ratio = 1.5`. `max_start_error` is
   how wrong the prediction may be; adding it keeps both points on the
   chosen side. `near_offset` and `far_offset` override these.
3. Measure the median HFD of the stars in each frame.
4. **Check the slope.** The secant `(HFD_far - HFD_near) / (x_far - x_near)`
   must be `side * slope` within `slope_tolerance` (20 %), using the
   calibrated slope (or the theoretical one).
   * Secant about `-side * slope`: the stars shrank as we moved away
     from the predicted focus, so we are on the **other side**. The focus
     is estimated from the other arm and the two points are taken again
     from there: 4 exposures.
   * Any other mismatch (a cloud, a different star, saturation, a blend,
     too few stars) **falls back to a sweep** (`fallback`; 2 + 9
     exposures). With `fallback: false` the run fails instead.
5. **Focus** = mean over the two points of `x - side * HFD / slope`, minus
   the calibrated arm offset (where the arm's line crosses zero, relative
   to the best focus).
6. Move there, approaching from `side`.

`vcurve_model: "hyperbola"` instead solves the arm's hyperbola
`HFD^2 = a^2 + m^2 (x - z)^2` through both points (it also returns the
seeing `a`). It is exact for Gaussian blur, but a defocused annulus is
not one: it over-corrects and amplifies slope errors, so it is not the
default.

### Donut (`donut`): 1 exposure

1. Expose at `x0 + side * d`, with `d` the larger of `far` and the offset
   at which the donut is `donut_min_ratio` (5) times the worst FWHM.
2. Measure the donut's outer diameter `D` (the radius where the
   azimuthally averaged profile falls to half its plateau, outside).
3. **Focus** = `x - side * D / donut_slope`, minus the calibrated donut
   offset. The donut slope can be computed from the optics alone, so this
   method needs no calibration (a calibration refines it).
4. If the donut is smaller than `donut_min_ratio x FWHM_max`, or there are
   too few stars, fall back to a sweep.

One exposure cannot tell which side of focus it is on: a defocused
annulus looks the same either side without aberrations. So the method
relies on `d > max_start_error`.

### Sweep (`sweep`; also the fallback): 9 exposures

`sweep_points` (9) positions evenly from `x0 + side * W` to `x0 - side * W`
(`W = sweep_half_width`, default `far`), each reached travelling toward
`-side`. Then a robust weighted fit of HFD against position:

* `sweep_fit: "hyperbola"` (default): `HFD = sqrt(a^2 + m(x)^2 (x - x0)^2)`,
  with separate slopes for the two arms, outliers clipped at 4 robust
  sigma. The focus is `x0`, and its error comes from the chi-square
  curvature.
* `sweep_fit: "parabola"`: the legacy fit with its bugs fixed. The weights
  are kept through the sigma clipping, and the vertex is the result.

The focus must lie between the points, with at least 2 points on each side
of it. If the smallest star is at the sweep's edge, the sweep is re-centred
there once (`sweep_recentre`). Otherwise the run fails. The **fit's** focus
is always used: it is never replaced by a model value or by the best single
point.

### Expected exposure counts

| Method | Normal | Wrong side | Inconsistent | + `verify` |
|---|---|---|---|---|
| vcurve | 2 | 4 | 2 + 9 (sweep) | +1 |
| donut | 1 | n/a | 1 + 9 (sweep) | +1 |
| sweep | 9 (+9 if re-centred) | n/a | fails | +1 |
| calibration | 17 | | fails | |

`verify: true` takes one more exposure at the new focus and reports its
FWHM as the seeing.

## 3. Calibration

Calibrate once per telescope, M3 port and filter, and again after anything
changes the optics (a new camera, a collimation). Ask for it like any
other autofocus, with `{"calibrate": true}`: as a plan observation
`{"kind": "autofocus", "autofocus": {"calibrate": true}, "ra_hours": ...,
"dec_deg": ..., "filter": "V", "port": "imager"}`, or as an `AUTO_FOCUS`
request configuration. Choose a field with a few isolated, unsaturated stars
and steady seeing.

The calibration is a sweep of `calibration_points` (17) over
`+-calibration_half_width` (default `1.2 x far`). It does the following:

1. fits the hyperbola, which gives the best focus `x0` and the seeing `a`;
2. on each arm, fits a straight line to the points whose geometric blur is
   at least `arm_min_ratio` (5) times the calibration's FWHM. That gives
   the arm's slope and its zero crossing relative to `x0` (`offset_plus`,
   `offset_minus`; non-zero with spherical aberration or a tilted
   detector);
3. fits the same kind of line to the donut diameters (`donut_plus`,
   `donut_minus`, and their offsets);
4. stores it in the `FocusStore` (a JSON file, `store`) under
   `telescope/port/filter`.

Compare the measured slopes with `FocusOptics.hfd_slope`
(`details.theory_slope` in the result). A large difference means the
optics are not configured right: the focal ratio, the pixel size, or the
focuser's units.

## 4. Focuser units, direction and backlash

Everything is in focuser positions (steps), so the methods do not need to
know which way the focus moves the focal plane:

* `side` (+1 or -1) is the side of focus used, *in focuser positions*.
  Every move ends travelling toward `-side`. When a move would otherwise
  arrive from the other direction, it first goes `backlash` steps past the
  target on `side`. Backlash in the focuser, or play in the mechanism,
  then never shifts the result (`test_backlash_is_taken_out...`).
* Only `|um_per_step * focal_plane_factor|` enters the slopes. A focuser
  that counts the other way, or a manual that has the direction backwards,
  changes nothing. If the manual has the *units* wrong, the calibration
  shows it (measured slope against `theory_slope`).
* TCSGO (the FLWO 60-inch): its focus is in microns of secondary travel.
  The server reports it as steps offset by `focus_min`. Set `um_per_step: 1`
  and `focal_plane_factor` to the secondary's factor, and calibrate. The
  manual may be wrong about focus, so the calibration's measured slope is
  the authority. TCSGO has no Halt; a focuser timeout just raises.
* Limits: `min_position` and `max_position` (default 0 and `MaxStep`). A
  position outside them fails the run before the focuser moves.
* Each move is bounded by `move_timeout`. If it times out the runner tries
  `Halt` and raises `FocuserError`. A move that ends more than
  `position_tolerance` steps from its target also raises `FocuserError`.
  In the sequencer a device error is the telescope's error: back off, then
  give up after repeated errors.

## 5. Star measurement

`robofast_core/starmeasure.py`, numpy only:

1. Background: clipped means of tiles, interpolated bilinearly, computed
   twice, the second time with the sources masked. A median of integer,
   Poisson-noisy pixels can be off by half an ADU, and a large donut
   aperture turns that into a large HFD error.
2. Detection: smooth the image (sigma about a quarter of the expected star
   size, so a donut is one detection), threshold it, and find connected
   pieces (run-length union-find).
3. Rejection, counted by reason: `saturated` (any aperture pixel at or
   above `measure.saturation`), `edge` (the aperture leaves the image or
   `region`), `blended` (another real source's light reaches the
   aperture), `faint` (S/N), `elongated` (moment ellipticity), and `small`.
4. Per star: the aperture is shrunk to where the star's light ends (plus
   a margin for the wings), with a local background annulus. The star gets:
   * **HFD** from a sub-pixel growth curve;
   * **FWHM** from adaptive Gaussian moments;
   * the **donut edge** diameter.
5. The field: medians, and the standard error of the median.

Settings go in `measure` (`MeasureConfig`). The runner sets `smooth` and
`background_box` from the expected star size unless given. `region` limits
the field (`x0, y0, x1, y1`). Set `saturation` near the camera's
non-linearity.

## 6. Temperature model

```json
"model": {"terms": {"{tel}.focuser.temperature": -52.94},
          "reference": {"{tel}.focuser.temperature": 8.95}}
```

    focus = zero_point + sum_i c_i (v_i - v_i0)

The `v_i` are value-registry names (`{tel}` becomes the telescope's name).
A value that is missing or stale counts as its reference value: the term
drops out, and the result lists it. The model only **predicts the
starting point**. Each successful autofocus re-anchors the zero point on
the measured focus, kept in the store per telescope, port and filter, so
the next prediction starts there. The model **never** replaces a measured
focus. A failed run changes nothing.

The legacy MINERVA model was
`F0 + C0 (T_M1 - T0) + C1 (dT - dT0) + C2 (sin alt - G0)`. Its terms come
over once the primary's temperature and the other inputs are in the registry
(design.md section 7, item 4). Until then the focuser's own temperature
stands in. The sequencer publishes it as `{tel}.focuser.temperature` before
each run.

## 7. In the sequencer

* **Observations.** `Observation(kind="autofocus", ra_hours=..., dec_deg=...,
  filter=..., port=..., autofocus={...overrides...})`. The sequencer runs it
  like any light observation: lease, port, filter, slew, all under the
  generation's grants. The focuser grant is fenced with the others. It then
  runs `Autofocus.run()`, or `calibrate()` for `{"calibrate": true}`, with
  the telescope's `AutofocusSetup`: the base config plus overrides by
  `port/filter`, the model and the store. A telescope's `autofocus` entry
  in the enclosure config enables this, and needs its `focuser`.
* **Requests.** A request configuration with `"type": "AUTO_FOCUS"` (as in
  the LCO OCS) and one light instrument config becomes such an observation
  (its filter and port; a positive exptime overrides the autofocus
  exposure time).
* **Triggers.** `autofocus_triggers` in the enclosure config wraps the plan
  in `AutofocusTriggers`. That inserts an autofocus, on the field, filter
  and port of the next light observation, without taking it from the plan:
  * `start_of_night`: no success yet tonight;
  * `temperature_change`: more than this many degrees C since the last
    success (`temperature_value`, default `{tel}.focuser.temperature`);
  * `fwhm_degradation`: the image pipeline's FWHM (`{tel}.image.fwhm`,
    published when `images.measure_fwhm` is on) above this factor times
    the FWHM just after the last autofocus;
  * `max_interval`: seconds since the last success.

  A failed autofocus is not retried for `retry_after` (30 min). The
  telescope observes on at its old focus meanwhile.
* **Failure is not an error.** An autofocus that finds no focus is
  reported `failed`, with no backoff or park, and the telescope observes
  on. A focuser *device* error is the telescope's error, as usual.
* **Interruptions.** If the autofocus is cut short (a preemption, the
  lease revoked), the focuser may be left defocused. The sequencer keeps
  the position from before the run and moves back to it before that
  telescope's next light observation (event `focus_restored`). A
  `now-after-readout` preemption waits for the whole autofocus, which is a
  few exposures.
* **Results.**
  * Registry values `{tel}.autofocus.*` go into the header of every later
    frame: `AFFOCUS`, `AFFOCERR`, `AFMETHOD`, `AFOK`, `AFNEXP`, `AFTIME`,
    `AFSEEING`.
  * The focus frames are saved with `IMAGETYP = 'FOCUS'`, and `FOCPOS`
    comes from the focuser.
  * Timeline events: `autofocus_start`, `autofocus_point`,
    `autofocus_wrong_side`, `autofocus_fallback` and `autofocus_done`.
  * The plan record gets an `autofocus` entry when the plan keeps one.
  * The store keeps a history of the last 200 runs.

Example: `sites/minerva/autofocus.example.json`.

## 8. Configuration reference

`AutofocusConfig` (the `autofocus` entry; `overrides` keyed `port/filter`,
with `-` for any):

| Field | Default | Meaning |
|---|---|---|
| `optics` | required | `f_ratio`, `pixel_size` (um), `pixel_scale` ("/px), `obstruction`, `um_per_step`, `focal_plane_factor` |
| `method` | `vcurve` | `vcurve`, `donut` or `sweep` |
| `side` | +1 | side of focus used and approached from (focuser positions) |
| `exptime`, `binning`, `roi` | 5 s, camera's, camera's | focus frames; binning and subframe are restored afterwards |
| `max_seeing` | 4" | worst seeing to plan the offsets for |
| `max_start_error` | 0 | how far off the prediction may be (steps) |
| `blur_ratio`, `far_ratio` | 6, 1.5 | near blur / worst FWHM; far / near offset |
| `near_offset`, `far_offset` | derived | explicit offsets (steps) |
| `slope_tolerance` | 0.2 | allowed secant / slope mismatch |
| `vcurve_model` | `linear` | or `hyperbola` |
| `fallback` | true | sweep when the V-curve or donut is inconsistent |
| `donut_offset`, `donut_min_ratio` | derived, 5 | donut offset; least D / worst FWHM |
| `sweep_points`, `sweep_half_width`, `sweep_fit`, `sweep_recentre` | 9, `far`, `hyperbola`, 1 | the sweep |
| `calibration_points`, `calibration_half_width`, `arm_min_ratio` | 17, 1.2 `far`, 5 | the calibration |
| `backlash` | 0 | overshoot before approaching from `side` (steps) |
| `min_position`, `max_position` | 0, `MaxStep` | limits |
| `position_tolerance`, `move_timeout`, `poll_interval` | 2 steps, 120 s, 0.25 s | moves |
| `readout_margin`, `download_timeout` | 60 s, 120 s | exposures |
| `min_stars` | 3 | fewest measured stars per frame |
| `measure` | {} | `MeasureConfig` fields (`saturation`, `region`, ...) |
| `verify` | false | one more exposure at the new focus |

Also in the entry: `model` (above), `store` (the JSON file, relative to
the config file) and `overrides`.

## 9. Accuracy in simulation

Tests: `tests/core/test_autofocus.py`, `test_autofocus_sequencer.py`,
`test_starmeasure.py` (fast tier), and `tests/sim/test_sim_autofocus.py`
(OmniSim's focuser and camera). The scene is a 512x512 camera at 1"/px,
with 13.5 um pixels, f/6.5, a 47 % obstruction and 1 um of focal plane per
step. It has six stars and Moffat seeing. The calibration was made in
2" seeing. The prediction is off by up to 300 steps either way.

Each row below is ten fields. Errors are in steps (= um of focal plane).
The tolerance is a quarter of the seeing FWHM of blur.

| Method | Seeing | RMS error | Max error | Tolerance | Exposures |
|---|---|---|---|---|---|
| vcurve | 1" | 1.7 | 3.1 | 21.9 | 2 |
| vcurve | 2" | 1.6 | 2.8 | 43.9 | 2 |
| vcurve | 3" | 5.4 | 6.7 | 65.8 | 2 |
| vcurve | 4" | 13.9 | 18.0 | 87.8 | 2 |
| donut | 1" | 4.8 | 6.9 | 21.9 | 1 |
| donut | 2" | 2.8 | 3.9 | 43.9 | 1 |
| donut | 3" | 1.5 | 3.0 | 65.8 | 1 |
| donut | 4" | 1.9 | 3.3 | 87.8 | 1 |
| sweep | 1" | 3.2 | 5.1 | 21.9 | 9 |
| sweep | 2" | 4.2 | 6.2 | 43.9 | 9 |
| sweep | 3" | 5.1 | 8.0 | 65.8 | 9 |
| sweep | 4" | 5.3 | 8.0 | 87.8 | 9 |

The V-curve's error grows with the seeing. At 4" the near point
(`D = 6 x FWHM`) still carries about 2 % of seeing excess in its HFD,
which shows up as a bias of about 15 steps. That is a sixth of the
tolerance. Raise `blur_ratio` to trade it for larger donuts.

## 10. Differences from the legacy autofocus

* `newauto.py` refitted without the weights in its sigma-clipping loop. Its
  "best measured focus" fallback and the model value
  (`guessFocus`) could overwrite the last good focus. Its failure e-mail
  sat after a `return`, so it was never sent. Here:
  * the fit keeps its weights;
  * a failed run returns to where it started, and changes neither the
    model nor the stored focus;
  * a failure is reported to the plan and the timeline.
* `autofocus.py` (2021, not live) and the TRES guider's autofocus (J.
  Eastman, J. Kansky) were parabola sweeps. The sweep here is their fixed
  descendant. The V-curve and donut methods are new.

## 11. Open questions

1. The CDK700 obstruction, pixel size and focuser `StepSize` as PWI4
   reports them. Then a calibration per port and filter on the sky.
2. The 60-inch's `focal_plane_factor` and the direction of its focus, once
   TCSGO's focus units are confirmed (docs/tcsgo.md).
3. The legacy focus model's M1 temperature, delta-T and gravity terms need
   those values in the registry.
4. Field curvature and tilt: the HFD is a median over the field. A `region`
   per port, or per-star focus maps, if the FAU or the imager needs them.
