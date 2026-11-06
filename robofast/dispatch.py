"""The dispatch scheduler: the best observable request for a telescope, now.

:class:`DispatchScheduler` is a :class:`~robofast.plan.PlanProvider`
(design.md 3.7). Whenever a telescope is free the sequencer asks it for the
next observation, and it decides from scratch, RTS2/POCS-style:

1. **Hard constraints** (every one must hold, or the request is excluded):

   * the telescope may take it: it is in the request's ``telescopes``
     (``location.telescope`` or ``extra_params.telescopes``, if given) and
     has every configuration's ``instrument_type``;
   * the request is schedulable: some configuration is ``PENDING`` or
     ``ATTEMPTED``, and no other telescope is in the middle of it;
   * time: the whole visit (``now`` to ``now + duration``) fits in one of
     the request's windows; a ToO has not passed its deadline;
   * cadence (standing requests): at least ``min_interval`` since the last
     completed visit started, and fewer than ``max_per_night`` visits since
     local noon (of the request, or of all requests sharing its ``pool``),
     unless the cadence's ``limit`` is ``"soft"`` (step 3);
   * the Sun is below ``max_sun_alt`` (twilight), for anything with light
     frames;
   * for each light configuration, at the start, middle and end of its part
     of the visit: altitude within the telescope's ``[min_alt, max_alt]``,
     airmass at most ``max_airmass``; hour angle within ``[min_ha, max_ha]``
     at its start and end; and the Moon at least ``min_moon_sep`` away
     (a configuration's ``min_lunar_distance``) and, if the configuration
     sets ``max_lunar_phase``, at most that fraction illuminated; if it
     sets ``max_seeing``, the seeing now (the plan's seeing source,
     docs/seeing.md: by default the guiders' live FWHM) at most that.
     A configuration's ``constraints`` can only tighten these limits.

   **An error is not "observable".** An exception while evaluating a request
   excludes that request (and is logged, and kept in
   :attr:`DispatchScheduler.errors`); a non-finite altitude or separation
   fails its constraint. Nothing defaults to observable (the legacy
   scheduler gave a target whose window computation raised a 100-year
   window).

2. **Score** of each request that passes, using its first remaining
   configuration's target (all bonuses are in [0, 1])::

       score = priority
             + w_setting  * max(0, 1 - t_left / setting_horizon)
             + w_airmass  * airmass_at_transit / airmass_now
             + w_cadence  * min(1, max(0, (t_since_last - min_interval) / min_interval))

   ``priority`` is the request group's ``ipp_value``. ``t_left`` is the time until the
   target can no longer be observed: it sinks below ``min_alt``, reaches
   ``max_ha``, its window ends, or (a ToO) its deadline passes -- so a
   target about to be lost gets up to ``w_setting`` extra. The airmass term
   favours a target near its best altitude of the night. The cadence term
   favours a standing request that is overdue (a request never observed
   gets the full bonus). With the default weights (1, 1, 1) the bonuses
   break ties between priorities within about 3; scale ``priority``
   accordingly (legacy MINERVA priorities are 1-10).

3. **Ranking**: by priority class first (a ToO's ``priority_class``, 0 for
   normal requests), then by score; ties by request id, so a decision is
   reproducible. A request that has reached a *soft* ``max_per_night``
   (cadence ``"limit": "soft"``) is still observable but *demoted*: it ranks
   below every request that is not, whatever its class or score, so it is
   chosen only when the telescope would otherwise idle (legacy MINERVA
   divided a B star's weight by 10^6 once one was observed, "unless we have
   nothing better to do").

The visit is then executed: one :class:`~robofast.plan.Observation`
per instrument config, configurations in order. Between the observations of
a visit the remaining part is re-checked against the hard constraints (and
abandoned if it no longer passes, or if a ToO outranks it); it is not
re-scored.

Reports (:meth:`DispatchScheduler.report`): ``complete`` advances the visit
(and, at its end, records it in the history); ``interrupted`` marks the
configuration ``ATTEMPTED`` and drops the visit, so it is re-queued; ``failed``
counts towards ``max_attempts`` (then ``FAILED``). An interrupted or failed
observation is re-taken whole, unless its configuration has
``allow_partial``: then the frames already taken are kept (in the state
file), and the next visit takes only the rest of that configuration.

Filters and M3 ports are hard constraints too: a configuration naming a
``port`` is only for telescopes whose limits list it in ``ports`` (default:
none, so it is never sent to a telescope that would observe through the
wrong port), and one naming a ``filter`` only for telescopes whose
``filters`` (if given) include it, and one naming a calibration-stage
position (``optical_elements.calstage``) only for telescopes whose
``calstage`` lists it, and an ``ARC`` or ``LAMP_FLAT`` naming a lamp
(``optical_elements.lamp``) only for telescopes whose ``lamps`` list it
(none by default: MINERVA has no lamps). The sequencer then selects them
(and, for a lamp, switches it on and off: docs/pulizzi.md).

Spectra fed by several telescopes
---------------------------------
A ``SPECTRUM`` configuration (:mod:`robofast.request_model`) is taken
with the enclosure's fiber-fed spectrograph. The telescope that picks it
*leads* it: its observation (``kind="spectrum"``) names a ``feed`` -- itself
plus every other telescope the request allows that is free (not in a visit
of its own) and passes the hard constraints for it now. Each of those is
*reserved* for it: when it next asks, it gets the same spectrum as a member
(``spectrum["leader"]`` names the leader), and an idle one is woken through
the sequencer's ``preempt`` (policy ``next``, nothing is interrupted). The
sequencer starts the exposure once the feed is guiding (or after its ready
timeout, with those that are). Only the leader's report changes the
request's status; a member's report just ends its reservation, as does the
leader's report or the leader moving on.

A telescope under an operator hold (:meth:`DispatchScheduler.set_held`, from
the observer console's pause, stop or park) is never reserved to feed a
spectrum, and a reservation it has is dropped when the hold arrives, so a
leader does not wait for it. While the sequencer has marked the
spectrograph down (:meth:`DispatchScheduler.set_spectrograph_available`;
docs/rv_night.md), no SPECTRUM configuration is scheduled; imaging requests
go on. Both changes are written to the plan record (``held``,
``spectrograph``) and kept in its snapshots, so a replay sees them.


``idle_poll``), not "done": the night may bring a rising target or a ToO.
The night is over (``None``) once the Sun is above ``max_sun_alt`` and
rising. :meth:`DispatchScheduler.peek` answers the same question without
handing anything out (the sequencer asks it while the enclosure is closed).

The nightly plan record
-----------------------
With ``record`` set (a :class:`~robofast.plan_record.PlanRecord`), every
answer and report is written as one JSON line, one file per night: each
decision with its score terms, the best runners-up and the top rejected
candidates with the constraints they failed; idle answers with what was
rejected; visits continued or abandoned; ToOs received and cancelled;
preemptions; outcomes. The first line a process writes to a night's file is
a snapshot of the scheduler (requests, state, limits, settings), so
:func:`robofast.plan_record.replay` can re-run the night's decisions.

Targets of opportunity (design.md 3.7.1)
----------------------------------------
:meth:`DispatchScheduler.submit_too` (or a JSON file dropped into
``too_dropbox``, then rung: ``python -m robofast.doorbell ring <dropbox>``)
adds a request group whose ``observation_type`` is ``RAPID_RESPONSE`` or
``TIME_CRITICAL`` (its ``too``: deadline, policy, class;
:mod:`robofast.request_model`). A ToO ranks
above every normal request; it is taken only when it passes the hard
constraints; it stays pending until then, and expires at its deadline. When
it arrives (a dropped file is taken in at once: its doorbell), and every
``too_poll`` seconds while it waits, the scheduler
looks for a telescope that can take it now:

* an eligible telescope that is idle is woken (no interruption);
* otherwise, with policy ``now-after-readout`` or ``now``, the eligible
  telescope whose current observation has the lowest (class, score) below
  the ToO's class is preempted through the sequencer's
  :meth:`~robofast.sequencer.Sequencer.preempt`, and the ToO is
  reserved for it for ``reserve_timeout``;
* with policy ``next`` nothing is interrupted: the first eligible telescope
  to finish its observation takes it.

Submissions and the drop box
----------------------------
:meth:`DispatchScheduler.submit` adds any request group at run time (a ToO
goes through :meth:`~DispatchScheduler.submit_too`). The drop box is the
scheduler's intake from other processes (the request API,
:mod:`robofast.request_api`, and operators): each ``*.json`` file is a
request group (ToO or not), or ``{"cancel": "<group id>"}``; files are taken
in name order. :meth:`~DispatchScheduler.cancel` marks every schedulable
configuration ``CANCELED``; an observation in progress finishes, and its
visit then ends.

State
-----
With ``state`` set, the per-configuration status, the history of completed
visits (for cadence and nightly counts), the groups received at run time
(ToOs and submissions) and a record of each completed observation (for data
pointers) are kept in that JSON file (written atomically after every
change), so they survive a restart. A corrupt file is moved aside
(``*.corrupt-<time>``) with an error logged, and the scheduler starts from
the request file alone. Other processes may read it
(:meth:`DispatchScheduler.restore_state` applies one to a scheduler built
from the same plan entry) but never write it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from robofast import sky
from robofast.doorbell import Doorbell
from robofast.instruments import InstrumentType, instrument_param, resolve
from robofast.kinds import RequestMapping
from robofast.plan import Idle, Observation
from robofast.plan_record import PlanRecord
from robofast.plugins import load_config_plugins
from robofast.request_model import (
    Configuration,
    Request,
    RequestGroup,
    Status,
    import_requests,
    load_request_groups,
)

__all__ = [
    "Candidate",
    "DispatchConfig",
    "DispatchScheduler",
    "TelescopeLimits",
    "dispatch_from_config",
]

log = logging.getLogger(__name__)

#: A callback ``(telescope, policy, reason) -> accepted`` (the sequencer's
#: :meth:`~robofast.sequencer.Sequencer.preempt`).
PreemptCallback = Callable[[str, str, str], bool]


_ILLUMINATION: dict[datetime, float] = {}


def _moon_illumination(sky_now: sky.SkyNow) -> float:
    """The Moon's illuminated fraction at ``sky_now.when`` (0 new, 1 full),
    from its elongation from the Sun; cached per instant (only configurations
    with ``max_lunar_phase`` need it)."""
    when = sky_now.when
    if when not in _ILLUMINATION:
        from astropy.coordinates import angular_separation, get_body
        from astropy.time import Time

        try:
            sun, moon = get_body("sun", Time(when)), sky_now.moon
            # (Plain angles: the Moon's topocentric GCRS against the Sun's
            # geocentric differ by under a degree, which is negligible here.)
            elong = float(angular_separation(moon.ra, moon.dec, sun.ra, sun.dec).to_value("rad"))
            _ILLUMINATION[when] = (1.0 - math.cos(elong)) / 2.0
        except Exception as exc:  # a NaN fails the constraint
            log.error("Moon illumination at %s: %r", when, exc)
            _ILLUMINATION[when] = math.nan
        while len(_ILLUMINATION) > 64:
            _ILLUMINATION.pop(next(iter(_ILLUMINATION)))
    return _ILLUMINATION[when]


@dataclass
class TelescopeLimits:
    """What one telescope can do. ``instruments``, ``min_alt`` and
    ``max_alt`` are site values and have no defaults: a dispatch plan must
    give them for every telescope (docs/design.md 3.10)."""

    #: Keys a dispatch plan's telescope entry must give.
    REQUIRED: ClassVar[tuple[str, ...]] = ("instruments", "min_alt", "max_alt")

    instruments: list[str]
    min_alt: float
    max_alt: float
    #: Hour angle limits, hours.
    min_ha: float = -12.0
    max_ha: float = 12.0
    #: M3 ports it can select (the sequencer's ``port_action``); a
    #: configuration naming another port is not for this telescope.
    ports: list[str] = field(default_factory=list)
    #: Its filters; None: not checked here (the sequencer checks the wheel).
    filters: list[str] | None = None
    #: Its calibration-stage positions (the TRES front end's ``calstage``);
    #: a configuration naming another is not for this telescope.
    calstage: list[str] = field(default_factory=list)
    #: Its calibration lamps (names on its lamp switch: docs/pulizzi.md); an
    #: ARC or LAMP_FLAT naming another is not for this telescope. Empty (the
    #: default, and MINERVA): no lamp calibrations at all.
    lamps: list[str] = field(default_factory=list)
    #: The enclosure config's sequences an ARC or LAMP_FLAT request may name
    #: here (``extra_params.sequence``; docs/sequences.md, "Requests");
    #: checked against that config when the sequencer and the request API
    #: start. Empty (the default): none.
    sequences: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any], what: str) -> TelescopeLimits:
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"{what}: unknown keys {sorted(unknown)}")
        missing = [k for k in cls.REQUIRED if k not in d]
        if missing:
            raise ValueError(f"{what}: missing required key(s) {', '.join(missing)}")
        limits = cls(**d)
        if limits.min_alt >= limits.max_alt or limits.min_ha >= limits.max_ha:
            raise ValueError(f"{what}: empty altitude or hour-angle range")
        return limits


@dataclass
class DispatchConfig:
    #: Sun altitude limit, deg (legacy HORIZON).
    max_sun_alt: float = -12.0
    #: Moon separation limit, deg (legacy MINMOONSEP).
    min_moon_sep: float = 10.0
    #: Time model for a visit's duration: per light configuration (slew and
    #: acquisition), and per frame on top of its exposure time (readout).
    slew_overhead: float = 120.0
    frame_overhead: float = 10.0
    #: Score weights and the setting horizon (s); see the module doc.
    w_setting: float = 1.0
    w_airmass: float = 1.0
    w_cadence: float = 1.0
    setting_horizon: float = 7200.0
    #: Idle: ask again after this long when nothing is observable.
    idle_poll: float = 60.0
    #: How often the background task re-checks waiting ToOs (the drop box
    #: is taken in as each file arrives).
    too_poll: float = 10.0
    #: Where the drop box's doorbell listens (robofast.doorbell): loopback,
    #: a port of the system's choosing, unless the request API runs on
    #: another host (then an interface it reaches, and a fixed port).
    doorbell_listen: str = "127.0.0.1:0"
    #: A ToO stays reserved for the telescope it preempted this long.
    reserve_timeout: float = 60.0
    #: Completed visits kept per request in the state file.
    history_size: int = 200
    #: Runners-up and rejected candidates written per decision in the record.
    record_top_n: int = 5


@dataclass
class Candidate:
    """One request as evaluated for one telescope (for logs and tests)."""

    request: Request
    ok: bool
    score: float = -math.inf
    #: Why it was excluded (empty when ``ok``).
    reasons: list[str] = field(default_factory=list)
    #: The score's terms.
    terms: dict[str, float] = field(default_factory=dict)
    #: Why it is demoted (it reached a soft ``max_per_night``); "" if not.
    demoted: str = ""


def _rank(c: Candidate) -> tuple[bool, int, float, str]:
    """Sort key, best first: not demoted, then highest class, then highest
    score, then id."""
    assert c.request.group is not None
    return (bool(c.demoted), -c.request.group.priority_class, -c.score, c.request.id)


@dataclass
class _Visit:
    """A request a telescope is executing."""

    request: Request
    configs: list[Configuration]
    started: datetime
    cfg: int = 0
    ic: int = 0
    #: Frames of the current instrument config already taken (allow_partial).
    done: int = 0
    obs: Observation | None = None
    priority_class: int = 0
    score: float = 0.0
    #: Per configuration id: how it is guided (``on``, and for ``guide:
    #: "auto"`` the recommendation), decided once a visit.
    guide: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def config(self) -> Configuration:
        return self.configs[self.cfg]


@dataclass
class _TooState:
    reserved_for: str | None = None
    reserved_until: datetime | None = None


class DispatchScheduler:
    """See the module doc.

    ``groups``: the request groups (normal requests; ToOs may be included
    too). ``telescopes``: limits per telescope name. ``clock`` returns the
    current time (aware UTC); tests inject a fixed one.
    """

    def __init__(
        self,
        groups: Iterable[RequestGroup],
        site: sky.Site,
        telescopes: dict[str, TelescopeLimits],
        *,
        config: DispatchConfig | None = None,
        state: str | Path | None = None,
        too_dropbox: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        allow_iers_download: bool = False,
        record: PlanRecord | None = None,
        vetter: Any = None,
        instruments: dict[str, Any] | None = None,
        seeing: Any = None,
    ) -> None:
        if not allow_iers_download:
            sky.offline_iers()
        self.site = site
        self.limits = dict(telescopes)
        #: Each instrument name the telescopes list -> its type
        #: (:mod:`robofast.instruments`): ``instruments`` declares
        #: ``{name: {"type": ...}}``; an undeclared name is its own type.
        self.instrument_types: dict[str, InstrumentType] = resolve(
            instruments or {},
            dict.fromkeys(n for lim in self.limits.values() for n in lim.instruments),
            "dispatch plan: instruments",
        )
        #: Instruments' guiding models (``instruments.<name>.guiding``), for
        #: ``guide: "auto"`` (robofast.guider.recommend).
        self.guide_models: dict[str, Any] = {}
        self._instrument_conf = dict(instruments or {})
        for name, d in self._instrument_conf.items():
            if isinstance(d, dict) and d.get("guiding") is not None:
                from robofast.guider.recommend import GuideModel

                try:
                    self.guide_models[name] = GuideModel.from_dict(d["guiding"])
                except ValueError as exc:
                    raise ValueError(f"dispatch plan: instruments.{name}.guiding: {exc}") from None
        self.config = config or DispatchConfig()
        #: The enclosure config's sequences the telescopes' ``sequences``
        #: name (robofast.sequences.check_request_sequences sets it):
        #: a request naming one is checked against its frame type.
        self.sequences: dict[str, Any] = {}
        self.clock = clock or (lambda: datetime.now(UTC))
        #: Where the seeing comes from (robofast.seeing; docs/seeing.md):
        #: ``guide: "auto"`` and ``max_seeing``. Default: the guiders.
        if seeing is None:
            from robofast.seeing import seeing_from_config

            seeing = seeing_from_config(None, self.clock)
        self.seeing = seeing
        self.state_path = Path(state) if state is not None else None
        self.dropbox = Path(too_dropbox) if too_dropbox is not None else None
        self.groups: dict[str, RequestGroup] = {}
        self.requests: dict[str, Request] = {}
        self.visits: dict[str, list[datetime]] = {}
        #: (time, request id, error) of every evaluation that raised.
        self.errors: list[tuple[datetime, str, str]] = []
        #: Every report: (telescope, observation name, outcome, frames).
        self.history: list[tuple[str, str, str, int]] = []
        #: Decisions, for inspection: (time, telescope, request id or None, score).
        self.decisions: list[tuple[datetime, str, str | None, float]] = []
        self._current: dict[str, _Visit] = {}
        #: Spectrum feed reservations: member telescope -> (leader, the
        #: leader's observation).
        self._members: dict[str, tuple[str, Observation]] = {}
        self._idle: set[str] = set()
        self._too: dict[str, _TooState] = {}
        self._preempt: PreemptCallback | None = None
        #: allow_partial progress: configuration id -> (instrument config,
        #: frames of it taken).
        self._progress: dict[str, tuple[int, int]] = {}
        #: The nightly plan record (module doc), or None.
        self.record = record
        #: The enclosure's operating mode (set_mode; None: not told).
        self.mode: str | None = None
        #: Normal groups added at run time (:meth:`submit`); kept in the state.
        self._submitted: set[str] = set()
        #: Completed observations by configuration id (for data pointers).
        self.observed: dict[str, list[dict[str, Any]]] = {}
        #: Telescopes under an operator hold (the observer console's pause,
        #: stop or park; :meth:`set_held`): never reserved to feed a spectrum.
        self.held: set[str] = set()
        #: Why the spectrograph is down (:meth:`set_spectrograph_available`),
        #: or None: while it is down no SPECTRUM configuration is scheduled.
        self.spectrograph_down: str | None = None
        #: Target vetting at intake (:class:`robofast.vetting.Vetter`,
        #: docs/sky.md), and its results by group id.
        self.vetter = vetter
        self.vetting: dict[str, list[dict[str, Any]]] = {}
        for g in groups:
            self._add(g)
        if self.state_path is not None:
            self._load_state()

    # -- the plan-provider interface ----------------------------------------

    def set_preempt_callback(self, fn: PreemptCallback) -> None:
        self._preempt = fn

    def set_held(self, telescopes: Iterable[str]) -> None:
        """The telescopes under an operator hold now (the observer console,
        through :class:`~robofast.console.hold.OperatorHold`). A held
        telescope is not reserved to feed a spectrum, and a reservation it
        has is dropped."""
        held = {t for t in telescopes if t in self.limits}
        if held == self.held:
            return
        self.held = held
        for tel in held:
            self._members.pop(tel, None)
        self._record(self.clock(), "held", telescopes=sorted(held))

    def set_mode(self, mode: str | None, policy: dict[str, Any] | None = None) -> None:
        """The enclosure's operating mode, as the sequencer reads it
        (docs/modes.md): recorded (a ``mode`` entry) and stamped on every
        later entry of the plan record. The scheduler itself does not
        change: the sequencer decides what is asked of it."""
        if mode == self.mode:
            return
        self.mode = mode
        self._record(self.clock(), "mode", observe=(policy or {}).get("observe"))

    def set_spectrograph_available(self, available: bool, reason: str = "") -> None:
        """The sequencer's verdict on the spectrograph (module doc): while it
        is down, SPECTRUM configurations are not scheduled (imaging goes on)."""
        down = None if available else (reason or "down")
        if down == self.spectrograph_down:
            return
        self.spectrograph_down = down
        log.warning("spectrograph %s", "available again" if down is None else f"down: {down}")
        self._record(self.clock(), "spectrograph", available=available, reason=reason)

    def bind_values(self, registry: Any) -> bool:
        """The sequencer's value registry: the seeing source collects the
        guiders' FWHM from it (docs/seeing.md). True: the source wants the
        guiders' values published there."""
        return self.seeing.bind(registry)

    def seeing_now(self, now: datetime | None = None) -> Any:
        """The seeing source's estimate now (with the configured fallback),
        or None (:class:`robofast.seeing.Seeing`)."""
        return self.seeing.current(now or self.clock())

    def _seeing_record(self, now: datetime) -> dict[str, Any]:
        est = self.seeing_now(now)
        return {"seeing": None if est is None else est.as_json()}

    async def next_observation(self, telescope: str) -> Observation | Idle | None:
        now = self.clock()
        self._expire(now)
        member = self._member_observation(telescope)
        if member is not None:
            self._idle.discard(telescope)
            assert member.spectrum is not None
            lead = self._current[member.spectrum["leader"]]
            self._record(
                now,
                "join",
                telescope=telescope,
                request=lead.request.id,
                config=lead.config.id,
                leader=member.spectrum["leader"],
                observation=member.name,
                spectrum=member.spectrum["id"],
            )
            return member
        sky_now = sky.sky_now(self.site, now)
        visit = self._current.get(telescope)
        if visit is not None:
            why = self._continue_check(telescope, visit, now, sky_now)
            if why is None:
                obs = self._observation(visit)
                self._feed(telescope, visit, obs, now, sky_now)
                self._record(
                    now,
                    "continue",
                    telescope=telescope,
                    request=visit.request.id,
                    config=visit.config.id,
                    observation=obs.name,
                )
                self._wake_feed(obs)
                return obs
            self._abandon(telescope, visit, why, now)
        if sky_now.sun_alt > self.config.max_sun_alt and sky_now.sun_rising:
            self._idle.discard(telescope)
            log.info(
                "%s: the Sun is up (%.1f deg) and rising: night over", telescope, sky_now.sun_alt
            )
            self._record(now, "night_over", telescope=telescope, sun_alt=sky_now.sun_alt)
            return None
        best, cands = self._best(telescope, now, sky_now)
        if best is None:
            self._idle.add(telescope)
            self.decisions.append((now, telescope, None, -math.inf))
            self._record(
                now,
                "idle",
                telescope=telescope,
                **self._seeing_record(now),
                **self._summary(None, cands),
            )
            return Idle(self.config.idle_poll, "nothing observable")
        self._idle.discard(telescope)
        req = best.request
        visit = self._new_visit(req, now, best.score)
        self._current[telescope] = visit
        self._too.pop(req.id, None)
        self.decisions.append((now, telescope, req.id, best.score))
        log.info(
            "%s: %s (score %.3f: %s)%s",
            telescope,
            req.id,
            best.score,
            ", ".join(f"{k}={v:.3f}" for k, v in best.terms.items()),
            f" -- demoted ({best.demoted}), nothing else observable" if best.demoted else "",
        )
        obs = self._observation(visit)
        self._feed(telescope, visit, obs, now, sky_now)
        self._record(
            now,
            "decision",
            telescope=telescope,
            request=req.id,
            config=visit.config.id,
            observation=obs.name,
            priority_class=req.group.priority_class,
            score=best.score,
            terms=best.terms,
            **self._guide_record(visit, obs),
            **self._seeing_record(now),
            **({"demoted": best.demoted} if best.demoted else {}),
            **self._summary(best, cands),
        )
        self._wake_feed(obs)
        return obs

    async def peek(self, telescope: str) -> Observation | Idle | None:
        """What :meth:`next_observation` would answer now, without handing
        it out, recording it or changing any state."""
        now = self.clock()
        member = self._member_observation(telescope)
        if member is not None:
            return member
        sky_now = sky.sky_now(self.site, now)
        visit = self._current.get(telescope)
        if visit is not None and self._continue_check(telescope, visit, now, sky_now) is None:
            return self._make_observation(visit)
        if sky_now.sun_alt > self.config.max_sun_alt and sky_now.sun_rising:
            return None
        best, _ = self._best(telescope, now, sky_now)
        if best is None:
            return Idle(self.config.idle_poll, "nothing observable")
        return self._make_observation(self._new_visit(best.request, now, best.score))

    def report(
        self,
        telescope: str,
        obs: Observation,
        outcome: str,
        frames: int,
        files: list[str] | None = None,
    ) -> None:
        self.history.append((telescope, obs.name, outcome, frames))
        if obs.spectrum is not None and obs.spectrum.get("leader") not in (None, telescope):
            # A member of another telescope's spectrum: only its reservation ends.
            held = self._members.get(telescope)
            if held is not None and held[1].spectrum is not None:
                if held[1].spectrum["id"] == obs.spectrum["id"]:
                    del self._members[telescope]
            self._record(
                self.clock(),
                "member_outcome",
                telescope=telescope,
                leader=obs.spectrum["leader"],
                observation=obs.name,
                outcome=outcome,
                frames=frames,
            )
            return
        visit = self._current.get(telescope)
        if visit is None or visit.obs is not obs:
            log.warning("%s: report for an observation not handed out: %s", telescope, obs.name)
            return
        self._release_members(telescope)
        config = visit.config
        self._record(
            self.clock(),
            "outcome",
            telescope=telescope,
            request=visit.request.id,
            config=config.id,
            observation=obs.name,
            outcome=outcome,
            frames=frames,
        )
        if outcome == "complete":
            done = self.observed.setdefault(config.id, [])
            done.append(
                {
                    "time": self.clock().isoformat(),
                    "telescope": telescope,
                    "observation": obs.name,
                    "instrument_config": visit.ic,
                    "frames": frames,
                    # Written frames, relative to the data root (the
                    # sequencer's frame pipeline; absent without it).
                    **({"files": list(files)} if files else {}),
                }
            )
            del done[: -self.config.history_size]
            visit.ic, visit.done = visit.ic + 1, 0
            if visit.ic >= len(config.instrument_configs):
                self._progress.pop(config.id, None)
                visit.cfg, visit.ic = visit.cfg + 1, 0
                if config.status.schedulable:  # not if canceled meanwhile
                    config.status = Status.ATTEMPTED if visit.request.standing else Status.COMPLETED
                if visit.cfg >= len(visit.configs):
                    self._visit_done(telescope, visit)
                else:
                    self._enter_config(visit)
            elif config.allow_partial:
                self._progress[config.id] = (visit.ic, 0)
            visit.obs = None
        else:
            if config.allow_partial:
                self._progress[config.id] = (visit.ic, visit.done + max(0, frames))
            if outcome == "failed" and config.status.schedulable:
                config.failures += 1
                if config.failures >= visit.request.max_attempts:
                    config.status = Status.FAILED
                    self._progress.pop(config.id, None)
                    log.error(
                        "%s: %s failed %d times: FAILED", telescope, config.id, config.failures
                    )
            if config.status.schedulable:
                config.status = Status.ATTEMPTED
            del self._current[telescope]
            log.info("%s: %s %s after %d frames", telescope, config.id, outcome, frames)
        self._save_state()

    # -- targets of opportunity ----------------------------------------------

    async def submit_too(self, group: RequestGroup | dict[str, Any]) -> str:
        """Add a target of opportunity; returns its group id. Raises
        :class:`ValueError` if it is invalid or its id is taken."""
        if isinstance(group, dict):
            group = RequestGroup.from_dict(group)
        if group.too is None:
            raise ValueError(
                f"request group {group.id!r} is not a ToO (observation_type RAPID_RESPONSE"
                " or TIME_CRITICAL)"
            )
        now = self.clock()
        if group.submitted is None:
            group.submitted = now
        self._vet(group, now)
        self._add(group)
        self._record(now, "too", group=group.to_dict())
        log.warning(
            "ToO %s received: class %d, policy %s, deadline %s",
            group.id,
            group.too.priority_class,
            group.too.policy,
            group.too.deadline.isoformat(),
        )
        self._save_state()
        self.check_toos()
        return group.id

    async def submit(self, group: RequestGroup | dict[str, Any]) -> str:
        """Add a request group (a ToO goes to :meth:`submit_too`); returns
        its id. Raises :class:`ValueError` if it is invalid or an id is
        taken. Kept in the state file."""
        if isinstance(group, dict):
            group = RequestGroup.from_dict(group)
        if group.too is not None:
            return await self.submit_too(group)
        if group.submitted is None:
            group.submitted = self.clock()
        self._vet(group, self.clock())
        self._add(group)
        self._submitted.add(group.id)
        self._record(self.clock(), "submit", group=group.to_dict())
        log.info("request group %s received (%s)", group.id, group.submitter or "no submitter")
        self._save_state()
        return group.id

    def _vet(self, group: RequestGroup, now: datetime) -> None:
        """Vet a group at intake (docs/sky.md): the result goes into the plan
        record; a flag the vetting config rejects raises ValueError (the
        group is refused, and the drop box files the reason)."""
        if self.vetter is None:
            return
        try:
            results = self.vetter.vet_group(group, now)
        except Exception as exc:  # vetting never blocks intake by accident
            log.error("vetting %s failed: %r", group.id, exc)
            return
        found = [r.as_dict() for r in results]
        self.vetting[group.id] = found
        self._record(now, "vetting", group=group.id, results=found)
        for r in results:
            for f in r.flags:
                log.log(
                    logging.ERROR if f.severity == "error" else logging.WARNING,
                    "vetting %s %s %s: %s",
                    group.id,
                    r.target,
                    f.code,
                    f.message,
                )
        bad = [
            f"{r.target} [{r.port}] {f.code}: {f.message}"
            for r in results
            for f in r.flags
            if f.severity == "error"
        ]
        if bad:
            raise ValueError("vetting: " + "; ".join(bad))

    def acquisition(self, group_id: str, target: str) -> dict[str, Any] | None:
        """What vetting said about acquiring ``target`` (FAU port)."""
        for r in self.vetting.get(group_id, []):
            if r["port"].split("/")[-1] == "fau" and r["target"].startswith(target):
                codes = [f["code"] for f in r["flags"]]
                return {"unambiguous": bool(r["unambiguous"]), "flags": codes}
        return None

    def _acquisition_entry(self, visit: _Visit, c: Configuration) -> dict[str, Any]:
        group = visit.request.group
        acq = None if group is None else self.acquisition(group.id, c.target.name)
        if c.acquisition:
            acq = {**(acq or {}), **c.acquisition}
        return {} if acq is None else {"acquisition": acq}

    def cancel(self, group_id: str) -> None:
        """Cancel every schedulable configuration of a group (KeyError if
        unknown). An observation in progress finishes; its visit then ends
        (:meth:`_continue_check`)."""
        group = self.groups.get(group_id)
        if group is None:
            raise KeyError(f"unknown request group {group_id!r}")
        for c in group.configurations():
            if c.status.schedulable:
                c.status = Status.CANCELED
        self._record(self.clock(), "cancel", group=group_id)
        log.warning("request group %s canceled", group_id)
        self._save_state()

    def current(self) -> dict[str, dict[str, Any]]:
        """What each busy telescope is doing: request, configuration,
        observation and visit start (for status reports). A telescope
        feeding another's spectrum shows the leader's, with ``leader``."""
        out = {
            tel: {
                "group": v.request.group.id if v.request.group else None,
                "request": v.request.id,
                "configuration": v.config.id if v.cfg < len(v.configs) else None,
                "observation": None if v.obs is None else v.obs.name,
                "started": v.started.isoformat(),
                "priority_class": v.priority_class,
            }
            for tel, v in self._current.items()
        }
        for tel, (leader, obs) in self._members.items():
            if tel not in out and leader in out and obs.name == out[leader]["observation"]:
                out[tel] = {**out[leader], "leader": leader}
        return out

    def check_toos(self) -> None:
        """Expire ToOs past their deadline; for each one now observable, wake
        an idle telescope or preempt a busy one (module doc)."""
        now = self.clock()
        self._expire(now)
        pending = [
            r
            for r in self.requests.values()
            if r.group.too is not None and not r.done and not self._in_progress(r)
        ]
        if not pending:
            return
        sky_now = sky.sky_now(self.site, now)
        pending.sort(key=lambda r: (-r.group.priority_class, r.group.too.deadline, r.id))
        # A telescope already holding a reserved ToO is not asked again.
        claimed = {
            s.reserved_for
            for s in self._too.values()
            if s.reserved_until is not None and now < s.reserved_until
        }
        for req in pending:
            state = self._too.setdefault(req.id, _TooState())
            if state.reserved_until is not None and now < state.reserved_until:
                continue
            eligible = {}
            for tel in self.limits:
                if tel in claimed:
                    continue
                cand = self.evaluate(tel, now, sky_now, [req])[0]
                if cand.ok:
                    eligible[tel] = cand
            if not eligible:
                continue
            idle = sorted(t for t in eligible if t in self._idle)
            if idle:
                if self._call_preempt(idle[0], "next", f"ToO {req.id}"):
                    claimed.add(idle[0])
                continue
            policy = req.group.too.policy
            if policy == "next":
                continue
            busy = [
                (v.priority_class, v.score, tel)
                for tel, v in self._current.items()
                if tel in eligible and v.priority_class < req.group.priority_class
            ]
            if not busy:
                continue
            _, _, tel = min(busy)
            if self._call_preempt(tel, policy, f"ToO {req.id}"):
                state.reserved_for = tel
                state.reserved_until = now + timedelta(seconds=self.config.reserve_timeout)
                claimed.add(tel)

    def _call_preempt(self, telescope: str, policy: str, reason: str) -> bool:
        if self._preempt is None:
            return False
        try:
            accepted = bool(self._preempt(telescope, policy, reason))
        except Exception:
            log.exception("preempt(%s, %s)", telescope, policy)
            accepted = False
        log.warning("%s: preempt (%s) for %s: %s", telescope, policy, reason, accepted)
        self._record(
            self.clock(),
            "preempt",
            telescope=telescope,
            policy=policy,
            reason=reason,
            accepted=accepted,
        )
        return accepted

    async def serve(self) -> None:
        """Background task: take in the drop box as each file arrives
        (its doorbell; :mod:`robofast.doorbell`), and expire and re-check waiting
        ToOs every ``too_poll`` seconds. Never raises (but for cancellation)."""
        intake = None
        if self.dropbox is not None:
            intake = asyncio.create_task(self._serve_dropbox(self.dropbox))
        try:
            while True:
                try:
                    self.check_toos()
                except Exception:
                    log.exception("dispatch background check")
                await asyncio.sleep(self.config.too_poll)
        finally:
            if intake is not None:
                intake.cancel()

    async def _serve_dropbox(self, dropbox: Path) -> None:
        """Take in what is in the drop box now (dropped while this process
        was down), then each file as it arrives. A ToO is checked as it is
        taken in (:meth:`submit_too`)."""
        # Before the first look, so nothing slips between.
        bell = await Doorbell.open(dropbox, self.config.doorbell_listen)
        try:
            while True:
                try:
                    await self.poll_dropbox()
                except Exception:
                    log.exception("dispatch drop box")
                await bell.wait()
        finally:
            bell.close()

    async def poll_dropbox(self) -> list[str]:
        """Take in every ``*.json`` file in the drop box, in name order: a
        ToO (a request group with a ``too`` entry; :meth:`submit_too`),
        another request group (:meth:`submit`) or ``{"cancel": "<group
        id>"}`` (:meth:`cancel`); then move it to ``accepted/`` or
        ``rejected/`` (with a ``.error`` file saying why). Writers must
        create the file atomically (write elsewhere, then rename into the
        drop box). Returns the accepted group ids (the canceled ones too)."""
        assert self.dropbox is not None
        accepted = []
        for path in sorted(self.dropbox.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict) and "cancel" in data:
                    gid = str(data["cancel"])
                    self.cancel(gid)
                else:
                    gid = await self.submit(data)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.error("ToO file %s rejected: %s", path.name, exc)
                dest = self.dropbox / "rejected"
                dest.mkdir(exist_ok=True)
                (dest / f"{path.name}.error").write_text(f"{exc}\n")
            else:
                accepted.append(gid)
                dest = self.dropbox / "accepted"
                dest.mkdir(exist_ok=True)
            os.replace(path, dest / path.name)
        return accepted

    # -- status ----------------------------------------------------------------

    def status(self, config_id: str) -> Status:
        for r in self.requests.values():
            for c in r.configurations:
                if c.id == config_id:
                    return c.status
        raise KeyError(config_id)

    def visits_tonight(self, request_id: str, now: datetime | None = None) -> int:
        now = now or self.clock()
        start = self.site.night_start(now)
        return sum(1 for t in self.visits.get(request_id, ()) if t >= start)

    def pool_visits_tonight(self, pool: str, now: datetime | None = None) -> int:
        """Tonight's visits of every request whose cadence names ``pool``."""
        now = now or self.clock()
        return sum(
            self.visits_tonight(r.id, now)
            for r in self.requests.values()
            if r.cadence is not None and r.cadence.pool == pool
        )

    def _night_limit(self, req: Request, now: datetime, limit: str) -> str | None:
        """Why ``req`` has reached its ``max_per_night`` (of its own visits,
        or its pool's) if its cadence's limit is ``limit``; None if not."""
        cad = req.cadence
        if cad is None or cad.max_per_night is None or cad.limit != limit:
            return None
        n = cad.max_per_night
        if cad.pool is not None:
            if self.pool_visits_tonight(cad.pool, now) >= n:
                return f"cadence: {n} visits of pool {cad.pool!r} tonight already"
        elif self.visits_tonight(req.id, now) >= n:
            return f"cadence: {n} visits tonight already"
        return None

    # -- evaluation --------------------------------------------------------------

    def evaluate(
        self,
        telescope: str,
        now: datetime | None = None,
        sky_now: sky.SkyNow | None = None,
        requests: Sequence[Request] | None = None,
    ) -> list[Candidate]:
        """Every request (or those given) evaluated for ``telescope`` now."""
        now = now or self.clock()
        sky_now = sky_now or sky.sky_now(self.site, now)
        reqs = list(self.requests.values()) if requests is None else list(requests)
        limits = self.limits.get(telescope)
        if limits is None:
            return [Candidate(r, False, reasons=[f"unknown telescope {telescope!r}"]) for r in reqs]
        # Cheap checks first; collect the light configurations of the
        # survivors for one vectorized altitude computation.
        plans: list[tuple[Candidate, list[tuple[Configuration, datetime, datetime]]]] = []
        rows: list[tuple[float, float, list[datetime]]] = []
        out = []
        for req in reqs:
            cand = Candidate(req, False)
            out.append(cand)
            try:
                parts = self._precheck(telescope, limits, req, now, sky_now, cand.reasons)
            except Exception as exc:
                self._error(now, req, exc, cand)
                continue
            if cand.reasons:
                continue
            cand.demoted = self._night_limit(req, now, "soft") or ""
            plans.append((cand, parts))
            for c, t0, t1 in parts:
                if c.light:
                    rows.append((c.target.ra_hours, c.target.dec_deg, [t0, t0 + (t1 - t0) / 2, t1]))
        alts = self._altitudes(rows)
        k = 0
        for cand, parts in plans:
            n = sum(1 for c, _, _ in parts if c.light)
            mine, k = alts[k : k + n], k + n
            try:
                self._sky_checks(limits, cand, parts, mine, sky_now)
                if not cand.reasons:
                    self._score(limits, cand, parts, mine, now, sky_now)
                    cand.ok = True
            except Exception as exc:
                self._error(now, cand.request, exc, cand)
        return out

    def _error(self, now: datetime, req: Request, exc: Exception, cand: Candidate) -> None:
        cand.ok = False
        cand.reasons.append(f"error: {exc!r}")
        self.errors.append((now, req.id, repr(exc)))
        del self.errors[:-1000]
        log.error("evaluating %s: %r (excluded)", req.id, exc)

    def _altitudes(self, rows: list[tuple[float, float, list[datetime]]]) -> list[list[float]]:
        """Altitudes for every row; a row whose computation raises is all
        NaN (so it fails its altitude limit), the others are unaffected."""
        if not rows:
            return []
        try:
            a = sky.altitudes(
                self.site, [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]
            )
            return [list(map(float, row)) for row in a]
        except Exception as exc:
            log.error("vectorized altitudes failed (%r); computing one by one", exc)
        result = []
        for ra, dec, times in rows:
            try:
                result.append([float(x) for x in sky.altitudes(self.site, [ra], [dec], [times])[0]])
            except Exception as exc:
                log.error("altitude of ra=%r dec=%r: %r", ra, dec, exc)
                result.append([math.nan] * len(times))
        return result

    def _remaining(self, c: Configuration) -> tuple[int, int]:
        """Where a new visit starts in ``c``: (instrument config, frames of it
        already taken); (0, 0) unless ``allow_partial`` progress is kept."""
        if not c.allow_partial or c.id not in self._progress:
            return 0, 0
        ic, done = self._progress[c.id]
        if not (0 <= ic < len(c.instrument_configs)):
            return 0, 0
        return ic, done if 0 <= done < c.instrument_configs[ic].count else 0

    def duration(self, c: Configuration) -> float:
        """The time model's estimate of ``c``'s part of a visit (s): its
        remaining frames with their readout, and a slew if it has light
        frames."""
        return self._duration(c)

    def _duration(self, c: Configuration) -> float:
        cfg = self.config
        start, done = self._remaining(c)
        frames = sum(
            (ic.count - (done if i == start else 0)) * (ic.exptime + cfg.frame_overhead)
            for i, ic in enumerate(c.instrument_configs)
            if i >= start
        )
        return frames + (cfg.slew_overhead if c.light else 0.0)

    def _in_progress(self, req: Request) -> str | None:
        for tel, v in self._current.items():
            if v.request is req:
                return tel
        return None

    def instrument_for(self, limits: TelescopeLimits, c: Configuration) -> str | None:
        """The instrument of a telescope (its ``limits``) that takes ``c``:
        the one ``c`` names, or else the first whose type takes ``c``'s type
        and its instrument parameters; None if there is none."""
        names = [c.instrument] if c.instrument is not None else limits.instruments
        for name in names:
            t = self.instrument_types.get(name)
            if name in limits.instruments and t is not None and self._takes(t, c):
                return name
        return None

    @staticmethod
    def _takes(t: InstrumentType, c: Configuration) -> bool:
        return c.type in t.accepts and all(
            k in t.params for ic in c.instrument_configs for k in ic.params
        )

    def _instrument_reasons(
        self, telescope: str, limits: TelescopeLimits, c: Configuration, reasons: list[str]
    ) -> None:
        """Why no instrument of ``telescope`` takes ``c`` (nothing if one does)."""
        if self.instrument_for(limits, c) is not None:
            return
        if c.instrument is None:
            reasons.append(f"{telescope} has no instrument for this {c.type}")
            return
        if c.instrument not in limits.instruments:
            reasons.append(f"{telescope} has no {c.instrument}")
            return
        t = self.instrument_types[c.instrument]
        if c.type not in t.accepts:
            reasons.append(f"{telescope}'s {c.instrument} ({t.name}) takes no {c.type}")
        params = sorted({k for ic in c.instrument_configs for k in ic.params} - set(t.params))
        if params:
            reasons.append(
                f"{telescope}'s {c.instrument} ({t.name}) takes no extra_params {params}"
            )

    def _precheck(
        self,
        telescope: str,
        limits: TelescopeLimits,
        req: Request,
        now: datetime,
        sky_now: sky.SkyNow,
        reasons: list[str],
        configs: list[Configuration] | None = None,
        lead: str | None = None,
    ) -> list[tuple[Configuration, datetime, datetime]]:
        """The non-positional constraints; returns each remaining
        configuration's planned [start, end]. ``lead``: the telescope whose
        spectrum this one would feed (its use of the spectrograph is no
        obstacle)."""
        if configs is None:
            if req.done:
                reasons.append("done")
                return []
            other = self._in_progress(req)
            if other is not None and other != telescope:
                reasons.append(f"in progress on {other}")
            configs = [c for c in req.configurations if c.status.schedulable]
        if req.telescopes is not None and telescope not in req.telescopes:
            reasons.append(f"not for {telescope}")
        for c in configs:
            self._instrument_reasons(telescope, limits, c, reasons)
            for ic in c.instrument_configs:
                if ic.port is not None and ic.port not in limits.ports:
                    reasons.append(f"{telescope} cannot select port {ic.port!r}")
                if (
                    ic.filter is not None
                    and limits.filters is not None
                    and ic.filter not in limits.filters
                ):
                    reasons.append(f"{telescope} has no filter {ic.filter!r}")
                if ic.calstage is not None and ic.calstage not in limits.calstage:
                    reasons.append(f"{telescope} has no calstage position {ic.calstage!r}")
                if ic.lamp is not None and ic.lamp not in limits.lamps:
                    reasons.append(f"{telescope} has no lamp {ic.lamp!r}")
                if ic.sequence is not None:
                    if ic.sequence not in limits.sequences:
                        reasons.append(f"{telescope} has no sequence {ic.sequence!r}")
                    elif ic.sequence in self.sequences:
                        from robofast.sequences import requestable

                        why = requestable(self.sequences[ic.sequence], c.type)
                        if why is not None:
                            reasons.append(why)
        if any(c.kind.spectrograph for c in configs):
            if self.spectrograph_down is not None:
                reasons.append(f"spectrograph down ({self.spectrograph_down})")
            for tel, v in self._current.items():
                if tel not in (telescope, lead) and v.cfg < len(v.configs):
                    if v.config.kind.spectrograph:
                        reasons.append(f"spectrograph in use by {tel}")
        too = req.group.too
        if too is not None:
            if now > too.deadline:
                reasons.append("past the ToO deadline")
            st = self._too.get(req.id)
            if (
                st is not None
                and st.reserved_for not in (None, telescope)
                and st.reserved_until is not None
                and now < st.reserved_until
            ):
                reasons.append(f"reserved for {st.reserved_for}")
        parts = []
        t = now
        for c in configs:
            end = t + timedelta(seconds=self._duration(c))
            parts.append((c, t, end))
            t = end
        if not req.fits(now, t):
            reasons.append("outside its windows")
        if req.cadence is not None:
            last = max(self.visits.get(req.id, ()), default=None)
            if last is not None and (now - last).total_seconds() < req.cadence.min_interval:
                reasons.append("cadence: too soon after the last visit")
            reached = self._night_limit(req, now, "hard")
            if reached:
                reasons.append(reached)
        if any(c.light for c in configs):
            sun_limit = min(
                [self.config.max_sun_alt]
                + [
                    c.constraints.max_sun_alt
                    for c in configs
                    if c.constraints.max_sun_alt is not None
                ]
            )
            if not sky_now.sun_alt <= sun_limit:
                reasons.append(f"Sun at {sky_now.sun_alt:.1f} deg (limit {sun_limit:g})")
        return parts

    @staticmethod
    def _alt_range(limits: TelescopeLimits, c: Configuration) -> tuple[float, float]:
        lo, hi = limits.min_alt, limits.max_alt
        k = c.constraints
        if k.min_alt is not None:
            lo = max(lo, k.min_alt)
        if k.max_alt is not None:
            hi = min(hi, k.max_alt)
        if k.max_airmass is not None:
            lo = max(lo, math.degrees(math.asin(1.0 / k.max_airmass)))
        return lo, hi

    @staticmethod
    def _ha_range(limits: TelescopeLimits, c: Configuration) -> tuple[float, float]:
        lo, hi = limits.min_ha, limits.max_ha
        if c.constraints.min_ha is not None:
            lo = max(lo, c.constraints.min_ha)
        if c.constraints.max_ha is not None:
            hi = min(hi, c.constraints.max_ha)
        return lo, hi

    def _sky_checks(
        self,
        limits: TelescopeLimits,
        cand: Candidate,
        parts: list[tuple[Configuration, datetime, datetime]],
        alts: list[list[float]],
        sky_now: sky.SkyNow,
    ) -> None:
        light = [p for p in parts if p[0].light]
        if not light:
            return
        seps = sky.moon_separation(
            sky_now.moon,
            [c.target.ra_hours for c, _, _ in light],
            [c.target.dec_deg for c, _, _ in light],
        )
        for (c, t0, t1), alt, sep in zip(light, alts, seps, strict=True):
            name = c.target.name
            lo, hi = self._alt_range(limits, c)
            if not all(math.isfinite(a) and lo <= a <= hi for a in alt):
                shown = ", ".join(f"{a:.1f}" for a in alt)
                cand.reasons.append(f"{name}: altitude {shown} outside [{lo:g}, {hi:g}]")
            ha_lo, ha_hi = self._ha_range(limits, c)
            lst0 = sky_now.lst_hours + (t0 - sky_now.when).total_seconds() / 3600.0 / sky.SIDEREAL
            lst1 = sky_now.lst_hours + (t1 - sky_now.when).total_seconds() / 3600.0 / sky.SIDEREAL
            ha0 = sky.hour_angle(lst0, c.target.ra_hours)
            ha1 = ha0 + (lst1 - lst0)
            # (The full range [-12, 12] is no limit, even across the wrap.)
            if (ha_lo > -12.0 or ha_hi < 12.0) and not (ha_lo <= ha0 and ha1 <= ha_hi):
                cand.reasons.append(
                    f"{name}: hour angle {ha0:+.2f}..{ha1:+.2f} h outside [{ha_lo:g}, {ha_hi:g}]"
                )
            moon_limit = max(self.config.min_moon_sep, c.constraints.min_moon_sep or 0.0)
            if not (math.isfinite(sep) and sep >= moon_limit):
                cand.reasons.append(f"{name}: {sep:.1f} deg from the Moon (limit {moon_limit:g})")
            phase_limit = c.constraints.max_lunar_phase
            if phase_limit is not None and phase_limit < 1.0:
                phase = _moon_illumination(sky_now)
                if not (math.isfinite(phase) and phase <= phase_limit):
                    cand.reasons.append(
                        f"{name}: Moon {phase:.2f} illuminated (limit {phase_limit:g})"
                    )
            seeing_limit = c.constraints.max_seeing
            if seeing_limit is not None:
                why = self._seeing_check(seeing_limit, sky_now.when)
                if why is not None:
                    cand.reasons.append(f"{name}: {why}")

    def _seeing_check(self, limit: float, now: datetime) -> str | None:
        """Why ``max_seeing`` ``limit`` fails now (None: it holds). With no
        seeing at all, the seeing entry's ``unknown`` decides."""
        est = self.seeing_now(now)
        if est is None:
            if self.seeing.unknown == "fail":
                return f'seeing unknown (max_seeing {limit:g}")'
            return None
        if not (math.isfinite(est.value) and est.value <= limit):
            return f'seeing {est.value:.2f}" ({est.source}) > max_seeing {limit:g}"'
        return None

    def _score(
        self,
        limits: TelescopeLimits,
        cand: Candidate,
        parts: list[tuple[Configuration, datetime, datetime]],
        alts: list[list[float]],
        now: datetime,
        sky_now: sky.SkyNow,
    ) -> None:
        cfg = self.config
        req = cand.request
        terms = {"priority": req.group.priority}
        light = [p for p in parts if p[0].light]
        t_left = math.inf
        if light:
            c = light[0][0]
            alt_now = alts[0][0]
            lo, _ = self._alt_range(limits, c)
            _, ha_hi = self._ha_range(limits, c)
            ha = sky.hour_angle(sky_now.lst_hours, c.target.ra_hours)
            h_set = min(sky.setting_hour_angle(self.site.latitude, c.target.dec_deg, lo), ha_hi)
            t_left = (h_set - ha) * 3600.0 * sky.SIDEREAL
            x_best = sky.airmass(sky.transit_altitude(self.site.latitude, c.target.dec_deg))
            x_now = sky.airmass(alt_now)
            # min/max would swallow a NaN; refuse it explicitly.
            for what, v in (("t_left", t_left), ("x_best", x_best), ("x_now", x_now)):
                if math.isnan(v):
                    raise ValueError(f"{what} is NaN")
            terms["airmass"] = cfg.w_airmass * min(1.0, x_best / x_now)
        end = req.window_end(now)
        if end is not None:
            t_left = min(t_left, (end - now).total_seconds())
        if req.group.too is not None:
            t_left = min(t_left, (req.group.too.deadline - now).total_seconds())
        terms["setting"] = cfg.w_setting * max(0.0, 1.0 - t_left / cfg.setting_horizon)
        if req.cadence is not None:
            last = max(self.visits.get(req.id, ()), default=None)
            if last is None:
                overdue = 1.0
            else:
                since = (now - last).total_seconds()
                mi = req.cadence.min_interval
                overdue = 1.0 if mi <= 0 else min(1.0, max(0.0, (since - mi) / mi))
            terms["cadence"] = cfg.w_cadence * overdue
        score = sum(terms.values())
        if not math.isfinite(score):
            raise ValueError(f"score is not finite: {terms}")
        cand.score, cand.terms = score, terms

    def _best(
        self, telescope: str, now: datetime, sky_now: sky.SkyNow
    ) -> tuple[Candidate | None, list[Candidate]]:
        """The best candidate (or None), and every candidate evaluated."""
        cands = self.evaluate(telescope, now, sky_now)
        ok = sorted((c for c in cands if c.ok), key=_rank)
        return (ok[0] if ok else None), cands

    def _summary(self, best: Candidate | None, cands: list[Candidate]) -> dict[str, Any]:
        """The runners-up and the top rejected candidates, for the record."""
        n = self.config.record_top_n
        ok = sorted((c for c in cands if c.ok and c is not best), key=_rank)
        rejected = [c for c in cands if not c.ok and c.reasons != ["done"]]
        rejected.sort(
            key=lambda c: (-c.request.group.priority_class, -c.request.group.priority, c.request.id)
        )
        return {
            "runners_up": [
                {"request": c.request.id, "score": c.score, "terms": c.terms}
                | ({"demoted": c.demoted} if c.demoted else {})
                for c in ok[:n]
            ],
            "rejected": [
                {
                    "request": c.request.id,
                    "priority": c.request.group.priority,
                    "priority_class": c.request.group.priority_class,
                    "reasons": c.reasons,
                }
                for c in rejected[:n]
            ],
            "candidates": len(cands),
            "observable": sum(1 for c in cands if c.ok),
        }

    def _record(self, when: datetime, kind: str, **fields: Any) -> None:
        if self.mode is not None:
            fields.setdefault("mode", self.mode)
        if self.record is not None:
            self.record.write(when, kind, fields, snapshot=self.snapshot)

    def _continue_check(
        self, telescope: str, visit: _Visit, now: datetime, sky_now: sky.SkyNow
    ) -> str | None:
        """Why the visit must be abandoned (its remainder no longer passes the
        hard constraints, or a ToO outranks it); None: continue it."""
        req = visit.request
        why = None
        if any(c.status == Status.CANCELED for c in visit.configs[visit.cfg :]):
            return "canceled"
        outranked = [
            r
            for r in self.requests.values()
            if r.group.priority_class > visit.priority_class
            and not r.done
            and not self._in_progress(r)
        ]
        if outranked and any(c.ok for c in self.evaluate(telescope, now, sky_now, outranked)):
            why = "a ToO outranks it"
        else:
            cand = Candidate(req, False)
            rest = visit.configs[visit.cfg :]
            try:
                parts = self._precheck(
                    telescope, self.limits[telescope], req, now, sky_now, cand.reasons, rest
                )
                # The first remaining configuration may be part-done; only its
                # remaining instrument configs matter, but checking all of it
                # is the conservative choice.
                if not cand.reasons:
                    rows = [
                        (c.target.ra_hours, c.target.dec_deg, [t0, t0 + (t1 - t0) / 2, t1])
                        for c, t0, t1 in parts
                        if c.light
                    ]
                    self._sky_checks(
                        self.limits[telescope], cand, parts, self._altitudes(rows), sky_now
                    )
            except Exception as exc:
                self._error(now, req, exc, cand)
            if cand.reasons:
                why = "; ".join(cand.reasons)
        return why

    def _abandon(self, telescope: str, visit: _Visit, why: str, now: datetime) -> None:
        req = visit.request
        log.info("%s: abandoning %s mid-visit: %s", telescope, req.id, why)
        self._record(
            now, "abandon", telescope=telescope, request=req.id, config=visit.config.id, why=why
        )
        if visit.config.status.schedulable:
            visit.config.status = Status.ATTEMPTED
        del self._current[telescope]
        self._release_members(telescope)

    # -- spectra fed by several telescopes ---------------------------------------

    def _member_observation(self, telescope: str) -> Observation | None:
        """The spectrum ``telescope`` is reserved to feed, if its leader is
        still on it (else the stale reservation is dropped)."""
        held = self._members.get(telescope)
        if held is None:
            return None
        if telescope in self.held:
            del self._members[telescope]
            return None
        leader, obs = held
        visit = self._current.get(leader)
        if visit is None or visit.obs is not obs:
            del self._members[telescope]
            return None
        assert obs.spectrum is not None
        return replace(obs, spectrum={**obs.spectrum, "leader": leader})

    def _release_members(self, leader: str) -> None:
        for tel in [t for t, (lead, _) in self._members.items() if lead == leader]:
            del self._members[tel]

    def _feed(
        self, telescope: str, visit: _Visit, obs: Observation, now: datetime, sky_now: sky.SkyNow
    ) -> None:
        """For a spectrum: the telescopes that feed it with ``telescope`` (the
        free ones that may take the request now), reserved for it (module
        doc); :meth:`_wake_feed` then wakes the idle ones."""
        if obs.spectrum is None:
            return
        self._release_members(telescope)
        req = visit.request
        busy = set(self._current) | set(self._members)
        feed = [telescope]
        for tel in sorted(self.limits):
            if tel == telescope or tel in busy or tel in self.held:
                continue  # busy, or held by an operator
            if any(
                s.reserved_for == tel and s.reserved_until is not None and now < s.reserved_until
                for rid, s in self._too.items()
                if rid != req.id
            ):
                continue  # held for a ToO
            cand = self._evaluate_for_feed(tel, req, visit, now, sky_now, telescope)
            if cand:
                feed.append(tel)
        obs.spectrum["feed"] = feed
        obs.spectrum["leader"] = telescope
        for tel in feed[1:]:
            self._members[tel] = (telescope, obs)

    def _wake_feed(self, obs: Observation) -> None:
        """Wake the idle members of a spectrum's feed (after the decision is
        recorded, so a replay meets the wake-ups in the same order)."""
        if obs.spectrum is None:
            return
        for tel in obs.spectrum["feed"][1:]:
            if tel in self._idle:
                self._call_preempt(tel, "next", f"feed {obs.spectrum['id']}")

    def _evaluate_for_feed(
        self,
        telescope: str,
        req: Request,
        visit: _Visit,
        now: datetime,
        sky_now: sky.SkyNow,
        visit_lead: str,
    ) -> bool:
        """Whether ``telescope`` passes the hard constraints for the rest of
        ``visit`` (led by ``visit_lead``) now, as if it took the request."""
        cand = Candidate(req, False)
        try:
            parts = self._precheck(
                telescope,
                self.limits[telescope],
                req,
                now,
                sky_now,
                cand.reasons,
                visit.configs[visit.cfg :],
                lead=visit_lead,
            )
            if not cand.reasons:
                rows = [
                    (c.target.ra_hours, c.target.dec_deg, [t0, t0 + (t1 - t0) / 2, t1])
                    for c, t0, t1 in parts
                    if c.light
                ]
                self._sky_checks(
                    self.limits[telescope], cand, parts, self._altitudes(rows), sky_now
                )
        except Exception as exc:
            self._error(now, req, exc, cand)
        return not cand.reasons

    def _new_visit(self, req: Request, now: datetime, score: float) -> _Visit:
        configs = [c for c in req.configurations if c.status.schedulable]
        visit = _Visit(req, configs, now, priority_class=req.group.priority_class, score=score)
        self._enter_config(visit)
        return visit

    def _enter_config(self, visit: _Visit) -> None:
        """Start the visit's current configuration where its kept
        allow_partial progress says (else at its beginning)."""
        visit.ic, visit.done = self._remaining(visit.config)

    def _make_observation(self, visit: _Visit) -> Observation:
        c = visit.config
        ic = c.instrument_configs[visit.ic]
        params = {}
        for k, v in ic.params.items():
            p = instrument_param(k)
            params[k] = v if p is None else p.frame(v)
        mapping = RequestMapping(
            c,
            visit.ic,
            visit.done,
            visit.started,
            acquisition=self._acquisition_entry(visit, c),
            params=params,
        )
        obs = Observation(**c.kind.observation(mapping))
        if obs.guide and c.guide_on != "annulus":
            plan = visit.guide.get(c.id)
            if plan is None:
                plan = visit.guide[c.id] = self._guide_plan(c)
            if plan["on"] == "offset":
                obs.guide_offset = self._guide_offset(c)
        return obs

    def _guide_plan(self, c: Configuration) -> dict[str, Any]:
        """How configuration ``c`` is guided: ``offset``, or for ``guide:
        "auto"`` the recommendation of its instrument's guiding model for
        the target's ``target_mag`` under tonight's sky (Moon) and the
        seeing from the plan's seeing source, else the model's
        (docs/offset_guiding.md, docs/seeing.md). Without a model or a
        magnitude, ``auto`` guides on the target."""
        if c.guide_on == "offset":
            return {"on": "offset"}
        from robofast.guider import recommend as rc

        name = c.instrument
        if name is None:
            name = next(
                (n for n in self.guide_models if self._takes(self.instrument_types[n], c)), None
            )
        model = self.guide_models.get(name) if name is not None else None
        if model is None:
            return {"on": "annulus", "reason": f"auto: no guiding model for {name!r}"}
        if c.guide_mag is None:
            return {"on": "annulus", "reason": "auto: no target_mag"}
        star_g = None
        if isinstance(c.offset_star, dict) and c.offset_star.get("g") is not None:
            star_g = float(c.offset_star["g"])
        sky_mag = model.sky
        try:
            now = self.clock()
            sn = sky.sky_now(self.site, now)
            moon_alt = float(
                sky.altitudes(self.site, [sn.moon.ra.hour], [sn.moon.dec.deg], [[now]])[0][0]
            )
            sky_mag = rc.sky_brightness(model, _moon_illumination(sn), moon_alt)
        except Exception as exc:
            log.warning("guide auto: no Moon for the sky brightness (%r); dark sky assumed", exc)
        seeing = self.seeing.current(self.clock(), fallback=model.seeing)
        rec = rc.recommend(
            model, c.guide_mag, sky=sky_mag, star_g=star_g, seeing=seeing and seeing.value
        )
        log.info("guide auto for %s (%s): %s -- %s", c.id, name, rec.method, rec.reason)
        out = {"on": rec.method, "instrument": name, "recommendation": rec.as_json()}
        if seeing is not None:
            out["seeing"] = seeing.as_json()
        return out

    @staticmethod
    def _guide_record(visit: _Visit, obs: Observation) -> dict[str, Any]:
        """The decision record's ``guide`` entry: the mode, the offset star
        and, for ``auto``, the recommendation."""
        plan = visit.guide.get(visit.config.id)
        if plan is None and obs.guide_offset is None:
            return {}
        out: dict[str, Any] = dict(plan or {"on": "offset"})
        if obs.guide_offset is not None:
            out["offset_star"] = obs.guide_offset["star"]
        return {"guide": out}

    def _guide_offset(self, c: Configuration) -> dict[str, Any]:
        """The guider's ``offset_guide`` for configuration ``c``
        (docs/offset_guiding.md): its offset star, the target's astrometry
        and the site (for differential refraction)."""
        t = c.target
        return {
            "star": "auto" if c.offset_star is None else c.offset_star,
            "target": {
                "ra": t.ra,
                "dec": t.dec,
                "pmra": t.pm_ra or 0.0,
                "pmdec": t.pm_dec or 0.0,
                "epoch": t.epoch or 2000.0,
            },
            "site": {
                "latitude": self.site.latitude,
                "longitude": self.site.longitude,
                "elevation": self.site.elevation,
            },
        }

    def _observation(self, visit: _Visit) -> Observation:
        obs = self._make_observation(visit)
        visit.obs = obs
        return obs

    def _visit_done(self, telescope: str, visit: _Visit) -> None:
        req = visit.request
        del self._current[telescope]
        hist = self.visits.setdefault(req.id, [])
        hist.append(visit.started)
        del hist[: -self.config.history_size]
        for c in req.configurations:
            self._progress.pop(c.id, None)
        if req.standing:
            for c in req.configurations:
                if c.status.schedulable:
                    c.status = Status.PENDING
        log.info("%s: visit of %s complete", telescope, req.id)

    def _expire(self, now: datetime) -> None:
        for req in self.requests.values():
            if req.done or self._in_progress(req):
                continue
            too = req.group.too
            if (too is not None and now > too.deadline) or req.windows_over(now):
                for c in req.configurations:
                    if c.status.schedulable:
                        c.status = Status.EXPIRED
                log.warning("%s expired", req.id)
                self._record(now, "expired", request=req.id)
                self._save_state()

    # -- bookkeeping -------------------------------------------------------------

    def _add(self, group: RequestGroup) -> None:
        if group.id in self.groups:
            raise ValueError(f"request group id {group.id!r} is already in use")
        known = {c.id for g in self.groups.values() for c in g.configurations()}
        for r in group.requests:
            if r.id in self.requests:
                raise ValueError(f"request id {r.id!r} is already in use")
            for c in r.configurations:
                if c.id in known:
                    raise ValueError(f"configuration id {c.id!r} is already in use")
            if r.telescopes is not None:
                unknown = set(r.telescopes) - set(self.limits)
                if unknown:
                    raise ValueError(f"request {r.id!r}: unknown telescopes {sorted(unknown)}")
        self.groups[group.id] = group
        for r in group.requests:
            self.requests[r.id] = r

    def _state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "visits": {k: [t.isoformat() for t in v] for k, v in self.visits.items() if v},
            "configurations": {
                c.id: {"status": str(c.status), "failures": c.failures}
                for g in self.groups.values()
                for c in g.configurations()
                if c.status != Status.PENDING or c.failures
            },
            "toos": [g.to_dict() for g in self.groups.values() if g.too is not None],
            "progress": {k: list(v) for k, v in self._progress.items()},
            "submitted": [
                g.to_dict()
                for g in self.groups.values()
                if g.too is None and g.id in self._submitted
            ],
            "observed": {k: v for k, v in self.observed.items() if v},
        }

    def snapshot(self) -> dict[str, Any]:
        """Everything a replay needs to rebuild this scheduler as it is now
        (requests with their status, state, limits, settings)."""
        return {
            "site": {
                "latitude": self.site.latitude,
                "longitude": self.site.longitude,
                "elevation": self.site.elevation,
            },
            "telescopes": {t: asdict(lim) for t, lim in self.limits.items()},
            "instruments": {
                n: {"type": t.name}
                | (
                    {"guiding": self._instrument_conf[n]["guiding"]}
                    if n in self.guide_models
                    else {}
                )
                for n, t in self.instrument_types.items()
            },
            "config": asdict(self.config),
            "groups": [g.to_dict() for g in self.groups.values()],
            "state": self._state(),
            "held": sorted(self.held),
            "spectrograph_down": self.spectrograph_down,
        }

    @classmethod
    def from_snapshot(
        cls, snap: dict[str, Any], *, clock: Callable[[], datetime] | None = None, **kw: Any
    ) -> DispatchScheduler:
        """A scheduler rebuilt from :meth:`snapshot` (no state file)."""
        sched = cls(
            [RequestGroup.from_dict(g) for g in snap["groups"]],
            sky.Site(**snap["site"]),
            {
                t: TelescopeLimits.from_dict(d, f"telescopes.{t}")
                for t, d in snap["telescopes"].items()
            },
            config=DispatchConfig(**snap["config"]),
            clock=clock,
            instruments=snap.get("instruments"),
            **kw,
        )
        sched.restore_state(snap["state"])
        sched.held = set(snap.get("held") or ())
        sched.spectrograph_down = snap.get("spectrograph_down")
        return sched

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._state(), indent=1))
            os.replace(tmp, self.state_path)
        except OSError:
            log.exception("could not save the scheduler state to %s", self.state_path)

    def _load_state(self) -> None:
        assert self.state_path is not None
        if not self.state_path.exists():
            return
        try:
            self.restore_state(json.loads(self.state_path.read_text()))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            aside = self.state_path.with_name(
                f"{self.state_path.name}.corrupt-{self.clock().strftime('%Y%m%dT%H%M%S')}"
            )
            os.replace(self.state_path, aside)
            log.error(
                "scheduler state %s unreadable (%r); moved to %s", self.state_path, exc, aside
            )
            return

    def restore_state(self, data: Any) -> None:
        """Apply a saved state (the state file's JSON) to this scheduler.
        Raises (ValueError, TypeError, KeyError, AttributeError) without
        changing anything if it cannot be parsed. A saved group whose ids
        clash with the request file's is skipped, with an error logged."""
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("not a version-1 scheduler state")
        visits = {
            k: [datetime.fromisoformat(t) for t in v] for k, v in data.get("visits", {}).items()
        }
        toos = [RequestGroup.from_dict(g) for g in data.get("toos", [])]
        submitted = [RequestGroup.from_dict(g) for g in data.get("submitted", [])]
        configs = {
            k: (Status(v["status"]), int(v.get("failures", 0)))
            for k, v in data.get("configurations", {}).items()
        }
        progress = {k: (int(v[0]), int(v[1])) for k, v in data.get("progress", {}).items()}
        observed = data.get("observed", {})
        if not isinstance(observed, dict) or not all(
            isinstance(v, list) for v in observed.values()
        ):
            raise ValueError("'observed': expected {configuration id: [records]}")
        self.visits = visits
        self.observed = {k: list(v) for k, v in observed.items()}
        self._progress = progress
        for g in toos + submitted:
            if g.id in self.groups:
                continue
            try:
                self._add(g)
            except ValueError as exc:
                log.error("saved request group %s not restored: %s", g.id, exc)
                continue
            if g.too is None:
                self._submitted.add(g.id)
        for g in self.groups.values():
            for c in g.configurations():
                if c.id in configs:
                    c.status, c.failures = configs[c.id]


