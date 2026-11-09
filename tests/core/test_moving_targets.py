"""Moving targets (docs/moving_targets.md): OCS ORBITAL_ELEMENTS targets,
their places against JPL Horizons, the request model's checks, the dispatch
scheduler's places, and the sequencer following one, with and without the
mount's tracking rates."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta

import pytest

from robofast import sky
from robofast.dispatch import DispatchScheduler, TelescopeLimits
from robofast.orbit import Orbit
from robofast.plan import Observation, StaticPlan
from robofast.request_model import RequestGroup, load_request_groups
from robofast.testing.checks import wait_until

MINERVA = sky.Site(31.680407, -110.878977, 2316.0)

#: The MPC's elements (CometEls.txt, 5 Nov 2026) and JPL Horizons'
#: astrometric places and rates at MINERVA (QUANTITIES 1,3, user-supplied
#: elements; for 433 Eros, Horizons' own orbit, with its osculating elements
#: at 2026-11-10 0h TDB), fetched 5 Nov 2026: (UTC, RA deg, Dec deg,
#: dRA cos Dec "/h, dDec "/h).
HORIZONS = {
    "P/2009 B1": (
        {
            "scheme": "MPC_COMET",
            "epochofel": 61349.0,
            "orbinc": 22.2604,
            "longascnode": 297.0736,
            "argofperih": 129.0809,
            "eccentricity": 0.636157,
            "perihdist": 2.448047,
            "epochofperih": 61221.2434,
        },
        [
            ("2026-11-10T01:00", 132.695169475, 22.16509996, 20.95806, -19.9741),
            ("2026-11-10T07:00", 132.732221618, 22.13222798, 19.91198, -19.6424),
            ("2026-11-10T13:00", 132.766982801, 22.09938406, 18.87606, -19.9336),
        ],
    ),
    "C/2022 N2": (  # hyperbolic
        {
            "scheme": "MPC_COMET",
            "epochofel": 61349.0,
            "orbinc": 5.5035,
            "longascnode": 319.7265,
            "argofperih": 75.5164,
            "eccentricity": 1.004753,
            "perihdist": 3.828272,
            "epochofperih": 60888.3769,
        },
        [
            ("2026-11-10T01:00", 113.993669609, 25.348445822, 0.171085, 0.477943),
            ("2026-11-10T07:00", 113.993475299, 25.349391126, -0.44569, 0.598272),
            ("2026-11-10T13:00", 113.992193926, 25.350243315, -0.80342, 0.399658),
        ],
    ),
    "433 Eros": (
        {
            "scheme": "MPC_MINOR_PLANET",
            "epochofel": 61354.0,
            "orbinc": 10.82860916740598,
            "longascnode": 304.2678348087441,
            "argofperih": 178.9257055417879,
            "eccentricity": 0.2228473311977586,
            "meandist": 1.458276549926741,
            "meananom": 148.6988578851914,
        },
        [
            ("2026-11-10T01:00", 262.629475408, -26.118358441, 88.67411, 4.069308),
            ("2026-11-10T07:00", 262.795148105, -26.111849721, 89.79265, 4.1153),
            ("2026-11-10T13:00", 262.96199762, -26.105766468, 89.67383, 3.711605),
        ],
    ),
}


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def sep_arcsec(ra1: float, de1: float, ra2: float, de2: float) -> float:
    a1, d1, a2, d2 = map(math.radians, (ra1, de1, ra2, de2))
    h = math.sin((d2 - d1) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((a2 - a1) / 2) ** 2
    return math.degrees(2 * math.asin(math.sqrt(h))) * 3600


@pytest.fixture(autouse=True)
def _offline():
    sky.offline_iers()


# -- the orbit ---------------------------------------------------------------------


@pytest.mark.parametrize("name", list(HORIZONS))
def test_places_and_rates_agree_with_horizons(name):
    """Within 0.1" and 1"/h of Horizons (measured: 0.006-0.028", 0.26"/h at
    most, over 25 times of the night)."""
    elements, rows = HORIZONS[name]
    orbit = Orbit(**elements)
    for t, ra, dec, dra, ddec in rows:
        pra, pdec = orbit.place(utc(t), MINERVA.location)
        assert sep_arcsec(pra, pdec, ra, dec) < 0.1, (name, t)
        rra, rdec = orbit.rate(utc(t), MINERVA.location)
        assert abs(rra * 3600 - dra) < 1.0 and abs(rdec * 3600 - ddec) < 1.0, (name, t)


def test_the_site_matters():
    """Diurnal parallax: P/2009 B1 at 2.6 au moves a few arcsec between the
    geocenter and MINERVA (so the site is not optional for a mount)."""
    orbit = Orbit(**HORIZONS["P/2009 B1"][0])
    t = utc("2026-11-10T07:00")
    assert 1.0 < sep_arcsec(*orbit.place(t), *orbit.place(t, MINERVA.location)) < 10.0


def test_one_period_later_is_the_same_place_from_the_sun():
    orbit = Orbit(**HORIZONS["433 Eros"][0])
    a = orbit.meandist
    period = 2 * math.pi / (0.01720209895 / a**1.5)
    p0, p1 = orbit.heliocentric(61354.0), orbit.heliocentric(61354.0 + period)
    assert max(abs(p0 - p1)) < 1e-9


@pytest.mark.parametrize(
    "change, error",
    [
        ({"scheme": "JPL_MAJOR_PLANET"}, "major planet"),
        ({"scheme": "ASA_COMET"}, "send MPC_COMET"),
        ({"eccentricity": 1.2}, "must be < 1"),
        ({"eccentricity": 0.95}, "over 0.9"),
        ({"meananom": None}, "meananom is required"),
    ],
)
def test_bad_orbits_are_refused(change, error):
    with pytest.raises(ValueError, match=error):
        Orbit(**{**HORIZONS["433 Eros"][0], **change})


# -- the request model ---------------------------------------------------------------


def orbital_target(name: str = "P/2009 B1", **change) -> dict:
    return {"type": "ORBITAL_ELEMENTS", "name": name, **HORIZONS[name][0], **change}


def group(target: dict, ctype: str = "EXPOSE", guide: str = "OFF", **request) -> dict:
    return {
        "id": "g",
        "ipp_value": 1.0,
        "observation_type": "NORMAL",
        "requests": [
            {
                "id": "r",
                "configurations": [
                    {
                        "id": "c",
                        "type": ctype,
                        "instrument_type": "imager",
                        "target": target,
                        "instrument_configs": [{"exposure_time": 60.0, "exposure_count": 3}],
                        "guiding_config": {"mode": guide},
                    }
                ],
                **request,
            }
        ],
    }


def test_an_orbital_target_parses_and_round_trips():
    g = RequestGroup.from_dict(group(orbital_target()))
    t = g.requests[0].configurations[0].target
    assert t.moving and t.orbit == Orbit(**HORIZONS["P/2009 B1"][0])
    again = RequestGroup.from_dict(g.to_dict()).requests[0].configurations[0].target
    assert again.orbit == t.orbit and again.name == "P/2009 B1"
    assert g.to_dict()["requests"][0]["configurations"][0]["target"] == orbital_target()


def test_update_place_moves_only_an_orbital_target():
    g = RequestGroup.from_dict(group(orbital_target()))
    t = g.requests[0].configurations[0].target
    t.update_place(utc("2026-11-10T07:00"), MINERVA.location)
    assert sep_arcsec(t.ra, t.dec, 132.732221618, 22.13222798) < 0.1
    star = (
        RequestGroup.from_dict(group({"type": "ICRS", "name": "s", "ra": 10.0, "dec": 20.0}))
        .requests[0]
        .configurations[0]
        .target
    )
    star.update_place(utc("2026-11-10T07:00"), MINERVA.location)
    assert (star.ra, star.dec, star.moving) == (10.0, 20.0, False)


@pytest.mark.parametrize(
    "g, error",
    [
        (group(orbital_target(), guide="ON"), "unguided"),
        (group(orbital_target(), ctype="AUTO_FOCUS"), "EXPOSE only"),
        (group(orbital_target(scheme="JPL_MAJOR_PLANET")), "major planet"),
        (group(orbital_target(perihdist=None)), r"needs \['perihdist'\]"),
        (group(orbital_target(meandist=2.0)), "not used by scheme"),
        (group(orbital_target(ra=10.0)), "unknown keys"),
        (group({"type": "SATELLITE", "name": "x", "ra": 1.0, "dec": 2.0}), "ORBITAL_ELEMENTS"),
    ],
)
def test_orbital_requests_robofast_cannot_follow_are_refused(g, error):
    with pytest.raises(ValueError, match=error):
        RequestGroup.from_dict(g)


# -- the dispatch scheduler ----------------------------------------------------------


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


async def test_the_scheduler_places_the_target_when_it_observes_it():
    """The altitude checks and the observation read the place at the
    visit's time, and the observation carries the orbit and the site."""
    when = utc("2026-11-10T11:00")
    d = DispatchScheduler(
        load_request_groups([group(orbital_target())]),
        MINERVA,
        {"t1": TelescopeLimits(min_alt=20.0, max_alt=85.0, instruments=("imager",))},
        clock=Clock(when),
    )
    obs = await d.next_observation("t1")
    assert isinstance(obs, Observation), obs
    orbit = Orbit(**HORIZONS["P/2009 B1"][0])
    # The middle of a 3 x 60 s visit, give or take the scheduler's overheads.
    ra, dec = orbit.place(when + timedelta(seconds=90), MINERVA.location)
    assert sep_arcsec(obs.ra_hours * 15, obs.dec_deg, ra, dec) < 2.0
    assert obs.follow == {
        "orbit": orbit.to_dict(),
        "site": {"latitude": 31.680407, "longitude": -110.878977, "elevation": 2316.0},
    }


