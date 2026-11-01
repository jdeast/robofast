# robofast's pointing model

`robofast_core/pointmodel.py` gives each telescope (and each M3 port) a
pointing model of robofast's own, built from plate solves, applied before
the mount, and watched for drift. It replaces the legacy
`cdk700.makePointingModel` / `addPointToModel` runs, whose models lived
only inside PWI. The TPOINT terms, their formulae and the least-squares fit
are in `robofast_core/pointing.py` (`docs/sky.md` section 2).

## 1. Samples

A sample is one pointing measurement: the **mount position** (what the
mount was told and reports, in the coordinates the sequencer commands; with
robofast's model on, that is the *commanded* position, not the target),
the **true position** (the plate-solved optical axis, ICRS) and the local
sidereal time at mid-exposure. Samples come from

- a **pointing run** (section 3): `source = "run"`;
- **every solved light frame** (`images.solve`, `docs/sky.md` section 6):
  `source = "frame"`, if the solve matched at least `min_frame_matches`
  stars (6) with an rms under `max_frame_rms` (2 arcsec). The frame
  solver adds `axis_ra`/`axis_dec`, `mount_ra`/`mount_dec`, `lst` and the
  port to the frame index's `solve` entry for this.

Every sample is appended to `<store>/samples/<telescope>/<port>/samples.jsonl`.

## 2. Building a model

`build(samples, mount=..., latitude=...)`:

1. **Outliers**: a fit with every candidate term; points more than 5 robust
   sigma (and 2 arcsec) off are rejected (a misidentified field, a slew
   that had not settled), up to three passes.
2. **Term selection** (`select_terms`), as a TPOINT user would: start from
   the index terms (always kept) and the basic geometric set
   (equatorial `CH NP MA ME TF`; alt-az `CA NPAE AN AW TF`); drop the least
   significant until every term left is `significance` (4) sigma from zero
   (two terms the sky coverage cannot separate lose one, then the other
   stands); then add any other term (`FO DAF HCES HCEC DCES DCEC`;
   `TX ACES ACEC ECES ECEC`) that is that significant *and* lowers the
   Bayesian information criterion; prune again.
3. **Cross-validation**: the k-fold (5) rms of points left out of the fit,
   the model's honest accuracy (`cv_rms`), next to the in-sample rms.

The result (`BuiltModel`) carries its provenance: when it was built, the
number of points by source and their time span, the rejected count, the
terms with formal errors, rms before / after / cross-validated, and a
note. `ModelStore` writes it to
`<store>/models/<telescope>/<port>/current.json` and keeps every model
as `model-<UTC>.json` beside it.

From logged samples (e.g. a month of solved frames):

```sh
python -m robofast_core.pointmodel build --store /var/lib/robofast/pointing_t1 \
    --telescope t1 --port 1 --latitude 31.680407 [--since 2026-10-01] [--source frame]
python -m robofast_core.pointmodel show --store /var/lib/robofast/pointing_t1 --telescope t1 --port 1
```

## 3. The pointing run

A `POINTING_MODEL` request configuration (`docs/request_language.md`; in
the plan, an observation of `kind="pointing_model"`) is placed by the
scheduler like any other request -- in twilight, or a cloudy-but-safe gap,
by its windows and constraints. Its instrument config gives the number of
points (`exposure_count`), the exposure time and the port; its target only
says when it is schedulable (choose one near the zenith at the intended
time): the run covers the whole sky.

The sequencer (`_pointing_run`):

1. switches the vendor model off if configured (`vendor_off`);
2. lays `count` points on the sky between `min_alt` and `max_alt` (a
   Fibonacci lattice: equal area, in azimuth bands for short slews),
   avoiding the Moon by `moon_distance` deg and, for an equatorial mount,
   outside `max_hour_angle`/`min_dec`/`max_dec`;
3. for each point (converted to RA/Dec at that moment's sidereal time):
   asks the slew guard, slews **without** robofast's model, settles,
   reads the mount's position, exposes (the frame is saved as usual),
   plate-solves near the mount position (`solver`, `catalog`) and records a
   sample; an unsolved point is skipped;
4. with at least `min_points` solved, builds a model, saves it, and uses it
   from the next slew on (`pointing_model_built` in the timeline, with the
   terms and the rms).

Run settings (`pointing.run` in the config, overridable per observation):
`min_alt` 25, `max_alt` 85, `moon_distance` 15, `max_hour_angle`,
`min_dec`, `max_dec` (none), `settle` 2 s, `min_points` 12, `significance` 4.

## 4. Using it

With a model for the port, every slew is commanded through it: the target
(true position) goes forwards through the model to the mount position that
lands on it (`pointing_applied` in the timeline, with the offset). The
frame pipeline reads the mount's position at each exposure and puts it
through the inverse: `RA`/`DEC` (and the approximate WCS) are where the
telescope really pointed; `MNTRA`/`MNTDEC` are what the mount said.

`vendor_off: {"action": ..., "parameters": ...}` is an Alpaca `Action` sent
once per generation, before the first slew, to switch the mount
software's own model off, so the two never stack. Leave it out where the
vendor model is kept (then robofast's model fits what is left of the
error, and the run must be made in the same vendor state).

`enabled: false` measures (samples, drift) without changing any command:
the way to start on a telescope whose vendor model is still in use.

### Rolling local correction

After LCO: `local_correction` keeps the last acquisition's residual (the
first unguided solved frame after a slew: its solved axis minus the
target, on the sky, plus whatever local correction that slew had applied)
and applies it to the next slew, decayed as `exp(-sep / scale - dt / tau)`
(`scale` 15 deg, `tau` 3600 s; offsets over `max_offset`, 300 arcsec, are
ignored). It soaks up what the model misses locally (flexure on one side
of the pier, a recent shift) between rebuilds.

### Drift

Every frame sample's residual against the current model goes into
tonight's drift monitor for its port. Once there are `min_samples` (8),
a median offset larger than `threshold` (default the larger of `factor` (5)
times the model's cross-validated rms and `floor` (5 arcsec)) raises an
alarm, once a night: in the timeline (`alarm`), in the sequencer's status
snapshot (`telescopes.<t>.alarms`), and so on the observer console and the
notifier. `{tel}.pointing.residual` in the value registry is the last
frame's residual. `night_summary()` gives the night's statistics per port
for the morning report.

## 5. Configuration

Per telescope in the enclosure config:

```json
"pointing": {
  "latitude": 31.680407, "longitude": -110.878977, "mount": "equatorial",
  "store": "/var/lib/robofast/pointing_t1",
  "enabled": true,
  "vendor_off": {"action": "...", "parameters": "..."},
  "local_correction": {"scale": 15, "tau": 3600},
  "drift": {"min_samples": 8, "factor": 5, "floor": 5},
  "solver": {"plate_scale": 0.61, "search_radius": 0.25},
  "catalog": {"cache_dir": "/var/lib/robofast/catalog"},
  "run": {"min_alt": 25, "min_points": 12}
}
```

`solver` and `catalog` are needed only for pointing runs (frame samples
use the frame pipeline's solver). PWI4's command to switch its model off
is to be confirmed on the CDK700s before `vendor_off` is set there.

## 6. Tests

`tests/core/test_pointmodel.py`: term selection recovers injected terms
(equatorial and alt-az, the basic set and extras, nothing invented from
noise), outlier rejection, cross-validation, the store and sample log, the
command line, the local correction, the drift alarm, the grid; and a
pointing run on the catalog-based sky simulator
(`robofast_core/testing/skysim.py` with an all-sky synthetic catalog,
`robofast_core/testing/pointing_sim.py`) with injected TPOINT truth:
pointing errors of ~140 arcsec rms drop to ~0.02 arcsec (the solver's
noise), the injected terms recovered within 1 arcsec.
`tests/core/test_pointmodel_sequencer.py`: the whole loop in the
sequencer with the frame pipeline (run, model, the next target on the
axis to < 1 arcsec, headers), the drift alarm reaching the status
snapshot after an injected 60 arcsec shift, the request type and the
service config.
