"""When to autofocus: a plan wrapper that inserts autofocus observations.

:class:`AutofocusTriggers` wraps any :class:`~robofast_core.plan.PlanProvider`
(the dispatch scheduler, a static plan). When a telescope asks for its next
observation and an autofocus is due, it answers with an autofocus
observation (``kind="autofocus"``) on the field, filter and port of the
light observation that comes next, without taking that observation from the
plan (it uses the plan's ``peek``; a plan without one is asked and its
answer held for the telescope's next request). The sequencer then runs it
like any observation: under the lease, the grant fence and the usual
cleanup (design.md 3.4).

An autofocus is due on a telescope when (each optional, in this order):

* ``start_of_night`` -- none has succeeded since the sequencer started;
* ``temperature_change`` -- the registry value ``temperature_value``
  (default the focuser's temperature) has moved more than this (deg C)
  since the last successful autofocus;
* ``fwhm_degradation`` -- the image pipeline's FWHM (``fwhm_value``) is
  more than this factor above the FWHM right after the last autofocus;
* ``max_interval`` -- seconds since the last successful autofocus.

A failed autofocus is not retried for ``retry_after`` seconds (the
telescope observes on at its old focus meanwhile). Autofocus reports are
kept here and never reach the wrapped plan; results go to the plan record
(``autofocus`` entries) when the wrapped plan has one. Everything else is
passed through to the wrapped plan.
"""

from __future__ import annotations

import inspect
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

from robofast_core.plan import Idle, Observation, PlanProvider
from robofast_core.registry import ValueRegistry

log = logging.getLogger(__name__)

__all__ = ["AutofocusTriggers", "TriggerConfig"]


@dataclass
class TriggerConfig:
    start_of_night: bool = True
    temperature_change: float | None = 1.0
    temperature_value: str = "{tel}.focuser.temperature"
    fwhm_degradation: float | None = 1.5
    fwhm_value: str = "{tel}.image.fwhm"
    max_interval: float | None = None
    retry_after: float = 1800.0
    #: Overrides for the autofocus observations (``Observation.autofocus``).
    autofocus: dict[str, Any] = field(default_factory=dict)
    #: Only these telescopes (None: every telescope of the plan).
    telescopes: list[str] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TriggerConfig:
        d = dict(d or {})
        d.pop("_note", None)
        known = {f.name for f in fields(cls)}
        if set(d) - known:
            raise ValueError(f"autofocus_triggers: unknown keys {sorted(set(d) - known)}")
        return cls(**d)


@dataclass
class _State:
    last_success: float | None = None
    last_attempt: float | None = None
    last_failed: bool = False
    temperature: float | None = None
    fwhm: float | None = None
    #: The plan's next observation, held while the autofocus runs (plans
    #: without ``peek``).
    held: Observation | Idle | None = None
    holding: bool = False
    pending: Observation | None = None
    results: list[dict[str, Any]] = field(default_factory=list)


