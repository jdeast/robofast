"""The recovery ladder: what the sequencer tries when a device fails, before
it gives up on a telescope for the night (docs/recovery.md).

Legacy MINERVA had ``recover()``, ``killPWI``/``restartPWI`` and
``powercycle`` in ``cdk700.py``, called ad hoc. Here each device of a
telescope (``telescope``, ``camera``, ``focuser``, ``rotator``,
``filterwheel``) may have a :class:`DevicePolicy` listing the rungs to
climb, in order:

1. ``reconnect`` -- drop the device's Alpaca client and connect afresh;
2. ``restart`` -- restart the vendor software through a configured hook
   (a command, e.g. a service restart on the telescope's Windows PC over
   ssh, or an HTTP request), then reconnect;
3. ``powercycle`` -- power-cycle the device's PDU outlet through the PDU's
   Alpaca Switch server (:mod:`robofast_core.devices.pdu_server`,
   ``Action("PowerCycle")``), wait for it to boot, reconnect.

After each rung the device must answer (``verify``: a read of a member,
within ``verify_timeout``) or the next rung is tried. Every rung is bounded
(``step_timeout``) and alarmed (the sequencer's :meth:`alarm`). A rung that
worked is remembered: if the same device fails again within
``escalate_window`` the ladder starts one rung higher, and once every rung
has been tried and it still fails, the telescope **gives up for the night**.
A quiet ``escalate_window`` resets it.

Power cycling is guarded: only outlets the policy names (and the PDU
server marks ``cycle``: never the enclosure controller, the weather
station, the network or anything else the safety path needs), at most
``max_powercycles`` a night and once per ``min_interval`` s, and only with
the telescope parked (``requires_parked``) and the camera idle and, if
``camera_warm_above`` is set, warmer than that (so a cooled CCD is never
cut off cold) -- each check skipped only for the device being cycled
itself when it cannot answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["RUNGS", "DevicePolicy", "PDUSwitch", "RecoveryLadder", "RecoveryResult"]

RUNGS = ("reconnect", "restart", "powercycle")
DEVICES = ("telescope", "camera", "focuser", "rotator", "filterwheel")
#: The member read to check a device answers.
VERIFY = {
    "telescope": "AtPark",
    "camera": "CameraState",
    "focuser": "Position",
    "rotator": "Position",
    "filterwheel": "Position",
}


@dataclass
class DevicePolicy:
    """One device's rungs and their settings (module doc)."""

    steps: list[str] = field(default_factory=lambda: ["reconnect"])
    #: ``{"command": [...]}`` (run, must exit 0) or ``{"url": ..., "method":
    #: "POST"}``; ``settle`` s after it.
    restart: dict[str, Any] | None = None
    #: ``{"pdu": "host:port", "switch": id, "boot_time": s}`` (the PDU's
    #: Alpaca Switch server and the switch id of this device's outlet).
    powercycle: dict[str, Any] | None = None
    step_timeout: float = 180.0
    verify_timeout: float = 60.0
    escalate_window: float = 3600.0
    requires_parked: bool = True
    requires_camera_idle: bool = True
    camera_warm_above: float | None = None
    min_interval: float = 1800.0
    max_powercycles: int = 1

    def __post_init__(self) -> None:
        bad = [s for s in self.steps if s not in RUNGS]
        if bad:
            raise ValueError(f"recovery steps {bad}: expected some of {RUNGS}")
        if "restart" in self.steps and not self.restart:
            raise ValueError("recovery: a restart step needs a restart hook")
        if self.restart and not (self.restart.get("command") or self.restart.get("url")):
            raise ValueError("recovery.restart: a command or a url")
        if "powercycle" in self.steps:
            pc = self.powercycle or {}
            if not pc.get("pdu") or pc.get("switch") is None:
                raise ValueError("recovery: a powercycle step needs powercycle.pdu and .switch")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DevicePolicy:
        d = {k: v for k, v in dict(d).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"recovery: unknown keys {sorted(set(d) - known)}")
        return cls(**d)


