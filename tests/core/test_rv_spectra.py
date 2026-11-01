"""Spectra in the sequencer (fakes): telescopes feeding one fiber spectrum,
dropping out, cleanup, spectrograph calibrations; and the spectrum metadata
(barycentric correction, sidecars)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from robofast_core.guider.base import GuiderError
from robofast_core.plan import CalibrationPlan, Observation, StaticPlan
from robofast_core.sky import Site
from robofast_core.spectra import SpectrumRecorder, barycentric, propagate
from robofast_core.testing.checks import wait_until

MTHOPKINS = Site(31.680407, -110.878977, 2316.0)
TARGET = {"name": "HD 10700", "ra_hours": 1.7345, "dec_deg": -15.9375, "pm_ra": -1729.7}


class FakeSpectrograph:
    """The spectrograph service's API in memory. Each frame "exposes" for
    its exptime (``scale`` x), then reads out for ``readout``; every call is
    logged to the rig's timeline (source ``spectrograph``)."""

    def __init__(self, timeline, *, scale: float = 1.0, readout: float = 0.05) -> None:
        self.timeline = timeline
        self.scale, self.readout = scale, readout
        self.jobs: dict[int, dict] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.calls: list[tuple[str, dict]] = []
        self.current: int | None = None
        self.fail_next: str | None = None

    def _log(self, what, **detail):
        self.calls.append((what, detail))
        self.timeline.event("spectrograph", what, **detail)

    def _start(self, kind: str, frames: list[dict]) -> dict:
        if self.current is not None and self.jobs[self.current]["state"] == "running":
            raise RuntimeError("409 busy")
        job_id = len(self.jobs) + 1
        job = {"id": job_id, "kind": kind, "state": "running", "frames": frames, "results": []}
        self.jobs[job_id] = job
        self.current = job_id
        self.tasks[job_id] = asyncio.create_task(self._run(job))
        return dict(job)

    async def _run(self, job):
        try:
            for f in job["frames"]:
                start = datetime.now(UTC)
                await asyncio.sleep(f.get("exptime", 0.0) * self.scale + self.readout)
                n = sum(len(j["results"]) for j in self.jobs.values()) + 1
                night = f.get("night") or "n20261031"
                job["results"].append(
                    {
                        "file": f"{night}.{f.get('object', f['kind'])}.{n:04d}.fits",
                        "kind": f["kind"],
                        "object": f.get("object", f["kind"]),
                        "telescopes": f.get("telescopes", []),
                        "dark": f["kind"] in ("bias", "dark"),
                        "exptime": f.get("exptime", 0.0),
                        "exptime_requested": f.get("exptime", 0.0),
                        "ended_by": "time",
                        "start_utc": start.isoformat(),
                        "expmeter_counts": 1000.0,
                        "target_counts": f.get("target_counts"),
                        "flux_midpoint_s": f.get("exptime", 0.0) / 2,
                        "flux_midpoint_utc": start.isoformat(),
                        "flux_midpoint_jd": 2461000.0,
                        "iodine": {"requested": f.get("iodine", "in"), "mm": 11.0, "name": "in"},
                        "warnings": [],
                    }
                )
            if self.fail_next:
                job["state"], job["error"] = "failed", self.fail_next
            else:
                job["state"] = "done"
        except asyncio.CancelledError:
            job["state"] = "aborted"
            raise

    async def expose(self, **frame):
        self._log("expose", **frame)
        return self._start("expose", [frame])

    async def calibrate(self, frames, night=None):
        self._log("calibrate", frames=frames)
        out = []
        for f in frames:
            out += [{**f, "object": f["kind"]} for _ in range(int(f.get("n", 1)))]
        return self._start("calibrate", out)

    async def job(self, job_id, wait=0.0):
        task = self.tasks[job_id]
        if wait > 0 and not task.done():
            await asyncio.wait({task}, timeout=wait)
        return dict(self.jobs[job_id])

    async def abort(self, reason, *, timeout=None):
        self._log("abort", reason=reason)
        job = self.jobs.get(self.current) if self.current else None
        if job is not None and job["state"] == "running":
            self.tasks[job["id"]].cancel()
            await asyncio.wait({self.tasks[job["id"]]})
        return True

    async def status(self, refresh=False):
        job = self.jobs.get(self.current) if self.current else None
        return {"job": dict(job) if job else None}

    def science(self):
        return [d for w, d in self.calls if w == "expose"]


