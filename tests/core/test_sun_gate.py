"""The Sun gate (robofast.sun_gate, docs/first_light.md "The Sun"): only in
the dark, and never near the Sun; its twilight boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robofast import sun_gate
from robofast.sun_gate import SunGateError

SITE = (42.38, -71.13, 0.0)  # Cambridge, MA
NOON = datetime(2026, 11, 7, 17, 0, tzinfo=UTC)  # local noon (EST)
NIGHT = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)  # 22:00 EST


def _alt(when: datetime) -> float:
    return float(sun_gate.sun_altitudes(SITE, [when])[0])


def _evening_crossing(limit: float) -> datetime:
    """When the Sun sets through ``limit`` on the evening of 2026-11-07."""
    t = sun_gate._crossing(SITE, NOON, limit, rising=False, hours=12.0)
    assert t is not None
    return t


def test_daylight_is_refused_saying_when_it_is_dark():
    assert _alt(NOON) > 20
    with pytest.raises(SunGateError, match="the Sun is up") as exc:
        sun_gate.check_dark(SITE, NOON)
    assert "next at -12 deg at 2026-11-07 2" in str(exc.value), exc.value  # ~22:4x UTC


def test_the_night_is_dark_until_the_dawn():
    until = sun_gate.check_dark(SITE, NIGHT)
    assert until is not None and NIGHT < until < NIGHT + timedelta(hours=12)
    assert _alt(until) == pytest.approx(-12.0, abs=0.1)
    assert _alt(until + timedelta(minutes=5)) > -12.0  # rising: the dawn


@pytest.mark.parametrize("limit", [-12.0, -18.0, -6.0, 0.0])
def test_the_twilight_boundary(limit):
    """Two minutes before the Sun sets through the limit: refused; two
    minutes after: allowed."""
    t = _evening_crossing(limit)
    assert _alt(t) == pytest.approx(limit, abs=0.1)
    with pytest.raises(SunGateError):
        sun_gate.check_dark(SITE, t - timedelta(minutes=2), limit)
    assert sun_gate.check_dark(SITE, t + timedelta(minutes=2), limit) is not None


def test_civil_twilight_is_refused_by_default():
    t = _evening_crossing(-6.0) + timedelta(minutes=5)  # the Sun near -7 deg
    assert -12.0 < _alt(t) < -6.0
    with pytest.raises(SunGateError):
        sun_gate.check_dark(SITE, t)


def test_a_target_near_the_sun_is_refused():
    from astropy.coordinates import get_body
    from astropy.time import Time

    sun = get_body("sun", Time(NIGHT))
    ra, dec = float(sun.ra.deg), float(sun.dec.deg)
    with pytest.raises(SunGateError, match="the Sun's place is 0 deg from the Sun"):
        sun_gate.check_targets(SITE, NIGHT, [("the Sun's place", ra, dec)])
    # 29 deg away along the declination circle-ish: refused; 31: allowed.
    near, far = (ra, dec + 29.0), (ra, dec + 31.0)
    with pytest.raises(SunGateError, match="29 deg from the Sun"):
        sun_gate.check_targets(SITE, NIGHT, [("far", *far), ("near", *near)])
    sun_gate.check_targets(SITE, NIGHT, [("far", *far)])
    # The margin (what the sky turns before it is observed) counts too.
    with pytest.raises(SunGateError, match="far is 31 deg"):
        sun_gate.check_targets(SITE, NIGHT, [("far", *far)], margin=2.0)


@pytest.mark.parametrize(
    "alt, sep, words",
    [(5.0, 30.0, "max_sun_alt 5"), (-12.0, 10.0, "min_sun_separation 10")],
)
def test_neither_rule_can_be_turned_off(alt, sep, words):
    with pytest.raises(ValueError, match=words):
        sun_gate.check_limits(alt, sep)
    sun_gate.check_limits(0.0, 15.0)  # the loosest allowed
