# Acquisition: which star is the target

The FAU sees ~2 % of the light (98 % goes to the fiber), so a normal short
acquisition frame shows the target and perhaps a bright companion -- rarely
enough stars to plate solve. "Take the brightest star" puts the wrong
component of a near-equal pair on the fiber. The guider therefore decides
per frame, from what it actually detects, with an explicit pass/fail test
at every step and a recorded reason (`robofast_core/guider/acquire.py`).

## The AUTO cascade

By the number of stars detected in the acquisition frame, `n` (thresholds
per site: `acquisition` in the guider config, `AcqConfig`):

| `n` | branch | how | needs |
|---|---|---|---|
| >= `solve_min` (5) | `solve` | near-blind plate solve; the star at the target's catalog position (if the solve fails: the pattern matcher, >= 3 stars) | guider `solver` + `catalog` |
| 3-4 | `triangulate` | the detected stars against the catalog pattern around the target, projected with the port's calibrated PA, parity and plate scale: a translation-only matcher, >= 3 matched | PA calibration |
| 2 | `pair` | the pair's separation vector against the catalog companion's: the target end | PA calibration |
| 1 | `single` | the only star, taken; its offset from where the pointing put the target is logged, and flagged beyond `single_flag_sigma` x the pointing uncertainty (the target may be missing and that another star) | -- |
| 0 | none | fails; optionally one longer exposure (`retry_exposure`, off by default) | -- |

A geometric match is accepted only if it is **unique** (two positions
matching as many stars: ambiguous), the matched stars' **brightness order**
agrees with the catalog's where they differ by more than `dmag_order`
(0.5 mag), and -- until the target has been identified once, when the
pointing model's uncertainty is known -- it lies within `prior_sigma` (4)
sigma of where the pointing put it. So a PA calibration that is wrong (180
deg off, or the parity flipped) shows up as "the detected stars do not
match the catalog pattern" or "the pattern matches N px from where the
pointing put it: check the PA calibration", and the target is **not
identified** -- never silently the wrong component. (An exactly equal pair
with a PA 180 deg off and no pointing uncertainty known is the one case
geometry cannot tell; robofast's pointing model supplies the uncertainty.)

The pattern is the catalog (Gaia, proper motions to the date) around the
target within the camera's reach, stars up to `pattern_dmag` (4) mag fainter
than the target, plus any `companions` the request gives. The PA, parity
and plate scale come from the port's PA calibration (`docs/rotcal.md`; the
guider's `pa_calibration`); the pointing uncertainty is the pointing
model's cross-validated rms (`docs/pointing_model.md`), sent by the
sequencer with the target.

## Per target: acquisition_config

A request configuration's OCS `acquisition_config` chooses
(`docs/request_language.md`):

- `mode`: `AUTO` (default), `BRIGHTEST`, `GEOMETRY` (the pattern matcher
  whatever `n`), `SOLVE` (OCS `WCS`), `COORDINATES` (OCS `OFF`: trust the
  pointing, take the star nearest where it put the target -- which must
  be the only one within the tolerance);
- `extra_params.fallback`: an explicit mode that fails runs the AUTO
  cascade instead (otherwise it fails: "target not identified");
- `extra_params.companions`: `[{"ra", "dec", "g"}]`, stars the catalog
  lacks (e.g. a close pair Gaia does not resolve);
- `extra_params.tolerance`: match tolerance (pixels).

## The record

Every frame's decision is in the trace (frame, mode, method, star count,
pass/fail, reason, flag): in the guider's `GET /status` (`acquisition`),
the sequencer's `guiding` timeline event (`acq_method`, `acq_stars`,
`acq_trace`), the spectrum's sidecar (`acquisition` per feeding
telescope), and the FITS headers of later frames (`ACQMETH`, `ACQSTARS`) --
so the morning report can count how often each branch is used.

## Vetting

Vetting (`docs/sky.md` section 7) predicts the branch: `n_acq`, the stars
with S/N > `acq_snr` (5) in the acquisition box at the port's exposure and
throughput, and `acq_branch`. It flags `acq_relies_on_chip_pa` when the
branch is `pair` or `triangulate` ("FAU acquisition relies on the chip PA":
the port needs a PA calibration) and `acq_target_invisible` when the target
is below detection while other stars are not (the `single` branch would
take one of them).

## Tests

`tests/core/test_acquire.py`, on the sky simulator with the FAU's 2 %
throughput and short exposures: nu Draconis (an equal pair, 62 arcsec) --
the pair branch takes nu2 where the brightest star is nu1; a 180-deg-off
PA and a flipped parity are caught; no PA calibration refuses -- a closer
unequal pair on a flipped port (and its rotated calibration refused by the
brightness order), a triple matched as a whole, a single star (and a
companion outside the field), the solve branch and its fallback, every
explicit mode and `fallback`, the trace; the guide loop putting the right
twin on the fiber (and failing with a wrong PA); the request's
`acquisition_config`; vetting's branch prediction; the sequencer's record.
