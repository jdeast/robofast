"""HTTP clients for PlaneWave's PWI (CDK700 mounts): PWI4 and the legacy PWI2.

Both speak HTTP GET, one request per command, and answer with the whole
status. Everything here comes from the legacy MINERVA driver
(``MinervaCollaboration/minerva-control``, ``minerva_library/cdk700.py`` for
PWI2; ``minerva_library/pwi4.py`` and ``docs/pwi4.md`` for PWI4, which were
checked against PlaneWave's reference client and the PWI4 4.1.4 binary).
``docs/pwi.md`` has the full command mapping and what is not yet confirmed on
MINERVA's hardware.

* **PWI4** (default port 8220): ``GET /<device>/<command>?k=v``; the reply is
  ``key=value`` lines (``/status`` keys: ``mount.is_connected``,
  ``mount.altitude_degs``, ``m3.port``, ...); booleans ``true``/``false``.
  An unknown command or bad parameter is an HTTP error (400/404).
* **PWI2** (legacy port 44444): ``GET /?device=<d>&cmd=<c>&k=v``; the reply
  is the ``<system>`` XML status (``mount.alt_radian``, ``m3.port``, ...);
  booleans ``True``/``False``. Quirk: ``mount.moving`` is ``True`` whenever
  tracking is off, so it means "slewing" only while tracking.

This client
-----------
* One method per operation; nothing else can be sent (:data:`PWI4_PATHS`,
  :data:`PWI2_COMMANDS` are the allowlists, checked on every request). No
  sync, no pointing-model writes (the one model request is PWI2's
  ``setmodel``, which loads an existing model file: the PWI server's
  ``pointing_models``), no PWI process control, no jog.
* Every request has a hard timeout (connect + reply). A timeout or an
  unreachable PWI is :class:`PWITimeout`; an HTTP error
  :class:`PWICommandError`; a reply that is not a status
  :class:`PWIProtocolError`. Nothing is retried: every command that moves
  something is sent once.
* :meth:`PWIClient.status` returns a :class:`PWIStatus`, the same typed
  status for both backends (``None`` where a backend does not report a
  value; never a made-up number).
* The address must pass :func:`robofast.hwgate.check_endpoint` (the
  server and the jog tool do that); the constructor takes the IP literal
  and, without the hardware gate, a :class:`~robofast.hwgate.SimulatorGuard`
  that re-verifies the simulator before every request
  (:class:`PWIGateRefused` otherwise), so a tunnel that takes the port
  after the simulator has gone is never sent anything.
"""

from __future__ import annotations

import logging
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from robofast import hwgate
from robofast.owner import DeviceError, DeviceTimeout

log = logging.getLogger(__name__)

PWI4_PORT = 8220
PWI2_PORT = 44444
BACKENDS = ("pwi4", "pwi2")


class PWIError(DeviceError):
    """Talking to PWI failed."""


class PWITimeout(PWIError, DeviceTimeout):
    """No reply within the request's deadline (or PWI unreachable)."""


class PWICommandError(PWIError):
    """PWI answered with an HTTP error (a bad command or parameter)."""


class PWIProtocolError(PWIError):
    """A reply that is not a PWI status."""


class PWIGateRefused(PWIError):
    """The endpoint is no longer a verified simulator (the hardware gate)."""


# -- the typed status ------------------------------------------------------------------


