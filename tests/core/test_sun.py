"""The Sun's altitude (robofast_core.sun, astropy): legacy agreement, offline use."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from robofast_core.sun import LEGACY_PRESSURE_HPA, LEGACY_TEMPERATURE_C, sun_altitude
from robofast_core.weather_sources import SourceConfig, SunSource

ROOT = Path(__file__).resolve().parents[2]
MINERVA = (31.680407, -110.878977, 2316.0)

# Apparent altitudes of the Sun from PyEphem 4.2.1 with a default
# ephem.Observer (1010 mbar, 15 degC: refraction on), exactly as legacy
# minerva_library/env.py site.sunalt computed them; generated offline.
# astropy with the same pressure/temperature agrees to < 0.002 deg here (all
# above +6 deg, where the safety limit is).
PYEPHEM_REFERENCE = [
    ("2026-10-31T00:00:00", *MINERVA, 6.5014),
    ("2026-10-30T23:50:00", *MINERVA, 8.4544),
    ("2026-10-30T14:30:00", *MINERVA, 9.6343),
    ("2026-06-21T19:00:00", *MINERVA, 80.0261),
    ("2026-12-21T15:00:00", *MINERVA, 6.7828),
    ("2026-03-20T14:10:00", *MINERVA, 8.3960),
    ("2000-01-01T12:00:00", 51.4769, 0.0, 0.0, 15.5418),
    ("2045-07-04T20:00:00", -30.2446, -70.7494, 2200.0, 19.7350),
    ("1990-02-15T22:45:00", 19.8207, -155.4681, 4200.0, 57.5965),
]

# Geometric (pressure 0) topocentric altitudes from astropy 7.2, computed
# offline before this module used astropy; any astropy should reproduce them.
GEOMETRIC_REFERENCE = [
    ("2026-10-31T03:16:02", 31.680407, -110.878977, -34.607),
    ("2026-10-31T01:00:00", 31.680407, -110.878977, -5.844),
    ("2026-06-21T19:00:00", 31.680407, -110.878977, 80.023),
    ("2026-12-21T14:00:00", 31.680407, -110.878977, -4.519),
    ("2026-03-20T13:30:00", 31.680407, -110.878977, -0.212),
    ("2000-01-01T12:00:00", 51.4769, 0.0, 15.485),
    ("2045-07-04T08:00:00", -30.2446, -70.7494, -46.632),
    ("1990-02-15T22:45:00", 19.8207, -155.4681, 57.586),
]


def _t(when: str) -> datetime:
    return datetime.fromisoformat(when).replace(tzinfo=UTC)


@pytest.mark.parametrize(("when", "lat", "lon", "elev", "alt"), PYEPHEM_REFERENCE)
def test_apparent_altitude_matches_legacy_pyephem(when, lat, lon, elev, alt):
    got = sun_altitude(
        _t(when),
        lat,
        lon,
        elev,
        pressure_hpa=LEGACY_PRESSURE_HPA,
        temperature_c=LEGACY_TEMPERATURE_C,
    )
    assert got == pytest.approx(alt, abs=0.003)


@pytest.mark.parametrize(("when", "lat", "lon", "alt"), GEOMETRIC_REFERENCE)
def test_geometric_altitude_reference_values(when, lat, lon, alt):
    assert sun_altitude(_t(when), lat, lon) == pytest.approx(alt, abs=0.002)


def test_refraction_raises_the_sun_near_the_limit():
    t = _t("2026-10-31T00:00:00")  # the Sun near +6 deg at MINERVA
    geometric = sun_altitude(t, *MINERVA)
    apparent = sun_altitude(t, *MINERVA, pressure_hpa=1010, temperature_c=15)
    assert apparent - geometric == pytest.approx(0.127, abs=0.005)


def test_sun_needs_aware_time():
    with pytest.raises(ValueError, match="aware"):
        sun_altitude(datetime(2026, 1, 1), 0, 0)


def test_non_utc_aware_time_is_converted():
    t = _t("2026-10-31T00:00:00")
    mst = datetime.fromisoformat("2026-10-30T17:00:00-07:00")
    assert sun_altitude(mst, *MINERVA) == sun_altitude(t, *MINERVA)


def test_sun_source_reports_apparent_and_geometric():
    src = SunSource(
        SourceConfig("sun", "sun", options={"pressure_hpa": 1010.0, "temperature_c": 15.0}),
        latitude=MINERVA[0],
        longitude=MINERVA[1],
        elevation=MINERVA[2],
    )
    r = src.read(_t("2026-10-31T00:00:00"))
    assert r.values["altitude"] == pytest.approx(6.5014, abs=0.003)
    assert r.values["altitude_geometric"] == pytest.approx(6.373, abs=0.002)
    with pytest.raises(ValueError, match="unknown options"):
        SunSource(SourceConfig("sun", "sun", options={"presure": 1}), latitude=0, longitude=0)


def test_iers_downloads_are_off_after_import():
    from astropy.utils import iers

    assert iers.conf.auto_download is False
    assert iers.conf.auto_max_age is None


_OFFLINE = textwrap.dedent(
    """
    import socket, time
    attempts = []
    def refuse(*a, **k):
        attempts.append(a)
        raise OSError("network disabled for this test")
    socket.socket.connect = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse
    from datetime import UTC, datetime
    # Pretend it is 2029, so the IERS-A predictions bundled with astropy are
    # long out of date: what a machine running an old install meets.
    from astropy.time import Time
    Time.now = classmethod(lambda cls: Time("2029-01-01T00:00:00", scale="utc"))
    from robofast_core.sun import sun_altitude
    t0 = time.monotonic()
    for when in (
        datetime.now(UTC),
        datetime(2028, 12, 31, 20, tzinfo=UTC),
        datetime(2045, 7, 4, 20, tzinfo=UTC),
    ):
        alt = sun_altitude(when, 31.68, -110.88, 2316.0, pressure_hpa=1010.0)
        assert -91 < alt < 91, alt
    print("ok", len(attempts), round(time.monotonic() - t0, 2))
    """
)


def test_sun_works_offline_with_an_empty_astropy_cache(tmp_path):
    """No network and no cached IERS files: no download attempt, no error, no stall."""
    for d in ("cache", "config"):
        (tmp_path / d).mkdir()
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "PYTHONPATH": str(ROOT),
    }
    out = subprocess.run(
        [sys.executable, "-c", _OFFLINE],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    status, attempts, seconds = out.stdout.split()
    assert status == "ok" and attempts == "0", out.stdout
    assert float(seconds) < 20
