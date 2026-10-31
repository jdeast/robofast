"""Spectrum metadata: the barycentric correction and the sidecar files.

A spectrum's raw FITS file is written by the spectrograph PC (KiwiSpec's SI
Image camera; :mod:`robofast_core.devices.kiwispec_service`) and robofast
never rewrites it (docs/kiwispec.md, open question 10; docs/data.md: no file
is modified after it is written). What robofast knows about the spectrum and
the camera does not -- which telescopes fed it, how well each guided, the
exposure meter's flux-weighted midpoint, the target's astrometry, the site,
and the barycentric correction at that midpoint -- goes into a **sidecar**:
one JSON file per raw frame, in robofast's data tree::

    <data_root>/<site>/<spectrograph>/n<YYYYMMDD>/<raw file name>.json
    <data_root>/<site>/<spectrograph>/n<YYYYMMDD>/n<YYYYMMDD>.<spectrograph>.spectra.jsonl

The night is the local date of the evening (as for frames, docs/data.md),
by the sequencer's clock when the exposure was asked for (the record's
``night``; :mod:`robofast_core.clock`), the same night that names the raw
file on the spectrograph PC.
The index gets one line per sidecar. Sidecars are written atomically and
never overwritten (a second record for the same raw file gets ``.1``,
``.2``, ... before ``.json``).

The barycentric correction
--------------------------
:func:`barycentric` computes, with astropy, at the flux-weighted midpoint
(or, without one, the middle of the exposure; ``midpoint_source`` says
which):

* ``bjd_tdb`` -- the barycentric Julian date (TDB) of the midpoint: the
  midpoint in TDB plus the light travel time from the observatory to the
  solar-system barycentre along the target's direction;
* ``barycentric_correction`` -- the barycentric radial-velocity correction
  in m/s (astropy's ``SkyCoord.radial_velocity_correction``: add it to a
  measured velocity), with the site's position and velocity included.

Proper motion, when given, moves the coordinates from their ``epoch`` to
the midpoint linearly in RA and Dec (``pm_ra`` is mu_alpha cos(dec)); the
records keep both the catalog and the propagated position. Parallax and the
target's own radial velocity are recorded but not used: their effect on the
correction is far below a m/s for MINERVA's targets. IERS tables are never
downloaded (:func:`robofast_core.sky.offline_iers`); the Earth's position
comes from astropy's built-in ephemeris.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robofast_core.clock import real_clock
from robofast_core.plan_record import night_of

if TYPE_CHECKING:  # astropy is imported only when a spectrum is recorded
    from robofast_core import sky

__all__ = ["SpectrumRecorder", "barycentric", "propagate"]

log = logging.getLogger(__name__)

_NIGHT = re.compile(r"n\d{8}")

#: Julian years per day.
_YEAR_DAYS = 365.25


def propagate(target: dict[str, Any], when: datetime) -> tuple[float, float]:
    """The target's (RA hours, Dec degrees) at ``when``: its catalog position
    moved by its proper motion (if any) from ``epoch`` (default J2000)."""
    ra, dec = float(target["ra_hours"]), float(target["dec_deg"])
    pm_ra, pm_dec = target.get("pm_ra"), target.get("pm_dec")
    if pm_ra is None and pm_dec is None:
        return ra, dec
    from astropy.time import Time

    epoch = float(target.get("epoch") or 2000.0)
    years = Time(when).jyear - epoch
    ddec = (pm_dec or 0.0) * years / 3.6e6  # mas -> deg
    cosd = math.cos(math.radians(dec))
    dra_deg = (pm_ra or 0.0) * years / 3.6e6 / max(cosd, 1e-9)
    return (ra + dra_deg / 15.0) % 24.0, max(-90.0, min(90.0, dec + ddec))


def barycentric(target: dict[str, Any], site: sky.Site, when: datetime) -> dict[str, Any]:
    """BJD_TDB and the barycentric RV correction (m/s) of ``target`` seen
    from ``site`` at ``when`` (aware UTC). See the module doc."""
    if when.tzinfo is None:
        raise ValueError("barycentric needs an aware datetime")
    import astropy
    import astropy.units as u
    from astropy.coordinates import SkyCoord
    from astropy.time import Time

    from robofast_core import sky as _sky

    _sky.offline_iers()
    ra, dec = propagate(target, when)
    location = site.location
    t = Time(when.astimezone(UTC), scale="utc", location=location)
    coord = SkyCoord(ra=ra * 15.0 * u.deg, dec=dec * u.deg, frame="icrs")
    ltt = t.light_travel_time(coord, kind="barycentric")
    bjd = t.tdb + ltt
    corr = coord.radial_velocity_correction(
        kind="barycentric", obstime=Time(when.astimezone(UTC), scale="utc"), location=location
    )
    return {
        "midpoint_utc": when.astimezone(UTC).isoformat(),
        "jd_utc": float(t.utc.jd),
        "bjd_tdb": float(bjd.jd),
        "light_travel_time_s": float(ltt.to_value(u.s)),
        "barycentric_correction": float(corr.to_value(u.m / u.s)),
        "barycentric_correction_unit": "m/s",
        "ra_hours": ra,
        "dec_deg": dec,
        "ephemeris": "builtin",
        "software": f"astropy {astropy.__version__}",
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


@dataclass
class SpectrumRecorder:
    """Writes spectrum sidecars and the nightly index (module doc)."""

    data_root: Path
    site_name: str
    site: sky.Site
    instrument: str = "kiwispec"
    night_rollover_hour: float = 12.0
    timezone: str | None = None
    #: The sequencer's clock (:mod:`robofast_core.clock`): the night when a
    #: record does not name one, and ``written``.
    clock: Callable[[], datetime] = real_clock

    def __post_init__(self) -> None:
        self.data_root = Path(self.data_root)

    def night(self, when: datetime) -> str:
        d = night_of(
            when,
            rollover_hour=self.night_rollover_hour,
            timezone=self.timezone,
            longitude=self.site.longitude,
        )
        return "n" + d.strftime("%Y%m%d")

    def directory(self, when: datetime) -> Path:
        return self.data_root / self.site_name / self.instrument / self.night(when)

    def correct(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """The barycentric block for a science record (None for a
        calibration, or when it cannot be computed: logged, and recorded in
        ``record["warnings"]``)."""
        target = record.get("target")
        if not target or record.get("kind") != "science":
            return None
        mid = record.get("flux_midpoint_utc")
        source = "expmeter"
        if mid is None:
            start = record.get("start_utc")
            if start is None:
                return None
            span = float(record.get("exptime") or 0.0)
            mid_dt = datetime.fromisoformat(start).timestamp() + span / 2.0
            mid = datetime.fromtimestamp(mid_dt, UTC).isoformat()
            source = "geometric"
        try:
            out = barycentric(target, self.site, datetime.fromisoformat(mid))
        except Exception as exc:
            log.exception("barycentric correction for %s", record.get("file"))
            record.setdefault("warnings", []).append(f"barycentric correction failed: {exc!r}")
            return None
        out["midpoint_source"] = source
        return out

    def write(self, record: dict[str, Any]) -> Path:
        """Write ``record`` (a spectrum's metadata; must have ``file``, the
        raw file name) as a sidecar, add it to the index, return the
        sidecar's path relative to ``data_root``. Never overwrites. The
        night is ``record["night"]`` (``n<YYYYMMDD>``, set by the sequencer
        from its clock when the exposure was asked for), else the night of
        ``start_utc``, else of the clock now."""
        raw = str(record.get("file") or "unknown")
        night = record.get("night")
        if not (isinstance(night, str) and _NIGHT.fullmatch(night)):
            when = record.get("start_utc")
            night = self.night(datetime.fromisoformat(when) if when else self.clock())
        d = self.data_root / self.site_name / self.instrument / night
        d.mkdir(parents=True, exist_ok=True)
        stem = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        data = json.dumps(_clean(record), indent=1, default=_json_default, allow_nan=False)
        n = 0
        while True:
            name = f"{stem}.json" if n == 0 else f"{stem}.{n}.json"
            final = d / name
            tmp = d / f".{name}.tmp{os.getpid()}"
            with open(tmp, "w") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, final)
            except FileExistsError:
                n += 1
                continue
            finally:
                tmp.unlink(missing_ok=True)
            break
        _fsync_dir(d)
        rel = final.relative_to(self.data_root)
        index = d / f"{night}.{self.instrument}.spectra.jsonl"
        line = {
            "sidecar": str(rel),
            "file": raw,
            "kind": record.get("kind"),
            "object": record.get("object"),
            "telescopes": record.get("telescopes"),
            "start_utc": record.get("start_utc"),
            "exptime": record.get("exptime"),
            "bjd_tdb": (record.get("barycentric") or {}).get("bjd_tdb"),
            "written": self.clock().isoformat(),
        }
        with open(index, "a") as f:
            f.write(json.dumps(_clean(line), default=_json_default) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return rel


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
