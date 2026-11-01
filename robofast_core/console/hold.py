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

from robofast_core.cancel import stop_task
from robofast_core.plan import Idle, Observation, PlanProvider

log = logging.getLogger(__name__)

ACTIONS = (
    "pause",
    "resume",
    "stop",
    "park",
    "clear_spectrograph",
    "manual",
    "release",
    "command",
)
#: Actions on no telescope.
GLOBAL_ACTIONS = ("clear_spectrograph",)
#: The preemption each action asks for (``resume`` only wakes the telescope).
PREEMPT_FOR = {
    "pause": "now-after-readout",
    "stop": "now",
    "park": "now",
    "resume": "next",
    "manual": "now-after-readout",
    "release": "next",
}
#: Manual commands (docs/console.md, "Manual control").
MANUAL_OPS = ("slew", "offset", "focus", "expose", "guide_start", "guide_stop", "abort")
#: Manual commands that need the sky (a live lease and the enclosure open).
SKY_OPS = ("slew", "offset", "guide_start")
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
    manual: dict[str, Any] | None = None,
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
            **({"manual": manual} if manual is not None else {}),
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


def read_done(control_dir: str | Path, cid: str) -> dict[str, Any] | None:
    """The filed command ``cid`` with its ``result``, once applied."""
    for path in (Path(control_dir) / "done").glob(f"*-{cid}.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
    return None


def _accepts_files(fn: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "files" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


class ManualSession:
    """One telescope under an operator's manual control: the commands
    waiting, what the sequencer is doing with them, the latest results.
    The sequencer runs it as a ``kind="manual"`` observation (it calls
    :meth:`next`, :meth:`result`, :meth:`started`, :meth:`stopped`)."""

    def __init__(self, telescope: str, operator: str, on_change: Callable[[], None]) -> None:
        self.telescope = telescope
        self.operator = operator
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.active = False
        self.generation: int | None = None
        self.current: dict[str, Any] | None = None
        self.results: deque[dict[str, Any]] = deque(maxlen=20)
        self.ended = False
        self._on_change = on_change

    def state(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "active": self.active,
            "generation": self.generation,
            "current": self.current,
            "queued": self.queue.qsize(),
            "results": list(self.results),
        }

    # -- the sequencer's side --------------------------------------------------------

    async def next(self) -> dict[str, Any] | None:
        cmd = await self.queue.get()
        self.current = cmd
        self._on_change()
        return cmd

    def result(self, cmd: dict[str, Any] | None, ok: bool, detail: Any) -> None:
        self.current = None
        self.results.append(
            {
                "id": (cmd or {}).get("id"),
                "op": (cmd or {}).get("op"),
                "ok": ok,
                "detail": detail,
                "time": _now_iso(),
            }
        )
        self._on_change()

    def started(self, generation: int) -> None:
        self.active, self.generation = True, generation
        self._on_change()

    def stopped(self, why: str) -> None:
        self.active = False
        # Commands left waiting are dropped: the operator sends them again
        # once the session runs (never acted on later, out of context).
        while not self.queue.empty():
            cmd = self.queue.get_nowait()
            if cmd is not None:
                self.result(cmd, False, f"not run: manual session stopped ({why})")
            else:
                self.queue.put_nowait(None)  # keep the release
                break
        self._on_change()

    # -- the console's side ------------------------------------------------------------

    def submit(self, cmd: dict[str, Any]) -> None:
        self.queue.put_nowait(cmd)

    def end(self) -> None:
        self.ended = True
        self.queue.put_nowait(None)


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
        #: Telescopes under manual control (``action`` "manual").
        self.manual: dict[str, ManualSession] = {}
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
            for t, h in self.holds.items():
                if h.get("action") == "manual":
                    self.manual[t] = ManualSession(t, str(h.get("operator")), self.write_state)
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

    def _held(self, telescope: str) -> Observation | Idle | None:
        h = self.holds.get(telescope)
        if h is None:
            return None
        if h["action"] == "manual":
            # The robotic plan does not get this telescope: it observes the
            # operator's manual session (which needs, and keeps, the lease).
            return Observation(f"manual-{telescope}", kind="manual", object="manual")
        return Idle(self.hold_idle, f"operator {h['action']} by {h['operator']}: {h['reason']}")

    def manual_session(self, telescope: str) -> ManualSession | None:
        """The sequencer's hook for ``kind="manual"`` observations."""
        return self.manual.get(telescope)

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
        if obs.kind == "manual":
            return  # the operator's session: nothing of the robotic plan's
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
            # Bounded, passing our own cancellation on (robofast_core.cancel).
            await stop_task(task, 10.0)

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
        if action == "command":
            return self._manual_command(cmd, [str(t) for t in tels], operator)
        if action == "release":
            for tel in map(str, tels):
                if (self.holds.get(tel) or {}).get("action") != "manual":
                    raise ValueError(f"{tel} is not under manual control")
        preempted = {}
        parked: dict[str, bool] = {}
        for tel in map(str, tels):
            was_held = tel in self.holds
            session = self.manual.pop(tel, None)
            if session is not None:
                session.end()  # any change of hold ends a manual session
            if action in ("resume", "release"):
                self.holds.pop(tel, None)
            else:
                self.holds[tel] = {
                    "action": action,
                    "operator": operator,
                    "reason": reason,
                    "since": _now_iso(),
                    "command": cmd.get("id"),
                }
            if action == "manual":
                self.manual[tel] = ManualSession(tel, operator, self.write_state)
            if action in ("resume", "release") and not was_held:
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

    def _manual_command(
        self, cmd: dict[str, Any], tels: list[str], operator: str
    ) -> dict[str, Any]:
        """One manual command for one telescope under manual control."""
        if len(tels) != 1:
            raise ValueError("a manual command is for one telescope")
        tel = tels[0]
        session = self.manual.get(tel)
        m = cmd.get("manual")
        if session is None:
            raise ValueError(f"{tel} is not under manual control")
        if not isinstance(m, dict) or m.get("op") not in MANUAL_OPS:
            raise ValueError(f"manual: op one of {MANUAL_OPS}")
        if session.operator != operator:
            # One operator at a time: another must take control first.
            return {
                "ok": False,
                "id": cmd.get("id"),
                "error": f"under {session.operator}'s control",
            }
        if m["op"] == "abort":
            # Stop what it is doing now (exposure and slew aborted; the
            # session goes on, waiting for the next command).
            stopped = self._stop(tel, "now", f"manual abort by {operator}")
            return {"ok": True, "id": cmd.get("id"), "op": "abort", "preempted": stopped}
        if not session.active:
            return {
                "ok": False,
                "id": cmd.get("id"),
                "error": "the manual session is not running (no live lease yet, or it was revoked)",
            }
        session.submit({**m, "id": cmd.get("id")})
        self.applied.append({"id": cmd.get("id"), "action": "command", "time": _now_iso()})
        return {"ok": True, "id": cmd.get("id"), "op": m["op"], "queued": True}

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
                    "manual": {t: m.state() for t, m in self.manual.items()},
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
    ap.add_argument("action", choices=[a for a in ACTIONS if a not in ("manual", "command")])
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
