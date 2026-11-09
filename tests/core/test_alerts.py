"""Alerts to ToO request groups, by the operator's rules (robofast.alerts):
each source's real payloads, the rules, the service, and one alert end to
end through the request API into the scheduler."""

from __future__ import annotations

import asyncio
import copy
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robofast.alerts import gcn, rubin, tns, tocp
from robofast.alerts import service as service_mod
from robofast.alerts.core import Alert, decide, submit, why_not
from robofast.alerts.service import AlertsConfig, AlertService, main
from robofast.orbit import Orbit
from robofast.request_model import RequestGroup
from robofast.sky import Site
from robofast.testing.checks import wait_until
from tests.core.test_dispatch import Preempts
from tests.core.test_request_api import api, make_api  # noqa: F401 (fixtures)

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
MINERVA = Site(31.680407, -110.878977, 2316.0)
EXAMPLE = Path(__file__).resolve().parents[2] / "sites" / "minerva" / "alerts.example.json"
SAMPLES = Path(__file__).resolve().parent / "alert_samples"

#: A Rubin transient: no Solar System association (LSST alert schema v11).
TRANSIENT = {
    "diaSourceId": 1001,
    "diaSource": {
        "diaSourceId": 1001,
        "ra": 52.5,
        "dec": 24.1,
        "band": "r",
        "psfFlux": 1.0e6,
        "midpointMjdTai": 61351.25,
    },
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


def config(**over) -> AlertsConfig:
    d = json.loads(EXAMPLE.read_text())
    d.update(over)
    return AlertsConfig.from_dict(d)


def rules():
    return config().rules


def sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())


def sep_arcsec(a: tuple[float, float], b: tuple[float, float]) -> float:
    (r1, d1), (r2, d2) = (tuple(math.radians(x) for x in p) for p in (a, b))
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(min(1.0, c))) * 3600.0


# -- Rubin ---------------------------------------------------------------------------------


def test_a_bright_rubin_transient_becomes_a_valid_sidereal_too():
    group, rule = decide(rubin.from_rubin(TRANSIENT), rules(), NOW)
    assert group is not None and rule == "bright transients (Rubin)"
    rg = RequestGroup.from_dict(group)  # what the request API will check
    assert rg.observation_type == "RAPID_RESPONSE" and rg.too.policy == "now-after-readout"
    c = rg.requests[0].configurations[0]
    assert (c.target.ra, c.target.dec) == (52.5, 24.1) and not c.target.moving
    assert group["id"] == "rubin-1001" and group["name"].startswith(rule)


def test_a_tno_becomes_an_orbital_target_that_agrees_with_horizons():
    group, rule = decide(rubin.from_rubin(ERIS), rules(), NOW)
    assert group is not None and rule == "TNOs (Rubin)"
    conf = group["requests"][0]["configurations"][0]
    assert RequestGroup.from_dict(group).requests[0].configurations[0].target.moving
    assert conf["guiding_config"] == {"mode": "OFF"}
    t = conf["target"]
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
def test_the_rules_say_which_rubin_transients_pass(change, why):
    alert = copy.deepcopy(TRANSIENT)
    change(alert)
    group, reasons = decide(rubin.from_rubin(alert), rules(), NOW)
    assert group is None and why in reasons


def test_a_main_belt_asteroid_is_not_a_tno():
    alert = copy.deepcopy(ERIS)
    alert["mpc_orbits"].update(a=2.7, e=0.1)
    group, reasons = decide(rubin.from_rubin(alert), rules(), NOW)
    assert group is None and "semimajor axis" in reasons


def test_an_unbound_orbit_is_refused_with_the_reason():
    alert = copy.deepcopy(ERIS)
    alert["mpc_orbits"].update(e=1.2, a=80.0)
    group, reasons = decide(rubin.from_rubin(alert), rules(), NOW)
    assert group is None and "only bound orbits" in reasons


# -- CBAT TOCP ------------------------------------------------------------------------------


def test_tocp_rows_are_read_from_the_real_page():
    alerts = tocp.parse((SAMPLES / "tocp_2026-11-06.html").read_text())
    assert len(alerts) == 15
    by = {a.id: a for a in alerts}
    a = by["TCP J05210764+2338213"]  # 10.3 mag in Taurus, 2026 Oct 18.4020
    assert a.classification == "TCP" and (a.mag, a.band) == (10.3, "C")
    assert (a.ra, a.dec) == pytest.approx((80.281833, 23.639250), abs=1e-5)
    assert a.time == datetime(2026, 10, 18, tzinfo=UTC) + timedelta(days=0.4020)
    assert by["PNV J00433083+4126553"].classification == "PNV"


