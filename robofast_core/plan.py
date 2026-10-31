"""What the sequencer observes: observations and plan providers.

The sequencer asks its :class:`PlanProvider` for each telescope's next
observation whenever that telescope is free -- after the enclosure opens,
after each observation, after an error backoff -- so every choice is made
from scratch, at the time it is used (design.md 3.4: never resume a stale
action). It then reports how each observation ended, so the provider can
mark it done, retry it or drop it.

Providers:

* :class:`~robofast_core.dispatch.DispatchScheduler` (design.md 3.7) --
  picks the best request for that telescope right now (hard constraints,
  then priority plus a time-dependent bonus), updates the request's status
  from each report, and takes targets of opportunity, interrupting a
  telescope through the sequencer's ``preempt`` (design.md 3.7.1);
* :class:`StaticPlan` -- a fixed target list (JSON), worked through in order;
* :class:`CalibrationPlan` -- dark and bias frames only; the telescope is
  never unparked and the enclosure never opened (the sequencer takes
  frames with ``light=False`` without a lease when no telescope has light
  work: its closed phase, design.md 3.4).

A provider may also implement optional hooks, which the sequencer and its
service use when present:

* ``async peek(telescope)`` -- what ``next_observation`` would answer now,
  *without* handing it out or changing any state. The sequencer asks it
  while it holds no lease (its closed phase), to decide whether to request
  one (light work), take dark frames with the enclosure closed, or keep
  waiting. Without it the sequencer asks ``next_observation`` and holds the
  answer as that telescope's next observation.
* ``set_preempt_callback(fn)`` -- the sequencer passes its ``preempt``;
* ``async serve()`` -- a background task run alongside the sequencer (the
  dispatch scheduler's ToO intake);
* a ``files`` keyword on ``report`` -- with the sequencer's frame pipeline
  (:mod:`robofast_core.imaging`) on, the sequencer passes the observation's
  written frames as paths relative to the data root (a ``complete`` report
  only after they are all on disk). Plans whose ``report`` lacks it are
  called without it.

:func:`plan_from_config` builds any of them from a config entry (see
:mod:`robofast_core.sequencer_service`).
"""

from __future__ import annotations

import inspect
import json
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Protocol