def _resolve(path: str | Path, base: Path | None) -> Path:
    p = Path(path)
    return base / p if base is not None and not p.is_absolute() else p


def dispatch_from_config(
    config: dict[str, Any],
    telescopes: Sequence[str],
    *,
    base: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> DispatchScheduler:
    """Build a :class:`DispatchScheduler` from a plan entry (``type``
    removed)::

        {
          "site": {"latitude": 31.680407, "longitude": -110.878977, "elevation": 2316},
          "requests": "requests.json",        # file, or an inline list of groups
          "imports": [{"type": "minerva.legacy_csv", "path": "targets.csv"}],
                                              # optional: a plug-in's importers
          "state": "/var/lib/robofast/aqawan1-dispatch.json",   # optional
          "too_dropbox": "/var/lib/robofast/too",                # optional
          "record": {"dir": "/var/lib/robofast/plans",            # optional
                     "night_rollover_hour": 12, "timezone": "America/Phoenix"},
          "telescopes": {"t1": {"instruments": ["spectrograph"], "min_alt": 21}},
          "instruments": {"spectrograph": {"type": "kiwispec"}},   # names' types
          "plugins": ["mysite.robofast"],     # optional: plug-ins (docs/plugins.md)
          "vetting": "sky.json",          # optional: target vetting (docs/sky.md)
          "seeing": {"source": "guider"},  # optional: where seeing comes from
                                           # (docs/seeing.md)
          ...DispatchConfig fields (max_sun_alt, w_setting, idle_poll, ...)
        }

    ``record`` (the nightly plan record, :class:`PlanRecord`) is a
    directory, or ``{"dir": ..., "name": ..., "night_rollover_hour": ...,
    "timezone": ...}``; without a time zone a night's date is reckoned in
    the site's mean solar time.

    Relative paths are relative to ``base`` (the config file's directory).
    ``telescopes`` must give every telescope of the enclosure its
    :class:`TelescopeLimits` (``instruments``, ``min_alt`` and ``max_alt``
    at least); a telescope it omits, or one the enclosure lacks, is an error.
    ``instruments`` gives the type (:mod:`robofast.instruments`) of
    each instrument name the telescopes list; a name it omits is its own
    type (``"imager"``), and one that is not a type is an error.
    """
    config = dict(config)
    config.pop("_note", None)
    load_config_plugins(config, "dispatch plan: plugins")
    config.pop("plugins", None)
    site = config.pop("site", None)
    if not isinstance(site, dict):
        raise ValueError("dispatch plan: 'site' {latitude, longitude, elevation} is required")
    site_obj = sky.Site(**site)
    tel_cfg = config.pop("telescopes", {}) or {}
    unknown = set(tel_cfg) - set(telescopes)
    if unknown:
        raise ValueError(f"dispatch plan: unknown telescopes {sorted(unknown)}")
    missing = [t for t in telescopes if t not in tel_cfg]
    if missing:
        raise ValueError(
            f"dispatch plan: 'telescopes' has no entry for {missing} (each needs "
            f"{', '.join(TelescopeLimits.REQUIRED)})"
        )
    limits = {
        t: TelescopeLimits.from_dict(tel_cfg[t], f"dispatch plan: telescopes.{t}")
        for t in telescopes
    }
    groups: list[RequestGroup] = []
    requests = config.pop("requests", [])
    if isinstance(requests, str):
        groups += load_request_groups(_resolve(requests, base))
    else:
        groups += load_request_groups(requests)
    if "legacy_csv" in config:
        raise ValueError(
            "dispatch plan: 'legacy_csv' is now an entry of 'imports' ({'type': the "
            "importer a site plug-in registers, 'path': ...}; docs/plugins.md)"
        )
    if "imports" in config:
        groups += import_requests(config.pop("imports"), base, "dispatch plan: imports")
    state = config.pop("state", None)
    dropbox = config.pop("too_dropbox", None)
    vetting = config.pop("vetting", None)
    vetter = None
    if vetting is not None:
        from robofast.vetting import Vetter

        if isinstance(vetting, str):
            vpath = _resolve(vetting, base)
            vetter = Vetter.from_config(json.loads(vpath.read_text()), base=vpath.parent)
        else:
            vetter = Vetter.from_config(vetting, base=base)
    from robofast.seeing import seeing_from_config

    try:
        seeing = seeing_from_config(config.pop("seeing", None), clock)
    except ValueError as exc:
        raise ValueError(f"dispatch plan: {exc}") from None
    instruments = config.pop("instruments", None) or {}
    if not isinstance(instruments, dict):
        raise ValueError("dispatch plan: 'instruments' is {name: {'type': ...}}")
    record = config.pop("record", None)
    if isinstance(record, str):
        record = {"dir": record}
    if record is not None:
        if not isinstance(record, dict) or "dir" not in record:
            raise ValueError("dispatch plan: 'record' must be a directory or {'dir': ...}")
        record = dict(record)
        record_dir = _resolve(record.pop("dir"), base)
        record.setdefault("longitude", site_obj.longitude)
        try:
            record = PlanRecord(record_dir, **record)
        except TypeError as exc:
            raise ValueError(f"dispatch plan: record: {exc}") from exc
    known = {f.name for f in fields(DispatchConfig)}
    bad = set(config) - known
    if bad:
        raise ValueError(f"dispatch plan: unknown keys {sorted(bad)}")
    if dropbox is not None:
        _resolve(dropbox, base).mkdir(parents=True, exist_ok=True)
    return DispatchScheduler(
        groups,
        site_obj,
        limits,
        config=DispatchConfig(**config),
        state=None if state is None else _resolve(state, base),
        too_dropbox=None if dropbox is None else _resolve(dropbox, base),
        clock=clock,
        record=record,
        vetter=vetter,
        instruments=instruments,
        seeing=seeing,
    )
