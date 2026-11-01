# Throughput over time

Mirrors collect dust and lose their aluminium, a corrector dews over, a
cover is left on, a beamsplitter is knocked. Each shows up as fewer
counts from the same star. robofast measures a photometric zero point on
every plate-solved frame (docs/sky.md section 6: `ZPMAG`, the Gaia G
magnitude giving 1 e-/s, or 1 ADU/s without a gain). The throughput monitor
(`robofast_core/throughput.py`) keeps those zero points, corrects them for
the atmosphere, compares them with what the optics should give, follows
the trend of each telescope, port and filter through the clouds, and
alarms when it drops.

## What it does

1. **Records** every frame's zero point with its context in a store: one
   JSON line per frame (`kind: "zp"`). Each line holds the telescope, port,
   filter, zero point and its unit, error, scatter and star count, airmass,
   exposure, date, night, file, and `context`. The context is whatever the
   header has of: outside, primary-mirror, focuser and CCD temperatures,
   humidity, dew point, the cloud sensor, the Moon's separation and phase,
   the Sun's altitude, FWHM, focus, and the frame's raw sky level. The store
   is append-only and never rewritten. A frame is stored once (its key is
   the file). People add `kind: "event"` lines too: a cleaning, a recoat
   (below).
2. **Corrects** each frame for the atmosphere: `zp0 = zp + k X`. `k` is per
   site and band (configured), or fitted from the store
   (`fit_extinction`). The fit uses steady nights that cover at least 0.3
   in airmass, with one slope shared by all of them and an intercept for
   each night. It is used once it has at least 3 nights and its error is
   below 0.03.
3. **Compares** with the optics. The *efficiency* is
   `10**(0.4 (zp0 - expected))`. `expected` is the port's zero point from
   its efficiency chain (`PortPhotometry`, the `sky` config's ports:
   `"expected": "chain"`), or a measured zero point (`"zero_point"`). ADU
   are converted through the port's `gain`.
4. **Envelopes.** Clouds only take light away, so the monitor never
   averages. Each night counts if it has at least `min_frames` frames. Its
   value is the `percentile`-th (80th) percentile of its frames. The trend
   is a line through the nights of the last `window_days` (60). Nights
   more than `cloud_clip` (0.05 mag) or 2.5 sigma below the line are
   rejected as cloudy, whichever limit is tighter. Sigma is measured from
   the nights *above* the line, which no cloud reaches. The fit is iterated
   from the nights near the top. A slope is fitted only over at least
   `slope_days` (20). Before that the trend is a level.
5. **Baseline.** The baseline is the trend of the series' first
   `baseline_days` (30), taken back to its first night. It is set again
   after every reset. No alarm can be raised before it exists. A fixed
   value can come from the config (`baselines`) or from an annotation.
6. **Alarm** when the trend's level is `alarm_drop` (10 %) below the
   baseline on `alarm_nights` (3) nights in a row. It clears at a reset, or
   when the level comes back within half the limit.
7. **Steps.** Steps are judged on *steady* nights only: nights whose 10-90 %
   spread of frames is under `steady_spread`, 0.15 mag. Patchy cloud says
   nothing about a step.
   * **Step up** (a cleaning, a recoat): `step_nights` (2) steady nights,
     tonight one of them, are `step_up` (5 %) above the trend from before
     them. Steady nights in between that are not above it (cirrus) do not
     count against the step. The step resets the baseline, clears the alarm
     and is annotated ("throughput up 14 % (cleaning? recoat?)").
   * **Step down** (dew, a cover, the optics): `step_down_nights` (4)
     steady nights in a row are all `step_down` (10 %) below the trend and
     within 1.5 `cloud_clip` of each other (nights of uniform cirrus differ
     more). It raises an alarm at once and restarts the trend: one-sided
     clipping would otherwise call the new, lower level clouds for a whole
     window. A later step up back to the old trend counts as "recovered",
     and the baseline is kept. A step up above the old trend is a cleaning.
