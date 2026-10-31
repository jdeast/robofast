# RV spectra in the sequencer

How robofast takes a fiber-fed spectrum (KiwiSpec) with one or more
telescopes, from the request to the sidecar file. The spectrograph service
itself is in `docs/kiwispec.md`; running a whole simulated night is in
`docs/demo.md`.

## 1. Requests: the `SPECTRUM` configuration

A configuration of type `SPECTRUM` (`robofast_core/request_model.py`) is
one target observed with the enclosure's spectrograph:

```json
{"id": "HD10700-spec", "type": "SPECTRUM",
 "target": {"name": "HD 10700", "ra_hours": 1.734467, "dec_deg": -15.937478,
            "pm_ra": -1729.7, "pm_dec": 855.3, "epoch": 2000.0, "parallax": 273.8},
 "instrument_configs": [{"exptime": 300, "count": 3, "iodine": true,
                         "target_counts": 2e6}]}
```

* Its instrument is `spectrograph` (the default for `SPECTRUM`), and the
  telescopes that may feed it are the request's `telescopes` (or every
  telescope whose limits list the `spectrograph` instrument).
* It is always guided. Its instrument configs are light, with no `filter`
  or `port`; `iodine` (true/false; absent: the spectrograph service's
  default) and `target_counts` (exposure-meter termination, `exptime` then
  being the maximum) are only allowed here.
* The target's `pm_ra` (mu_alpha cos(dec)) and `pm_dec` (mas/yr), `epoch`
  (Julian year, default 2000), `parallax` (mas) and `rv` (km/s) are
  optional. The scheduler ignores them; the barycentric correction uses
  the proper motion.
* `from_legacy_csv` makes `SPECTRUM` configurations, and
  `sites/minerva/requests.example.json` uses them.

## 2. Dispatch: the leader and its feed

The telescope that picks a `SPECTRUM` request *leads* it. Its observation
(`kind="spectrum"`) names a `feed`: the leader, plus every other telescope
that the request allows, that is free (not in a visit of its own) and that
passes the hard constraints for the request now. Each of those is reserved:
when it next asks, it gets the same spectrum as a *member*
(`spectrum["leader"]` names the leader). An idle member is woken through
the sequencer's `preempt` with policy `next`, which interrupts nothing.

* Only the leader's report changes the request's status. A member's report
  only ends its reservation, and the leader's report ends all of them.
* One spectrum at a time: while one telescope's visit is a `SPECTRUM`, no
  other telescope may start another (`spectrograph in use by t1`).
* A busy telescope is not waited for. If T1 is imaging when T2 picks an RV
  target, T2 leads it alone.
* The plan record has `join` entries (a member given the spectrum) and
  `member_outcome` entries; `python -m robofast_core.plan_record FILE
  --replay` replays them.

## 3. The sequencer

Each telescope of the feed, in its own task: slew, locate the fiber with
the backlight (`locate_fiber`; one telescope at a time, since the backlight
lights the whole bundle; on failure it guides on the configured fiber
position, as legacy did), and start guiding. Then:

* The **leader** opens the session and waits until every expected member
  is guiding, or until `spectrum_ready_timeout`; then it starts with the
  telescopes that are guiding. A member that arrives after the exposure
  has started does not join it (`spectrum_missed`).
* For each frame, the leader asks the spectrograph service for a science
  exposure (`POST /expose` with the telescopes feeding it, under the
  service's names: `spectrograph.telescopes` in the enclosure config maps
  `t1` to `T1`).
* **Never without guiding.** No spectrum starts unless at least one
  telescope of the feed is guiding.
* **Dropping out.** During the spectrum every feeding telescope checks its
  guider and its mount every `spectrum_check_interval`. One that fails
  drops out: it is made safe and backs off, like after any error, and the
  spectrum goes on with the rest. The record says who dropped out and why.
  A leader that drops out keeps running the exposures; it reports the
  spectrum, then its own error is handled. If every telescope drops out,
  the spectrograph job is aborted and the observation fails.
