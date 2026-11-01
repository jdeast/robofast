"""Fixtures for the OmniSim integration tests (marked ``alpaca_sim``).

OmniSim (ASCOM Alpaca Simulators) provides the telescope, camera and dome;
our own :mod:`robofast_core.weather_server` provides the SafetyMonitor and
ObservingConditions, because OmniSim's SafetyMonitor cannot be made unsafe
through the Alpaca API.

Point the tests at a simulator in one of two ways:

* ``ROBOFAST_OMNISIM_ADDRESS=host:port`` -- an already running OmniSim;
* ``ROBOFAST_OMNISIM_BIN=/path/to/ascom.alpaca.simulators`` -- the tests
  start it on a free localhost port with a throw-away HOME, and stop it.

``scripts/fetch_omnisim.sh`` downloads and verifies the Linux build. If
neither variable is set the tests *fail* (they never silently skip), since a
green run that tested nothing is worse than a red one.

OmniSim serves one device of each type (v0.4.0 always configures device 0
only), so the two-telescope tests run a *second* OmniSim (``omnisim2``) for
the second telescope and camera: started from ``ROBOFAST_OMNISIM_BIN``, or
found at ``ROBOFAST_OMNISIM_ADDRESS2``. OmniSim refuses to start while a
process of its own name runs, so the second runs from a renamed copy of the
executable, next to links to the rest of the release.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from robofast_core import (
    Observation,
    SafetyConfig,
    SafetySupervisor,
    Sequencer,
    SequencerConfig,
    Timeline,
)
from robofast_core.alpaca import alpaca_owner
from robofast_core.lease_client import RemoteSupervisor
from robofast_core.safety import ENCLOSURE_MEMBERS
from robofast_core.supervisor_service import SupervisorServer
from robofast_core.testing.faultproxy import FaultProxy
from robofast_core.weather_server import WeatherServer, WeatherState

pytestmark = pytest.mark.alpaca_sim

SIM_SAFETY = dict(
    attended_only=False,  # no hardware heartbeat in most sim rigs; not about duty
    poll_interval=0.25,
    read_timeout=2.0,
    stale_after=3.0,
    reopen_hold=3.0,
    lease_ttl=10.0,
    shutter_timeout=30.0,
    dome_timeout=3.0,
)
SIM_SEQ = dict(
    poll_interval=0.2,
    slew_timeout=60.0,
    readout_margin=30.0,
    lease_renew_interval=2.0,
    cancel_ack_timeout=10.0,
    park_timeout=60.0,
    error_backoff=600.0,  # no retry within a test
)
COMMAND_TIMEOUT = 3.0
#: The stated bound for S1/S2/S4: from "weather went bad" to "exposure
#: aborted, telescope parked, shutter closed", on the simulator.
BOUND = 30.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get_json(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _ready(address: str) -> bool:
    try:
        devs = _get_json(f"http://{address}/management/v1/configureddevices")["Value"]
    except Exception:
        return False
    return {"Telescope", "Camera", "Dome"} <= {d["DeviceType"] for d in devs}


def _renamed(binary: str, workdir: Path, name: str) -> str:
    """``binary`` as ``workdir/name``, with links to the files beside it."""
    src = Path(binary)
    for f in src.parent.iterdir():
        if f != src:
            (workdir / f.name).symlink_to(f)
    shutil.copy2(src, workdir / name)
    return str(workdir / name)


def _omnisim(tmp_path_factory, address_var: str, log_var: str, rename: str | None = None):
    """Yield the address of an OmniSim: the one at ``$address_var``, or a
    fresh one started from ``$ROBOFAST_OMNISIM_BIN`` (as ``rename``, if given)."""
    address = os.environ.get(address_var)
    if address:
        if not _ready(address):
            pytest.fail(f"no OmniSim answering at {address_var}={address}")
        yield address
        return
    binary = os.environ.get("ROBOFAST_OMNISIM_BIN")
    if not binary:
        pytest.fail(
            f"alpaca_sim tests need ROBOFAST_OMNISIM_BIN or {address_var}"
            " (see scripts/fetch_omnisim.sh)"
        )
    home = tmp_path_factory.mktemp("omnisim-home")
    if rename:
        binary = _renamed(binary, tmp_path_factory.mktemp("omnisim-bin"), rename)
    port = _free_port()
    address = f"127.0.0.1:{port}"
    log_path = Path(os.environ.get(log_var) or home / "omnisim.log")
    log = open(log_path, "w")  # closed in teardown
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.Popen(
        [binary, f"--urls=http://{address}"],
        cwd=home,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60
        while not _ready(address):
            if proc.poll() is not None:
                pytest.fail(f"OmniSim exited with {proc.returncode}; see {log_path}")
            if time.monotonic() > deadline:
                pytest.fail(f"OmniSim not ready on {address} after 60 s; see {log_path}")
            time.sleep(0.5)
        yield address
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.fixture(scope="session")
def omnisim(tmp_path_factory):
    yield from _omnisim(tmp_path_factory, "ROBOFAST_OMNISIM_ADDRESS", "ROBOFAST_OMNISIM_LOG")


@pytest.fixture(scope="session")
def omnisim2(tmp_path_factory):
    """A second OmniSim, for a second telescope and camera."""
    yield from _omnisim(
        tmp_path_factory, "ROBOFAST_OMNISIM_ADDRESS2", "ROBOFAST_OMNISIM_LOG2", rename="omnisim2"
    )


class _Plain:
    """A minimal synchronous Alpaca client, independent of the one under test."""

    def __init__(self, address: str, device_type: str) -> None:
        self.http = httpx.Client(
            base_url=f"http://{address}/api/v1/{device_type}/0/", trust_env=False, timeout=10
        )
        self.put("Connected", Connected="True")

    def _check(self, r: httpx.Response):
        r.raise_for_status()
        body = r.json()
        if body["ErrorNumber"]:
            raise RuntimeError(f"{r.url}: {body['ErrorMessage']}")
        return body.get("Value")

    def get(self, member: str):
        return self._check(self.http.get(member.lower()))

    def put(self, member: str, **params: str):
        return self._check(self.http.put(member.lower(), data=params))


class Truth:
    """Ground truth read straight from OmniSim with a plain synchronous
    client, bypassing the owners (and therefore never in the command log)."""

    def __init__(self, address: str) -> None:
        self.tel, self.cam, self.dome = (
            _Plain(address, kind) for kind in ("telescope", "camera", "dome")
        )

    def shutter(self) -> int:
        return int(self.dome.get("ShutterStatus"))

    def closed(self) -> bool:
        return self.shutter() == 1

    def open(self) -> bool:
        return self.shutter() == 0

    def parked(self) -> bool:
        return bool(self.tel.get("AtPark"))

    def camera_state(self) -> int:
        return int(self.cam.get("CameraState"))

    def exposing(self) -> bool:
        return self.camera_state() == 2

    def safe_state(self) -> bool:
        return self.closed() and self.parked() and self.camera_state() == 0

    def reset(self) -> None:
        """Camera idle, telescope parked, shutter closed."""
        if self.camera_state() != 0:
            self.cam.put("AbortExposure")
        if self.tel.get("Slewing"):
            self.tel.put("AbortSlew")
        if not self.tel.get("AtPark"):
            self.tel.put("Park")
        if self.shutter() != 1:
            self.dome.put("CloseShutter")
        deadline = time.monotonic() + 60
        while not self.safe_state():
            if time.monotonic() > deadline:
                raise RuntimeError("OmniSim did not reach the reset state")
            time.sleep(0.2)


def _truth(address: str):
    t = Truth(address)
    t.reset()
    yield t
    for d in (t.tel, t.cam, t.dome):
        d.http.close()


@pytest.fixture
def truth(omnisim):
    yield from _truth(omnisim)


@pytest.fixture
def truth2(omnisim2):
    """Ground truth for the second OmniSim (only its telescope and camera are used)."""
    yield from _truth(omnisim2)


@pytest.fixture
def weather():
    state = WeatherState(safe=True, stale_after=None)
    server = WeatherServer(state, hang_limit=60).start()
    state.start_feed(0.2)
    yield state, server
    server.stop()


@pytest.fixture
def camera_proxy(omnisim):
    host, port = omnisim.split(":")
    proxy = FaultProxy(host, int(port)).start()
    yield proxy
    proxy.stop()


@dataclass
class SimRig:
    timeline: Timeline
    supervisor: SafetySupervisor
    sequencer: Sequencer
    owners: dict
    weather: WeatherState
    #: "local", or "remote": the sequencer leases through the supervisor's
    #: HTTP API (served in this process).
    lease_path: str = "local"
    server: SupervisorServer | None = None
    remote: RemoteSupervisor | None = None
    tasks: list = field(default_factory=list)

    async def start(self) -> None:
        import asyncio

        self.tasks.append(asyncio.create_task(self.supervisor.run()))
        if self.lease_path == "remote":
            self.server = await SupervisorServer(self.supervisor).start()
            self.remote = RemoteSupervisor(self.server.url, timeout=2.0, poll=1.0, retry=0.5)
            self.sequencer.supervisor = self.remote
        self.tasks.append(asyncio.create_task(self.sequencer.run()))

    async def stop(self) -> None:
        import asyncio

        for t in reversed(self.tasks):
            t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(t, 20)
        if self.remote is not None:
            await self.remote.aclose()
        if self.server is not None:
            await self.server.stop()
        for o in self.owners.values():
            o.close()


@pytest.fixture
async def make_sim_rig(omnisim, weather, truth, request):
    rigs: list[SimRig] = []
    state, server = weather

    def build(
        *,
        camera_address: str | None = None,
        exptime: float = 60.0,
        safety: dict | None = None,
        seq: dict | None = None,
        lease_path: str = "local",
    ) -> SimRig:
        tl = Timeline()

        def owner(name, kind, address, **kw):
            return alpaca_owner(name, kind, address, tl, default_timeout=COMMAND_TIMEOUT, **kw)

        owners = {
            "dome": owner("dome", "dome", omnisim, privileged_members=ENCLOSURE_MEMBERS),
            "telescope": owner("telescope", "telescope", omnisim),
            "camera": owner("camera", "camera", camera_address or omnisim),
            "safetymonitor": owner("safetymonitor", "safetymonitor", server.address),
            "observingconditions": owner(
                "observingconditions", "observingconditions", server.address
            ),
        }
        sup = SafetySupervisor(
            "safety",
            dome=owners["dome"],
            safety_monitor=owners["safetymonitor"],
            observing_conditions=owners["observingconditions"],
            timeline=tl,
            config=SafetyConfig(**{**SIM_SAFETY, **(safety or {})}),
        )
        n = iter(range(10**6))
        seqr = Sequencer(
            "seq",
            supervisor=sup,
            telescope=owners["telescope"],
            camera=owners["camera"],
            plan=lambda: Observation(f"field{next(n)}", exptime=exptime, count=1000),
            timeline=tl,
            config=SequencerConfig(**{**SIM_SEQ, **(seq or {})}),
        )
        rig = SimRig(tl, sup, seqr, owners, state, lease_path)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        await rig.stop()
        report = getattr(request.node, "rep_call", None)
        if report is not None and report.failed:
            print("\n--- timeline ---\n" + rig.timeline.dump())


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    if rep.when == "call":
        item.rep_call = rep
    return rep


_TIMINGS: dict[str, dict[str, float]] = {}


@pytest.fixture
def record_timing(request):
    def record(**values: float) -> None:
        _TIMINGS.setdefault(request.node.name, {}).update(
            {k: round(v, 2) for k, v in values.items()}
        )
        print(f"{request.node.name}: {values}")

    return record


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("ROBOFAST_TIMINGS_FILE")
    if path and _TIMINGS:
        Path(path).write_text(json.dumps(_TIMINGS, indent=2))
