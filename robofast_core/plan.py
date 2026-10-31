"""What the sequencer observes: observations and plan providers.

The sequencer asks its :class:`PlanProvider` for each telescope's next
observation whenever that telescope is free -- after the enclosure opens,
after each observation, after an error backoff -- so every choice is made
from scratch, at the time it is used (design.md 3.4: never resume a stale
action). It then reports how each observation ended, so the provider can
mark it done, retry it or drop it.

The dispatch scheduler (design.md 3.7) will be a :class:`PlanProvider`: its
``next_observation`` picks the best request for that telescope right now
(priority, time-dependent bonus, hard constraints), and its ``report``
updates the request's status. Until it exists there are two simple ones:

* :class:`StaticPlan` -- a fixed target list (JSON), worked through in order;
* :class:`CalibrationPlan` -- dark and bias frames only; the telescope is
  never unparked.

:func:`plan_from_config` builds either from a config entry (see
:mod:`robofast_core.sequencer_service`).
"""

from __future__ import annotations

import inspect
import json
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Protocol

#: How an observation ended, as passed to :meth:`PlanProvider.report`.
#: "complete": every frame taken. "interrupted": stopped by the enclosure
#: closing or the sequencer stopping (not its fault; worth retrying).
#: "failed": a device error or a refused slew.
OUTCOMES = ("complete", "interrupted", "failed")


@dataclass
class Observation:
    name: str
    #: Target; ``None`` means "half an hour east of the meridian, 10 deg
    #: south of zenith", computed from the mount (handy for simulators).
    ra_hours: float | None = None
    dec_deg: float | None = None
    exptime: float = 10.0
    count: int = 1
    #: False for dark and bias frames: the telescope is not unparked or
    #: slewed, and the shutter stays closed (``StartExposure(..., False)``).
    light: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Observation:
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"observation {d.get('name')!r}: unknown keys {sorted(unknown)}")
        return cls(**d)


class PlanProvider(Protocol):
    """The sequencer's source of observations, one telescope at a time.

    Both methods are called from the sequencer's event loop. A provider that
    needs I/O (a database, the request API) does it in ``next_observation``,
    which the sequencer bounds with ``plan_timeout``; a timeout or exception
    there counts as that telescope's error. ``report`` must not block.
    """

    async def next_observation(self, telescope: str) -> Observation | None:
        """What ``telescope`` should do now, or None: nothing more tonight."""
        ...

    def report(self, telescope: str, obs: Observation, outcome: str, frames: int) -> None:
        """How ``obs`` ended on ``telescope`` (one of :data:`OUTCOMES`), and
        how many of its frames were taken."""
        ...


class _Queues:
    """Per-telescope FIFO queues. An observation that did not complete goes
    back to the front of its telescope's queue (whole: frames already taken
    are taken again), so the telescope retries it after the enclosure reopens
    or after its error backoff; the sequencer's give-up bounds the retries."""

    def __init__(self, queues: dict[str, Iterable[Observation]]) -> None:
        self.queues = {tel: deque(obs) for tel, obs in queues.items()}
        self.history: list[tuple[str, str, str, int]] = []

    async def next_observation(self, telescope: str) -> Observation | None:
        queue = self.queues.get(telescope)
        return queue.popleft() if queue else None

    def report(self, telescope: str, obs: Observation, outcome: str, frames: int) -> None:
        self.history.append((telescope, obs.name, outcome, frames))
        if outcome != "complete":
            self.queues.setdefault(telescope, deque()).appendleft(obs)


class StaticPlan(_Queues):
    """A fixed list of targets.

    Each target is observed by every telescope named in its ``telescopes``
    list (default: all of them), in list order, until it completes.
    ``history`` records every report.
    """

    def __init__(self, targets: Iterable[dict[str, Any]], telescopes: Sequence[str]) -> None:
        queues: dict[str, list[Observation]] = {tel: [] for tel in telescopes}
        for t in targets:
            t = dict(t)
            names = t.pop("telescopes", None) or list(telescopes)
            obs = Observation.from_dict(t)
            for tel in names:
                if tel not in queues:
                    raise ValueError(f"target {obs.name!r}: unknown telescope {tel!r}")
                queues[tel].append(obs)
        super().__init__(queues)


class CalibrationPlan(_Queues):
    """Bias and dark frames on every telescope, then nothing.

    ``darks`` is a list of ``[exptime, count]``. The frames run under the
    sequencer's open lease like any other observation (the telescope stays
    parked; the enclosure may open); a closed-roof calibration mode is
    future work.
    """

    def __init__(
        self, telescopes: Sequence[str], biases: int = 0, darks: Iterable[Sequence[float]] = ()
    ) -> None:
        frames = [Observation("bias", exptime=0.0, count=biases, light=False)] if biases else []
        for exptime, count in darks:
            name = f"dark{exptime:g}"
            frames.append(Observation(name, exptime=float(exptime), count=int(count), light=False))
        super().__init__({tel: list(frames) for tel in telescopes})


class CallablePlan:
    """Adapt a function ``fn(telescope) -> Observation | None`` (or a
    zero-argument ``fn()``) to :class:`PlanProvider`; reports are kept in
    ``history``."""

    def __init__(self, fn: Callable[..., Observation | None]) -> None:
        self._fn = fn
        self._takes_telescope = bool(inspect.signature(fn).parameters)
        self.history: list[tuple[str, str, str, int]] = []

    async def next_observation(self, telescope: str) -> Observation | None:
        return self._fn(telescope) if self._takes_telescope else self._fn()

    def report(self, telescope: str, obs: Observation, outcome: str, frames: int) -> None:
        self.history.append((telescope, obs.name, outcome, frames))


def as_plan(plan: PlanProvider | Callable[..., Observation | None]) -> PlanProvider:
    """A :class:`PlanProvider` as is; a plain function through :class:`CallablePlan`."""
    if hasattr(plan, "next_observation") and hasattr(plan, "report"):
        return plan  # type: ignore[return-value]
    return CallablePlan(plan)  # type: ignore[arg-type]


def plan_from_config(
    config: dict[str, Any], telescopes: Sequence[str], base: Path | None = None
) -> PlanProvider:
    """Build a plan from a config entry.

    * ``{"type": "static", "targets": [...]}``, or ``"file": "targets.json"``
      (a JSON list of targets, relative to ``base``). A target is an
      :class:`Observation`'s fields plus an optional ``telescopes`` list.
    * ``{"type": "calibration", "biases": 10, "darks": [[60, 5]]}``.
    """
    config = dict(config)
    kind = config.pop("type", None)
    if kind == "static":
        if "file" in config:
            path = Path(config.pop("file"))
            if base is not None and not path.is_absolute():
                path = base / path
            targets = json.loads(path.read_text())
        else:
            targets = config.pop("targets")
        if config:
            raise ValueError(f"static plan: unknown keys {sorted(config)}")
        return StaticPlan(targets, telescopes)
    if kind == "calibration":
        return CalibrationPlan(telescopes, **config)
    raise ValueError(f"plan type {kind!r}: expected 'static' or 'calibration'")
