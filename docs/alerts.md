# Alerts: from transient streams to ToO requests

`robofast.alerts` follows alert streams and turns the alerts the operator
wants into target-of-opportunity request groups, and microlensing events
into followed series (below). It submits them through the request API
(`POST /api/toos`, a token with the `too` scope; a series, `POST
/api/requestgroups`, the `submit` scope: Token scopes below), so it never
touches the control processes: a source outage, a bad alert or a bug here
costs ToOs, never the night. The scheduler takes a submitted ToO in at once
(the drop box's doorbell) and preempts by its policy (`design.md` 3.7.1).

```
python -m robofast.alerts run    --config alerts.json            # follow the sources
python -m robofast.alerts submit --config alerts.json --source SOURCE FILE... [--dry-run]
python -m robofast.alerts check  --config alerts.json            # load it, say what runs
```

## Sources

| Source | What | Access | How |
|---|---|---|---|
| `tocp` | CBAT's Transient Object Confirmation Page: possible novae (`PNV`), possible supernovae (`PSN`), other transients (`TCP`), with discovery magnitude; often the first report of a bright one | public | polled (`poll_seconds`, default 300) |
| `tns` | the Transient Name Server: every reported transient, its classification once typed (`SN Ia`, `Nova`, ...) | a TNS bot: `bot_id`, `bot_name`, `api_key_file` | polled |
| `gcn` | GCN notices (JSON, Kafka): Swift BAT-GUANO, Einstein Probe WXT, SVOM, Fermi GBM; retractions and test notices are never followed | gcn.nasa.gov client credentials; the `gcn-kafka` package | pushed |
| `rubin` | Rubin alerts (LSST alert schema v11): transients; Solar System objects with their MPC orbit | a broker (ANTARES, Fink, Lasair, ALeRCE) | files, via `submit --source rubin` |
| `ogle` | OGLE-IV's Early Warning System: the season's microlensing events (Bulge and disk) with their point-lens fits (`ews.html`) | public | polled (1800 s in MINERVA's example) |
| `kmtnet` | KMTNet's events of the season, fits and class (`clear`, `probable`; `not-ulens` never followed), and the OGLE event each is, if KMTNet says (`listpage.dat`) | public page, but its data are proprietary until KMTNet's yearly release: opt-in (off in MINERVA's example; read its data policy before enabling it) | polled |
| `moa` | MOA's alerts of the year with their fits (the year's index) | public | polled |

A source runs only when it is listed under `sources`. KMTNet is left out
of MINERVA's example on purpose: its events are public to look at, but its
data are proprietary until KMTNet's yearly release, so following them is
the operator's decision, made after reading KMTNet's data policy. To opt
in, add `"kmtnet": {"poll_seconds": 1800}` to `sources`.

TOCP and TNS offer no push, so they are polled. At its first start a polled
source only marks what it already lists as seen, so a page's history is
never submitted; every alert is handled once (`state`, kept 30 days).
`submit --source` takes a source's own file: Rubin, GCN or TNS JSON, or a
polled source's saved page (`--dry-run` says what the rules would take).

The TNS reply fields follow the TNS API documentation; the live API is not
yet checked here (no bot key). GCN notices are checked against NASA's own
schema examples (`tests/core/alert_samples`), TOCP against the live page.

## Token scopes

The alerts token always needs the `too` scope. It needs the `submit` scope
as well only when an enabled source can meet a rule with a `cadence` (the
microlensing sources, `ogle`, `kmtnet`, `moa`, in MINERVA's example): their
follow-up is a NORMAL request group (`POST /api/requestgroups`), which the
request API takes only with `submit`. A ToO-only setup needs `too` alone.

```
python -m robofast.request_api tokens add --name alerts --scope too --scope submit
```

`check` prints the scopes a config needs. `run` and `submit` (not
`--dry-run`) ask the request API who the token is (`GET /api/`) at startup
and stop with exit code 2, naming the missing scope, if it lacks one (for
`submit`, the scopes its `--source` needs); if the API cannot be asked then,
they log a warning and go on.

## Rules

One set of rules for every source, tried in order; the first whose `match`
an alert meets makes the ToO from its `too`; an alert no rule takes is
logged (with each rule's reason) and dropped. `match` keys (each optional):
`sources`, `kind` (`sidereal`, `solar_system`), `classifications` (prefixes,
any case: `PNV`, `SN`, `Nova`, `GRB`, `WXT`), `dec` `[min, max]`, `max_mag`,
`bands`, `max_error` (localization radius, degrees: keeps arcminute bursts,
drops Fermi GBM's degrees), `max_age_hours`, `min_a`/`max_a` (au),
`designations`, `min_amax`, `peak_hours` (microlensing, below). `too`:
`observation_type`, `policy`, `priority_class`, `deadline_hours`,
`telescopes`, `instrument_type`, `exposure_time`, `exposure_count`,
`filter`, `cadence`. MINERVA's: `sites/minerva/alerts.example.json`.

MINERVA's rules make imaging ToOs: KiwiSpec needs V brighter than about 10
(the FAU sees 2% of the light through the beamsplitter, so acquisition is
hard below that), which an alert almost never is. A Solar System object
becomes an `ORBITAL_ELEMENTS` target, observed only by a mount that can track
it (`moving_targets.md`): not PWI2.

## Microlensing

A microlensing event's fit (t0, tE, u0) says, days ahead, when it will peak
and how bright: A(u) = (u² + 2) / (u √(u² + 4)), u(t) = √(u0² + ((t − t0)/tE)²).
Follow-up is a series of visits, denser toward the peak and denser still for
a high-magnification event, where a planet's signal (hours for an Earth, a
day or two for a Jupiter) is likeliest; the follow-up teams' own practice:
μFUN asked for "High-cadence (<10 min)" data over the peak of A > 200 events
(Gould et al. 2010, arXiv:1001.0572), ROME/REA "a requested cadence of 1
hour", 15 minutes on an anomaly, in SDSS-i′ (Tsapras et al. 2019,
arXiv:1910.05156).

A ToO cannot have a cadence, so a microlensing rule makes a NORMAL request
group (`"observation_type": "NORMAL"` with a `cadence`), one standing request
per tier (`extra_params.cadence`, design.md 3.7), each with a window: the
time the event's fit predicts a magnification of at least the tier's, from
now on (a tier it never reaches is left out). MINERVA's example:

| tier | window (an event with u0 0.008, tE 55 d: Amax 125) | cadence |
|---|---|---|
| `{"magnified": 1.34, "min_interval": 43200, "max_per_night": 1}` | A ≥ 1.34 (u ≤ 1): ±55 d | once a night |
| `{"magnified": 10, "min_interval": 3600}` | A ≥ 10: ±5.5 d | hourly |
| `{"magnified": 100, "min_interval": 600}` | A ≥ 100: ±0.33 d | every 10 min |

The tiers are separate standing requests, by design: near the peak of a
high-magnification event every tier's window is open at once, so the
nightly, hourly and 10-minute requests can all come due together and all be
observed (the nightly visit is one more point in the 10-minute series).
That is intended: each tier keeps its own cadence when the others' windows
close, and a tier is never merged into another.

The rule's `match` chooses the events: `min_amax` (the predicted peak
magnification) and `peak_hours` (`[min, max]`: the peak from `min` to `max`
hours from now), with `max_mag` the predicted peak I (the surveys' own
blending where given). The numbers are the operator's; nothing is built in.

The surveys refit their events in place, so a survey's page is never primed
(the rules' `peak_hours` keep the season's history out), an event no rule
takes is looked at again at the next poll, and one star is followed once:
another survey's event named as its twin (KMTNet names OGLE's), or within 2″
of a followed one, is marked seen. A follow-up stands as first submitted: a
refit that raises an event's magnification does not change it.

Each survey's numbers, as read: OGLE's Tmax is HJD, KMTNet's t0 HJD −
2400000, MOA's t0 a UT date (HJD taken as UTC: at most 8.3 min off, against
tE of days). Where OGLE's umin is 0.000 its page gives Amax 99999 and Dmag
capped at 12.5; such an event has no peak magnification or magnitude here,
so it passes no `min_amax` or `max_mag` (on the 2026 pages, KMTNet's own
fits of such events were not A > 2000: pinned fits).

The Bulge (RA 17h55m, Dec −29°) peaks at 29° from Mt Hopkins: above
MINERVA's 21° with the Sun below −12°, 0 h a night from November to
mid-February, about 0.6 h on 1 March and 2 h on 1 April. A southern site
such as MEarth-South (Cerro Tololo), where the Bulge passes near the
zenith, can follow microlensing events months earlier in the season, with
the same sources and rules in its own alerts config.
