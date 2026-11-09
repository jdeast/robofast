"""Enclosure operating modes (docs/modes.md, docs/design.md 3.10 (b)).

An enclosure runs in one **mode** at a time: a named bundle of policy that
the safety supervisor, the sequencer, the observer console and the request
API all read from the supervisor and honor. The site config says which modes
exist and what each means; robofast ships four built-in definitions::

    {
      "mode": "attended",                 # the default (missing/corrupt state file)
      "modes": {
        "robotic": {...}, "attended": {...}, "engineering": {...}, "shadow": {...},
        "my_mode": {"like": "attended", "duty_ttl": 600, "description": "..."}
      }
    }

A ``modes`` entry for a built-in name changes only the fields it gives; a
new name starts from ``like`` (a built-in or an earlier entry) or gives every
field; ``null`` removes a built-in. Each mode (:class:`ModePolicy`):

* ``open`` -- may the supervisor grant a lease (and so open) at all;
* ``observe`` -- what the sequencer does: ``plan`` (observe the plan, as
  always), ``manual`` (nothing autonomous: only the console's manual
  sessions run, and they may ask for the lease), ``shadow`` (ask the plan
  what it would do and log it; command nothing that moves: no generation,
  no startup park);
* ``enclosure`` -- ``command`` (the supervisor opens and closes, as
  always) or ``watch`` (it reads the enclosure and logs what it would
  command, and commands nothing; ``open`` must be false);
* ``requests`` -- the request API accepts new requests and ToOs;
* the operator-duty checks: ``attended_only`` (no lease unless an operator
  is on duty), ``duty_expiry`` (``close``: an expired duty closes after
  ``duty_grace``, as before; ``alarm``: it only raises the critical alarm),
  ``duty_ttl`` and ``duty_grace`` (null: the ``safety`` entry's);
* ``fault_auto_clear`` -- the supervisor's automatic fault clearing (null:
  the ``safety`` entry's; true needs ``safety.fault_action``);
* ``intervene`` -- a ``watch`` mode still closes an enclosure found open
  (by anyone) while an enforced close condition is active: the weather
  verdict unsafe, or an enclosure fault (default true for ``shadow``; JDE
  2026-11-02: "if there's an active safety risk, it should intervene
  immediately"). Ignored by a ``command`` mode, which closes anyway.

The other checks are not per mode: the weather verdict and its staleness,
enclosure faults, the shutter's health, the lease and its renewal, the park
check before a lease and the hardware heartbeat are enforced in every mode
that may open (docs/modes.md says why).

An enclosure that must not run unattended (``safety.attended_only``: by
default, one with no hardware heartbeat) is a floor no mode lowers: every
mode that may open there needs ``attended_only`` true and ``duty_expiry``
``close``; the built-in ``robotic`` is not offered there, and a site's own
definition that breaks the rule is refused when the config is loaded. A site
may lift that floor on purpose (``safety.allow_unattended_without_watchdog``,
docs/modes.md "Unattended without a watchdog"): then every mode is offered and
the supervisor warns loudly while it runs in one that is :func:`unattended`.

The active mode is kept in a small JSON state file (:class:`ModeStore`;
``mode_file`` in the config, default ``modes/<enclosure>.json`` under the
data directory), so it survives a restart. A missing or unreadable file
means the config's ``mode`` (a site should make that its safe one); the
supervisor says so in its log and alarms.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_MODES",
    "ModeGate",
    "ModePolicy",
    "ModeSet",
    "ModeStore",
    "lease_survives",
    "mode_path",
    "modes_from_config",
    "unattended",
]

OBSERVE = ("plan", "manual", "shadow")
ENCLOSURE = ("command", "watch")
DUTY_EXPIRY = ("close", "alarm")
STATE_VERSION = 1


@dataclass(frozen=True)
class ModePolicy:
    """One mode's policy (module doc)."""

    open: bool
    observe: str
    requests: bool
    attended_only: bool
    enclosure: str = "command"
    duty_expiry: str = "close"
    duty_ttl: float | None = None
    duty_grace: float | None = None
    fault_auto_clear: bool | None = None
    intervene: bool = False
    description: str = ""

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


