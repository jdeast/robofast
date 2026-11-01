"""A simulator of the MEarth South roof behind its parallel-port interface.

It implements :class:`~robofast_core.devices.mearth_roof_port.RoofPort`
(``write_data``, ``frob_control``, ``read_status``), so the server drives it
exactly as it drives ``/dev/parportN``, and models the hardware the legacy
MEarth code and operator guide describe (adapted from the MEarth observing
system, J. Irwin, used with permission; see that module's docs):

* **The interface box** latches the data byte when the strobe bit is
  released (the end of the pulse). ``o`` drives towards open, ``c`` towards
  closed, 0x00 stops. Any other byte stops (*unconfirmed*). The latch holds
  until the next strobe, whatever the computer does (no watchdog).
* **The drive** moves the roof at a constant rate (``travel_time`` for a
  full stroke; the VFD's ramps are not modelled) and stops by itself at the
  end it is driving towards (the hardware limits).
* **The limit switches** report position: open made at the open end, closed
  made at the closed end, in the status register as the real port shows them
  (``BUSY`` set for open, ``PAPEROUT`` clear for closed).

Faults, each a method (thread-safe):

* :meth:`set_manual` -- the local/computer switch left on local: the box
  latches but the drive ignores it; :meth:`manual_drive` moves the roof as
  someone at the controls would.
* :meth:`trip_vfd` -- the VFD's over-current trip (a jam, a crash into the
  end stop, an earthquake): the drive stops and ignores commands until
  :meth:`reset_vfd` (on site, by hand).
* :meth:`set_jammed` -- the motor runs but the roof does not move (a broken
  or frozen chain, a roof stuck on its rails).
* :meth:`set_sense_power` -- the 15 V supply for the limit contacts is off:
  both read "not made" though the drive still stops at the ends.
* :meth:`stick_limit` -- one limit contact reads made whatever the roof does
  (both made at once is then possible).
* :meth:`set_mains` -- mains power off: the drive stops; whether the real
  drive is on a UPS is *unconfirmed* (``drive_on_ups``).
* :meth:`set_drop_strobes` -- the box misses strobes (a flaky cable): commands
  are lost.
* :meth:`power_cycle_box` -- the box loses its latch (stops).

``position`` runs from 0 (closed) to 1 (open). The simulator never touches
real hardware.
"""

from __future__ import annotations

import logging
import threading
import time

from robofast_core.devices.mearth_roof_port import (
    CMD_CLOSE,
    CMD_OPEN,
    CMD_STOP,
    CONTROL_STROBE,
    STATUS_BUSY,
    STATUS_PAPEROUT,
)

log = logging.getLogger(__name__)

#: Status register bits that are not limit switches, as an idle port reads
#: (ERROR and SELECT high); they mean nothing to the roof.
_IDLE_BITS = 0x18


class MEarthSouthRoofSim:
    """The simulated roof, interface box and drive (one roll-off section)."""

    def __init__(
        self,
        *,
        travel_time: float = 60.0,
        position: float = 0.0,
        drive_on_ups: bool = False,
    ) -> None:
        if travel_time <= 0:
            raise ValueError("travel_time must be positive")
        self.travel_time = travel_time
        self.drive_on_ups = drive_on_ups
        self._lock = threading.RLock()
        self._position = min(1.0, max(0.0, position))
        self._t = time.monotonic()
        self._data = 0
        self._control = 0
        self.latched = CMD_STOP
        self.manual = False
        self.vfd_tripped = False
        self.jammed = False
        self.sense_power = True
        self.stuck_limit: str | None = None
        self.mains = True
        self.drop_strobes = False
        #: Bytes latched, as (monotonic time, byte).
        self.log: list[tuple[float, int]] = []
        self.strobes = 0

    # -- the model ------------------------------------------------------------

    def _direction(self) -> int:
        """+1 opening, -1 closing, 0 still: what the drive does right now."""
        if self.manual or self.vfd_tripped or not (self.mains or self.drive_on_ups):
            return 0
        if self.latched == CMD_OPEN and self._position < 1.0:
            return 1
        if self.latched == CMD_CLOSE and self._position > 0.0:
            return -1
        return 0

    def _advance(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        dt = max(0.0, now - self._t)
        self._t = now
        direction = self._direction()
        if direction and not self.jammed:
            step = direction * dt / self.travel_time
            self._position = min(1.0, max(0.0, self._position + step))

    # -- RoofPort -----------------------------------------------------------------

    def write_data(self, value: int) -> None:
        with self._lock:
            self._advance()
            self._data = value & 0xFF

    def frob_control(self, mask: int, value: int) -> None:
        with self._lock:
            self._advance()
            before = self._control
            self._control = (self._control & ~mask) | (value & mask)
            released = before & CONTROL_STROBE and not self._control & CONTROL_STROBE
            if released:
                self.strobes += 1
                if self.drop_strobes:
                    log.warning("sim: strobe dropped (data 0x%02x)", self._data)
                    return
                byte = self._data
                self.latched = byte if byte in (CMD_OPEN, CMD_CLOSE, CMD_STOP) else CMD_STOP
                self.log.append((self._t, byte))

    def read_status(self) -> int:
        with self._lock:
            self._advance()
            open_made, closed_made = self._contacts()
            status = _IDLE_BITS | STATUS_PAPEROUT  # nothing made
            if open_made:
                status |= STATUS_BUSY
            if closed_made:
                status &= ~STATUS_PAPEROUT
            return status

    def close(self) -> None:
        pass

    def _contacts(self) -> tuple[bool, bool]:
        if not self.sense_power:
            return False, False
        open_made = self._position >= 1.0 or self.stuck_limit == "open"
        closed_made = self._position <= 0.0 or self.stuck_limit == "closed"
        return open_made, closed_made

    # -- inspection -------------------------------------------------------------------

    @property
    def position(self) -> float:
        with self._lock:
            self._advance()
            return self._position

    def closed(self) -> bool:
        return self.position <= 0.0

    def open(self) -> bool:
        return self.position >= 1.0

    def moving(self) -> bool:
        with self._lock:
            self._advance()
            return self._direction() != 0 and not self.jammed

    def commands(self) -> list[int]:
        with self._lock:
            return [b for _, b in self.log]

    # -- faults ---------------------------------------------------------------------

    def _set(self, **changes: object) -> None:
        with self._lock:
            self._advance()
            for k, v in changes.items():
                setattr(self, k, v)

    def set_manual(self, manual: bool) -> None:
        self._set(manual=manual)

    def manual_drive(self, position: float) -> None:
        """Someone at the local controls moves the roof to ``position``."""
        self._set(_position=min(1.0, max(0.0, position)))

    def trip_vfd(self) -> None:
        self._set(vfd_tripped=True)

    def reset_vfd(self) -> None:
        self._set(vfd_tripped=False)

    def set_jammed(self, jammed: bool) -> None:
        self._set(jammed=jammed)

    def set_sense_power(self, on: bool) -> None:
        self._set(sense_power=on)

    def stick_limit(self, which: str | None) -> None:
        if which not in (None, "open", "closed"):
            raise ValueError(which)
        self._set(stuck_limit=which)

    def set_mains(self, on: bool) -> None:
        self._set(mains=on)

    def set_drop_strobes(self, drop: bool) -> None:
        self._set(drop_strobes=drop)

    def power_cycle_box(self) -> None:
        self._set(latched=CMD_STOP)