@dataclass
class RecoveryResult:
    device: str
    #: The rungs tried and how each went.
    tried: list[tuple[str, str]]
    recovered: bool
    #: Every rung has been used up: give up for the night.
    exhausted: bool


class PDUSwitch:
    """The PDU's Alpaca Switch server (robofast_core.devices.pdu_server)."""

    def __init__(self, address: str, timeout: float = 15.0) -> None:
        self.address = address
        self.timeout = timeout

    async def cycle(self, switch: int) -> None:
        from robofast_core.alpaca_client import AlpacaDevice

        dev = AlpacaDevice(self.address, "switch", 0)
        try:
            await dev.connect(timeout=self.timeout)
            await dev.call("Action", "PowerCycle", str(int(switch)), timeout=self.timeout)
        finally:
            await dev.aclose()


async def run_hook(hook: dict[str, Any], timeout: float) -> str:
    """Run a restart hook (module doc); raise on failure."""
    if hook.get("command"):
        cmd = [str(c) for c in hook["command"]]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"restart hook {cmd[0]} timed out after {timeout} s") from None
        text = out.decode("utf-8", "replace").strip()[-300:]
        if proc.returncode != 0:
            raise RuntimeError(f"restart hook {cmd[0]} exited {proc.returncode}: {text}")
        return text
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(hook.get("method", "POST"), hook["url"])
        r.raise_for_status()
        return r.text[-300:]


