"""TPOINT-style pointing-error models: applied, inverted, fitted."""

from __future__ import annotations

import numpy as np
import pytest

from robofast_core import pointing as pm
from robofast_core.catalog import separation

LAT = 31.68


def _grid(n: int = 60, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    ha = rng.uniform(-80, 80, n)
    dec = rng.uniform(-30, 80, n)
    return ha, dec


EQ = {"IH": 120.0, "ID": -45.0, "CH": 30.0, "NP": -12.0, "MA": 40.0, "ME": -25.0, "TF": 15.0,
      "FO": 8.0}  # fmt: skip
AZ = {"IA": 300.0, "IE": -60.0, "CA": 20.0, "NPAE": 10.0, "AN": 35.0, "AW": -18.0, "TF": 12.0}


def test_index_terms_are_offsets():
    m = pm.PointingModel({"IH": 36.0, "ID": 18.0}, latitude=LAT)
    mh, md = m.mount_from_true(10.0, 20.0)
    assert mh == pytest.approx(10.0 - 0.01) and md == pytest.approx(20.0 - 0.005)


@pytest.mark.parametrize("mount,terms", [("equatorial", EQ), ("altaz", AZ)])
def test_inverse_round_trips(mount, terms):
    m = pm.PointingModel(terms, mount=mount, latitude=LAT)
    a, b = _grid()
    if mount == "altaz":
        a, b = pm.hadec_to_altaz(a, b, LAT)
        keep = b > 15
        a, b = a[keep], b[keep]
    m1, m2 = m.mount_from_true(a, b)
    # The error is real (arcminutes), and the inverse undoes it to < 1 mas.
    assert np.max(separation(a, b, m1, m2)) * 3600 > 60
    t1, t2 = m.true_from_mount(m1, m2)
    assert np.max(np.abs(t1 - a)) * 3600 < 1e-3
    assert np.max(np.abs(t2 - b)) * 3600 < 1e-3


@pytest.mark.parametrize("mount,terms", [("equatorial", EQ), ("altaz", AZ)])
def test_fit_recovers_the_terms(mount, terms):
    m = pm.PointingModel(terms, mount=mount, latitude=LAT)
    a, b = _grid(80, seed=2)
    if mount == "altaz":
        a, b = pm.hadec_to_altaz(a, b, LAT)
        keep = b > 15
        a, b = a[keep], b[keep]
    m1, m2 = m.mount_from_true(a, b)
    rng = np.random.default_rng(3)
    noise = 0.5 / 3600.0  # 0.5 arcsec measurement noise
    t1 = a + rng.normal(0, noise, a.shape) / np.cos(np.radians(b))
    t2 = b + rng.normal(0, noise, b.shape)
    fitted, info = pm.fit(list(terms), t1, t2, m1, m2, mount=mount, latitude=LAT)
    for k, v in terms.items():
        assert fitted.terms[k] == pytest.approx(v, abs=max(5 * info["errors"][k], 0.5)), k
    assert info["rms_after"] < 1.0 < info["rms_before"]


def test_unknown_term_and_mount():
    with pytest.raises(ValueError, match="unknown terms"):
        pm.PointingModel({"XX": 1.0})
    with pytest.raises(ValueError, match="equatorial or altaz"):
        pm.PointingModel({}, mount="dobsonian")


def test_round_trip_dict():
    m = pm.PointingModel(EQ, latitude=LAT)
    assert pm.PointingModel.from_dict(m.as_dict()) == m


def test_altaz_conversions_invert():
    ha, dec = _grid()
    az, alt = pm.hadec_to_altaz(ha, dec, LAT)
    h2, d2 = pm.altaz_to_hadec(az, alt, LAT)
    assert np.allclose(h2, ha) and np.allclose(d2, dec)
    # On the meridian, south of the zenith: azimuth 180.
    az0, alt0 = pm.hadec_to_altaz(0.0, LAT - 30.0, LAT)
    assert az0 == pytest.approx(180.0) and alt0 == pytest.approx(60.0)