class FakeGuider:
    def __init__(self, timeline, scope):
        self.timeline, self.scope = timeline, scope
        self.guiding = False
        self.fail = None
        self.fail_start = None
        self.calls = []
        self.start_delay = 0.0

    def _log(self, what, **detail):
        self.calls.append(what)
        self.timeline.event(f"guider.{self.scope}", what, **detail)

    async def locate(self):
        self._log("locate")
        return (949.0, 617.0)

    async def start(self, **params):
        self._log("start", **params)
        await asyncio.sleep(self.start_delay)
        if self.fail_start:
            raise GuiderError(self.fail_start)
        self.guiding = True

    async def check(self):
        if self.fail:
            raise GuiderError(f"guider {self.scope}: failed: {self.fail}")

    async def status(self):
        return {
            "state": "guiding" if self.guiding else "idle",
            "target": [949.0, 617.0],
            "reacquisitions": 0,
            "telemetry": {"residual_rms": 0.12, "flux": 5.0e4, "frames": 40},
        }

    async def stop(self, reason="requested"):
        self._log("stop", reason=reason)
        self.guiding = False
        return True


SEQ = dict(
    spectrum_ready_timeout=3.0,
    spectrum_check_interval=0.05,
    spectrum_poll=0.05,
    spectrum_margin=3.0,
    spectrograph_abort_wait=1.0,
)


def spectrum_target(name="rv1", count=1, exptime=0.6, **spec):
    return {
        "name": name,
        "kind": "spectrum",
        "exptime": exptime,
        "count": count,
        "ra_hours": TARGET["ra_hours"],
        "dec_deg": TARGET["dec_deg"],
        "spectrum": {"feed": ["t1", "t2"], "target": TARGET, **spec},
    }


def rv_rig(make_rig, tmp_path, plan, *, seq=None, **kw):
    rig = make_rig(telescopes=2, plan=plan, seq={**SEQ, **(seq or {})}, **kw)
    s = rig.sequencer
    rig.spec = s.spectrograph = FakeSpectrograph(rig.timeline)
    s.spectra = SpectrumRecorder(tmp_path, "minerva", MTHOPKINS)
    rig.guiders = []
    for name in ("t1", "t2"):
        g = FakeGuider(rig.timeline, name)
        s.units[name].guider = g
        rig.guiders.append(g)
    return rig


async def run_to_end(rig, timeout=20.0):
    await rig.start()
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), timeout)


def events(rig, what):
    return rig.timeline.events(what, "seq")


# -- the barycentric correction ---------------------------------------------------------


def test_barycentric_correction_matches_the_astropy_documented_value():
    # astropy's documented example (SkyCoord.radial_velocity_correction):
    # Keck, 2016-06-04 00:00 UTC, RA 4.88375 deg, Dec 35.0436389 deg ->
    # 20.077135 km/s barycentric (docs: agrees with barycorr, Wright &
    # Eastman 2014, to a few m/s).
    keck = Site(19.8283, -155.4783, 4160.0)
    target = {"ra_hours": 4.88375 / 15.0, "dec_deg": 35.0436389}
    out = barycentric(target, keck, datetime(2016, 6, 4, tzinfo=UTC))
    assert out["barycentric_correction"] == pytest.approx(20077.135, abs=0.5)
    # BJD_TDB = JD_UTC + (TDB - UTC: 32.184 s + 36 leap seconds, +-2 ms) +
    # the light travel time (|t| < 8.4 min).
    jd_utc = 2457543.5
    assert out["jd_utc"] == pytest.approx(jd_utc, abs=1e-9)
    assert abs(out["light_travel_time_s"]) < 500.0
    tdb_minus_utc = (out["bjd_tdb"] - jd_utc) * 86400.0 - out["light_travel_time_s"]
    assert tdb_minus_utc == pytest.approx(68.184, abs=0.003)
    assert out["bjd_tdb"] == pytest.approx(2457543.4978169, abs=2e-7)