class RecoveryLadder:
    """One telescope's recovery policies (module doc).

    ``policies`` by device kind. ``alarm(text)`` and ``event(what, **kw)``
    report; ``pdu`` makes a :class:`PDUSwitch`-like object from an address
    (tests pass fakes); ``hook`` runs a restart hook."""

    def __init__(
        self,
        telescope: str,
        policies: dict[str, DevicePolicy],
        *,
        alarm: Callable[[str], None] | None = None,
        event: Callable[..., None] | None = None,
        pdu: Callable[[str], Any] = PDUSwitch,
        hook: Callable[[dict[str, Any], float], Awaitable[str]] = run_hook,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        bad = set(policies) - set(DEVICES)
        if bad:
            raise ValueError(f"recovery: unknown devices {sorted(bad)}")
        self.telescope = telescope
        self.policies = policies
        self.alarm = alarm or (lambda text: None)
        self.event = event or (lambda what, **kw: None)
        self.pdu = pdu
        self.hook = hook
        self.clock = clock
        self.sleep = sleep
        #: Next rung per device, when it last failed, power cycles tonight.
        self.level: dict[str, int] = {}
        self.last_failure: dict[str, float] = {}
        self.cycles: dict[str, list[float]] = {}
        self.history: list[RecoveryResult] = []

    @classmethod
    def from_config(cls, telescope: str, d: dict[str, Any], **kw: Any) -> RecoveryLadder:
        d = {k: v for k, v in dict(d).items() if not k.startswith("_")}
        return cls(telescope, {k: DevicePolicy.from_dict(v) for k, v in d.items()}, **kw)

    def device_for(self, error: str, names: dict[str, str]) -> str | None:
        """Which device an error came from: ``names`` maps device kind to
        its owner name (DeviceError messages start with it)."""
        for kind, name in names.items():
            if kind in self.policies and f"{name}." in error:
                return kind
        return None

    async def recover(
        self,
        kind: str,
        *,
        reconnect: Callable[[], None],
        probe: Callable[[str], Awaitable[Any]],
        others: Callable[[str, str], Awaitable[Any]] | None = None,
    ) -> RecoveryResult:
        """Climb ``kind``'s ladder (module doc). ``reconnect()`` drops its
        client; ``probe(member)`` reads one of its members; ``others(device,
        member)`` reads another device of the telescope (for the power-cycle
        preconditions)."""
        pol = self.policies[kind]
        now = self.clock()
        if now - self.last_failure.get(kind, -1e18) > pol.escalate_window:
            self.level[kind] = 0
        self.last_failure[kind] = now
        start = self.level.get(kind, 0)
        tried: list[tuple[str, str]] = []
        who = f"{self.telescope} {kind}"
        for i in range(start, len(pol.steps)):
            rung = pol.steps[i]
            self.level[kind] = i + 1
            try:
                async with asyncio.timeout(pol.step_timeout + pol.verify_timeout):
                    await self._rung(rung, kind, pol, reconnect, probe, others)
                    await self._verify(kind, pol, reconnect, probe)
            except Exception as exc:
                why = str(exc) or type(exc).__name__
                tried.append((rung, f"failed: {why}"))
                self.event("recovery_step", device=kind, step=rung, ok=False, error=why[:200])
                self.alarm(f"{who}: recovery {rung} failed: {why[:120]}")
                continue
            tried.append((rung, "ok"))
            self.event("recovery_step", device=kind, step=rung, ok=True)
            self.alarm(f"{who}: recovered by {rung}")
            res = RecoveryResult(kind, tried, True, False)
            self.history.append(res)
            return res
        res = RecoveryResult(kind, tried, False, True)
        self.history.append(res)
        self.alarm(f"{who}: recovery ladder exhausted ({', '.join(pol.steps)}): giving up tonight")
        return res

    async def _verify(self, kind: str, pol: DevicePolicy, reconnect, probe) -> None:
        deadline = self.clock() + pol.verify_timeout
        last: Exception | None = None
        while True:
            try:
                await probe(VERIFY[kind])
                return
            except Exception as exc:
                last = exc
                reconnect()
            if self.clock() >= deadline:
                raise RuntimeError(f"still not answering: {last}")
            await self.sleep(min(2.0, pol.verify_timeout / 10))

    async def _rung(self, rung, kind, pol: DevicePolicy, reconnect, probe, others) -> None:
        if rung == "reconnect":
            reconnect()
            return
        if rung == "restart":
            assert pol.restart is not None
            out = await self.hook(pol.restart, pol.step_timeout)
            self.event("recovery_restart", device=kind, output=out[-200:])
            if pol.restart.get("settle"):
                await self.sleep(float(pol.restart["settle"]))
            reconnect()
            return
        assert rung == "powercycle" and pol.powercycle is not None
        await self._may_cycle(kind, pol, probe, others)
        pc = pol.powercycle
        self.cycles.setdefault(kind, []).append(self.clock())
        await self.pdu(str(pc["pdu"])).cycle(int(pc["switch"]))
        self.event("recovery_powercycle", device=kind, switch=int(pc["switch"]))
        await self.sleep(float(pc.get("boot_time", 60.0)))
        reconnect()

    async def _may_cycle(self, kind: str, pol: DevicePolicy, probe, others) -> None:
        done = self.cycles.get(kind, [])
        if len(done) >= pol.max_powercycles:
            raise RuntimeError(f"already power-cycled {len(done)} time(s) tonight")
        if done and self.clock() - done[-1] < pol.min_interval:
            raise RuntimeError(f"power-cycled {self.clock() - done[-1]:.0f} s ago")

        async def read(device: str, member: str) -> Any:
            if device == kind:
                try:
                    return await probe(member)
                except Exception:
                    return None  # the device being cycled cannot answer: skip its checks
            if others is None:
                raise RuntimeError(f"cannot check the {device}")
            return await others(device, member)

        if pol.requires_parked:
            parked = await read("telescope", "AtPark")
            if parked is False or (parked is None and kind != "telescope"):
                raise RuntimeError("the telescope is not parked")
        if pol.requires_camera_idle:
            state = await read("camera", "CameraState")
            if state not in (None, 0) or (state is None and kind != "camera"):
                raise RuntimeError(f"the camera is not idle (state {state})")
        if pol.camera_warm_above is not None:
            temp = await read("camera", "CCDTemperature")
            if temp is not None and float(temp) < pol.camera_warm_above:
                raise RuntimeError(
                    f"the camera is at {float(temp):.1f} C (below {pol.camera_warm_above} C)"
                )
            if temp is None and kind != "camera":
                raise RuntimeError("the camera's temperature is unknown")
