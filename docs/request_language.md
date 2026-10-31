# The request language

robofast's observing requests (`robofast_core/request_model.py`) use the
field names, nesting and units of the LCO **Observatory Control System**
(OCS) observation portal
([observatorycontrolsystem/observation-portal](https://github.com/observatorycontrolsystem/observation-portal)),
so OCS clients, TOM Toolkit's generic `OCSFacility` first among them
(`docs/tom_facility.md`), submit to robofast unchanged. (Field names and
units are facts about the OCS's public API; no OCS code is used.)

What the OCS has no field for is robofast's own and lives in the
`extra_params` of the level it belongs to, the OCS's own extension point.
Unknown keys are refused, with an error naming the entry, as before.

```
RequestGroup            name, proposal, ipp_value, operator, observation_type
  Request               windows, location, configurations
    Configuration       type, instrument_type, target, constraints, guiding_config
      InstrumentConfig  exposure_time, exposure_count, optical_elements, mode
```

## Fields

`*` marks robofast extensions. *Python* is the attribute name the scheduler
and sequencer use where it differs. "Kept" means stored and reported, not
acted on.

### RequestGroup

| JSON | Unit / values | Python | Notes |
|---|---|---|---|
| `id` | string | | robofast: optional on submit (generated); OCS assigns integers |
| `name`, `proposal` | string | | `proposal` checked against the API's `proposals` setting, if set |
| `ipp_value` | > 0 | `priority` | the dispatch score's base (design.md 3.7). Previously `priority` |
| `operator` | `SINGLE`, `MANY` | | SINGLE takes exactly one request (default by count) |
| `observation_type` | `NORMAL`, `RAPID_RESPONSE`, `TIME_CRITICAL` | | the last two are ToOs (below). `DIRECT`, `REAL_TIME` refused |
| `extra_params.too`* | `deadline` (ISO time), `policy`, `priority_class` | `too` | ToO details, each optional (below). Previously top-level `too` |
| `state` | OCS state | | read only (below) |
| `created`, `submitter` | ISO time, token name | `submitted` | set by the API |

### Request

| JSON | Unit / values | Python | Notes |
|---|---|---|---|
| `id` | string | | optional on submit |
| `windows` | `[{start, end}]` ISO times | | a visit must fit in one; none: any time. A time without a zone is UTC (as in the OCS) |
| `location.telescope` | name | `telescopes` | one telescope |
| `extra_params.telescopes`* | names | `telescopes` | several telescopes, e.g. a `SPECTRUM`'s fiber feeds. Previously `telescopes` |
| `location.enclosure` | name | `location` | picks the enclosure at the API |
| `location.telescope_class` | e.g. `0m7` | `location` | must equal the API's `telescope_class` |
| `location.site` | string | `location` | kept |
| `extra_params.cadence`* | `min_interval` (s), `max_per_night` | `cadence` | a standing request (design.md 3.7). Previously `cadence` |
| `extra_params.max_attempts`* | int, default 3 | | failures before FAILED. Previously top level |
| `configuration_repeats` | int, default 1 | | the configurations repeated in order, stored expanded (ids `c.2`, `c.3`, ...) |
| `optimization_type` | `TIME`, `AIRMASS` | | kept; the dispatch score has its own airmass term |
| `acceptability_threshold` | % | | kept; partial completion is per configuration (`allow_partial`) |
| `observation_note` | string | | kept |
| `cadence` | OCS `start`, `end`, `period`, `jitter` | | refused in a stored request: expand it first with `POST /api/requestgroups/cadence/` |
| `state` | OCS state | | read only |

### Configuration

| JSON | Unit / values | Python | Notes |
|---|---|---|---|
| `id` | string | | optional on submit |
| `type` | `EXPOSE`, `DARK`, `BIAS`, `AUTO_FOCUS`, `SPECTRUM` | | DARK/BIAS: every frame shutter closed. `SPECTRUM` is a fiber spectrum fed by every free telescope of the request (docs/rv_night.md) |
| `instrument_type` | e.g. `imager`, `spectrograph` | `instrument` | the telescope must list it. Previously `instrument` |
| `target` | below | | |
| `constraints` | below | | |
| `guiding_config.mode` | `ON`, `OFF` | `guide` | ON: guide on the target. A SPECTRUM is always guided. `optional`, `exposure_time`, ... accepted, not used. Previously `guide` |
| `acquisition_config` | | | accepted, not used (acquisition is the telescope's) |
| `extra_params.allow_partial`* | bool | | keep frames of an interrupted visit. Previously top level |
| `repeat_duration` | | | refused unless null (REPEAT_* types not supported) |
| `status`, `failures` | robofast status | | set by the scheduler; refused on submit |

### Target (`type: ICRS` only)

| JSON | Unit | Python | Notes |
|---|---|---|---|
| `name` | | | |
| `ra` | deg, [0, 360] | `ra` (`ra_hours` = ra/15) | previously `ra_hours` (hours) |
| `dec` | deg | `dec_deg` | previously `dec_deg` |
| `proper_motion_ra` | mas/yr (mu_alpha cos dec) | `pm_ra` | barycentric correction only |
| `proper_motion_dec` | mas/yr | `pm_dec` | |
| `epoch` | Julian year | | |
| `parallax` | mas | | |
| `extra_params.radial_velocity`* | km/s | `rv` | previously `rv` |

`ORBITAL_ELEMENTS`, `HOUR_ANGLE` and `SATELLITE` targets are refused.

### InstrumentConfig

| JSON | Unit / values | Python | Notes |
|---|---|---|---|
| `exposure_time` | s | `exptime` | for `AUTO_FOCUS`, if positive, the focus exposure time |
| `exposure_count` | int ≥ 1 | `count` | |
| `optical_elements.filter` | name | `filter` | a hard constraint on the telescope's filters. Other groups refused |
| `mode` | readout mode code | `mode` | kept |
| `extra_params.port`* | M3 port name | `port` | |
| `extra_params.iodine`* | bool | | SPECTRUM only |
| `extra_params.target_counts`* | counts | | SPECTRUM only: exposure-meter termination (`exposure_time` the maximum) |
| `extra_params.light`* | bool | `light` | per-frame shutter, where it differs from the type's |
| `rotator_mode`, `rois` | | | refused unless empty |

### Constraints

| JSON | Unit | Python | Notes |
|---|---|---|---|
| `max_airmass` | | | |
| `min_lunar_distance` | deg | `min_moon_sep` | previously `min_moon_sep` |
| `max_lunar_phase` | illuminated fraction 0-1 | | enforced whether or not the Moon is up |
| `extra_params.min_alt`, `max_alt`, `max_sun_alt`* | deg | | previously top level |
| `extra_params.min_ha`, `max_ha`* | hours | | previously top level |
| `max_seeing`, `min_transparency` | | | refused unless null |

Constraints only tighten the telescope's and the site's limits.

## Targets of opportunity

`observation_type` `RAPID_RESPONSE` or `TIME_CRITICAL` makes a group a ToO
(design.md 3.7.1); submitting one needs the API's `too` scope. Its
`extra_params.too` may set each of:

| | RAPID_RESPONSE default | TIME_CRITICAL default |
|---|---|---|
| `policy` | `now-after-readout` (interrupt at the next exposure boundary) | `next` (interrupt nothing; first free telescope) |
| `priority_class` | 2 | 1 |
| `deadline` | the end of the group's last window | the same |

So an OCS client's Rapid-Response request interrupts a normal observation
(and a Time-Critical one), and a Time-Critical request jumps the queue
without interrupting anything. `policy: "now"` (abort the exposure) is
available through `extra_params.too`. A ToO cannot have a cadence.

## States

Each configuration has robofast's `status` (PENDING, ATTEMPTED, COMPLETED,
FAILED, EXPIRED, CANCELED). Requests and groups report the OCS `state`:
`PENDING` while any configuration can still be scheduled; `COMPLETED` when
all completed; otherwise `CANCELED`, `FAILURE_LIMIT_REACHED` or
`WINDOW_EXPIRED`, the first that applies. Before the scheduler has taken a
group in, the API reports `PENDING` (queued), `CANCELED` (withdrawn while
queued) or `NOT_ATTEMPTED` (refused by the scheduler; `error` says why),
with robofast's `intake` alongside.

## The previous robofast JSON

Request files, drop-box files and state files in robofast's previous JSON
(`priority`, `too`, `telescopes`, `cadence`, `instrument`, `guide`,
`ra_hours`/`dec_deg`, `exptime`/`count`/`filter`, `min_moon_sep`, ...)
**still load for this release**: `upgrade_legacy` rewrites them into the
OCS shape (the "previously" column above; `ra_hours` is multiplied by 15; a
`too` becomes `extra_params.too` with `observation_type` TIME_CRITICAL for
policy `next`, else RAPID_RESPONSE) and logs a deprecation warning once.
Everything robofast writes is in the new shape, so a state file migrates
the first time the scheduler saves it. The next release drops the
converter: rewrite request files before then (loading and re-saving them
through `RequestGroup.from_dict(...).to_dict()` does it).