#: How an observation ended, as passed to :meth:`PlanProvider.report`.
#: "complete": every frame taken. "interrupted": stopped by the enclosure
#: closing or the sequencer stopping (not its fault; worth retrying).
#: "failed": a device error or a refused slew.
OUTCOMES = ("complete", "interrupted", "failed")
#: What an observation does on its target (:attr:`Observation.kind`).
KINDS = ("expose", "autofocus", "spectrum", "spectrograph_calibration")


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
    #: With no light work on any telescope they are taken with the
    #: enclosure closed (no lease).
    light: bool = True
    #: Filter name, selected on the telescope's filter wheel (its ``Names``)
    #: before the exposures; None: leave the wheel alone.
    filter: str | None = None
    #: M3 port, selected through the telescope's port ``Action``; None: leave
    #: it. A telescope without a port action fails an observation that names
    #: one (never observes through the wrong port).
    port: str | None = None
    #: If it is interrupted or fails part-way, only the frames not yet taken
    #: are re-taken (otherwise the whole observation is).
    allow_partial: bool = False
    #: The target's name for FITS ``OBJECT`` and file names (``name`` if None;
    #: the dispatch scheduler's names carry request ids as well).
    object: str | None = None
    #: Guide while exposing: the telescope's guider is started on target and
    #: stopped when the observation ends (docs/guider.md). A telescope
    #: without a guider fails such an observation.
    guide: bool = False
    #: What to do on the target: ``"expose"`` (``count`` frames),
    #: ``"autofocus"`` (focus the telescope there: :mod:`robofast_core.autofocus`;
    #: ``exptime`` and ``count`` come from the telescope's autofocus config),
    #: ``"spectrum"`` (acquire the target on the fiber, guide, and take
    #: ``count`` spectra with the enclosure's spectrograph, together with the
    #: other telescopes of ``spectrum["feed"]``; docs/rv_night.md), or
    #: ``"spectrograph_calibration"`` (the spectrograph's ``calibration``
    #: frames; no light, the telescope is not moved).
    kind: str = "expose"
    #: For ``kind="autofocus"``: overrides of the telescope's autofocus
    #: config (e.g. ``{"method": "sweep"}``), and ``{"calibrate": true}``
    #: for a calibration sweep.
    autofocus: dict[str, Any] | None = None
    #: For ``kind="spectrum"``: ``id`` (the same for every telescope feeding
    #: one spectrum), ``feed`` (the telescopes asked to feed it), ``leader``
    #: (the telescope that runs the exposures and reports to the plan; None:
    #: the first to start it), ``iodine`` (bool or None for the
    #: spectrograph's default), ``target_counts`` (exposure-meter
    #: termination; ``exptime`` is then the maximum) and ``target`` (the
    #: target's astrometry for the barycentric correction:
    #: :meth:`~robofast_core.request_model.Target.to_dict`).
    spectrum: dict[str, Any] | None = None
    #: For ``kind="spectrograph_calibration"``: the frames, each ``{"kind":
    #: "thar"|"slitflat"|"fiberflat"|"bias"|"dark", "exptime": s, "n": N}``
    #: (the spectrograph service's ``POST /calibrate``).
    calibration: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"observation {self.name!r}: kind {self.kind!r} not in {KINDS}")
        if self.kind == "autofocus" and not self.light:
            raise ValueError(f"observation {self.name!r}: an autofocus needs light")
        if self.kind == "spectrum":
            if not self.light:
                raise ValueError(f"observation {self.name!r}: a spectrum needs light")
            self.guide = True
            spec = dict(self.spectrum or {})
            spec.setdefault("id", self.name)
            spec["feed"] = list(spec.get("feed") or [])
            self.spectrum = spec
        elif self.spectrum is not None:
            raise ValueError(f"observation {self.name!r}: 'spectrum' is for kind 'spectrum'")
        if self.kind == "spectrograph_calibration":
            if self.light:
                raise ValueError(
                    f"observation {self.name!r}: spectrograph calibrations take no light"
                    " (light: false)"
                )
            if not self.calibration:
                raise ValueError(f"observation {self.name!r}: no calibration frames")
        elif self.calibration is not None:
            raise ValueError(
                f"observation {self.name!r}: 'calibration' is for kind 'spectrograph_calibration'"
            )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Observation:
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"observation {d.get('name')!r}: unknown keys {sorted(unknown)}")
        return cls(**d)


@dataclass
class Idle:
    """Returned by :meth:`PlanProvider.next_observation`: nothing to do *now*,
    ask again after ``seconds`` (or sooner, if the sequencer is woken by
    :meth:`~robofast_core.sequencer.Sequencer.preempt`). The telescope stops
    tracking and stays where it is (not parked) while it waits; the
    enclosure stays open -- until every telescope of the enclosure has been
    idle for the sequencer's ``idle_release_after``: then the generation ends
    (park, release the lease, the enclosure closes) and the sequencer asks
    the plan again, without a lease, until something is observable.
    ``None`` still means "nothing more tonight"."""

    seconds: float
    reason: str = ""


class PlanProvider(Protocol):
    """The sequencer's source of observations, one telescope at a time.

    Both methods are called from the sequencer's event loop. A provider that
    needs I/O (a database, the request API) does it in ``next_observation``,
    which the sequencer bounds with ``plan_timeout``; a timeout or exception
    there counts as that telescope's error. ``report`` must not block.
    """

    async def next_observation(self, telescope: str) -> Observation | Idle | None:
        """What ``telescope`` should do now; :class:`Idle` to wait; or None:
        nothing more tonight."""
        ...

    def report(self, telescope: str, obs: Observation, outcome: str, frames: int) -> None:
        """How ``obs`` ended on ``telescope`` (one of :data:`OUTCOMES`), and
        how many of its frames were taken. (Optionally also ``files=``: see
        the module doc.)"""
        ...


