# A TOM Toolkit facility on the request API (sketch)

Not built yet: how a [TOM Toolkit](https://tom-toolkit.readthedocs.io/)
facility module would map onto robofast's request API
(`robofast_core/request_api.py`, design.md section 3.7). The module would
live in its own small package (for example `tom_robofast`), installed in the
TOM, not in the observatory's control processes.

## Setup

The observatory creates a token for the TOM, with the scopes it needs:

```
python -m robofast_core.request_api tokens add --config /etc/robofast/request_api.json \
    --name my-tom --scope read --scope submit [--scope too]
```

and the TOM keeps it in its settings:

```python
FACILITIES = {
    "ROBOFAST": {
        "url": "https://api.observatory.example",
        "token": os.environ["ROBOFAST_API_TOKEN"],
        "enclosure": "aqawan1",      # optional; the API infers it from telescopes
    }
}
```

Every call sends `Authorization: Token <token>`. Ids the TOM gets back are
request-group ids; the API shows a client only its own groups.

## The facility class

`tom_observations.facility.BaseRoboticObservationFacility` methods and the
endpoint each one uses (all under `/api/v1`):

| Facility method | Request API |
|---|---|
| `get_form(observation_type)` | a form for target, exposure time, count, filter, instrument, telescopes, windows, constraints (altitude, airmass, Moon); `GET /` lists the enclosures and telescopes for its choices |
| `validate_observation(payload)` | `POST /requestgroups/validate`; returns `errors` (each names the offending entry, e.g. `requests[0].configurations[0].target.dec_deg: ...`), and the dry run (which telescopes could start it tonight, when, and why not) to show the user |
| `submit_observation(payload)` | `POST /requestgroups` (or `POST /toos` for a target of opportunity, with a `too` block: `deadline`, `policy`, `priority_class`); returns `[id]` from the 202 response |
| `get_observation_status(id)` | `GET /requestgroups/{id}`; `state` maps to the TOM's status string, `scheduled_start`/`scheduled_end` are not known in advance for a dispatch scheduler (None) |
| `get_terminal_observing_states()` | `["COMPLETED", "FAILED", "EXPIRED", "CANCELED", "DONE", "REJECTED"]` |
| `get_failed_observing_states()` | `["FAILED", "EXPIRED", "CANCELED", "REJECTED"]` |
| `cancel_observation(id)` | `POST /requestgroups/{id}/cancel` |
| `data_products(id)` | `GET /requestgroups/{id}/data`: per configuration, each completed observation (telescope, time, frames) and the expected file paths. No files are served yet; when they are, this returns their URLs for the TOM to fetch |
| `get_observation_url(id)` | the API URL `/api/v1/requestgroups/{id}` (no web page yet) |
| `get_facility_status()` | `GET /status`: per enclosure, weather verdict and reason, shutter, and each telescope's state, in the TOM's `{"code", "sites": [{"code", "telescopes": [{"code", "status", "reason"}]}]}` shape |
| `get_observing_sites()` | static: the site's coordinates (from the facility settings) |

### Building the request group

`submit_observation` turns the form's fields into one request group, the
same JSON the API takes (`robofast_core/request_model.py`):

```python
def _request_group(self, payload):
    target = Target.objects.get(pk=payload["target_id"])
    p = payload["params"]
    group = {
        "name": p["name"],
        "proposal": p.get("proposal", ""),
        "priority": p.get("priority", 1),
        "requests": [{
            "telescopes": p.get("telescopes") or None,
            "windows": [{"start": p["start"], "end": p["end"]}] if p.get("start") else [],
            "configurations": [{
                "instrument": p.get("instrument", "imager"),
                "target": {"name": target.name, "ra_hours": target.ra / 15.0,
                           "dec_deg": target.dec},
                "instrument_configs": [{"exptime": p["exposure_time"],
                                        "count": p["exposure_count"],
                                        "filter": p.get("filter")}],
                "constraints": {"max_airmass": p.get("max_airmass")},
            }],
        }],
    }
    if p.get("too"):
        group["too"] = {"deadline": p["deadline"], "policy": p.get("policy", "next"),
                        "priority_class": 1}
    return group
```

Ids are left out (the API generates them); `status`, `submitted` and
`submitter` are set by the API and the scheduler and are refused or
overwritten if sent.

### Status

A group's `intake` says where it is (`QUEUED` in the scheduler's drop box,
`ACCEPTED`, `REJECTED` with an `error`, or `CANCELED` while still queued);
`state` is `PENDING` while any configuration can still be scheduled, then
the configurations' common terminal status (`COMPLETED`, `FAILED`,
`EXPIRED`, `CANCELED`) or `DONE` if they differ. The TOM's
`update_observation_status` polls `GET /requestgroups/{id}`; a TOM-side
rate of one call per group per minute is well within the API's limits.

## Alert brokers

A broker-to-ToO process (design.md 3.7.1) is the same client with the `too`
scope: it turns each accepted alert into a `POST /toos` and reads the
outcome back with `GET /requestgroups/{id}`.
