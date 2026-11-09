"""The recovery ladder's last step: escalate to an agent, then tell a person
(docs/recovery.md, "Escalation").

When a device's ladder (robofast/recovery.py) is exhausted, the sequencer
opens an **incident**. With an ``agent`` hook configured (and the rate limits
allowing) it runs the hook in the background -- the failing telescope is out
of the plan meanwhile and the others observe on -- with a JSON context: the
telescope, the device, the error, every rung tried and how it went, a recent
excerpt of the telescope's timeline, a status snapshot and what is safe to
do. The hook may try a fix by its own means; it exits 0 if the device should
be retried (robofast then verifies the device itself: it never takes the
agent's word) and nonzero otherwise, and writes a human-readable summary
(diagnosis, what it did, the result) to the file named by
``ROBOFAST_ESCALATION_SUMMARY`` or to stdout.

Whatever happens -- no agent configured, the limit reached, the agent
failed, timed out or fixed it -- the incident ends with **one summary for a
person**: it goes into the sequencer's status snapshot (``incidents``), the
observer console shows it as an alarm with the summary as its detail, and
the alarm notifier (docs/notifier.md) sends it, once, like any alarm. Many
hardware faults cannot be fixed remotely, by a person or an agent: a
telescope the agent does not bring back stays dropped for the night and the
others observe on.

This module is the policy, the agent's runner and the summary; the
sequencer (robofast/sequencer.py) drives it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_SAFE",
    "AgentResult",
    "EscalationPolicy",
    "Incident",
    "fault_signature",
    "run_agent",
]

#: What the agent is told it may and may not do, before any site lines.
DEFAULT_SAFE = (
    "The failing telescope is out of robofast's plan while you work: robofast "
    "sends it nothing until you exit, then only reads to verify the device.",
    "The other telescopes of the enclosure may be observing: never command "
    "them, their devices or their computers.",
    "Never open or close the enclosure, and never command the safety "
    "supervisor, the weather station or the network.",
    "Never power-cycle an outlet other than the failing device's own, and "
    "never one the safety path needs.",
    "Never move the failing telescope (slew, home, unpark) unless the site lines below allow it.",
    "You may read logs and status, restart the failing device's vendor "
    "software or service on its own computer, and reconnect its drivers.",
    "When unsure, change nothing and say so in the summary: a person reads it.",
)


@dataclass
class EscalationPolicy:
    """A device's escalation (docs/recovery.md): the ``escalate`` entry of a
    telescope's ``recovery``, merged with the device's own."""

    #: ``{"command": [argv...], "timeout": s}``; None: no agent, the person
    #: is told at once.
    agent: dict[str, Any] | None = None
    #: Agent runs per device per night (the sequencer runs one night).
    max_per_night: int = 2
    #: Timeline entries of the telescope in the context.
    log_entries: int = 200
    #: Site lines appended to :data:`DEFAULT_SAFE`.
    safe: list[str] = field(default_factory=list)
    #: The agent's summary is cut to this many characters.
    summary_max: int = 4000
    #: Where the agent's working directory (context, summary, output) is
    #: made; default the system's temporary directory.
    workdir: str | None = None

    def __post_init__(self) -> None:
        if self.agent is not None:
            if not isinstance(self.agent, dict):
                raise ValueError("recovery.escalate.agent: {command: [...], timeout: s}")
            bad = set(self.agent) - {"command", "timeout", "env"}
            if bad:
                raise ValueError(f"recovery.escalate.agent: unknown keys {sorted(bad)}")
            cmd = self.agent.get("command")
            if not cmd or not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
                raise ValueError("recovery.escalate.agent.command: a list of strings (no shell)")
            if not os.path.isabs(cmd[0]):
                raise ValueError("recovery.escalate.agent.command[0]: an absolute path")
            t = self.agent.get("timeout", 1800)
            if not isinstance(t, int | float) or t <= 0:
                raise ValueError("recovery.escalate.agent.timeout: seconds, > 0")
            env = self.agent.get("env") or {}
            if not isinstance(env, dict):
                raise ValueError("recovery.escalate.agent.env: a mapping")
        if int(self.max_per_night) < 0:
            raise ValueError("recovery.escalate.max_per_night: >= 0")
        if not isinstance(self.safe, list) or not all(isinstance(s, str) for s in self.safe):
            raise ValueError("recovery.escalate.safe: a list of strings")

    @property
    def timeout(self) -> float:
        return float((self.agent or {}).get("timeout", 1800))

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> EscalationPolicy:
        d = {k: v for k, v in dict(d or {}).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"recovery.escalate: unknown keys {sorted(set(d) - known)}")
        return cls(**d)


def fault_signature(device: str, error: str) -> str:
    """The same fault, whatever its numbers: the device and its error with
    digits and hex runs blanked (so a recurrence is recognised)."""
    text = re.sub(r"0x[0-9a-fA-F]+|\d+(\.\d+)?", "#", error)
    return f"{device}: {text[:200]}"


@dataclass
class AgentResult:
    #: The agent's exit status; None if it did not exit (timed out, or did
    #: not start).
    exit: int | None
    summary: str
    timed_out: bool = False
    #: Why it could not run at all.
    error: str | None = None


def _tail(path: Path, n: int) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4 * n))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace").strip()
    return text if len(text) <= n else "..." + text[-(n - 3) :]


async def run_agent(policy: EscalationPolicy, context: dict[str, Any]) -> AgentResult:
    """Run ``policy.agent`` with ``context`` (module doc): JSON on stdin and in
    the file ``ROBOFAST_ESCALATION_CONTEXT``; no shell; its own process
    group, killed (TERM, then KILL) past the timeout or on cancellation."""
    assert policy.agent is not None
    argv = list(policy.agent["command"])
    base = policy.workdir and os.path.expandvars(policy.workdir)
    work = Path(tempfile.mkdtemp(prefix="robofast-escalation-", dir=base or None))
    try:
        ctx_file, summary_file = work / "context.json", work / "summary.txt"
        out_file, err_file = work / "stdout.txt", work / "stderr.txt"
        data = json.dumps(context, indent=1, default=str).encode()
        fd = os.open(ctx_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        env = {
            **os.environ,
            **{str(k): str(v) for k, v in (policy.agent.get("env") or {}).items()},
            "ROBOFAST_ESCALATION_CONTEXT": str(ctx_file),
            "ROBOFAST_ESCALATION_SUMMARY": str(summary_file),
            "ROBOFAST_ESCALATION_TELESCOPE": str(context.get("telescope", "")),
            "ROBOFAST_ESCALATION_DEVICE": str(context.get("device", "")),
        }
        with (
            ctx_file.open("rb") as stdin,
            out_file.open("wb") as out,
            err_file.open("wb") as err,
        ):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=stdin,
                    stdout=out,
                    stderr=err,
                    env=env,
                    cwd=str(work),
                    start_new_session=True,
                )
            except OSError as exc:
                return AgentResult(None, "", error=f"could not start {argv[0]}: {exc}")
            timed_out = False
            try:
                async with asyncio.timeout(policy.timeout):
                    await proc.wait()
            except TimeoutError:
                timed_out = True
                await _kill(proc)
            except BaseException:  # cancelled (the sequencer stopping): kill it
                await _kill(proc)
                raise
        summary = ""
        with contextlib.suppress(OSError):
            summary = summary_file.read_text(errors="replace").strip()
        if summary:
            if len(summary) > policy.summary_max:
                tail = "\n... (truncated)"
                summary = summary[: policy.summary_max - len(tail)] + tail
        else:
            summary = _tail(out_file, policy.summary_max)
        if not summary:
            stderr = _tail(err_file, 500)
            summary = f"(no summary; stderr: {stderr})" if stderr else "(no summary)"
        if timed_out:
            return AgentResult(None, summary, timed_out=True)
        return AgentResult(proc.returncode, summary)
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _kill(proc: asyncio.subprocess.Process, grace: float = 10.0) -> None:
    """TERM the agent's process group, then KILL it if it lingers."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(proc.wait()), wait)
            return


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Incident:
    """One exhausted ladder, from escalation to the summary for a person."""

    telescope: str
    device: str
    number: int
    error: str
    tried: list[tuple[str, str]]
    started: str = field(default_factory=_now)
    #: ``escalated`` (the agent is working), ``recovered`` or ``dropped``.
    outcome: str = "escalated"
    #: no agent configured / limit reached / the agent's exit / timed out ...
    agent: str = ""
    agent_summary: str = ""
    #: The outcome's reason (robofast's own verification, the limit, ...).
    reason: str = ""
    finished: str | None = None

    @property
    def id(self) -> str:
        return f"{self.telescope}-{self.device}-{self.number}"

    @property
    def level(self) -> str:
        return "alarm" if self.outcome == "recovered" else "critical"

    @property
    def text(self) -> str:
        """The alarm's text: unique per incident (its id is a hash of it),
        so the notifier sends each incident once."""
        what = {
            "recovered": "recovered after escalation",
            "dropped": "dropped for the night, the others observe on",
        }.get(self.outcome, "escalated")
        return f"{self.telescope} {self.device} incident {self.number} ({self.started}): {what}"

    def summary(self) -> str:
        lines = [
            f"{self.telescope} {self.device}: {self.error[:300]}",
            "",
            "Recovery ladder:",
            *(f"  {rung}: {how[:200]}" for rung, how in self.tried),
            "",
            f"Agent: {self.agent}",
        ]
        if self.agent_summary:
            lines += ["Agent's summary:", *("  " + s for s in self.agent_summary.splitlines())]
        outcome = {
            "recovered": "RECOVERED: the device answered robofast's own check; "
            f"{self.telescope} rejoins the night.",
            "dropped": f"DROPPED: {self.telescope} is out for the night; the other "
            "telescopes observe on.",
        }.get(self.outcome, "escalated (the agent is working)")
        lines += ["", f"Outcome: {outcome}"]
        if self.reason:
            lines.append(f"Why: {self.reason}")
        lines.append(f"Started {self.started}, finished {self.finished or '-'}.")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "telescope": self.telescope,
            "device": self.device,
            "error": self.error,
            "tried": [list(t) for t in self.tried],
            "started": self.started,
            "finished": self.finished,
            "outcome": self.outcome,
            "agent": self.agent,
            "level": self.level,
            "text": self.text,
            "summary": self.summary(),
        }

    def finish(self, outcome: str, reason: str) -> None:
        self.outcome, self.reason, self.finished = outcome, reason, _now()
