"""One night rule for every night name (robofast/clock.py: NightRule).

The regression: PR #140's e2e rehearsal started at 19:02 UTC (12:02 MST)
and named its frames and spectra ``n20261108`` but its plan record
``plan-2026-11-09``. The plan record had ``America/Phoenix`` (the dispatch
plan's ``record``), the frame writer and the spectrum sidecars no time zone
(``images``), so they reckoned the night in mean solar time at the site's
longitude (UTC-7.39 h), whose noon is 19:23 UTC: for 23 minutes after
local noon every day the two disagreed. The KiwiSpec service's own night,
for a request naming none, was the bare UT date, which flips at 17:00 MST
during the evening calibrations.

Here every component that names a night is built from the shipped MINERVA
configs, the way the sequencer, the dispatch scheduler, the request API and
the KiwiSpec service build them, and asked for the night at each boundary:
local noon (the rollover), 00:00 UT (17:00 MST) and local midnight.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robofast.clock import NightRule, plan_with_night_rule
from robofast.config import load_config
from robofast.devices import kiwispec_service
from robofast.dispatch import dispatch_from_config
from robofast.sequencer_service import build
from robofast.timeline import Timeline

SITES = Path(__file__).resolve().parents[2] / "sites" / "minerva"
S = timedelta(seconds=1)
#: (UTC time, the night MINERVA's convention names: the MST date of the evening).
BOUNDARIES = [
    # Local noon, the rollover: 19:00 UTC = 12:00 MST.
    (datetime(2026, 11, 9, 19, 0, tzinfo=UTC) - S, "n20261108"),
    (datetime(2026, 11, 9, 19, 0, tzinfo=UTC), "n20261109"),
    # PR #140's e2e failure: 12:02 MST, before mean solar noon (19:23 UTC).
    (datetime(2026, 11, 9, 19, 2, 27, tzinfo=UTC), "n20261109"),
    (datetime(2026, 11, 9, 19, 22, tzinfo=UTC), "n20261109"),
    # 00:00 UT = 17:00 MST, the evening calibrations: no split.
    (datetime(2026, 11, 10, 0, 0, tzinfo=UTC) - S, "n20261109"),
    (datetime(2026, 11, 10, 0, 0, tzinfo=UTC), "n20261109"),
    # Local midnight: 07:00 UTC.
    (datetime(2026, 11, 10, 7, 0, tzinfo=UTC) - S, "n20261109"),
    (datetime(2026, 11, 10, 7, 0, tzinfo=UTC), "n20261109"),
    # Late morning: still last night until noon.
    (datetime(2026, 11, 10, 18, 59, 59, tzinfo=UTC), "n20261109"),
]


def _plan() -> dict:
    """The shipped dispatch plan, with no requests and a plan record."""
    plan = load_config(SITES / "dispatch.example.json")
    for key in ("_note", "state", "too_dropbox"):
        plan.pop(key, None)
    plan.update(type="dispatch", requests=[], record={"dir": "plans"})
    for lim in plan["telescopes"].values():
        lim.pop("filters", None)
    return plan


def _components(tmp_path: Path) -> dict[str, object]:
    """Night namers built from the shipped configs (module doc)."""
    config = load_config(SITES / "aqawan1.json")
    config["images"] = {**config["images"], "data_root": "frames", "imagebytes": False}
    plan = _plan()
    config["plan"] = plan
    seq, _ = build(config, Timeline(), base=tmp_path)
    record = seq.plan.record
    assert record is not None
    kcfg = json.loads((SITES / "kiwispec.example.json").read_text())
    kiwi = kiwispec_service.KiwiSpec(
        kiwispec_service.KiwiSpecConfig.from_dict(kcfg),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    # The request API's view: the plan entry with the enclosure's site.
    view = dispatch_from_config(
        plan_with_night_rule({k: v for k, v in plan.items() if k != "type"}, config["site"]),
        list(config["telescopes"]),
        base=tmp_path,
    )
    return {
        "frames": lambda t: f"n{seq.imaging.store.night(t):%Y%m%d}",
        "sidecars": seq.spectra.night,
        "raw spectra (sequencer)": seq._night_name,
        "raw spectra (KiwiSpec, no night given)": lambda t: kiwispec_service.night(
            t, kiwi.night_rule
        ),
        "plan record": lambda t: "n" + record.path(t).stem.split("-", 1)[1].replace("-", ""),
        "request API data paths": lambda t: f"n{view.night_rule.date(t):%Y%m%d}",
    }


@pytest.mark.parametrize(("when", "want"), BOUNDARIES, ids=lambda x: str(x))
def test_every_component_names_the_same_night_at_each_boundary(tmp_path, when, want):
    names = {what: name(when) for what, name in _components(tmp_path).items()}
    assert names == dict.fromkeys(names, want), f"{when:%H:%M:%S} UTC: {names}"


def test_minerva_rule_is_the_mst_date_of_the_evening():
    rule = NightRule.from_site(load_config(SITES / "aqawan1.json")["site"])
    assert rule == NightRule("America/Phoenix")
    # Without a time zone: standard time (UTC-7 at -110.88), the same names.
    standard = NightRule(longitude=-110.878977)
    for when, want in BOUNDARIES:
        assert rule.name(when) == standard.name(when) == want
    with pytest.raises(ValueError, match="aware"):
        rule.name(datetime(2026, 11, 9, 19))


def test_the_night_keys_live_only_in_the_site(tmp_path):
    config = load_config(SITES / "aqawan1.json")
    config["images"] = {**config["images"], "data_root": "frames", "timezone": "America/Phoenix"}
    with pytest.raises(ValueError, match="moved to the config's 'site'"):
        build(config, Timeline(), base=tmp_path)
    # A plan whose site gives another rule than the enclosure's is refused,
    # not left to name half the night's files differently.
    plan = {"site": {"latitude": 31.68, "longitude": -110.88, "timezone": "America/Denver"}}
    with pytest.raises(ValueError, match="disagrees"):
        plan_with_night_rule(plan, config["site"])
    # A plan site without the keys takes the enclosure's.
    got = plan_with_night_rule({"site": {"latitude": 31.68, "longitude": -110.88}}, config["site"])
    assert got["site"]["timezone"] == "America/Phoenix"


def test_a_plan_record_on_another_rule_is_refused(tmp_path):
    """The rehearsal's mis-configuration, caught at start: a plan (here from
    its own file, so the enclosure site cannot fill it in) whose record names
    nights by another rule than the frames."""
    config = load_config(SITES / "aqawan1.json")
    config["images"] = {**config["images"], "data_root": "frames", "imagebytes": False}
    config["site"] = {k: v for k, v in config["site"].items() if k != "timezone"}
    config["site"]["night_rollover_hour"] = 15
    from robofast.plan import plan_from_config

    p = plan_from_config(_plan(), ["t1", "t2"], base=tmp_path)
    with pytest.raises(ValueError, match="plan record names nights"):
        build(config, Timeline(), plan=p, base=tmp_path)
