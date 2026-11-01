"""Target vetting: what the catalog says about a field before it is observed.

A close twin next to an RV target fools "take the brightest star in the
FAU" acquisition; a G = 4 target saturates the guide camera; a faint one is
lost in it; a sparse field cannot be plate solved; a fast-moving star is
not where its J2000 coordinates say. :func:`vet` looks at the catalog
(:mod:`robofast_core.catalog`: Gaia DR3, the bright stars Gaia lacks, the
WDS for pairs Gaia misses) around a target, for one telescope port, and
returns :class:`Flag` s:

========================  ===================================================
``not_in_catalog``        no catalog star within ``match_radius`` of the target
``companion``             a star within ``companion_radius`` and ``companion_dmag``
``twin``                  ... of nearly equal brightness (``twin_dmag``)
``blend``                 ... closer than ``blend_radius`` (blends in typical seeing)
``wds_companion``         a WDS pair at the target with such a companion
``not_brightest``         a star at least as bright (within ``brightest_margin``)
                          inside the acquisition search box: brightest-star
                          acquisition could take the wrong one
``saturates``             the target's peak exceeds ``saturation_fraction`` of
                          full well in the port's exposure
``too_faint``             the target's S/N in that exposure is below ``min_snr``
``few_stars``             fewer than ``min_solve_stars`` stars with S/N above 10
                          in the field: no plate solve, no catalog-verified
                          acquisition
``high_proper_motion``    the target moves more than ``high_pm`` mas/yr
``fiber_contamination``   another star within the guider's capture radius of
                          the target (it sits on the fiber) and within
                          ``fiber_dmag``
``catalog_unavailable``   the catalog could not answer (offline, tiles missing)
========================  ===================================================

Every flag is a warning unless its code is in the config's ``reject``.
:attr:`VettingResult.unambiguous` says whether "brightest star" acquisition
is safe for this field (no twin, companion or brighter star in the search
box): the guider's catalog acquisition falls back to the brightest star
only then (:mod:`robofast_core.guider.identify`).

Ports (``ports`` in the config, keyed ``"<telescope>/<port>"`` or
``"<port>"``) give the geometry and photometry: detector ``shape`` (rows,
columns), ``plate_scale``, ``fiber`` (x, y pixel; None for an imager:
the target is at the centre), ``max_star_distance`` (the acquisition search
box half-size, px), ``capture_radius`` (px), ``exposure`` (s: the
acquisition exposure for the FAU, the science exposure otherwise),
``seeing`` (arcsec FWHM) and ``photometry``
(:class:`robofast_core.photometry.PortPhotometry`).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from robofast_core.catalog import (
    Catalog,
    CatalogUnavailable,
    _tan,
    decimal_year,
    propagate,
    separation,
)
from robofast_core.photometry import PortPhotometry

log = logging.getLogger(__name__)

__all__ = ["FLAGS", "Flag", "PortGeometry", "Vetter", "VettingConfig", "VettingResult", "vet"]

FLAGS = (
    "not_in_catalog",
    "companion",
    "twin",
    "blend",
    "wds_companion",
    "not_brightest",
    "saturates",
    "too_faint",
    "few_stars",
    "high_proper_motion",
    "fiber_contamination",
    "catalog_unavailable",
)


@dataclass(frozen=True)
class PortGeometry:
    """One port's camera, for vetting (module doc)."""

    shape: tuple[int, int] = (1216, 1936)
    plate_scale: float = 0.265
    fiber: tuple[float, float] | None = None
    #: Position angle of +y (deg E of N); None: unknown (any rotation).
    pa: float | None = None
    max_star_distance: float = 400.0
    capture_radius: float = 8.0
    exposure: float = 0.5
    seeing: float = 1.5
    sky: float = 21.0
    photometry: PortPhotometry = field(default_factory=PortPhotometry)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> PortGeometry:
        d = {k: v for k, v in dict(d).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"vetting port: unknown keys {sorted(set(d) - known)}")
        if "photometry" in d:
            d["photometry"] = PortPhotometry.from_dict(d["photometry"])
        if "shape" in d:
            d["shape"] = (int(d["shape"][0]), int(d["shape"][1]))
        if d.get("fiber") is not None:
            d["fiber"] = (float(d["fiber"][0]), float(d["fiber"][1]))
        return cls(**d)

    @property
    def target_pixel(self) -> tuple[float, float]:
        if self.fiber is not None:
            return self.fiber
        ny, nx = self.shape
        return (nx - 1) / 2.0, (ny - 1) / 2.0


