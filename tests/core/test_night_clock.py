"""One clock for every night name and time stamp (robofast_core.clock), and
the dispatch scheduler's view of operator holds and of a spectrograph that
is down (docs/rv_night.md)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from robofast_core.autofocus_plan import AutofocusTriggers
from robofast_core.clock import FileClock, ScaledClock, clock_from_config, real_clock
from robofast_core.console.hold import OperatorHold
from robofast_core.plan import Idle, Observation, StaticPlan
from robofast_core.plan_record import PlanRecord, read_record, replay
from robofast_core.spectra import SpectrumRecorder

from .test_imaging import attach, run_night
from .test_rv_spectra import (
    MTHOPKINS,
    FakeGuider,
    FakeSpectrograph,
    _img_group,
    _limits,
    _rv_group,
    spectrum_target,
)

#: 03:00 UTC on 2026-11-10 is the evening of 2026-11-09 in Arizona.
SIM = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)


def test_scaled_clock_and_file_clock(tmp_path):
    c = ScaledClock(SIM, 100.0)
    t0 = c()
    time.sleep(0.05)
    assert 4.0 < (c() - t0).total_seconds() < 30.0
    # Another process given the same numbers keeps the same time.
    twin = ScaledClock.from_dict(c.to_dict())
    assert abs((twin() - c()).total_seconds()) < 1.0
    path = tmp_path / "clock.json"
    fc = FileClock(path)
    assert abs((fc() - datetime.now(UTC)).total_seconds()) < 5  # no file yet: real
    ScaledClock(SIM, 1.0).write(path)
    assert abs((fc() - SIM).total_seconds()) < 5
    later = SIM + timedelta(hours=10)
    time.sleep(0.01)
    ScaledClock(later, 1.0).write(path)  # every reader follows the file
    assert abs((fc() - later).total_seconds()) < 5
    path.write_text("garbage")
    assert abs((fc() - later).total_seconds()) < 5  # the last good clock goes on
    assert clock_from_config(None) is real_clock
    assert isinstance(clock_from_config({"file": "c.json"}, tmp_path), FileClock)
    with pytest.raises(ValueError):
        ScaledClock(datetime(2026, 1, 1), 1.0)  # naive
    with pytest.raises(ValueError):
        clock_from_config({"start": SIM.isoformat(), "bogus": 1})


async def test_a_time_scaled_night_has_one_night_name_everywhere(make_rig, tmp_path):
    """Frames, the frame index, spectrum sidecars and their index, the raw
    spectrum file names asked for, autofocus-plan entries and the plan
    record all take the sequencer's (simulated) clock -- not the wall
    clock, which is days away."""
    clock = ScaledClock(SIM, 50.0)
    record = PlanRecord(tmp_path / "plans", timezone="America/Phoenix")

    class Recorded(StaticPlan):
        """A static plan with the dispatch scheduler's ``clock`` and ``record``."""

    targets = [
        {"name": "field", "exptime": 0.1, "count": 2, "ra_hours": 1.0, "dec_deg": 10.0},
        spectrum_target("rv1", feed=["t1"]),
    ]
    inner = Recorded(targets, ["t1"])
    inner.clock, inner.record = clock, record
    plan = AutofocusTriggers(inner)
    rig = make_rig(telescopes=1, plan=plan, seq=dict(spectrum_poll=0.05, spectrum_margin=3.0))
    seq = rig.sequencer
    seq.clock = clock
    imaging = attach(rig, tmp_path)
    imaging.set_clock(clock)
    rig.cam.clock = clock  # the (simulated) camera's own clock agrees
    seq.spectrograph = FakeSpectrograph(rig.timeline)
    seq.spectra = SpectrumRecorder(
        tmp_path / "data", "testsite", MTHOPKINS, timezone="America/Phoenix", clock=clock
    )
    seq.units["t1"].guider = FakeGuider(rig.timeline, "t1")
    # An autofocus result reaches the plan record through the triggers.
    plan.autofocus_result("t1", Observation("af"), {"success": True, "focus": 1.0})
    await run_night(rig, imaging)

    data = tmp_path / "data" / "testsite"
    nights = {p.name for p in data.glob("*/n*")}
    assert nights == {"n20261109"}
    names = {p.name.split(".", 1)[0] for p in data.glob("*/n*/*")}
    assert names == {"n20261109"}  # FITS, frame index, sidecar, spectra index
    [frame] = [d for w, d in seq.spectrograph.calls if w == "expose"]
    assert frame["night"] == "n20261109"  # the raw file's name on the spectrograph PC
    [sidecar] = data.glob("kiwispec/n*/*.json")
    rec = json.loads(sidecar.read_text())
    assert rec["night"] == "n20261109" and rec["sequencer_utc"].startswith("2026-11-10")
    from astropy.io import fits

    for p in data.glob("t1/n*/*.fits"):
        h = fits.getheader(p)
        assert h["NIGHT"] == "n20261109"
        assert h["DATE-OBS"].startswith("2026-11-10") and h["DATE"].startswith("2026-11-10")
    index = next(data.glob("t1/n*/*.frames.jsonl")).read_text().splitlines()
    assert all(json.loads(x)["written"].startswith("2026-11-10") for x in index)
    [plan_file] = (tmp_path / "plans").glob("*.jsonl")
    assert plan_file.name == "plan-2026-11-09.jsonl"
    assert [e["kind"] for e in read_record(plan_file)] == ["autofocus"]