@dataclass
class PWIStatus:
    """What both backends report, in one shape. ``None``: not reported."""

    backend: str
    #: PWI is connected to the mount controller.
    connected: bool
    #: Axis motors enabled (az = axis 0, alt = axis 1).
    az_enabled: bool
    alt_enabled: bool
    #: PWI's own "slewing" (PWI4 ``is_slewing``; PWI2 ``moving`` while
    #: tracking). PWI2 cannot say whether an untracked mount is moving.
    slewing: bool | None
    tracking: bool
    alt_deg: float
    az_deg: float
    ra_hours: float  # apparent
    dec_deg: float  # apparent
    ra2000_hours: float | None
    dec2000_deg: float | None
    lst_hours: float | None
    #: Homing in progress (PWI2 ``is_finding_home``; PWI4 does not say).
    finding_home: bool | None
    #: Encoders set by a home (PWI2 ``encoders_have_been_set``; PWI4
    #: ``axis*.is_homed`` only where reported).
    homed: bool | None
    #: Axis motor errors (PWI2 only): (code, message) or None.
    alt_motor_error: tuple[int, str] | None = None
    az_motor_error: tuple[int, str] | None = None
    #: The M3 port (1, 2); 0: between ports or unknown.
    m3_port: int = 0
    m3_moving: bool = False
    #: The focuser on the current M3 port: microns.
    focuser_connected: bool | None = None
    focuser_position: float | None = None
    focuser_moving: bool | None = None
    #: The rotator on the current M3 port: mechanical degrees (None where the
    #: backend does not report that port's rotator; see docs/pwi.md).
    rotator_connected: bool | None = None
    rotator_mech_deg: float | None = None
    rotator_moving: bool | None = None
    pointing_model: str | None = None
    #: PWI's version (PWI4 ``pwi4.version``; PWI2 reports none).
    version: str | None = None
    #: Per-axis homed flags where reported (PWI4 ``axisN.is_homed``), az, alt.
    axes_homed: tuple[bool, bool] | None = None
    #: PWI4: the azimuth axis's mechanical angle (``mount.axis0.position_degs``,
    #: not wrapped) and the minimum of PWI4's axis-0 wrap range
    #: (``mount.axis0_wrap_range_min_degs``: an alt/az or RA/Dec goto puts
    #: azimuth at the mechanical angle in [min, min + 360)).
    az_mech_deg: float | None = None
    az_wrap_min: float | None = None
    raw: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def motors_enabled(self) -> bool:
        return self.az_enabled and self.alt_enabled

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


def _b(text: str | None) -> bool:
    return (text or "").strip().lower() == "true"