# -- the sequencer -------------------------------------------------------------------


def follow_obs(count: int = 3, **kw) -> dict:
    return {
        "name": "B1",
        "exptime": 0.2,
        "count": count,
        "follow": {
            "orbit": Orbit(**HORIZONS["433 Eros"][0]).to_dict(),  # 90"/h: it moves
            "site": {"latitude": MINERVA.latitude, "longitude": MINERVA.longitude},
        },
        **kw,
    }


def test_a_plan_refuses_a_follow_it_cannot_do():
    with pytest.raises(ValueError, match="unguided light"):
        Observation.from_dict(follow_obs(guide=True))
    with pytest.raises(ValueError, match="follow"):
        Observation.from_dict({**follow_obs(), "follow": {"orbit": {"scheme": "X"}, "site": {}}})


def _events(rig, what):
    return rig.timeline.events(what, "seq")


async def test_a_mount_without_rates_cannot_follow(make_rig):
    """One way to follow: the mount's rates. A mount that cannot set them
    (the PWI and TCS servers today) refuses the observation before it slews:
    Unsupported -- reported once, not an error of that telescope (night1108:
    it was, and a healthy t1 gave up for the night) -- no frames, no rate
    set."""
    plan = StaticPlan([follow_obs()], ["t1"])
    rig = make_rig(plan=plan)
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    assert [(t, n, out) for t, n, out, _ in plan.history] == [("t1", "B1", "unsupported")]
    tel = rig.tels[0]
    assert tel.rate_log == [] and tel.slews == []
    [why] = _events(rig, "observation_unsupported")
    assert "cannot set tracking rates" in why.detail["reason"]
    assert not _events(rig, "telescope_error") and not _events(rig, "telescope_backoff")


