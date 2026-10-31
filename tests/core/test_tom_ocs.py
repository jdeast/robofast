"""TOM Toolkit's generic ``OCSFacility`` against robofast's request API.

The request bodies and calls below are built exactly as tom_base's
``tom_observations/facilities/ocs.py`` builds them (``OCSFullObservationForm.
observation_payload`` and the ``OCSFacility`` methods), from that form's
documented fields; tom_base itself is not imported. Every path is the
absolute ``/api/...`` the facility joins onto its ``portal_url``, with the
``Authorization: Token <api_key>`` header it sends.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from robofast_core.plan import Observation
from tests.core.test_dispatch import LST, NIGHT
from tests.core.test_request_api import api, make_api  # noqa: F401 (fixtures)

PROPOSAL = "KEY2026B-001"
OCS_SETTINGS = {
    "telescope_class": "0m7",
    "rate_burst": 100,
    "site_code": "flwo",
    "proposals": {PROPOSAL: {"title": "RV follow-up"}, "other": {"clients": ["nobody"]}},
}


@pytest.fixture
async def tom(make_api):  # noqa: F811
    return await make_api(**OCS_SETTINGS)


def form_fields(**kw) -> dict:
    """The cleaned data of TOM's OCSFullObservationForm (one configuration,
    one instrument config), with a sidereal TOM target."""
    fields = {
        "name": "TOM obs",
        "proposal": PROPOSAL,
        "ipp_value": 1.05,
        "observation_mode": "NORMAL",
        "optimization_type": "TIME",
        "configuration_repeats": 1,
        # clean_start/clean_end: isoformat of aware datetimes.
        "start": (NIGHT - timedelta(hours=1)).isoformat(),
        "end": (NIGHT + timedelta(hours=6)).isoformat(),
        "c_1_instrument_type": "imager",
        "c_1_configuration_type": "EXPOSE",
        "c_1_max_airmass": 1.6,
        "c_1_min_lunar_distance": 30,
        "c_1_max_lunar_phase": None,
        "c_1_ic_1_exposure_count": 2,
        "c_1_ic_1_exposure_time": 30.0,
        "c_1_ic_1_readout_mode": "default",
        "c_1_ic_1_filter": None,
        # The TOM Target (degrees; pm and epoch may be None).
        "target": {
            "name": "AT 2026tom",
            "ra": (LST + 1.0) % 24.0 * 15.0,
            "dec": 25.0,
            "pm_ra": None,
            "pm_dec": None,
            "epoch": 2000.0,
        },
    }
    fields.update(kw)
    return fields


def observation_payload(f: dict, instruments: dict) -> dict:
    """OCSFullObservationForm.observation_payload, field for field."""
    t = f["target"]
    target = {
        "name": t["name"],
        "type": "ICRS",
        "ra": t["ra"],
        "dec": t["dec"],
        "proper_motion_ra": t["pm_ra"],
        "proper_motion_dec": t["pm_dec"],
        "epoch": t["epoch"],
    }
    ic = {
        "exposure_count": f["c_1_ic_1_exposure_count"],
        "exposure_time": f["c_1_ic_1_exposure_time"],
        "optical_elements": {},
        "mode": f["c_1_ic_1_readout_mode"],
    }
    # One entry per optical-element group of every instrument ("filters" -> "filter").
    for group in sorted(
        {g.rstrip("s") for i in instruments.values() for g in i["optical_elements"]}
    ):
        ic["optical_elements"][group] = f.get(f"c_1_ic_1_{group}")
    configuration = {
        "type": f["c_1_configuration_type"],
        "instrument_type": f["c_1_instrument_type"],
        "instrument_configs": [ic],
        "acquisition_config": {},
        "guiding_config": {},
        "constraints": {"max_airmass": f["c_1_max_airmass"]},
        "target": target,
    }
    if f.get("c_1_min_lunar_distance"):
        configuration["constraints"]["min_lunar_distance"] = f["c_1_min_lunar_distance"]
    if f.get("c_1_max_lunar_phase"):
        configuration["constraints"]["max_lunar_phase"] = f["c_1_max_lunar_phase"]
    return {
        "name": f["name"],
        "proposal": f["proposal"],
        "ipp_value": f["ipp_value"],
        "operator": "SINGLE",
        "observation_type": f["observation_mode"],
        "requests": [
            {
                "optimization_type": f["optimization_type"],
                "configuration_repeats": f["configuration_repeats"],
                "configurations": [configuration],
                "windows": [{"start": f["start"], "end": f["end"]}],
                "location": {"telescope_class": instruments[f["c_1_instrument_type"]]["class"]},
            }
        ],
    }


async def test_the_tom_ocs_facility_end_to_end(tom):
    """Form choices, validate, submit, status, observations, cancel: each
    call as OCSFacility makes it, each answer read as it reads it."""
    # OCSBaseForm._get_instruments / proposal_choices.
    r = await tom.get("tom", "/instruments/")
    assert r.status_code == 200
    instruments = r.json()
    assert set(instruments) == {"imager", "spectrograph"}
    imager = instruments["imager"]
    assert imager["class"] == "0m7" and imager["name"] == "imager"
    assert imager["default_configuration_type"] == "EXPOSE"
    assert [m["code"] for m in imager["modes"]["readout"]["modes"]] == ["default"]
    assert {c["code"] for c in imager["configuration_types"].values() if c["schedulable"]} >= {
        "EXPOSE",
        "AUTO_FOCUS",
    }
    assert instruments["spectrograph"]["default_configuration_type"] == "SPECTRUM"
    profile = (await tom.get("tom", "/profile/")).json()
    choices = [(p["id"], f"{p['title']} ({p['id']})") for p in profile["proposals"] if p["current"]]
    assert choices == [(PROPOSAL, f"RV follow-up ({PROPOSAL})")]

    payload = observation_payload(form_fields(), instruments)

    # OCSBaseObservationForm.validate_at_facility.
    v = (await tom.post("tom", "/requestgroups/validate/", payload)).json()
    assert v["errors"] == {} and v["request_durations"]["duration"] == 2 * 40.0 + 120.0
    bad = observation_payload(form_fields(proposal="someone-elses"), instruments)
    v = (await tom.post("tom", "/requestgroups/validate/", bad)).json()
    # Keyed "proposal": the form shows it on its proposal field.
    assert "not one of yours" in v["errors"]["proposal"][0]
    bad = observation_payload(form_fields(c_1_max_airmass=0.5), instruments)
    v = await tom.post("tom", "/requestgroups/validate/", bad)
    assert v.status_code == 200
    (key,) = v.json()["errors"]  # the form shows "key: message"
    assert key == "requests[0].configurations[0].constraints.max_airmass"

    # OCSFacility.submit_observation: [r['id'] for r in response.json()['requests']].
    r = await tom.post("tom", "/requestgroups/", payload)
    assert r.status_code == 202, r.text
    (obs_id,) = [x["id"] for x in r.json()["requests"]]

    # OCSFacility.get_observation_status.
    assert (await tom.get("tom", f"/requests/{obs_id}")).json()["state"] == "PENDING"
    assert (await tom.get("tom", f"/requests/{obs_id}/observations/")).json() == []

    # The sequencer takes it in and observes it.
    sched = tom.scheduler()
    await sched.poll_dropbox()
    req = sched.requests[obs_id]
    cfg = req.configurations[0]
    assert cfg.instrument_configs[0].count == 2 and cfg.instrument_configs[0].mode == "default"
    assert cfg.constraints.max_airmass == 1.6 and cfg.constraints.min_moon_sep == 30
    assert req.group.priority == 1.05 and req.group.proposal == PROPOSAL
    obs = None
    for tel in ("t1", "t2"):
        o = await sched.next_observation(tel)
        if isinstance(o, Observation) and o.name.startswith("AT 2026tom"):
            obs, where = o, tel
            break
    assert obs is not None
    sched.report(where, obs, "complete", 2)
    detail = (await tom.get("tom", f"/requests/{obs_id}")).json()
    assert detail.get("state") == "COMPLETED", detail
    blocks = (await tom.get("tom", f"/requests/{obs_id}/observations/")).json()
    assert [b["state"] for b in blocks] == ["COMPLETED"] and blocks[0]["telescope"] == where
    assert blocks[0]["start"] == NIGHT.isoformat()
    # Terminal in TOM's terms: the group is no longer pending.
    assert detail["state"] in ("COMPLETED", "WINDOW_EXPIRED", "CANCELED", "FAILURE_LIMIT_REACHED")


async def test_the_tom_cancel_flow_and_facility_status(tom):
    instruments = (await tom.get("tom", "/instruments/")).json()
    payload = observation_payload(form_fields(name="to cancel"), instruments)
    (obs_id,) = [
        x["id"] for x in (await tom.post("tom", "/requestgroups/", payload)).json()["requests"]
    ]
    # OCSFacility._get_requestgroup_id: GET /api/requestgroups?request_id=... (no slash).
    found = (await tom.get("tom", f"/requestgroups?request_id={obs_id}")).json()
    assert found["count"] == 1
    gid = found["results"][0]["id"]
    # OCSFacility.cancel_observation: response.json()['state'] == 'CANCELED'.
    r = await tom.post("tom", f"/requestgroups/{gid}/cancel/")
    assert r.json()["state"] == "CANCELED"
    assert (await tom.get("tom", f"/requests/{obs_id}")).json()["state"] == "CANCELED"
    # Canceling an accepted group answers CANCELED too (the scheduler does it next).
    payload = observation_payload(form_fields(name="accepted"), instruments)
    (rid,) = [
        x["id"] for x in (await tom.post("tom", "/requestgroups/", payload)).json()["requests"]
    ]
    await tom.scheduler().poll_dropbox()
    gid = (await tom.get("tom", f"/requestgroups?request_id={rid}")).json()["results"][0]["id"]
    r = await tom.post("tom", f"/requestgroups/{gid}/cancel/")
    assert r.json()["state"] == "CANCELED" and r.json()["cancel_pending"]

    # OCSFacility.get_facility_status: keys split into site.enclosure.telescope,
    # telescope_value[0]['event_type'].
    states = (await tom.get("tom", "/telescope_states/")).json()
    assert set(states) == {"flwo.aqawan1.t1", "flwo.aqawan1.t2"}
    for value in states.values():
        assert value[0]["event_type"] == "NOT_AVAILABLE"  # the supervisor is down here
        assert "supervisor unreachable" in value[0]["event_reason"]

    # A wrong token: 401, which OCSFacility turns into ImproperCredentials.
    r = await tom.client.get("/api/instruments/", headers={"Authorization": "Token nope"})
    assert r.status_code == 401


async def test_rapid_response_and_time_critical_from_the_tom_form(tom):
    """observation_mode RAPID_RESPONSE / TIME_CRITICAL are robofast ToOs:
    the too scope, a deadline at the window's end, and the type's policy."""
    instruments = (await tom.get("alerts", "/instruments/")).json()
    for mode, policy in (("RAPID_RESPONSE", "now-after-readout"), ("TIME_CRITICAL", "next")):
        f = form_fields(name=mode, observation_mode=mode, c_1_ic_1_exposure_count=1)
        payload = observation_payload(f, instruments)
        assert (await tom.post("tom", "/requestgroups/", payload)).status_code == 403
        r = await tom.post("alerts", "/requestgroups/", payload)
        assert r.status_code == 202, r.text
        too = r.json()["extra_params"]["too"]
        assert too["policy"] == policy and too["deadline"] == f["end"]


async def test_the_ocs_cadence_expansion(tom):
    """TOM's cadence form posts to /api/requestgroups/cadence/ and submits
    the answer."""
    instruments = (await tom.get("tom", "/instruments/")).json()
    payload = observation_payload(form_fields(), instruments)
    payload["requests"][0]["cadence"] = {
        "start": NIGHT.isoformat(),
        "end": (NIGHT + timedelta(hours=6)).isoformat(),
        "period": 2.0,
        "jitter": 1.0,
    }
    payload["requests"][0]["windows"] = []
    r = await tom.post("tom", "/requestgroups/cadence/", payload)
    assert r.status_code == 200, r.text
    expanded = r.json()
    assert expanded["operator"] == "MANY"
    windows = [x["windows"][0] for x in expanded["requests"]]
    assert windows[0] == {
        "start": NIGHT.isoformat(),
        "end": (NIGHT + timedelta(minutes=30)).isoformat(),
    }
    assert len(windows) == 3
    r = await tom.post("tom", "/requestgroups/", expanded)
    assert r.status_code == 202, r.text
    assert len(r.json()["requests"]) == 3