* **Cleanup** (lease revoked, the generation over, the sequencer stopping)
  aborts the spectrograph job this sequencer started
  (`KiwiSpecRemote.abort`: bounded, never raises), and in parallel aborts
  the cameras and stops the guiders and any slews. It parks only after
  the spectrograph's abort has ended, or after `spectrograph_abort_wait`.
  The generation's fence also stops any new spectrograph job. At startup
  it aborts a spectrum that a crashed predecessor left running on its
  telescopes.
* **Preemption.** A leader preempted with `now` aborts its spectrum; with
  `now-after-readout` the frame in progress finishes. A member that is
  preempted only drops out. The observer console's `pause` (an operator
  hold) is such a preemption.
* **Calibrations.** An observation of kind `spectrograph_calibration`
  (`light=False`) is one `POST /calibrate` job: biases, darks, ThAr and
  flats. The calibration plan's `spectrograph` list makes one (see
  `sites/minerva/calibration.example.json`), and the sequencer takes it
  with the roof closed and no lease, in its closed phase. Biases and darks
  are true dark frames (shutter closed). Legacy took them as light frames
  (`docs/kiwispec.md`, section 3).
* **Autofocus.** The autofocus triggers run before spectra as well as
  before images: legacy `doSpectra` focused on the target before guiding.

Settings (`SequencerConfig`, defaults): `spectrum_ready_timeout` 300 s,
`spectrum_check_interval` 2 s, `spectrum_poll` 2 s, `spectrum_margin` 180 s
(the bound beyond the exposure time), `locate_fiber` true, `locate_timeout`
60 s, `spectrograph_abort_wait` 10 s.

## 4. The metadata: sidecars, not rewritten raw files

The raw FITS file stays on the spectrograph PC exactly as the camera and
the spectrograph server wrote it. robofast never rewrites it in place
(`docs/kiwispec.md`, open question 10). Everything robofast knows about a
spectrum goes into a JSON **sidecar** (`robofast_core/spectra.py`):

```
<data_root>/<site>/kiwispec/n<YYYYMMDD>/<raw file name>.json
<data_root>/<site>/kiwispec/n<YYYYMMDD>/n<YYYYMMDD>.kiwispec.spectra.jsonl   (index)
```

A sidecar holds:

* the raw file name and the spectrograph service's result;
* the requested feed, the telescopes at the start (`telescopes`) and at
  the end (`telescopes_end`), and who dropped out and why (`dropped`);
* each telescope's guide statistics at the end of the frame (`guide`:
  state, residual rms, flux, and so on, from the guider's status);
* the exposure meter's counts and flux-weighted midpoint (seconds, UTC,
  JD), what ended the exposure (`time` or `counts`), and the iodine
  position;
* the target (with its astrometry), the site and the request ids;
* `barycentric`: computed with astropy at the flux-weighted midpoint. If
  there is no midpoint, the middle of the exposure is used, and
  `midpoint_source` says which. It contains:
  * `bjd_tdb`: the midpoint in TDB plus the light travel time to the
    solar-system barycentre;
  * `barycentric_correction`: the RV correction in m/s
    (`SkyCoord.radial_velocity_correction`);
  * the coordinates used, with the proper motion propagated from `epoch`
    to the midpoint.

  `tests/core/test_rv_spectra.py` checks the computation against astropy's
  documented value (20.077135 km/s for the Keck example, 2016-06-04) and
  checks the light travel time against an independent Roemer-delay
  calculation.

Sidecars are written atomically and never overwritten. Calibration frames
get sidecars too, without the barycentric block. The plan's `report`
carries the sidecar paths as the observation's files, so the request API's
data pointers lead to them.

## 5. What is left

* The spectrograph is shared by both MINERVA enclosures, but here it is an
  enclosure-level resource. Two sequencers would contend for it: the
  service answers 409 and the second spectrum is that telescope's error.
* ThAr and fiber-flat lamps are not switched (`docs/kiwispec.md`, question
  3).
* An operator hold on a reserved member is not known to dispatch. The
  leader waits `spectrum_ready_timeout` for the held member, then starts
  without it.
* A spectrograph that keeps failing causes errors and backoffs on the
  leading telescopes. Nothing marks the spectrograph itself as down.
