"""The 60-inch ADC's tracking math: from the telescope's position to the two
prism motor angles.

The FLWO 60-inch's atmospheric dispersion corrector is a pair of
counter-rotating prisms, each turned by its own stepper motor
(``docs/adc.md``). The math here is ported from FLWO's legacy ADC daemon
(``adc_d``, part of the 60-inch TCS software "telshell", T. Groner; with
thanks to FLWO), checked against its own code with test vectors
(``tests/core/data/adc_legacy_vectors.json``):

1. The parallactic angle ``pa`` (deg, -180..180) of the target, from the
   hour angle and declination (legacy: SOFA ``iauHd2pa`` with the latitude
   hard-coded at 31.69 deg; here :func:`erfa.hd2pa` with the site's).
2. A **deflection** ``defl`` (deg) from the zenith distance: how far each
   prism turns away from the null setting (where the two prisms cancel).
   The legacy daemon uses a step table (:data:`LEGACY_TABLE`); robofast
   also offers a model from ERFA's refraction constants
   (:func:`refraction_deflection`), which reproduces that table to within
   its steps.
3. The prism angles (the legacy ``atarg``/``btarg``): prism A at
   ``90 - pa - defl``, prism B at ``270 - pa + defl`` (mod 360), so the
   pair sits at the null (180 deg apart) turned by ``-pa``, each prism
   counter-rotated by ``defl``.
4. Motor angles from each motor's home: ``home - angle`` (mod 360), with
   the homes at prism angles 202.5 (A) and 22.5 (B); steps =
   ``round(11.1111 * motor angle)``. Combined: motor A turns
   ``112.5 + pa + defl`` and motor B ``112.5 + pa - defl`` degrees from
   home (mod 360). Both homes are a null setting: home is neutral.

The net dispersion of two equal prisms ``2 * defl`` away from opposition
is proportional to ``sin(defl)`` (the sum of two equal vectors), along
the bisector of the two prisms, which follows the parallactic angle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Steps per degree of prism rotation (legacy ``STEPS_DEG``): 4000 steps a turn.
STEPS_PER_DEG = 11.1111
#: The prism angles (legacy convention) at each motor's home (step 0).
HOME_ANGLES = (202.5, 22.5)
#: The legacy table: (zenith distance above which, deflection deg), first match.
LEGACY_TABLE: tuple[tuple[float, float], ...] = (
    (67.0, 80.0),
    (63.0, 55.0),
    (58.0, 38.0),
    (53.0, 32.0),
    (48.0, 27.0),
    (43.0, 23.0),
    (38.0, 20.0),
    (28.0, 16.0),
    (18.0, 10.0),
    (8.0, 7.0),
    (4.0, 4.0),
    (2.0, 2.0),
)
#: The legacy daemon's latitude (rad), for the test vectors only.
LEGACY_LATITUDE_RAD = 0.5530948399570033


def legacy_deflection(zd: float) -> float:
    """The legacy table's deflection (deg) for zenith distance ``zd`` (deg)."""
    for above, defl in LEGACY_TABLE:
        if zd > above:
            return defl
    return 0.0


def legacy_adj_deg(num: float) -> float:
    """Legacy ``adj_deg``: one wrap into [0, 360] (not a full modulo)."""
    if num > 360.0:
        return num - 360.0
    if num >= 0.0:
        return num
    return 360.0 - (num * -1.0)


def legacy_prism_angles(pa: float, defl: float, *, swap: bool = False) -> tuple[float, float]:
    """The legacy ``atarg``, ``btarg`` (deg), computed exactly as ``adc_d``.

    ``swap`` is the legacy ``adc_d_swap_congruent`` variant, which gives
    prism A the target the production daemon gives B and vice versa.
    """
    apt = -1 * legacy_adj_deg(pa + 90.0)
    bpt = -1 * legacy_adj_deg(pa - 90.0)
    if swap:
        return legacy_adj_deg(apt + defl), legacy_adj_deg(bpt - defl)
    return legacy_adj_deg(bpt - defl), legacy_adj_deg(apt + defl)


