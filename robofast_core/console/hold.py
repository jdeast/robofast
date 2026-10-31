"""Operator holds: pause, resume and stop telescopes (docs/console.md).

The observer console never reaches into the sequencer. It writes a command
file into the enclosure's ``operator_control`` directory, and this plan
wrapper, which runs inside the sequencer process (the sequencer service
wraps the enclosure's plan in it when the config has ``operator_control``),
takes the files in and acts through the two hooks every plan already has:

* while a telescope is **held**, its plan answers :class:`~robofast_core.plan.Idle`
  (to ``next_observation`` and ``peek``), so the sequencer starts nothing on
  it: the telescope stops tracking and stays where it is, exactly as for a
  gap in the schedule (design.md 3.4). Once every telescope of a generation
  has been idle for ``idle_release_after`` the generation ends as usual (park,
  release the lease, the supervisor closes). A hold never commands the roof.
* the observation in progress is stopped with the sequencer's own
  ``preempt`` (the hook the dispatch scheduler uses for ToOs):
  ``pause`` asks for ``now-after-readout`` (the exposure in progress finishes,
  is read out and kept), ``stop`` for ``now`` (exposure and slew aborted).
  The observation is reported ``interrupted``, so the plan re-queues it.
* ``park`` is ``stop`` plus "park this telescope now": the sequencer's
  park hook (:meth:`~robofast_core.sequencer.Sequencer.request_park`) parks
  that one telescope as soon as its observation has stopped, with the
  telescope's own grant. Only the mount moves; the roof, the lease and the
  other telescopes are not touched (the supervisor alone opens and closes).
  The telescope stays held (and parked) until ``resume``.
* ``clear_spectrograph`` (no telescopes) is the operator's clear of a
  spectrograph the sequencer marked down after repeated failures
  (:meth:`~robofast_core.sequencer.Sequencer.clear_spectrograph`;
  docs/rv_night.md).
* ``resume`` lifts the hold and wakes the telescope (``preempt(..., "next")``,
  which interrupts nothing); it asks its plan afresh. If the enclosure is
  closed, the sequencer asks the supervisor for a lease as it always does;
  the supervisor alone decides whether and when to open.

The held telescopes are also told to the plan (``set_held``, the dispatch
scheduler's), so a held telescope is never reserved to feed a spectrum.

Commands are JSON files ``inbox/<ns>-<id>.json`` (written atomically by
:func:`send_command`, or from a shell: ``python -m
robofast_core.console.hold DIR ACTION [TELESCOPE ...] --operator NAME
--reason TEXT``)::

    {"id": "...", "action": "pause"|"resume"|"stop"|"park"|"clear_spectrograph",
     "telescopes": ["t1"], "operator": "...", "reason": "...", "time": "..."}

Each is applied in order, then moved to ``done/`` with its result. The
holds, the ids of the last commands applied and the time are written to
``holds.json`` (atomically) after every poll, so the console can show them
and confirm a command was applied; on start the wrapper reloads the holds
from that file, so a restarted sequencer stays held.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robofast_core.plan import Idle, Observation, PlanProvider

log = logging.getLogger(__name__)

ACTIONS = ("pause", "resume", "stop", "park", "clear_spectrograph")
#: Actions on no telescope.
GLOBAL_ACTIONS = ("clear_spectrograph",)
#: The preemption each action asks for (``resume`` only wakes the telescope).
PREEMPT_FOR = {"pause": "now-after-readout", "stop": "now", "park": "now", "resume": "next"}
_NAME_RE = re.compile(r"\d{20}-[0-9a-f]{12}\.json")
HOLDS_FILE = "holds.json"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def send_command(
    control_dir: str | Path,
    action: str,
    telescopes: Sequence[str],
    *,
    operator: str,
    reason: str,
) -> str:
    """Drop one command into ``control_dir``; returns its id."""
    if action not in ACTIONS:
        raise ValueError(f"action {action!r}: expected one of {ACTIONS}")
    if not telescopes and action not in GLOBAL_ACTIONS:
        raise ValueError("no telescopes named")
    inbox = Path(control_dir) / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    cid = secrets.token_hex(6)
    _write_json(
        inbox / f"{time.time_ns():020d}-{cid}.json",
        {
            "id": cid,
            "action": action,
            "telescopes": list(telescopes),
            "operator": operator,
            "reason": reason,
            "time": _now_iso(),
        },
    )
    return cid


def read_holds(control_dir: str | Path) -> dict[str, Any] | None:
    """``holds.json``, or None if it is missing or unreadable."""
    try:
        data = json.loads((Path(control_dir) / HOLDS_FILE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _accepts_files(fn: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "files" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


class OperatorHold:
    """A :class:`~robofast_core.plan.PlanProvider` wrapping ``inner`` with
    operator holds (module doc). ``hold_idle`` is the ``Idle`` a held
    telescope is given (it is also woken on resume); ``poll`` the inbox
    period (s)."""

    def __init__(
        self,
        inner: PlanProvider,
        telescopes: Sequence[str],
        control_dir: str | Path,
        *,
        poll: float = 1.0,
        hold_idle: float = 30.0,
    ) -> None:
        self.inner = inner
        self.telescopes = list(telescopes)
        self.dir = Path(control_dir)
        self.poll = poll
        self.hold_idle = hold_idle
        self._preempt: Callable[[str, str, str], bool] | None = None
        #: The sequencer's hooks (:meth:`set_operator_hooks`).
        self._park: Callable[[str, str], Any] | None = None
        self._clear_spectrograph: Callable[[str, str], Any] | None = None
        #: telescope -> {"action", "operator", "reason", "since", "command"}.
        self.holds: dict[str, dict[str, Any]] = {}
        self.applied: deque[dict[str, Any]] = deque(maxlen=50)
        previous = read_holds(self.dir)
        if previous is not None and isinstance(previous.get("holds"), dict):
            self.holds = {
                t: h
                for t, h in previous["holds"].items()
                if t in self.telescopes and isinstance(h, dict)
            }
            if self.holds:
                log.warning("operator holds carried over from %s: %s", self.dir, sorted(self.holds))
        self._tell_plan()
        if getattr(inner, "peek", None) is None:
            # No peek to wrap: the sequencer asks next_observation instead.
            self.peek = None  # type: ignore[assignment]

    def __getattr__(self, name: str) -> Any:
        # Optional hooks (current, record, autofocus_result, ...).
        if name.startswith("__") or name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    # -- the plan-provider interface -------------------------------------------------

    def _held(self, telescope: str) -> Idle | None:
        h = self.holds.get(telescope)
        if h is None:
            return None
        return Idle(self.hold_idle, f"operator {h['action']} by {h['operator']}: {h['reason']}")

    async def next_observation(self, telescope: str) -> Observation | Idle | None:
        return self._held(telescope) or await self.inner.next_observation(telescope)

    async def peek(self, telescope: str) -> Observation | Idle | None:  # type: ignore[no-redef]
        return self._held(telescope) or await self.inner.peek(telescope)  # type: ignore[attr-defined]

    def report(
        self,
        telescope: str,
        obs: Observation,
        outcome: str,
        frames: int,
        files: list[str] | None = None,
    ) -> None:
        if files is not None and _accepts_files(self.inner.report):
            self.inner.report(telescope, obs, outcome, frames, files=files)  # type: ignore[call-arg]
        else:
            self.inner.report(telescope, obs, outcome, frames)

    def set_preempt_callback(self, fn: Callable[[str, str, str], bool]) -> None:
        self._preempt = fn
        hook = getattr(self.inner, "set_preempt_callback", None)
        if hook is not None:
            hook(fn)

    @property
    def held(self) -> set[str]:
        """The telescopes under a hold now."""
        return set(self.holds)

    def set_operator_hooks(
        self,
        *,
        park: Callable[[str, str], Any] | None = None,
        clear_spectrograph: Callable[[str, str], Any] | None = None,
    ) -> None:
        """The sequencer's hooks for ``park`` (telescope, reason) and
        ``clear_spectrograph`` (operator, reason)."""
        self._park = park
        self._clear_spectrograph = clear_spectrograph

    def _tell_plan(self) -> None:
        hook = getattr(self.inner, "set_held", None)
        if hook is None:
            return
        try:
            hook(sorted(self.holds))
        except Exception:
            log.exception("operator holds: plan.set_held")

    async def serve(self) -> None:
        """Take commands in every ``poll`` s (and run the inner plan's
        background task, if it has one)."""
        inner = getattr(self.inner, "serve", None)
        task = asyncio.create_task(inner(), name="plan-serve") if inner else None
        try:
            while True:
                try:
                    self.take_in()
                except Exception:
                    log.exception("operator holds: taking commands in")
                await asyncio.sleep(self.poll)
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    # -- commands -------------------------------------------------------------------

    def take_in(self) -> list[dict[str, Any]]:
        """Apply every command in the inbox, in order; write ``holds.json``."""
        inbox, done = self.dir / "inbox", self.dir / "done"
        results = []
        if inbox.is_dir():
            for path in sorted(inbox.iterdir()):
                if not _NAME_RE.fullmatch(path.name):
                    continue
                try:
                    cmd = json.loads(path.read_text())
                    result = self.apply(cmd)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    cmd, result = {"file": path.name}, {"ok": False, "error": repr(exc)}
                results.append(result)
                done.mkdir(parents=True, exist_ok=True)
                try:
                    _write_json(done / path.name, {**cmd, "result": result})
                    path.unlink()
                except OSError:
                    log.exception("operator holds: filing %s", path)
        self.write_state()
        return results

    def apply(self, cmd: dict[str, Any]) -> dict[str, Any]:
        action, tels = cmd["action"], cmd.get("telescopes") or []
        if action not in ACTIONS:
            raise ValueError(f"action {action!r}")
        operator, reason = str(cmd["operator"]), str(cmd.get("reason", ""))
        if action in GLOBAL_ACTIONS:
            return self._apply_global(cmd, action, operator, reason)
        if not isinstance(tels, list) or not tels:
            raise ValueError("telescopes: expected a non-empty list")
        unknown = sorted(set(map(str, tels)) - set(self.telescopes))
        if unknown:
            raise ValueError(f"unknown telescopes {unknown}")
        preempted = {}
        parked: dict[str, bool] = {}
        for tel in map(str, tels):
            was_held = tel in self.holds
            if action == "resume":
                self.holds.pop(tel, None)
            else:
                self.holds[tel] = {
                    "action": action,
                    "operator": operator,
                    "reason": reason,
                    "since": _now_iso(),
                    "command": cmd.get("id"),
                }
            if action == "resume" and not was_held:
                continue
            preempted[tel] = self._stop(tel, PREEMPT_FOR[action], f"operator {action}: {reason}")
            if action == "park":
                parked[tel] = self._call(self._park, tel, f"operator park by {operator}: {reason}")
        self._tell_plan()
        log.warning(
            "operator %s by %s on %s: %s (preempted: %s)", action, operator, tels, reason, preempted
        )
        result = {"ok": True, "id": cmd.get("id"), "action": action, "preempted": preempted}
        if action == "park":
            result["park_requested"] = parked
        self.applied.append({"id": cmd.get("id"), "action": action, "time": _now_iso()})
        return result

    def _apply_global(
        self, cmd: dict[str, Any], action: str, operator: str, reason: str
    ) -> dict[str, Any]:
        assert action == "clear_spectrograph"
        done = self._call(self._clear_spectrograph, operator, reason)
        log.warning("operator %s by %s: %s (applied: %s)", action, operator, reason, done)
        self.applied.append({"id": cmd.get("id"), "action": action, "time": _now_iso()})
        return {"ok": done, "id": cmd.get("id"), "action": action}

    @staticmethod
    def _call(fn: Callable[[str, str], Any] | None, a: str, b: str) -> bool:
        if fn is None:
            return False
        try:
            return bool(fn(a, b))
        except Exception:
            log.exception("operator holds: sequencer hook %s(%s)", fn, a)
            return False

    def _stop(self, telescope: str, policy: str, reason: str) -> bool:
        if self._preempt is None:
            return False
        try:
            return bool(self._preempt(telescope, policy, reason))
        except Exception:
            log.exception("operator holds: preempt(%s, %s)", telescope, policy)
            return False

    def write_state(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            _write_json(
                self.dir / HOLDS_FILE,
                {
                    "time": _now_iso(),
                    "poll": self.poll,
                    "telescopes": self.telescopes,
                    "holds": self.holds,
                    "applied": list(self.applied),
                },
            )
        except OSError:
            log.exception("operator holds: writing %s", self.dir / HOLDS_FILE)


def main(argv: list[str] | None = None) -> None:
    """Send one command from a shell (module doc)."""
    import argparse

    ap = argparse.ArgumentParser(description="Send an operator command to a sequencer.")
    ap.add_argument("control_dir", help="the enclosure's operator_control directory")
    ap.add_argument("action", choices=ACTIONS)
    ap.add_argument("telescopes", nargs="*")
    ap.add_argument("--operator", required=True)
    ap.add_argument("--reason", required=True)
    args = ap.parse_args(argv)
    try:
        cid = send_command(
            args.control_dir,
            args.action,
            args.telescopes,
            operator=args.operator,
            reason=args.reason,
        )
    except ValueError as exc:
        ap.error(str(exc))
    print(cid)


if __name__ == "__main__":
    main()
