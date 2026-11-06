"""Rubin alerts to ToO request groups, by the operator's rules (robofast.rubin_alerts)."""

from __future__ import annotations

import asyncio
import copy
import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from robofast.orbit import Orbit
from robofast.request_model import RequestGroup
from robofast.rubin_alerts import AlertConfig, decide, main, submit, why_not
from robofast.sky import Site
from robofast.testing.checks import wait_until
from tests.core.test_dispatch import Preempts
from tests.core.test_request_api import api, make_api  # noqa: F401 (fixtures)

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
MINERVA = Site(31.680407, -110.878977, 2316.0)
EXAMPLE = Path(__file__).resolve().parents[2] / "sites" / "minerva" / "rubin_alerts.example.json"

#: A transient: no Solar System association (LSST alert schema v11).
TRANSIENT = {
    "diaSourceId": 1001,
    "diaSource": {"diaSourceId": 1001, "ra": 52.5, "dec": 24.1, "band": "r", "psfFlux": 1.0e6},
    "diaObject": {"diaObjectId": 77},
}
#: Eris as Rubin sends a Solar System alert: its MPC orbit (JPL SBDB's
#: full-precision elements, epoch JD 2461200.5) and the predicted V.
ERIS = {
    "diaSourceId": 2002,
    "diaSource": {"diaSourceId": 2002, "ra": 26.8, "dec": -0.16, "band": "r", "psfFlux": 2.0e4},
    "ssSource": {"designation": "136199 Eris", "ephVmag": 18.7},
    "mpc_orbits": {
        "designation": "136199 Eris",
        "a": 67.93394687853566,
        "e": 0.4382385347971672,
        "i": 43.9258279471791,
        "node": 36.00477044417249,
        "argperi": 150.7949235840312,
        "mean_anomaly": 211.774434275007,
        "epoch_mjd": 61200.0,
    },
}
#: JPL Horizons, astrometric RA/Dec from Mt Hopkins (696), 2026-11-07 06:00 UTC.
ERIS_HORIZONS = (26.824934414, -0.158985202)


def config(**over) -> AlertConfig:
    d = json.loads(EXAMPLE.read_text())
    d.update(over)
    return AlertConfig.from_dict(d)


def sep_arcsec(a: tuple[float, float], b: tuple[float, float]) -> float:
    (r1, d1), (r2, d2) = (tuple(math.radians(x) for x in p) for p in (a, b))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(min(1.0, c))) * 3600.0


def test_a_bright_transient_becomes_a_valid_sidereal_too():
    group, rule = decide(TRANSIENT, config(), NOW)
    assert group is not None and rule == "bright transients"
    rg = RequestGroup.from_dict(group)  # what the request API will check
    assert rg.observation_type == "RAPID_RESPONSE" and rg.too.policy == "now-after-readout"
    c = rg.requests[0].configurations[0]
    assert (c.target.ra, c.target.dec) == (52.5, 24.1) and not c.target.moving
    assert group["requests"][0]["extra_params"]["telescopes"] == ["t1", "t2"]
    assert group["id"] == "rubin-1001" and group["name"].startswith(rule)


def test_a_tno_becomes_an_orbital_target_that_agrees_with_horizons():
    group, rule = decide(ERIS, config(), NOW)
    assert group is not None and rule == "TNOs"
    c = RequestGroup.from_dict(group).requests[0].configurations[0]
    assert c.target.moving and group["requests"][0]["configurations"][0]["guiding_config"] == {
        "mode": "OFF"
    }
    t = group["requests"][0]["configurations"][0]["target"]
    orbit = Orbit(**{k: v for k, v in t.items() if k not in ("type", "name")})
    assert sep_arcsec(orbit.place(NOW, MINERVA.location), ERIS_HORIZONS) < 0.5


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda a: a["diaSource"].update(psfFlux=-5.0), "magnitude"),  # fading: no magnitude
        (lambda a: a["diaSource"].update(psfFlux=1.0e3), "magnitude"),  # too faint (23.9)
        (lambda a: a["diaSource"].update(dec=-60.0), "dec"),
        (lambda a: a["diaSource"].update(band="u"), "band"),
    ],
)
def test_the_rules_say_which_transients_pass(change, why):
    alert = copy.deepcopy(TRANSIENT)
    change(alert)
    group, reasons = decide(alert, config(), NOW)
    assert group is None and why in reasons


