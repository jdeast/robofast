"""Which telescopes a simulated night or a hardware rehearsal has
(robofast.testing.rv_night, robofast.testing.rehearsal): the telescope names
live in the config and the command line only."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from robofast.dispatch import dispatch_from_config
from robofast.testing import rehearsal as reh
from robofast.testing.rv_night import (
    ENCLOSURE,
    SEQUENCER,
    NightSettings,
    Outcome,
    SimulatedNight,
    console_park_problems,
    roles,
)
from robofast.timeline import Entry, Timeline

THREE = ("t1", "t2", "t4")


def _args(names: tuple[str, ...], catalog_guide: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        i_know_the_enclosure_is_closed=True,
        telescope=[f"{n}=127.0.0.1:{11120 + i}/0" for i, n in enumerate(names, 1)],
        camera=[f"{n}=127.0.0.1:{7150 + i}/0" for i, n in enumerate(names, 1)],
        focuser=[],
        rotator=[],
        end_action="leave",
        az_window=[],
        duration=3600.0,
        exptime=60.0,
        images=1,
        image_exptime=1.0,
        spectra=None,
        ignore_twilight=False,
        console_port=-1,
        kiwispec=None,
        catalog_guide=catalog_guide,
        catalog_source=None,
        catalog_dir=None,
    )


def _accepted(entry: dict, names: list[str], base: Path) -> None:
    """The dispatch scheduler takes the plan entry for exactly ``names``."""
    dispatch_from_config({k: v for k, v in entry.items() if k != "type"}, names, base=base)


async def test_regression_20261127_a_rehearsal_is_exactly_the_telescopes_given(tmp_path):
    """MINERVA 2026-11-27: the four-telescope dry run only ran through an
    uncommitted patch of rv_night.NAMES (the rehearsal refused any telescope
    not in it), and a three-telescope night (t1, t2, t4 real) got a simulated
    t3 added because NAMES listed it. The night is now exactly the
    telescopes given, under any names."""
    s = reh.settings_from_args(_args(THREE))
    s.start = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)
    assert s.active == list(THREE)
    # --catalog-guide: T4 has no guider example; its FAU from instruments.json.
    assert reh.fau_example("t4") == {"fiber": [879.0, 675.0], "plate_scale": 0.265}
    night = reh.Rehearsal(tmp_path, s)
    assert night.s.telescopes == THREE
    assert night._spectrograph_telescopes() == ["T1", "T2", "T4"]
    try:
        night._start_telescopes()
        assert list(night.telescopes) == list(THREE)
        assert not hasattr(night, "alpaca")  # no simulated mount or camera served
        night.aqawan = SimpleNamespace(address="127.0.0.1:1")
        night._bridge_address = "127.0.0.1:2"
        cfg = night._enclosure()
        assert list(cfg["telescopes"]) == list(THREE)
        assert all(
            t["telescope"].startswith(night.guard.address) for t in cfg["telescopes"].values()
        )
        entry, _ = night._plan()
        assert list(entry["telescopes"]) == list(THREE)
        # T4 is not in the example (Aqawan 1's plan): T1's entry, the
        # rehearsal's limits.
        assert entry["telescopes"]["t4"] == entry["telescopes"]["t1"]
        spectra = [g for g in entry["requests"] if g["id"].startswith("REH")]
        assert spectra and all(
            g["requests"][0]["extra_params"]["telescopes"] == list(THREE) for g in spectra
        )
        imaging = [
            g["requests"][0]["location"]["telescope"]
            for g in entry["requests"]
            if g["id"].startswith("IMG")
        ]
        assert set(imaging) <= set(THREE)
        _accepted(entry, list(THREE), tmp_path)
    finally:
        await night.close()


def test_regression_20261127_a_rehearsal_takes_any_telescope_name():
    s = reh.settings_from_args(_args(("t4",)))
    assert s.active == ["t4"]
    s = reh.settings_from_args(_args(("north", "south"), catalog_guide=False))
    assert s.active == ["north", "south"]
    # --catalog-guide needs each telescope's FAU geometry: refused at once.
    with pytest.raises(reh.RehearsalRefused, match="no FAU geometry"):
        reh.settings_from_args(_args(("north",)))


async def test_regression_20261127_the_simulated_night_takes_its_telescopes_from_the_config(
    tmp_path,
):
    """The fully simulated night's default telescopes are the enclosure
    config's (sites/minerva/aqawan1.json), not a list in the code; another
    list runs the same night on those."""
    site = tuple(json.loads(ENCLOSURE.read_text())["telescopes"])
    assert NightSettings().telescopes == site
    night = SimulatedNight(tmp_path / "default")
    try:
        entry = night._dispatch_entry()
        assert tuple(entry["telescopes"]) == site
        _accepted(entry, list(site), tmp_path)
        assert night._spectrograph_telescopes() == [n.upper() for n in site]
        field = next(g for g in entry["requests"] if g["id"] == "FIELD")
        assert field["requests"][0]["location"]["telescope"] == site[0]
    finally:
        await night.close()

    night = SimulatedNight(tmp_path / "three", NightSettings(telescopes=THREE))
    try:
        entry = night._dispatch_entry()
        assert tuple(entry["telescopes"]) == THREE
        _accepted(entry, list(THREE), tmp_path)
        spectra = [g for g in entry["requests"] if g["id"].startswith("RV")]
        assert all(g["requests"][0]["extra_params"]["telescopes"] == list(THREE) for g in spectra)
        assert night._spectrograph_telescopes() == ["T1", "T2", "T4"]
        assert roles(night.s.telescopes) == ("t1", "t4")
    finally:
        await night.close()


def test_night_settings_validate_the_telescopes():
    with pytest.raises(ValueError, match="at least one telescope"):
        NightSettings(telescopes=())
    with pytest.raises(ValueError, match="named twice"):
        NightSettings(telescopes=("t1", "t1"))
    with pytest.raises(ValueError, match="at least two telescopes"):
        roles(("t1",))


def test_the_console_park_check_follows_the_telescope_list():
    """The console parks the night's last telescope (roles); another
    telescope parking meanwhile is reported by name."""
    tl = Timeline()
    tl._add(Entry(5.0, "event", SEQUENCER, None, "park_requested", "", {"telescope": "t4"}))
    tl._add(
        Entry(
            6.0,
            "event",
            SEQUENCER,
            None,
            "operator_parked",
            "",
            {"telescope": "t4", "parked": True},
        )
    )
    o = Outcome(out=Path("."), settings=NightSettings(telescopes=THREE), timelines={"night": tl})
    o.mount_events.extend([(1.0, "t4", "slew"), (5.5, "t4", "park")])
    assert console_park_problems(o) == []
    o.mount_events.append((5.6, "t2", "park"))
    assert console_park_problems(o) == ["t2 parked when the console parked t4"]