class AutofocusTriggers:
    """A :class:`~robofast_core.plan.PlanProvider` that adds autofocus
    observations to ``inner`` (module doc). ``registry`` (or :meth:`bind`)
    gives the temperatures and FWHMs; ``clock`` is ``time.monotonic``."""

    def __init__(
        self,
        inner: PlanProvider,
        config: TriggerConfig | None = None,
        *,
        registry: ValueRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
        telescopes: Sequence[str] | None = None,
    ) -> None:
        self.inner = inner
        self.config = config or TriggerConfig()
        self.registry = registry
        self.clock = clock
        self._only = set(self.config.telescopes or telescopes or []) or None
        self._state: dict[str, _State] = {}
        #: (telescope, reason) of every autofocus handed out.
        self.history: list[tuple[str, str]] = []
        self._inner_peeks = getattr(inner, "peek", None) is not None
        if not self._inner_peeks:
            # Without the inner plan's peek there is nothing to peek with:
            # the sequencer then asks next_observation (and gives answers back).
            self.peek = None  # type: ignore[assignment]

    def bind(self, registry: ValueRegistry) -> None:
        self.registry = registry

    def __getattr__(self, name: str) -> Any:
        # Optional hooks (serve, current, set_preempt_callback, record, ...).
        if name.startswith("__") or name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    # -- the plan-provider interface --------------------------------------------

    async def next_observation(self, telescope: str) -> Observation | Idle | None:
        st = self._st(telescope)
        if st.holding:
            af = self._autofocus_for(telescope, st.held)
            if af is not None:
                return af  # the last one was interrupted: still due
            st.holding = False
            nxt, st.held = st.held, None
            return nxt
        nxt = await self._peek_inner(telescope)
        af = self._autofocus_for(telescope, nxt)
        if af is not None:
            if not self._inner_peeks:
                st.held, st.holding = nxt, True
            return af
        if self._inner_peeks:
            return await self.inner.next_observation(telescope)
        return nxt

    async def peek(self, telescope: str) -> Observation | Idle | None:  # type: ignore[no-redef]
        nxt = await self._peek_inner(telescope, consume=False)
        af = self._autofocus_for(telescope, nxt, dry_run=True)
        return af if af is not None else nxt

    def report(
        self,
        telescope: str,
        obs: Observation,
        outcome: str,
        frames: int,
        files: list[str] | None = None,
    ) -> None:
        st = self._st(telescope)
        if obs.kind == "autofocus" and obs is st.pending:
            st.pending = None
            st.last_attempt = self.clock()
            if outcome == "complete":
                st.last_success = st.last_attempt
                st.last_failed = False
                st.temperature = self._value(self.config.temperature_value, telescope)
                st.fwhm = None  # re-learned from the next frames
            elif outcome == "failed":
                st.last_failed = True
            # "interrupted": due again at once (nothing was learned).
            if outcome == "interrupted":
                st.last_attempt = None
            return
        if files is not None and _accepts_files(self.inner.report):
            self.inner.report(telescope, obs, outcome, frames, files=files)
        else:
            self.inner.report(telescope, obs, outcome, frames)
        # The FWHM baseline: the first value after an autofocus.
        if st.last_success is not None and st.fwhm is None and obs.light:
            st.fwhm = self._value(self.config.fwhm_value, telescope)

    def autofocus_result(self, telescope: str, obs: Observation, result: dict[str, Any]) -> None:
        """The sequencer's autofocus result: kept, and written to the plan record."""
        st = self._st(telescope)
        st.results.append(result)
        record = getattr(self.inner, "record", None)
        if record is not None:
            keep = (
                "success",
                "method",
                "used",
                "focus",
                "focus_err",
                "start",
                "predicted",
                "seeing",
                "exposures",
                "fallbacks",
                "message",
            )
            try:
                record.write(
                    datetime.now(UTC),
                    "autofocus",
                    {"telescope": telescope, "observation": obs.name}
                    | {k: result.get(k) for k in keep},
                )
            except Exception:
                log.exception("plan record: autofocus entry")
        hook = getattr(self.inner, "autofocus_result", None)
        if hook is not None:
            hook(telescope, obs, result)

    # -- triggers --------------------------------------------------------------

    def due(self, telescope: str) -> str | None:
        """Why an autofocus is due on ``telescope`` now (None: it is not)."""
        if self._only is not None and telescope not in self._only:
            return None
        cfg = self.config
        st = self._st(telescope)
        now = self.clock()
        if st.pending is not None:
            return None
        if st.last_failed and st.last_attempt is not None:
            if now - st.last_attempt < cfg.retry_after:
                return None
            return "retry"
        if st.last_success is None:
            if cfg.start_of_night and (
                st.last_attempt is None or now - st.last_attempt >= cfg.retry_after
            ):
                return "start of night"
            return None
        if cfg.temperature_change is not None and st.temperature is not None:
            t = self._value(cfg.temperature_value, telescope)
            if t is not None and abs(t - st.temperature) > cfg.temperature_change:
                return f"temperature changed {t - st.temperature:+.2f} C"
        if cfg.fwhm_degradation is not None and st.fwhm is not None:
            f = self._value(cfg.fwhm_value, telescope)
            if f is not None and f > cfg.fwhm_degradation * st.fwhm:
                return f"FWHM {f:.2f} > {cfg.fwhm_degradation:g} x {st.fwhm:.2f}"
        if cfg.max_interval is not None and now - st.last_success > cfg.max_interval:
            return f"{now - st.last_success:.0f} s since the last autofocus"
        return None

    def _autofocus_for(
        self, telescope: str, nxt: Observation | Idle | None, dry_run: bool = False
    ) -> Observation | None:
        if not isinstance(nxt, Observation) or not nxt.light or nxt.kind != "expose":
            return None
        reason = self.due(telescope)
        if reason is None:
            return None
        af = Observation(
            f"autofocus [{reason}]",
            ra_hours=nxt.ra_hours,
            dec_deg=nxt.dec_deg,
            exptime=0.0,
            count=1,
            filter=nxt.filter,
            port=nxt.port,
            object="autofocus",
            kind="autofocus",
            autofocus=dict(self.config.autofocus) or None,
        )
        if not dry_run:
            st = self._st(telescope)
            st.pending = af
            self.history.append((telescope, reason))
            log.info("%s: autofocus due: %s", telescope, reason)
        return af

    async def _peek_inner(self, telescope: str, consume: bool = True) -> Observation | Idle | None:
        peek = getattr(self.inner, "peek", None)
        if peek is not None:
            return await peek(telescope)
        if not consume:
            return None  # cannot look without taking; do not trigger on a guess
        return await self.inner.next_observation(telescope)

    def _value(self, template: str, telescope: str) -> float | None:
        if self.registry is None:
            return None
        v = self.registry.snapshot().current(template.replace("{tel}", telescope))
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    def _st(self, telescope: str) -> _State:
        return self._state.setdefault(telescope, _State())


def _accepts_files(fn: Callable[..., Any]) -> bool:
    try:
        return "files" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
