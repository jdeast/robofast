# TOM Toolkit: use the OCS facility

robofast's request API speaks the LCO Observatory Control System (OCS)
request language and endpoints (`docs/request_language.md`,
`robofast_core/request_api.py`), so a [TOM Toolkit](https://tom-toolkit.readthedocs.io/)
needs no robofast-specific facility module: its generic
`tom_observations.facilities.ocs.OCSFacility` talks to robofast with
settings alone. `tests/core/test_tom_ocs.py` drives the API with request
bodies and calls built exactly as that facility builds them.

## On the observatory

1. Create a token for the TOM, with the scopes it needs (`too` for
   Rapid-Response and Time-Critical requests):

   ```
   python -m robofast_core.request_api tokens add --config /etc/robofast/request_api.json \
       --name my-tom --scope read --scope submit --scope too
   ```

2. In the request API config, name the proposals the TOM may submit under
   (its form offers them; a submission under another is refused), the
   telescope class the TOM puts in each request's `location`, and the site
   code for facility status:

   ```json
   "proposals": {"KEY2026B-001": {"title": "RV follow-up", "clients": ["my-tom"]}},
   "telescope_class": "0m7",
   "site_code": "flwo"
   ```

   `instruments` can rename an instrument type or override any field of its
   `/api/instruments/` entry (`{"imager": {"name": "T1/T2 imager"}}`).

3. Expose the API at the root of its host name: the facility joins absolute
   paths (`/api/requestgroups/`, ...) onto its `portal_url`, so a path
   prefix in front of `/api` is lost. The `deploy/` proxy examples forward
   `/api/*` as they are.

## In the TOM's settings

```python
FACILITIES = {
    "OCS": {
        "portal_url": "https://api.observatory.example",
        "api_key": os.environ["ROBOFAST_API_TOKEN"],
        # Required non-empty by OCSSettings; robofast serves no archive yet,
        # so "fetch data" fails (data pointers: GET /api/requestgroups/{id}/data).
        "archive_url": "https://api.observatory.example",
        "max_instrument_configs": 5,
        "max_configurations": 5,
    },
}
TOM_FACILITY_CLASSES = ["tom_observations.facilities.ocs.OCSFacility", ...]
```

To list robofast beside an LCO facility, subclass `OCSSettings` and
`OCSFacility` with another `name` and facility key (as `LCOFacility`
does); no method needs overriding. For the facility status page, give the
settings' sites the API's `site_code` (`get_sites()`).

## What each facility call gets

| OCSFacility / form | robofast |
|---|---|
| instrument, filter, readout mode and configuration type choices: `GET /api/instruments/` | each instrument type on the enclosures' telescopes: `type`, `class`, `name`, `optical_elements.filters` (the telescopes' dispatch `filters`), `modes` (readout, guiding, acquisition), `configuration_types` (EXPOSE, AUTO_FOCUS, DARK, BIAS; SPECTRUM for the spectrograph) |
| proposal choices: `GET /api/profile/` | the `proposals` this token may use |
| `validate_observation`: `POST /api/requestgroups/validate/` | `errors` keyed by entry (shown on the form), `request_durations` (the scheduler's time model), plus robofast's dry run of the coming night |
| `submit_observation`: `POST /api/requestgroups/` | 202 with the group; the facility keeps its requests' ids as observation ids |
| cadence form: `POST /api/requestgroups/cadence/` | one request per period (`jitter`-wide windows), not checked for visibility |
| `get_observation_status`: `GET /api/requests/{id}` and `.../observations/` | the request's OCS `state`; completed observations as blocks (no scheduled blocks: a dispatch scheduler plans nothing ahead, so `scheduled_start`/`end` stay empty until one completes) |
| `cancel_observation`: `GET /api/requestgroups?request_id=`, `POST /api/requestgroups/{id}/cancel/` | `state: CANCELED` (the scheduler cancels it at its next drop-box check; an observation in progress finishes) |
| `get_facility_status`: `GET /api/telescope_states/` | per telescope, AVAILABLE or NOT_AVAILABLE with the reason (weather, fault, sequencer, telescope) |
| `data_products`: `archive_url/frames/` | not served yet |

Terminal and failed states are the OCS's (`COMPLETED`; `WINDOW_EXPIRED`,
`CANCELED`, `FAILURE_LIMIT_REACHED`, and `NOT_ATTEMPTED` for a group the
scheduler refused), so `OCSSettings`' lists apply as they are.

## Observation modes

The form's *observation mode* maps onto robofast's ToO policies
(`docs/request_language.md`): **Rapid-Response** interrupts the
lowest-ranked busy telescope at its next exposure boundary
(`now-after-readout`, class 2); **Time-Critical** takes the first telescope
to come free without interrupting anything (`next`, class 1). Both expire
at the end of the request's window. Normal requests are ranked by
`ipp_value` (the form's IPP factor) with the dispatch score.

## Not supported (yet)

- **Time accounting.** Proposals are a static list; no time is allocated,
  charged or refunded, and `ipp_value` is a plain priority (no IPP
  debit/credit).
- Non-sidereal targets (`ORBITAL_ELEMENTS`, ...), `max_seeing`,
  `min_transparency`, `REPEAT_*` configuration types and `repeat_duration`,
  rotator modes, regions of interest; the dither and mosaic expansion
  endpoints (`/api/configurations/dither/`, `/api/requests/mosaic/`; the
  form falls back to the unexpanded request); `DIRECT` and `REAL_TIME`
  observation types.
- `guiding_config.optional`, `optimization_type` and
  `acceptability_threshold` are accepted and kept, not acted on.
  `acquisition_config.mode` is acted on (docs/acquisition.md): OCS `WCS`
  maps to robofast's `SOLVE`, `OFF` to `COORDINATES`, and the robofast
  modes `AUTO` (default), `BRIGHTEST`, `GEOMETRY` are accepted too; its
  `exposure_time` is not used.
- The archive (`/frames/`): no data products to download yet.
- Cadence windows are not filtered for visibility (the OCS drops windows
  in which the target never rises; robofast leaves them to expire).

## Alert brokers

A broker-to-ToO process (design.md 3.7.1) is the same client with the `too`
scope: it turns each accepted alert into a `RAPID_RESPONSE` (or
`TIME_CRITICAL`) request group posted to `/api/requestgroups/`, and reads
the outcome back with `GET /api/requests/{id}`.