def test_a_bright_new_tocp_transient_is_followed_and_a_faint_or_old_one_is_not():
    alerts = {a.id: a for a in tocp.parse((SAMPLES / "tocp_2026-11-06.html").read_text())}
    soon = datetime(2026, 10, 19, tzinfo=UTC)
    group, rule = decide(alerts["TCP J05210764+2338213"], rules(), soon)
    assert group is not None and rule == "bright new transients (CBAT TOCP)"
    assert group["id"] == "tocp-TCP_J05210764+2338213"
    RequestGroup.from_dict(group)
    assert "magnitude" in decide(alerts["PNV J00433083+4126553"], rules(), soon)[1]
    late = soon + timedelta(days=3)
    assert "age" in decide(alerts["TCP J05210764+2338213"], rules(), late)[1]


# -- GCN ------------------------------------------------------------------------------------


def test_gcn_notices_from_nasas_schema_examples():
    ep = gcn.from_gcn(
        "gcn.notices.einstein_probe.wxt.alert", sample("einstein_probe_wxt_alert.example.json")
    )
    assert (ep.ra, ep.dec, ep.error) == (120, 40, 0.02) and ep.classification == "WXT"
    assert ep.time == datetime(2024, 3, 1, 21, 46, 5, 130000, tzinfo=UTC)
    bat = gcn.from_gcn(
        "gcn.notices.swift.bat.guano", sample("swift_bat_guano.loc_arc_min.example.json")
    )
    assert bat.classification == "GRB (BAT-GUANO)" and bat.error == 0.5
    gone = gcn.from_gcn(
        "gcn.notices.swift.bat.guano", sample("swift_bat_Guano.Retraction.example.json")
    )
    assert gone.ignore == "a retraction"
    svom = gcn.from_gcn(
        "gcn.notices.svom.trigger", sample("svom_trigger.eclairs_slewing.example.json")
    )
    assert svom.ignore == "a test notice"


def test_an_arcminute_burst_is_followed_and_a_degree_one_is_not():
    ep = gcn.from_gcn(
        "gcn.notices.einstein_probe.wxt.alert", sample("einstein_probe_wxt_alert.example.json")
    )
    soon = ep.time + timedelta(minutes=10)
    group, rule = decide(ep, rules(), soon)
    assert group is not None and rule == "bursts with arcminute localizations (GCN)"
    assert RequestGroup.from_dict(group).too.policy == "now"
    gbm = gcn.from_gcn("gcn.notices.fermi.gbm", sample("fermi_gbm_trigger.final.example.json"))
    assert "localization" in decide(gbm, rules(), gbm.time + timedelta(minutes=10))[1]
    swift = gcn.from_gcn(
        "gcn.notices.swift.bat.guano", sample("swift_bat_guano.loc_arc_min.example.json")
    )
    assert "localization" in decide(swift, rules(), soon)[1]  # 0.5 deg: wider than the field


# -- TNS ------------------------------------------------------------------------------------

#: The fields robofast reads of a TNS object reply (TNS API documentation).
TNS_OBJECT = {
    "objname": "2026abc",
    "name_prefix": "SN",
    "radeg": 150.25,
    "decdeg": 20.5,
    "discoverymag": 14.2,
    "discmagfilter": {"name": "o"},
    "discoverydate": "2026-11-06 08:00:00.000",
    "object_type": {"name": "SN II"},
}


def test_a_tns_object_becomes_an_alert_and_a_classified_bright_sn_is_followed():
    a = tns.from_tns(TNS_OBJECT)
    assert (a.name, a.classification, a.mag, a.band) == ("SN 2026abc", "SN II", 14.2, "o")
    group, rule = decide(a, rules(), NOW)
    assert group is not None and rule == "classified bright supernovae and novae (TNS)"
    faint = tns.from_tns({**TNS_OBJECT, "discoverymag": 18.0})
    assert "magnitude" in decide(faint, rules(), NOW)[1]
    agn = tns.from_tns({**TNS_OBJECT, "object_type": {"name": "AGN"}})
    assert "classification" in decide(agn, rules(), NOW)[1]


# -- the rules and the config ----------------------------------------------------------------


def test_the_first_matching_rule_wins():
    d = json.loads(EXAMPLE.read_text())
    first = copy.deepcopy(d["rules"][3])
    first["name"] = "anything"
    first["match"] = {}
    cfg = AlertsConfig.from_dict({**d, "rules": [first, *d["rules"]]})
    a = rubin.from_rubin(TRANSIENT)
    assert decide(a, cfg.rules, NOW)[1] == "anything"
    assert why_not(a, cfg.rules[4], NOW) is None  # it would have matched too


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({"rules": []}, "no rules"),
        ({"rules": [{"name": "x", "match": {"colour": "red"}, "too": {}}]}, "unknown keys"),
        ({"rules": [{"name": "x", "match": {}, "too": {"exposure_time": 1}}]}, "required"),
        ({"rules": [{"match": {"kind": "comet"}, "too": {}}]}, "kind"),
        ({"rules": [{"match": {"sources": ["ztf"]}, "too": {}}]}, "sources"),
        ({"sources": {"gcn": {"client_id_file": "x", "client_secret_file": "y"}}}, "topics"),
        ({"sources": {"tocp": {"poll": 1}}}, "unknown keys"),
        ({"bogus": 1}, "unknown keys"),
    ],
)
def test_the_config_is_checked(over, error):
    with pytest.raises(ValueError, match=error):
        config(**over)


