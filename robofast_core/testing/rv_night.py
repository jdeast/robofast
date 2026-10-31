"""A whole simulated MINERVA RV night, end to end, compressed in time.

Every piece is the real robofast code, against simulators on loopback
(docs/demo.md):

* the **weather bridge** (``python -m robofast_core.weather_bridge``, the
  shipped ``sites/minerva/weather.yaml`` pointed at a fake site Redis and
  fake SAO pages, :mod:`robofast_core.testing.redis_feed`,
  :mod:`robofast_core.testing.weather_feed`), as a child process;
* the **Aqawan**: the PAC simulator and the Alpaca Dome server
  (:mod:`robofast_core.devices.aqawan_sim`, ``aqawan_server``);
* the **safety supervisor** (``python -m robofast_core.supervisor_service``)
  as a child process, with ``sites/minerva/aqawan1.json``'s safety settings
  (park before close, the dome's heartbeat), timings shortened;
* **T1 and T2**: fake mounts served over Alpaca (the supervisor and the
  sequencer both reach them over HTTP), and simulated star-field cameras on
  simulated focusers (for autofocus and imaging);
* a **guider service** per telescope (``python -m robofast_core.guider.service``,
  simulated FAU camera, tip/tilt and backlight), as child processes;
* **KiwiSpec**: the spectrograph PC simulator and the KiwiSpec service
  (``python -m robofast_core.devices.kiwispec_service``, a child process);
* the **sequencer service** (:func:`robofast_core.sequencer_service.build`
  and :func:`~robofast_core.sequencer_service.run`) on
  ``sites/minerva/aqawan1.json`` with the dispatch plan of
  ``sites/minerva/dispatch.example.json`` -- three times, as
  ``deploy/robofast-sequencer@.service`` runs it: evening calibrations
  (``calibration`` plan), the night, morning calibrations.

One simulated clock (:class:`robofast_core.clock.FileClock` on
``clock.json`` in ``out``) is the time of every night name and time stamp:
the scheduler, the sequencer (FITS times and frame directories, spectrum
sidecars, autofocus results, the plan record) and the KiwiSpec service (raw
file names and exposure start times). It runs at real speed during the
evening calibrations (starting half an hour before ``start``), ``scale``
times faster than real time during the night (from ``start``, early evening
twilight, so the night's Sun, targets and dawn pass in a few minutes), and
at real speed again for the morning calibrations. Exposures, holds and
timeouts run in real (short) seconds. The whole night is therefore one
night, ``n20261109``, everywhere.

A *director* plays the night's events: bad weather in the middle of a
spectrum (and good weather again once the roof is shut), a target of
opportunity dropped into the scheduler's drop box, and a mount fault on T2
in the middle of a spectrum (cleared a few seconds later). :func:`check`
then verifies the timeline and the outputs (the e2e test,
``tests/e2e/test_rv_night.py``, and the demo, ``python -m
robofast_core.testing.rv_night``, both use it).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from robofast_core import sky
from robofast_core.autofocus import AutofocusSetup, FocusModel, FocusStore
from robofast_core.autofocus_plan import AutofocusTriggers, TriggerConfig
from robofast_core.clock import FileClock, ScaledClock
from robofast_core.console.hold import send_command
from robofast_core.devices.aqawan_server import AqawanConfig, AqawanServer
from robofast_core.devices.aqawan_sim import AqawanSim
from robofast_core.devices.kiwispec_sim import KiwiSpecSim, SimConfig
from robofast_core.dispatch import dispatch_from_config
from robofast_core.owner import DeviceOwner
from robofast_core.plan_record import read_record
from robofast_core.sequencer import Sequencer
from robofast_core.sequencer_service import build, run
from robofast_core.sun import now_utc
from robofast_core.testing.alpaca_server import FakeAlpacaServer
from robofast_core.testing.fakes import FakeTelescope
from robofast_core.testing.focus_sim import FocusScene, SimFocuser, StarfieldCamera
from robofast_core.testing.processes import ROOT, Service, SupervisorProcess
from robofast_core.testing.redis_feed import RedisFeed, RedisFeedState
from robofast_core.testing.starfield import Detector, SimOptics, random_stars
from robofast_core.testing.weather_feed import FeedServer, FeedState
from robofast_core.timeline import Timeline

log = logging.getLogger(__name__)

SITES = ROOT / "sites" / "minerva"
MINERVA = sky.Site(31.680407, -110.878977, 2316.0)
#: The night of 2026-11-09 (new Moon): the Sun is at -8 deg at 01:05 UTC,
#: -12 deg at 01:25 UTC and again at 12:55 UTC.
DEFAULT_START = datetime(2026, 11, 10, 1, 5, tzinfo=UTC)
NAMES = ("t1", "t2")
SEQUENCER = "aqawan1-sequencer"
#: The simulated imagers (and their focus): 512x512, 1"/px, f/6.5.
PIXEL_SCALE, PIXEL_SIZE, F_RATIO, OBSTRUCTION = 1.0, 13.5, 6.5, 0.47
TRUE_FOCUS = 20000.0


@dataclass
class NightSettings:
    """What the night is made of (all times real seconds unless said)."""

    #: Simulated seconds per real second for the scheduler's clock. 200, not
    #: faster: exposures, slews and readouts take real seconds, and on a
    #: slow CI runner at 400 the night ran out before every target was done.
    scale: float = 200.0
    start: datetime = DEFAULT_START
    #: RV spectra: maximum exposure time; the exposure meter's count rate
    #: (counts/s) and one target that ends on counts.
    rv_exptime: float = 2.0
    expmeter_rate: float = 2.0e5
    #: The supervisor's reopen hold after bad weather, and how long the bad
    #: weather lasts after the roof is shut.
    reopen_hold: float = 4.0
    bad_weather: float = 2.0
    #: Bad weather arrives during this spectrum (counted from the night's start).
    weather_during_spectrum: int = 2
    #: How long T2's mount stays unresponsive, and then paused by the console.
    fault_duration: float = 3.0
    pause: float = 3.0
    #: The observer console (docs/console.md) on this loopback port during
    #: the night (0: any free port); None: no console.
    console_port: int | None = 0
    #: Bound on the whole night (real seconds), as a safety net.
    timeout: float = 600.0


def _recording_telescope(name: str, events: list[tuple[float, str, str]]) -> Any:
    """A fake mount that logs every park, unpark, slew and fault with its
    ``time.monotonic()`` (ground truth for the safety checks)."""

    class _Tel(FakeTelescope):
        def Park(self) -> None:
            super().Park()
            events.append((time.monotonic(), name, "park"))

        def Unpark(self) -> None:
            super().Unpark()
            events.append((time.monotonic(), name, "unpark"))

        def SlewToCoordinatesAsync(self, ra: float, dec: float) -> None:
            super().SlewToCoordinatesAsync(ra, dec)
            events.append((time.monotonic(), name, "slew"))

        def fail(self, msg: str = "device unreachable") -> None:
            super().fail(msg)
            events.append((time.monotonic(), name, "fault"))

        def unfail(self) -> None:
            super().unfail()
            events.append((time.monotonic(), name, "fault cleared"))

    return _Tel(slew_time=0.3, park_time=0.3)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _scene(seed: int) -> FocusScene:
    rng = np.random.default_rng(seed)
    detector = Detector(shape=(512, 512))
    stars = random_stars(
        6, detector.shape, flux=(1.5e5, 6e5), margin=60, min_separation=110, rng=rng
    )
    optics = SimOptics(
        pixel_scale=PIXEL_SCALE, pixel_size=PIXEL_SIZE, f_ratio=F_RATIO, obstruction=OBSTRUCTION
    )
    return FocusScene(
        stars, optics=optics, detector=detector, true_focus=TRUE_FOCUS, seeing=2.0, seed=seed
    )


AUTOFOCUS = {
    "optics": {
        "f_ratio": F_RATIO,
        "pixel_size": PIXEL_SIZE,
        "pixel_scale": PIXEL_SCALE,
        "obstruction": OBSTRUCTION,
    },
    "method": "sweep",
    "exptime": 0.01,
    "poll_interval": 0.005,
    "max_start_error": 400,
    "readout_margin": 5.0,
    "move_timeout": 3.0,
}


@dataclass
class Telescope:
    """One simulated telescope's devices (shared by every sequencer run)."""

    name: str
    mount: Any
    camera: StarfieldCamera
    focuser: SimFocuser
    guider: Service | None = None
    guider_url: str = ""


