# Twilight sky flats

`robofast_core/skyflats.py` (legacy `control.py` `doSkyFlat`). An
observation of `kind="sky_flat"` -- a `SKY_FLAT` request configuration,
one instrument config per filter with its `exposure_count` -- takes flats of
the twilight sky. They need the enclosure open (a light observation under
the lease); with the roof closed there are none.

- **Where**: the flattest twilight sky, 75 deg high opposite the Sun
  (`flat_spot`), dithered in a spiral of `dither` arcsec so stars median out.
- **How long**: the twilight sky fades ~1.2 mag per degree of the Sun's
  depression (`photometry.sky_brightness`). Each exposure is predicted from
  the model's rate at the Sun's altitude at mid-exposure (the Sun moves
  during the exposure and its `overhead`), scaled by what the previous frame
  measured (its median minus `bias`), to reach `target_adu`. The first guess
  is `rate_at_minus6` (ADU/s for sensitivity 1 with the Sun at -6 deg); a
  frame clipped at `saturation` only teaches "brighter" (x4).
- **Order**: filters by `sensitivity` (relative count rate on the same
  sky): in the evening the least sensitive first, in the morning the most
  sensitive first.
- **Stops**: a filter is done with `count` good frames, or when its
  predicted exposure leaves `[min_exptime, max_exptime]` the way the sky is
  going (too faint in the evening, too bright in the morning); the other way
  it waits (`wait`, at most `max_wait` per filter). Frames above `max_adu`
  (near saturation) or below `min_adu` are taken but marked not kept, and
  the scale relearned. Everything stops when the Sun leaves
  `[min_sun_alt, max_sun_alt]`.

Per telescope in the enclosure config:

```json
"sky_flats": {"latitude": 31.680407, "longitude": -110.878977,
              "filters": {"B": 0.35, "V": 1.0, "R": 1.6, "I": 1.2},
              "count": 7, "target_adu": 25000, "min_adu": 10000, "max_adu": 45000,
              "bias": 1000, "saturation": 60000, "min_exptime": 1, "max_exptime": 60,
              "dither": 20, "min_sun_alt": -12, "max_sun_alt": -1, "rate_at_minus6": 60000}
```

The sensitivities and `rate_at_minus6` are first guesses; the routine
learns the sky's rate from each frame. Frames are saved like any other
(`IMAGETYP = 'FLAT'`, `FILTER`); `sky_flats_done` in the timeline.

Tests (`tests/core/test_skyflats.py`): evening and morning twilight on the
sky simulator (the Sun moving, three filters): the order, every kept frame
within 25 % of the target after the first, the guards and limits, dithers;
a bad first guess discarded then learned; a Sun outside the window; the
request type; the sequencer taking flats through the filter wheel with the
headers.
