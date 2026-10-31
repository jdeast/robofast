"""The dispatch scheduler: the best observable request for a telescope, now.

:class:`DispatchScheduler` is a :class:`~robofast_core.plan.PlanProvider`
(design.md 3.7). Whenever a telescope is free the sequencer asks it for the
next observation, and it decides from scratch, RTS2/POCS-style:

1. **Hard constraints** (every one must hold, or the request is excluded):

   * the telescope may take it: it is in the request's ``telescopes`` (if
     given) and has every configuration's ``instrument``;
   * the request is schedulable: some configuration is ``PENDING`` or
     ``ATTEMPTED``, and no other telescope is in the middle of it;
   * time: the whole visit (``now`` to ``now + duration``) fits in one of
     the request's windows; a ToO has not passed its deadline;
   * cadence (standing requests): at least ``min_interval`` since the last
     completed visit started, and fewer than ``max_per_night`` visits since
     local noon;
   * the Sun is below ``max_sun_alt`` (twilight), for anything with light
     frames;
   * for each light configuration, at the start, middle and end of its part
     of the visit: altitude within the telescope's ``[min_alt, max_alt]``,
     airmass at most ``max_airmass``; hour angle within ``[min_ha, max_ha]``
     at its start and end; and the Moon at least ``min_moon_sep`` away.
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

   ``priority`` is the request group's. ``t_left`` is the time until the
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
   reproducible.

The visit is then executed: one :class:`~robofast_core.plan.Observation`
per instrument config, configurations in order. Between the observations of
a visit the remaining part is re-checked against the hard constraints (and
abandoned if it no longer passes, or if a ToO outranks it); it is not
re-scored.

Reports (:meth:`DispatchScheduler.report`): ``complete`` advances the visit
(and, at its end, records it in the history); ``interrupted`` marks the
configuration ``ATTEMPTED`` and drops the visit, so it is re-queued; ``failed``
counts towards ``max_attempts`` (then ``FAILED``).

Nothing observable now is :class:`~robofast_core.plan.Idle` (ask again in
``idle_poll``), not "done": the night may bring a rising target or a ToO.
The night is over (``None``) once the Sun is above ``max_sun_alt`` and
rising.

Targets of opportunity (design.md 3.7.1)
----------------------------------------
:meth:`DispatchScheduler.submit_too` (or a JSON file dropped into
``too_dropbox``) adds a request group with a ``too`` entry. A ToO ranks
above every normal request; it is taken only when it passes the hard
constraints; it stays pending until then, and expires at its deadline. When
it arrives, and every ``too_poll`` seconds while it waits, the scheduler
looks for a telescope that can take it now:

* an eligible telescope that is idle is woken (no interruption);
* otherwise, with policy ``now-after-readout`` or ``now``, the eligible
  telescope whose current observation has the lowest (class, score) below
  the ToO's class is preempted through the sequencer's
  :meth:`~robofast_core.sequencer.Sequencer.preempt`, and the ToO is
  reserved for it for ``reserve_timeout``;
* with policy ``next`` nothing is interrupted: the first eligible telescope
  to finish its observation takes it.

State
-----
With ``state`` set, the per-configuration status, the history of completed
visits (for cadence and nightly counts) and the ToOs received are kept in
that JSON file (written atomically after every change), so they survive a
restart. A corrupt file is moved aside (``*.corrupt-<time>``) with an error
logged, and the scheduler starts from the request file alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from robofast_core import sky
from robofast_core.plan import Idle, Observation
from robofast_core.request_model import (
    Configuration,
    Request,
    RequestGroup,
    Status,
    from_legacy_csv,
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
#: :meth:`~robofast_core.sequencer.Sequencer.preempt`).
PreemptCallback = Callable[[str, str, str], bool]


@dataclass
class TelescopeLimits:
    """What one telescope can do. The altitude defaults are the legacy
    MINERVA ``scheduler.ini`` values (MINALT, MAXALT), which the legacy code
    read but never applied."""

    instruments: list[str] = field(default_factory=lambda: ["imager"])
    min_alt: float = 21.0
    max_alt: float = 83.0
    #: Hour angle limits, hours.
    min_ha: float = -12.0
    max_ha: float = 12.0

    @classmethod
    def from_dict(cls, d: dict[str, Any], what: str) -> TelescopeLimits:
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"{what}: unknown keys {sorted(unknown)}")
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
    #: How often the background task re-checks waiting ToOs and the drop box.
    too_poll: float = 10.0
    #: A ToO stays reserved for the telescope it preempted this long.
    reserve_timeout: float = 60.0
    #: Completed visits kept per request in the state file.
    history_size: int = 200


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


def _rank(c: Candidate) -> tuple[int, float, str]:
    """Sort key, best first: highest class, then highest score, then id."""
    assert c.request.group is not None
    return (-c.request.group.priority_class, -c.score, c.request.id)


@dataclass
class _Visit:
    """A request a telescope is executing."""

    request: Request
    configs: list[Configuration]
    started: datetime
    cfg: int = 0
    ic: int = 0
    obs: Observation | None = None
    priority_class: int = 0
    score: float = 0.0

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
    ) -> None:
        if not allow_iers_download:
            sky.offline_iers()
        self.site = site
        self.limits = dict(telescopes)
        self.config = config or DispatchConfig()
        self.clock = clock or (lambda: datetime.now(UTC))
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
        self._idle: set[str] = set()
        self._too: dict[str, _TooState] = {}
        self._preempt: PreemptCallback | None = None
        for g in groups:
            self._add(g)
        if self.state_path is not None:
            self._load_state()

    # -- the plan-provider interface ----------------------------------------

    def set_preempt_callback(self, fn: PreemptCallback) -> None:
        self._preempt = fn

    async def next_observation(self, telescope: str) -> Observation | Idle | None:
        now = self.clock()
        self._expire(now)
        sky_now = sky.sky_now(self.site, now)
        visit = self._current.get(telescope)
        if visit is not None:
            obs = self._continue(telescope, visit, now, sky_now)
            if obs is not None:
                return obs
        if sky_now.sun_alt > self.config.max_sun_alt and sky_now.sun_rising:
            self._idle.discard(telescope)
            log.info(
                "%s: the Sun is up (%.1f deg) and rising: night over", telescope, sky_now.sun_alt
            )
            return None
        best = self._best(telescope, now, sky_now)
        if best is None:
            self._idle.add(telescope)
            self.decisions.append((now, telescope, None, -math.inf))
            return Idle(self.config.idle_poll, "nothing observable")
        self._idle.discard(telescope)
        req = best.request
        configs = [c for c in req.configurations if c.status.schedulable]
        visit = _Visit(req, configs, now, priority_class=req.group.priority_class, score=best.score)
        self._current[telescope] = visit
        self._too.pop(req.id, None)
        self.decisions.append((now, telescope, req.id, best.score))
        log.info(
            "%s: %s (score %.3f: %s)",
            telescope,
            req.id,
            best.score,
            ", ".join(f"{k}={v:.3f}" for k, v in best.terms.items()),
        )
        return self._observation(visit)

    def report(self, telescope: str, obs: Observation, outcome: str, frames: int) -> None:
        self.history.append((telescope, obs.name, outcome, frames))
        visit = self._current.get(telescope)
        if visit is None or visit.obs is not obs:
            log.warning("%s: report for an observation not handed out: %s", telescope, obs.name)
            return
        config = visit.config
        if outcome == "complete":
            visit.ic += 1
            if visit.ic >= len(config.instrument_configs):
                visit.cfg, visit.ic = visit.cfg + 1, 0
                config.status = Status.ATTEMPTED if visit.request.standing else Status.COMPLETED
                if visit.cfg >= len(visit.configs):
                    self._visit_done(telescope, visit)
            visit.obs = None
        else:
            if outcome == "failed":
                config.failures += 1
                if config.failures >= visit.request.max_attempts:
                    config.status = Status.FAILED
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
            raise ValueError(f"request group {group.id!r} has no 'too' entry")
        now = self.clock()
        if group.submitted is None:
            group.submitted = now
        self._add(group)
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

    def cancel(self, group_id: str) -> None:
        """Cancel every schedulable configuration of a group."""
        for c in self.groups[group_id].configurations():
            if c.status.schedulable:
                c.status = Status.CANCELED
        self._save_state()

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
            return False
        log.warning("%s: preempt (%s) for %s: %s", telescope, policy, reason, accepted)
        return accepted

    async def serve(self) -> None:
        """Background task: take in dropped ToO files, expire and re-check
        waiting ToOs, every ``too_poll`` seconds. Never raises (but for
        cancellation)."""
        while True:
            try:
                if self.dropbox is not None:
                    await self.poll_dropbox()
                self.check_toos()
            except Exception:
                log.exception("dispatch background check")
            await asyncio.sleep(self.config.too_poll)

    async def poll_dropbox(self) -> list[str]:
        """Submit every ``*.json`` file in the drop box as a ToO, then move it
        to ``accepted/`` or ``rejected/`` (with a ``.error`` file saying
        why). Writers must create the file atomically (write elsewhere, then
        rename into the drop box). Returns the accepted group ids."""
        assert self.dropbox is not None
        accepted = []
        for path in sorted(self.dropbox.glob("*.json")):
            try:
                gid = await self.submit_too(json.loads(path.read_text()))
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

    def _duration(self, c: Configuration) -> float:
        cfg = self.config
        frames = sum(ic.count * (ic.exptime + cfg.frame_overhead) for ic in c.instrument_configs)
        return frames + (cfg.slew_overhead if c.light else 0.0)

    def _in_progress(self, req: Request) -> str | None:
        for tel, v in self._current.items():
            if v.request is req:
                return tel
        return None

    def _precheck(
        self,
        telescope: str,
        limits: TelescopeLimits,
        req: Request,
        now: datetime,
        sky_now: sky.SkyNow,
        reasons: list[str],
        configs: list[Configuration] | None = None,
    ) -> list[tuple[Configuration, datetime, datetime]]:
        """The non-positional constraints; returns each remaining
        configuration's planned [start, end]."""
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
            if c.instrument not in limits.instruments:
                reasons.append(f"{telescope} has no {c.instrument}")
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
            n = req.cadence.max_per_night
            if n is not None and self.visits_tonight(req.id, now) >= n:
                reasons.append(f"cadence: {n} visits tonight already")
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

    def _best(self, telescope: str, now: datetime, sky_now: sky.SkyNow) -> Candidate | None:
        cands = [c for c in self.evaluate(telescope, now, sky_now) if c.ok]
        if not cands:
            return None
        cands.sort(key=_rank)
        return cands[0]

    def _continue(
        self, telescope: str, visit: _Visit, now: datetime, sky_now: sky.SkyNow
    ) -> Observation | None:
        """The visit's next observation, if its remainder still passes the
        hard constraints and no ToO outranks it; else abandon it."""
        req = visit.request
        why = None
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
        if why is None:
            return self._observation(visit)
        log.info("%s: abandoning %s mid-visit: %s", telescope, req.id, why)
        if visit.config.status.schedulable:
            visit.config.status = Status.ATTEMPTED
        del self._current[telescope]
        return None

    def _observation(self, visit: _Visit) -> Observation:
        c = visit.config
        ic = c.instrument_configs[visit.ic]
        obs = Observation(
            f"{c.target.name} [{c.id}#{visit.ic}]",
            ra_hours=c.target.ra_hours if ic.light else None,
            dec_deg=c.target.dec_deg if ic.light else None,
            exptime=ic.exptime,
            count=ic.count,
            light=ic.light,
        )
        visit.obs = obs
        return obs

    def _visit_done(self, telescope: str, visit: _Visit) -> None:
        req = visit.request
        del self._current[telescope]
        hist = self.visits.setdefault(req.id, [])
        hist.append(visit.started)
        del hist[: -self.config.history_size]
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
        }

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
            data = json.loads(self.state_path.read_text())
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("not a version-1 scheduler state")
            visits = {
                k: [datetime.fromisoformat(t) for t in v] for k, v in data.get("visits", {}).items()
            }
            toos = [RequestGroup.from_dict(g) for g in data.get("toos", [])]
            configs = {
                k: (Status(v["status"]), int(v.get("failures", 0)))
                for k, v in data.get("configurations", {}).items()
            }
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            aside = self.state_path.with_name(
                f"{self.state_path.name}.corrupt-{self.clock().strftime('%Y%m%dT%H%M%S')}"
            )
            os.replace(self.state_path, aside)
            log.error(
                "scheduler state %s unreadable (%r); moved to %s", self.state_path, exc, aside
            )
            return
        self.visits = visits
        for g in toos:
            if g.id not in self.groups:
                self._add(g)
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
          "legacy_csv": "targets.csv",        # optional: legacy target list too
          "state": "/var/lib/robofast/aqawan1-dispatch.json",   # optional
          "too_dropbox": "/var/lib/robofast/too",                # optional
          "telescopes": {"t1": {"instruments": ["spectrograph"], "min_alt": 21}},
          ...DispatchConfig fields (max_sun_alt, w_setting, idle_poll, ...)
        }

    Relative paths are relative to ``base`` (the config file's directory).
    Every telescope of the enclosure gets limits (default
    :class:`TelescopeLimits` where ``telescopes`` does not name it); naming a
    telescope the enclosure lacks is an error.
    """
    config = dict(config)
    config.pop("_note", None)
    site = config.pop("site", None)
    if not isinstance(site, dict):
        raise ValueError("dispatch plan: 'site' {latitude, longitude, elevation} is required")
    site_obj = sky.Site(**site)
    tel_cfg = config.pop("telescopes", {}) or {}
    unknown = set(tel_cfg) - set(telescopes)
    if unknown:
        raise ValueError(f"dispatch plan: unknown telescopes {sorted(unknown)}")
    limits = {
        t: TelescopeLimits.from_dict(tel_cfg.get(t, {}), f"dispatch plan: telescopes.{t}")
        for t in telescopes
    }
    groups: list[RequestGroup] = []
    requests = config.pop("requests", [])
    if isinstance(requests, str):
        groups += load_request_groups(_resolve(requests, base))
    else:
        groups += load_request_groups(requests)
    if "legacy_csv" in config:
        groups += from_legacy_csv(_resolve(config.pop("legacy_csv"), base))
    state = config.pop("state", None)
    dropbox = config.pop("too_dropbox", None)
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
    )
