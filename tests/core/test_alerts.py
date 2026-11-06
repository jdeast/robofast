"""Alerts to ToO request groups, by the operator's rules (robofast.alerts):
each source's real payloads, the rules, the service, and one alert end to
end through the request API into the scheduler."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robofast.alerts import gcn, kmtnet, moa, ogle, rubin, tns, tocp
from robofast.alerts.core import Alert, decide, magnification, magnified, submit, why_not
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


# -- microlensing: OGLE, KMTNet, MOA ---------------------------------------------------------

#: The microlensing rule of MINERVA's example: its cadence tiers.
TIERED = next(r for r in json.loads(EXAMPLE.read_text())["rules"] if "cadence" in r["too"])["too"]


def page(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="latin-1")


def test_ogle_ews_rows_are_read_in_the_pages_format():
    """Made-up events in the EWS table's row format (Bulge and disk)."""
    alerts = {a.id: a for a in ogle.parse(page("ogle_ews_synthetic.html"))}
    assert len(alerts) == 6 and "OGLE-2026-DG-9001" in alerts and "OGLE-2026-GD-9001" in alerts
    a = alerts["OGLE-2026-BLG-9001"]  # 17:52:10.40 -29:12:05.3, Tmax 2461105.200, blended
    assert (a.ra, a.dec) == pytest.approx((268.0433333, -29.2014722), abs=1e-6)
    assert a.peak == datetime(2026, 3, 5, 16, 48, tzinfo=UTC)
    assert (a.te, a.u0, a.amax) == (24.5, 0.27, 3.804)
    assert (a.mag, a.band) == (pytest.approx(17.6 - 0.886), "I")  # I_bl less Dmag
    assert a.updates and a.kind == "sidereal"
    assert alerts["OGLE-2026-DG-9001"].dec == pytest.approx(-(7 + 5 / 60 + 40 / 3600))
    assert magnification(a.u0) == pytest.approx(a.amax, abs=0.01)  # the page's own Amax


def test_kmtnet_lines_are_read_in_its_text_lists_format():
    """Made-up events in the format of KMTNet's listpage.dat; t0 is HJD - 2400000."""
    alerts = {a.id: a for a in kmtnet.parse(page("kmtnet_listpage_synthetic.dat"))}
    assert len(alerts) == 4
    a = alerts["KMT-2026-BLG-9035"]
    assert a.classification == "clear" and a.aliases == ("ogle:OGLE-2026-BLG-9130",)  # not PRIME's
    assert (a.te, a.u0) == (98.0, 0.05) and a.amax == pytest.approx(magnification(0.05))
    assert alerts["KMT-2026-BLG-9001"].classification == "probable"
    assert alerts["KMT-2026-BLG-9061"].ignore == "KMTNet: not microlensing"
    # KMT-2026-BLG-9554 is OGLE-2026-BLG-9004: their fits' peaks agree, so the epochs do
    twin = {o.id: o for o in ogle.parse(page("ogle_ews_synthetic.html"))}["OGLE-2026-BLG-9004"]
    k = alerts["KMT-2026-BLG-9554"]
    assert k.aliases == ("ogle:OGLE-2026-BLG-9004",)
    assert abs((k.peak - twin.peak).total_seconds()) < 86400
    assert 3600 * abs(k.dec - twin.dec) < 2.0


def test_moa_rows_are_read_in_its_indexs_format():
    """Made-up events in the format of MOA's yearly index (made from u0 0.2: Amax 5.07)."""
    alerts = {a.id: a for a in moa.parse(page("moa_synthetic.html"))}
    assert len(alerts) == 3
    a = alerts["MOA-2026-BLG-9019"]  # t0 2026-04-27.50; u0 from Amax, the fit unblended
    assert a.u0 == pytest.approx(0.2, abs=5e-4) and a.amax == 5.07
    assert a.peak == datetime(2026, 4, 27, 12, tzinfo=UTC)
    assert a.mag == pytest.approx(17.5 - 2.5 * math.log10(5.07))
    assert a.time == datetime(2026, 11, 4, 12, 30, 0, 500000, tzinfo=UTC)


def test_the_windows_are_when_the_fit_predicts_the_magnification():
    a = Alert("ogle", "x", "x", 270.0, -29.0, peak=NOW, te=20.0, u0=0.05, amax=magnification(0.05))
    for level in (1.34, 10.0, 15.0):
        start, end = magnified(a, level)
        assert end - NOW == NOW - start
        u = math.hypot(a.u0, (end - NOW).total_seconds() / 86400 / a.te)
        assert magnification(u) == pytest.approx(level)
    assert magnified(a, 50.0) is None  # never that bright