8. **Annotations** reset the baseline at the first night on or after
   theirs: `annotate --what cleaning`. With `--no-reset` they are a note
   only. An annotation for a telescope covers all its ports and filters.
   With `--port` or `--filter` it covers only that one.

The analysis is a pure function of the store, run forward night by night,
so it gives the same answer at any time. A backfill shows when an alarm
*would* have fired.

## Running it

```sh
# each morning: ingest the night's frame indexes, analyze, write the status file
python -m robofast_core.throughput --config throughput.json update \
    --night n20261109 --data-root /data [--notifier notifier.json]
# or as part of the morning report (same ingest, plus a report section and QA lines)
python -m robofast_core.morning ... --throughput throughput.json
# what is there
python -m robofast_core.throughput --config throughput.json status [--json]
python -m robofast_core.throughput --config throughput.json extinction
# a cleaning (or a recoat, a new camera, ...)
python -m robofast_core.throughput --config throughput.json annotate \
    --telescope t2 --night 2026-11-12 --what cleaning --note "CO2 snow" --who jde
# the past: robofast's frame indexes, or FITS files (solved when they have no ZPMAG)
python -m robofast_core.throughput --config throughput.json backfill --data-root /data
python -m robofast_core.throughput --config throughput.json backfill --solve \
    --plate-scale 0.265 --catalog /var/lib/robofast/catalog --jnow \
    --instruments sites/minerva/instruments.json --per-night 20 \
    /nas/t2/n2026*/n2026*.T2.FAU.*.fits
```

The **backfill** takes robofast frames and legacy MINERVA frames. For a
legacy frame:

* the header RA/Dec are JNow (`--jnow` precesses them, as `rotcal` does);
* `PORT` is a number, which `--instruments` maps to a port name
  (`instruments.json`'s `ports`), or `.FAU.` in the file name means the FAU;
* `GAIN` is the *spectrograph's*, so only robofast's `EGAIN` is used, and
  legacy zero points are in ADU/s (stored 16-bit ADU, as
  `sky.example.json` says);
* the night is taken from the file name.

`--per-night N` solves at most N frames a night, evenly spaced: a night of
FAU frames is hundreds. Each solve takes about a second.

**Alarms** go out two ways:

* The status file (`status` in the config) lists each series' state and
  the active alarms. The observer console reads it (`throughput` in its
  enclosure entry, docs/console.md): a Throughput section, and the alarms
  in its alarm list. The notifier polls the console's sources
  (docs/notifier.md), so the alarms reach its sinks with deduplication,
  acknowledgement and escalation. A drop is a `warning`, with a stable text
  so that an acknowledgement sticks: "t2/fau/-: throughput below baseline
  by more than its limit". A sudden step down is an `alarm`.
* `update --notifier` sends the alarms raised that night once, straight
  through the notifier's sinks, for a site without a console. The morning
  report lists them in its QA verdict.

## Configuration (`sites/minerva/throughput.example.json`)

| Key | Default | What |
|---|---|---|
| `store`, `status` | `throughput.jsonl`, none | the store; the status file the console reads |
| `sky`, `ports` | -- | port photometry (`<telescope>/<port>` or `<port>`); `sky` reads the vetting config's `ports` |
| `expected` | `chain` | the expected zero point: the efficiency chain, or `zero_point` (a measured one where the port has it) |
| `default_port` | none | the port of a frame with none (a name, or `{telescope: port}`) |
| `extinction` | the port's `extinction`, else 0.15 | mag/airmass by series, `<telescope>/<port>`, filter or `default` |
| `fit_extinction` | false | fit `k` per series, used when good |
| `min_stars`, `max_scatter` | 3, 0.5 | a frame's zero point is used with at least this many stars and at most this scatter |
| `min_frames`, `percentile` | 3, 80 | a night counts with this many frames; its value is this percentile |
| `steady_spread` | 0.15 mag | a night is steady below this 10-90 % spread |
| `window_days`, `cloud_clip`, `slope_days`, `min_nights` | 60, 0.05, 20, 5 | the trend |
| `baseline_days`, `baselines` | 30, `{}` | the baseline |
| `alarm_drop`, `alarm_nights` | 0.10, 3 | the alarm |
| `step_up`, `step_nights`, `step_lookback` | 0.05, 2, 8 | a step up |
| `step_down`, `step_down_nights` | 0.10, 4 | a step down |
| `longitude` | none | the night of a FITS file with no night in its name or header |

## How well it works (simulated)

`robofast_core/testing/throughput_sim.py` makes the store records a
telescope would leave over months, with nothing rendered. Each frame's zero
point is what the sky simulator renders and the frame solver measures: the
port's zero point, times the injected throughput and the cloud
transparency, less extinction at the frame's airmass, plus 0.03 mag of
measurement noise. The weather is 15 % closed nights, 25 % patchy cloud
(frames losing 10-100 % of the light at random), 10 % uniform thin cirrus
(5-20 % all night) and a week-long run of bad nights. Each night has
12 frames at airmass 1-2.

`tests/core/test_throughput.py` checks one scenario on 5 seeds. In it,
T2's FAU (the efficiency chain of `sky.example.json`) loses 2.5 % a month
and is cleaned on night 180, back to 98 %. Over 50 seeds:

| | result |
|---|---|
| alarm raised (truth crosses -10 % on night ~128) | +2 nights on average, scatter 9 nights (about 0.8 % of throughput); one alarm, every seed |
| change against the baseline, every night | error +0.07 % on average, 0.8 % rms |
| the cleaning | found every seed, 1.7 nights (0-5) after the first clear night after it; the baseline reset, the alarm cleared; its size within 1.6 % rms |
| slope per 30 days | -2.50 % +- 0.32 % (injected -2.5 %) |
| false steps or alarms | none |
| extinction fit | 0.218 +- 0.004 (injected 0.22; configured 0.15 gives the efficiency 7 % low) |
| sudden 20-25 % losses (`losses`) | a step-down alarm 6-16 nights after it starts (4 steady nights are needed), the drop alarm 2 nights later; "recovered" when it ends; a 16-night loss was missed once in 7 seeds, when it fell in bad weather |
| a 50-night shutdown with a cleaning in it | the step found the first or second steady night after reopening |

The **absolute efficiency** sits about 1.3 sigma of the frame noise above
the truth (2 % here), because a night's value is its 80th percentile. The
offset is the same for the baseline and the level, so it cancels in the
change. The test also renders and plate-solves frames: three nights at
100, 90 and 80 % throughput, imaged through the simulator and the frame
solver and ingested from the frame index. It recovers the night-to-night
ratios to 2 %. It also backfills a legacy-style FITS file by solving it.

**What fools it**: uniform cirrus over 4 or more steady nights at one depth
looks like a step down. It clears as "recovered" when the clear nights come
back. The cloud sensor in the context can tell the two apart; nothing reads
it yet. A cleaning hidden by a long run of cloud is found late. An
annotation dates it exactly.

## What needs real data

* **Extinction coefficients** for each band at each site. 0.15 mag/airmass
  is an estimate. Run `extinction` on a few months of frames, or turn on
  `fit_extinction`.
* **Baselines.** The monitor sets each one from the first 30 days. A known
  value (just after a recoat) can be set in `baselines` or with
  `annotate --baseline`.
* **The efficiency chain** of each port is ESTIMATED
  (sites/minerva/README.md). Until it is measured, the efficiency is right
  as a trend but not as an absolute value. For the T2 FAU, the measured
  19.70 for 1 ADU/s on 2026-04-23 was not corrected for airmass.
  Corrected, at k = 0.15 and X of about 1.2-1.6, it would be about
  19.9-19.95.
* **FAU frames** are not written by the frame store, so they have no live
  zero points; only the imagers' frames do. The FAU series comes from the
  backfill of legacy frames, or later from the guider's frames.
* **Thresholds** (10 % alarm, 5 % step) are first guesses. They need a
  look once a year of real data has gone through.