@dataclass(frozen=True)
class VettingConfig:
    match_radius: float = 3.0
    companion_radius: float = 10.0
    companion_dmag: float = 2.5
    twin_dmag: float = 0.5
    blend_radius: float = 3.0
    brightest_margin: float = 0.5
    saturation_fraction: float = 0.8
    min_snr: float = 20.0
    min_solve_stars: int = 4
    #: Acquisition (docs/acquisition.md): a star counts as seen above this
    #: S/N; this many or more and the cascade plate-solves.
    acq_snr: float = 5.0
    acq_solve_min: int = 5
    high_pm: float = 500.0
    fiber_dmag: float = 5.0
    #: Flag codes that reject a request at intake (the rest warn).
    reject: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> VettingConfig:
        d = {k: v for k, v in dict(d).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"vetting: unknown keys {sorted(set(d) - known)}")
        if "reject" in d:
            bad = set(d["reject"]) - set(FLAGS)
            if bad:
                raise ValueError(f"vetting.reject: unknown flags {sorted(bad)}; known {FLAGS}")
            d["reject"] = tuple(d["reject"])
        return cls(**d)


@dataclass
class Flag:
    code: str
    message: str
    severity: str = "warning"
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class VettingResult:
    target: str
    port: str
    flags: list[Flag] = field(default_factory=list)
    #: The target's catalog entry (G, source id, separation), if found.
    match: dict[str, Any] | None = None
    #: Brightest-star acquisition would take the target.
    unambiguous: bool = False
    n_field: int = 0
    n_solve: int = 0
    #: Stars the acquisition frame should show (S/N > 5 at the port's
    #: exposure, in the acquisition box), and the branch of the AUTO cascade
    #: that count leads to (docs/acquisition.md).
    n_acq: int = 0
    acq_branch: str = ""

    @property
    def rejected(self) -> bool:
        return any(f.severity == "error" for f in self.flags)

    def codes(self) -> list[str]:
        return [f.code for f in self.flags]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "port": self.port,
            "rejected": self.rejected,
            "unambiguous": self.unambiguous,
            "match": self.match,
            "n_field": self.n_field,
            "n_solve": self.n_solve,
            "n_acq": self.n_acq,
            "acq_branch": self.acq_branch,
            "flags": [asdict(f) for f in self.flags],
        }


def _peak_fraction(fwhm_px: float) -> float:
    """Fraction of a Gaussian star's light in its brightest pixel."""
    s = max(fwhm_px, 0.5) / 2.3548
    edge = math.erf(0.5 / (math.sqrt(2) * s))
    return edge * edge