async def test_the_sequencer_sets_the_rates_and_resets_them(make_rig):
    """A mount that can set rates: set to the target's motion (ASCOM units)
    before each exposure, back to 0 when the observation ends."""
    plan = StaticPlan([follow_obs(count=2)], ["t1"])
    rig = make_rig(plan=plan)
    tel = rig.tels[0]
    tel.CanSetRightAscensionRate = tel.CanSetDeclinationRate = True
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    assert plan.history == [("t1", "B1", "complete", 2)]
    follows = _events(rig, "follow")
    dra, ddec = follows[0].detail["rate_arcsec_s"]
    dec = follows[0].detail["dec"]
    # Eros: about 90"/h in RA, 4"/h in Dec.
    assert dra * 3600 == pytest.approx(89.5, abs=2.0)
    expect_ra = dra / math.cos(math.radians(dec)) / 15.0 * sky.SIDEREAL
    assert tel.rate_log[0] == ("ra", pytest.approx(expect_ra, rel=1e-3))
    assert tel.rate_log[1] == ("dec", pytest.approx(ddec, rel=1e-3))
    assert tel.rate_log[-2:] == [("ra", 0.0), ("dec", 0.0)]
    assert (tel.RightAscensionRate, tel.DeclinationRate) == (0.0, 0.0)
    assert _events(rig, "follow_rates_reset")
    # Pointed once; the rates follow it from there (no re-pointing).
    assert len(tel.slews) == 1


def test_tom_toolkits_non_sidereal_target_is_taken_unchanged():
    """TOM Toolkit's OCSFacility (tom_observations/facilities/ocs.py,
    _build_target_fields): the scheme's required fields and an empty
    extra_params."""
    tom = {
        "name": "433",
        "type": "ORBITAL_ELEMENTS",
        **HORIZONS["433 Eros"][0],
        "extra_params": {},
    }
    t = RequestGroup.from_dict(group(tom)).requests[0].configurations[0].target
    assert t.orbit == Orbit(**HORIZONS["433 Eros"][0])


