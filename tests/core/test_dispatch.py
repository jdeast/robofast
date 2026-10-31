"""The dispatch scheduler, with a fixed clock (no devices)."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robofast_core import sky
from robofast_core.dispatch import (
    DispatchConfig,
    DispatchScheduler,
    TelescopeLimits,
    dispatch_from_config,
)
from robofast_core.plan import Idle, Observation, plan_from_config
from robofast_core.request_model import RequestGroup, Status, load_request_groups

SITES = Path(__file__).resolve().parents[2] / "sites"
MINERVA = sky.Site(31.680407, -110.878977, 2316.0)
#: Midnight MST at Mt. Hopkins: the Sun is far down (about -77 deg).
NIGHT = datetime(2026, 11, 15, 7, 0, tzinfo=UTC)
sky.offline_iers()
LST = sky.sky_now(MINERVA, NIGHT).lst_hours


class Clock:
    def __init__(self, t: datetime = NIGHT) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def group(
    gid: str,
    ha: float = -1.0,
    dec: float = 20.0,
    *,
    priority: float = 1.0,
    instrument: str = "imager",
    exptime: float = 60.0,
    count: int = 1,
    ics: list | None = None,
    constraints: dict | None = None,
    too: dict | None = None,
    **request,
) -> dict:
    """A one-configuration group; ``ha`` is its hour angle at NIGHT."""
    config = {
        "id": f"{gid}-c",
        "instrument": instrument,
        "target": {"name": gid, "ra_hours": (LST - ha) % 24.0, "dec_deg": dec},
        "instrument_configs": ics or [{"exptime": exptime, "count": count}],
    }
    if constraints:
        config["constraints"] = constraints
    d = {
        "id": gid,
        "priority": priority,
        "requests": [{"id": f"{gid}-r", "configurations": [config], **request}],
    }
    if too is not None:
        d["too"] = too
    return d


def alt_now(g: dict, when: datetime = NIGHT) -> float:
    t = g["requests"][0]["configurations"][0]["target"]
    return float(sky.altitudes(MINERVA, [t["ra_hours"]], [t["dec_deg"]], [[when]])[0][0])


def make(groups, clock=None, limits=None, **kw) -> DispatchScheduler:
    limits = limits or {"t1": TelescopeLimits(), "t2": TelescopeLimits()}
    return DispatchScheduler(
        load_request_groups(groups), MINERVA, limits, clock=clock or Clock(), **kw
    )


def by_id(d: DispatchScheduler, telescope: str = "t1") -> dict:
    return {c.request.id: c for c in d.evaluate(telescope)}


async def run_visit(d: DispatchScheduler, telescope: str = "t1", outcome="complete"):
    obs = await d.next_observation(telescope)
    assert isinstance(obs, Observation), obs
    d.report(telescope, obs, outcome, obs.count if outcome == "complete" else 0)
    return obs


# -- scoring -----------------------------------------------------------------


async def test_score_is_priority_plus_bonuses():
    d = make([group("a", ha=-1, priority=2), group("b", ha=-1, priority=3)])
    cands = by_id(d)
    for c in cands.values():
        assert c.ok
        assert c.score == pytest.approx(sum(c.terms.values()))
        assert set(c.terms) == {"priority", "airmass", "setting"}
        assert 0 < c.terms["airmass"] <= 1 and 0 <= c.terms["setting"] <= 1
    assert cands["b-r"].score - cands["a-r"].score == pytest.approx(1.0)
    obs = await d.next_observation("t1")
    assert obs.name.startswith("b ") and obs.ra_hours == pytest.approx((LST + 1) % 24)


def test_setting_bonus_favours_a_target_about_to_be_lost():
    # Same priority: one near transit, one about to sink below 21 deg.
    d = make([group("rising", ha=-1.5), group("setting", ha=4.7)])
    assert 21 < alt_now(group("setting", ha=4.7)) < 35
    c = by_id(d)
    assert c["setting-r"].terms["setting"] > 0.5 > c["rising-r"].terms["setting"]
    # t_left: the analytic setting hour angle for 21 deg.
    h_set = sky.setting_hour_angle(MINERVA.latitude, 20.0, 21.0)
    t_left = (h_set - 4.7) * 3600 * sky.SIDEREAL
    assert c["setting-r"].terms["setting"] == pytest.approx(1 - t_left / 7200, abs=0.01)


def test_airmass_bonus_is_one_at_transit():
    c = by_id(make([group("transit", ha=0.0, dec=10.0), group("east", ha=-3.0, dec=10.0)]))
    assert c["transit-r"].terms["airmass"] == pytest.approx(1.0, abs=0.01)
    assert c["east-r"].terms["airmass"] < 0.9


def test_weights_are_tunable():
    d = make([group("a")], config=DispatchConfig(w_airmass=0.0, w_setting=0.0))
    c = by_id(d)["a-r"]
    assert c.score == pytest.approx(1.0)


def test_cadence_bonus_grows_until_overdue():
    clock = Clock()
    d = make([group("a", cadence={"min_interval": 3600})], clock=clock)
    assert by_id(d)["a-r"].terms["cadence"] == 1.0  # never observed
    d.visits["a-r"] = [NIGHT - timedelta(seconds=5400)]
    assert by_id(d)["a-r"].terms["cadence"] == pytest.approx(0.5)


def test_ties_are_broken_by_id():
    d = make([group("b"), group("a")])  # same place, same priority
    a, b = d.evaluate("t1")[::-1]
    assert a.score == b.score
    for _ in range(3):
        assert d._best("t1", NIGHT, sky.sky_now(MINERVA, NIGHT))[0].request.id == "a-r"


# -- hard constraints -----------------------------------------------------------


def _reasons(d, rid, telescope="t1"):
    c = by_id(d, telescope)[rid]
    assert not c.ok
    return " | ".join(c.reasons)


def test_altitude_limits_per_telescope():
    g = group("low", ha=4.9)
    assert 21 < alt_now(g) < 30
    d = make(
        [g, group("zenith", ha=0.0, dec=31.7)],
        limits={"t1": TelescopeLimits(min_alt=21), "t2": TelescopeLimits(min_alt=30)},
    )
    assert by_id(d, "t1")["low-r"].ok
    assert "outside [30, 83]" in _reasons(d, "low-r", "t2")
    assert "outside [21, 83]" in _reasons(d, "zenith-r")  # MAXALT: too close to the zenith


def test_altitude_checked_over_the_whole_visit():
    # Up now, but below 21 deg before a long visit ends.
    g = group("sinking", ha=5.0, exptime=3600)
    assert alt_now(g) > 21
    assert "altitude" in _reasons(make([g]), "sinking-r")


def test_constraints_only_tighten():
    d = make(
        [
            group("strict", ha=-3, constraints={"min_alt": 60}),
            group("loose", ha=5.6, constraints={"min_alt": 5}),
            group("airmass", ha=-3, constraints={"max_airmass": 1.1}),
        ]
    )
    assert 5 < alt_now(group("loose", ha=5.6)) < 21
    assert "outside [60, 83]" in _reasons(d, "strict-r")
    assert "outside [21, 83]" in _reasons(d, "loose-r")  # cannot loosen the telescope's
    assert "altitude" in _reasons(d, "airmass-r")


def test_hour_angle_limits():
    d = make(
        [group("west", ha=2.5), group("east", ha=-2.5)],
        limits={"t1": TelescopeLimits(min_ha=-2.0, max_ha=2.0), "t2": TelescopeLimits()},
    )
    assert "hour angle" in _reasons(d, "west-r") and "hour angle" in _reasons(d, "east-r")
    assert by_id(d, "t2")["west-r"].ok


def test_moon_separation():
    moon = sky.sky_now(MINERVA, NIGHT).moon  # topocentric (GCRS) place
    g = group("near-moon")
    g["requests"][0]["configurations"][0]["target"].update(
        ra_hours=float(moon.ra.hour), dec_deg=float(moon.dec.deg) + 3.0
    )
    assert "from the Moon (limit 10)" in _reasons(make([g]), "near-moon-r")
    # A configuration may ask for more separation, never less.
    far = group("far", constraints={"min_moon_sep": 179})
    assert "from the Moon (limit 179)" in _reasons(make([far]), "far-r")


async def test_sun_limit_and_the_night_ending():
    # Late afternoon (Sun setting) at Mt. Hopkins: idle, not done.
    clock = Clock(datetime(2026, 11, 15, 0, 0, tzinfo=UTC))
    d = make([group("a")], clock=clock)
    assert "Sun at" in _reasons(d, "a-r")
    assert isinstance(await d.next_observation("t1"), Idle)
    # Dawn (Sun rising past -12): the night is over.
    clock.t = datetime(2026, 11, 15, 13, 30, tzinfo=UTC)
    assert sky.sky_now(MINERVA, clock.t).sun_alt > -12
    assert await d.next_observation("t1") is None


def test_time_windows():
    start = NIGHT - timedelta(hours=1)
    d = make(
        [
            group("in", windows=[{"start": start.isoformat(), "end": "2026-11-16T00:00:00Z"}]),
            group(
                "short",
                exptime=600,
                windows=[
                    {"start": start.isoformat(), "end": (NIGHT + timedelta(minutes=5)).isoformat()}
                ],
            ),
            group(
                "later", windows=[{"start": "2026-11-16T00:00:00Z", "end": "2026-11-17T00:00:00Z"}]
            ),
        ]
    )
    assert by_id(d)["in-r"].ok
    assert "outside its windows" in _reasons(d, "short-r")  # the visit would not fit
    assert "outside its windows" in _reasons(d, "later-r")


def test_window_end_counts_as_setting():
    end = NIGHT + timedelta(minutes=30)
    d = make(
        [group("a", ha=-1, windows=[{"start": "2026-11-14T00:00:00Z", "end": end.isoformat()}])]
    )
    assert by_id(d)["a-r"].terms["setting"] == pytest.approx(1 - 1800 / 7200)


def test_instrument_and_telescope_compatibility():
    d = make(
        [group("spec", instrument="spectrograph"), group("t2only", telescopes=["t2"])],
        limits={
            "t1": TelescopeLimits(instruments=["imager"]),
            "t2": TelescopeLimits(instruments=["imager", "spectrograph"]),
        },
    )
    assert "t1 has no spectrograph" in _reasons(d, "spec-r")
    assert "not for t1" in _reasons(d, "t2only-r")
    assert by_id(d, "t2")["spec-r"].ok and by_id(d, "t2")["t2only-r"].ok
    with pytest.raises(ValueError, match="unknown telescopes"):
        make([group("x", telescopes=["t9"])])


async def test_cadence_and_nightly_max():
    clock = Clock()
    d = make([group("a", cadence={"min_interval": 3600, "max_per_night": 2})], clock=clock)
    await run_visit(d)
    assert d.visits_tonight("a-r") == 1
    assert d.status("a-c") is Status.PENDING  # standing: back to pending
    clock.advance(1800)
    assert "too soon" in _reasons(d, "a-r")
    assert isinstance(await d.next_observation("t1"), Idle)
    clock.advance(1800)
    await run_visit(d)
    clock.advance(3600)
    assert "2 visits tonight" in _reasons(d, "a-r")
    # The next night (after local noon) counts afresh.
    clock.t = NIGHT + timedelta(days=1)
    assert by_id(d)["a-r"].ok


async def test_one_request_on_one_telescope_at_a_time():
    d = make([group("a", count=2, ics=[{"exptime": 1}, {"exptime": 2}])])
    obs = await d.next_observation("t1")
    assert obs.exptime == 1
    assert "in progress on t1" in _reasons(d, "a-r", "t2")
    assert isinstance(await d.next_observation("t2"), Idle)


# -- errors never mean observable ---------------------------------------------------


def test_an_exception_excludes_only_that_target(monkeypatch, caplog):
    d = make([group("good"), group("bad"), group("also-good")])
    real = DispatchScheduler._sky_checks

    def boom(self, limits, cand, *args):
        if cand.request.id == "bad-r":
            raise RuntimeError("ephemeris exploded")
        return real(self, limits, cand, *args)

    monkeypatch.setattr(DispatchScheduler, "_sky_checks", boom)
    with caplog.at_level(logging.ERROR, logger="robofast_core.dispatch"):
        c = by_id(d)
    assert not c["bad-r"].ok and "ephemeris exploded" in c["bad-r"].reasons[0]
    assert c["good-r"].ok and c["also-good-r"].ok
    assert d.errors[-1][1] == "bad-r"
    assert "bad-r" in caplog.text and "excluded" in caplog.text


def test_a_failed_position_is_not_observable(monkeypatch):
    """A target whose position cannot be computed is excluded; the vectorized
    computation falls back to one at a time, so the others are unaffected."""
    d = make([group("good"), group("bad")])
    d.requests["bad-r"].configurations[0].target.ra_hours = "garbage"  # past validation
    c = by_id(d)
    assert c["good-r"].ok
    assert not c["bad-r"].ok and c["bad-r"].reasons


def test_a_non_finite_score_is_an_error(monkeypatch):
    d = make([group("a")])
    monkeypatch.setattr(sky, "airmass", lambda alt: math.nan)
    c = by_id(d)["a-r"]
    assert not c.ok and "NaN" in c.reasons[0]


# -- visits and reports ---------------------------------------------------------------


async def test_a_visit_runs_every_instrument_config_in_order():
    d = make([group("a", ics=[{"exptime": 1, "count": 2}, {"exptime": 2, "filter": "gp"}])])
    first = await run_visit(d)
    assert (first.exptime, first.count) == (1, 2)
    assert d.status("a-c") is Status.PENDING
    second = await run_visit(d)
    assert second.exptime == 2 and second.name.endswith("#1]")
    assert d.status("a-c") is Status.COMPLETED  # one-shot
    assert isinstance(await d.next_observation("t1"), Idle)


async def test_interrupted_is_requeued_and_failed_counts_attempts():
    d = make([group("a", max_attempts=2)])
    await run_visit(d, outcome="interrupted")
    assert d.status("a-c") is Status.ATTEMPTED
    await run_visit(d, outcome="failed")
    assert d.status("a-c") is Status.ATTEMPTED
    await run_visit(d, outcome="failed")
    assert d.status("a-c") is Status.FAILED
    assert isinstance(await d.next_observation("t1"), Idle)


async def test_a_visit_is_abandoned_if_it_becomes_unobservable():
    clock = Clock()
    d = make(
        [group("a", ha=4.6, ics=[{"exptime": 60}, {"exptime": 60}]), group("b", ha=-2)],
        clock=clock,
        config=DispatchConfig(w_setting=10),
    )
    obs = await run_visit(d)
    assert obs.name.startswith("a ")
    clock.advance(3 * 3600)  # "a" has set meanwhile
    nxt = await d.next_observation("t1")
    assert nxt.name.startswith("b ")
    assert d.status("a-c") is Status.ATTEMPTED


async def test_unexpected_reports_are_ignored():
    d = make([group("a")])
    d.report("t1", Observation("stray"), "complete", 1)
    assert d.status("a-c") is Status.PENDING


async def test_expiry_of_windows():
    clock = Clock()
    end = NIGHT + timedelta(hours=1)
    d = make(
        [group("a", windows=[{"start": "2026-11-14T00:00:00Z", "end": end.isoformat()}])],
        clock=clock,
    )
    clock.advance(7200)
    await d.next_observation("t1")
    assert d.status("a-c") is Status.EXPIRED


# -- persistence -------------------------------------------------------------------------


async def test_history_and_status_survive_a_restart(tmp_path):
    state = tmp_path / "state.json"
    groups = [group("standing", cadence={"min_interval": 3600}), group("once", priority=0.1)]
    clock = Clock()
    d = make(groups, clock=clock, state=state)
    await run_visit(d)  # standing (higher priority)
    clock.advance(60)
    await run_visit(d)  # once
    assert d.status("once-c") is Status.COMPLETED
    again = make(groups, clock=clock, state=state)
    assert again.status("once-c") is Status.COMPLETED
    assert again.visits["standing-r"] == [NIGHT]
    assert "too soon" in _reasons(again, "standing-r")
    assert isinstance(await again.next_observation("t1"), Idle)


async def test_a_corrupt_state_file_is_moved_aside(tmp_path, caplog):
    state = tmp_path / "state.json"
    state.write_text("{not json")
    with caplog.at_level(logging.ERROR):
        d = make([group("a")], state=state)
    assert not state.exists()
    assert list(tmp_path.glob("state.json.corrupt-*"))
    assert "unreadable" in caplog.text
    await run_visit(d)
    assert json.loads(state.read_text())["configurations"]["a-c"]["status"] == "COMPLETED"


# -- targets of opportunity ----------------------------------------------------------------


def too(policy="now", deadline=NIGHT + timedelta(hours=2), cls=1) -> dict:
    return {"deadline": deadline.isoformat(), "policy": policy, "priority_class": cls}


class Preempts:
    """Records preempt calls; ``accept`` says what to answer."""

    def __init__(self, accept: bool = True) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.accept = accept

    def __call__(self, telescope: str, policy: str, reason: str) -> bool:
        self.calls.append((telescope, policy, reason))
        return self.accept


async def _busy(d, *telescopes):
    for t in telescopes:
        assert isinstance(await d.next_observation(t), Observation)


async def test_too_ranks_above_everything():
    d = make([group("normal", priority=100)])
    await d.submit_too(group("burst", priority=0.1, too=too("next")))
    assert (await d.next_observation("t1")).name.startswith("burst ")


@pytest.mark.parametrize("policy", ["now", "now-after-readout"])
async def test_too_preempts_the_lowest_ranked_busy_telescope(policy):
    d = make([group("hi", priority=5), group("lo", priority=1)])
    p = Preempts()
    d.set_preempt_callback(p)
    await _busy(d, "t1", "t2")  # t1 takes "hi", t2 "lo"
    await d.submit_too(group("burst", too=too(policy)))
    assert p.calls == [("t2", policy, "ToO burst-r")]
    # Reserved for t2: t1 finishing first does not take it.
    d.report("t1", d._current["t1"].obs, "complete", 1)
    assert isinstance(await d.next_observation("t1"), Idle)
    d.report("t2", d._current["t2"].obs, "interrupted", 0)
    assert (await d.next_observation("t2")).name.startswith("burst ")
    assert d.status("lo-c") is Status.ATTEMPTED


async def test_too_with_policy_next_interrupts_nothing():
    d = make([group("a"), group("b")])
    p = Preempts()
    d.set_preempt_callback(p)
    await _busy(d, "t1", "t2")
    await d.submit_too(group("burst", too=too("next")))
    assert p.calls == []
    d.report("t2", d._current["t2"].obs, "complete", 1)
    assert (await d.next_observation("t2")).name.startswith("burst ")


async def test_too_wakes_an_idle_telescope_instead_of_preempting():
    d = make([group("a", telescopes=["t1"])])
    p = Preempts()
    d.set_preempt_callback(p)
    await _busy(d, "t1")
    assert isinstance(await d.next_observation("t2"), Idle)
    await d.submit_too(group("burst", too=too("now")))
    assert p.calls == [("t2", "next", "ToO burst-r")]


async def test_too_only_for_eligible_telescopes():
    d = make([group("a"), group("b")])
    p = Preempts()
    d.set_preempt_callback(p)
    await _busy(d, "t1", "t2")
    await d.submit_too(group("burst", too=too("now"), telescopes=["t1"]))
    assert [c[0] for c in p.calls] == ["t1"]


async def test_unobservable_too_waits_then_expires():
    clock = Clock()
    d = make([group("a")], clock=clock)
    p = Preempts()
    d.set_preempt_callback(p)
    await _busy(d, "t1")
    # Below the horizon until well after its deadline.
    await d.submit_too(group("burst", ha=9.0, too=too("now", deadline=NIGHT + timedelta(hours=1))))
    assert p.calls == []
    assert d.status("burst-c") is Status.PENDING
    clock.advance(1800)
    d.check_toos()
    assert p.calls == [] and d.status("burst-c") is Status.PENDING
    clock.advance(1801)
    d.check_toos()
    assert d.status("burst-c") is Status.EXPIRED


async def test_too_that_rises_is_taken_when_it_becomes_observable():
    clock = Clock()
    d = make([group("a"), group("b")], clock=clock)
    p = Preempts()
    d.set_preempt_callback(p)
    await _busy(d, "t1", "t2")
    # 5.3 h east of the meridian, dec 20: below 21 deg now, up within an hour.
    g = group("rising", ha=-5.3, too=too("now", deadline=NIGHT + timedelta(hours=3)))
    assert alt_now(g) < 21 < alt_now(g, NIGHT + timedelta(hours=1))
    await d.submit_too(g)
    assert p.calls == []
    clock.advance(3600)
    d.check_toos()
    assert len(p.calls) == 1


async def test_refused_preemption_is_retried_later():
    """The sequencer refuses while the enclosure is closed; the ToO is not
    reserved, and the next check (after reopening) tries again."""
    d = make([group("a"), group("b")])
    p = Preempts(accept=False)
    d.set_preempt_callback(p)
    await _busy(d, "t1", "t2")
    await d.submit_too(group("burst", too=too("now")))
    assert len(p.calls) == 1 and d._too["burst-r"].reserved_for is None
    p.accept = True
    d.check_toos()
    assert len(p.calls) == 2 and d._too["burst-r"].reserved_for is not None


async def test_repeated_toos():
    """A higher-class ToO preempts a telescope doing a lower-class one; an
    equal-class ToO waits its turn."""
    d = make([group("a")], limits={"t1": TelescopeLimits()})
    p = Preempts()
    d.set_preempt_callback(p)
    await _busy(d, "t1")
    await d.submit_too(group("first", too=too("now", cls=1)))
    assert len(p.calls) == 1
    d.report("t1", d._current["t1"].obs, "interrupted", 0)
    assert (await d.next_observation("t1")).name.startswith("first ")
    await d.submit_too(group("same", too=too("now", cls=1)))
    assert len(p.calls) == 1  # equal class: no preemption
    await d.submit_too(group("urgent", too=too("now", cls=2)))
    assert p.calls[-1] == ("t1", "now", "ToO urgent-r")
    d.report("t1", d._current["t1"].obs, "interrupted", 0)
    order = []
    for _ in range(4):
        obs = await d.next_observation("t1")
        if not isinstance(obs, Observation):
            break
        order.append(obs.name.split()[0])
        d.report("t1", obs, "complete", obs.count)
    # urgent, then the two class-1 ToOs (earliest deadline, then id), then normal work.
    assert order == ["urgent", "first", "same", "a"]
    assert d.status("first-c") is Status.COMPLETED


async def test_too_outranks_a_visit_in_progress():
    d = make([group("a", ics=[{"exptime": 1}, {"exptime": 1}])], limits={"t1": TelescopeLimits()})
    await run_visit(d)  # first half of "a"
    await d.submit_too(group("burst", too=too("next")))
    assert (await d.next_observation("t1")).name.startswith("burst ")
    assert d.status("a-c") is Status.ATTEMPTED


async def test_submit_too_validation():
    d = make([group("a")])
    with pytest.raises(ValueError, match="no 'too' entry"):
        await d.submit_too(group("plain"))
    with pytest.raises(ValueError, match="already in use"):
        await d.submit_too(group("a", too=too()))


async def test_toos_survive_a_restart(tmp_path):
    state = tmp_path / "state.json"
    d = make([group("a")], state=state)
    await d.submit_too(group("burst", too=too("next")))
    again = make([group("a")], state=state)
    assert (await again.next_observation("t1")).name.startswith("burst ")


async def test_dropbox(tmp_path):
    box = tmp_path / "too"
    box.mkdir()
    d = make([group("a")], too_dropbox=box)
    (box / "1-good.json").write_text(json.dumps(group("burst", too=too("next"))))
    (box / "2-normal.json").write_text(json.dumps(group("plain")))  # not a ToO: also fine
    bad = group("nope")
    del bad["requests"]
    (box / "3-bad.json").write_text(json.dumps(bad))
    (box / "4-cancel.json").write_text(json.dumps({"cancel": "plain"}))
    (box / "5-cancel.json").write_text(json.dumps({"cancel": "unknown"}))
    (box / "partial.json.tmp").write_text("{")
    assert await d.poll_dropbox() == ["burst", "plain", "plain"]
    assert (box / "accepted" / "1-good.json").exists()
    assert (box / "rejected" / "3-bad.json").exists()
    assert "missing ['requests']" in (box / "rejected" / "3-bad.json.error").read_text()
    assert "unknown request group" in (box / "rejected" / "5-cancel.json.error").read_text()
    assert (box / "partial.json.tmp").exists()  # not a .json: left alone
    assert d.status("plain-c") is Status.CANCELED
    assert "burst-r" in d.requests


# -- configuration ------------------------------------------------------------------------------


def _example(tmp_path) -> dict:
    cfg = json.loads((SITES / "minerva" / "dispatch.example.json").read_text())
    for name in ("requests.example.json",):
        (tmp_path / name).write_text((SITES / "minerva" / name).read_text())
    return cfg


async def test_example_plan_entry(tmp_path):
    cfg = _example(tmp_path)
    plan = plan_from_config(cfg, ["t1", "t2"], base=tmp_path)
    assert isinstance(plan, DispatchScheduler)
    assert plan.limits["t1"].instruments == ["spectrograph", "imager"]
    assert plan.state_path == tmp_path / "dispatch-state.json"
    assert (tmp_path / "too-dropbox").is_dir()
    assert len(plan.groups) >= 8
    plan.clock = Clock()
    obs = await plan.next_observation("t1")
    assert isinstance(obs, Observation) and obs.light


def test_config_errors(tmp_path):
    cfg = _example(tmp_path)
    with pytest.raises(ValueError, match="unknown telescopes"):
        plan_from_config({**cfg, "telescopes": {"t9": {}}}, ["t1", "t2"], base=tmp_path)
    with pytest.raises(ValueError, match="unknown keys"):
        plan_from_config({**cfg, "w_bogus": 1}, ["t1", "t2"], base=tmp_path)
    with pytest.raises(ValueError, match="'site'"):
        plan_from_config({"type": "dispatch"}, ["t1"])
    with pytest.raises(ValueError, match="empty altitude"):
        dispatch_from_config(
            {
                "site": {"latitude": 0, "longitude": 0},
                "telescopes": {"t1": {"min_alt": 50, "max_alt": 40}},
            },
            ["t1"],
        )


def test_legacy_csv_in_the_plan_entry(tmp_path):
    (tmp_path / "targets.csv").write_text(
        "name,ra,dec,exptime,priority,active\nHD 1,1.5,20,300,5,1\n"
    )
    d = dispatch_from_config(
        {"site": {"latitude": 31.68, "longitude": -110.88}, "legacy_csv": "targets.csv"},
        ["t1"],
        base=tmp_path,
    )
    assert list(d.requests) == ["HD_1/r"]


def test_requests_inline():
    d = dispatch_from_config(
        {"site": {"latitude": 31.68, "longitude": -110.88}, "requests": [group("a")]}, ["t1"]
    )
    assert isinstance(d.groups["a"], RequestGroup)


async def test_sequencer_service_builds_a_dispatch_plan(tmp_path):
    """The service wires the plan's preempt callback to the sequencer."""
    from robofast_core.config import load_config
    from robofast_core.sequencer_service import build, load_plan
    from robofast_core.timeline import Timeline

    _example(tmp_path)
    (tmp_path / "dispatch.json").write_text(
        (SITES / "minerva" / "dispatch.example.json").read_text()
    )
    config = load_config(SITES / "minerva" / "aqawan1.json")
    plan = load_plan(tmp_path / "dispatch.json", ["t1", "t2"])
    seq, remote = build(config, Timeline(), plan=plan)
    try:
        assert seq.plan is plan and plan._preempt == seq.preempt
        assert plan.dropbox == tmp_path / "too-dropbox"
    finally:
        await remote.aclose()


async def test_serve_takes_dropped_toos(tmp_path):
    import asyncio

    box = tmp_path / "too"
    box.mkdir()
    d = make([group("a")], too_dropbox=box, config=DispatchConfig(too_poll=0.02))
    task = asyncio.create_task(d.serve())
    try:
        (box / "x.json").write_text(json.dumps(group("burst", too=too("next"))))
        for _ in range(100):
            if "burst-r" in d.requests:
                break
            await asyncio.sleep(0.02)
        assert "burst-r" in d.requests
    finally:
        task.cancel()


async def test_two_toos_preempt_two_telescopes():
    d = make([group("hi", priority=5), group("lo", priority=1)])
    p = Preempts()
    d.set_preempt_callback(p)
    await _busy(d, "t1", "t2")
    await d.submit_too(group("one", too=too("now")))
    await d.submit_too(group("two", too=too("now")))
    assert [c[0] for c in p.calls] == ["t2", "t1"]  # lowest-ranked first, then the other