def vet(
    catalog: Catalog,
    name: str,
    ra: float,
    dec: float,
    port: PortGeometry,
    config: VettingConfig | None = None,
    *,
    port_name: str = "",
    when: datetime | float | None = None,
    pm: tuple[float, float] | None = None,
    epoch: float | None = None,
) -> VettingResult:
    """Vet one target (ICRS ``ra``/``dec`` deg at ``epoch``, default J2000;
    ``pm`` (pmra*, pmdec) mas/yr) for one port at ``when`` (module doc)."""
    c = config or VettingConfig()
    res = VettingResult(name, port_name)

    def flag(code: str, message: str, **detail: Any) -> None:
        sev = "error" if code in c.reject else "warning"
        res.flags.append(Flag(code, message, sev, detail))

    ep = decimal_year(when)
    if pm is not None and ep is not None:
        ra, dec = (
            float(v)
            for v in propagate(ra, dec, pm[0], pm[1], ep - (epoch if epoch is not None else 2000.0))
        )
    ny, nx = port.shape
    scale = port.plate_scale
    tx, ty = port.target_pixel
    # Everything the camera can see with the target on its target pixel.
    far = max(math.hypot(x - tx, y - ty) for x in (0, nx - 1) for y in (0, ny - 1))
    radius = far * scale / 3600.0 + 5.0 / 3600.0
    try:
        stars = catalog.cone(ra, dec, radius, epoch=when)
    except CatalogUnavailable as exc:
        flag("catalog_unavailable", f"catalog: {exc}")
        return res
    if not len(stars):
        flag("not_in_catalog", "no catalog stars in the field")
        return res
    d = separation(ra, dec, stars.ra, stars.dec) * 3600.0
    k = int(np.argmin(d))
    if d[k] > c.match_radius:
        flag("not_in_catalog", f"no catalog star within {c.match_radius:g} arcsec of the target")
        tmag = None
    else:
        tmag = float(stars.g[k])
        res.match = {
            "g": round(tmag, 3),
            "source_id": int(stars.source_id[k]),
            "source": str(stars.source[k]),
            "separation": round(float(d[k]), 3),
            "pm": [round(float(stars.pmra[k]), 2), round(float(stars.pmdec[k]), 2)],
        }
        tot = math.hypot(float(stars.pmra[k]), float(stars.pmdec[k]))
        if tot > c.high_pm:
            flag(
                "high_proper_motion",
                f"proper motion {tot:.0f} mas/yr: give coordinates with their epoch and pm",
                pm=tot,
            )
    others = np.arange(len(stars)) != k if tmag is not None else np.ones(len(stars), bool)
    # Companions (catalog) and pairs (WDS).
    if tmag is not None:
        dm = stars.g - tmag
        near = others & (d <= c.companion_radius) & (dm < c.companion_dmag)
        for j in np.nonzero(near)[0]:
            sep, dmag = float(d[j]), float(dm[j])
            info = {"separation": round(sep, 2), "dmag": round(dmag, 3), "g": float(stars.g[j])}
            if sep < c.blend_radius:
                flag("blend", f"star {sep:.1f} arcsec away, dG {dmag:+.2f}: blends", **info)
            elif abs(dmag) <= c.twin_dmag:
                flag("twin", f"near-twin {sep:.1f} arcsec away, dG {dmag:+.2f}", **info)
            else:
                flag("companion", f"star {sep:.1f} arcsec away, dG {dmag:+.2f}", **info)
    if catalog.doubles is not None:
        # A pair whose primary or secondary is the target (the WDS gives the
        # primary's position and the secondary's offset rho, theta).
        tol = max(c.match_radius, 10.0)  # WDS positions are rough
        for p in catalog.doubles.cone(ra, dec, (c.companion_radius + tol) / 3600.0):
            rho, theta = (
                float(p["rho"]),
                math.radians(float(p["theta"]) if p["theta"] == p["theta"] else 0.0),
            )
            if not 0 < rho <= c.companion_radius:
                continue
            if not abs(p["mag2"] - p["mag1"]) < c.companion_dmag:
                continue
            d1 = float(separation(ra, dec, p["ra"], p["dec"])) * 3600.0
            sra = p["ra"] + rho * math.sin(theta) / 3600.0 / max(math.cos(math.radians(dec)), 1e-6)
            sdec = p["dec"] + rho * math.cos(theta) / 3600.0
            d2 = float(separation(ra, dec, sra, sdec)) * 3600.0
            if min(d1, d2) > tol:
                continue
            flag(
                "wds_companion",
                f"WDS {p['wds']} {p['disc']} {p['comp']}: rho {rho:.1f} arcsec, "
                f"mags {p['mag1']:.2f}/{p['mag2']:.2f} ({p['last']})",
                rho=rho,
                dmag=float(p["mag2"] - p["mag1"]),
            )
    # Positions on the detector (tangent plane, pixels from the target).
    xi, eta = _tan(ra, dec, stars.ra, stars.dec)
    u = np.degrees(xi) * 3600.0 / scale
    v = np.degrees(eta) * 3600.0 / scale
    r_px = np.hypot(u, v)
    if port.pa is None:
        # Unknown rotation: count what is inside the inscribed circle.
        inside = r_px <= min(tx, ty, nx - 1 - tx, ny - 1 - ty)
    else:
        p = math.radians(port.pa)
        # Pixel offsets (fits_writer's normal parity): east is -x at PA 0.
        dx = -u * math.cos(p) + v * math.sin(p)
        dy = u * math.sin(p) + v * math.cos(p)
        inside = (tx + dx >= 0) & (tx + dx <= nx - 1) & (ty + dy >= 0) & (ty + dy <= ny - 1)
    res.n_field = int(inside.sum())
    phot = port.photometry
    sky_e = phot.sky_rate(port.sky, scale)
    fwhm_px = port.seeing / scale
    npix = math.pi * fwhm_px**2
    rates = 10.0 ** (-0.4 * (stars.g + phot.extinction - phot.zp))
    signal = rates * port.exposure
    noise = np.sqrt(signal + npix * ((sky_e + phot.dark) * port.exposure + phot.read_noise**2))
    snr = signal / noise
    res.n_solve = int(np.sum(inside & (snr > 10)))
    if res.n_solve < c.min_solve_stars:
        flag(
            "few_stars",
            f"{res.n_solve} stars with S/N > 10 in {port.exposure:g} s: too few to plate solve",
            n=res.n_solve,
        )
    # The acquisition search box around the target pixel.
    box = inside & (r_px <= port.max_star_distance)
    if tmag is not None:
        rivals = box & others & (stars.g <= tmag + c.brightest_margin)
        if rivals.any():
            j = int(np.nonzero(rivals)[0][np.argmin(stars.g[rivals])])
            flag(
                "not_brightest",
                f"a G {stars.g[j]:.2f} star {d[j]:.0f} arcsec away is as bright as the target "
                f"(G {tmag:.2f}) in the acquisition box",
                g=float(stars.g[j]),
                separation=round(float(d[j]), 1),
            )
        # Saturation and S/N of the target itself.
        peak = signal[k] * _peak_fraction(fwhm_px) + sky_e * port.exposure
        well = phot.saturation * phot.gain
        if peak > c.saturation_fraction * well:
            flag(
                "saturates",
                f"peak ~{peak:.0f} e- in {port.exposure:g} s, over "
                f"{c.saturation_fraction:.0%} of full well ({well:.0f} e-)",
                peak=float(peak),
            )
        if snr[k] < c.min_snr:
            flag("too_faint", f"S/N {snr[k]:.1f} in {port.exposure:g} s", snr=float(snr[k]))
        cap = port.capture_radius * scale
        contam = others & (d <= max(cap, 0.0)) & (stars.g - tmag < c.fiber_dmag)
        if contam.any():
            j = int(np.nonzero(contam)[0][0])
            flag(
                "fiber_contamination",
                f"a G {stars.g[j]:.2f} star {d[j]:.1f} arcsec from the target, inside the "
                f"capture radius ({cap:.1f} arcsec)",
            )
    # Which branch of the acquisition cascade the frame will take.
    visible = box & (snr > c.acq_snr)
    res.n_acq = n = int(visible.sum())
    res.acq_branch = (
        "solve"
        if n >= c.acq_solve_min
        else "triangulate"
        if n >= 3
        else "pair"
        if n == 2
        else "single"
        if n == 1
        else "none"
    )
    if res.acq_branch in ("pair", "triangulate"):
        flag(
            "acq_relies_on_chip_pa",
            f"{n} stars in the acquisition frame: acquisition relies on the chip PA"
            " (the port needs a PA calibration)",
            branch=res.acq_branch,
        )
    if tmag is not None and not snr[k] > c.acq_snr and n:
        flag(
            "acq_target_invisible",
            f"the target is below S/N {c.acq_snr:g} in {port.exposure:g} s but {n} other"
            " star(s) are not: acquisition would take one of them",
        )
    ambiguous = {"twin", "blend", "companion", "wds_companion", "not_brightest", "not_in_catalog"}
    res.unambiguous = tmag is not None and not (ambiguous & set(res.codes()))
    return res


