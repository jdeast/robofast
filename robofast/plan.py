"""What the sequencer observes: observations and plan providers.

The sequencer asks its :class:`PlanProvider` for each telescope's next
observation whenever that telescope is free -- after the enclosure opens,
after each observation, after an error backoff -- so every choice is made
from scratch, at the time it is used (design.md 3.4: never resume a stale
action). It then reports how each observation ended, so the provider can
mark it done, retry it or drop it.

Providers:

* :class:`~robofast.dispatch.DispatchScheduler` (design.md 3.7) --
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
* ``unsupported(telescope, obs, reason)`` -- called just before ``obs`` is
  reported ``unsupported``: ``telescope`` cannot do it (the reason is the
  sequencer's :class:`~robofast.owner.Unsupported`). The plan must not hand
  ``obs`` to that telescope again (a repeat counts as that telescope's
  error); the dispatch scheduler offers it to the others, or refuses it;
* ``set_capabilities(telescope, caps)`` -- what the telescope can do, read
  from its devices before its first observation (``{"tracking_rates":
  bool}``), so the plan can avoid handing out what would be unsupported;
* a ``files`` keyword on ``report`` -- with the sequencer's frame pipeline
  (:mod:`robofast.imaging`) on, the sequencer passes the observation's
  written frames as paths relative to the data root (a ``complete`` report
  only after they are all on disk). Plans whose ``report`` lacks it are
  called without it.

:func:`plan_from_config` builds any of them from a config entry (see
:mod:`robofast.sequencer_service`).
"""

from __future__ import annotations

import inspect
import json
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from robofast.instruments import calibration_kinds
from robofast.kinds import OBSERVATION_KINDS
from robofast.plugins import Registry, load_config_plugins

