"""``python -m robofast_core.when``: wait for a Sun altitude (no real waiting)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robofast_core.config import load_config
from robofast_core.sky import Site
from robofast_core.when import main, next_time, site_from_config, sun_altitudes, wait_for_sun

SITES = Path(__file__).resolve().parents[2] / "sites"
MINERVA = Site(31.680407, -110.878977, 2316.0)
#: 12:00 MST on 2026-11-15; sunset is near 00:20 UTC.
NOON = datetime(2026, 11, 15, 19, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 11, 16, 7, 0, tzinfo=UTC)


def _alt(t: datetime) -> float:
    return float(sun_altitudes(MINERVA, [t])[0])


def test_next_time_finds_the_evening_crossing_to_a_second():
    t = next_time(MINERVA, NOON, below=-6.0)
    assert NOON + timedelta(hours=5) < t < NOON + timedelta(hours=7)
    assert _alt(t) < -6.0 < _alt(t - timedelta(seconds=2))
    assert _alt(t) == pytest.approx(-6.0, abs=0.01)


def test_next_time_now_if_it_already_holds_and_the_morning_for_above():
    assert next_time(MINERVA, MIDNIGHT, below=-6.0) == MIDNIGHT
    dawn = next_time(MINERVA, MIDNIGHT, above=0.0)
    assert MIDNIGHT < dawn < MIDNIGHT + timedelta(hours=8)
    assert _alt(dawn) == pytest.approx(0.0, abs=0.01)
    # Both: a band (twilight between -12 and -6), in the evening.
    t = next_time(MINERVA, NOON, below=-6.0, above=-12.0)
    assert -12.0 < _alt(t) < -6.0
    with pytest.raises(ValueError):
        next_time(MINERVA, NOON)


def test_never_within_the_horizon():
    pole = Site(89.9, 0.0)
    june = datetime(2026, 6, 21, tzinfo=UTC)
    assert next_time(pole, june, below=-6.0) is None


class FakeTime:
    def __init__(self, t: datetime) -> None:
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)


async def test_wait_for_sun_sleeps_until_the_crossing_replanning_each_poll():
    ft = FakeTime(NOON)
    assert await wait_for_sun(MINERVA, below=-6.0, poll=1800, now=ft.now, sleep=ft.sleep)
    assert all(s <= 1800 for s in ft.sleeps) and len(ft.sleeps) > 3
    assert ft.t == next_time(MINERVA, NOON, below=-6.0)


async def test_wait_for_sun_returns_at_once_or_gives_up():
    ft = FakeTime(MIDNIGHT)
    assert await wait_for_sun(MINERVA, below=-6.0, now=ft.now, sleep=ft.sleep)
    assert ft.sleeps == []
    ft = FakeTime(NOON)
    assert not await wait_for_sun(MINERVA, below=-6.0, max_wait=600, now=ft.now, sleep=ft.sleep)
    assert ft.sleeps == []


def test_site_from_the_enclosure_config_or_its_plan():
    assert site_from_config(load_config(SITES / "minerva" / "aqawan1.json")) == MINERVA
    plan = json.loads((SITES / "minerva" / "dispatch.example.json").read_text())
    assert site_from_config({"plan": plan}) == MINERVA
    with pytest.raises(ValueError, match="site"):
        site_from_config({"plan": {"type": "calibration"}})


def _exit(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_cli_check():
    config = str(SITES / "minerva" / "aqawan1.json")
    args = ["--config", config, "--sun-below", "-6", "--check", "--now"]
    assert _exit([*args, MIDNIGHT.isoformat()]) == 0
    assert _exit([*args, NOON.isoformat()]) == 1
    site = "31.680407,-110.878977"
    assert _exit(["--site", site, "--sun-above", "0", "--check", "--now", NOON.isoformat()]) == 0
    assert _exit(["--site", site, "--check"]) == 2  # no condition
    assert _exit(["--sun-below", "-6", "--check"]) == 2  # no site
    assert _exit(["--site", site, "--sun-below", "-6", "--now", NOON.isoformat()]) == 2
    assert _exit(["--site", "1", "--sun-below", "-6", "--check"]) == 2
