"""Archived all-sky nights and the site weather log, for validation.

Two layouts:

* MMTO 2016-18 nights: ``nYYYYMMDD/skycam/frame.NNNNN.png`` (640x480,
  one per ~34 s) with that night's ``site_mtHopkins.log`` beside them.
* 2023 nights: ``nYYYYMMDD/<camera>/frame.NNNNN.<ext>``.

Frames carry no time header. The file modification time (kept by the
copy) is when the frame was saved; :func:`frame_times` uses it, less a
fixed lag (the frame was taken a little before it was saved). The
burned-in stamps agree with it to a few seconds.

The site log (the 2016-18 MINERVA control software) has lines like
``2016-01-08T23:10:12 [env.py:250 - getWeather()] DEBUG: MearthCloud=-21.0``
(UTC); later logs prefix the telescope (``DEBUG: T4: MearthCloud=-51.3``).
:func:`read_site_log` returns each key's (time, value) series; the
cloud sensors are ``MearthCloud`` (sky minus ambient temperature, deg C:
more negative is clearer; 999 or -999 means no reading), ``HATCloud`` and
``AuroraCloud``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

__all__ = ["frame_paths", "frame_times", "read_site_log", "series_at"]

_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\S* .*?DEBUG: (?:\w+: )?(\w+)=(\S+)\s*$")
_FRAME = re.compile(r"frame\.(\d+)\.(png|jpg|jpeg)$", re.IGNORECASE)


def frame_paths(directory: str | Path) -> list[Path]:
    d = Path(directory)
    out = [p for p in d.iterdir() if _FRAME.search(p.name)]
    return sorted(out, key=lambda p: int(_FRAME.search(p.name).group(1)))  # type: ignore[union-attr]


def frame_times(paths: list[Path], lag_s: float = 15.0) -> list[datetime]:
    """UTC time of each frame: modification time minus ``lag_s``."""
    return [
        datetime.fromtimestamp(os.path.getmtime(p), UTC) - timedelta(seconds=lag_s) for p in paths
    ]


def read_site_log(
    path: str | Path,
    keys: tuple[str, ...] = (
        "MearthCloud",
        "HATCloud",
        "MINERVACloud",
        "AuroraCloud",
        "sunAltitude",
        "outsideHumidity",
        "outsideTemp",
        "outsideDewPt",
    ),
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """{key: (times as POSIX seconds, values)} for numeric ``keys``."""
    want = set(keys)
    acc: dict[str, list[tuple[float, float]]] = {k: [] for k in keys}
    with open(path, encoding="latin-1") as f:
        for line in f:
            m = _LINE.match(line)
            if not m or m.group(2) not in want:
                continue
            try:
                v = float(m.group(3))
            except ValueError:
                continue
            t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
            acc[m.group(2)].append((t.timestamp(), v))
    out = {}
    for k, rows in acc.items():
        a = np.array(rows, dtype=float).reshape(-1, 2)
        out[k] = (a[:, 0], a[:, 1])
    return out


def series_at(
    series: tuple[np.ndarray, np.ndarray],
    when: datetime,
    max_gap_s: float = 300.0,
    bad: tuple[float, ...] = (999.0, -999.0),
) -> float:
    """The value nearest ``when`` (NaN if none within ``max_gap_s`` or a
    no-reading code)."""
    t, v = series
    if t.size == 0:
        return float("nan")
    ts = when.timestamp()
    i = int(np.argmin(np.abs(t - ts)))
    if abs(t[i] - ts) > max_gap_s or v[i] in bad:
        return float("nan")
    return float(v[i])


def iter_night(
    directory: str | Path, step: int = 1, lag_s: float = 15.0
) -> Iterator[tuple[Path, datetime]]:
    paths = frame_paths(directory)[::step]
    yield from zip(paths, frame_times(paths, lag_s), strict=True)
