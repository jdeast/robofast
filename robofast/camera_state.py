"""When a camera's image is really this exposure's.

ASCOM says ``ImageReady`` goes false at ``StartExposure``. Andor's ASCOM
camera driver (MINERVA T1, 2026-11-05) leaves it true from the previous
image for seconds while ``CameraState`` reports *exposing*: a client that
believes it downloads the previous image again (byte-identical frames), and
its next ``StartExposure`` fails (Andor 20072, still acquiring). So
``ImageReady`` counts only once ``CameraState`` has left waiting, exposing
and reading.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

#: ASCOM ``CameraState`` values (cameraIdle .. cameraError).
CAMERA_IDLE, CAMERA_WAITING, CAMERA_EXPOSING, CAMERA_READING = 0, 1, 2, 3
#: The states in which this exposure's image cannot exist yet.
CAMERA_BUSY = frozenset({CAMERA_WAITING, CAMERA_EXPOSING, CAMERA_READING})


async def image_ready(get: Callable[[str], Awaitable[Any]]) -> bool:
    """``ImageReady`` from ``get(member)``, believed only when ``CameraState``
    is not busy (module doc). ``CameraState`` is read only once
    ``ImageReady`` is true."""
    if not await get("ImageReady"):
        return False
    return await get("CameraState") not in CAMERA_BUSY