# -- the service ------------------------------------------------------------------------------


def _tocp_alert(name: str, ra: float) -> Alert:
    return Alert(
        "tocp",
        name,
        name,
        ra,
        20.0,
        mag=9.0,
        band="V",
        time=datetime.now(UTC),
        classification="PNV",
    )


async def test_the_service_primes_then_follows_only_new_alerts_once(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(service_mod, "submit", lambda g, _cfg: sent.append(g["id"]) or {})
    state = tmp_path / "seen.json"
    old, new = _tocp_alert("PNV old", 10.0), _tocp_alert("PNV new", 11.0)
    svc = AlertService(config(state=str(state)))
    await svc.handle([old], prime=True)  # what the page listed at the first start
    await svc.handle([old, new])
    assert [g["id"] for g in svc.submitted] == sent == ["tocp-PNV_new"]
    again = AlertService(config(state=str(state)))  # a restart
    await again.handle([old, new])
    assert again.submitted == [] and sent == ["tocp-PNV_new"]


async def test_a_dry_run_writes_no_state(tmp_path):
    """night1108: ``--dry-run submit`` wrote the seen-state, and crashed after
    the work when its directory did not exist. A dry run writes nothing."""
    state = tmp_path / "missing" / "seen.json"
    svc = AlertService(config(state=str(state)), dry_run=True)
    await svc.handle([_tocp_alert("PNV new", 11.0)])
    assert [g["id"] for g in svc.submitted] == ["tocp-PNV_new"]
    assert not (tmp_path / "missing").exists()


async def test_a_missing_state_directory_is_made_at_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "submit", lambda g, _cfg: {})
    state = tmp_path / "a" / "b" / "seen.json"
    svc = AlertService(config(state=str(state)))
    assert state.parent.is_dir()  # before any alert
    await svc.handle([_tocp_alert("PNV new", 11.0)])
    assert "tocp:PNV new" in json.loads(state.read_text())


def test_an_unusable_state_directory_stops_the_service_at_startup(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")  # a file where the directory should be
    cfg = tmp_path / "alerts.json"
    d = json.loads(EXAMPLE.read_text())
    d["state"] = str(blocker / "seen.json")
    cfg.write_text(json.dumps(d))
    alert = tmp_path / "t.json"
    alert.write_text(json.dumps(TRANSIENT))
    assert main(["submit", "--config", str(cfg), "--source", "rubin", str(alert)]) == 2
    err = capsys.readouterr().err
    assert "alerts: state" in err and str(blocker) in err


def test_the_cli_dry_run_puts_files_through_the_rules(tmp_path, capsys):
    paths = []
    for name, alert in (("t.json", TRANSIENT), ("eris.json", ERIS)):
        p = tmp_path / name
        p.write_text(json.dumps(alert))
        paths.append(str(p))
    cfg = tmp_path / "alerts.json"
    d = json.loads(EXAMPLE.read_text())
    d["state"] = str(tmp_path / "seen.json")
    cfg.write_text(json.dumps(d))
    assert main(["submit", "--config", str(cfg), "--source", "rubin", "--dry-run", *paths]) == 0
    out = capsys.readouterr().out
    assert '"rubin-1001"' in out and '"rubin-2002"' in out
    assert not (tmp_path / "seen.json").exists()  # a dry run writes nothing


async def test_an_alert_end_to_end_through_the_request_api_into_the_scheduler(
    api,  # noqa: F811 (the request API tests' fixture)
    tmp_path,
):
    """A Rubin transient through the real request API (a token with the too
    scope), its drop box and the scheduler: taken in and preempting t1."""
    sched = api.scheduler()
    await sched.poll_dropbox()
    p = Preempts()
    sched.set_preempt_callback(p)
    base = await sched.next_observation("t1")
    serve = asyncio.create_task(sched.serve())
    token = tmp_path / "alerts.token"
    token.write_text(api.tokens["alerts"])
    d = json.loads(EXAMPLE.read_text())
    d["api"] = {"url": api.server.url, "token_file": str(token)}
    d["rules"][3]["too"]["telescopes"] = ["t1"]
    cfg = AlertsConfig.from_dict(d)
    try:
        group, _ = decide(rubin.from_rubin(TRANSIENT), cfg.rules, api.clock())
        reply = await asyncio.to_thread(submit, group, cfg.api)
        assert reply["intake"] == "QUEUED" and reply["too"]
        await wait_until(lambda: p.calls, 5, what="the preemption")
    finally:
        serve.cancel()
    assert p.calls == [("t1", "now-after-readout", "ToO rubin-1001-r")]
    assert (await api.get("alerts", "/requestgroups/rubin-1001")).json()["intake"] == "ACCEPTED"
    sched.report("t1", base, "interrupted", 0)
    obs = await sched.next_observation("t1")
    assert obs.name.startswith("Rubin 77 [rubin-1001-c") and (obs.exptime, obs.count) == (120.0, 5)
