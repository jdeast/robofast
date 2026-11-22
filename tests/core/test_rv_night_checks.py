"""The simulated night's safety checks (robofast.testing.rv_night) on hand-made timelines."""

from __future__ import annotations

from pathlib import Path

from robofast.testing.rv_night import (
    SEQUENCER,
    NightSettings,
    Outcome,
    console_park_problems,
    safety_violations,
)
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


def console_park(*mount_events: tuple[float, str, str]) -> Outcome:
    """The console parks t2 at t 5 (requested) to 6 (parked), with these
    ``(t, telescope, what)`` mount events."""
    tl = Timeline()
    tl._add(Entry(5.0, "event", SEQUENCER, None, "park_requested", "", {"telescope": "t2"}))
    tl._add(
        Entry(
            6.0,
            "event",
            SEQUENCER,
            None,
            "operator_parked",
            "",
            {"telescope": "t2", "parked": True},
        )
    )
    o = Outcome(out=Path("."), settings=NightSettings(), timelines={"night": tl})
    o.mount_events.extend(mount_events)
    return o


def test_the_console_park_parks_t2():
    assert console_park_problems(console_park((1.0, "t2", "slew"), (5.5, "t2", "park"))) == []


def test_t2_already_parked_needs_no_park_command():
    # test_rv_night "never parked" (master CI): T2's error path had parked it
    # and it was idle, unmoved, when the console asked; the sequencer then
    # sends no Park (it is AtPark), and the check failed.
    o = console_park((1.0, "t2", "slew"), (2.0, "t2", "park"))
    assert console_park_problems(o) == []


def test_t2_moved_and_not_parked_again_is_caught():
    o = console_park((1.0, "t2", "park"), (5.2, "t2", "unpark"), (5.3, "t2", "slew"))
    assert console_park_problems(o) == ["t2's mount never parked for the console"]
    assert console_park_problems(console_park((1.0, "t2", "slew"))) == [
        "t2's mount never parked for the console"
    ]


async def test_the_simulated_night_keeps_its_emergency_log_in_its_own_directory(tmp_path):
    """2026-11-21, the MINERVA gateway (robofast installed, /var/log/robofast
    root:robofast 0750): every e2e night read the example config's production
    emergency log, could not read it, assumed a latched emergency close, and
    never opened the simulated roof (6 e2e tests failed, 900 s timeouts)."""
    from robofast.emergency import EmergencyConfig, EmergencyGate
    from robofast.testing.rv_night import SimulatedNight

    night = SimulatedNight(tmp_path)
    try:
        night._start_aqawan()
        cfg = EmergencyConfig.from_dict(night.aqawan.config.emergency_close)
        assert cfg.log == str(tmp_path / "aqawan1-emergency.jsonl")
        assert EmergencyGate(cfg).latched is None
    finally:
        await night.close()
