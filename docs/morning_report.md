# Morning report and nightly QA

After dawn, `python -m robofast_core.morning` summarizes the night from the
files it left behind -- no device is read:

```sh
python -m robofast_core.morning --night n20261109 --data-root /data \
    --plan-record /var/lib/robofast/plans/plan-2026-11-09.jsonl \
    --log /var/log/robofast/supervisor.log --log /var/log/robofast/sequencer.log \
    --out /var/lib/robofast/reports [--history DIR] [--notifier notifier.json] \
    [--throughput sites/minerva/throughput.example.json]
```

It writes `report-<night>.md` and `report-<night>.json` to `--out` and, with
`--notifier`, sends the Markdown through every sink of the notifier's config
(`docs/notifier.md`), subject "robofast <night>: all good" or "needs a look".

| Section | From |
|---|---|
| Enclosure: open time, each close and why | the timeline lines of the supervisor's log (`enclosure_open`/`enclosure_closed`, the `unsafe`/`lease_revoked`/`idle_release` before it) |
| Weather interruptions | `unsafe` events |
| Per telescope: light frames, exposure, spectra time, plate solves, median pointing offset, zero points per filter and their change against earlier reports | the frame index (`*.frames.jsonl`), the spectrum sidecars, `--history` (default `--out`) |
| Requests: outcomes, failures, ToOs, preemptions | the plan record |
| Autofocus results and the seeing trend | the plan record's `autofocus` entries |
| Guiding rms, fiber flux (exposure-meter counts/s), acquisition methods | the sidecars; the sequencer's `guiding` events (`acq_method`, `docs/acquisition.md`) |
| Spectrograph frames by kind (science, ThAr, flats, biases, darks) | the sidecars |
| Alarms (incl. pointing drift, recovery, giving up) and anomalies (errors, dropped telescopes, recovery) | the timeline |
| Throughput (with `--throughput`): per telescope, port and filter, tonight's clear-night zero point above the atmosphere, the trend's level, efficiency, change against the baseline and slope, events and alarms | the throughput monitor (docs/throughput.md): the report first ingests the night's frame indexes into its store and writes its status file |

**QA verdict** (`QAConfig`): a zero point more than `zp_drop` (0.3 mag)
fainter than the history's median, guiding rms above `guide_rms` (1
arcsec), a median pointing offset above `pointing_offset` (30 arcsec),
`autofocus_failures` (2) or more failed autofocus runs, a telescope that
gave up, failed observations; with `--throughput`, a throughput alarm or a
step (up: a cleaning; down: dew, a cover) found tonight.

The e2e night (`tests/e2e/test_rv_night.py`) ends by building its report
(written next to the night's outputs, `report-<night>.md`) and checking
every section saw what the night did.