def test_an_event_before_its_peak_becomes_standing_requests_by_tier():
    """OGLE-2026-BLG-9002 (made up: Amax 125, peak I 15.5 on 21 June) the day before."""
    a = {o.id: o for o in ogle.parse(page("ogle_ews_synthetic.html"))}["OGLE-2026-BLG-9002"]
    now = datetime(2026, 6, 20, 7, 0, tzinfo=UTC)
    group, rule = decide(a, rules(), now)
    assert rule == "microlensing events near a bright peak (OGLE, KMTNet, MOA)"
    g = RequestGroup.from_dict(group)  # valid in the request language
    assert group["observation_type"] == "NORMAL" and g.too is None and g.operator == "MANY"
    reqs = {r["id"]: r for r in group["requests"]}
    assert sorted(reqs) == [f"ogle-OGLE-2026-BLG-9002-r{k}" for k in (1, 2, 3)]
    cads = [r["extra_params"]["cadence"] for r in reqs.values()]
    assert cads == [{"min_interval": 43200.0, "max_per_night": 1}, {"min_interval": 3600.0},
                    {"min_interval": 600.0}]  # fmt: skip
    win = [(datetime.fromisoformat(r["windows"][0]["start"]),
            datetime.fromisoformat(r["windows"][0]["end"])) for r in reqs.values()]  # fmt: skip
    assert win[0][0] == win[1][0] == now and win[2][0] > now  # A >= 100 is still ahead
    assert win[0][1] > win[1][1] > win[2][1] > a.peak  # nested, the narrowest last
    conf = group["requests"][0]["configurations"][0]
    assert conf["instrument_configs"][0]["optical_elements"] == {"filter": "ip"}


def test_the_rules_take_microlensing_by_peak_time_and_magnification():
    alerts = {o.id: o for o in ogle.parse(page("ogle_ews_synthetic.html"))}
    now = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
    soon = alerts["OGLE-2026-BLG-9003"]  # Tmax 18 Nov 16:48; umin 0.000: Amax 99999, Dmag 12.5
    assert (soon.amax, soon.mag, soon.u0) == (None, None, 0.0)  # no peak given
    assert "magnitude None" in decide(soon, rules(), soon.peak)[1]
    assert "peak (t0" in decide(alerts["OGLE-2026-BLG-9002"], rules(), now)[1]  # June
    low = alerts["OGLE-2026-BLG-9001"]
    assert "peak magnification" in decide(low, rules(), low.peak)[1]  # A 3.8
    ignored = {k.id: k for k in kmtnet.parse(page("kmtnet_listpage_synthetic.dat"))}
    gone = ignored["KMT-2026-BLG-9061"]
    assert "not microlensing" in decide(gone, rules(), gone.peak)[1]


async def test_a_refit_event_is_followed_once_and_once_across_surveys(tmp_path):
    """KMT-2026-BLG-9554 is OGLE-2026-BLG-9004 (KMT names it; OGLE's own fit on
    the page has umin 0.000, no peak: here a refit gives it one). Made-up events."""
    state = tmp_path / "seen.json"
    o = {a.id: a for a in ogle.parse(page("ogle_ews_synthetic.html"))}
    k = {a.id: a for a in kmtnet.parse(page("kmtnet_listpage_synthetic.dat"))}
    kmt = k["KMT-2026-BLG-9554"]  # u0 0.045: A 22.2
    now = kmt.peak - timedelta(days=2)
    svc = AlertService(config(state=str(state)), dry_run=True)
    first = dataclasses.replace(kmt, u0=0.5, amax=magnification(0.5))  # an earlier fit: A 2.2
    await svc.handle([first], prime=True, now=now)  # never primed: it updates
    assert svc.submitted == [] and "kmtnet:KMT-2026-BLG-9554" not in svc.seen
    await svc.handle([kmt], now=now)  # refit: now it is taken
    assert [g["id"] for g in svc.submitted] == ["kmtnet-KMT-2026-BLG-9554"]
    end = max(r["windows"][0]["end"] for r in svc.submitted[0]["requests"])
    assert json.loads(state.read_text())["ids"]["kmtnet:KMT-2026-BLG-9554"] == end
    # OGLE's name for the star, once its fit has a peak: by position (OGLE names no twin)
    refit = dataclasses.replace(o["OGLE-2026-BLG-9004"], u0=0.045, amax=22.2, mag=16.0)
    assert decide(refit, rules(), now)[0] is not None
    again = AlertService(config(state=str(state)), dry_run=True)  # a restart
    await again.handle([kmt, refit], now=now)
    assert again.submitted == []
    # the other way round: OGLE's followed first, KMTNet's alias names it
    other = AlertService(config(state=str(tmp_path / "other.json")), dry_run=True)
    await other.handle([refit], now=now)
    await other.handle([dataclasses.replace(kmt, ra=0.0, dec=0.0)], now=now)  # not by position
    assert [g["id"] for g in other.submitted] == ["ogle-OGLE-2026-BLG-9004"]


