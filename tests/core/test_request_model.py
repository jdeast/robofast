"""The OCS-shaped request model: loading, validation, round trips, and the
previous robofast JSON (still loaded for one release)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from robofast_core.request_model import (
    RequestGroup,
    Status,
    from_legacy_csv,
    load_request_groups,
    ocs_state,
    upgrade_legacy,
)

SITES = Path(__file__).resolve().parents[2] / "sites"

GROUP = {
    "id": "g1",
    "name": "a group",
    "proposal": "p",
    "ipp_value": 3,
    "operator": "SINGLE",
    "observation_type": "NORMAL",
    "requests": [
        {
            "id": "r1",
            "location": {"telescope_class": "0m7", "telescope": "t1"},
            "windows": [{"start": "2026-11-15T00:00:00Z", "end": "2026-11-16T00:00:00+00:00"}],
            "extra_params": {"cadence": {"min_interval": 3600, "max_per_night": 2}},
            "configurations": [
                {
                    "id": "c1",
                    "type": "EXPOSE",
                    "instrument_type": "imager",
                    "target": {"type": "ICRS", "name": "x", "ra": 22.5, "dec": -10},
                    "instrument_configs": [
                        {
                            "exposure_time": 30,
                            "exposure_count": 2,
                            "optical_elements": {"filter": "rp"},
                        }
                    ],
                    "constraints": {
                        "max_airmass": 2.0,
                        "min_lunar_distance": 20,
                        "extra_params": {"min_alt": 30},
                    },
                }
            ],
        }
    ],
}

# The same group in robofast's previous JSON.
LEGACY = {
    "id": "g1",
    "name": "a group",
    "proposal": "p",
    "priority": 3,
    "requests": [
        {
            "id": "r1",
            "telescopes": ["t1"],
            "windows": [{"start": "2026-11-15T00:00:00Z", "end": "2026-11-16T00:00:00+00:00"}],
            "cadence": {"min_interval": 3600, "max_per_night": 2},
            "configurations": [
                {
                    "id": "c1",
                    "instrument": "imager",
                    "target": {"name": "x", "ra_hours": 1.5, "dec_deg": -10},
                    "instrument_configs": [{"exptime": 30, "count": 2, "filter": "rp"}],
                    "constraints": {"max_airmass": 2.0, "min_alt": 30, "min_moon_sep": 20},
                }
            ],
        }
    ],
}


def test_round_trip():
    g = RequestGroup.from_dict(GROUP)
    again = RequestGroup.from_dict(g.to_dict())
    assert again.to_dict() == g.to_dict()
    r = g.requests[0]
    assert r.group is g and r.standing and not r.done
    assert r.telescopes == ["t1"] and r.location == {"telescope_class": "0m7"}
    c = r.configurations[0]
    assert c.status is Status.PENDING and c.status.schedulable
    assert c.constraints.min_alt == 30 and c.constraints.min_moon_sep == 20
    assert c.instrument_configs[0].filter == "rp" and c.instrument_configs[0].count == 2
    assert (c.target.ra, c.target.ra_hours, c.target.dec_deg) == (22.5, 1.5, -10.0)
    assert r.windows[0].start == datetime(2026, 11, 15, tzinfo=UTC)
    assert g.priority_class == 0 and g.priority == g.ipp_value == 3
    d = g.to_dict()
    assert d["state"] == "PENDING" and d["requests"][0]["state"] == "PENDING"
    assert d["requests"][0]["location"] == {"telescope_class": "0m7", "telescope": "t1"}


def test_windows():
    r = RequestGroup.from_dict(GROUP).requests[0]
    t = datetime(2026, 11, 15, 12, tzinfo=UTC)
    assert r.fits(t, t.replace(hour=13))
    assert not r.fits(t, datetime(2026, 11, 16, 1, tzinfo=UTC))
    assert r.window_end(t) == datetime(2026, 11, 16, tzinfo=UTC)
    assert not r.windows_over(t) and r.windows_over(datetime(2026, 11, 17, tzinfo=UTC))


def test_a_time_without_a_zone_is_utc_as_in_the_ocs():
    d = copy.deepcopy(GROUP)
    d["requests"][0]["windows"][0]["start"] = "2026-11-15T00:00:00"
    w = RequestGroup.from_dict(d).requests[0].windows[0]
    assert w.start == datetime(2026, 11, 15, tzinfo=UTC)


def _bad(path: list, value) -> dict:
    d = copy.deepcopy(GROUP)
    node = d
    for k in path[:-1]:
        node = node[k]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return d


C = ["requests", 0, "configurations", 0]
IC = [*C, "instrument_configs", 0]
OCS_CADENCE = {
    "start": "2026-11-15T00:00:00Z",
    "end": "2026-11-16T00:00:00Z",
    "period": 2,
    "jitter": 1,
}


@pytest.mark.parametrize(
    "path, value, match",
    [
        ([*C, "target", "dec"], 95, r"target.dec: 95 is outside"),
        ([*C, "target", "ra"], "1h", r"target.ra: expected a number"),
        ([*C, "target", "ra"], float("nan"), r"outside"),
        ([*C, "target", "ra"], 361, r"outside \[0, 360\]"),
        ([*C, "target", "type"], "ORBITAL_ELEMENTS", r"only ICRS"),
        ([*C, "target", "extra_params"], {"radial_velocity": "x"}, r"radial_velocity"),
        ([*C, "instrument_configs"], [], r"non-empty list"),
        ([*IC, "exposure_count"], 0, r"exposure_count: expected a positive integer"),
        ([*IC, "exposure_time"], -1, r"exposure_time"),
        ([*IC, "optical_elements"], {"slit": "1.0"}, r"unknown keys \['slit'\]"),
        ([*IC, "rotator_mode"], "VFLOAT", r"rotator_mode.*not supported"),
        ([*IC, "extra_params"], {"defocus": 1}, r"extra_params: unknown keys"),
        ([*C, "constraints", "max_airmass"], 0.5, r"max_airmass"),
        ([*C, "constraints", "max_lunar_phase"], 1.5, r"max_lunar_phase"),
        ([*C, "constraints", "max_seeing"], 2.0, r"max_seeing.*not supported"),
        ([*C, "constraints", "extra_params"], {"min_ha": 3, "max_ha": 2}, r"min_ha >= max_ha"),
        ([*C, "status"], "DONE", r"status"),
        ([*C, "type"], "LAMP_FLAT", r"type: 'LAMP_FLAT'"),
        ([*C, "repeat_duration"], 600, r"repeat_duration.*not supported"),
        ([*C, "guiding_config"], {"mode": "AUTO"}, r"guiding_config.mode"),
        ([*C, "exposure"], 3, r"unknown keys \['exposure'\]"),
        (["requests", 0, "windows", 0, "end"], "2026-11-14T00:00:00Z", r"end is not after"),
        (["requests", 0, "extra_params", "telescopes"], [], r"non-empty list of names"),
        (["requests", 0, "extra_params", "cadence", "max_per_night"], 0, r"positive integer"),
        (["requests", 0, "cadence"], OCS_CADENCE, r"cadence/"),
        (["requests", 0, "optimization_type"], "SPEED", r"optimization_type"),
        (["requests", 0, "configurations"], KeyError, r"missing \['configurations'\]"),
        (["operator"], "MANY_MORE", r"operator"),
        (["observation_type"], "DIRECT", r"observation_type: 'DIRECT'"),
        (["extra_params"], {"too": {"policy": "now"}}, r"only for observation_type"),
    ],
)
def test_validation(path, value, match):
    d = _bad(path, value)
    with pytest.raises(ValueError, match=match):
        RequestGroup.from_dict(d)


def test_single_takes_one_request_and_ids_are_unique():
    d = copy.deepcopy(GROUP)
    d["requests"].append(copy.deepcopy(d["requests"][0]))
    with pytest.raises(ValueError, match="SINGLE takes exactly one"):
        RequestGroup.from_dict(d)
    d["operator"] = "MANY"
    with pytest.raises(ValueError, match="duplicate"):
        RequestGroup.from_dict(d)
    with pytest.raises(ValueError, match="duplicate id 'g1'"):
        load_request_groups([GROUP, GROUP])


def _too(otype: str, too: dict | None = None) -> dict:
    d = copy.deepcopy(GROUP)
    del d["requests"][0]["extra_params"]
    d["observation_type"] = otype
    if too is not None:
        d["extra_params"] = {"too": too}
    return d


def test_rapid_response_and_time_critical_are_toos():
    rr = RequestGroup.from_dict(_too("RAPID_RESPONSE"))
    # Defaults: the deadline is the end of the last window.
    assert rr.too.policy == "now-after-readout" and rr.priority_class == 2
    assert rr.too.deadline == datetime(2026, 11, 16, tzinfo=UTC)
    tc = RequestGroup.from_dict(_too("TIME_CRITICAL"))
    assert tc.too.policy == "next" and tc.priority_class == 1
    g = RequestGroup.from_dict(
        _too(
            "RAPID_RESPONSE",
            {"deadline": "2026-11-15T10:00:00Z", "policy": "now", "priority_class": 3},
        )
    )
    assert g.priority_class == 3 and g.too.policy == "now"
    again = RequestGroup.from_dict(g.to_dict())
    assert again.too == g.too and again.to_dict()["observation_type"] == "RAPID_RESPONSE"
    d = _too("RAPID_RESPONSE")
    d["requests"][0]["extra_params"] = {"cadence": {"min_interval": 60}}
    with pytest.raises(ValueError, match="cadence"):
        RequestGroup.from_dict(d)
    with pytest.raises(ValueError, match=r"too.policy"):
        RequestGroup.from_dict(_too("RAPID_RESPONSE", {"policy": "asap"}))
    with pytest.raises(ValueError, match=r"priority_class"):
        RequestGroup.from_dict(_too("RAPID_RESPONSE", {"priority_class": 0}))
    d = _too("TIME_CRITICAL")
    d["requests"][0]["windows"] = []
    with pytest.raises(ValueError, match=r"deadline: required"):
        RequestGroup.from_dict(d)


def test_types_dark_bias_spectrum_and_autofocus():
    d = copy.deepcopy(GROUP)
    c = d["requests"][0]["configurations"][0]
    c["type"] = "DARK"
    g = RequestGroup.from_dict(d)
    cfg = g.requests[0].configurations[0]
    assert not cfg.light and not cfg.instrument_configs[0].light
    assert RequestGroup.from_dict(g.to_dict()).to_dict() == g.to_dict()
    assert "extra_params" not in g.to_dict()["requests"][0]["configurations"][0]
    c["type"] = "SPECTRUM"
    with pytest.raises(ValueError, match="instrument_type is 'spectrograph'"):
        RequestGroup.from_dict(d)
    c["instrument_type"] = "spectrograph"
    with pytest.raises(ValueError, match="no filter or port"):
        RequestGroup.from_dict(d)
    c["instrument_configs"] = [
        {"exposure_time": 600, "extra_params": {"iodine": True, "target_counts": 1e6}}
    ]
    g = RequestGroup.from_dict(d)
    cfg = g.requests[0].configurations[0]
    assert cfg.guide and cfg.instrument_configs[0].iodine is True
    assert g.to_dict()["requests"][0]["configurations"][0]["guiding_config"] == {"mode": "ON"}
    c["guiding_config"] = {"mode": "OFF"}
    with pytest.raises(ValueError, match="always guided"):
        RequestGroup.from_dict(d)
    c["type"], c["instrument_type"], c["guiding_config"] = "EXPOSE", "imager", {}
    with pytest.raises(ValueError, match="are for SPECTRUM"):
        RequestGroup.from_dict(d)
    c["type"] = "AUTO_FOCUS"
    c["instrument_configs"] = [{"exposure_time": 5}, {"exposure_time": 5}]
    with pytest.raises(ValueError, match="exactly one"):
        RequestGroup.from_dict(d)


def test_configuration_repeats_expand_in_order():
    d = copy.deepcopy(GROUP)
    d["requests"][0]["configuration_repeats"] = 3
    r = RequestGroup.from_dict(d).requests[0]
    assert [c.id for c in r.configurations] == ["c1", "c1.2", "c1.3"]


def test_ocs_state():
    p, done, x = Status.PENDING, Status.COMPLETED, Status.CANCELED
    assert ocs_state([p, done]) == "PENDING"
    assert ocs_state([done, done]) == "COMPLETED"
    assert ocs_state([done, x]) == "CANCELED"
    assert ocs_state([done, Status.FAILED]) == "FAILURE_LIMIT_REACHED"
    assert ocs_state([Status.EXPIRED]) == "WINDOW_EXPIRED"


def test_the_previous_json_still_loads():
    new = RequestGroup.from_dict(GROUP).to_dict()
    old = RequestGroup.from_dict(LEGACY).to_dict()
    # The legacy group has no telescope_class; otherwise the same group.
    new["requests"][0]["location"].pop("telescope_class")
    assert old == new
    assert upgrade_legacy(GROUP) is GROUP  # already OCS-shaped: untouched
    # A legacy ToO, guide flag, port, partial frames and astrometry.
    d = copy.deepcopy(LEGACY)
    del d["requests"][0]["cadence"]
    d["too"] = {"deadline": "2026-11-15T10:00:00Z", "policy": "now", "priority_class": 2}
    c = d["requests"][0]["configurations"][0]
    c["guide"] = True
    c["allow_partial"] = True
    c["target"].update(pm_ra=10.0, pm_dec=-5.0, parallax=3.0, rv=12.5, epoch=2000.0)
    c["instrument_configs"][0]["port"] = "imager"
    g = RequestGroup.from_dict(d)
    cfg = g.requests[0].configurations[0]
    assert g.too.policy == "now" and g.priority_class == 2
    assert g.to_dict()["observation_type"] == "RAPID_RESPONSE"
    assert cfg.guide and cfg.allow_partial and cfg.instrument_configs[0].port == "imager"
    assert cfg.target.astrometry() == {
        "pm_ra": 10.0,
        "pm_dec": -5.0,
        "epoch": 2000.0,
        "parallax": 3.0,
        "rv": 12.5,
    }
    out = g.to_dict()["requests"][0]["configurations"][0]
    assert out["target"]["proper_motion_ra"] == 10.0
    assert out["target"]["extra_params"] == {"radial_velocity": 12.5}
    d["too"]["policy"] = "next"
    assert RequestGroup.from_dict(d).to_dict()["observation_type"] == "TIME_CRITICAL"


def test_example_files_load():
    groups = load_request_groups(SITES / "minerva" / "requests.example.json")
    assert len(groups) >= 8
    assert all(c.status is Status.PENDING for g in groups for c in g.configurations())
    too = RequestGroup.from_dict(json.loads((SITES / "minerva" / "too.example.json").read_text()))
    assert too.too is not None and too.too.policy == "now-after-readout"
    # The examples are written in the OCS shape, not the deprecated one.
    for name in ("requests.example.json", "too.example.json"):
        data = json.loads((SITES / "minerva" / name).read_text())
        for g in data.get("groups", [data]):
            assert upgrade_legacy(g) is g, name


def test_legacy_csv(tmp_path):
    # The columns minerva-control's targetlist.mkdict reads (units in
    # parentheses are stripped, as the legacy reader did).
    path = tmp_path / "targets.csv"
    path.write_text(
        "name,ra (hours),dec (deg),exptime (s),priority,seplimit,maxobs,active,vmag\n"
        "HD 1,1.5,20,300,5,7200,3,1,6.1\n"
        "HD 2,2.5,30,600,2,,,1,7.0\n"
        "HD 3,3.5,40,600,9,,,0,5.0\n"
    )
    groups = from_legacy_csv(path)
    assert [g.name for g in groups] == ["HD 1", "HD 2"]  # HD 3 is inactive
    r1, r2 = groups[0].requests[0], groups[1].requests[0]
    assert r1.cadence.min_interval == 7200 and r1.cadence.max_per_night == 3
    assert r2.cadence.min_interval == 3 * 3600 and r2.cadence.max_per_night == 2  # legacy defaults
    c = r1.configurations[0]
    assert (c.target.ra_hours, c.target.dec_deg, c.instrument) == (1.5, 20.0, "spectrograph")
    assert c.type == "SPECTRUM" and c.target.ra == 22.5
    assert groups[0].priority == 5
    assert groups[0].to_dict()["requests"][0]["extra_params"]["cadence"]["max_per_night"] == 3
    path.write_text("name,ra,dec,exptime,priority,active\nHD 1,1.5,95,300,5,1\n")
    with pytest.raises(ValueError, match=r"targets.csv:2: .*target.dec"):
        from_legacy_csv(path)


# -- KiwiSpec is iodine only (JDE 2026-10-31) ---------------------------------------


def _spectrum(**conf) -> dict:
    c = {
        "id": "c",
        "type": "SPECTRUM",
        "instrument_type": "spectrograph",
        "target": {"type": "ICRS", "name": "HD 1", "ra": 10.0, "dec": 20.0},
        "instrument_configs": [{"exposure_time": 60.0}],
        **conf,
    }
    return {"id": "g", "requests": [{"id": "r", "configurations": [c]}]}


@pytest.mark.parametrize(
    "conf",
    [
        {"type": "ARC"},
        {"type": "LAMP_FLAT"},
        {"instrument_configs": [{"exposure_time": 1.0, "extra_params": {"lamp": "ThAr"}}]},
        {"instrument_configs": [{"exposure_time": 1.0, "extra_params": {"thar": True}}]},
        {"extra_params": {"calibration_lamp": "thar"}},
    ],
)
def test_lamp_calibrations_are_refused_with_the_reason(conf):
    with pytest.raises(ValueError, match="iodine only"):
        RequestGroup.from_dict(_spectrum(**conf))


def test_iodine_calibration_examples():
    """The B stars go through the iodine cell, once a night; templates
    without it, once."""
    groups = {g.id: g for g in load_request_groups(SITES / "minerva" / "requests.example.json")}
    bstars = [g for k, g in groups.items() if k.startswith("BSTAR-")]
    templates = [g for k, g in groups.items() if k.startswith("TEMPLATE-")]
    assert len(bstars) >= 2 and templates
    for g in bstars:
        [r] = g.requests
        [c] = r.configurations
        assert c.type == "SPECTRUM" and all(ic.iodine is True for ic in c.instrument_configs)
        assert r.cadence is not None and r.cadence.max_per_night == 1
        assert r.cadence.pool == "bstars"  # one of them a night, whichever
    for g in templates:
        [r] = g.requests
        [c] = r.configurations
        assert c.type == "SPECTRUM" and all(ic.iodine is False for ic in c.instrument_configs)
        assert r.cadence is None