@dataclass
class Outcome:
    """What the night produced, for :func:`check` and the demo's summary."""

    out: Path
    settings: NightSettings
    runs: dict[str, Sequencer] = field(default_factory=dict)
    timelines: dict[str, Timeline] = field(default_factory=dict)
    #: (time.monotonic(), telescope, what): parks, unparks, slews, faults.
    mount_events: list[tuple[float, str, str]] = field(default_factory=list)
    #: The Aqawan simulator's command log: (time.monotonic(), command).
    dome_log: list[tuple[float, str]] = field(default_factory=list)
    #: (time.monotonic(), "unsafe" | "safe") as the director set the weather.
    weather: list[tuple[float, str]] = field(default_factory=list)
    #: The director's notes: (time.monotonic(), what).
    script: list[tuple[float, str]] = field(default_factory=list)
    #: The spectrograph simulator's files: name -> {"dark", "exptime", "header", ...}.
    spectrograph_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Request statuses at dawn, by configuration id.
    statuses: dict[str, str] = field(default_factory=dict)
    plan_record: list[dict[str, Any]] = field(default_factory=list)
    t_start: float = 0.0
    t_end: float = 0.0
    reopen_hold: float = 0.0
    errors: list[str] = field(default_factory=list)
    #: What the observer console answered during the night (endpoint -> facts).
    console: dict[str, Any] = field(default_factory=dict)

    @property
    def runtime(self) -> float:
        return self.t_end - self.t_start