def _f(text: str | None) -> float | None:
    try:
        v = float(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _req(raw: dict[str, str], key: str) -> float:
    v = _f(raw.get(key))
    if v is None:
        raise PWIProtocolError(f"status has no numeric {key!r}: {raw.get(key)!r}")
    return v


def sexagesimal_to_float(text: str) -> float:
    """``'HH MM SS.SSS'`` / ``'+DD MM SS.SS'`` (or a plain number) as a float."""
    t = text.strip()
    with_number = _f(t)
    if with_number is not None:
        return with_number
    parts = re.split(r"[ :]+", t)
    if len(parts) != 3:
        raise PWIProtocolError(f"cannot parse {text!r} as sexagesimal")
    sign = -1.0 if parts[0].startswith("-") else 1.0
    try:
        return sign * (abs(float(parts[0])) + float(parts[1]) / 60.0 + float(parts[2]) / 3600.0)
    except ValueError:
        raise PWIProtocolError(f"cannot parse {text!r} as sexagesimal") from None


def parse_pwi4_status(text: str) -> PWIStatus:
    raw: dict[str, str] = {}
    for line in text.split("\n"):
        k, sep, v = line.rstrip("\r").partition("=")
        if sep:
            raw[k] = v
    if "pwi4.version" not in raw or "mount.is_connected" not in raw:
        raise PWIProtocolError(f"not a PWI4 status: {text[:120]!r}")
    try:
        port = int(float(raw.get("m3.port", "0")))
    except ValueError:
        port = 0
    homed = axes_homed = None
    if "mount.axis0.is_homed" in raw and "mount.axis1.is_homed" in raw:
        axes_homed = (_b(raw["mount.axis0.is_homed"]), _b(raw["mount.axis1.is_homed"]))
        homed = all(axes_homed)
    return PWIStatus(
        backend="pwi4",
        connected=_b(raw.get("mount.is_connected")),
        az_enabled=_b(raw.get("mount.axis0.is_enabled")),
        alt_enabled=_b(raw.get("mount.axis1.is_enabled")),
        slewing=_b(raw.get("mount.is_slewing")),
        tracking=_b(raw.get("mount.is_tracking")),
        alt_deg=_req(raw, "mount.altitude_degs"),
        az_deg=_req(raw, "mount.azimuth_degs") % 360.0,
        ra_hours=_req(raw, "mount.ra_apparent_hours"),
        dec_deg=_req(raw, "mount.dec_apparent_degs"),
        ra2000_hours=_f(raw.get("mount.ra_j2000_hours")),
        dec2000_deg=_f(raw.get("mount.dec_j2000_degs")),
        lst_hours=_f(raw.get("site.lmst_hours")),
        finding_home=None,
        homed=homed,
        axes_homed=axes_homed,
        version=raw.get("pwi4.version") or None,
        m3_port=port if port in (1, 2) else 0,
        m3_moving=port not in (1, 2) and raw.get("m3.exists", "0").strip() not in ("0", "false"),
        focuser_connected=_b(raw.get("focuser.is_connected"))
        if "focuser.is_connected" in raw
        else None,
        focuser_position=_f(raw.get("focuser.position")),
        focuser_moving=_b(raw.get("focuser.is_moving")) if "focuser.is_moving" in raw else None,
        rotator_connected=_b(raw.get("rotator.is_connected"))
        if "rotator.is_connected" in raw
        else None,
        # Always Rotator index 0's angle (docs/pwi.md); the caller decides
        # whether that is the current port's rotator.
        rotator_mech_deg=_f(raw.get("rotator.mech_position_degs")),
        rotator_moving=_b(raw.get("rotator.is_moving")) if "rotator.is_moving" in raw else None,
        pointing_model=raw.get("mount.model.filename") or None,
        az_mech_deg=_f(raw.get("mount.axis0.position_degs")),
        az_wrap_min=_f(raw.get("mount.axis0_wrap_range_min_degs")),
        raw=raw,
    )


def _flatten(elem: ET.Element, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for child in elem:
        key = f"{prefix}{child.tag}"
        if len(child):
            out |= _flatten(child, key + ".")
        else:
            out[key] = (child.text or "").strip()
    return out


def parse_pwi2_status(text: str) -> PWIStatus:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise PWIProtocolError(f"not PWI2 XML ({exc}): {text[:120]!r}") from None
    if root.tag != "system":
        raise PWIProtocolError(f"not a PWI2 <system> status: <{root.tag}>")
    raw = _flatten(root)
    if "mount.connected" not in raw or "mount.alt_radian" not in raw:
        raise PWIProtocolError("PWI2 status without mount.connected / mount.alt_radian")
    tracking = _b(raw.get("mount.tracking"))
    port_text = raw.get("m3.port", "0")
    try:
        port = int(float(port_text))
    except ValueError:
        port = 0
    port = port if port in (1, 2) else 0

    def motor(axis: str) -> tuple[int, str] | None:
        code = raw.get(f"mount.{axis}_motor_error_code")
        if code is None:
            return None
        try:
            n = int(float(code))
        except ValueError:
            n = -1
        return (n, raw.get(f"mount.{axis}_motor_error_message", ""))

    def sexa(key: str) -> float | None:
        v = raw.get(key)
        if v is None:
            return None
        try:
            return sexagesimal_to_float(v)
        except PWIProtocolError:
            return None

    dev = f"{port}" if port else ""
    foc, rot = f"focuser{dev}", f"rotator{dev}"
    ra_rad, dec_rad = _f(raw.get("mount.ra_radian")), _f(raw.get("mount.dec_radian"))
    ra = sexa("mount.ra") if ra_rad is None else math.degrees(ra_rad) / 15.0
    dec = sexa("mount.dec") if dec_rad is None else math.degrees(dec_rad)
    if ra is None or dec is None:
        raise PWIProtocolError("PWI2 status without the mount's RA/Dec")
    return PWIStatus(
        backend="pwi2",
        connected=_b(raw.get("mount.connected")),
        az_enabled=_b(raw.get("mount.azm_enabled")),
        alt_enabled=_b(raw.get("mount.alt_enabled")),
        # PWI2's "moving" is true whenever tracking is off (cdk700.isMoving).
        slewing=_b(raw.get("mount.moving")) if tracking else None,
        tracking=tracking,
        alt_deg=math.degrees(_req(raw, "mount.alt_radian")),
        az_deg=math.degrees(_req(raw, "mount.azm_radian")) % 360.0,
        ra_hours=ra % 24.0,
        dec_deg=dec,
        ra2000_hours=sexa("mount.ra_2000"),
        dec2000_deg=sexa("mount.dec_2000"),
        lst_hours=sexa("status.lst"),
        finding_home=_b(raw.get("mount.is_finding_home"))
        if "mount.is_finding_home" in raw
        else None,
        homed=_b(raw.get("mount.encoders_have_been_set"))
        if "mount.encoders_have_been_set" in raw
        else None,
        alt_motor_error=motor("alt"),
        az_motor_error=motor("azm"),
        m3_port=port,
        m3_moving=_b(raw.get("m3.moving_rotate")),
        focuser_connected=_b(raw.get(f"{foc}.connected")) if f"{foc}.connected" in raw else None,
        focuser_position=_f(raw.get(f"{foc}.position")),
        focuser_moving=_b(raw.get(f"{foc}.moving")) if f"{foc}.moving" in raw else None,
        rotator_connected=_b(raw.get(f"{rot}.connected")) if f"{rot}.connected" in raw else None,
        rotator_mech_deg=_f(raw.get(f"{rot}.position")) if port else None,
        rotator_moving=_b(raw.get(f"{rot}.moving")) if f"{rot}.moving" in raw else None,
        pointing_model=raw.get("mount.pointing_model") or None,
        raw=raw,
    )


# -- the allowlists -----------------------------------------------------------------------

#: Every PWI4 request this client may send (path -> moves hardware).
PWI4_PATHS: dict[str, bool] = {
    "/status": False,
    "/mount/connect": False,
    "/mount/enable": False,
    "/mount/disable": True,  # stops the axes
    "/mount/find_home": True,  # both axes (4.0.3+)
    "/mount/findhome": True,  # ?axis=0|1: one axis (4.1.7 beta 3+, changelog spelling)
    "/mount/goto_ra_dec_j2000": True,
    "/mount/goto_alt_az": True,
    "/mount/offset": True,
    "/mount/stop": True,
    "/mount/set_axis0_wrap_range_min": False,  # ?degs=: where gotos put azimuth
    "/mount/tracking_on": True,
    "/mount/tracking_off": True,
    "/m3/goto": True,
    "/m3/stop": True,
    "/focuser/goto": True,
    "/focuser/stop": True,
    "/rotator/goto_mech": True,
    "/rotator/stop": True,
}

#: Every PWI2 (device, cmd) this client may send (-> moves hardware).
PWI2_COMMANDS: dict[tuple[str, str], bool] = {
    ("", "getsystem"): False,
    ("mount", "connect"): False,
    ("mount", "enable"): False,
    ("mount", "disable"): True,
    ("mount", "findhome"): True,
    # Loads an existing pointing-model file (legacy cdk700.startPWI); moves nothing.
    ("mount", "setmodel"): False,
    ("mount", "move"): True,
    ("mount", "stop"): True,
    ("mount", "trackingon"): True,
    ("mount", "trackingoff"): True,
    ("m3", "select"): True,
    ("m3", "stop"): True,
    ("focuser1", "move"): True,
    ("focuser2", "move"): True,
    ("focuser1", "stop"): True,
    ("focuser2", "stop"): True,
    ("rotator1", "move"): True,
    ("rotator2", "move"): True,
    ("rotator1", "stop"): True,
    ("rotator2", "stop"): True,
}


def _num(x: float) -> str:
    if not math.isfinite(x):
        raise ValueError(f"not finite: {x}")
    return repr(float(x))


# -- the clients ------------------------------------------------------------------------


class PWIClient:
    """Base: one PWI at ``host:port`` (an IP literal that passed the gate).

    Use from one event loop. Requests may overlap (a stop never waits behind
    a slow status read)."""

    backend = ""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = 5.0,
        connect_timeout: float = 2.0,
        guard: hwgate.SimulatorGuard | None = None,
    ):
        #: Re-verifies the simulator before every request (None: gate open,
        #: or a caller that verified it some other way).
        self.guard = guard
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        netloc = f"[{host}]" if ":" in host else host
        self.base = f"http://{netloc}:{self.port}"
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=0),
        )
        #: Every request sent (path and parameters), for logs and tests.
        self.sent: list[str] = []

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, timeout: float | None) -> str:
        if self.guard is not None:
            try:
                self.guard.check()
            except hwgate.HardwareNotAllowed as exc:
                raise PWIGateRefused(str(exc)) from None
        self.sent.append(url[len(self.base) :])
        log.debug("PWI %s", url)
        try:
            r = await self._http.get(url, timeout=timeout or self.timeout)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise PWITimeout(f"PWI {self.host}:{self.port}: {type(exc).__name__}: {exc}") from None
        if r.status_code != 200:
            raise PWICommandError(
                f"PWI {url[len(self.base) :]}: HTTP {r.status_code}: {r.text.strip()[:200]}"
            )
        return r.text

    # Implemented by each backend.
    async def status(self) -> PWIStatus:
        raise NotImplementedError

    async def mount_connect(self) -> PWIStatus:
        raise NotImplementedError

    async def enable_motors(self) -> PWIStatus:
        raise NotImplementedError

    async def disable_motors(self) -> PWIStatus:
        raise NotImplementedError

    async def find_home(self, axis: int | None = None) -> PWIStatus:
        """Home both axes (``axis`` None) or one (0 azimuth, 1 altitude)."""
        raise NotImplementedError

    async def goto_radec_j2000(self, ra_hours: float, dec_deg: float) -> PWIStatus:
        raise NotImplementedError

    async def goto_altaz(self, alt_deg: float, az_deg: float) -> PWIStatus:
        raise NotImplementedError

    async def offset_radec(self, ra_arcsec: float, dec_arcsec: float) -> PWIStatus:
        """Add to the target: RA *coordinate* arcsec (no cos Dec), Dec arcsec."""
        raise NotImplementedError

    async def stop(self) -> PWIStatus:
        raise NotImplementedError

    async def tracking(self, on: bool) -> PWIStatus:
        raise NotImplementedError

    async def set_az_wrap_min(self, degs: float) -> PWIStatus:
        """PWI4: gotos put azimuth at the mechanical angle in [degs, degs + 360)."""
        raise PWIError(f"{self.backend} has no azimuth wrap range setting")

    async def set_model(self, filename: str) -> PWIStatus:
        """PWI2: load the pointing-model file ``filename`` (``setmodel``)."""
        raise PWIError(f"{self.backend}: robofast does not load pointing models through it")

    async def m3_select(self, port: int) -> PWIStatus:
        raise NotImplementedError

    async def m3_stop(self) -> PWIStatus:
        raise NotImplementedError

    async def focuser_goto(self, port: int, microns: float) -> PWIStatus:
        raise NotImplementedError

    async def focuser_stop(self, port: int) -> PWIStatus:
        raise NotImplementedError

    async def rotator_goto_mech(self, port: int, degs: float) -> PWIStatus:
        raise NotImplementedError

    async def rotator_stop(self, port: int) -> PWIStatus:
        raise NotImplementedError


