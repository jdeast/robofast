# All-sky cloud maps (prototype)

`robofast/allsky/` turns frames from the public all-sky cameras on
Mt Hopkins into a per-patch cloud map by counting catalogue stars: a star
that a clear night would show and the frame does not is behind cloud.
It is a prototype. It runs in **shadow** only: the weather bridge logs it
with every evaluation and never lets it into IsSafe; the supervisor and
the scheduler do not read it. The end of this page says how it could feed
them.

## Cameras

| name | what | frames |
|---|---|---|
| `mmto` | MMT Observatory all-sky, summit | `https://skycam.mmto.arizona.edu/skycam/latest_image.jpg`: 1422x1411 colour JPEG every ~34 s, with an overlay (altitude rings, N-S/E-W lines, labelled bright stars, planets, Moon, Sun, text boxes) |
| `ridge` | SAO all-sky on the FLWO ridge | `http://linmax.sao.arizona.edu/weather/sky/last_sky.jpg`: 1040x1040 grey JPEG once a minute, sunset to sunrise; a mast from the southern horizon to the centre, city glow at the horizon |
| `mmto-png` | the MMTO camera before its upgrade (2016-2023 archives) | 640x480 PNG, 256-frame stacks, gain and stamps burned in |

`robofast/allsky/cameras.py` holds each camera's starting guess
(centre, horizon radius, orientation) and the pixels that are never sky
(text, the mast). The fitted model is a JSON file per camera,
`robofast/allsky/models/<camera>.json` (override the directory with
`$ROBOFAST_ALLSKY_MODELS`).

## Dependencies

numpy and astropy, and Pillow to read the frames and draw the maps: the
`allsky` extra (`pip install 'robofast[allsky]'`; in the `test` dependency
group too). Pillow is imported lazily, so importing robofast never needs
it; reading a frame without it raises a `PillowMissing` error that says
what to install.

The catalogue is the Yale Bright Star Catalogue (5th revised edition,
Hoffleit & Warren 1991; NASA ADC / CDS V/50): 9 110 stars to V ~ 6.5,
public, in V. `python -m robofast.allsky fetch-catalog` downloads it
once (600 kB) into `$ROBOFAST_DATA_DIR/bsc5` or
`~/.cache/robofast/data/bsc5`. It is used instead of robofast's Gaia
bright-star list because that list is not published yet, is in Gaia G,
and is licensed non-commercial.

## The camera model (`model.py`, `calibrate.py`)

Pixel <-> (altitude, azimuth) through: a small tilt of the lens axis from
the zenith (`tilt_e`, `tilt_n`), the angle `theta` from that axis mapped
to a radius `r = f1 theta + f2 theta^2 + f3 theta^3` (equidistant plus
distortion), a rotation `rot` and a parity (mirror or not) about the
centre `(x0, y0)`. Positions are apparent (refracted).

* `solve`: from the camera's guess (or an old model), a brute-force search
  over rotation, parity, a centre shift and a scale that counts how many
  bright catalogue stars land on detected peaks; then iterative
  nearest-peak matching and a Levenberg-Marquardt fit with a shrinking
  match radius (outliers clipped at 3 sigma).
* `refine`: one joint fit over the matches of several clear frames.
* `check`: does the saved model still fit a frame? It matches the bright
  stars and reports the matched fraction and the median offset; a median
  offset above 1.5 px means the camera moved (refit with
  `calibrate --start <old model>`), few matches means clouds or a bump.
  Cameras get bumped: run `check` at the start of each night.

The fit residuals are reported in pixels (`meta.fit` in the JSON).

## The cloud map (`clouds.py`, `skycal.py`)

1. **Mask** below 15 deg, the camera's text and mast, saturated pixels,
   coloured overlay labels, the **static-feature mask** (pixels bright in
   the median of hours of frames: hot pixels, the overlay's rings, horizon
   lights; stars move, these do not), the Moon (a disc of 4 deg new to
   20 deg full) and 1.5 deg round each bright planet.
