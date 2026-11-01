"""Plate-solve every light frame as it is written (docs/sky.md).

:class:`FrameSolver` is :class:`robofast_core.fits_writer.FrameStore`'s
``solver``: for a light frame with the approximate (mount) WCS it solves
near the mount's pointing (:func:`robofast_core.astrometry.solve_near`),
and on success

* replaces the approximate WCS with the solved one (TAN, or TAN-SIP),
  ``WCSSRC = 'robofast'`` and the solve's provenance (``WCSRMS``,
  ``WCSNSTAR``, ``WCSPA``, ``WCSSCALE``, ...);
* measures the frame's **photometric zero point** from the matched stars
  (:func:`robofast_core.astrometry.zero_point`): ``ZPMAG`` (Gaia G of a
  source giving 1 e-/s, or 1 ADU/s with no gain), ``ZPERR``, ``ZPSCAT``,
  ``ZPNSTAR``, ``ZPUNIT``; the airmass is already ``AIRMASS``. The
  throughput monitor (:mod:`robofast_core.throughput`: mirrors, dew, a
  cover left closed) compares these with the port's expected zero point;
* returns a summary that goes into the nightly frame index (``solve``,
  with the frame's conditions in ``context`` for the throughput monitor,
  docs/throughput.md) and
  that :class:`robofast_core.imaging.Imaging` publishes to the value
  registry (``{tel}.image.zp``, ``{tel}.image.wcs_rms``, ...).

A failed solve leaves the approximate WCS (``WCSSRC = 'mount'``) and adds
``WCSFAIL`` with the reason; it never fails the frame.

Configuration (``images.solve`` in the enclosure config)::

    "solve": {
      "catalog": {"cache_dir": "/var/lib/robofast/catalog"},
      "telescopes": {"t1": {"plate_scale": 0.61, "search_radius": 0.2}}
    }

The plate scale defaults to the telescope's ``optics.plate_scale``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import numpy as np
from astropy.io import fits

from robofast_core.astrometry import SolveError, SolverConfig, solve_near, wcs_cards, zero_point
from robofast_core.catalog import Catalog, CatalogUnavailable, catalog_from_config, separation
from robofast_core.fits_writer import FrameInfo, replace_wcs
from robofast_core.throughput import header_context

log = logging.getLogger(__name__)

__all__ = ["FrameSolver"]


def _num(h: fits.Header, key: str) -> float | None:
    v = h.get(key)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class FrameSolver:
    """Solves frames for a :class:`~robofast_core.fits_writer.FrameStore`
    (module doc). ``configs`` per telescope; ``catalog`` shared."""

    def __init__(self, catalog: Catalog, configs: dict[str, SolverConfig]) -> None:
        self.catalog = catalog
        self.configs = configs

    @classmethod
    def from_config(
        cls, entry: dict[str, Any], optics: dict[str, dict[str, Any]], base: Any = None
    ) -> FrameSolver:
        entry = dict(entry)
        entry.pop("_note", None)
        known = {"catalog", "telescopes"}
        if set(entry) - known:
            raise ValueError(f"images.solve: unknown keys {sorted(set(entry) - known)}")
        catalog = catalog_from_config(entry.get("catalog"), base=base)
        configs: dict[str, SolverConfig] = {}
        names = set(entry.get("telescopes", {})) | set(optics)
        for name in names:
            d = dict(entry.get("telescopes", {}).get(name, {}))
            o = optics.get(name, {})
            d.setdefault("plate_scale", o.get("plate_scale"))
            if d.get("plate_scale") is None:
                continue
            if "flip" not in d and "flip" in o:
                d["flip"] = bool(o["flip"])
            configs[name] = SolverConfig.from_dict(d)
        return cls(catalog, configs)

    def __call__(self, data: np.ndarray, h: fits.Header, info: FrameInfo) -> dict[str, Any] | None:
        cfg = self.configs.get(info.telescope)
        if cfg is None or data.ndim != 2:
            return None
        if h.get("IMAGETYP") not in ("LIGHT", None):
            return None
        ra, dec = _num(h, "CRVAL1"), _num(h, "CRVAL2")
        if ra is None or dec is None:
            return None
        binning = int(_num(h, "XBINNING") or 1)
        epoch = _epoch(h)
        # The approximate WCS's reference pixel is the optical axis (full
        # chip centre); the guess is at that pixel, so solve with it there.
        ny, nx = data.shape
        crpix = (
            (_num(h, "CRPIX1") or (nx + 1) / 2.0) - 1.0,
            (_num(h, "CRPIX2") or (ny + 1) / 2.0) - 1.0,
        )
        try:
            sol = None
            pa, flip = _num(h, "PACALIB"), h.get("PAFLIP")
            if pa is not None and isinstance(flip, bool) and cfg.pa is None:
                # The port's PA calibration (docs/rotcal.md) as the hint; the
                # unhinted solve is the fallback if it is wrong.
                hinted = replace(cfg, pa=pa, pa_tolerance=5.0, flip=flip)
                try:
                    sol = solve_near(
                        data,
                        self.catalog,
                        ra,
                        dec,
                        hinted,
                        binning=binning,
                        epoch=epoch,
                        crpix=crpix,
                    )
                except SolveError:
                    sol = None
            if sol is None:
                sol = solve_near(
                    data, self.catalog, ra, dec, cfg, binning=binning, epoch=epoch, crpix=crpix
                )
        except (SolveError, CatalogUnavailable) as exc:
            h["WCSFAIL"] = (str(exc)[:68], "Plate solve failed: approximate WCS kept")
            return {"solved": False, "error": str(exc)}
        replace_wcs(h, wcs_cards(sol))
        out: dict[str, Any] = {"solved": True, **sol.summary()}
        # How far the mount's idea of the optical axis was from the truth.
        tra, tdec = sol.sky([crpix[0]], [crpix[1]])
        out["offset_arcsec"] = round(float(separation(ra, dec, tra[0], tdec[0])) * 3600.0, 2)
        # For the pointing model (robofast_core.pointmodel): where the optical
        # axis really pointed, where the mount said it was (MNTRA/MNTDEC
        # when robofast's model rewrote RA/DEC), the sidereal time at
        # mid-exposure, the port, and the requested target.
        out["axis_ra"], out["axis_dec"] = round(float(tra[0]), 7), round(float(tdec[0]), 7)
        mra, mdec = _num(h, "MNTRA"), _num(h, "MNTDEC")
        if mra is not None and mdec is not None:
            out["mount_ra"], out["mount_dec"] = round(mra * 15.0, 7), round(mdec, 7)
        else:
            out["mount_ra"], out["mount_dec"] = ra, dec
        lst = _num(h, "LST")
        if lst is not None:
            half = 0.5 * (_num(h, "EXPTIME") or 0.0) * 360.98564736629 / 86400.0
            out["lst"] = round((lst * 15.0 + half) % 360.0, 6)
        for key, kw in (("port", "PORT"), ("objra", "OBJRA"), ("objdec", "OBJDEC")):
            if h.get(kw) is not None:
                out[key] = h.get(kw)
        if h.get("DATE-OBS"):
            out["date_obs"] = str(h["DATE-OBS"])
        # The conditions, for the throughput monitor (robofast_core.throughput).
        ctx = header_context(h)
        ctx["sky_adu"] = round(float(np.median(data)), 1)  # raw: bias included
        out["context"] = ctx
        exptime = _num(h, "EXPTIME") or 0.0
        gain = _num(h, "EGAIN")
        sat = _num(h, "MAXADU") or _num(h, "SATURATE")
        zp = zero_point(
            data,
            sol,
            exptime=exptime,
            gain=gain,
            saturation=0.95 * sat if sat else None,
        )
        if zp is not None:
            h["ZPMAG"] = (round(zp["zp"], 4), f"[mag] G giving 1 {zp['unit']} (aperture)")
            h["ZPERR"] = (round(zp["error"], 4), "[mag] error of the median zero point")
            h["ZPSCAT"] = (round(zp["scatter"], 4), "[mag] robust star-to-star scatter")
            h["ZPNSTAR"] = (zp["n"], "Stars in the zero point")
            h["ZPUNIT"] = (zp["unit"], "Zero point counts unit")
            out["zp"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in zp.items()}
            am = _num(h, "AIRMASS")
            if am is not None:
                out["zp"]["airmass"] = am
            if exptime:
                out["zp"]["exptime"] = exptime
        return out


def _epoch(h: fits.Header) -> datetime | None:
    v = h.get("DATE-OBS")
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None
