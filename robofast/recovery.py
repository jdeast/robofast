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
   ssh, or an HTTP request; it must exit 0 once the software answers
   again), then reconnect;
3. ``home`` (telescope only) -- if robofast's PWI server
   (:mod:`robofast.devices.pwi_server`) reports the mount not homed: its
   Alpaca ``FindHome`` (unparking first and parking again after if it was
   parked), wait until it stops, bounded by ``home_timeout``. A server
   that does not allow homing (``allow_find_home`` false: NotImplemented),
   or a telescope that is not a PWI server, **skips** the rung (logged as
   unavailable, never counted as a recovery);
4. ``powercycle`` -- power-cycle the device's PDU outlet through the PDU's
   Alpaca Switch server (:mod:`robofast.devices.pdu_server`,
   ``Action("PowerCycle")``), wait for it to boot, reconnect.

On a telescope served by robofast's PWI server (it answers
``Action("Status")``), ``reconnect`` and ``restart`` also reconnect PWI to
the mount: ``Action("MountConnect")`` if PWI says it is not connected, then
``Action("MotorsEnable")`` if both axes are disabled (a freshly started
PWI's state, not a drive fault: an axis PWI disabled while connected is left
alone).

After each rung the device must answer (``verify``: a read of a member,
within ``verify_timeout``) or the next rung is tried. A PWI telescope must
also be usable: ``Status`` shows it connected with both axes enabled and,
if its steps include ``home``, homed (a mount that answers but is not homed
fails the rung at once, so ``home`` comes next). Every rung is bounded
(``step_timeout``; ``home``: ``home_timeout``) and alarmed (the sequencer's
:meth:`alarm`). A rung that
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
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from robofast.cancel import isolated

log = logging.getLogger(__name__)

__all__ = ["RUNGS", "DevicePolicy", "PDUSwitch", "RecoveryLadder", "RecoveryResult"]

RUNGS = ("reconnect", "restart", "home", "powercycle")
#: Rungs only a telescope has.
TELESCOPE_RUNGS = ("home",)
#: ASCOM NotImplemented and ActionNotImplemented: the device cannot do it.
NOT_IMPLEMENTED = (0x400, 0x40C)
#: robofast's PDU server's power-cycle action (``powercycle.action``).
POWERCYCLE_ACTION = "PowerCycle"
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
    #: ``{"pdu": "host:port", "switch": id, "boot_time": s, "action":
    #: name}`` (the PDU's Alpaca Switch server, the switch id of this
    #: device's outlet, and the server's power-cycle action: default
    #: ``"PowerCycle"``, robofast's PDU server's).
    powercycle: dict[str, Any] | None = None
    step_timeout: float = 180.0
    verify_timeout: float = 60.0
    escalate_window: float = 3600.0
    requires_parked: bool = True
    requires_camera_idle: bool = True
    camera_warm_above: float | None = None
    min_interval: float = 1800.0
    max_powercycles: int = 1
    #: The ``home`` rung's bound (s): a CDK700 homes in 33-58 s, but azimuth
    #: may first travel ~100 deg to its NoWrap sensor (docs/pwi.md).
    home_timeout: float = 300.0

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
        if self.powercycle is not None:
            action = self.powercycle.get("action", POWERCYCLE_ACTION)
            if not isinstance(action, str) or not action:
                raise ValueError("recovery.powercycle.action: an Alpaca action name")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DevicePolicy:
        d = {k: v for k, v in dict(d).items() if not k.startswith("_")}
        known = set(cls.__dataclass_fields__)
        if set(d) - known:
            raise ValueError(f"recovery: unknown keys {sorted(set(d) - known)}")
        return cls(**d)


class Skipped(Exception):
    """A rung that cannot be done here (logged; not a recovery)."""


class NotUsable(Exception):
    """The device answers but cannot observe (not connected, an axis
    disabled, not homed): no point waiting for it."""


def _not_implemented(exc: BaseException) -> bool:
    return getattr(exc, "number", None) in NOT_IMPLEMENTED


@dataclass
class RecoveryResult:
    device: str
    #: The rungs tried and how each went.
    tried: list[tuple[str, str]]
    recovered: bool
    #: Every rung has been used up: give up for the night.
    exhausted: bool