#: The built-in definitions (docs/modes.md); a site's ``modes`` changes them.
BUILTIN_MODES: dict[str, ModePolicy] = {
    "robotic": ModePolicy(
        open=True,
        observe="plan",
        requests=True,
        attended_only=False,
        description="unattended: the plan runs and the supervisor opens by its own policy",
    ),
    "attended": ModePolicy(
        open=True,
        observe="plan",
        requests=True,
        attended_only=True,
        description="the plan runs, but nothing opens unless an operator is on duty",
    ),
    "engineering": ModePolicy(
        open=True,
        observe="manual",
        requests=False,
        attended_only=True,
        fault_auto_clear=False,
        description=(
            "no autonomous observing: only the console's manual control, with an"
            " operator on duty; no automatic fault clearing; no new requests"
        ),
    ),
    "shadow": ModePolicy(
        open=False,
        observe="shadow",
        requests=True,
        attended_only=False,
        enclosure="watch",
        fault_auto_clear=False,
        intervene=True,
        description=(
            "compute and log what robofast would do; command nothing that moves,"
            " except to close an open enclosure on an active safety risk (rain, a fault)"
        ),
    ),
}

_FIELDS = {f.name for f in fields(ModePolicy)}
_REQUIRED = ("open", "observe", "requests", "attended_only")


def _policy(name: str, entry: dict[str, Any], base: ModePolicy | None, where: str) -> ModePolicy:
    """A :class:`ModePolicy` from a config entry over ``base``; ValueError
    naming the entry's path."""
    unknown = set(entry) - _FIELDS - {"like"}
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)} (known: {sorted(_FIELDS)})")
    if base is None:
        missing = [k for k in _REQUIRED if k not in entry]
        if missing:
            raise ValueError(
                f"{where}: a new mode gives {', '.join(missing)} (or starts from 'like':"
                " another mode)"
            )
    values = {} if base is None else asdict(base)
    for key, value in entry.items():
        if key == "like":
            continue
        at = f"{where}.{key}"
        if key in ("open", "requests", "attended_only", "intervene"):
            if not isinstance(value, bool):
                raise ValueError(f"{at}: true or false")
        elif key == "fault_auto_clear":
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{at}: true, false, or null (the safety entry's)")
        elif key == "observe":
            if value not in OBSERVE:
                raise ValueError(f"{at}: one of {', '.join(OBSERVE)}")
        elif key == "enclosure":
            if value not in ENCLOSURE:
                raise ValueError(f"{at}: one of {', '.join(ENCLOSURE)}")
        elif key == "duty_expiry":
            if value not in DUTY_EXPIRY:
                raise ValueError(f"{at}: one of {', '.join(DUTY_EXPIRY)}")
        elif key in ("duty_ttl", "duty_grace"):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value < 0
                or (key == "duty_ttl" and value == 0)
            ):
                raise ValueError(f"{at}: seconds (> 0 for duty_ttl, >= 0 for duty_grace) or null")
            value = None if value is None else float(value)
        elif key == "description":
            if not isinstance(value, str):
                raise ValueError(f"{at}: text")
        values[key] = value
    policy = ModePolicy(**values)
    if policy.observe == "shadow" and policy.open:
        raise ValueError(f"{where}: observe 'shadow' commands nothing, so open must be false")
    if policy.enclosure == "watch" and policy.open:
        raise ValueError(f"{where}: enclosure 'watch' commands nothing, so open must be false")
    return policy


@dataclass(frozen=True)
class ModeSet:
    """An enclosure's modes and its default (module doc)."""

    modes: dict[str, ModePolicy]
    default: str
    #: The enclosure must not run unattended (``safety.attended_only``).
    attended_floor: bool = False
    #: The default came from the config (False: no ``mode`` there, so the
    #: one matching the old behavior: ``robotic``, or ``attended`` on an
    #: attended-only enclosure).
    configured: bool = True

    def get(self, name: str) -> ModePolicy:
        try:
            return self.modes[name]
        except KeyError:
            raise ValueError(f"unknown mode {name!r}: one of {', '.join(self.modes)}") from None

    def as_json(self) -> dict[str, Any]:
        return {name: p.as_json() for name, p in self.modes.items()}