# -- requests ------------------------------------------------------------------------

_CACHE: dict[str, Vetter] = {}


@dataclass
class Vetter:
    """Vets request groups for a site (``vetting`` in the dispatch plan).

    ``ports`` maps ``"<telescope>/<port>"`` or ``"<port>"`` to geometry; a
    SPECTRUM configuration is vetted for port ``fau``, anything else for
    its instrument configuration's ``port`` or ``imager``."""

    catalog: Catalog
    ports: dict[str, PortGeometry]
    config: VettingConfig = field(default_factory=VettingConfig)

    @classmethod
    def from_config(cls, d: Mapping[str, Any], base: Any = None) -> Vetter:
        """From a ``vetting`` config; cached by content (the request API
        rebuilds its scheduler view whenever the state file changes)."""
        import json

        key = json.dumps(d, sort_keys=True, default=str) + f"|{base}"
        hit = _CACHE.get(key)
        if hit is None:
            hit = _CACHE[key] = cls._build(d, base)
            while len(_CACHE) > 8:
                _CACHE.pop(next(iter(_CACHE)))
        return hit

    @classmethod
    def _build(cls, d: Mapping[str, Any], base: Any) -> Vetter:
        from robofast_core.catalog import catalog_from_config

        d = {k: v for k, v in dict(d).items() if not k.startswith("_")}
        catalog = catalog_from_config(d.pop("catalog", None), base=base)
        ports = {k: PortGeometry.from_dict(v) for k, v in (d.pop("ports", {}) or {}).items()}
        return cls(catalog, ports, VettingConfig.from_dict(d))

    def port(self, telescope: str | None, name: str) -> PortGeometry | None:
        if telescope is not None and f"{telescope}/{name}" in self.ports:
            return self.ports[f"{telescope}/{name}"]
        return self.ports.get(name)

    def vet_group(self, group: Any, when: datetime | None = None) -> list[VettingResult]:
        """Every (configuration, port, telescope) combination of a
        :class:`~robofast_core.request_model.RequestGroup`, once each."""
        out: list[VettingResult] = []
        seen: set[tuple] = set()
        for r in group.requests:
            tels = list(r.telescopes or [None])
            for conf in r.configurations:
                t = conf.target
                if t is None:
                    continue
                if conf.type == "SPECTRUM":
                    ports = ["fau"]
                else:
                    ports = sorted(
                        {ic.port or "imager" for ic in conf.instrument_configs if ic.light}
                    )
                for tel in tels:
                    for pname in ports:
                        geo = self.port(tel, pname)
                        key = (
                            t.name,
                            t.ra_hours,
                            t.dec_deg,
                            tel if geo is not self.ports.get(pname) else None,
                            pname,
                        )
                        if geo is None or key in seen:
                            continue
                        seen.add(key)
                        res = vet(
                            self.catalog,
                            t.name,
                            t.ra_hours * 15.0,
                            t.dec_deg,
                            geo,
                            self.config,
                            port_name=pname if tel is None else f"{tel}/{pname}",
                            when=when,
                            pm=(t.pm_ra, t.pm_dec)
                            if t.pm_ra is not None and t.pm_dec is not None
                            else None,
                            epoch=t.epoch,
                        )
                        res_cfg = getattr(conf, "id", None)
                        res.target = f"{t.name} ({res_cfg})" if res_cfg else t.name
                        out.append(res)
        return out
