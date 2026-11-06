"""The simulated night's safety checks (robofast.testing.rv_night) on hand-made timelines."""

from __future__ import annotations

from pathlib import Path

from robofast.testing.rv_night import SEQUENCER, NightSettings, Outcome, safety_violations
from robofast.timeline import Entry, Timeline


def cleanup(*commands: tuple[float, str, str]) -> Outcome:
    """One cleanup (t 0 to 10) with ``(t, device, member)`` commands sent."""
    tl = Timeline()
    tl._add(Entry(0.0, "event", SEQUENCER, None, "cleanup_start", "", {"gen": 2}))
    for t, device, member in commands:
        tl._add(Entry(t, "command", "cleanup#2", device, member, "sent", {}))
    tl._add(Entry(10.0, "event", SEQUENCER, None, "cleanup_done", "", {"gen": 2}))
    return Outcome(out=Path("."), settings=NightSettings(), timelines={"night": tl})


def test_each_telescope_aborts_before_it_parks():
    # MINERVA dry run 2026-11-05: t1's camera abort was slow and came after
    # t2 had parked. The telescopes clean up in parallel: that is fine.
    o = cleanup(
        (1.0, "t2.camera", "AbortExposure"),
        (2.0, "t2.telescope", "Park"),
        (3.0, "t1.camera", "AbortExposure"),
        (4.0, "t1.telescope", "Park"),
    )
    assert safety_violations(o) == []


def test_a_telescope_parked_before_its_abort_is_a_violation():
    o = cleanup((1.0, "t1.telescope", "Park"), (2.0, "t1.camera", "AbortExposure"))
    assert safety_violations(o) == ["night cleanup #2: an abort after a park (t1)"]