class SimulatedNight:
    """The services and the night (module doc). ``out`` receives every
    config, log, data file and record."""

    def __init__(self, out: Path, settings: NightSettings | None = None) -> None:
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.s = settings or NightSettings()
        self.outcome = Outcome(self.out, self.s, reopen_hold=self.s.reopen_hold)
        self.services: list[Service] = []
        self.telescopes: dict[str, Telescope] = {}
        self._stack = contextlib.AsyncExitStack()
        #: The one clock (module doc); its phases are written to clock.json.
        self.clock_file = self.out / "clock.json"
        self.set_clock(self.s.start - timedelta(minutes=30), 1.0)
        self.clock = FileClock(self.clock_file)

    def set_clock(self, start: datetime, scale: float) -> None:
        """Move every process's clock (module doc)."""
        ScaledClock(start, scale).write(self.clock_file)

    # -- the services ----------------------------------------------------------------

    async def start(self) -> None:
        self.outcome.t_start = time.monotonic()
        await self._stack.__aenter__()
        self._start_weather()
        self._start_aqawan()
        self._start_telescopes()
        await self._start_spectrograph()
        self._start_guiders()
        self._start_supervisor()
        await self._wait_ready()

    def _start_weather(self) -> None:
        import yaml

        self.redis = RedisFeed(RedisFeedState(clock=now_utc)).start()
        self.pages = FeedServer(FeedState(user="user", password="secret")).start()
        self._stack.callback(self.redis.stop)
        self._stack.callback(self.pages.stop)
        creds = self.out / "weather-credentials"
        creds.write_text("user\nsecret\n")
        raw = yaml.safe_load((SITES / "weather.yaml").read_text())
        raw["sources"]["redis"]["url"] = self.redis.url + "/7"
        raw["sources"]["ridge"]["url"] = self.pages.base_url + "/weather/weather.cur_cond"
        raw["sources"]["cloud"]["url"] = self.pages.base_url + "/temps/sky_temps_now"
        raw["sources"]["cloud"]["credentials_file"] = str(creds)
        for name in ("redis", "ridge", "cloud"):
            raw["sources"][name]["timeout"] = 1.0
        # The scheduler's clock is simulated; the bridge's is real. Its Sun
        # limit would follow the real Sun (CI runs at any time of day), so it
        # is opened up here: the scheduler's twilight limit (max_sun_alt)
        # keeps the night dark.
        raw["sources"]["sun"]["limits"]["altitude"] = {"open": [-90, 90]}
        self.bridge_address = f"127.0.0.1:{_free_port()}"
        raw.update(
            listen=self.bridge_address,
            poll_interval=0.2,
            evaluate_interval=0.1,
            server_stale_after=3.0,
            rain_hold=5.0,
        )
        path = self.out / "weather_bridge.json"
        path.write_text(json.dumps(raw, indent=1, default=str))
        self.bridge = Service(
            "robofast_core.weather_bridge",
            "--config",
            str(path),
            log_path=self.out / "weather_bridge.log",
            stdout_path=self.out / "weather_bridge.out",
        )
        self.services.append(self.bridge)

    def set_weather(self, safe: bool) -> None:
        db = dict(self.redis.feed.dbs[7])
        db["humidity"] = "30.0" if safe else "95.0"
        self.redis.feed.dbs[7] = db
        self.pages.feed.ridge["outsideHumidity"] = 30.0 if safe else 95.0
        self.outcome.weather.append((time.monotonic(), "safe" if safe else "unsafe"))
        self._note(f"weather {'safe' if safe else 'UNSAFE (humidity 95 %)'}")

    def _start_aqawan(self) -> None:
        self.aqawan_sim = AqawanSim(
            open_time=0.6, close_time=0.6, settle=0.05, heartbeat_timeout=15
        )
        self.aqawan_sim.start()
        self._stack.callback(self.aqawan_sim.stop)
        base = json.loads((ROOT / "deploy" / "aqawan.example.json").read_text())
        base.update(
            listen="127.0.0.1:0",
            pac={
                "host": "127.0.0.1",
                "port": self.aqawan_sim.port,
                "connect_timeout": 1.0,
                "command_timeout": 2.0,
                "resend_after": 0.15,
                "idle_end": 0.1,
            },
            status_max_age=0.1,
            poll_interval=0.05,
            start_grace=1.0,
            shutter_open_timeout=5.0,
            request_timeout=3.0,
        )
        self.aqawan = AqawanServer(AqawanConfig.from_dict(base)).start()
        self._stack.callback(self.aqawan.stop)

    def _start_telescopes(self) -> None:
        devices: dict[str, Any] = {}
        for i, name in enumerate(NAMES):
            mount = _recording_telescope(name, self.outcome.mount_events)
            foc = SimFocuser(position=round(TRUE_FOCUS + 250 * (1 if i == 0 else -1)))
            cam = StarfieldCamera(foc, _scene(31 + i))
            cam.clock = self.clock
            self.telescopes[name] = Telescope(name, mount, cam, foc)
            devices[f"telescope/{i}"] = mount
            devices[f"camera/{i}"] = cam
        self.alpaca = FakeAlpacaServer(devices, hang_limit=10).start()
        self._stack.callback(self.alpaca.stop)

    async def _start_spectrograph(self) -> None:
        self.kiwi_sim = await KiwiSpecSim(
            SimConfig(
                readout=0.3,
                stage_speed=400.0,
                stage_home_time=0.05,
                stage_power_cycle=0.1,
                expmeter_period=0.1,
                heater_tau=0.2,
                backlight_failsafe=1.0,
            )
        ).start()
        self.kiwi_sim.set_sky(self.s.expmeter_rate)
        self._stack.push_async_callback(self.kiwi_sim.stop)
        base = json.loads((SITES / "kiwispec.example.json").read_text())
        base = {k: v for k, v in base.items() if not k.startswith("_")}
        base.update(
            listen="127.0.0.1:0",
            spectrograph={
                "host": "127.0.0.1",
                "port": self.kiwi_sim.spec_port,
                "connect_timeout": 1.0,
                "send_timeout": 1.0,
                "max_reply_timeout": 2.0,
            },
            camera={
                "host": "127.0.0.1",
                "port": self.kiwi_sim.camera_port,
                "connect_timeout": 1.0,
                "command_timeout": 2.0,
            },
            telescopes=["T1", "T2"],
            stage_poll=0.05,
            stage_timeout=3.0,
            heater_wait=0.5,
            readout_time=0.3,
            exposure_margin=2.0,
            expmeter_poll=0.1,
            expmeter_period=0.1,
            abort_timeout=3.0,
            save_timeout=3.0,
            save_retry=0.2,
            status_interval=1.0,
            status_timeout=3.0,
            clock={"file": str(self.clock_file)},
        )
        path = self.out / "kiwispec.json"
        path.write_text(json.dumps(base, indent=1))
        self.kiwispec = Service(
            "robofast_core.devices.kiwispec_service",
            "--config",
            str(path),
            log_path=self.out / "kiwispec.log",
        )
        self.services.append(self.kiwispec)

    def _start_guiders(self) -> None:
        for name in NAMES:
            cfg = {
                "name": f"{name}-guider",
                "scope": name,
                "listen": "127.0.0.1:0",
                "port": "fau",
                "calibration_dir": str(self.out / "guider-calibrations" / name),
                "camera": {"kind": "sim"},
                "tiptilt": {"kind": "sim", "limits": [[-200, 200], [-200, 200]]},
                "backlight": {"sim": True},
                "guide": {
                    "exposure": 0.05,
                    "acq_exposure": 0.05,
                    "acq_roi": [200, 160, 240, 160],
                    "plate_scale": 0.5,
                    "centroider": {"kind": "annulus"},
                },
                "sim": {
                    "star": [323.0, 241.0],
                    "fiber": {"x": 321.5, "y": 239.5, "radius": 3.0},
                    "jitter": {"rms": 0.1, "knee": 1.0, "seed": 5 + len(name)},
                    "tt_matrix": [[0.08, 0.03], [-0.03, 0.08]],
                },
                "calibrations": {
                    "tiptilt": {"matrix": [[0.08, 0.03], [-0.03, 0.08]], "units": "steps"}
                },
            }
            path = self.out / f"guider_{name}.json"
            path.write_text(json.dumps(cfg, indent=1))
            svc = Service(
                "robofast_core.guider.service",
                "--config",
                str(path),
                log_path=self.out / f"guider_{name}.log",
            )
            self.services.append(svc)
            self.telescopes[name].guider = svc

    def _enclosure(self) -> dict[str, Any]:
        """``sites/minerva/aqawan1.json`` with simulator addresses and
        shortened timings."""
        cfg = json.loads((SITES / "aqawan1.json").read_text())
        cfg.pop("_note", None)
        a = self.alpaca.address
        cfg["devices"] = {
            "dome": self.aqawan.address,
            "safetymonitor": self.bridge_address,
            "observingconditions": self.bridge_address,
        }
        cfg["telescopes"] = {
            name: {"telescope": f"{a}/{i}", "camera": f"{a}/{i}"} for i, name in enumerate(NAMES)
        }
        cfg["safety"].update(
            poll_interval=0.2,
            read_timeout=1.0,
            stale_after=3.0,
            reopen_hold=self.s.reopen_hold,
            lease_ttl=3.0,
            shutter_timeout=5.0,
            dome_timeout=4.0,
            park_wait=10.0,
            heartbeat_interval=1.0,
            heartbeat_timeout=10.0,
        )
        return cfg

    def _start_supervisor(self) -> None:
        cfg = self._enclosure()
        cfg["listen"] = "127.0.0.1:0"
        self.supervisor = SupervisorProcess(cfg, self.out, "supervisor")
        self.services.append(self.supervisor)

    async def _wait_ready(self) -> None:
        self.kiwispec_url = json.loads(self.kiwispec.first_line())["url"]
        for t in self.telescopes.values():
            assert t.guider is not None
            t.guider_url = json.loads(t.guider.first_line())["url"]
        deadline = time.monotonic() + 30
        async with httpx.AsyncClient(trust_env=False, timeout=2) as c:
            while True:
                try:
                    r = await c.get(f"http://{self.bridge_address}/api/v1/safetymonitor/0/issafe")
                    if r.status_code == 200 and r.json().get("Value") is True:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(f"weather bridge not safe; see {self.bridge.log_path}")
                await asyncio.sleep(0.2)
        self.outcome.weather.append((time.monotonic(), "safe"))

    async def close(self) -> None:
        for svc in reversed(self.services):
            with contextlib.suppress(Exception):
                svc.stop()
        await self._stack.aclose()

    # -- the sequencer runs ----------------------------------------------------------

    def _sequencer_config(self, plan: dict[str, Any]) -> dict[str, Any]:
        cfg = self._enclosure()
        cfg["supervisor_url"] = self.supervisor.url
        cfg["lease_client"] = {"timeout": 1.0, "poll": 0.3, "retry": 0.2}
        cfg["command_timeout"] = 3.0
        cfg["sequencer"] = {
            "poll_interval": 0.05,
            "slew_timeout": 10.0,
            "readout_margin": 5.0,
            "lease_renew_interval": 0.5,
            "cancel_ack_timeout": 5.0,
            "park_timeout": 10.0,
            "error_backoff": 2.0,
            "max_consecutive_errors": 3,
            "recover_poll": 0.2,
            "idle_release_after": 6.0,
            "plan_timeout": 15.0,
            "spectrum_ready_timeout": 15.0,
            "spectrum_check_interval": 0.2,
            "spectrum_poll": 0.2,
            "spectrum_margin": 15.0,
            "locate_timeout": 10.0,
            "spectrograph_abort_wait": 5.0,
        }
        cfg["images"].update(data_root=str(self.out / "data"), queue_size=4, imagebytes=False)
        cfg["spectrograph"] = {
            "url": self.kiwispec_url,
            "telescopes": {"t1": "T1", "t2": "T2"},
            "timeout": 5.0,
            "abort_timeout": 8.0,
        }
        cfg["status_file"] = str(self.out / "sequencer-status.json")
        cfg["status_interval"] = 1.0
        cfg["clock"] = {"file": str(self.clock_file)}
        for name, t in self.telescopes.items():
            cfg["telescopes"][name]["guider"] = {
                "url": t.guider_url,
                "acquire_timeout": 10.0,
                "timeout": 3.0,
                "poll_interval": 0.5,
            }
        cfg["plan"] = plan
        return cfg

    def _build(self, tag: str, plan_entry: dict[str, Any], plan: Any = None) -> Sequencer:
        cfg = self._sequencer_config(plan_entry)
        (self.out / f"sequencer-{tag}.json").write_text(json.dumps(cfg, indent=1, default=str))
        tl = Timeline(maxlen=200000)
        seq, remote = build(cfg, tl, plan=plan, base=self.out)
        seq._remote = remote  # type: ignore[attr-defined]
        for name, unit in seq.units.items():
            t = self.telescopes[name]
            unit.camera = DeviceOwner(f"{name}.camera", lambda c=t.camera: c, tl, default_timeout=5)
            unit.focuser = DeviceOwner(
                f"{name}.focuser", lambda f=t.focuser: f, tl, default_timeout=3
            )
            unit.autofocus = AutofocusSetup(dict(AUTOFOCUS), store=FocusStore(), model=FocusModel())
        self.outcome.runs[tag] = seq
        self.outcome.timelines[tag] = tl
        return seq

    async def _run(self, seq: Sequencer, timeout: float) -> None:
        handler = logging.FileHandler(self.out / f"sequencer-{_tag(self, seq)}.log")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.setLevel(logging.INFO)
        root = logging.getLogger("robofast_core")
        level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        try:
            await asyncio.wait_for(
                run(
                    seq,
                    seq._remote,  # type: ignore[attr-defined]
                    status_file=self.out / "sequencer-status.json",
                    status_interval=1.0,
                ),
                timeout,
            )
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
            handler.close()
            for sig in ("SIGTERM", "SIGINT"):
                with contextlib.suppress(Exception):
                    import signal

                    asyncio.get_running_loop().remove_signal_handler(getattr(signal, sig))

    def _calibration_plan(self) -> dict[str, Any]:
        entry = json.loads((SITES / "calibration.example.json").read_text())
        entry.pop("_note", None)
        entry.update(
            biases=1,
            darks=[[0.2, 1]],
            spectrograph=[
                {"kind": "bias", "exptime": 0, "n": 2},
                {"kind": "dark", "exptime": 0.5, "n": 1},
                {"kind": "thar", "exptime": 0.3, "n": 2},
                {"kind": "fiberflat", "exptime": 0.3, "n": 1},
            ],
        )
        return entry

    def _requests(self) -> list[dict[str, Any]]:
        """Six RV standards (SPECTRUM, both telescopes), one imaging field
        for T1 in a window in the middle of the night."""
        start = self.s.start
        lst0 = sky.sky_now(MINERVA, start).lst_hours
        groups = []
        # Targets transit 1-8 h after the start, at dec +10..+35 (MINERVA's
        # RV stars are bright and northern-ish); one ends on counts.
        for i, (dh, dec) in enumerate(
            [(1.0, 20), (2.0, 30), (3.5, 15), (5.0, 35), (6.5, 25), (8.0, 10)]
        ):
            gid = f"RV{i + 1}"
            ic: dict[str, Any] = {"exptime": self.s.rv_exptime, "count": 1, "iodine": True}
            if i == 2:
                ic["target_counts"] = self.s.expmeter_rate * self.s.rv_exptime * 0.6
            groups.append(
                {
                    "id": gid,
                    "name": f"RV standard {i + 1}",
                    "proposal": "rv-demo",
                    "priority": 5 - 0.5 * i,
                    "requests": [
                        {
                            "id": f"{gid}-r",
                            "telescopes": list(NAMES),
                            "configurations": [
                                {
                                    "id": f"{gid}-spec",
                                    "type": "SPECTRUM",
                                    "target": {
                                        "name": f"RV{i + 1}",
                                        "ra_hours": (lst0 + dh) % 24.0,
                                        "dec_deg": float(dec),
                                        "pm_ra": 100.0 * (i + 1),
                                        "pm_dec": -50.0 * (i + 1),
                                        "epoch": 2000.0,
                                    },
                                    "instrument_configs": [ic],
                                }
                            ],
                        }
                    ],
                }
            )
        w0 = start + timedelta(hours=4)
        groups.append(
            {
                "id": "FIELD",
                "name": "Imaging field",
                "proposal": "imaging-demo",
                "priority": 20,
                "requests": [
                    {
                        "id": "FIELD-r",
                        "telescopes": ["t1"],
                        "windows": [
                            {"start": w0.isoformat(), "end": (w0 + timedelta(hours=2)).isoformat()}
                        ],
                        "configurations": [
                            {
                                "id": "FIELD-img",
                                "instrument": "imager",
                                "target": {
                                    "name": "FIELD",
                                    "ra_hours": (lst0 + 4.5) % 24.0,
                                    "dec_deg": 25.0,
                                },
                                "instrument_configs": [{"exptime": 0.3, "count": 2}],
                            }
                        ],
                    }
                ],
            }
        )
        return groups

    def _too(self) -> dict[str, Any]:
        now = self.clock()
        lst = sky.sky_now(MINERVA, now).lst_hours
        return {
            "id": "TOO1",
            "name": "Target of opportunity",
            "proposal": "too-demo",
            "priority": 10,
            "too": {
                "deadline": (now + timedelta(hours=3)).isoformat(),
                "policy": "next",
                "priority_class": 1,
            },
            "requests": [
                {
                    "id": "TOO1-r",
                    "telescopes": list(NAMES),
                    "configurations": [
                        {
                            "id": "TOO1-spec",
                            "type": "SPECTRUM",
                            "target": {
                                "name": "TOO1",
                                "ra_hours": (lst + 0.5) % 24.0,
                                "dec_deg": 28.0,
                            },
                            "instrument_configs": [{"exptime": self.s.rv_exptime, "count": 1}],
                        }
                    ],
                }
            ],
        }

    def _dispatch_entry(self) -> dict[str, Any]:
        entry = json.loads((SITES / "dispatch.example.json").read_text())
        entry.pop("_note", None)
        entry.update(
            requests=self._requests(),
            state=str(self.out / "dispatch-state.json"),
            too_dropbox=str(self.out / "too-dropbox"),
            record={"dir": str(self.out / "plans"), "timezone": "America/Phoenix"},
            idle_poll=1.0,
            too_poll=0.3,
            reserve_timeout=5.0,
        )
        for lim in entry["telescopes"].values():
            lim.pop("filters", None)  # the simulated imagers have no filter wheel
        return entry

    async def run(self) -> Outcome:
        """Evening calibrations, the night, morning calibrations."""
        s = self.s
        # 1. Evening calibrations, the roof closed (no lease).
        self._note("evening calibrations")
        cal = self._build("evening", self._calibration_plan())
        await self._run(cal, 120)
        # 2. The night.
        self._note("night")
        entry = self._dispatch_entry()
        self.set_clock(s.start, s.scale)
        dispatch = dispatch_from_config(
            {k: v for k, v in entry.items() if k != "type"},
            list(NAMES),
            base=self.out,
            clock=self.clock,
        )
        plan = AutofocusTriggers(
            dispatch,
            TriggerConfig(start_of_night=True, temperature_change=None, fwhm_degradation=None),
            telescopes=list(NAMES),
        )
        seq = self._build("night", entry, plan=plan)
        plan.bind(seq.registry)
        if s.console_port is not None:
            await self._start_console()
        self.dispatch = dispatch
        director = asyncio.create_task(self._direct(seq), name="director")
        try:
            await self._run(seq, s.timeout)
        finally:
            director.cancel()
            with contextlib.suppress(BaseException):
                await director
        self.outcome.statuses = {
            c.id: str(c.status) for g in dispatch.groups.values() for c in g.configurations()
        }
        # 3. Morning calibrations, once the roof is shut.
        await self._until(lambda: self.aqawan_sim.closed(), 30, "roof closed at dawn")
        self.set_clock(self.clock(), 1.0)
        self._note("morning calibrations")
        cal = self._build("morning", self._calibration_plan())
        await self._run(cal, 120)
        self.outcome.t_end = time.monotonic()
        self.outcome.dome_log = list(self.aqawan_sim.log)
        self.outcome.spectrograph_files = {
            k: {kk: vv for kk, vv in v.items() if kk != "data"}
            for k, v in self.kiwi_sim.files.items()
        }
        for f in sorted((self.out / "plans").glob("*.jsonl")):
            self.outcome.plan_record += read_record(f)
        return self.outcome

    # -- the observer console --------------------------------------------------------

    async def _start_console(self) -> None:
        """The observer console on the night's services, for people to watch
        (its URL and where its operator token is are printed) and for the
        checks."""
        from robofast_core.api_auth import TokenStore
        from robofast_core.console.server import (
            Console,
            ConsoleConfig,
            ConsoleHttpServer,
            build_enclosures,
        )

        store = TokenStore(self.out / "console-tokens.json")
        token = store.add("demo", ["read", "operator"])[1]
        tfile = self.out / "console-token.txt"
        tfile.write_text(token + "\n")
        os.chmod(tfile, 0o600)
        cfg = ConsoleConfig(
            listen=f"127.0.0.1:{self.s.console_port}",
            tokens=str(self.out / "console-tokens.json"),
            audit_log=str(self.out / "console-audit.jsonl"),
            acks=str(self.out / "console-acks.json"),
            cache=0.5,
            refresh=1.0,
            sky_cache=2.0,
        )
        spec = {"aqawan1": {"config": "sequencer-night.json", "spectrograph": self.kiwispec_url}}
        encs = build_enclosures(spec, self.out)
        self.console = Console(encs, store, config=cfg, clock=self.clock)
        self.console_server = await ConsoleHttpServer(
            self.console, port=int(self.s.console_port or 0)
        ).start()
        self._stack.push_async_callback(self.console.aclose)
        self._stack.push_async_callback(self.console_server.stop)
        self.console_token = token
        self._note(f"observer console: {self.console_server.url} (token in {tfile})")

    async def _probe_console(self) -> None:
        """Read the console as an observer would (outcome.console)."""
        c = self.outcome.console
        server = getattr(self, "console_server", None)
        if server is None:
            return
        async with httpx.AsyncClient(base_url=server.url, trust_env=False, timeout=20) as http:
            auth = {"Authorization": f"Token {self.console_token}"}
            r = await http.get("/console/api/status", headers=auth)
            st = r.json()["enclosures"]["aqawan1"] if r.status_code == 200 else {}
            c["status"] = {
                "code": r.status_code,
                "shutter": (st.get("enclosure") or {}).get("shutter"),
                "telescopes": sorted(st.get("telescopes") or {}),
            }
            r = await http.get("/console/api/sky", headers=auth)
            sk = r.json()["enclosures"]["aqawan1"] if r.status_code == 200 else {}
            c["sky"] = {
                "code": r.status_code,
                "targets": [t["name"] for t in sk.get("targets", [])],
            }
            r = await http.get("/console/api/log?after=0", headers=auth)
            entries = r.json().get("entries", []) if r.status_code == 200 else []
            c["log"] = {"code": r.status_code, "sources": sorted({e["source"] for e in entries})}
            r = await http.get("/console/api/image/aqawan1/guider-t1?max=128", headers=auth)
            c["guider_image"] = {"code": r.status_code, "bytes": len(r.content)}

    # -- the director ---------------------------------------------------------------

    def _note(self, what: str) -> None:
        self.outcome.script.append((time.monotonic(), what))
        log.warning("night: %s", what)
        print(f"[{time.monotonic() - self.outcome.t_start:7.1f} s] {what}", flush=True)

    async def _until(self, pred: Any, timeout: float, what: str) -> None:
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > deadline:
                raise TimeoutError(f"director: timed out waiting for {what}")
            await asyncio.sleep(0.05)

    async def _direct(self, seq: Sequencer) -> None:
        """Play the night's events (module doc)."""
        s = self.s
        tl = seq.timeline
        try:
            # Bad weather during a spectrum.
            await self._until(
                lambda: len(tl.events("spectrum_started")) >= s.weather_during_spectrum,
                s.timeout,
                "spectra",
            )
            await asyncio.sleep(0.3)
            self._note(f"bad weather during spectrum {s.weather_during_spectrum}")
            self.set_weather(False)
            await self._until(lambda: tl.events("lease_released"), 60, "lease released")
            await self._until(self.aqawan_sim.closed, 60, "roof closed")
            self._note("roof closed")
            await asyncio.sleep(s.bad_weather)
            self.set_weather(True)
            n_open = len(tl.events("observe_start"))
            await self._until(lambda: len(tl.events("observe_start")) > n_open, 60, "reopen")
            self._note("reopened")
            try:
                await self._probe_console()
                self._note(f"console: {self.outcome.console}")
            except Exception as exc:
                self.outcome.errors.append(f"console probe: {exc!r}")
            # A target of opportunity, once a spectrum has been taken again.
            n_done = len(tl.events("spectrum_done"))
            await self._until(lambda: len(tl.events("spectrum_done")) > n_done, 60, "a spectrum")
            dropbox = self.out / "too-dropbox"
            tmp = self.out / "TOO1.json.tmp"
            tmp.write_text(json.dumps(self._too()))
            os.replace(tmp, dropbox / "TOO1.json")
            self._note("ToO dropped into the drop box")
            # T2's mount fails in the middle of a spectrum both feed.
            n_both = len(self._both(tl))
            await self._until(lambda: len(self._both(tl)) > n_both, 120, "a two-telescope spectrum")
            await asyncio.sleep(0.4)
            self._note("T2 mount fault")
            self.telescopes["t2"].mount.fail("mount controller not answering")
            await self._until(
                lambda: any(e.detail["telescope"] == "t2" for e in tl.events("spectrum_dropped")),
                30,
                "t2 dropped",
            )
            await asyncio.sleep(s.fault_duration)
            self.telescopes["t2"].mount.unfail()
            self._note("T2 mount fault cleared")
            # The observer console pauses T2 for a while, then resumes it.
            await asyncio.sleep(1.0)
            control = self.out / "aqawan1-control"
            send_command(control, "pause", ["t2"], operator="demo", reason="check the FAU")
            self._note("console: pause t2")
            await self._until(lambda: "t2" in _holds(control), 30, "t2 held")
            await asyncio.sleep(s.pause)
            send_command(control, "resume", ["t2"], operator="demo", reason="FAU fine")
            self._note("console: resume t2")
            await self._until(lambda: "t2" not in _holds(control), 30, "t2 resumed")
            # Then parks T2 alone at once, and resumes it again.
            await asyncio.sleep(1.0)
            send_command(control, "park", ["t2"], operator="demo", reason="check the mount")
            self._note("console: park t2")
            await self._until(lambda: tl.events("operator_parked"), 30, "t2 parked")
            self._note("t2 parked by the console")
            await asyncio.sleep(0.5)
            send_command(control, "resume", ["t2"], operator="demo", reason="mount fine")
            self._note("console: resume t2 after the park")
            await self._until(lambda: "t2" not in _holds(control), 30, "t2 resumed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.outcome.errors.append(f"director: {exc}")
            log.exception("director")
            self.telescopes["t2"].mount.unfail()
            self.set_weather(True)

    @staticmethod
    def _both(tl: Timeline) -> list[Any]:
        return [e for e in tl.events("spectrum_started") if len(e.detail["telescopes"]) == 2]


def _holds(control: Path) -> dict[str, Any]:
    try:
        return json.loads((control / "holds.json").read_text()).get("holds") or {}
    except (OSError, ValueError):
        return {}


def _tag(night: SimulatedNight, seq: Sequencer) -> str:
    return next((k for k, v in night.outcome.runs.items() if v is seq), "run")


# -- the checks ---------------------------------------------------------------------------


def check(o: Outcome) -> list[str]:
    """Every expectation of the night (module doc) that failed, as text."""
    bad: list[str] = list(o.errors)
    night = o.timelines.get("night")
    if night is None:
        return [*bad, "no night run"]

    def ev(tl: Timeline, what: str) -> list[Any]:
        return tl.events(what, SEQUENCER)

    # Calibrations: no lease, roof closed, through the spectrograph; the
    # biases and darks are true dark frames.
    for tag in ("evening", "morning"):
        tl = o.timelines.get(tag)
        if tl is None:
            bad.append(f"no {tag} calibrations")
            continue
        if ev(tl, "observe_start"):
            bad.append(f"{tag} calibrations took a lease")
        if not ev(tl, "spectrograph_calibration_done"):
            bad.append(f"{tag}: no spectrograph calibration")
        recs = o.runs[tag].spectrum_records
        if [r["kind"] for r in recs] != ["bias", "bias", "dark", "thar", "thar", "fiberflat"]:
            bad.append(f"{tag}: calibration frames {[r['kind'] for r in recs]}")
    for name, f in o.spectrograph_files.items():
        kind = f.get("header", {}).get("IMAGETYP", [None])[0]
        if kind in ("bias", "dark") and not f.get("dark"):
            bad.append(f"{name}: a {kind} taken as a light frame")
        if kind in ("science", "thar", "fiberflat") and f.get("dark"):
            bad.append(f"{name}: a {kind} taken as a dark frame")
    opens = [t for t, c in o.dome_log if c.startswith("OPEN")]
    for tag in ("evening", "morning"):
        tl = o.timelines.get(tag)
        if tl and tl.entries():
            t0, t1 = tl.entries()[0].t, tl.entries()[-1].t
            if any(t0 <= t <= t1 for t in opens):
                bad.append(f"the roof opened during the {tag} calibrations")

    # The night: autofocus first, spectra with both telescopes, imaging.
    af = ev(night, "autofocus_done")
    if {e.detail.get("telescope") for e in af} != set(NAMES):
        bad.append(f"autofocus on {sorted({e.detail.get('telescope') for e in af})}")
    started = ev(night, "spectrum_started")
    done = ev(night, "spectrum_done")
    both = [e for e in done if sorted(e.detail["telescopes"]) == list(NAMES)]
    if len(both) < 3:
        bad.append(f"only {len(both)} spectra fed by both telescopes")
    first_spec = {
        n: min((e.t for e in started if n in e.detail["telescopes"]), default=None) for n in NAMES
    }
    for e in af:
        t = first_spec.get(e.detail.get("telescope"))
        if t is not None and e.t > t:
            bad.append(f"{e.detail.get('telescope')}: autofocus after its first spectrum")
    for cid, st in o.statuses.items():
        if st != "COMPLETED":
            bad.append(f"{cid} {st} at dawn")
    if "TOO1-spec" not in o.statuses:
        bad.append("the ToO never arrived")
    fits = list((o.out / "data").glob("minerva/t1/n*/*.FIELD.*.fits*"))
    if len(fits) != 2:
        bad.append(f"{len(fits)} FIELD frames on disk")

    # The weather interruption: a spectrum aborted, then re-observed.
    aborts = ev(night, "spectrograph_aborted")
    revoked = [e for e in ev(night, "cancel_requested") if "unsafe" in str(e.detail.get("reason"))]
    if not revoked:
        bad.append("no lease revoked for weather")
    if not aborts:
        bad.append("no spectrum aborted")
    outcomes = [e for e in o.plan_record if e["kind"] == "outcome"]
    interrupted = [e for e in outcomes if e["outcome"] == "interrupted" and "spec" in e["config"]]
    if not interrupted:
        bad.append("no spectrum reported interrupted")
    for e in interrupted:
        if not any(x["config"] == e["config"] and x["outcome"] == "complete" for x in outcomes):
            bad.append(f"{e['config']} interrupted and never re-observed")

    # T2's fault: dropped from a spectrum that T1 finished.
    # (A drop because the lease was revoked -- the bad weather, when it
    # falls in a spectrum t2 fed -- is not the mount fault.)
    t2_drops = [
        e
        for e in ev(night, "spectrum_dropped")
        if e.detail["telescope"] == "t2" and "GrantRevoked" not in str(e.detail.get("error"))
    ]
    if not t2_drops:
        bad.append("t2 never dropped out")
    else:
        sid = t2_drops[0].detail["spectrum"]
        fin = [e for e in done if e.detail["spectrum"] == sid]
        if not fin or fin[0].detail["telescopes_end"] != ["t1"]:
            bad.append(f"the spectrum t2 dropped out of did not finish with t1: {fin}")
    con = o.console
    if con:
        if con.get("status", {}).get("code") != 200 or con["status"]["telescopes"] != ["t1", "t2"]:
            bad.append(f"the console's status: {con.get('status')}")
        if not con.get("sky", {}).get("targets"):
            bad.append(f"the console's sky has no targets: {con.get('sky')}")
        if not {"supervisor", "sequencer"} <= set(con.get("log", {}).get("sources", [])):
            bad.append(f"the console's log: {con.get('log')}")
        if con.get("guider_image", {}).get("code") != 200:
            bad.append(f"the console's guider image: {con.get('guider_image')}")
    if not any(c == "console: resume t2" for _, c in o.script):
        bad.append("the console never paused and resumed t2")
    bad += console_park_problems(o)
    if any(u.gave_up for u in o.runs["night"].units.values()):
        bad.append("a telescope gave up for the night")
    if not ev(night, "plan_exhausted"):
        bad.append("the night did not end at dawn")

    bad += safety_violations(o)
    bad += output_problems(o)
    return bad


def safety_violations(o: Outcome) -> list[str]:
    """The safety invariants (docs/demo.md): never open while unsafe; never a
    spectrum without guiding; every abort before park; every park before
    close."""
    bad = []
    # 1. Never open while unsafe: no OPEN between bad weather and the end
    #    of the reopen hold after it cleared.
    unsafe_spans = []
    t_bad = None
    for t, state in o.weather:
        if state == "unsafe" and t_bad is None:
            t_bad = t
        elif state == "safe" and t_bad is not None:
            unsafe_spans.append((t_bad, t + o.reopen_hold))
            t_bad = None
    if t_bad is not None:
        unsafe_spans.append((t_bad, float("inf")))
    for t, c in o.dome_log:
        if c.startswith("OPEN") and any(a <= t < b for a, b in unsafe_spans):
            bad.append(f"{c} at {t:.2f} while unsafe or within the reopen hold")
    # 2. Never a spectrum without guiding.
    for tag, tl in o.timelines.items():
        guiding: dict[str, bool] = {}
        for e in tl.entries():
            if e.source != SEQUENCER or e.kind != "event":
                continue
            if e.what == "guiding":
                guiding[e.detail["telescope"]] = True
            elif e.what == "guiding_stopped":
                guiding[e.detail["telescope"]] = False
            elif e.what == "spectrum_started":
                tels = e.detail["telescopes"]
                if not tels or not all(guiding.get(t) for t in tels):
                    bad.append(f"{tag}: spectrum {e.detail['spectrum']} started unguided: {tels}")
    # 3. Every abort before park, in each cleanup.
    for tag, tl in o.timelines.items():
        for start in tl.events("cleanup_start", SEQUENCER):
            gen = start.detail["gen"]
            done = next(
                (e for e in tl.events("cleanup_done", SEQUENCER) if e.detail["gen"] == gen), None
            )
            t_end = done.t if done else float("inf")
            span = [e for e in tl.entries() if start.t <= e.t <= t_end]
            parks = [
                e.t
                for e in span
                if e.kind == "command" and e.what == "Park" and e.outcome == "sent"
            ]
            aborts = [
                e.t
                for e in span
                if (e.kind == "command" and e.what in ("AbortExposure", "StopExposure"))
                or (e.kind == "event" and e.what == "spectrograph_aborted")
            ]
            if parks and aborts and max(aborts) > min(parks):
                bad.append(f"{tag} cleanup #{gen}: an abort after a park")
    # 4. Every park before close: at each CLOSE command after the roof had
    #    opened, every mount's last motion was a park, completed.
    opened = False
    for t, c in o.dome_log:
        if c.startswith("OPEN"):
            opened = True
        if c.startswith("CLOSE") and opened:
            for name in NAMES:
                last = [
                    e
                    for e in o.mount_events
                    if e[1] == name and e[0] <= t and e[2] != "fault cleared"
                ]
                if last and last[-1][2] != "park":
                    bad.append(f"{c} at {t:.2f} with {name} not parked (last: {last[-1][2]})")
                elif last and t - last[-1][0] < 0.3:
                    bad.append(f"{c} at {t:.2f} with {name} still parking")
    return bad


def console_park_problems(o: Outcome) -> list[str]:
    """The console parked T2 alone: T2 parked, T1 not, the roof untouched."""
    bad = []
    tl = o.timelines["night"]
    req = [e for e in tl.events("park_requested", SEQUENCER) if e.detail["telescope"] == "t2"]
    parked = tl.events("operator_parked", SEQUENCER)
    if not req or not parked:
        return ["the console's park of t2 never happened"]
    p = parked[0]
    if p.detail["telescope"] != "t2" or not p.detail["parked"]:
        bad.append(f"console park: {p.detail}")
    t0, t1 = req[0].t, p.t
    for t, c in o.dome_log:
        if t0 <= t <= t1 + 1.0 and c.startswith(("OPEN", "CLOSE")):
            bad.append(f"the roof moved ({c}) while the console parked t2")
    if any(e[1] == "t1" and e[2] == "park" and t0 <= e[0] <= t1 + 1.0 for e in o.mount_events):
        bad.append("t1 parked when the console parked t2")
    if not any(e[1] == "t2" and e[2] == "park" and t0 <= e[0] <= t1 + 0.5 for e in o.mount_events):
        bad.append("t2's mount never parked for the console")
    return bad


def night_names(o: Outcome) -> list[str]:
    """Every night name the night's outputs carry: data directories, FITS
    ``NIGHT`` and index files, raw spectrum file names, sidecar ``night``
    fields, plan-record files (as nYYYYMMDD)."""
    data = o.out / "data"
    names: set[str] = set()
    for d in data.glob("minerva/*/n*"):
        names.add(d.name)
    for f in data.glob("minerva/*/n*/*"):
        names.add(f.name.split(".", 1)[0])
    for p in data.glob("minerva/kiwispec/n*/*.fits.json"):
        r = json.loads(p.read_text())
        names.add(str(r.get("night")))
        names.add(str(r.get("file")).split(".", 1)[0])
    for name in o.spectrograph_files:
        names.add(str(name).rsplit("/", 1)[-1].split(".", 1)[0])
    for p in (o.out / "plans").glob("*.jsonl"):
        names.add("n" + p.stem.split("-", 1)[1].replace("-", ""))
    return sorted(names)


def output_problems(o: Outcome) -> list[str]:
    """The files: sidecars for every spectrum (with the barycentric
    correction), the plan record, the FITS frames' index."""
    bad = []
    data = o.out / "data"
    sidecars = sorted(data.glob("minerva/kiwispec/n*/*.fits.json"))
    science = [json.loads(p.read_text()) for p in sidecars]
    science = [r for r in science if r.get("kind") == "science"]
    n_done = len(o.timelines["night"].events("spectrum_done", SEQUENCER))
    if len(science) != n_done:
        bad.append(f"{len(science)} science sidecars for {n_done} spectra")
    for r in science:
        b = r.get("barycentric") or {}
        if not b.get("bjd_tdb") or abs(b.get("barycentric_correction", 1e9)) > 31000:
            bad.append(f"{r['file']}: no barycentric correction")
        if not r.get("guide") or not all("telemetry" in g for g in r["guide"].values()):
            bad.append(f"{r['file']}: no guide statistics")
        if r.get("flux_midpoint_utc") is None:
            bad.append(f"{r['file']}: no flux-weighted midpoint")
    if not any(r.get("ended_by") == "counts" for r in science):
        bad.append("no spectrum ended on the exposure meter's counts")
    if not o.plan_record:
        bad.append("no plan record")
    elif not any(e["kind"] == "join" for e in o.plan_record):
        bad.append("no telescope joined a spectrum in the plan record")
    if not list(data.glob("minerva/t*/n*/*.frames.jsonl")):
        bad.append("no frame index")
    # One night, by the one clock, everywhere (robofast_core.clock).
    want = f"n{o.settings.start - timedelta(hours=12):%Y%m%d}"
    names = night_names(o)
    if names != [want]:
        bad.append(f"night names {names}, expected only {want}")
    return bad


def summary(o: Outcome) -> str:
    night = o.timelines.get("night")
    lines = [f"runtime {o.runtime:.1f} s (scheduler clock x{o.settings.scale:g})"]
    if night is not None:
        done = night.events("spectrum_done", SEQUENCER)
        lines.append(f"spectra: {len(done)}")
        for e in done:
            d = e.detail
            lines.append(
                f"  {d['spectrum']:<48} {','.join(d['telescopes']):<6} -> "
                f"{','.join(d['telescopes_end']):<6} {d['ended_by']:<6} {d['file']}"
            )
    lines.append(
        "requests at dawn: " + ", ".join(f"{k} {v}" for k, v in sorted(o.statuses.items()))
    )
    moves = [c for _, c in o.dome_log if c.startswith(("OPEN", "CLOSE"))]
    lines.append(f"roof: {' '.join(moves)}")
    return "\n".join(lines)


async def simulate(out: Path, settings: NightSettings | None = None) -> Outcome:
    night = SimulatedNight(out, settings)
    try:
        await night.start()
        return await night.run()
    finally:
        await night.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run a whole simulated MINERVA RV night (docs/demo.md)."
    )
    ap.add_argument("--out", required=True, help="directory for configs, logs, data and records")
    ap.add_argument(
        "--scale", type=float, default=NightSettings.scale, help="scheduler clock speed-up"
    )
    ap.add_argument(
        "--console-port",
        type=int,
        default=0,
        help="loopback port for the observer console during the night (0: any; -1: none)",
    )
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for h in logging.getLogger().handlers:  # the per-run log files get INFO
        h.setLevel(args.log_level)
    out = Path(args.out)
    port = None if args.console_port < 0 else args.console_port
    outcome = asyncio.run(simulate(out, NightSettings(scale=args.scale, console_port=port)))
    print(summary(outcome))
    problems = check(outcome)
    for p in problems:
        print("PROBLEM:", p)
    print("OK: every check passed" if not problems else f"{len(problems)} problems")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