async def test_rates_left_set_are_reset_before_a_sidereal_slew(make_rig):
    """If the reset at the end fails, the next sidereal target's slew sets
    the rates back to 0 before it moves."""
    plan = StaticPlan(
        [follow_obs(count=1), {"name": "star", "ra_hours": 3.0, "dec_deg": 20.0, "exptime": 0.1}],
        ["t1"],
    )
    rig = make_rig(plan=plan)
    tel = rig.tels[0]
    tel.CanSetRightAscensionRate = tel.CanSetDeclinationRate = True
    seq = rig.sequencer
    real_end = seq._follow_end

    async def failing_end(state, unit, t):
        pass  # the reset "fails": the rates stay set

    seq._follow_end = failing_end
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    seq._follow_end = real_end
    assert [h[1] for h in plan.history] == ["B1", "star"]
    late = [e for e in _events(rig, "follow_rates_reset") if e.detail.get("late")]
    assert len(late) == 1
    assert (tel.RightAscensionRate, tel.DeclinationRate) == (0.0, 0.0)
    assert tel.slews[-1] == (3.0, 20.0)


async def test_the_cleanup_resets_the_rates_before_it_parks(make_rig):
    """An observation cut short (here: the enclosure's lease revoked
    mid-observation) leaves no rates set: the cleanup sets them back to 0
    before the park."""
    plan = StaticPlan([follow_obs(count=50, exptime=0.3)], ["t1"])
    rig = make_rig(plan=plan)
    tel = rig.tels[0]
    tel.CanSetRightAscensionRate = tel.CanSetDeclinationRate = True
    await rig.start()
    await wait_until(lambda: _events(rig, "follow"), 10, what="following")
    rig.weather.set_safe(False)  # the supervisor revokes the lease: cleanup
    await wait_until(lambda: rig.timeline.events("cleanup_done", "seq"), 10, what="cleanup done")
    assert (tel.RightAscensionRate, tel.DeclinationRate) == (0.0, 0.0)
    assert rig.sequencer.units["t1"].follow_rates is False


# -- what a telescope cannot do (night1108) ----------------------------------------
#
# MINERVA's live dry run, 2026-11-09: a ToO for Eris (ORBITAL_ELEMENTS) went
# to t1, whose PWI2 mount cannot set tracking rates. The Unsupported was
# counted as t1's error, dispatch handed the same request back three times,
# and a healthy t1 gave up for the night. Unsupported is a property of the
# (request, telescope) pair: never retried there, still eligible elsewhere,
# refused once no telescope can do it, and the telescope goes on at once.

NOW = utc("2026-11-10T11:00")  # B1 high in the east at MINERVA
IMAGER = TelescopeLimits(min_alt=20.0, max_alt=85.0, instruments=("imager",))


def _config(cid: str, target: dict) -> dict:
    return {
        "id": cid,
        "type": "EXPOSE",
        "instrument_type": "imager",
        "target": target,
        "instrument_configs": [{"exposure_time": 0.1, "exposure_count": 1}],
        "guiding_config": {"mode": "OFF"},
    }


def _groups(stars: int = 3) -> list[dict]:
    """The moving target first (highest priority), then ``stars`` sidereal ones."""
    out = [
        {
            "id": "eris",
            "ipp_value": 10.0,
            "observation_type": "NORMAL",
            "requests": [{"id": "eris-r", "configurations": [_config("eris-c", orbital_target())]}],
        }
    ]
    for i in range(stars):
        star = {"type": "ICRS", "name": f"star{i}", "ra": 130.0 + i, "dec": 20.0}
        out.append(
            {
                "id": f"s{i}",
                "ipp_value": 1.0,
                "observation_type": "NORMAL",
                "requests": [{"id": f"s{i}-r", "configurations": [_config(f"s{i}-c", star)]}],
            }
        )
    return out


def _scheduler(telescopes: list[str], **kw) -> DispatchScheduler:
    return DispatchScheduler(
        load_request_groups(_groups()),
        MINERVA,
        {t: IMAGER for t in telescopes},
        clock=Clock(NOW),
        **kw,
    )


async def test_dispatch_never_offers_an_unsupported_request_to_that_telescope_again():
    d = _scheduler(["t1", "t2"])
    obs = await d.next_observation("t1")
    assert obs.follow is not None  # the moving target first
    d.unsupported("t1", obs, "t1: the mount cannot set tracking rates")
    d.report("t1", obs, "unsupported", 0)
    c = d.requests["eris-r"].configurations[0]
    assert c.failures == 0 and c.status.schedulable  # not a failure: t2 may do it
    again = await d.next_observation("t1")
    assert again.follow is None  # t1 goes on with a star
    [cand] = d.evaluate("t1", requests=[d.requests["eris-r"]])
    assert not cand.ok and any("cannot set tracking rates" in r for r in cand.reasons)
    assert (await d.next_observation("t2")).follow is not None