class PWI4Client(PWIClient):
    backend = "pwi4"

    async def _cmd(self, path: str, timeout: float | None = None, **params: Any) -> PWIStatus:
        if path not in PWI4_PATHS:
            raise PWIError(f"PWI4 {path} is not an allowed request")
        url = self.base + path
        q = [(k, v) for k, v in params.items() if v is not None]
        if q:
            url += "?" + urlencode(q).replace("+", "%20")
        return parse_pwi4_status(await self._get(url, timeout))

    async def status(self) -> PWIStatus:
        return await self._cmd("/status")

    async def mount_connect(self) -> PWIStatus:
        return await self._cmd("/mount/connect")

    async def enable_motors(self) -> PWIStatus:
        # axis=-1 is rejected by PWI4 4.1.4 (legacy docs/pwi4.md).
        await self._cmd("/mount/enable", axis=0)
        return await self._cmd("/mount/enable", axis=1)

    async def disable_motors(self) -> PWIStatus:
        await self._cmd("/mount/disable", axis=0)
        return await self._cmd("/mount/disable", axis=1)

    async def find_home(self, axis: int | None = None) -> PWIStatus:
        if axis is None:
            return await self._cmd("/mount/find_home")
        if axis not in (0, 1):
            raise ValueError(f"axis {axis}: 0 (azimuth) or 1 (altitude)")
        # The changelog (4.1.7 beta 3) spells this one "findhome"; an older
        # PWI4 answers 404 and nothing moves. Callers check the version first.
        return await self._cmd("/mount/findhome", axis=axis)

    async def goto_radec_j2000(self, ra_hours: float, dec_deg: float) -> PWIStatus:
        return await self._cmd(
            "/mount/goto_ra_dec_j2000", ra_hours=_num(ra_hours), dec_degs=_num(dec_deg)
        )

    async def goto_altaz(self, alt_deg: float, az_deg: float) -> PWIStatus:
        return await self._cmd("/mount/goto_alt_az", alt_degs=_num(alt_deg), az_degs=_num(az_deg))

    async def offset_radec(self, ra_arcsec: float, dec_arcsec: float) -> PWIStatus:
        return await self._cmd(
            "/mount/offset", ra_add_arcsec=_num(ra_arcsec), dec_add_arcsec=_num(dec_arcsec)
        )

    async def stop(self) -> PWIStatus:
        return await self._cmd("/mount/stop")

    async def tracking(self, on: bool) -> PWIStatus:
        return await self._cmd("/mount/tracking_on" if on else "/mount/tracking_off")

    async def set_az_wrap_min(self, degs: float) -> PWIStatus:
        return await self._cmd("/mount/set_axis0_wrap_range_min", degs=_num(degs))

    async def m3_select(self, port: int) -> PWIStatus:
        return await self._cmd("/m3/goto", port=int(port))

    async def m3_stop(self) -> PWIStatus:
        return await self._cmd("/m3/stop")

    # PWI4 acts on the focuser/rotator *selected in its GUI* ("Select from
    # M3": the current port's); ``port`` is checked by the caller.
    async def focuser_goto(self, port: int, microns: float) -> PWIStatus:
        return await self._cmd("/focuser/goto", target=_num(microns))

    async def focuser_stop(self, port: int) -> PWIStatus:
        return await self._cmd("/focuser/stop")

    async def rotator_goto_mech(self, port: int, degs: float) -> PWIStatus:
        return await self._cmd("/rotator/goto_mech", degs=_num(degs))

    async def rotator_stop(self, port: int) -> PWIStatus:
        return await self._cmd("/rotator/stop")