def test_barycentric_light_travel_time_is_the_romer_delay():
    """The light travel time against an independent calculation: the
    Earth's barycentric position projected on the target direction."""
    import astropy.units as u
    from astropy.constants import c
    from astropy.coordinates import SkyCoord, get_body_barycentric
    from astropy.time import Time

    when = datetime(2026, 11, 15, 6, 0, tzinfo=UTC)
    out = barycentric(TARGET | {"pm_ra": None}, MTHOPKINS, when)
    t = Time(when)
    n = SkyCoord(ra=TARGET["ra_hours"] * 15 * u.deg, dec=TARGET["dec_deg"] * u.deg).cartesian
    earth = get_body_barycentric("earth", t)
    romer = (earth.dot(n) / c).to_value(u.s)
    # The observatory is ~21 ms of light from the geocentre.
    assert out["light_travel_time_s"] == pytest.approx(romer, abs=0.025)


def test_proper_motion_moves_the_target():
    barnard = {"ra_hours": 17.963472, "dec_deg": 4.693391, "pm_ra": -798.58, "pm_dec": 10328.12}
    ra, dec = propagate(barnard, datetime(2026, 1, 1, tzinfo=UTC))
    years = 26.0
    assert dec - barnard["dec_deg"] == pytest.approx(10328.12 * years / 3.6e6, rel=1e-3)
    assert (ra - barnard["ra_hours"]) * 15 < 0
    assert propagate({"ra_hours": 1.0, "dec_deg": 2.0}, datetime.now(UTC)) == (1.0, 2.0)


def test_sidecars_are_never_overwritten(tmp_path):
    rec = SpectrumRecorder(tmp_path, "minerva", MTHOPKINS)
    r = {
        "file": "n20261115.HD10700.0001.fits",
        "kind": "thar",
        "start_utc": "2026-11-16T03:00:00+00:00",
    }
    a, b = rec.write(r), rec.write(r)
    assert str(a) == "minerva/kiwispec/n20261115/n20261115.HD10700.0001.fits.json"
    assert str(b).endswith(".0001.fits.1.json")
    index = tmp_path / "minerva/kiwispec/n20261115/n20261115.kiwispec.spectra.jsonl"
    assert len(index.read_text().splitlines()) == 2


# -- spectra in the sequencer -------------------------------------------------------------


async def test_two_telescopes_feed_one_spectrum(make_rig, tmp_path):
    plan = StaticPlan([spectrum_target(iodine=False, target_counts=5e5)], ["t1", "t2"])
    rig = rv_rig(make_rig, tmp_path, plan)
    await run_to_end(rig)
    [frame] = rig.spec.science()
    assert sorted(frame["telescopes"]) == ["t1", "t2"]
    assert frame["iodine"] == "out" and frame["target_counts"] == 5e5
    assert frame["object"] == "rv1"
    # Never without guiding: both were guiding before it started.
    start = rig.timeline.events("expose", "spectrograph")[0].t
    for name in ("t1", "t2"):
        guiding = [e for e in events(rig, "guiding") if e.detail["telescope"] == name]
        assert guiding and guiding[0].t < start
        assert rig.timeline.events("locate", f"guider.{name}")[0].t < guiding[0].t
    # One leader reported the spectrum; the member reported its part.
    outcomes = sorted(plan.history)
    assert [o[2] for o in outcomes] == ["complete", "complete"]
    assert sorted(o[3] for o in outcomes) == [0, 1]
    # The sidecar.
    [rec] = rig.sequencer.spectrum_records
    assert rec["telescopes"] == ["t1", "t2"] and rec["telescopes_end"] == ["t1", "t2"]
    assert set(rec["guide"]) == {"t1", "t2"}
    assert rec["guide"]["t1"]["telemetry"]["residual_rms"] == 0.12
    assert rec["barycentric"]["midpoint_source"] == "expmeter"
    assert abs(rec["barycentric"]["barycentric_correction"]) < 31000
    assert rec["target"]["pm_ra"] == -1729.7
    files = list(tmp_path.glob("minerva/kiwispec/n*/*.fits.json"))
    assert len(files) == 1
    on_disk = json.loads(files[0].read_text())
    assert on_disk["barycentric"]["bjd_tdb"] == rec["barycentric"]["bjd_tdb"]
    leader = next(o for o in plan.history if o[3] == 1)[0]
    assert [f for t, _, f in plan.files if t == leader] == [[str(files[0].relative_to(tmp_path))]]


