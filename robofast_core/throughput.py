"""Telescope throughput over time: mirrors, dew, a cover left on (docs/throughput.md).

Every plate-solved frame carries a photometric zero point (``ZPMAG``, the
Gaia G magnitude giving 1 e-/s or 1 ADU/s: :mod:`robofast_core.framesolve`).
This module keeps them, with their context, and watches the telescope's
efficiency drift:

1. **Record** -- :class:`ThroughputStore`, an append-only JSON-lines file:
   one ``zp`` line per frame (telescope, port, filter, zero point, airmass,
   exposure, date, temperatures, sky, Moon, ...), plus ``event`` lines a
   person adds (a mirror cleaning, a recoat: :func:`annotate`). Frames come
   from the nightly frame index (``*.frames.jsonl``: :func:`ingest_night`)
   or from FITS files already on disk (``backfill``, solving them if their
   header has no ``ZPMAG``).
2. **Correct** -- above the atmosphere, ``zp0 = zp + k X`` with ``k`` per
   site and band (configured, or fitted from steady nights:
   :func:`fit_extinction`); against the zero point the port's optics
   predict (:class:`robofast_core.photometry.PortPhotometry`, its efficiency
   chain), ``efficiency = 10**(0.4 (zp0 - expected))``.
3. **Envelope** -- clouds only ever take light away, so a night is its
   ``percentile``-th percentile (default 80), not its mean, and the trend
   is fitted to the nights' upper envelope: a line over the last
   ``window_days`` with nights more than ``cloud_clip`` below it rejected
   (one-sided, iterated) as cloudy.
4. **Baseline, steps, alarms** -- per telescope + port + filter
   (a *series*). The baseline is the first ``baseline_nights`` clear nights
   after the series starts or is reset. The current level is the trend at
   the latest night; an **alarm** when it is ``alarm_drop`` (10 %) below
   the baseline. A **step up** (``step_nights`` nights all ``step_up``
   above the trend: a cleaning, a recoat) resets the baseline and is
   annotated; so does a person's ``event``. A **step down**
   (``step_down_nights`` steady nights all ``step_down`` below it: dew, a
   cover, a misaligned beamsplitter) alarms at once and restarts the trend.

:func:`analyze` is a pure function of the stored lines, run forward night
by night, so it gives the same answer at any time (and a backfill shows
when an alarm *would* have fired). :func:`update` ingests a night, analyzes
and writes the **status file** that the observer console (and so the
notifier) reads (``throughput`` in the console's enclosure entry); the
morning report (``--throughput``) adds a section and QA lines.

``python -m robofast_core.throughput update|backfill|annotate|status|extinction``.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

__all__ = [
    "ThroughputConfig",
    "ThroughputStore",
    "analyze",
    "annotate",
    "fit_extinction",
    "header_context",
    "ingest_night",
    "main",
    "record_from_header",
    "record_from_index",
    "series_key",
    "update",
]

#: Header keywords kept as a frame's context: (context key, keywords in order
#: of preference). robofast's own and the legacy MINERVA ones.
CONTEXT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("temperature", ("OUTTEMP", "AMBTEMP", "TEMPERAT", "WXTEMP")),
    ("m1_temp", ("M1TEMP",)),
    ("focus_temp", ("FOCTEMP",)),
    ("ccd_temp", ("CCD-TEMP", "CCDTEMP", "CCD_TEMP")),
    ("humidity", ("HUMIDITY", "OUTHUMID")),
    ("dewpoint", ("DEWPOINT",)),
    ("cloud", ("MINCLOUD", "SKYTEMP", "ACLOUD")),
    ("moon_sep", ("MOONSEP", "MOONDIS")),
    ("moon_phase", ("MOONPHAS",)),
    ("sun_alt", ("SUNALT",)),
    ("fwhm", ("FWHM", "SEEING")),
    ("focus", ("FOCPOS",)),
)

_NIGHT = re.compile(r"n(\d{8})")


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def header_context(h: Mapping[str, Any]) -> dict[str, float]:
    """The frame's conditions from its header (``CONTEXT_KEYWORDS``):
    numbers only; ``UNKNOWN`` and stale (undefined) cards are left out."""
    out: dict[str, float] = {}
    for key, kws in CONTEXT_KEYWORDS:
        for kw in kws:
            v = _num(h.get(kw)) if kw in h else None
            if v is not None:
                out[key] = round(v, 4)
                break
    return out


def series_key(telescope: str, port: str | None, filt: str | None) -> str:
    """``<telescope>/<port>/<filter>``, ``-`` for none (``t2/fau/-``)."""
    return f"{str(telescope).lower()}/{port or '-'}/{filt or '-'}"


def _night_str(n: Any) -> str:
    if isinstance(n, date):
        return f"n{n:%Y%m%d}"
    s = str(n)
    m = _NIGHT.search(s)
    if m:
        return f"n{m.group(1)}"
    if re.fullmatch(r"\d{4}-\d\d-\d\d", s):
        return "n" + s.replace("-", "")
    raise ValueError(f"not a night: {n!r} (nYYYYMMDD or YYYY-MM-DD)")


def _night_mjd(night: str) -> float:
    d = datetime.strptime(night[1:], "%Y%m%d").replace(tzinfo=UTC)
    return d.timestamp() / 86400.0 + 40587.0


def _mag(fraction: float) -> float:
    """A fractional loss of light as magnitudes (0.10 -> 0.114)."""
    return -2.5 * math.log10(1.0 - fraction)


def _eff(zp0: float, expected: float) -> float:
    """Efficiency against the optics' prediction."""
    return round(10.0 ** (0.4 * (zp0 - expected)), 4)


def _frac(dmag: float) -> float:
    """Magnitudes (positive: brighter) as a fractional change of light."""
    return 10.0 ** (0.4 * dmag) - 1.0


# -- the store --------------------------------------------------------------------------