class PDUSwitch:
    """The PDU's Alpaca Switch server (robofast.devices.pdu_server)."""

    def __init__(self, address: str, timeout: float = 15.0) -> None:
        self.address = address
        self.timeout = timeout

    async def cycle(self, switch: int, action: str = POWERCYCLE_ACTION) -> None:
        from robofast.alpaca_client import AlpacaDevice

        dev = AlpacaDevice(self.address, "switch", 0)
        try:
            await dev.connect(timeout=self.timeout)
            await dev.call("Action", action, str(int(switch)), timeout=self.timeout)
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
        r = await isolated(client.request(hook.get("method", "POST"), hook["url"]))
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
        for kind, pol in policies.items():
            only = [s for s in pol.steps if s in TELESCOPE_RUNGS]
            if only and kind != "telescope":
                raise ValueError(f"recovery.{kind}: {only} is a telescope step")
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
        call: Callable[..., Awaitable[Any]] | None = None,
    ) -> RecoveryResult:
        """Climb ``kind``'s ladder (module doc). ``reconnect()`` drops its
        client; ``probe(member)`` reads one of its members; ``others(device,
        member)`` reads another device of the telescope (for the power-cycle
        preconditions); ``call(member, *args)`` calls one of its methods
        (a telescope's PWI actions and ``FindHome``; None: none)."""
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
            bound = pol.home_timeout if rung == "home" else pol.step_timeout
            try:
                async with asyncio.timeout(bound + pol.verify_timeout):
                    await self._rung(rung, kind, pol, reconnect, probe, others, call)
                    await self._verify(kind, pol, reconnect, probe, call)
            except Skipped as exc:
                why = str(exc)
                tried.append((rung, f"skipped: {why}"))
                log.warning("%s: recovery %s skipped: %s", who, rung, why)
                self.event("recovery_step", device=kind, step=rung, ok=False, skipped=why[:200])
                self.alarm(f"{who}: recovery {rung} unavailable: {why[:120]}")
                continue
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

    async def _verify(self, kind: str, pol: DevicePolicy, reconnect, probe, call=None) -> None:
        deadline = self.clock() + pol.verify_timeout
        last: Exception | None = None
        while True:
            try:
                await probe(VERIFY[kind])
                if kind == "telescope":
                    await self._usable(probe, call, need_homed="home" in pol.steps)
                return
            except NotUsable:
                raise
            except Exception as exc:
                last = exc
                reconnect()
            if self.clock() >= deadline:
                raise RuntimeError(f"still not answering: {last}")
            await self.sleep(min(2.0, pol.verify_timeout / 10))

    # -- robofast's PWI server (a telescope) ---------------------------------------------

    @staticmethod
    async def _status(probe, call) -> dict[str, Any] | None:
        """The PWI server's ``Action("Status")``; None if the telescope has
        none (not robofast's PWI server: ``SupportedActions`` does not list
        it, or cannot be read). Other errors propagate."""
        if call is None:
            return None
        try:
            actions = await probe("SupportedActions")
        except Exception:
            return None
        if "status" not in {str(a).lower() for a in actions or ()}:
            return None
        try:
            text = await call("Action", "Status", "")
        except Exception as exc:
            if _not_implemented(exc):
                return None
            raise
        try:
            st = json.loads(text)
        except (TypeError, ValueError):
            return None
        return st if isinstance(st, dict) else None

    async def _usable(self, probe, call, *, need_homed: bool) -> None:
        """Raise :class:`NotUsable` unless the PWI server's ``Status`` shows
        the mount connected, both axes enabled and (``need_homed``) homed."""
        st = await self._status(probe, call)
        if st is None:
            return
        pwi = st.get("pwi") or {}
        if pwi.get("connected") is False:
            raise NotUsable("PWI is not connected to the mount")
        off = [a for a in ("az", "alt") if pwi.get(f"{a}_enabled") is False]
        if off:
            raise NotUsable(f"the {' and '.join(off)} axis is disabled")
        if need_homed and st.get("homed") is not True:
            raise NotUsable("the mount is not homed")

    async def _mount_up(self, kind: str, probe, call) -> None:
        """A PWI telescope: MountConnect if PWI is not connected, then
        MotorsEnable if both axes are disabled (module doc)."""
        st = await self._status(probe, call)
        if st is None:
            return
        assert call is not None
        pwi = st.get("pwi") or {}
        if pwi.get("connected") is not False:
            return
        await call("Action", "MountConnect", "")
        self.event("recovery_mount_connect", device=kind)
        pwi = (await self._status(probe, call) or {}).get("pwi") or {}
        if pwi.get("az_enabled") is False and pwi.get("alt_enabled") is False:
            await call("Action", "MotorsEnable", "")
            self.event("recovery_motors_enable", device=kind)

    async def _until_still(self, probe, poll: float = 1.0) -> None:
        """Until the mount stops (homing reads as Slewing)."""
        await self.sleep(poll)
        while await probe("Slewing"):
            await self.sleep(poll)

    async def _home(self, kind: str, probe, call) -> None:
        st = await self._status(probe, call)
        if st is None:
            raise Skipped("no Status action (not robofast's PWI server, or it does not answer)")
        assert call is not None
        if st.get("homed") is True:
            raise Skipped("the mount reports itself homed")
        if await probe("CanFindHome") is False:
            raise Skipped("FindHome is not allowed by the server (CanFindHome false)")
        await self._mount_up(kind, probe, call)
        parked = bool(await probe("AtPark"))
        if parked:
            await call("Unpark")
        try:
            await call("FindHome")
        except Exception as exc:
            if _not_implemented(exc):
                if parked:
                    await call("Park")
                raise Skipped(f"FindHome not allowed by the server ({exc})") from None
            raise
        self.event("recovery_home", device=kind)
        await self._until_still(probe)
        if parked:
            await call("Park")
            await self._until_still(probe)

    async def _rung(self, rung, kind, pol: DevicePolicy, reconnect, probe, others, call) -> None:
        if rung == "reconnect":
            reconnect()
            if kind == "telescope":
                await self._mount_up(kind, probe, call)
            return
        if rung == "home":
            await self._home(kind, probe, call)
            return
        if rung == "restart":
            assert pol.restart is not None
            out = await self.hook(pol.restart, pol.step_timeout)
            self.event("recovery_restart", device=kind, output=out[-200:])
            if pol.restart.get("settle"):
                await self.sleep(float(pol.restart["settle"]))
            reconnect()
            if kind == "telescope":
                await self._mount_up(kind, probe, call)
            return
        assert rung == "powercycle" and pol.powercycle is not None
        await self._may_cycle(kind, pol, probe, others)
        pc = pol.powercycle
        self.cycles.setdefault(kind, []).append(self.clock())
        await self.pdu(str(pc["pdu"])).cycle(
            int(pc["switch"]), action=str(pc.get("action", POWERCYCLE_ACTION))
        )
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