#: How an observation ended, as passed to :meth:`PlanProvider.report`.
#: "complete": every frame taken. "interrupted": stopped by the enclosure
#: closing or the sequencer stopping (not its fault; worth retrying).
#: "failed": a device error or a refused slew. "unsupported": this telescope
#: cannot do it (no tracking rates for a moving target, no such filter,
#: port, lamp or guider): not a fault, and not worth retrying on this
#: telescope; a plan with ``unsupported(telescope, obs, reason)`` is told
#: why first (robofast.sequencer, "Several telescopes").
OUTCOMES = ("complete", "interrupted", "failed", "unsupported")


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
    #: Calibration-stage position (the TRES front end's ``calstage``, an
    #: Alpaca FilterWheel by name: docs/tres_frontend.md), selected before
    #: the exposures; None: leave the stage alone. A telescope without a
    #: calstage fails an observation that names one.
    calstage: str | None = None
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
    #: Offset guiding (docs/offset_guiding.md): guide on a star near the
    #: target, held where the target falls on the fiber -- the guider's
    #: ``offset_guide`` (``star``: ``"auto"``, ``{"east", "north"}`` arcsec
    #: of the guide star from the target, or its ``{"ra", "dec", ...}``;
    #: ``target``: ``{"ra", "dec"}`` deg and proper motion; ``site``, for
    #: differential refraction). None: guide on the target itself.
    guide_offset: dict[str, Any] | None = None
    #: A moving target (an OCS ORBITAL_ELEMENTS target; docs/moving_targets.md):
    #: ``orbit`` (:meth:`robofast.orbit.Orbit.to_dict`) and ``site``
    #: (``latitude``, ``longitude`` deg E, ``elevation`` m). The sequencer
    #: points at its place at each exposure's middle and, where the mount
    #: can set them, tracks at its rates. None: a sidereal target.
    follow: dict[str, Any] | None = None
    #: What to do on the target: ``"expose"`` (``count`` frames),
    #: ``"autofocus"`` (focus the telescope there: :mod:`robofast.autofocus`;
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
    #: :meth:`~robofast.request_model.Target.to_dict`).
    spectrum: dict[str, Any] | None = None
    #: For ``kind="spectrograph_calibration"``: the frames, each ``{"kind":
    #: "thar"|"slitflat"|"fiberflat"|"bias"|"dark", "exptime": s, "n": N}``
    #: (the spectrograph service's ``POST /calibrate``).
    calibration: list[dict[str, Any]] | None = None
    #: For ``kind="pointing_model"`` (a pointing run, docs/pointing_model.md:
    #: ``count`` grid points of ``exptime`` each, through ``port``; the
    #: target is only where the scheduler checks visibility): overrides of
    #: the telescope's ``pointing.run`` settings (``min_alt``, ...).
    pointing: dict[str, Any] | None = None
    #: For ``kind="rotator_calibration"`` (docs/rotcal.md: measure the
    #: port's PA and parity on the target's field at several rotator
    #: angles): overrides of the telescope's ``pa_calibration`` settings
    #: (``angles``, ``sense``).
    rotator: dict[str, Any] | None = None
    #: For ``kind="sky_flat"`` (twilight flats, docs/skyflats.md; the target
    #: is computed: opposite the Sun, 75 deg high): overrides of the
    #: telescope's ``sky_flats`` settings (``filters``, ``count``, ...).
    flats: dict[str, Any] | None = None
    #: For ``kind="arc"`` and ``kind="lamp_flat"`` (request types ARC and
    #: LAMP_FLAT; docs/pulizzi.md): the calibration lamp, by its name on the
    #: telescope's lamp switch. The telescope is not moved and no lease is
    #: needed (``light`` is false: no sky light), but the shutter opens: the
    #: sequencer selects the calibration stage, switches the lamp on, waits
    #: its warm-up, takes ``count`` frames and switches it off again.
    lamp: str | None = None
    #: For ``kind="sequence"`` (docs/sequences.md): the name of one of the
    #: enclosure config's ``sequences``, a step list the sequencer runs (the
    #: calibration stage, lamps, waits, frames, ...). Its ``light`` is the
    #: definition's (set by the sequencer when it receives the observation).
    sequence: str | None = None
    #: For ``kind="sequence"``: values for the parameters of the template the
    #: sequence is made from (docs/sequences.md, "Templates"), e.g. an ARC
    #: request's ``{"exptime": 30, "count": 3}``; the sequencer makes the
    #: sequence again with them, checked as at load, before anything moves.
    sequence_params: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        kind = OBSERVATION_KINDS.get(self.kind)
        if kind is None:
            raise ValueError(
                f"observation {self.name!r}: kind {self.kind!r} not in"
                f" {tuple(OBSERVATION_KINDS.names())}"
            )
        kind.validate(self)
        if self.guide_offset is not None:
            if not self.guide:
                raise ValueError(f"observation {self.name!r}: guide_offset without guide")
            if not isinstance(self.guide_offset, dict) or "target" not in self.guide_offset:
                raise ValueError(f"observation {self.name!r}: guide_offset needs its target")
        if self.follow is not None:
            _check_follow(self)
        for f in _KIND_FIELDS:
            if getattr(self, f) is None or f in kind.fields:
                continue
            owners = [k.name for k in OBSERVATION_KINDS.values() if f in k.fields]
            kinds = ", ".join(repr(k) for k in owners)
            raise ValueError(
                f"observation {self.name!r}: {f!r} is for kind{'s' if len(owners) > 1 else ''}"
                f" {kinds}"
            )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Observation:
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"observation {d.get('name')!r}: unknown keys {sorted(unknown)}")
        return cls(**d)


def _check_follow(obs: Observation) -> None:
    """A moving target's ``follow``: a light, unguided ``expose`` whose
    orbit and site parse (so a plan file's mistake fails at load)."""
    from robofast.orbit import Orbit

    what = f"observation {obs.name!r}: follow"
    f = obs.follow
    if not isinstance(f, dict) or set(f) != {"orbit", "site"}:
        raise ValueError(f"{what}: expected {{'orbit', 'site'}}")
    if obs.kind != "expose" or not obs.light or obs.guide:
        raise ValueError(f"{what}: only for an unguided light 'expose'")
    try:
        Orbit(**f["orbit"])
        site = f["site"]
        float(site["latitude"]), float(site["longitude"])
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError(f"{what}: {exc}") from None


#: Observation fields that belong to one kind or a few (each kind's
#: :attr:`~robofast.kinds.ObservationKind.fields`); another kind
#: refuses them. ``autofocus`` is advisory and accepted on any kind.
_KIND_FIELDS = (
    "pointing",
    "flats",
    "rotator",
    "spectrum",
    "lamp",
    "calibration",
    "sequence",
    "sequence_params",
)


