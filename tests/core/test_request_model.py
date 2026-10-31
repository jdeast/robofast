"""The OCS-shaped request model: loading, validation, round trips."""

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
)

SITES = Path(__file__).resolve().parents[2] / "sites"

GROUP = {
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
                    "constraints": {"min_alt": 30, "min_moon_sep": 20},
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
    c = r.configurations[0]
    assert c.status is Status.PENDING and c.status.schedulable
    assert c.constraints.min_alt == 30 and c.instrument_configs[0].filter == "rp"
    assert r.windows[0].start == datetime(2026, 11, 15, tzinfo=UTC)
    assert g.priority_class == 0


def test_windows():
    r = RequestGroup.from_dict(GROUP).requests[0]
    t = datetime(2026, 11, 15, 12, tzinfo=UTC)
    assert r.fits(t, t.replace(hour=13))
    assert not r.fits(t, datetime(2026, 11, 16, 1, tzinfo=UTC))
    assert r.window_end(t) == datetime(2026, 11, 16, tzinfo=UTC)
    assert not r.windows_over(t) and r.windows_over(datetime(2026, 11, 17, tzinfo=UTC))


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


@pytest.mark.parametrize(
    "path, value, match",
    [
        ([*C, "target", "dec_deg"], 95, r"dec_deg: 95 is outside"),
        ([*C, "target", "ra_hours"], "1h", r"ra_hours: expected a number"),
        ([*C, "target", "ra_hours"], float("nan"), r"outside"),
        ([*C, "instrument_configs"], [], r"non-empty list"),
        ([*C, "instrument_configs", 0, "count"], 0, r"positive integer"),
        ([*C, "constraints", "max_airmass"], 0.5, r"max_airmass"),
        ([*C, "constraints"], {"min_ha": 3, "max_ha": 2}, r"min_ha >= max_ha"),
        ([*C, "status"], "DONE", r"status"),
        ([*C, "exposure"], 3, r"unknown keys \['exposure'\]"),
        (["requests", 0, "windows", 0, "end"], "2026-11-16T00:00:00", r"no time zone"),
        (["requests", 0, "windows", 0, "end"], "2026-11-14T00:00:00Z", r"end is not after"),
        (["requests", 0, "telescopes"], [], r"non-empty list of names"),
        (["requests", 0, "cadence", "max_per_night"], 0, r"positive integer"),
        (["requests", 0, "configurations"], KeyError, r"missing \['configurations'\]"),
        (["too"], {"deadline": "2026-11-15T10:00:00Z", "policy": "asap"}, r"policy"),
        (["too"], {"policy": "now"}, r"missing \['deadline'\]"),
        (["too"], {"deadline": "2026-11-15T10:00:00Z", "priority_class": 0}, r"priority_class"),
    ],
)
def test_validation(path, value, match):
    d = _bad(path, value)
    with pytest.raises(ValueError, match=match):
        RequestGroup.from_dict(d)


def test_too_cannot_have_a_cadence_and_ids_are_unique():
    d = copy.deepcopy(GROUP)
    d["too"] = {"deadline": "2026-11-15T10:00:00Z"}
    with pytest.raises(ValueError, match="cadence"):
        RequestGroup.from_dict(d)
    d = copy.deepcopy(GROUP)
    d["requests"][0]["configurations"][0]["id"] = "r1"
    with pytest.raises(ValueError, match="duplicate"):
        RequestGroup.from_dict(d)
    with pytest.raises(ValueError, match="duplicate id 'g1'"):
        load_request_groups([GROUP, GROUP])


def test_too_group():
    d = copy.deepcopy(GROUP)
    del d["requests"][0]["cadence"]
    d["too"] = {"deadline": "2026-11-15T10:00:00Z", "policy": "now", "priority_class": 2}
    g = RequestGroup.from_dict(d)
    assert g.priority_class == 2 and g.too.policy == "now"
    assert RequestGroup.from_dict(g.to_dict()).too == g.too


def test_example_files_load():
    groups = load_request_groups(SITES / "minerva" / "requests.example.json")
    assert len(groups) >= 8
    assert all(c.status is Status.PENDING for g in groups for c in g.configurations())
    too = RequestGroup.from_dict(json.loads((SITES / "minerva" / "too.example.json").read_text()))
    assert too.too is not None and too.too.policy == "now-after-readout"


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
    assert groups[0].priority == 5
    path.write_text("name,ra,dec,exptime,priority,active\nHD 1,1.5,95,300,5,1\n")
    with pytest.raises(ValueError, match=r"targets.csv:2: .*dec_deg"):
        from_legacy_csv(path)