def modes_from_config(
    config: dict[str, Any],
    *,
    attended_floor: bool = False,
    fault_action: bool = True,
    enclosure: bool = True,
    where: str = "",
) -> ModeSet:
    """The ``mode`` and ``modes`` entries of an enclosure config (module
    doc). ``attended_floor``: the enclosure's ``safety.attended_only``;
    ``fault_action``: it has a fault channel (``fault_auto_clear`` true needs
    one); ``enclosure`` false: there is none (``"enclosure": "none"``), so
    the built-in modes that only watch one are left out and a configured
    one is an error. Errors name the entry's path (``where``: the file)."""
    pre = f"{where}: " if where else ""
    raw = config.get("modes")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"{pre}modes: {{name: {{...}}}} (docs/modes.md)")
    modes: dict[str, ModePolicy] = {
        name: p
        for name, p in BUILTIN_MODES.items()
        if not (attended_floor and unattended(p)) and (enclosure or p.enclosure != "watch")
    }
    for name, entry in raw.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError(f"{pre}modes: {name!r} is not a mode name (letters, digits, _)")
        at = f"{pre}modes.{name}"
        if name.startswith("_"):
            continue  # a comment
        if entry is None:
            modes.pop(name, None)
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"{at}: {{field: value}} or null (to remove a built-in)")
        base: ModePolicy | None = modes.get(name) or BUILTIN_MODES.get(name)
        if "like" in entry:
            like = entry["like"]
            if not isinstance(like, str) or (like not in modes and like not in BUILTIN_MODES):
                known = ", ".join(sorted(set(modes) | set(BUILTIN_MODES)))
                raise ValueError(f"{at}.like: one of {known}")
            base = modes.get(like) or BUILTIN_MODES[like]
        policy = _policy(name, entry, base, at)
        if attended_floor and unattended(policy):
            raise ValueError(
                f"{at}: this enclosure must not run unattended (safety.attended_only: no"
                " hardware heartbeat), so a mode that may open needs attended_only true and"
                " duty_expiry 'close' (or the site opts in:"
                " safety.allow_unattended_without_watchdog, docs/modes.md)"
            )
        if not enclosure and policy.enclosure == "watch":
            raise ValueError(f"{at}: enclosure 'watch' needs an enclosure (this site has none)")
        if policy.fault_auto_clear and not fault_action:
            raise ValueError(f"{at}.fault_auto_clear: needs safety.fault_action (a fault channel)")
        modes[name] = policy
    if not modes:
        raise ValueError(f"{pre}modes: no mode left")
    default = config.get("mode")
    configured = default is not None
    if default is None:
        default = "attended" if attended_floor else "robotic"
        if default not in modes:
            raise ValueError(f"{pre}mode: name the default mode (one of {', '.join(modes)})")
    if not isinstance(default, str) or default not in modes:
        raise ValueError(f"{pre}mode: {default!r} is not a mode here (one of {', '.join(modes)})")
    return ModeSet(modes, default, attended_floor, configured)


def unattended(p: ModePolicy) -> bool:
    """May open without an operator on duty, or keep open after duty expires."""
    return p.open and (not p.attended_only or p.duty_expiry != "close")


def lease_survives(old: ModePolicy, new: ModePolicy) -> bool:
    """May a live lease granted under ``old`` go on under ``new``? Only if
    the new mode may open and does the same work (a different ``observe``
    means the generation was chosen under other rules): anything else is the
    normal close/park path. Duty is checked by the supervisor on top."""
    return new.open and new.enclosure == "command" and new.observe == old.observe


# -- the state file ------------------------------------------------------------------