def test_a_seen_file_of_the_previous_release_still_loads(tmp_path):
    from robofast.alerts.core import Seen

    path = tmp_path / "seen.json"
    path.write_text(json.dumps({"tocp:PNV x": NOW.isoformat()}))
    seen = Seen(path).load()
    assert "tocp:PNV x" in seen and seen.where == {}
    seen.save(NOW)
    assert json.loads(path.read_text()) == {"ids": {"tocp:PNV x": NOW.isoformat()}, "where": {}}


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
        ({"rules": [{"match": {"peak_hours": [5, 1]}, "too": TIERED}]}, "peak_hours"),
        (
            {"rules": [{"match": {}, "too": {**TIERED, "observation_type": "TIME_CRITICAL"}}]},
            "NORMAL",
        ),
        ({"rules": [{"match": {}, "too": {**TIERED, "cadence": []}}]}, "list of tiers"),
        (
            {"rules": [{"match": {}, "too": {**TIERED, "cadence": [{"min_interval": 60}]}}]},
            "magnified",
        ),
        (
            {"rules": [{"match": {}, "too": {**TIERED, "cadence": [{"magnified": 2, "x": 1}]}}]},
            "unknown",
        ),
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


async def test_the_service_primes_then_follows_only_new_alerts_once(tmp_path):
    state = tmp_path / "seen.json"
    old, new = _tocp_alert("PNV old", 10.0), _tocp_alert("PNV new", 11.0)
    svc = AlertService(config(state=str(state)), dry_run=True)
    await svc.handle([old], prime=True)  # what the page listed at the first start
    await svc.handle([old, new])
    assert [g["id"] for g in svc.submitted] == ["tocp-PNV_new"]
    again = AlertService(config(state=str(state)), dry_run=True)  # a restart
    await again.handle([old, new])
    assert again.submitted == []


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


async def test_a_microlensing_event_end_to_end_through_the_request_api_into_the_scheduler(
    api,  # noqa: F811 (the request API tests' fixture)
    tmp_path,
):
    """OGLE-2026-BLG-9002 (made up) the night before its peak (MINERVA, 20 June 2026,
    local midnight): one NORMAL group through the real request API (a token
    with the submit scope) into the scheduler, its tiers each a standing
    request: the nightly one once, the hourly one again only after an hour."""
    api.clock.t = datetime(2026, 6, 20, 7, 0, tzinfo=UTC)
    sched = api.scheduler()
    await sched.poll_dropbox()
    serve = asyncio.create_task(sched.serve())
    token = tmp_path / "alerts.token"
    token.write_text(api.tokens["tom"])  # read, submit
    d = json.loads(EXAMPLE.read_text())
    d["api"] = {"url": api.server.url, "token_file": str(token)}
    rule = next(r for r in d["rules"] if "cadence" in r["too"])
    rule["too"]["telescopes"] = ["t1"]
    del rule["too"]["filter"]  # the rig's t1 lists no filters
    cfg = AlertsConfig.from_dict(d)
    alert = {o.id: o for o in ogle.parse(page("ogle_ews_synthetic.html"))}["OGLE-2026-BLG-9002"]
    gid = "ogle-OGLE-2026-BLG-9002"
    try:
        group, _ = decide(alert, cfg.rules, api.clock())
        reply = await asyncio.to_thread(submit, group, cfg.api)
        assert reply["intake"] == "QUEUED" and not reply.get("too")

        async def accepted():
            return (await api.get("tom", f"/requestgroups/{gid}")).json()["intake"] == "ACCEPTED"

        for _ in range(250):
            if await accepted():
                break
            await asyncio.sleep(0.02)
        assert await accepted()
    finally:
        serve.cancel()
    taken = []
    for _ in range(3):  # the nightly tier, the hourly tier, then neither
        obs = await sched.next_observation("t1")
        if not getattr(obs, "name", "").startswith("OGLE-2026-BLG-9002"):  # Idle, or another
            break
        taken.append(obs.name.split("[")[1].split("#")[0])
        sched.report("t1", obs, "complete", obs.count)
    assert sorted(taken) == [f"{gid}-c1", f"{gid}-c2"]  # the 10-min tier opens tomorrow
    api.clock.advance(1800)
    obs = await sched.next_observation("t1")
    assert not getattr(obs, "name", "").startswith("OGLE-2026-BLG-9002")  # too soon
    api.clock.advance(1900)
    obs = await sched.next_observation("t1")
    assert f"[{gid}-c2" in getattr(obs, "name", "")  # the hourly tier, not the nightly