# -- the dispatch scheduler: operator holds, a spectrograph that is down ------------------


async def test_a_held_telescope_is_not_reserved_to_feed_a_spectrum(tmp_path):
    from .test_dispatch import Clock, make

    rec = PlanRecord(tmp_path, longitude=MTHOPKINS.longitude)
    d = make([_rv_group("rv")], clock=Clock(), limits=_limits(), record=rec)
    # The console's hold reaches the scheduler through the plan wrappers.
    hold = OperatorHold(AutofocusTriggers(d), ["t1", "t2"], tmp_path / "control")
    hold.apply({"action": "pause", "telescopes": ["t2"], "operator": "obs", "reason": "FAU"})
    assert d.held == {"t2"}
    lead = await d.next_observation("t1")
    assert lead.spectrum["feed"] == ["t1"]
    assert "t2" not in d._members
    # Resumed: the next spectrum may be fed by t2 again.
    hold.apply({"action": "resume", "telescopes": ["t2"], "operator": "obs", "reason": "ok"})
    assert d.held == set()
    d.report("t1", lead, "interrupted", 0)
    lead = await d.next_observation("t1")
    assert lead.spectrum["feed"] == ["t1", "t2"]
    # A hold that arrives after the reservation drops it.
    d.set_held(["t2"])
    assert "t2" not in d._members
    entries = read_record(next(tmp_path.glob("*.jsonl")))
    assert [e["telescopes"] for e in entries if e["kind"] == "held"] == [["t2"], [], ["t2"]]
    result = await replay(entries)
    assert result.ok, result.mismatches


async def test_no_spectra_while_the_spectrograph_is_down_imaging_goes_on(tmp_path):
    from .test_dispatch import Clock, make

    rec = PlanRecord(tmp_path, longitude=MTHOPKINS.longitude)
    d = make(
        [_rv_group("rv", priority=5.0), _img_group()], clock=Clock(), limits=_limits(), record=rec
    )
    d.set_spectrograph_available(False, "3 faults in a row")
    assert d.spectrograph_down == "3 faults in a row"
    reasons = {c.request.id: c.reasons for c in d.evaluate("t1")}
    assert any("spectrograph down" in r for r in reasons["rv-r"])
    obs = await d.next_observation("t1")
    assert isinstance(obs, Idle)  # nothing: the image is for t2 only
    img = await d.next_observation("t2")
    assert img.kind == "expose"
    d.set_spectrograph_available(True, "recovery check passed")
    obs = await d.next_observation("t1")
    assert obs.kind == "spectrum"
    entries = read_record(next(tmp_path.glob("*.jsonl")))
    assert [e["available"] for e in entries if e["kind"] == "spectrograph"] == [False, True]
    result = await replay(entries)
    assert result.ok, result.mismatches
    # The snapshot keeps it, for a replay that starts later in the night.
    d.set_spectrograph_available(False, "again")
    snap = d.snapshot()
    assert snap["spectrograph_down"] == "again"
    assert type(d).from_snapshot(snap, clock=Clock()).spectrograph_down == "again"
