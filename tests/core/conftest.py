"""A rig of pure-Python fake devices behind real owners, supervisor and sequencer.

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
from robofast_core.supervisor_service import SupervisorServer
from robofast_core.testing.fakes import (
    FakeCamera,
    FakeDome,
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
    tel: FakeTelescope
    cam: FakeCamera
    sm: FakeSafetyMonitor
    oc: FakeObservingConditions
    owners: dict[str, DeviceOwner]
    supervisor: SafetySupervisor
    sequencer: Sequencer
    lease_path: str = "local"
    server: SupervisorServer | None = None
    remote: RemoteSupervisor | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)

    async def start(self, sequencer: bool = True) -> None:
        self.tasks.append(asyncio.create_task(self.supervisor.run(), name="supervisor"))
        if self.lease_path == "remote":
            self.server = await SupervisorServer(self.supervisor).start()
            self.remote = RemoteSupervisor(self.server.url, **FAST_REMOTE)
            self.sequencer.supervisor = self.remote
        if sequencer:
            self.tasks.append(asyncio.create_task(self.sequencer.run(), name="sequencer"))

    async def stop(self) -> None:
        for fake in (self.dome, self.tel, self.cam, self.sm, self.oc):
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

    def parked(self) -> bool:
        return self.tel._parked and self.tel._now() >= self.tel._park_until

    def exposing(self) -> bool:
        return self.cam._state == 2 and self.cam._now() < self.cam._ends

    def camera_idle(self) -> bool:
        return self.cam._state == 0


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
    ) -> Rig:
        tl = Timeline()
        weather = FakeWeather(safe=weather_safe)
        dome = FakeDome(heartbeat_timeout=heartbeat_timeout)
        tel, cam = FakeTelescope(), FakeCamera()
        sm, oc = FakeSafetyMonitor(weather), FakeObservingConditions(weather)

        def owner(name, obj, **kw):
            return DeviceOwner(name, lambda: obj, tl, default_timeout=command_timeout, **kw)

        owners = {
            "dome": owner("dome", dome, privileged_members=ENCLOSURE_MEMBERS),
            "telescope": owner("telescope", tel),
            "camera": owner("camera", cam),
            "safetymonitor": owner("safetymonitor", sm),
            "observingconditions": owner("observingconditions", oc),
        }
        scfg = SafetyConfig(**{**FAST_SAFETY, **(safety or {})})
        sup = SafetySupervisor(
            "safety",
            dome=owners["dome"],
            safety_monitor=owners["safetymonitor"],
            observing_conditions=owners["observingconditions"] if with_conditions else None,
            telescope=owners["telescope"] if scfg.park_before_close else None,
            heartbeat=owners["dome"] if scfg.heartbeat_interval is not None else None,
            timeline=tl,
            config=scfg,
        )
        n = iter(range(10**6))
        seqr = Sequencer(
            "seq",
            supervisor=sup,
            telescope=owners["telescope"],
            camera=owners["camera"],
            plan=lambda: Observation(f"target{next(n)}", exptime=exptime, count=count),
            timeline=tl,
            config=SequencerConfig(**{**FAST_SEQ, **(seq or {})}),
        )
        rig = Rig(tl, weather, dome, tel, cam, sm, oc, owners, sup, seqr, lease_path)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        await rig.stop()