class _Queues:
    """Per-telescope FIFO queues. An observation that did not complete goes
    back to the front of its telescope's queue, so the telescope retries it
    after the enclosure reopens or after its error backoff; the sequencer's
    give-up bounds the retries. It is re-taken whole, or, with
    ``allow_partial``, only its frames not yet taken."""

    def __init__(self, queues: dict[str, Iterable[Observation]]) -> None:
        self.queues = {tel: deque(obs) for tel, obs in queues.items()}
        self.history: list[tuple[str, str, str, int]] = []
        #: Frames written per report that had any: (telescope, name, paths).
        self.files: list[tuple[str, str, list[str]]] = []

    async def next_observation(self, telescope: str) -> Observation | None:
        queue = self.queues.get(telescope)
        return queue.popleft() if queue else None

    async def peek(self, telescope: str) -> Observation | None:
        queue = self.queues.get(telescope)
        return queue[0] if queue else None

    def report(
        self,
        telescope: str,
        obs: Observation,
        outcome: str,
        frames: int,
        files: list[str] | None = None,
    ) -> None:
        self.history.append((telescope, obs.name, outcome, frames))
        if files:
            self.files.append((telescope, obs.name, list(files)))
        if outcome != "complete":
            if obs.allow_partial and 0 < frames < obs.count:
                obs = replace(obs, count=obs.count - frames)
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
    """Bias and dark frames on every telescope, and the spectrograph's
    calibrations, then nothing.

    ``darks`` is a list of ``[exptime, count]``. ``spectrograph`` is a list
    of spectrograph calibration frames, ``{"kind": "thar"|"slitflat"|
    "fiberflat"|"bias"|"dark", "exptime": s, "n": N}``, taken as one
    ``spectrograph_calibration`` observation by ``spectrograph_telescope``
    (default: the first telescope; the telescope itself does nothing).
    Every frame is dark (``light=False``), so the sequencer takes them in its
    closed phase: no lease is requested, the enclosure stays closed and the
    telescopes stay parked (design.md 3.4). The spectrograph's biases and
    darks are true dark frames (shutter closed): legacy MINERVA took them as
    light frames (docs/kiwispec.md, section 3).
    """

    def __init__(
        self,
        telescopes: Sequence[str],
        biases: int = 0,
        darks: Iterable[Sequence[float]] = (),
        spectrograph: Iterable[dict[str, Any]] = (),
        spectrograph_telescope: str | None = None,
    ) -> None:
        frames = [Observation("bias", exptime=0.0, count=biases, light=False)] if biases else []
        for exptime, count in darks:
            name = f"dark{exptime:g}"
            frames.append(Observation(name, exptime=float(exptime), count=int(count), light=False))
        queues = {tel: list(frames) for tel in telescopes}
        spec = [dict(f) for f in spectrograph]
        if spec:
            tel = spectrograph_telescope or next(iter(telescopes), None)
            if tel not in queues:
                raise ValueError(f"calibration plan: unknown spectrograph_telescope {tel!r}")
            for f in spec:
                if f.get("kind") not in ("thar", "slitflat", "fiberflat", "bias", "dark"):
                    raise ValueError(f"calibration plan: spectrograph frame {f!r}: bad kind")
            queues[tel].insert(
                0,
                Observation(
                    "spectrograph calibration",
                    exptime=0.0,
                    light=False,
                    kind="spectrograph_calibration",
                    calibration=spec,
                ),
            )
        super().__init__(queues)


class CallablePlan:
    """Adapt a function ``fn(telescope) -> Observation | None`` (or a
    zero-argument ``fn()``) to :class:`PlanProvider`; reports are kept in
    ``history``."""

    def __init__(self, fn: Callable[..., Observation | None]) -> None:
        self._fn = fn
        self._takes_telescope = bool(inspect.signature(fn).parameters)
        self.history: list[tuple[str, str, str, int]] = []
        self.files: list[tuple[str, str, list[str]]] = []

    async def next_observation(self, telescope: str) -> Observation | None:
        return self._fn(telescope) if self._takes_telescope else self._fn()

    def report(
        self,
        telescope: str,
        obs: Observation,
        outcome: str,
        frames: int,
        files: list[str] | None = None,
    ) -> None:
        self.history.append((telescope, obs.name, outcome, frames))
        if files:
            self.files.append((telescope, obs.name, list(files)))


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
    * ``{"type": "dispatch", "site": {...}, "requests": "requests.json", ...}``
      (:func:`robofast_core.dispatch.dispatch_from_config`).
    """
    config = dict(config)
    config.pop("_note", None)
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
    if kind == "dispatch":
        from robofast_core.dispatch import dispatch_from_config  # imports astropy

        return dispatch_from_config(config, telescopes, base=base)
    raise ValueError(f"plan type {kind!r}: expected 'static', 'calibration' or 'dispatch'")