def legacy_motor_degrees(angle: float, home: float) -> float:
    """Legacy ``adc_todegree``: the motor angle from home for prism ``angle``."""
    if angle <= home:
        return home - angle
    return 360.0 - (angle - home)


def steps_for(motor_deg: float) -> int:
    """Motor steps for a motor angle (C ``round``: half away from zero)."""
    x = STEPS_PER_DEG * motor_deg
    return math.floor(abs(x) + 0.5) * (1 if x >= 0 else -1)


def motor_degrees(pa: float, defl: float, *, swap: bool = False) -> tuple[float, float]:
    """Motor angles from home (deg, [0, 360)) for prisms A and B: the
    legacy chain (:func:`legacy_prism_angles`, :func:`legacy_motor_degrees`)
    in closed form."""
    base = 112.5 + pa
    if swap:
        base += 180.0
        defl = -defl
    return (base + defl) % 360.0, (base - defl) % 360.0


def prism_angle(motor_deg: float, home: float) -> float:
    """The legacy prism angle (deg, [0, 360)) at a motor angle from home."""
    return (home - motor_deg) % 360.0


def angle_diff(a: float, b: float) -> float:
    """|a - b| on the circle (deg, 0..180)."""
    d = (a - b) % 360.0
    return min(d, 360.0 - d)


def parallactic_angle(ha_rad: float, dec_rad: float, lat_rad: float) -> float:
    """The parallactic angle (deg, -180..180) by :func:`erfa.hd2pa`."""
    import erfa

    return math.degrees(float(erfa.hd2pa(ha_rad, dec_rad, lat_rad)))


def from_altaz(alt_deg: float, az_deg: float, lat_deg: float) -> tuple[float, float]:
    """(parallactic angle, zenith distance), deg, from the mount's altitude
    and azimuth (north 0, east 90: ASCOM's and ERFA's convention)."""
    import erfa

    phi = math.radians(lat_deg)
    ha, dec = erfa.ae2hd(math.radians(az_deg), math.radians(alt_deg), phi)
    return parallactic_angle(float(ha), float(dec), phi), 90.0 - alt_deg


@dataclass(frozen=True)
class Atmosphere:
    """Conditions for :func:`refraction_deflection` (ERFA ``refco``)."""

    pressure_hpa: float = 770.0
    temperature_c: float = 5.0
    humidity: float = 0.3


def dispersion_arcsec(zd: float, blue_um: float, red_um: float, atm: Atmosphere) -> float:
    """Atmospheric dispersion (arcsec) between two wavelengths at zenith
    distance ``zd`` (deg): ERFA's refraction model, ``A tan z + B tan^3 z``
    (:func:`erfa.refco`), blue minus red."""
    import erfa

    ab, bb = erfa.refco(atm.pressure_hpa, atm.temperature_c, atm.humidity, blue_um)
    ar, br = erfa.refco(atm.pressure_hpa, atm.temperature_c, atm.humidity, red_um)
    t = math.tan(math.radians(min(zd, 85.0)))
    return math.degrees((ab - ar) * t + (bb - br) * t**3) * 3600.0


def refraction_deflection(
    zd: float,
    blue_um: float,
    red_um: float,
    max_dispersion_arcsec: float,
    max_deflection: float,
    atm: Atmosphere,
) -> float:
    """The deflection (deg) whose correction ``max_dispersion * sin(defl)``
    cancels the atmosphere's dispersion over [blue, red], capped at
    ``max_deflection``."""
    if zd <= 0.0:
        return 0.0
    ratio = dispersion_arcsec(zd, blue_um, red_um, atm) / max_dispersion_arcsec
    defl = 90.0 if ratio >= 1.0 else math.degrees(math.asin(max(0.0, ratio)))
    return min(defl, max_deflection)