async def test_a_member_failing_mid_exposure_does_not_kill_the_spectrum(make_rig, tmp_path):
    plan = StaticPlan([spectrum_target(exptime=1.5)], ["t1", "t2"])
    rig = rv_rig(make_rig, tmp_path, plan)
    await rig.start()
    await wait_until(lambda: rig.spec.science(), 10, what="spectrum started")
    leader = events(rig, "spectrum_session")[0].detail["telescope"]
    member = "t2" if leader == "t1" else "t1"
    rig.guiders[int(member[1]) - 1].fail = "star lost 4 times"
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    [rec] = rig.sequencer.spectrum_records
    assert rec["telescopes"] == ["t1", "t2"] and rec["telescopes_end"] == [leader]
    assert "star lost" in rec["dropped"][member]
    assert not [w for w, _ in rig.spec.calls if w == "abort" and _.get("reason") != "cleanup #1"]
    errors = [e.detail for e in events(rig, "telescope_error")]
    assert errors and errors[0]["telescope"] == member
    assert (leader, "rv1", "complete", 1) in plan.history


async def test_the_leader_failing_mid_exposure_keeps_the_spectrum(make_rig, tmp_path):
    plan = StaticPlan([spectrum_target(exptime=1.5)], ["t1", "t2"])
    rig = rv_rig(make_rig, tmp_path, plan, seq=dict(max_consecutive_errors=1))
    await rig.start()
    await wait_until(lambda: rig.spec.science(), 10, what="spectrum started")
    leader = events(rig, "spectrum_session")[0].detail["telescope"]
    member = "t2" if leader == "t1" else "t1"
    i = int(leader[1]) - 1
    rig.guiders[i].fail = "star lost"
    # The dropped leader is made safe at once, while the spectrum goes on.
    await wait_until(lambda: rig.parked(i), 5, what="leader parked")
    assert rig.spec.jobs[1]["state"] == "running"
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    [rec] = rig.sequencer.spectrum_records
    assert rec["telescopes_end"] == [member] and leader in rec["dropped"]
    # Reported complete, then the leader's error handled (it gave up).
    assert (leader, "rv1", "complete", 1) in plan.history
    assert rig.sequencer.units[leader].gave_up


async def test_every_telescope_failing_aborts_the_spectrum(make_rig, tmp_path):
    plan = StaticPlan([spectrum_target(exptime=3.0)], ["t1", "t2"])
    rig = rv_rig(make_rig, tmp_path, plan, seq=dict(max_consecutive_errors=1))
    await rig.start()
    await wait_until(lambda: rig.spec.science(), 10, what="spectrum started")
    for g in rig.guiders:
        g.fail = "clouds"
    await asyncio.wait_for(asyncio.shield(rig.tasks[-1]), 20)
    assert rig.spec.jobs[1]["state"] == "aborted"
    aborts = [d for w, d in rig.spec.calls if w == "abort"]
    assert aborts and "dropped out" in aborts[0]["reason"]
    assert not rig.sequencer.spectrum_records
    assert all(u.gave_up for u in rig.sequencer.units.values())


