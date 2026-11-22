"""Image files for the all-sky code, through Pillow (the ``allsky`` extra).

Pillow is imported lazily, here only: importing robofast (or
``robofast.allsky``) never needs it. Without it, reading or writing an
image raises :class:`PillowMissing`, which says what to install.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["PillowMissing", "decode", "pil", "read_mask", "write_png"]


class PillowMissing(ImportError):
    """Pillow is not installed."""


def pil() -> Any:
    """The ``PIL`` package, or :class:`PillowMissing` with the remedy."""
    try:
        import PIL.Image
        import PIL.ImageDraw
        import PIL.ImageFont
    except ImportError as exc:
        raise PillowMissing(
            "robofast.allsky reads and draws images with Pillow: "
            "pip install 'robofast[allsky]' (or pip install 'pillow>=10')"
        ) from exc
    return PIL


def decode(data: bytes) -> np.ndarray:
    """An encoded image (JPEG, PNG, ...) as uint8 (rows, cols) grey or
    (rows, cols, 3) RGB."""
    im = pil().Image.open(io.BytesIO(data))
    if im.mode not in ("L", "RGB"):
        grey = im.mode in ("1", "LA", "I", "I;16", "F")
        im = im.convert("L" if grey else "RGB")
    return np.asarray(im)


def write_png(path: str | Path, image: np.ndarray) -> None:
    """Write uint8 grey or RGB ``image`` as PNG."""
    pil().Image.fromarray(np.ascontiguousarray(image, dtype=np.uint8)).save(str(path), "PNG")


def read_mask(path: str | Path) -> np.ndarray:
    """A mask PNG as a bool array (true where brighter than mid-grey)."""
    a = decode(Path(path).read_bytes())
    return (a if a.ndim == 2 else a[:, :, 0]) > 127