def test_a_main_belt_asteroid_is_not_a_tno():
    alert = copy.deepcopy(ERIS)
    alert["mpc_orbits"].update(a=2.7, e=0.1)
    group, reasons = decide(alert, config(), NOW)
    assert group is None and "semimajor axis" in reasons


def test_the_first_matching_rule_wins():
    d = json.loads(EXAMPLE.read_text())
    first = copy.deepcopy(d["rules"][0])
    first["name"] = "anything"
    first["match"] = {}
    cfg = AlertConfig.from_dict({**d, "rules": [first, *d["rules"]]})
    assert decide(TRANSIENT, cfg, NOW)[1] == "anything"
    assert why_not(TRANSIENT, cfg.rules[1]) is None  # it would have matched too


def test_an_unbound_orbit_is_refused_with_the_reason():
    alert = copy.deepcopy(ERIS)
    alert["mpc_orbits"]["e"] = 1.2
    alert["mpc_orbits"]["a"] = 80.0  # (still passes the TNO rule's min_a)
    group, reasons = decide(alert, config(), NOW)
    assert group is None and "only bound orbits" in reasons


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({"rules": []}, "no rules"),
        ({"rules": [{"name": "x", "match": {"colour": "red"}, "too": {}}]}, "unknown keys"),
        ({"rules": [{"name": "x", "match": {}, "too": {"exposure_time": 1}}]}, "required"),
        ({"rules": [{"match": {"kind": "comet"}, "too": {}}]}, "kind"),
        ({"bogus": 1}, "unknown keys"),
    ],
)
def test_the_config_is_checked(over, error):
    with pytest.raises(ValueError, match=error):
        config(**over)


def test_the_cli_dry_run_prints_the_requests_and_submits_nothing(tmp_path, capsys):
    paths = []
    for name, alert in (("t.json", TRANSIENT), ("eris.json", ERIS)):
        p = tmp_path / name
        p.write_text(json.dumps(alert))
        paths.append(str(p))
    assert main(["submit", "--config", str(EXAMPLE), "--dry-run", *paths]) == 0
    out = capsys.readouterr().out
    assert '"rubin-1001"' in out and '"rubin-2002"' in out


async def test_an_alert_end_to_end_through_the_request_api_into_the_scheduler(
    api,  # noqa: F811 (the request API tests' fixture)
    tmp_path,
):
    """The transient, through the real request API (a token with the too
    scope), its drop box and the scheduler: taken in and preempting t1."""
    sched = api.scheduler()
    await sched.poll_dropbox()
    p = Preempts()
    sched.set_preempt_callback(p)
    base = await sched.next_observation("t1")
    serve = asyncio.create_task(sched.serve())
    token = tmp_path / "alerts.token"
    token.write_text(api.tokens["alerts"])
    try:
        group, _ = decide(TRANSIENT, config(), api.clock())
        group["requests"][0]["extra_params"]["telescopes"] = ["t1"]
        reply = await asyncio.to_thread(
            submit, group, {"url": api.server.url, "token_file": str(token)}
        )
        assert reply["intake"] == "QUEUED" and reply["too"]
        await wait_until(lambda: p.calls, 5, what="the preemption")
    finally:
        serve.cancel()
    assert p.calls == [("t1", "now-after-readout", "ToO rubin-1001-r")]
    g = (await api.get("alerts", "/requestgroups/rubin-1001")).json()
    assert g["intake"] == "ACCEPTED"
    sched.report("t1", base, "interrupted", 0)
    obs = await sched.next_observation("t1")
    assert obs.name.startswith("Rubin 77 [rubin-1001-c") and (obs.exptime, obs.count) == (120.0, 5)
    assert (obs.ra_hours, obs.dec_deg) == pytest.approx((52.5 / 15.0, 24.1))