2. **Detect**: background (block medians) subtracted, smoothed, divided
   by the local noise. A catalogue star is detected when the signal to
   noise peaks above 4 within `radius_px` of its predicted position. The
   same test at four **control** positions round each star measures how
   often noise alone passes (per patch, shrunk toward the frame's rate).
3. **Expect**: each star's chance of detection on a clear night,
   `P = 1 / (1 + exp((V - m_lim) / w))`, with
   `m_lim = m0 + a1 (X - 1) - 2.5 log10(noise / clear-sky noise)
   - twilight * (Sun alt + 18, clipped to 0-12) - moon * F`
   (X the airmass, capped at 3; `F = illum * sqrt(sin(Moon alt)) *
   (1 + exp(-separation / 30 deg))`). `m0`, `a1`, `w` are fitted by maximum
   likelihood to the clear, dark, moonless frames of a calibration night
   (`skycal.calibrate_sky` finds those frames itself: at least 85% of the
   V < 2.5 stars above 30 deg detected); on a night without such frames it
   falls back to moonlit frames, then to the clear *patches* of the dark
   frames (and says so in the report). The cameras' automatic gain hides
   twilight and moonlight from the noise term, hence the two fitted terms
   (`skycal.fit_twilight`, `skycal.fit_moon`: the smallest value at which
   clear twilight or moonlit frames read at least 0.9 clear). Blocks where
   clear frames never show their stars (buildings, the mast) are added to
   the static mask.
4. **Patches**: 36 near-equal-area cells above 20 deg (rings 20-35-50-65-80-90
   deg with 12, 10, 8, 5 and 1 sectors). The clear fraction of a cell is
   `sum(detected - fp) / sum(P (1 - fp))`, clipped to [0, 1]: every star
   counts by its `P`, so the bright stars a clear sky always shows carry the
   weight. A cell whose expected count is under 2 is `unknown`, unless on a
   dark night its background is 1.6 times the clear-sky background (cloud
   lit from below), when it is `cloudy` from the background alone.
5. **Outputs**: per cell the clear fraction, expected count, background
   ratio and status; the whole-sky clear fraction (mean over the known
   cells) and coverage (fraction known); and
   `CloudMap.clear_fraction(alt, az, radius)` for any pointing, computed from
   the stars inside the circle (falling back to the cells it touches).
6. **Two cameras**: `clouds.combine` pools the stars of frames taken within
   3 minutes of each other; each star keeps its own camera's expectation
   and false-positive rate.
7. **Trend**: `clouds.trend` fits each cell's clear fraction against time
   over the last N maps: `rising`/`falling` when the fitted change over the
   span is at least 0.15 and significant (2.5 sigma), else `stable`.

## Validation

On 12 archived MMTO nights (2016-2018: 4 clear, 4 overcast, 4 mixed; 3 547
frames, Sun below -12 deg) against the MEarth IR cloud sensor in the
legacy site log (clear at or below -38, cloudy above -28):

| | frames both decide | agree | camera clear, sensor cloudy | camera cloudy, sensor clear |
|---|---:|---:|---:|---:|
| dark, no Moon, whole sky | 1 270 | 96.4% | 1.4% | 4.9% |
| dark, any Moon, 45 deg round the zenith | 2 215 | 97.8% | 1.0% | 2.8% |
| Moon up (> 30% lit), 45 deg round the zenith | 894 | 99.4% | 0% | 0.8% |

Camera-model residuals, median (90%): MMTO 2026 0.37 px (0.65) on 2 179
stars, ridge 2026 0.28 px (0.57) on 805, MMTO 2016 0.50 px (0.90), MMTO
2018 0.38 px (0.74): 0.04-0.15 deg.

Clear nights read 0.95-0.98 clear, overcast nights 0.00; on mixed nights
the map follows every clear/cloudy transition the sensor shows, and
agrees on 84-91% of their frames. The disagreements are sensor lag at
transitions, cloud near the horizon the zenith sensor does not see, and
starless grey frames (frost or dew on the dome, or a uniform high layer)
that the sensor reads as clear. Under a bright Moon the cells go mostly
`unknown` (few stars left to count) and only wide cones are useful; the
Moon terms were fitted on one clear full-Moon night per camera (in-sample
there; the out-of-sample moonlit nights agree on 98.6% and 100%). Thin
cirrus that the bright stars shine through can read clear.

## Shadow wiring

1. **The watcher** writes the map:

   ```
   python -m robofast.allsky watch --config sites/minerva/allsky_watch.yaml
   ```

   Every `interval` it takes each camera's newest frame from a directory
   laid out like the site fetcher's (`<dir>/<UTdate>/<camera>_<UTC>Z.jpg`),
   leaves out cameras whose newest frame is older than `stale`, analyses
   them (re-using the last result while a camera's frame is unchanged),
   pools frames within `max_age` of each other, and writes `latest.json`
   atomically (temporary file, then rename): the map's time, whole-sky
   and zenith-60 clear fraction, coverage, the 36 cells, the trend, the
   Sun and Moon, and each camera's frame and its age. `history` keeps the
   maps for the trend across restarts. Downloading the frames itself is
   opt-in (`fetch: true` and each camera's `url`); at MINERVA the site
   fetcher already saves them, so the shipped config reads the directory.
2. **The weather bridge's `allsky` source** (docs/weather_bridge.md)
   reads that file. Its type is shadow-only: whatever it reads, or if it
   fails, it is logged in every evaluation (`--json`, the status JSON, the
   log line, the console's weather card) and never part of the
   safe/unsafe/unknown verdict. It is in `sites/minerva/weather.yaml`,
   disabled until the watcher runs on the bridge's host.

## Recalibrating

The shipped models were calibrated on 2026-11-22 under a 91% Moon with
cloud about, from the clear cells only, so their limiting magnitudes
include moonlight and are conservative. On the first clear moonless night,
redo the clear-sky part from a few hours of dark frames (the geometry and
masks stay):

```
python -m robofast.allsky calibrate ~/allsky/mmto/<UTdate>/*.jpg --camera mmto \
    --start robofast/allsky/models/mmto.json --sky-only --step 2 --out-dir robofast/allsky/models
python -m robofast.allsky calibrate ~/allsky/ridge/<UTdate>/*.jpg --camera ridge \
    --start robofast/allsky/models/ridge.json --sky-only --out-dir robofast/allsky/models
```

After a camera is bumped (`check` reports an offset), refit the geometry
too: the same without `--sky-only`. Then check that the `static_mask`
entry in each JSON names its PNG relative to the models directory.

## Feeding robofast (proposal, not wired)

* **Roof**: a second opinion, never the only one. The IR sensor (or rain,
  wind, humidity) keeps the right to close. The camera may *veto an
  opening* and *advance a closing*: with fresh frames (under 3 min) and
  coverage at least 0.5, a zenith-60-deg cone below 0.3 clear for three
  maps in a row, or a `falling` trend in the cells upwind, says close or
  stay closed. `unknown` (Moon, twilight, camera down) defers to the
  sensor. It must not open the roof on its own.
* **Scheduler**: weight each candidate by `clear_fraction(alt, az, 8)` at
  its position (now and over its next exposure along its track), treat
  `unknown` as neutral, skip targets under 0.5, and prefer cells that are
  `stable` or `rising`. For RVs, require 0.8 along the track.

## Command line

```
python -m robofast.allsky fetch-catalog
python -m robofast.allsky calibrate ~/allsky/mmto/20261122/*.jpg --step 4
python -m robofast.allsky check  ~/allsky/mmto/20261122/mmto_20261122T060000Z.jpg
python -m robofast.allsky analyze ~/allsky/mmto/20261122/mmto_20261122T060000Z.jpg \
    [~/allsky/ridge/20261122/ridge_20261122T060012Z.jpg] [--at 60,180,10]
python -m robofast.allsky watch --config sites/minerva/allsky_watch.yaml [--once]
python -m robofast.allsky.validate run NIGHT_DIR --camera mmto-png --model M.json --out N.jsonl
python -m robofast.allsky.validate report *.jsonl --out-dir plots/
```

`analyze` writes `<frame>.cloud.png` (the frame with detected stars green
and missed stars red, beside the cell map) and `<frame>.cloud.json`.