async def test_dispatch_refuses_what_no_telescope_can_do(tmp_path):
    state = tmp_path / "state.json"
    d = _scheduler(["t1"], state=state)
    obs = await d.next_observation("t1")
    d.unsupported("t1", obs, "t1: the mount cannot set tracking rates")
    d.report("t1", obs, "unsupported", 0)
    c = d.requests["eris-r"].configurations[0]
    assert str(c.status) == "FAILED" and c.failures == 0
    assert "cannot set tracking rates" in d.refused["eris-c"]
    assert (await d.next_observation("t1")).follow is None
    # Kept across a restart: not tried again.
    again = _scheduler(["t1"], state=state)
    assert str(again.requests["eris-r"].configurations[0].status) == "FAILED"
    assert "t1" in again.unsupported_on["eris-c"] and "eris-c" in again.refused


async def test_dispatch_filters_a_moving_target_by_the_mounts_rates():
    """Told the capabilities, the scheduler never offers a moving target to
    a mount without rates, and refuses it once no mount has them."""
    d = _scheduler(["t1", "t2"])
    d.set_capabilities("t1", {"tracking_rates": False})
    assert (await d.next_observation("t1")).follow is None
    assert d.requests["eris-r"].configurations[0].status.schedulable
    d.set_capabilities("t2", {"tracking_rates": False})
    assert str(d.requests["eris-r"].configurations[0].status) == "FAILED"
    assert "t1's mount cannot set tracking rates" in d.refused["eris-c"]
    # A ToO arriving now is refused at intake.
    too = {
        **_groups(0)[0],
        "id": "eris2",
        "observation_type": "RAPID_RESPONSE",
        "too": {"deadline": "2026-11-11T00:00:00+00:00"},
    }
    too["requests"] = [{"id": "eris2-r", "configurations": [_config("eris2-c", orbital_target())]}]
    await d.submit_too(too)
    assert "eris2-c" in d.refused


def _rig_with(make_rig, d: DispatchScheduler, telescopes: int, tell: bool):
    if not tell:
        d.set_capabilities = None  # the runtime Unsupported alone (the backstop)
    return make_rig(plan=d, telescopes=telescopes)


def _done(d: DispatchScheduler, prefix: str) -> list[str]:
    return [
        c.id
        for r in d.requests.values()
        for c in r.configurations
        if c.id.startswith(prefix) and str(c.status) == "COMPLETED"
    ]


@pytest.mark.parametrize("tell", [True, False])
async def test_a_moving_target_on_a_rateless_mount_does_not_ground_it(make_rig, tell):
    """One telescope, no rates: the moving request ends refused with the
    reason, the telescope never errs or gives up, and the stars are observed."""
    d = _scheduler(["t1"])
    rig = _rig_with(make_rig, d, 1, tell)
    await rig.start()
    await wait_until(lambda: len(_done(d, "s")) == 3, 30, what="the stars observed")
    assert "cannot set tracking rates" in d.refused["eris-c"]
    assert str(d.requests["eris-r"].configurations[0].status) == "FAILED"
    for what in ("telescope_error", "telescope_backoff", "giving_up"):
        assert not rig.timeline.events(what), what
    unsupported = _events(rig, "observation_unsupported")
    assert len(unsupported) == (0 if tell else 1)  # told: never even tried
    assert rig.tels[0].rate_log == []
    assert not rig.sequencer.units["t1"].gave_up


@pytest.mark.parametrize("tell", [True, False])
async def test_a_moving_target_runs_on_the_telescope_that_has_rates(make_rig, tell):
    d = _scheduler(["t1", "t2"])
    rig = _rig_with(make_rig, d, 2, tell)
    rig.tels[1].CanSetRightAscensionRate = rig.tels[1].CanSetDeclinationRate = True
    await rig.start()
    await wait_until(
        lambda: len(_done(d, "s")) == 3 and _done(d, "eris"), 30, what="everything observed"
    )
    assert [o["telescope"] for o in d.observed["eris-c"]] == ["t2"]
    assert "eris-c" not in d.refused
    for what in ("telescope_error", "giving_up"):
        assert not rig.timeline.events(what), what
    assert rig.tels[0].rate_log == []