class ThroughputStore:
    """Append-only JSON lines (module doc): ``{"kind": "zp", ...}`` per frame
    (unique by ``key``: the file, or telescope and DATE-OBS) and
    ``{"kind": "event", ...}`` annotations. Never rewritten; a line that
    does not parse is skipped."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def read(self) -> list[dict[str, Any]]:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return []
        out = []
        for line in text.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
        return out

    def append(self, records: Iterable[dict[str, Any]]) -> int:
        """Append ``records`` (zero points already stored are skipped):
        how many were written."""
        records = list(records)
        if not records:
            return 0
        have = {r.get("key") for r in self.read() if r.get("kind") == "zp"}
        lines = []
        for r in records:
            if r.get("kind") == "zp":
                if r.get("key") in have:
                    continue
                have.add(r.get("key"))
            lines.append(json.dumps(r, default=str, sort_keys=True))
        if not lines:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write("".join(line + "\n" for line in lines))
            f.flush()
            os.fsync(f.fileno())
        return len(lines)


def _zp_record(
    *,
    key: str,
    night: str,
    t: str | None,
    telescope: str,
    port: str | None,
    filt: str | None,
    zp: Mapping[str, Any],
    airmass: float | None,
    exptime: float | None,
    file: str | None,
    context: Mapping[str, Any] | None,
    source: str,
) -> dict[str, Any]:
    r: dict[str, Any] = {
        "kind": "zp",
        "key": key,
        "night": night,
        "t": t,
        "telescope": str(telescope).lower(),
        "port": port,
        "filter": filt,
        "zp": round(float(zp["zp"]), 4),
        "unit": zp.get("unit", "e-/s"),
        "airmass": None if airmass is None else round(float(airmass), 4),
        "exptime": exptime,
        "source": source,
    }
    for k, name in (("error", "zp_err"), ("scatter", "zp_scatter"), ("n", "n_stars")):
        if zp.get(k) is not None:
            r[name] = zp[k]
    if file:
        r["file"] = file
    if context:
        r["context"] = dict(context)
    return r


def record_from_index(
    rec: Mapping[str, Any], night: str, *, default_port: str | None = None
) -> dict[str, Any] | None:
    """A frame-index line (``*.frames.jsonl``, docs/data.md) with a zero point
    as a store record; None without one."""
    s = rec.get("solve") or {}
    zp = s.get("zp") or {}
    if not s.get("solved") or zp.get("zp") is None:
        return None
    tel = rec.get("telescope")
    if not tel:
        return None
    key = str(rec.get("file") or f"{tel}@{rec.get('date_obs')}")
    return _zp_record(
        key=key,
        night=night,
        t=rec.get("date_obs") or s.get("date_obs"),
        telescope=str(tel),
        port=s.get("port") or default_port,
        filt=rec.get("filter"),
        zp=zp,
        airmass=zp.get("airmass"),
        exptime=_num(rec.get("exptime")),
        file=rec.get("file"),
        context=s.get("context"),
        source="index",
    )


def record_from_header(
    h: Mapping[str, Any],
    zp: Mapping[str, Any],
    *,
    file: str,
    night: str | None = None,
    telescope: str | None = None,
    port: str | None = None,
    filt: str | None = None,
    port_map: Mapping[str, Mapping[str, int]] | None = None,
    longitude: float | None = None,
    source: str = "fits",
) -> dict[str, Any]:
    """A FITS header (robofast's or the legacy MINERVA one) and its zero point
    as a store record. The telescope from ``TELESCOP``; the port from
    ``PORT`` (a name; a legacy port *number* through ``port_map``,
    ``{telescope: {port name: number}}`` as in instruments.json, or ``FAU``
    in the file name); the night from the file name (``nYYYYMMDD``), else
    ``NIGHT``, else from DATE-OBS (local mean time at ``longitude``, UTC
    without it, rolling over at noon)."""
    tel = str(telescope or h.get("TELESCOP") or "").strip().lower()
    if not tel:
        raise ValueError(f"{file}: no telescope (TELESCOP or --telescope)")
    if port is None:
        p = h.get("PORT")
        if isinstance(p, str) and p.strip() and not p.strip().isdigit():
            port = p.strip()
        elif p is not None and port_map and tel in port_map:
            inv = {int(v): k for k, v in port_map[tel].items()}
            port = inv.get(int(p))
        if port is None and ".FAU." in Path(file).name.upper():
            port = "fau"
    if filt is None:
        f = h.get("FILTER")
        filt = str(f).strip() if isinstance(f, str) and f.strip() else None
    t = str(h.get("DATE-OBS")) if h.get("DATE-OBS") else None
    if night is None:
        m = _NIGHT.search(Path(file).name)
        if m:
            night = f"n{m.group(1)}"
        elif h.get("NIGHT"):
            night = _night_str(h["NIGHT"])
        elif t:
            from robofast_core.plan_record import night_of

            d = datetime.fromisoformat(t.replace("Z", "+00:00"))
            d = d if d.tzinfo else d.replace(tzinfo=UTC)
            night = f"n{night_of(d, longitude=longitude):%Y%m%d}"
        else:
            raise ValueError(f"{file}: no night (file name, NIGHT or DATE-OBS)")
    ctx = header_context(h)
    return _zp_record(
        key=file,
        night=night,
        t=t,
        telescope=tel,
        port=port,
        filt=filt,
        zp=zp,
        airmass=_num(h.get("AIRMASS")),
        exptime=_num(h.get("EXPTIME")),
        file=file,
        context=ctx,
        source=source,
    )


def ingest_night(
    store: ThroughputStore,
    data_root: str | Path,
    night: str,
    *,
    default_port: str | Mapping[str, str] | None = None,
) -> int:
    """Every zero point in the night's frame indexes under ``data_root``
    (``<site>/<telescope>/<night>/*.frames.jsonl``) into the store: how many
    were new."""
    night = _night_str(night)
    recs = []
    for p in sorted(Path(data_root).rglob(f"{night}/*.frames.jsonl")):
        try:
            lines = p.read_text().splitlines()
        except OSError as exc:
            log.warning("%s: %s", p, exc)
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            tel = str(rec.get("telescope") or "").lower()
            dp = default_port.get(tel) if isinstance(default_port, Mapping) else default_port
            r = record_from_index(rec, night, default_port=dp)
            if r is not None:
                recs.append(r)
    return store.append(recs)


def annotate(
    store: ThroughputStore,
    *,
    telescope: str,
    night: str,
    what: str,
    note: str = "",
    port: str | None = None,
    filt: str | None = None,
    reset: bool = True,
    baseline: float | None = None,
    who: str | None = None,
) -> dict[str, Any]:
    """Record an event (a cleaning, a recoat, a camera change). ``reset``:
    the baseline starts again with the first night *on or after* ``night``
    (``baseline``, a zero point above the atmosphere, sets it outright)."""
    e: dict[str, Any] = {
        "kind": "event",
        "telescope": telescope.lower(),
        "port": port,
        "filter": filt,
        "night": _night_str(night),
        "what": what,
        "note": note,
        "reset": bool(reset),
        "added": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if baseline is not None:
        e["baseline"] = float(baseline)
    if who:
        e["who"] = who
    store.append([e])
    return e


# -- configuration ----------------------------------------------------------------------


@dataclass
class ThroughputConfig:
    """How the monitor reads, corrects and judges (docs/throughput.md)."""

    #: The store (JSON lines) and the status file the console reads.
    store: Path = Path("throughput.jsonl")
    status: Path | None = None
    #: Port photometry, ``<telescope>/<port>`` or ``<port>``: the expected
    #: zero point and the default extinction (the vetting config's ports).
    ports: dict[str, Any] = field(default_factory=dict)
    #: The expected zero point: ``"chain"`` (the efficiency chain alone) or
    #: ``"zero_point"`` (a measured zero point if the port has one).
    expected: str = "chain"
    #: A frame with no port: this port (or ``{telescope: port}``).
    default_port: Any = None
    #: Extinction (mag/airmass) by series, ``<telescope>/<port>``, filter or
    #: ``default``; else the port photometry's ``extinction``.
    extinction: dict[str, float] = field(default_factory=dict)
    #: Fit ``k`` per series from steady nights when enough (else configured).
    fit_extinction: bool = False
    #: A frame is used with at least this many stars and at most this scatter.
    min_stars: int = 3
    max_scatter: float = 0.5
    #: A night counts with at least ``min_frames``; its value is this
    #: percentile of its frames (clouds only take light away).
    min_frames: int = 3
    percentile: float = 80.0
    #: A night is *steady* (no patchy cloud) when its 10-90 % spread is below this (mag).
    steady_spread: float = 0.15
    #: The trend: a line over this many days of nights, nights more than
    #: ``cloud_clip`` (mag; or 2.5 sigma, the larger) below it rejected.
    window_days: float = 60.0
    cloud_clip: float = 0.05
    #: Clear nights needed before steps are looked for (and a slope fitted:
    #: over at least ``slope_days``; a level only before).
    min_nights: int = 5
    slope_days: float = 20.0
    #: The baseline: the trend of the first this many days after a (re)start,
    #: taken back to its first night (no alarm before).
    baseline_days: float = 30.0
    #: Fixed baselines (zero point above the atmosphere) by series.
    baselines: dict[str, float] = field(default_factory=dict)
    #: Alarm: the level this fraction below the baseline, ``alarm_nights``
    #: nights in a row (nights with data).
    alarm_drop: float = 0.10
    alarm_nights: int = 3
    #: Step up (resets the baseline): this many nights all this fraction above the trend.
    step_up: float = 0.05
    step_nights: int = 2
    #: How many steady nights back a step up may have started (cirrus between).
    step_lookback: int = 8
    #: Step down (alarms): this many *steady* nights all this fraction below
    #: the trend, and within 1.5 ``cloud_clip`` of each other.
    step_down: float = 0.10
    step_down_nights: int = 4
    #: Site longitude (deg east) for the night of a FITS file with no night in its name.
    longitude: float | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *, base: Path | None = None) -> ThroughputConfig:
        d = {k: v for k, v in dict(d).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__) | {"sky"}
        if set(d) - known:
            raise ValueError(f"throughput: unknown keys {sorted(set(d) - known)}")
        base = base or Path(".")

        def path(p: Any) -> Path:
            q = Path(p)
            return q if q.is_absolute() else base / q

        ports = dict(d.pop("ports", {}) or {})
        sky = d.pop("sky", None)
        if sky is not None:
            sky_path = path(sky)
            sky_ports = json.loads(sky_path.read_text()).get("ports", {})
            ports = {**sky_ports, **ports}
        from robofast_core.photometry import PortPhotometry

        phot = {}
        for k, v in ports.items():
            if k.startswith("_"):
                continue
            v = v.get("photometry", v) if isinstance(v, Mapping) else v
            phot[k] = v if isinstance(v, PortPhotometry) else PortPhotometry.from_dict(v)
        d["ports"] = phot
        if "store" in d:
            d["store"] = path(d["store"])
        if d.get("status") is not None:
            d["status"] = path(d["status"])
        cfg = cls(**d)
        if cfg.expected not in ("chain", "zero_point"):
            raise ValueError("throughput: expected is 'chain' or 'zero_point'")
        for name in ("alarm_drop", "step_up", "step_down"):
            if not 0 < getattr(cfg, name) < 1:
                raise ValueError(f"throughput: {name} is a fraction in (0, 1)")
        if not 0 < cfg.percentile <= 100:
            raise ValueError("throughput: percentile in (0, 100]")
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> ThroughputConfig:
        from robofast_core.config import load_config

        p = Path(path)
        return cls.from_dict(load_config(p), base=p.parent)

    def port_photometry(self, telescope: str, port: str | None) -> Any:
        if port is None:
            return None
        return self.ports.get(f"{telescope}/{port}") or self.ports.get(port)

    def expected_zp(self, telescope: str, port: str | None, unit: str) -> float | None:
        """The zero point the optics predict above the atmosphere, in
        ``unit`` (e-/s, or ADU/s through the port's ``gain``)."""
        p = self.port_photometry(telescope, port)
        if p is None:
            return None
        if self.expected == "zero_point" and p.zero_point is not None:
            zp, zunit = p.zero_point, p.zero_point_unit
        else:
            zp, zunit = p.zp_estimated, "e-/s"
        if zunit == unit:
            return zp
        g = 2.5 * math.log10(p.gain or 1.0)  # e- per ADU
        return zp - g if unit == "ADU/s" else zp + g

    def extinction_for(self, key: str) -> float:
        tel, port, filt = key.split("/")
        for k in (key, f"{tel}/{port}", filt, "default"):
            if k in self.extinction:
                return float(self.extinction[k])
        p = self.port_photometry(tel, None if port == "-" else port)
        return float(p.extinction) if p is not None else 0.15


# -- analysis ---------------------------------------------------------------------------


def _usable(r: Mapping[str, Any], cfg: ThroughputConfig) -> bool:
    if r.get("kind") != "zp" or _num(r.get("zp")) is None or _num(r.get("airmass")) is None:
        return False
    if r.get("n_stars") is not None and int(r["n_stars"]) < cfg.min_stars:
        return False
    sc = _num(r.get("zp_scatter"))
    return not (sc is not None and sc > cfg.max_scatter)


def _by_series(records: Iterable[Mapping[str, Any]], cfg: ThroughputConfig) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for r in records:
        if _usable(r, cfg):
            out[series_key(r["telescope"], r.get("port"), r.get("filter"))].append(r)
    return out


def fit_extinction(
    frames: Sequence[Mapping[str, Any]],
    *,
    min_frames: int = 5,
    min_range: float = 0.3,
    steady_spread: float = 0.1,
) -> dict[str, Any] | None:
    """The extinction coefficient (mag/airmass) from one series' frames:
    nights with at least ``min_frames`` over ``min_range`` in airmass whose
    residuals about their own line are steady (``steady_spread``, 10-90 %:
    no patchy cloud) share one slope (each night its own intercept). None
    with no such night."""
    nights: dict[str, list] = defaultdict(list)
    for f in frames:
        nights[f["night"]].append((float(f["airmass"]), float(f["zp"])))
    xs, ys, used = [], [], []
    for n, pts in nights.items():
        if len(pts) < min_frames:
            continue
        x, y = np.array(pts).T
        if np.ptp(x) < min_range:
            continue
        a, b = np.polyfit(x, y, 1)
        r = y - (a * x + b)
        if np.percentile(r, 90) - np.percentile(r, 10) > steady_spread:
            continue
        xs.append(x - x.mean())
        ys.append(y - y.mean())
        used.append(n)
    if not used:
        return None
    x, y = np.concatenate(xs), np.concatenate(ys)
    slope = float(np.sum(x * y) / np.sum(x * x))
    resid = y - slope * x
    dof = max(len(x) - len(used) - 1, 1)
    err = float(math.sqrt(np.sum(resid**2) / dof / np.sum(x * x)))
    return {"k": round(-slope, 4), "error": round(err, 4), "nights": len(used), "frames": len(x)}


def _fit(t: np.ndarray, y: np.ndarray, cfg: ThroughputConfig) -> tuple[float, float, np.ndarray]:
    """The upper envelope of nights (module doc): (level at t[-1], slope
    mag/day, kept mask). One-sided clipping: clouds are below."""
    n = len(y)
    if n == 0:
        raise ValueError("no nights")
    # Start from the nights near the top; then keep every night not more than the
    # clip below the line. The night-to-night noise is measured above the
    # line only (clouds are all below it).
    top = np.sort(y)[-2] if n >= 2 else y[0]  # the second best: one wild night is not the top
    keep = y >= top - 2.0 * cfg.cloud_clip
    slope, level = 0.0, float(np.max(y))
    for _ in range(20):
        tk, yk = t[keep], y[keep]
        if keep.sum() >= cfg.min_nights and np.ptp(tk) >= cfg.slope_days:
            a, b = np.polyfit(tk - t[-1], yk, 1)
            slope, level = float(a), float(b)
        else:
            slope, level = 0.0, float(np.median(yk))
        r = y - (level + slope * (t - t[-1]))
        above = r[keep & (r > 0)]
        s = 1.4826 * float(np.median(above)) if len(above) >= 5 else 0.0
        # 2.5 sigma of the clear nights' scatter, at most cloud_clip (uniform
        # cirrus a few per cent deep is then not taken for a clear night).
        thr = min(cfg.cloud_clip, max(2.5 * s, 0.02)) if s > 0 else cfg.cloud_clip
        new = r > -thr
        if new.sum() < max(1, int(0.3 * n)):  # mostly clouds: the top 30 %
            new = r >= np.sort(r)[-max(1, int(0.3 * n))]
        if np.array_equal(new, keep):
            break
        keep = new
    return level, slope, keep


def _extrapolate(level: float, slope: float, dt: np.ndarray, cfg: ThroughputConfig) -> np.ndarray:
    """A trend carried forward, its slope for at most ``slope_days`` (a
    long shutdown is not extrapolated)."""
    return level + slope * np.minimum(dt, cfg.slope_days)


def _prior(
    t: np.ndarray, y: np.ndarray, trend: int, lo: int, cfg: ThroughputConfig
) -> tuple[float, float, float] | None:
    """The trend of the nights before ``lo`` (its last ``window_days`` of
    data, across a shutdown too): (level, slope, its time), or None without
    ``min_nights`` clear nights to step from."""
    w = t[trend:lo] >= t[lo - 1] - cfg.window_days
    pt, py = t[trend:lo][w], y[trend:lo][w]
    if len(pt) < 3:
        return None
    lv, sl, kept = _fit(pt, py, cfg)
    return (lv, sl, float(pt[-1])) if kept.sum() >= cfg.min_nights else None


def _step_up(
    t: np.ndarray,
    y: np.ndarray,
    steady: np.ndarray,
    trend: int,
    up_mag: float,
    cfg: ThroughputConfig,
) -> tuple[int, np.ndarray, float] | None:
    """A step up ending tonight (``steady``: tonight's and the earlier steady
    nights' indexes): the earliest of the last ``step_lookback`` steady
    nights from which ``step_nights`` steady nights, tonight among them, are
    ``up_mag`` above the trend before it. Clouds only take light away, so a
    cirrus night after a cleaning does not count against it. (first night,
    the nights above, the gain) or None."""
    if len(steady) < cfg.step_nights:
        return None
    for lo in steady[-cfg.step_lookback :]:
        lo = int(lo)
        if lo - trend < cfg.min_nights:
            continue
        pr = _prior(t, y, trend, lo, cfg)
        if pr is None:
            continue
        sel = steady[steady >= lo]
        d = y[sel] - _extrapolate(pr[0], pr[1], t[sel] - pr[2], cfg)
        above = d > up_mag
        if above[0] and above[-1] and above.sum() >= cfg.step_nights:
            return lo, sel[above], _frac(float(np.median(d[above])))
    return None


@dataclass
class _Night:
    night: str
    mjd: float
    n: int
    env: float
    median: float
    spread: float
    steady: bool


def _nights(frames: list, k: float, cfg: ThroughputConfig) -> list[_Night]:
    by: dict[str, list[float]] = defaultdict(list)
    for f in frames:
        by[f["night"]].append(float(f["zp"]) + k * float(f["airmass"]))
    out = []
    for n in sorted(by):
        v = np.array(by[n])
        if len(v) < cfg.min_frames:
            continue
        spread = float(np.percentile(v, 90) - np.percentile(v, 10))
        out.append(
            _Night(
                n,
                _night_mjd(n),
                len(v),
                float(np.percentile(v, cfg.percentile)),
                float(np.median(v)),
                spread,
                spread <= cfg.steady_spread,
            )
        )
    return out


def _events_for(key: str, events: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    tel, port, filt = key.split("/")
    out = []
    for e in events:
        if str(e.get("telescope", "")).lower() != tel:
            continue
        if e.get("port") not in (None, port) or e.get("filter") not in (None, filt):
            continue
        out.append(e)
    return sorted(out, key=lambda e: e["night"])


def _series(
    key: str, frames: list, events: list[Mapping[str, Any]], cfg: ThroughputConfig
) -> dict[str, Any]:
    tel, port, filt = key.split("/")
    unit = frames[0].get("unit", "e-/s")
    k, k_source = cfg.extinction_for(key), "configured"
    ext = None
    if cfg.fit_extinction:
        ext = fit_extinction(frames, steady_spread=cfg.steady_spread)
        if ext and ext["nights"] >= 3 and ext["error"] < 0.03:
            k, k_source = ext["k"], "fitted"
    expected = cfg.expected_zp(tel, None if port == "-" else port, unit)
    nights = _nights(frames, k, cfg)
    out: dict[str, Any] = {
        "series": key,
        "telescope": tel,
        "port": None if port == "-" else port,
        "filter": None if filt == "-" else filt,
        "unit": unit,
        "extinction": {"k": k, "source": k_source, **({"fit": ext} if ext else {})},
        "expected_zp": None if expected is None else round(expected, 3),
        "frames": len(frames),
        "nights": [],
        "events": [],
        "alarms": [],
        "status": None,
    }
    if not nights:
        return out
    t = np.array([n.mjd for n in nights])
    y = np.array([n.env for n in nights])
    steady = np.array([n.steady for n in nights])
    evs = _events_for(key, events)
    ev_i = 0
    seg = trend = 0  # first night of the baseline segment and of the trend
    baseline: float | None = cfg.baselines.get(key)
    baseline_from: str | None = "configured" if baseline is not None else None
    up_mag = 2.5 * math.log10(1.0 + cfg.step_up)
    down_mag, drop_mag = _mag(cfg.step_down), _mag(cfg.alarm_drop)
    alarm: dict[str, Any] | None = None
    below = 0  # nights in a row with the level below the alarm limit
    rows = []

    #: After a step down: the trend from before it (level, slope, its time),
    #: to tell a recovery from a cleaning.
    down: tuple[float, float, float] | None = None
    down_alarm: dict[str, Any] | None = None

    def close_alarm(night: str, why: str) -> None:
        nonlocal alarm
        if alarm is not None:
            alarm["end"], alarm["cleared"] = night, why
            alarm = None

    def end_down(night: str, why: str) -> None:
        nonlocal down, down_alarm
        if down_alarm is not None:
            down_alarm["end"], down_alarm["cleared"] = night, why
        down, down_alarm = None, None

    for j, nt in enumerate(nights):
        # A person's event on or before this night (after the previous one).
        while ev_i < len(evs) and evs[ev_i]["night"] <= nt.night:
            e = evs[ev_i]
            ev_i += 1
            out["events"].append(
                {"night": nt.night, "what": e.get("what"), "note": e.get("note", ""),
                 "source": "annotation", "reset": bool(e.get("reset", True)),
                 "detected_on": nt.night}
            )  # fmt: skip
            if e.get("reset", True):
                seg = trend = j
                baseline = _num(e.get("baseline"))
                baseline_from = "annotation" if baseline is not None else None
                close_alarm(nt.night, f"{e.get('what')} (annotated)")
                end_down(nt.night, f"{e.get('what')} (annotated)")
        # Steps against the trend of the nights before.
        # Steps are judged on *steady* nights (patchy cloud says nothing).
        sel_all = np.flatnonzero(steady[trend : j + 1]) + trend if steady[j] else np.zeros(0, int)
        up = _step_up(t, y, sel_all, trend, up_mag, cfg)
        if up is not None:
            lo, sel, gain = up
            if (
                down is not None
                and float(np.median(y[sel] - _extrapolate(down[0], down[1], t[sel] - down[2], cfg)))
                < up_mag
            ):
                # Back to the trend from before a step down: not a cleaning.
                out["events"].append(
                    {"night": nights[lo].night, "what": "recovered",
                     "change": round(gain, 4), "source": "detected", "reset": False,
                     "detected_on": nt.night,
                     "note": f"throughput back up {100 * gain:.0f}% to its earlier trend"}
                )  # fmt: skip
                end_down(nights[lo].night, "recovered")
                trend = lo
            else:
                out["events"].append(
                    {"night": nights[lo].night, "what": "step_up",
                     "change": round(gain, 4), "source": "detected", "reset": True,
                     "detected_on": nt.night,
                     "note": f"throughput up {100 * gain:.0f}% (cleaning? recoat?): baseline reset"}
                )  # fmt: skip
                close_alarm(nights[lo].night, "step up: baseline reset")
                end_down(nights[lo].night, "step up: baseline reset")
                seg = trend = lo
                baseline, baseline_from = None, None
        md = cfg.step_down_nights
        sel = sel_all[sel_all >= trend][-md:]
        pr = None
        if len(sel) == md and sel[0] - trend >= cfg.min_nights:
            lo = int(sel[0])
            pr = _prior(t, y, trend, lo, cfg)
        if pr is not None:
            lv, sl = pr[0], pr[1]
            d = y[sel] - _extrapolate(lv, sl, t[sel] - pr[2], cfg)
            # A step is one level: nights of uniform cirrus scatter more.
            if np.all(d < -down_mag) and np.ptp(d) <= 1.5 * cfg.cloud_clip:
                change = _frac(float(np.median(d)))
                note = (
                    f"throughput down {-100 * change:.0f}% on {md} steady nights"
                    " (dew? a cover? optics? or steady cirrus)"
                )
                out["events"].append(
                    {"night": nights[lo].night, "what": "step_down",
                     "change": round(change, 4), "source": "detected", "reset": False,
                     "detected_on": nt.night, "note": note}
                )  # fmt: skip
                if down is None:
                    down = pr
                    down_alarm = {"start": nights[lo].night, "raised_on": nt.night, "end": None,
                                  "kind": "step_down", "change": round(change, 4),
                                  "note": note}  # fmt: skip
                    out["alarms"].append(down_alarm)
                trend = lo
        # The baseline: the first nights of the segment.
        if (
            baseline is None
            and t[j] - t[seg] >= cfg.baseline_days
            and j - seg + 1 >= cfg.min_nights
        ):
            # The trend of the segment's first baseline_days, at its first night.
            b = seg + int(np.searchsorted(t[seg : j + 1], t[seg] + cfg.baseline_days, "right"))
            lv0, sl0, _ = _fit(t[seg:b], y[seg:b], cfg)
            baseline = lv0 + sl0 * (t[seg] - t[b - 1])
            baseline_from = f"{nights[seg].night}..{nights[b - 1].night}"
        # The level now.
        w = t[trend : j + 1] >= t[j] - cfg.window_days
        lv, sl, kept = _fit(t[trend : j + 1][w], y[trend : j + 1][w], cfg)
        change = None if baseline is None else _frac(lv - baseline)
        below = below + 1 if baseline is not None and lv < baseline - drop_mag else 0
        if below >= cfg.alarm_nights:
            if alarm is None:
                alarm = {"start": nights[j - below + 1].night, "raised_on": nt.night,
                         "end": None, "kind": "drop",
                         "change": round(change, 4),
                         "note": f"throughput {100 * -change:.0f}% below its baseline"}  # fmt: skip
                out["alarms"].append(alarm)
            alarm["worst"] = round(min(alarm.get("worst", 0.0), change), 4)
        elif alarm is not None and baseline is not None and lv > baseline - 0.5 * drop_mag:
            close_alarm(nt.night, "recovered")
        rows.append((lv, sl, change, baseline))
    # Per night, with the final window's cloud flags.
    j = len(nights) - 1
    w = np.zeros(len(nights), bool)
    w[trend:] = t[trend:] >= t[j] - cfg.window_days
    _, _, kept = _fit(t[w], y[w], cfg)
    cloudy = np.zeros(len(nights), bool)
    cloudy[np.flatnonzero(w)[~kept]] = True
    for i, (nt, (lv, _sl, ch, bl)) in enumerate(zip(nights, rows, strict=True)):
        out["nights"].append(
            {
                "night": nt.night,
                "frames": nt.n,
                "envelope": round(nt.env, 4),
                "median": round(nt.median, 4),
                "spread": round(nt.spread, 3),
                "steady": nt.steady,
                "cloudy": bool(cloudy[i]) if w[i] else None,
                "level": round(lv, 4),
                "baseline": None if bl is None else round(bl, 4),
                "change": None if ch is None else round(ch, 4),
                "efficiency": None if expected is None else _eff(nt.env, expected),
            }
        )
    lv, sl, ch, bl = rows[-1]
    st: dict[str, Any] = {
        "night": nights[-1].night,
        "level": round(lv, 4),
        "slope_per_30d": round(_frac(sl * 30.0), 4),
        "nights_in_window": int(w.sum()),
        "clear_nights_in_window": int(kept.sum()),
        "baseline": None if bl is None else round(bl, 4),
        "baseline_from": baseline_from,
        "change": None if ch is None else round(ch, 4),
        "alarm": alarm is not None,
    }
    if expected is not None:
        st["efficiency"] = _eff(lv, expected)
        if bl is not None:
            st["baseline_efficiency"] = _eff(bl, expected)
    if alarm is not None:
        st["alarm_since"] = alarm["start"]
        st["alarm_text"] = (
            f"{key}: throughput {100 * -ch:.0f}% below its baseline (since {alarm['start']};"
            f" limit {100 * cfg.alarm_drop:.0f}%)"
        )
    if down_alarm is not None:
        st["step_down"] = {"night": down_alarm["start"], "note": down_alarm["note"]}
    out["status"] = st
    return out


def analyze(records: Iterable[Mapping[str, Any]], cfg: ThroughputConfig) -> dict[str, Any]:
    """Every series' nights, events, alarms and current status (module doc)."""
    records = list(records)
    events = [r for r in records if r.get("kind") == "event"]
    series = {}
    for key, frames in sorted(_by_series(records, cfg).items()):
        frames.sort(key=lambda r: (r["night"], r.get("t") or ""))
        series[key] = _series(key, frames, events, cfg)
    return {"time": datetime.now(UTC).isoformat(timespec="seconds"), "series": series}


# -- status, report ---------------------------------------------------------------------


def status_document(result: Mapping[str, Any], *, recent: int = 14) -> dict[str, Any]:
    """What the console reads: each series' status, its recent nights and
    events, and the active alarms (stable texts: an acknowledgement sticks)."""
    out: dict[str, Any] = {"time": result["time"], "series": {}, "alarms": []}
    for key, s in result["series"].items():
        st = s.get("status")
        if st is None:
            continue
        out["series"][key] = {
            **st,
            "telescope": s["telescope"],
            "port": s["port"],
            "filter": s["filter"],
            "unit": s["unit"],
            "extinction": s["extinction"]["k"],
            "expected_zp": s["expected_zp"],
            "recent": [
                {k: n[k] for k in ("night", "envelope", "level", "cloudy", "change")}
                for n in s["nights"][-recent:]
            ],
            "events": s["events"][-5:],
        }
        if st.get("alarm"):
            out["alarms"].append(
                {"series": key, "telescope": s["telescope"], "level": "warning",
                 "text": f"{key}: throughput below baseline by more than its limit",
                 "detail": st["alarm_text"]}
            )  # fmt: skip
        sd = st.get("step_down")
        if sd:
            out["alarms"].append(
                {"series": key, "telescope": s["telescope"], "level": "alarm",
                 "text": f"{key}: sudden throughput drop on {sd['night']}",
                 "detail": sd["note"]}
            )  # fmt: skip
    return out


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def report_section(result: Mapping[str, Any], night: str) -> dict[str, Any]:
    """The morning report's ``throughput`` entry: per series, tonight's
    envelope, the level, change against baseline, slope, events and alarms
    that started tonight, and QA lines."""
    night = _night_str(night)
    out: dict[str, Any] = {"series": {}, "look_at": []}
    for key, s in result["series"].items():
        st = s.get("status")
        if st is None:
            continue
        tonight = next((n for n in s["nights"] if n["night"] == night), None)
        new_events = [e for e in s["events"] if e["detected_on"] == night]
        out["series"][key] = {
            "tonight": tonight,
            "level": st["level"],
            "efficiency": st.get("efficiency"),
            "change": st.get("change"),
            "slope_per_30d": st["slope_per_30d"],
            "baseline_from": st.get("baseline_from"),
            "alarm": st.get("alarm_text"),
            "events": new_events,
        }
        if st.get("alarm"):
            out["look_at"].append(st["alarm_text"])
        for e in new_events:
            if e["source"] == "detected":
                out["look_at"].append(f"{key}: {e['note']} ({e['night']})")
    return out


def render_markdown(section: Mapping[str, Any]) -> list[str]:
    """The morning report's throughput section (Markdown lines)."""
    L = [
        "## Throughput",
        "",
        "| series | tonight (zp above atm.) | level | efficiency | vs baseline | per 30 d |"
        " alarm / events |",
        "|---|---|---|---|---|---|---|",
    ]

    def pct(x: Any) -> str:
        return "-" if x is None else f"{100 * x:+.1f}%"

    for key, s in section["series"].items():
        tn = s["tonight"]
        tonight = "-"
        if tn is not None:
            tonight = f"{tn['envelope']:.3f}" + (" (cloudy)" if tn["cloudy"] else "")
        notes = [s["alarm"]] if s["alarm"] else []
        notes += [e["note"] or e["what"] for e in s["events"]]
        eff = "-" if s["efficiency"] is None else f"{s['efficiency']:.3f}"
        L.append(
            f"| {key} | {tonight} | {s['level']:.3f} | {eff} | {pct(s['change'])} |"
            f" {pct(s['slope_per_30d'])} | {'; '.join(notes) or '-'} |"
        )
    return [*L, ""]


@dataclass
class UpdateResult:
    added: int
    result: dict[str, Any]
    status: dict[str, Any]
    new_alarms: list[dict[str, Any]]


def update(
    cfg: ThroughputConfig, night: str | None = None, data_root: str | Path | None = None
) -> UpdateResult:
    """Ingest ``night``'s frame indexes (if given), analyze everything,
    write the status file. ``new_alarms``: the alarms that started on
    ``night`` (for a notification that is sent once)."""
    store = ThroughputStore(cfg.store)
    added = 0
    if night is not None and data_root is not None:
        added = ingest_night(store, data_root, night, default_port=cfg.default_port)
    result = analyze(store.read(), cfg)
    status = status_document(result)
    if cfg.status is not None:
        _atomic_write(cfg.status, json.dumps(status, indent=1, default=str))
    new = []
    if night is not None:
        n = _night_str(night)
        for key, s in result["series"].items():
            new += [{"series": key, **a} for a in s["alarms"] if a["raised_on"] == n]
    return UpdateResult(added, result, status, new)


# -- the command line --------------------------------------------------------------------


def _solve_frames(
    paths: Sequence[str | Path],
    *,
    plate_scale: float,
    catalog: Any,
    jnow: bool,
    search_radius: float,
) -> Iterable[tuple[Path, Any, dict[str, Any] | None]]:
    """(path, header, zero point) for FITS files: the header's ``ZPMAG`` if
    it has one, else a plate solve near its RA/Dec and aperture photometry."""
    from astropy.io import fits

    from robofast_core.astrometry import SolverConfig, solve_near, zero_point
    from robofast_core.rotcal import _pointing

    cfg = SolverConfig(plate_scale=plate_scale, search_radius=search_radius)
    for p in paths:
        p = Path(p)
        try:
            with fits.open(p) as hdul:
                hdu = next(h for h in hdul if h.data is not None and h.data.ndim == 2)
                h, data = hdu.header.copy(), np.asarray(hdu.data, dtype=float)
        except Exception as exc:
            log.warning("%s: %s", p, exc)
            continue
        if str(h.get("IMAGETYP", "")).upper() in ("BIAS", "DARK", "FLAT", "SKYFLAT", "DOMEFLAT"):
            continue
        if _num(h.get("ZPMAG")) is not None:
            zp = {"zp": float(h["ZPMAG"]), "unit": str(h.get("ZPUNIT", "e-/s"))}
            for k, kw in (("error", "ZPERR"), ("scatter", "ZPSCAT"), ("n", "ZPNSTAR")):
                if h.get(kw) is not None:
                    zp[k] = h[kw]
            yield p, h, zp
            continue
        if catalog is None:
            log.info("%s: no ZPMAG (give --solve to measure it)", p)
            continue
        try:
            ra, dec, when = _pointing(h, jnow)
            sol = solve_near(
                data, catalog, ra, dec, cfg, binning=int(_num(h.get("XBINNING")) or 1), epoch=when
            )
            sat = _num(h.get("MAXADU")) or _num(h.get("SATURATE"))
            # Only robofast's EGAIN is the camera's e-/ADU (a legacy GAIN is the spectrograph's).
            z = zero_point(
                data,
                sol,
                exptime=_num(h.get("EXPTIME")) or 0.0,
                gain=_num(h.get("EGAIN")),
                saturation=0.95 * sat if sat else 0.95 * 65535,
            )
        except Exception as exc:
            log.info("%s: %s", p, exc)
            continue
        if z is not None:
            yield p, h, z


def _per_night(paths: list[Path], n: int) -> list[Path]:
    """At most ``n`` files a night, evenly spaced through it."""
    by: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(paths):
        m = _NIGHT.search(p.name)
        by[m.group(0) if m else ""].append(p)
    out = []
    for files in by.values():
        if len(files) <= n:
            out += files
        else:
            out += [files[int(i)] for i in np.linspace(0, len(files) - 1, n)]
    return out


def _print_status(result: Mapping[str, Any]) -> None:
    for key, s in result["series"].items():
        st = s.get("status")
        if st is None:
            print(f"{key}: {s['frames']} frames, no night with enough frames")
            continue
        eff = f" efficiency {st['efficiency']:.3f}" if st.get("efficiency") is not None else ""
        ch = "" if st["change"] is None else f" {100 * st['change']:+.1f}% vs baseline"
        print(
            f"{key}: {st['night']} level {st['level']:.3f} ({s['unit']}, k={s['extinction']['k']})"
            f"{eff}{ch}, {100 * st['slope_per_30d']:+.1f}%/30d, {len(s['nights'])} nights"
            + (f"  ALARM since {st['alarm_since']}" if st["alarm"] else "")
        )
        for e in s["events"]:
            print(f"    {e['night']} {e['what']} ({e['source']}): {e.get('note', '')}")
        for a in s["alarms"]:
            print(f"    alarm {a['start']} .. {a.get('end') or 'now'}: worst {a.get('worst')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m robofast_core.throughput")
    ap.add_argument("--config", required=True, help="throughput config (JSON)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update", help="ingest a night's frame indexes, analyze, write status")
    u.add_argument("--night", required=True)
    u.add_argument("--data-root", required=True)
    u.add_argument("--notifier", default=None, help="notifier config: send new alarms")
    b = sub.add_parser("backfill", help="ingest past frames (frame indexes or FITS files)")
    b.add_argument("--data-root", default=None, help="every night's frame index under it")
    b.add_argument("--solve", action="store_true", help="solve frames with no ZPMAG")
    b.add_argument("--plate-scale", type=float, default=None)
    b.add_argument("--catalog", default=None, help="catalog cache directory")
    b.add_argument("--jnow", action="store_true", help="header RA/Dec are JNow (legacy frames)")
    b.add_argument("--search-radius", type=float, default=0.3)
    b.add_argument("--instruments", default=None, help="instruments.json: legacy port numbers")
    b.add_argument("--telescope", default=None)
    b.add_argument("--port", default=None)
    b.add_argument("--per-night", type=int, default=None, help="at most N files a night")
    b.add_argument("files", nargs="*")
    a = sub.add_parser("annotate", help="record a cleaning, a recoat, ...")
    a.add_argument("--telescope", required=True)
    a.add_argument("--port", default=None)
    a.add_argument("--filter", default=None)
    a.add_argument("--night", required=True)
    a.add_argument("--what", required=True, help="cleaning, recoat, camera, ...")
    a.add_argument("--note", default="")
    a.add_argument("--no-reset", action="store_true", help="annotate only; keep the baseline")
    a.add_argument("--baseline", type=float, default=None)
    a.add_argument("--who", default=None)
    s = sub.add_parser("status", help="analyze and print every series")
    s.add_argument("--json", action="store_true")
    sub.add_parser("extinction", help="fit the extinction coefficient per series")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = ThroughputConfig.load(args.config)
    store = ThroughputStore(cfg.store)

    if args.cmd == "update":
        r = update(cfg, args.night, args.data_root)
        print(f"{r.added} new zero points")
        _print_status(r.result)
        if args.notifier and r.new_alarms:
            import asyncio

            from robofast_core.morning import notify

            text = "\n".join(
                f"- {x['series']}: {x.get('note') or 'throughput ' + str(x.get('change'))}"
                for x in r.new_alarms
            )
            asyncio.run(notify(text, "robofast: throughput", args.notifier, "warning"))
        return 0
    if args.cmd == "backfill":
        n = 0
        if args.data_root:
            nights = sorted(
                {p.parent.name for p in Path(args.data_root).rglob("n*/*.frames.jsonl")}
            )
            for night in nights:
                n += ingest_night(store, args.data_root, night, default_port=cfg.default_port)
        if args.files:
            paths = [Path(p) for p in args.files]
            if args.per_night:
                paths = _per_night(paths, args.per_night)
            catalog = None
            if args.solve:
                if args.plate_scale is None:
                    ap.error("--solve needs --plate-scale")
                from robofast_core.catalog import catalog_from_config

                catalog = catalog_from_config({"cache_dir": args.catalog} if args.catalog else None)
            port_map = None
            if args.instruments:
                inst = json.loads(Path(args.instruments).read_text())
                port_map = {t: v["ports"] for t, v in inst.get("telescopes", {}).items()
                            if isinstance(v, dict) and "ports" in v}  # fmt: skip
            batch = []
            for p, h, zp in _solve_frames(
                paths,
                plate_scale=args.plate_scale or 0.0,
                catalog=catalog,
                jnow=args.jnow,
                search_radius=args.search_radius,
            ):
                try:
                    batch.append(
                        record_from_header(h, zp, file=str(p), telescope=args.telescope,
                                           port=args.port, port_map=port_map,
                                           longitude=cfg.longitude)
                    )  # fmt: skip
                except ValueError as exc:
                    log.warning("%s", exc)
                if len(batch) >= 100:
                    n += store.append(batch)
                    batch = []
            n += store.append(batch)
        print(f"{n} new zero points in {cfg.store}")
        return 0
    if args.cmd == "annotate":
        e = annotate(
            store,
            telescope=args.telescope,
            night=args.night,
            what=args.what,
            note=args.note,
            port=args.port,
            filt=args.filter,
            reset=not args.no_reset,
            baseline=args.baseline,
            who=args.who,
        )
        print(json.dumps(e))
        return 0
    result = analyze(store.read(), cfg)
    if args.cmd == "status":
        if args.json:
            print(json.dumps(status_document(result), indent=1, default=str))
        else:
            _print_status(result)
        return 0
    for key, frames in sorted(_by_series(store.read(), cfg).items()):
        fit = fit_extinction(frames, steady_spread=cfg.steady_spread)
        print(f"{key}: configured k={cfg.extinction_for(key)}; fitted {fit or 'no steady night'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