async def test_the_spectrum_starts_without_a_member_that_never_comes(make_rig, tmp_path):
    plan = StaticPlan([spectrum_target()], ["t1", "t2"])
    rig = rv_rig(make_rig, tmp_path, plan, seq=dict(spectrum_ready_timeout=0.8))
    # t2 is slow to acquire: it misses the start and does not join late.
    rig.guiders[1].start_delay = 2.0
    rig.sequencer.units["t1"].guider.start_delay = 0.0
    await run_to_end(rig)
    [frame] = rig.spec.science()
    assert frame["telescopes"] == ["t1"]
    timeout = events(rig, "spectrum_feed_timeout")
    assert timeout and timeout[0].detail["missing"] == ["t2"]
    # A static plan names no leader: t2 finds it already taken.
    late = events(rig, "spectrum_missed") + events(rig, "spectrum_already_taken")
    assert [e.detail["telescope"] for e in late] == ["t2"]


async def test_lease_revoked_mid_spectrum_aborts_it_before_parking(make_rig, tmp_path):
    plan = StaticPlan([spectrum_target(exptime=30.0)], ["t1", "t2"])
    rig = rv_rig(make_rig, tmp_path, plan)
    await rig.start()
    await wait_until(lambda: rig.spec.science(), 10, what="spectrum started")
    rig.weather.set_safe(False)
    await wait_until(lambda: rig.timeline.events("lease_released"), 10)
    assert rig.spec.jobs[1]["state"] == "aborted"
    abort = rig.timeline.events("abort", "spectrograph")[0].t
    parks = [
        e
        for name in ("t1", "t2")
        for e in rig.timeline.commands(f"{name}.telescope", outcome="sent")
        if e.what == "Park"
    ]
    stops = [e for g in ("t1", "t2") for e in rig.timeline.events("stop", f"guider.{g}")]
    assert parks and abort < min(p.t for p in parks)
    assert max(s.t for s in stops if s.t < min(p.t for p in parks)) < min(p.t for p in parks)
    report = rig.sequencer.cleanups[-1]
    assert report.spectrograph_aborted is True and report.all_parked
    assert ("rv1" in {o[1] for o in plan.history}) and "interrupted" in {o[2] for o in plan.history}
    assert not rig.sequencer.spectrum_records


async def test_spectrograph_calibrations_take_no_lease(make_rig, tmp_path):
    # MINERVA's (KiwiSpec is iodine only): biases and darks.
    frames = [
        {"kind": "bias", "exptime": 0, "n": 2},
        {"kind": "dark", "exptime": 0.1, "n": 1},
    ]
    plan = CalibrationPlan(["t1", "t2"], biases=1, spectrograph=frames)
    rig = rv_rig(make_rig, tmp_path, plan)
    await run_to_end(rig)
    assert not rig.timeline.events("lease_granted")
    assert rig.dome_closed()
    [(_, call)] = [c for c in rig.spec.calls if c[0] == "calibrate"]
    assert call["frames"] == frames
    kinds = [r["kind"] for r in rig.sequencer.spectrum_records]
    assert kinds == ["bias", "bias", "dark"]
    assert [r["dark"] for r in rig.sequencer.spectrum_records] == [True, True, True]
    assert ("t1", "spectrograph calibration", "complete", 3) in plan.history
    assert len(list(tmp_path.glob("minerva/kiwispec/n*/*.json"))) == 3


def test_observation_validation():
    with pytest.raises(ValueError, match="needs light"):
        Observation("x", kind="spectrum", light=False)
    with pytest.raises(ValueError, match="no calibration frames"):
        Observation("x", kind="spectrograph_calibration", light=False)
    obs = Observation("x", kind="spectrum")
    assert obs.guide and obs.spectrum == {"id": "x", "feed": []}


# -- the request model and the dispatch scheduler -------------------------------------


def _rv_group(gid, ha=-1.0, *, priority=2.0, telescopes=("t1", "t2"), filter=None, **extra):
    """A SPECTRUM group in the OCS shape; ``extra``: the instrument config's
    extra_params (iodine, target_counts)."""
    from .test_dispatch import group, ic

    one = ic(600, 1, filter=filter)
    if extra:
        one["extra_params"] = extra
    g = group(gid, ha, priority=priority, ics=[one], telescopes=list(telescopes))
    cfg = g["requests"][0]["configurations"][0]
    cfg["type"] = "SPECTRUM"
    del cfg["instrument_type"]
    cfg["target"].update(
        proper_motion_ra=-1729.7, proper_motion_dec=855.3, epoch=2000.0, parallax=273.8
    )
    return g


