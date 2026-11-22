"""Reading all-sky frames: pixels and the time they were taken.

:func:`load_frame` decodes with Pillow (the ``allsky`` extra, imported
lazily: :mod:`robofast.allsky.imageio`). The analysis uses one grey
(luminance) channel; colour images also keep their RGB, which the MMTO
overlay mask needs (its labels are coloured).

Frame times: the live fetcher names frames
``<camera>_<YYYYMMDDTHHMMSS>Z.<ext>`` (the server's Last-Modified, UTC);
:func:`time_from_name` reads that. Anything else must pass the time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from robofast.allsky import imageio

__all__ = ["Frame", "decode_image", "load_frame", "time_from_name"]

_NAME = re.compile(r"(?P<cam>[A-Za-z0-9-]+)_(?P<t>\d{8}T\d{6})Z")


def time_from_name(path: str | Path) -> datetime | None:
    m = _NAME.search(Path(path).name)
    if not m:
        return None
    return datetime.strptime(m.group("t"), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)


def camera_from_name(path: str | Path) -> str | None:
    m = _NAME.search(Path(path).name)
    return m.group("cam") if m else None


def decode_image(data: bytes) -> np.ndarray:
    """uint8 (rows, cols) or (rows, cols, 3); needs Pillow."""
    return imageio.decode(data)


@dataclass
class Frame:
    camera: str
    time: datetime
    #: Luminance, float32.
    grey: np.ndarray
    rgb: np.ndarray | None = None
    path: str = ""

    @property
    def shape(self) -> tuple[int, int]:
        return self.grey.shape  # type: ignore[return-value]


def to_grey(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img.astype(np.float32)
    rgb = img[:, :, :3].astype(np.float32)
    return 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]


def load_frame(path: str | Path, camera: str | None = None, when: datetime | None = None) -> Frame:
    """Read a frame. ``camera`` and ``when`` default to the file name's."""
    path = Path(path)
    img = decode_image(path.read_bytes())
    cam = camera or camera_from_name(path)
    t = when or time_from_name(path)
    if cam is None or t is None:
        raise ValueError(f"{path.name}: give the camera and the time (name not <cam>_<UTC>Z)")
    return Frame(
        camera=cam,
        time=t if t.tzinfo else t.replace(tzinfo=UTC),
        grey=to_grey(img),
        rgb=img if img.ndim == 3 else None,
        path=str(path),
    )
