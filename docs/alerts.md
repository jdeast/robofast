# Alerts: from transient streams to ToO requests

`robofast.alerts` follows alert streams and turns the alerts the operator
wants into target-of-opportunity request groups. It submits them through the
request API (`POST /api/toos`, a token with the `too` scope), so it never
touches the control processes: a source outage, a bad alert or a bug here
costs ToOs, never the night. The scheduler takes a submitted ToO in at once
(the drop box's doorbell) and preempts by its policy (`design.md` 3.7.1).

```
python -m robofast.alerts run    --config alerts.json            # follow the sources
python -m robofast.alerts submit --config alerts.json --source rubin|gcn|tns FILE... [--dry-run]
python -m robofast.alerts check  --config alerts.json            # load it, say what runs
```

## Sources

| Source | What | Access | How |
|---|---|---|---|
| `tocp` | CBAT's Transient Object Confirmation Page: possible novae (`PNV`), possible supernovae (`PSN`), other transients (`TCP`), with discovery magnitude; often the first report of a bright one | public | polled (`poll_seconds`, default 300) |
| `tns` | the Transient Name Server: every reported transient, its classification once typed (`SN Ia`, `Nova`, ...) | a TNS bot: `bot_id`, `bot_name`, `api_key_file` | polled |
| `gcn` | GCN notices (JSON, Kafka): Swift BAT-GUANO, Einstein Probe WXT, SVOM, Fermi GBM; retractions and test notices are never followed | gcn.nasa.gov client credentials; the `gcn-kafka` package | pushed |
| `rubin` | Rubin alerts (LSST alert schema v11): transients; Solar System objects with their MPC orbit | a broker (ANTARES, Fink, Lasair, ALeRCE) | files, via `submit --source rubin` |

TOCP and TNS offer no push, so they are polled. At its first start a polled
source only marks what it already lists as seen, so a page's history is
never submitted; every alert is handled once (`state`, kept 30 days).

The TNS reply fields follow the TNS API documentation; the live API is not
yet checked here (no bot key). GCN notices are checked against NASA's own
schema examples (`tests/core/alert_samples`), TOCP against the live page.

## Rules

One set of rules for every source, tried in order; the first whose `match`
an alert meets makes the ToO from its `too`; an alert no rule takes is
logged (with each rule's reason) and dropped. `match` keys (each optional):
`sources`, `kind` (`sidereal`, `solar_system`), `classifications` (prefixes,
any case: `PNV`, `SN`, `Nova`, `GRB`, `WXT`), `dec` `[min, max]`, `max_mag`,
`bands`, `max_error` (localization radius, degrees: keeps arcminute bursts,
drops Fermi GBM's degrees), `max_age_hours`, `min_a`/`max_a` (au),
`designations`. `too`: `observation_type`, `policy`, `priority_class`,
`deadline_hours`, `telescopes`, `instrument_type`, `exposure_time`,
`exposure_count`, `filter`. MINERVA's: `sites/minerva/alerts.example.json`.

MINERVA's rules make imaging ToOs: KiwiSpec needs V brighter than about 10
(the FAU sees 2% of the light through the beamsplitter, so acquisition is
hard below that), which an alert almost never is. A Solar System object
becomes an `ORBITAL_ELEMENTS` target, observed only by a mount that can track
it (`moving_targets.md`): not PWI2.