@dataclass
class Idle:
    """Returned by :meth:`PlanProvider.next_observation`: nothing to do *now*,
    ask again after ``seconds`` (or sooner, if the sequencer is woken by
    :meth:`~robofast.sequencer.Sequencer.preempt`). The telescope stops
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
        #: Observations a telescope could not do: (telescope, name, reason).
        self.unsupported_log: list[tuple[str, str, str]] = []
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
        if outcome not in ("complete", "unsupported"):  # unsupported: never here again
            if obs.allow_partial and 0 < frames < obs.count:
                obs = replace(obs, count=obs.count - frames)
            self.queues.setdefault(telescope, deque()).appendleft(obs)

    def unsupported(self, telescope: str, obs: Observation, reason: str) -> None:
        self.unsupported_log.append((telescope, obs.name, reason))


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
    of spectrograph calibration frames, ``{"kind": ..., "exptime": s, "n":
    N}``, each kind one a registered spectrograph type takes (its
    :attr:`~robofast.instruments.InstrumentType.calibration_kinds`;
    the spectrograph service refuses kinds it cannot take: MINERVA's
    KiwiSpec takes only biases and darks here, docs/kiwispec.md section 4),
    taken as one
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
        sequences: Iterable[str | dict[str, Any]] = (),
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
            kinds = calibration_kinds()
            for f in spec:
                if f.get("kind") not in kinds:
                    raise ValueError(
                        f"calibration plan: spectrograph frame {f!r}: bad kind"
                        f" (the spectrograph types take {list(kinds)})"
                    )
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
        for i, entry in enumerate(sequences):
            # A config-defined sequence (docs/sequences.md): a name (on every
            # telescope), or {"sequence": name, "telescopes": [...]}; the
            # sequencer service checks the names and that each is dark.
            if isinstance(entry, str):
                entry = {"sequence": entry}
            if not isinstance(entry, dict) or set(entry) - {"sequence", "telescopes"}:
                raise ValueError(
                    f"calibration plan: sequences[{i}]: a name or {{'sequence', 'telescopes'}}"
                )
            name = entry.get("sequence")
            if not isinstance(name, str) or not name:
                raise ValueError(f"calibration plan: sequences[{i}]: a sequence name")
            on = entry.get("telescopes") or list(telescopes)
            for tel in on:
                if tel not in queues:
                    raise ValueError(f"calibration plan: sequences[{i}]: unknown telescope {tel!r}")
                queues[tel].append(
                    Observation(name, exptime=0.0, light=False, kind="sequence", sequence=name)
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


#: ``factory(config, telescopes, *, base, clock) -> PlanProvider``: builds
#: a plan of one type from its config entry (less ``type``, ``plugins``
#: and ``_note``).
PlanFactory = Callable[..., PlanProvider]
#: Every plan type, by name (docs/plugins.md).
PLAN_TYPES: Registry[PlanFactory] = Registry("plan type")


def _static(
    config: dict[str, Any],
    telescopes: Sequence[str],
    *,
    base: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PlanProvider:
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


def _calibration(
    config: dict[str, Any],
    telescopes: Sequence[str],
    *,
    base: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PlanProvider:
    return CalibrationPlan(telescopes, **config)


def _dispatch(
    config: dict[str, Any],
    telescopes: Sequence[str],
    *,
    base: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PlanProvider:
    from robofast.dispatch import dispatch_from_config  # imports astropy

    return dispatch_from_config(config, telescopes, base=base, clock=clock)


PLAN_TYPES.register("static", _static)
PLAN_TYPES.register("calibration", _calibration)
PLAN_TYPES.register("dispatch", _dispatch)


def plan_from_config(
    config: dict[str, Any],
    telescopes: Sequence[str],
    base: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PlanProvider:
    """Build a plan from a config entry: ``type`` names a plan type of
    :data:`PLAN_TYPES` (``plugins``, a list of plug-in modules, are loaded
    first: docs/plugins.md). The built-in types:

    * ``{"type": "static", "targets": [...]}``, or ``"file": "targets.json"``
      (a JSON list of targets, relative to ``base``). A target is an
      :class:`Observation`'s fields plus an optional ``telescopes`` list.
    * ``{"type": "calibration", "biases": 10, "darks": [[60, 5]]}``.
    * ``{"type": "dispatch", "site": {...}, "requests": "requests.json", ...}``
      (:func:`robofast.dispatch.dispatch_from_config`; ``clock`` is
      its clock, :mod:`robofast.clock`).
    """
    config = dict(config)
    config.pop("_note", None)
    load_config_plugins(config, "plan.plugins")
    config.pop("plugins", None)
    kind = config.pop("type", None)
    factory = PLAN_TYPES.lookup(kind, "plan.type")
    return factory(config, telescopes, base=base, clock=clock)