def _limits():
    from robofast_core.dispatch import TelescopeLimits

    both = ["imager", "spectrograph"]
    return {"t1": TelescopeLimits(instruments=both), "t2": TelescopeLimits(instruments=both)}


def test_spectrum_configurations_validate_and_round_trip():
    from robofast_core.request_model import RequestGroup

    g = RequestGroup.from_dict(_rv_group("rv", iodine=True, target_counts=2e6))
    c = g.requests[0].configurations[0]
    assert (c.type, c.instrument, c.guide) == ("SPECTRUM", "spectrograph", True)
    assert c.instrument_configs[0].iodine is True and c.target.parallax == 273.8
    assert RequestGroup.from_dict(g.to_dict()).to_dict() == g.to_dict()
    bad = _rv_group("x", filter="V")
    with pytest.raises(ValueError, match="no filter or port"):
        RequestGroup.from_dict(bad)
    imaging = _rv_group("y")
    imaging["requests"][0]["configurations"][0]["type"] = "EXPOSE"
    imaging["requests"][0]["configurations"][0]["instrument_configs"][0]["extra_params"] = {
        "iodine": True
    }
    with pytest.raises(ValueError, match="for SPECTRUM"):
        RequestGroup.from_dict(imaging)


async def test_dispatch_reserves_the_feed_and_only_the_leader_counts(tmp_path):
    from robofast_core.plan_record import PlanRecord, read_record, replay
    from robofast_core.request_model import Status

    from .test_dispatch import NIGHT, Clock, make

    clock = Clock()
    rec = PlanRecord(tmp_path, longitude=MTHOPKINS.longitude)
    # An idle member is woken (preempt "next": nothing is interrupted).
    d0 = make([_rv_group("rv")], limits=_limits())
    woken = []
    d0.set_preempt_callback(lambda t, p, r: woken.append((t, p)) or True)
    d0._idle.add("t2")
    await d0.next_observation("t1")
    assert woken == [("t2", "next")]

    d = make([_rv_group("rv", iodine=False)], clock=clock, limits=_limits(), record=rec)
    lead = await d.next_observation("t1")
    assert lead.kind == "spectrum" and lead.spectrum["feed"] == ["t1", "t2"]
    assert lead.spectrum["leader"] == "t1" and lead.spectrum["iodine"] is False
    assert lead.spectrum["target"]["pm_dec"] == 855.3
    member = await d.next_observation("t2")
    assert member.spectrum["id"] == lead.spectrum["id"] and member.spectrum["leader"] == "t1"
    assert d.current()["t2"]["leader"] == "t1"
    # The member's report changes nothing; the leader's completes it.
    d.report("t2", member, "failed", 0)
    assert d.status("rv-c") == Status.PENDING
    clock.advance(700)
    d.report("t1", lead, "complete", 1)
    assert d.status("rv-c") == Status.COMPLETED
    assert "t2" not in d._members
    # The record replays, joins included.
    entries = read_record(next(tmp_path.glob("*.jsonl")))
    assert [e["kind"] for e in entries if e["kind"] != "snapshot"][:2] == ["decision", "join"]
    result = await replay(entries)
    assert result.ok, result.mismatches
    assert NIGHT  # (the fixed clock's night)


async def test_dispatch_one_spectrum_at_a_time_and_busy_telescopes_are_not_fed():
    from .test_dispatch import make

    d = make(
        [_rv_group("rv1", priority=3.0), _rv_group("rv2", priority=2.0), _img_group()],
        limits=_limits(),
    )
    img = await d.next_observation("t2")  # t2 busy imaging (only t2 may)
    assert img.kind == "expose"
    lead = await d.next_observation("t1")
    assert lead.spectrum["feed"] == ["t1"]  # t2 is busy
    cands = {c.request.id: c for c in d.evaluate("t2")}
    assert "spectrograph in use by t1" in cands["rv2-r"].reasons


def _img_group():
    from .test_dispatch import group

    return group("img", -0.5, priority=5.0, telescopes=["t2"])
