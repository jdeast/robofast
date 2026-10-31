"""A rig of pure-Python fake devices behind real owners, supervisor and sequencer.

The enclosure holds ``telescopes`` telescope+camera pairs (default 1), named
``t1``, ``t2``, ...; their owners are ``t1.telescope``, ``t1.camera``, ...

``lease_path`` selects how the sequencer reaches the supervisor: ``"local"``
(the in-process object) or ``"remote"`` (the supervisor's HTTP API on a
loopback port, through :class:`RemoteSupervisor`). A test module
parametrizes it to run both.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

import pytest

from robofast_core import (
    DeviceOwner,
    Observation,
    SafetyConfig,
    SafetySupervisor,
    Sequencer,
    SequencerConfig,
    Timeline,
)
from robofast_core.lease_client import RemoteSupervisor
from robofast_core.safety import ENCLOSURE_MEMBERS
from robofast_core.sequencer import TelescopeUnit
from robofast_core.supervisor_service import SupervisorServer
from robofast_core.testing.fakes import (
    FakeCamera,
    FakeDome,
    FakeFilterWheel,
    FakeObservingConditions,
    FakeSafetyMonitor,
    FakeTelescope,
    FakeWeather,
)

#: Fast timings for unit tests (seconds).
FAST_SAFETY = dict(
    poll_interval=0.05,
    read_timeout=0.3,
    stale_after=0.6,
    reopen_hold=0.5,
    lease_ttl=1.0,
    shutter_timeout=2.0,
    dome_timeout=0.3,
)
FAST_SEQ = dict(
    poll_interval=0.02,
    slew_timeout=3.0,
    readout_margin=2.0,
    lease_renew_interval=0.2,
    cancel_ack_timeout=1.0,
    park_timeout=2.0,
    error_backoff=0.5,
    max_consecutive_errors=2,
    recover_poll=0.1,
)
#: RemoteSupervisor timings for the "remote" lease path.
FAST_REMOTE = dict(timeout=0.5, poll=0.3, retry=0.1)


@dataclass
class Rig:
    timeline: Timeline
    weather: FakeWeather
    dome: FakeDome
    tels: list[FakeTelescope]
    cams: list[FakeCamera]
    sm: FakeSafetyMonitor
    oc: FakeObservingConditions
    owners: dict[str, DeviceOwner]
    supervisor: SafetySupervisor
    sequencer: Sequencer
    lease_path: str = "local"
    server: SupervisorServer | None = None
    remote: RemoteSupervisor | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)
    #: Filter wheels (with ``filterwheels=True``), one per telescope.
    wheels: list[FakeFilterWheel] = field(default_factory=list)

    @property
    def tel(self) -> FakeTelescope:
        return self.tels[0]

    @property
    def cam(self) -> FakeCamera:
        return self.cams[0]

    async def start(self, sequencer: bool = True) -> None:
        self.tasks.append(asyncio.create_task(self.supervisor.run(), name="supervisor"))
        if self.lease_path == "remote":
            self.server = await SupervisorServer(self.supervisor).start()
            self.remote = RemoteSupervisor(self.server.url, **FAST_REMOTE)
            self.sequencer.supervisor = self.remote
        if sequencer:
            self.tasks.append(asyncio.create_task(self.sequencer.run(), name="sequencer"))

    async def stop(self) -> None:
        for fake in (self.dome, *self.tels, *self.cams, self.sm, self.oc):
            fake.unhang()
        for t in reversed(self.tasks):
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 10)
        if self.remote is not None:
            await self.remote.aclose()
        if self.server is not None:
            await self.server.stop()
        for o in self.owners.values():
            o.close()

    # Ground truth straight from the fakes (no owner, no timeline).
    def dome_closed(self) -> bool:
        return self.dome.state() == 1

    def dome_open(self) -> bool:
        return self.dome.state() == 0

    # With ``i``, telescope/camera i only; without, all of them.
    def parked(self, i: int | None = None) -> bool:
        tels = self.tels if i is None else [self.tels[i]]
        return all(t._parked and t._now() >= t._park_until for t in tels)

    def exposing(self, i: int | None = None) -> bool:
        cams = self.cams if i is None else [self.cams[i]]
        return all(c._state == 2 and c._now() < c._ends for c in cams)

    def camera_idle(self, i: int | None = None) -> bool:
        cams = self.cams if i is None else [self.cams[i]]
        return all(c._state == 0 for c in cams)


@pytest.fixture
def lease_path() -> str:
    return "local"


@pytest.fixture
async def make_rig(lease_path):
    rigs: list[Rig] = []

    def build(
        *,
        exptime: float = 5.0,
        count: int = 100,
        safety: dict | None = None,
        seq: dict | None = None,
        weather_safe: bool = True,
        command_timeout: float = 0.5,
        with_conditions: bool = True,
        heartbeat_timeout: float | None = None,
        telescopes: int = 1,
        plan=None,
        slew_guard=None,
        supervise_parking: bool = False,
        filterwheels: bool = False,
        port_action: str | None = None,
    ) -> Rig:
        tl = Timeline()
        weather = FakeWeather(safe=weather_safe)
        dome = FakeDome(heartbeat_timeout=heartbeat_timeout)
        tels = [FakeTelescope() for _ in range(telescopes)]
        cams = [FakeCamera() for _ in range(telescopes)]
        wheels = [FakeFilterWheel() for _ in range(telescopes)] if filterwheels else []
        for t in tels:
            t.port_action = port_action
        names = [f"t{i + 1}" for i in range(telescopes)]
        sm, oc = FakeSafetyMonitor(weather), FakeObservingConditions(weather)

        def owner(name, obj, **kw):
            return DeviceOwner(name, lambda: obj, tl, default_timeout=command_timeout, **kw)

        owners = {
            "dome": owner("dome", dome, privileged_members=ENCLOSURE_MEMBERS),
            **{
                f"{n}.telescope": owner(f"{n}.telescope", t)
                for n, t in zip(names, tels, strict=True)
            },
            **{f"{n}.camera": owner(f"{n}.camera", c) for n, c in zip(names, cams, strict=True)},
            **{
                f"{n}.filterwheel": owner(f"{n}.filterwheel", w)
                for n, w in zip(names, wheels, strict=False)
            },
            "safetymonitor": owner("safetymonitor", sm),
            "observingconditions": owner("observingconditions", oc),
        }
        scfg = SafetyConfig(**{**FAST_SAFETY, **(safety or {})})
        sup = SafetySupervisor(
            "safety",
            dome=owners["dome"],
            safety_monitor=owners["safetymonitor"],
            observing_conditions=owners["observingconditions"] if with_conditions else None,
            telescopes=[owners[f"{n}.telescope"] for n in names]
            if scfg.park_before_close or supervise_parking
            else (),
            heartbeat=owners["dome"] if scfg.heartbeat_interval is not None else None,
            timeline=tl,
            config=scfg,
        )
        k = iter(range(10**6))
        seqr = Sequencer(
            "seq",
            supervisor=sup,
            telescopes=[
                TelescopeUnit(
                    n,
                    owners[f"{n}.telescope"],
                    owners[f"{n}.camera"],
                    filterwheel=owners.get(f"{n}.filterwheel"),
                    port_action=port_action,
                )
                for n in names
            ],
            plan=plan or (lambda: Observation(f"target{next(k)}", exptime=exptime, count=count)),
            timeline=tl,
            config=SequencerConfig(**{**FAST_SEQ, **(seq or {})}),
            slew_guard=slew_guard,
        )
        rig = Rig(tl, weather, dome, tels, cams, sm, oc, owners, sup, seqr, lease_path)
        rig.wheels = wheels
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        await rig.stop()