class PWI2Client(PWIClient):
    backend = "pwi2"

    async def _cmd(
        self, device: str, cmd: str, timeout: float | None = None, **params: Any
    ) -> PWIStatus:
        if (device, cmd) not in PWI2_COMMANDS:
            raise PWIError(f"PWI2 device={device!r} cmd={cmd!r} is not an allowed request")
        q: list[tuple[str, Any]] = [("device", device)] if device else []
        q += [("cmd", cmd)] + [(k, v) for k, v in params.items() if v is not None]
        return parse_pwi2_status(await self._get(f"{self.base}/?{urlencode(q)}", timeout))

    async def status(self) -> PWIStatus:
        return await self._cmd("", "getsystem")

    async def mount_connect(self) -> PWIStatus:
        return await self._cmd("mount", "connect")

    async def enable_motors(self) -> PWIStatus:
        return await self._cmd("mount", "enable")

    async def disable_motors(self) -> PWIStatus:
        return await self._cmd("mount", "disable")

    async def find_home(self, axis: int | None = None) -> PWIStatus:
        if axis is not None:
            raise PWIError("PWI2 homes both axes together; it cannot home one axis")
        return await self._cmd("mount", "findhome")

    async def goto_radec_j2000(self, ra_hours: float, dec_deg: float) -> PWIStatus:
        return await self._cmd("mount", "move", ra2000=_num(ra_hours), dec2000=_num(dec_deg))

    async def goto_altaz(self, alt_deg: float, az_deg: float) -> PWIStatus:
        return await self._cmd("mount", "move", alt=_num(alt_deg), azm=_num(az_deg))

    async def offset_radec(self, ra_arcsec: float, dec_arcsec: float) -> PWIStatus:
        return await self._cmd(
            "mount", "move", incrementra=_num(ra_arcsec), incrementdec=_num(dec_arcsec)
        )

    async def stop(self) -> PWIStatus:
        return await self._cmd("mount", "stop")

    async def tracking(self, on: bool) -> PWIStatus:
        return await self._cmd("mount", "trackingon" if on else "trackingoff")

    async def set_model(self, filename: str) -> PWIStatus:
        if not filename or any(c in filename for c in "\r\n"):
            raise ValueError(f"setmodel: bad file name {filename!r}")
        return await self._cmd("mount", "setmodel", filename=filename)

    async def m3_select(self, port: int) -> PWIStatus:
        return await self._cmd("m3", "select", port=int(port))

    async def m3_stop(self) -> PWIStatus:
        return await self._cmd("m3", "stop")

    async def focuser_goto(self, port: int, microns: float) -> PWIStatus:
        return await self._cmd(f"focuser{int(port)}", "move", position=_num(microns))

    async def focuser_stop(self, port: int) -> PWIStatus:
        return await self._cmd(f"focuser{int(port)}", "stop")

    async def rotator_goto_mech(self, port: int, degs: float) -> PWIStatus:
        return await self._cmd(f"rotator{int(port)}", "move", position=_num(degs))

    async def rotator_stop(self, port: int) -> PWIStatus:
        return await self._cmd(f"rotator{int(port)}", "stop")


def version_at_least(version: str | None, minimum: tuple[int, ...]) -> bool:
    """``"4.1.8.0"`` >= ``(4, 1, 7)``; False when unknown or unparsable.
    A beta counts as its base version (4.1.7 beta 3 reports 4.1.7.x)."""
    if not version:
        return False
    try:
        parts = tuple(int(x) for x in re.findall(r"\d+", version)[: len(minimum)])
    except ValueError:
        return False
    return len(parts) == len(minimum) and parts >= minimum


#: PWI4 versions with per-axis homing (``/mount/findhome?axis=``).
PER_AXIS_HOME_VERSION = (4, 1, 7)


def make_client(backend: str, host: str, port: int, **kw: Any) -> PWIClient:
    if backend == "pwi4":
        return PWI4Client(host, port, **kw)
    if backend == "pwi2":
        return PWI2Client(host, port, **kw)
    raise ValueError(f"backend must be one of {BACKENDS}, not {backend!r}")