class ModeStore:
    """The active mode, kept in a JSON file (atomically written)::

        {"version": 1, "mode": "robotic", "since": "...", "by": "...", "reason": "..."}

    ``path`` None: in memory only (tests, simulators)."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = None if path is None else Path(path)
        self.error: str | None = None

    def load(self, modes: ModeSet) -> tuple[str, dict[str, Any], str | None]:
        """(mode, its record, why the default was used or None). A missing
        file (a first start) says ``no mode file``; anything else is a file
        that is there but unusable."""
        if self.path is None:
            return modes.default, {}, None
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return modes.default, {}, f"no mode file {self.path}"
        except (OSError, ValueError) as exc:
            return modes.default, {}, f"mode file {self.path} unreadable ({exc})"
        if not isinstance(data, dict) or not isinstance(data.get("mode"), str):
            return modes.default, {}, f"mode file {self.path} has no mode"
        if data["mode"] not in modes.modes:
            return (
                modes.default,
                {},
                f"mode file {self.path}: {data['mode']!r} is not a mode of this config",
            )
        return data["mode"], data, None

    def save(self, record: dict[str, Any]) -> bool:
        """Write the record; False (and :attr:`error`) if it could not be."""
        if self.path is None:
            return True
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
            with os.fdopen(fd, "w") as f:
                json.dump({"version": STATE_VERSION, **record}, f, indent=1)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)
            self.error = f"could not write {self.path}: {exc}"
            log.error("mode: %s (the mode is kept in memory)", self.error)
            return False
        self.error = None
        return True


def mode_path(config: dict[str, Any]) -> Path:
    """Where the enclosure's mode is kept: ``mode_file`` (relative: under the
    data directory), default ``modes/<name>.json`` there."""
    from robofast.zenodo import data_dir

    raw = config.get("mode_file")
    if raw is None:
        return data_dir() / "modes" / f"{config.get('name', 'enclosure')}.json"
    if not isinstance(raw, str) or not raw:
        raise ValueError("mode_file: a path (relative: under the data directory)")
    p = Path(raw).expanduser()
    return p if p.is_absolute() else data_dir() / p


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# -- the command line ------------------------------------------------------------------


def _supervisor_url(config: dict[str, Any]) -> str:
    return config.get("supervisor_url") or f"http://{config.get('listen', '127.0.0.1:8100')}"


def main(argv: list[str] | None = None) -> None:
    """``python -m robofast.modes`` (docs/modes.md): show the modes, or
    switch the running supervisor's mode (logged with who and why)."""
    import httpx

    from robofast.config import load_config

    ap = argparse.ArgumentParser(description="Show or switch an enclosure's operating mode.")
    ap.add_argument("--config", required=True, help="the enclosure config file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="the active mode and the modes defined")
    s = sub.add_parser("set", help="switch the mode (the supervisor must be running)")
    s.add_argument("mode")
    s.add_argument("--operator", required=True, help="who is switching")
    s.add_argument("--reason", required=True, help="why")
    args = ap.parse_args(argv)
    config = load_config(args.config)
    url = _supervisor_url(config)
    try:
        if args.cmd == "show":
            r = httpx.get(f"{url}/status", timeout=10)
            r.raise_for_status()
            print(json.dumps(r.json().get("mode"), indent=1))
            return
        r = httpx.post(
            f"{url}/mode",
            json={"operator": args.operator, "mode": args.mode, "reason": args.reason},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        sys.exit(f"{url}: {exc}")
    if r.status_code != 200:
        sys.exit(f"refused: {r.text[:500]}")
    print(json.dumps(r.json(), indent=1))


if __name__ == "__main__":
    main()


# -- the sequencer's side --------------------------------------------------------------


class ModeGate:
    """What the sequencer asks instead of its plan (docs/modes.md): the
    plan's ``next_observation`` and ``peek``, as the enclosure's mode allows.

    The mode is the supervisor's (``source().mode_status()``: the in-process
    :class:`~robofast.safety.SafetySupervisor`'s, or a
    :class:`~robofast.lease_client.RemoteSupervisor`'s ``GET /status``),
    read at most every ``refresh`` seconds. ``observe``:

    * ``plan`` -- the plan's answers, unchanged;
    * ``manual`` -- only a telescope under the console's manual control is
      asked (its plan, the :class:`~robofast.console.hold.OperatorHold`,
      answers with the manual session); every other one is told
      :class:`~robofast.plan.Idle`;
    * ``shadow`` -- the plan is only *peeked* (no state changes) and what it
      would do is logged (``shadow_would`` events, on each change); every
      telescope is told Idle.

    A source without ``mode_status`` (a test's fake), or a supervisor whose
    status has no mode (before modes), is ``plan``. A mode that cannot be
    read (the supervisor unreachable, never read yet) is treated as
    ``shadow``: nothing autonomous starts until it is known.
    """

    def __init__(
        self,
        plan: Any,
        source: Callable[[], Any],
        event: Callable[..., None],
        *,
        idle: float = 10.0,
        refresh: float = 2.0,
    ) -> None:
        self.plan = plan
        self.source = source
        self.event = event
        self.idle = idle
        self.refresh = refresh
        #: The mode's name and its ``observe`` (None: not known).
        self.mode: str | None = None
        self.observe: str | None = None
        self.policy: dict[str, Any] = {}
        #: The supervisor's no-watchdog warning (``unwatched`` in its mode
        #: status; None: the enclosure has a watchdog or is attended only).
        self.unwatched: dict[str, Any] | None = None
        self._read_at = -math.inf
        self._shadow: dict[str, str] = {}

    async def update(self, force: bool = False) -> str:
        """Read the mode (at most every ``refresh`` s); returns ``observe``
        (``unknown`` while it cannot be read)."""
        import inspect
        import time

        fn = getattr(self.source(), "mode_status", None)
        if fn is None:
            self.observe = "plan"
            return "plan"
        now = time.monotonic()
        if not force and now - self._read_at < self.refresh and self.observe is not None:
            return self.observe
        try:
            st = fn()
            if inspect.isawaitable(st):
                st = await st
        except Exception as exc:
            log.warning("mode: cannot read the supervisor's mode: %r", exc)
            return self.observe or "unknown"
        self._read_at = now
        if st is None:  # a supervisor from before modes
            name, policy = None, {"observe": "plan"}
            self.unwatched = None
        else:
            name, policy = st.get("mode"), dict(st.get("policy") or {})
            uw = st.get("unwatched")
            self.unwatched = dict(uw) if isinstance(uw, dict) else None
        observe = policy.get("observe") if policy.get("observe") in OBSERVE else "shadow"
        if name != self.mode or observe != self.observe:
            was = self.mode
            self.mode, self.observe, self.policy = name, observe, policy
            self._shadow.clear()
            self.event("mode_seen", mode=name, was=was, observe=observe)
            hook = getattr(self.plan, "set_mode", None)
            if hook is not None:
                try:
                    hook(name, policy)
                except Exception:
                    log.exception("plan.set_mode")
        return observe

    async def next_observation(self, telescope: str) -> Any:
        return await self._ask(telescope, "next_observation")

    async def peek(self, telescope: str) -> Any:
        return await self._ask(telescope, "peek")

    async def _ask(self, telescope: str, how: str) -> Any:
        from robofast.plan import Idle

        observe = await self.update()
        if observe == "plan":
            return await getattr(self.plan, how)(telescope)
        if observe == "manual":
            session = getattr(self.plan, "manual_session", None)
            if session is not None and session(telescope) is not None:
                return await getattr(self.plan, how)(telescope)
            if await self.shadow(telescope) is _OVER:
                return None
            return Idle(self.idle, f"mode {self.mode}: manual control only")
        if await self.shadow(telescope) is _OVER:
            return None  # the night is over for the plan too
        return Idle(self.idle, f"mode {self.mode or 'unknown'}: nothing autonomous")

    async def shadow(self, telescope: str) -> object:
        """Peek at what the plan would do for ``telescope`` now (no state
        changes); in shadow, log it on each change. Returns ``_OVER`` when
        the plan has nothing more tonight (so the sequencer still ends its
        night), else None."""
        peek = getattr(self.plan, "peek", None)
        if peek is None or self.observe not in ("shadow", "manual"):
            return None
        try:
            answer = await peek(telescope)
        except Exception as exc:
            answer, what = False, f"plan error: {exc!r}"
        else:
            what = _describe(answer)
        if self.observe == "shadow" and self._shadow.get(telescope) != what:
            self._shadow[telescope] = what
            self.event("shadow_would", telescope=telescope, mode=self.mode, would=what)
        return _OVER if answer is None else None


#: :meth:`ModeGate.shadow`: the plan has nothing more tonight.
_OVER = object()


def _describe(answer: Any) -> str:
    if answer is None:
        return "nothing more tonight"
    seconds = getattr(answer, "seconds", None)
    if seconds is not None and not hasattr(answer, "kind"):
        return f"idle ({getattr(answer, 'reason', '') or 'nothing observable'})"
    light = "light" if getattr(answer, "light", False) else "dark"
    return f"observe {getattr(answer, 'name', answer)} ({getattr(answer, 'kind', '?')}, {light})"
