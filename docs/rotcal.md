# Rotator and position-angle calibration

`robofast_core/rotcal.py` measures, for each telescope and port, how the
sky lies on the detector -- the image position angle (`pa`, of the
detector's +y axis, degrees east of north) and parity (`flip`: east to the
right with north up, a mirror image) -- against the rotator's mechanical
angle, and **saves it**:

    pa = offset + sense * mechanical      (mod 360)

`sense` (+1 or -1) is the direction the rotator turns the sky. A port with
no rotator (MINERVA's FAUs) has `mechanical = 0`, so `offset` is its PA.

The legacy `calibrateRotator` / `solveRotatorPosition` measured this from
plate solves at several rotator angles (158 results over the years) but
never wrote the answer anywhere (updating the config was a TODO), so every
consumer kept its own guess. Known: T2's FAU image is mirrored with PA
99-106 deg (n20260423, `docs/sky.md` section 9).

## Measuring

`fit(measurements)`: each measurement is (mechanical angle, solved PA,
solved parity, scale). The offset is the circular mean of `pa - sense *
mechanical`; with angles spanning at least 20 deg both senses are tried and
the better kept, otherwise the sense is assumed (+1, or the configured one)
and flagged `sense_assumed`. Measurements that disagree about the parity
are refused (two ports mixed, or a bad solve). The result carries the
measured plate scale, the PA rms, the angles, when, from what, and the
measurements themselves.

Three ways to get measurements:

- **In the sequencer**: an observation of `kind="rotator_calibration"` (a
  `ROTATOR_CALIBRATION` request configuration; the target is the field):
  slew there as usual, then at each of the angles (the telescope's
  `pa_calibration.angles`, default 0/90/180/270, or the observation's
  `rotator.angles`) move the rotator (`MoveMechanical`, bounded by
  `move_timeout`, then `settle`), expose (frames saved as usual), solve
  near the target with any PA and either parity; fit; save; move the
  rotator back. A telescope without a rotator takes one measurement.
  `pa_calibrated` in the timeline.
- **`rotcal.calibrate(angles, move=..., expose=..., solve=...)`**: the same
  routine with callables, for any camera (e.g. a guide camera).
- **From FITS files already taken**:

  ```sh
  python -m robofast_core.rotcal solve --store /var/lib/robofast/pa_calibration.json \
      --telescope t2 --port fau --plate-scale 0.2655 --jnow --catalog /var/lib/robofast/catalog \
      /data/n20260423/*.fits
  python -m robofast_core.rotcal show --store ... --telescope t2 --port fau
  ```

  The header's `RA`/`DEC` (degrees or sexagesimal; else `CRVAL1/2`) are
  the solver's starting point; `--jnow` precesses them from the equinox of
  date to ICRS first (legacy MINERVA headers are JNow); `ROTMECH` (or
  `--mechanical-key`) is the mechanical angle, 0 if absent.

## The store

One JSON file for the site (`PAStore`): per telescope and port the
`current` calibration and the `history` of every one saved. It is read
fresh whenever the file changes, so a calibration saved by the sequencer
or the command line is used from the next frame on.

## Who uses it

- **The frame pipeline** (`images`, with the telescope's `pa_calibration`
  configured): each light frame's approximate WCS takes the port's PA at
  the rotator's mechanical angle (`ROTMECH`) and its parity, and the header
  gets `PACALIB` (deg) and `PAFLIP`.
- **The plate solver** (`images.solve`): with `PACALIB`/`PAFLIP` in the
  header it solves with that PA (within 5 deg) and parity -- faster, and a
  mirror image can never be mistaken for a rotation; if that fails (a
  wrong calibration) it solves again without the hint.
- **The guider** (`pa_calibration: {"store": ..., "telescope": ...}` in the
  guider service config): with no measured mount calibration for its port,
  the mount's matrix (pixels per arcsec of offset east and north) is seeded
  from the PA, parity and plate scale (`seed_mount_matrix`, the same
  geometry as the guider simulator), and the Redis telemetry gets the PA.

## Configuration

Per telescope in the enclosure config (with a `rotator` device entry for
a rotating port):

```json
"pa_calibration": {
  "store": "/var/lib/robofast/pa_calibration.json",
  "solver": {"plate_scale": 0.61},
  "catalog": {"cache_dir": "/var/lib/robofast/catalog"},
  "angles": [0, 90, 180, 270], "sense": null,
  "move_timeout": 120, "settle": 1.0, "min_measurements": 2
}
```

`solver` and `catalog` are needed only for the sequencer's routine.

## Tests

`tests/core/test_rotcal.py`: the fit (offsets, both senses, both parities,
wrap-around, one angle, mixed parity), the store, the routine on the
catalog-based sky simulator for a rotator port (offset 250 deg, sense -1)
and a flipped fixed port (PA 101.3 deg; recovered within 0.05 deg), the
command line on JNow-headed FITS frames, the sequencer (calibrate, save,
the rotator returned, the next frames' `PACALIB`/`PAFLIP` and their solved
PA agreeing to < 0.05 deg), the fallback when a calibration is wrong, the
guider's seeded mount matrix equal to the simulator's, the request type
and the service config.
