"""An Alpaca proxy that stands between robofast and a real mount in a rehearsal.

The hardware rehearsal (:mod:`robofast.testing.rehearsal`,
docs/demo.md) points the supervisor and the sequencer at real mounts while
the enclosure is closed. They reach each real Telescope (and its Focuser
and Rotator, read for the FITS headers) only through this guard, which:

* forwards every read (``GET``) unchanged (``AtPark`` answers the guard's
  own flag, below, but only once the real mount has answered its own
  ``AtPark``: a dead mount does not look parked and alive);
* forwards only the commands a rehearsal needs: ``SlewToCoordinatesAsync``
  (refused unless the target is within the altitude limits at that moment
  and the guard is not "parked"), ``AbortSlew``, ``Tracking`` and
  ``Connected``. Every other command (``Action``, ``SlewToAltAz*``,
  ``MoveAxis``, ``PulseGuide``, ``FindHome``, a focuser ``Move``, a
  rotator move, ...) is refused with an Alpaca error and recorded, except
  ``Action("Status")`` (a read of robofast's PWI server) and, for a
  telescope with a recovery policy (``recovery``), what its recovery
  ladder sends (robofast/recovery.py): ``Action("MountConnect")``,
  ``Action("MotorsEnable")`` and ``FindHome`` (each recorded in
  :attr:`MountGuard.recovery_commands`);
* turns ``Park`` into "stop where you are": ``AbortSlew`` and
  ``Tracking=False`` on the real mount, and ``AtPark`` reads True (from the
  guard) until the next ``Unpark``, which only clears that flag. The real
  mount is never sent ``Park`` or ``Unpark``: what the real mounts do at the
  end of a rehearsal is the harness's explicit end action, not robofast's
  park;
* records every park, unpark and slew (with the mount's own position read
  back when the slew ends: the pointing error against the commanded
  position, and how long the slew took), for the rehearsal's report. A
  slew ends at the first ``Slewing`` False after a ``Slewing`` True, or at
  a ``Slewing`` False with the mount within ``arrive_arcsec`` of the
  target: a PWI2 may not have started moving yet when ``Slewing`` is first
  read (night1108: a slew "ended" 0.24 s after its goto, 25 deg away).

:meth:`MountGuard.lock` refuses every command from then on (the harness
locks the guard before its end action, so nothing robofast still sends can
move a mount afterwards).

It is a harness, not a device server: it does not register as a robofast
simulator (:mod:`robofast.hwgate`), since what it fronts is real.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger(__name__)

#: ASCOM error numbers the guard answers with.
INVALID_VALUE = 0x401
INVALID_OPERATION = 0x40B
INVALID_WHILE_PARKED = 0x408

#: Commands (lower case) forwarded to the real device, by device type.
FORWARDED = {
    "telescope": {"slewtocoordinatesasync", "abortslew", "tracking", "connected"},
    "focuser": {"connected"},
    "rotator": {"connected"},
}
#: Telescope actions (lower case) forwarded for every telescope (reads).
READ_ACTIONS = {"status"}
#: What a recovery ladder sends a telescope (``recovery`` telescopes only):
#: actions, and Alpaca methods.
RECOVERY_ACTIONS = {"mountconnect", "motorsenable"}
RECOVERY_METHODS = {"findhome"}


@dataclass(frozen=True)
class Endpoint:
    """One Alpaca device: ``host:port/N``."""

    host: str
    port: int
    number: int

    @classmethod
    def parse(cls, spec: str) -> Endpoint:
        """``"host:port/N"`` (all three required)."""
        address, slash, number = spec.partition("/")
        host, colon, port = address.rpartition(":")
        if not (slash and colon and host and port.isdigit() and number.isdigit()):
            raise ValueError(f"{spec!r}: expected HOST:PORT/N (e.g. 127.0.0.1:11121/0)")
        return cls(host, int(port), int(number))

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def url(self, kind: str) -> str:
        return f"http://{self.host}:{self.port}/api/v1/{kind}/{self.number}"

    def __str__(self) -> str:
        return f"{self.host}:{self.port}/{self.number}"


def altitude(ra_hours: float, dec_deg: float, lst_hours: float, latitude: float) -> float:
    """Altitude (deg) of ``ra_hours, dec_deg`` at local sidereal time ``lst_hours``."""
    ha = math.radians((lst_hours - ra_hours) * 15.0)
    lat, dec = math.radians(latitude), math.radians(dec_deg)
    s = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def azimuth(ra_hours: float, dec_deg: float, lst_hours: float, latitude: float) -> float:
    """Azimuth (deg, N through E) of ``ra_hours, dec_deg`` at ``lst_hours``."""
    ha = math.radians((lst_hours - ra_hours) * 15.0)
    lat, dec = math.radians(latitude), math.radians(dec_deg)
    y = -math.cos(dec) * math.sin(ha)
    x = math.sin(dec) * math.cos(lat) - math.cos(dec) * math.cos(ha) * math.sin(lat)
    return math.degrees(math.atan2(y, x)) % 360.0


def separation(ra1_h: float, dec1: float, ra2_h: float, dec2: float) -> float:
    """Angular distance (arcsec) between two positions (RA in hours)."""
    a1, a2 = math.radians(ra1_h * 15.0), math.radians(ra2_h * 15.0)
    d1, d2 = math.radians(dec1), math.radians(dec2)
    c = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 3600.0


def lst_hours(when: datetime, longitude: float) -> float:
    """Local apparent sidereal time (h) at ``when``."""
    from astropy import units as u
    from astropy.time import Time

    return float(Time(when).sidereal_time("apparent", longitude=longitude * u.deg).hour)


@dataclass
class _Route:
    name: str  # the telescope's name (t1, ...)
    kind: str  # telescope, focuser, rotator
    upstream: str  # http://host:port/api/v1/kind/N


def _alpaca_error(data: bytes) -> bool:
    try:
        return bool(json.loads(data).get("ErrorNumber"))
    except (ValueError, AttributeError):
        return True


def _alpaca(value: Any = None, *, ctid: int = 0, err: int = 0, msg: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {
        "ClientTransactionID": ctid,
        "ServerTransactionID": 0,
        "ErrorNumber": err,
        "ErrorMessage": msg,
    }
    if value is not None:
        body["Value"] = value
    return body


class _Handler(BaseHTTPRequestHandler):
    server: MountGuard
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _answer(self, value: Any = None, *, ctid: int, err: int = 0, msg: str = "") -> None:
        self._send(200, json.dumps(_alpaca(value, ctid=ctid, err=err, msg=msg)).encode())

    def do_GET(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        g = self.server
        url = urlparse(self.path)
        body = b""
        if self.command == "PUT":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
        params = {k.lower(): v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        form = {k.lower(): v[0] for k, v in parse_qs(body.decode(), keep_blank_values=True).items()}
        parts = [p for p in url.path.lower().split("/") if p]
        route = g.routes.get("/".join(parts[2:4])) if len(parts) == 5 else None
        if parts[:2] != ["api", "v1"] or route is None:
            self._send(400, f"unknown path {self.path}".encode(), "text/plain")
            return
        member = parts[4]
        ctid = int((form or params).get("clienttransactionid", 0) or 0)
        if self.command == "GET":
            if route.kind == "telescope" and member == "atpark":
                code, ctype, data = g.forward("GET", route, member, url.query, b"", None)
                if code != 200 or _alpaca_error(data):
                    self._send(code, data, ctype)  # the real mount does not answer
                    return
                self._answer(g.parked(route.name), ctid=ctid)
                return
            code, ctype, data = g.forward("GET", route, member, url.query, b"", None)
            if route.kind == "telescope" and member == "slewing" and code == 200:
                g.slew_status(route, data)
            self._send(code, data, ctype)
            return
        # Commands.
        if g.locked:
            self._refuse(route, member, form, ctid, "the rehearsal is over (guard locked)")
            return
        if route.kind == "telescope" and member == "park":
            err = g.park(route)
            self._answer(ctid=ctid, err=0x500 if err else 0, msg=err or "")
            return
        if route.kind == "telescope" and member == "unpark":
            g.unpark(route)
            self._answer(ctid=ctid)
            return
        allowed = member in FORWARDED.get(route.kind, set())
        recovery = route.kind == "telescope" and route.name in g.recovery
        if route.kind == "telescope" and member == "action":
            name = form.get("action", "").lower()
            allowed = name in READ_ACTIONS or (recovery and name in RECOVERY_ACTIONS)
            if allowed and name not in READ_ACTIONS:
                g.record_recovery(route, f"Action {form.get('action')}")
        elif recovery and member in RECOVERY_METHODS:
            allowed = True
            g.record_recovery(route, member)
        if not allowed:
            self._refuse(route, member, form, ctid, "not allowed in a hardware rehearsal")
            return
        if route.kind == "telescope" and member == "slewtocoordinatesasync":
            refusal = g.check_slew(route, form)
            if refusal is not None:
                self._refuse(route, member, form, ctid, refusal[1], err=refusal[0])
                return
        ctype_in = self.headers.get("Content-Type") or "application/x-www-form-urlencoded"
        code, ctype, data = g.forward("PUT", route, member, url.query, body, ctype_in)
        if route.kind == "telescope" and member == "slewtocoordinatesasync" and code == 200:
            g.slew_started(route, form, data)
        self._send(code, data, ctype)

    def _refuse(
        self,
        route: _Route,
        member: str,
        form: dict[str, str],
        ctid: int,
        why: str,
        err: int = INVALID_OPERATION,
    ) -> None:
        self.server.record_refusal(route, member, form, why)
        self._answer(ctid=ctid, err=err, msg=f"rehearsal mount guard: {member}: {why}")


class MountGuard(ThreadingHTTPServer):
    """The guard (module doc) for ``devices``: ``{telescope name: {"telescope":
    Endpoint, "focuser": Endpoint | None, "rotator": Endpoint | None}}``.

    Telescope *i* (in the order given) is served as device *i* of each type
    at :attr:`address`. ``limits`` is ``(min_alt, max_alt)``; ``clock`` and
    ``site`` (latitude, longitude) give the altitude of a slew's target at
    the moment it is commanded. ``az_windows`` (optional) ``{name: (lo, hi)}``:
    a slew whose target azimuth at that moment is outside ``lo..hi`` deg is
    refused (a mount whose mechanical azimuth is kept in 0..360, as PWI4's is
    with wrap minimum 0, then never leaves the window). ``events`` (if given)
    receives ``(time.monotonic(), name, "park" | "unpark" | "slew")``.
    ``recovery``: the telescopes whose recovery ladder's commands are
    forwarded (module doc).
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        devices: dict[str, dict[str, Endpoint | None]],
        *,
        limits: tuple[float, float],
        site: tuple[float, float],
        clock: Callable[[], datetime],
        events: list[tuple[float, str, str]] | None = None,
        az_windows: dict[str, tuple[float, float]] | None = None,
        recovery: set[str] | frozenset[str] = frozenset(),
        timeout: float = 30.0,
        host: str = "127.0.0.1",
    ) -> None:
        super().__init__((host, 0), _Handler)
        self.routes: dict[str, _Route] = {}
        self.index: dict[str, int] = {}
        for i, (name, eps) in enumerate(devices.items()):
            self.index[name] = i
            for kind in ("telescope", "focuser", "rotator"):
                ep = eps.get(kind)
                if ep is not None:
                    self.routes[f"{kind}/{i}"] = _Route(name, kind, ep.url(kind))
        self.limits = limits
        self.az_windows = dict(az_windows or {})
        self.recovery = frozenset(recovery)
        #: Every recovery command forwarded: telescope, what, t (monotonic).
        self.recovery_commands: list[dict[str, Any]] = []
        self.site = site
        self.clock = clock
        self.timeout = timeout
        self.events = events if events is not None else []
        self.locked = False
        self._parked = dict.fromkeys(devices, False)
        self._pending: dict[str, dict[str, Any]] = {}
        #: The pending slew has read Slewing True (telescope -> bool).
        self._seen_slewing: dict[str, bool] = {}
        #: A Slewing False ends a slew never seen slewing only this close
        #: to its target (arcsec; robofast's PWI server's slew_tolerance).
        self.arrive_arcsec = 360.0
        self._lock = threading.Lock()
        self._local = threading.local()
        self._clients: list[httpx.Client] = []
        #: Every slew: telescope, ra (h), dec (deg), alt at command, t (monotonic),
        #: and once it ended: seconds, mount_ra, mount_dec, error_arcsec.
        self.slews: list[dict[str, Any]] = []
        #: Every refused command: telescope, device, member, params, why.
        self.refused: list[dict[str, Any]] = []
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    def spec(self, name: str, kind: str = "telescope") -> str:
        """The ``host:port/N`` robofast's config names for ``name``'s ``kind``."""
        return f"{self.address}/{self.index[name]}"

    def start(self) -> MountGuard:
        self._thread = threading.Thread(target=self.serve_forever, name="mount-guard", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        for c in self._clients:
            c.close()

    def lock(self) -> None:
        """Refuse every command from now on (module doc)."""
        self.locked = True

    def parked(self, name: str) -> bool:
        return self._parked[name]

    # -- upstream ----------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        c = getattr(self._local, "client", None)
        if c is None:
            c = httpx.Client(trust_env=False, timeout=self.timeout)
            self._local.client = c
            with self._lock:
                self._clients.append(c)
        return c

    def forward(
        self, method: str, route: _Route, member: str, query: str, body: bytes, ctype: str | None
    ) -> tuple[int, str, bytes]:
        url = f"{route.upstream}/{member}" + (f"?{query}" if query else "")
        headers = {"Content-Type": ctype} if ctype else {}
        try:
            r = self._client().request(method, url, content=body or None, headers=headers)
        except httpx.HTTPError as exc:
            return 500, "text/plain", f"rehearsal mount guard: {route.name}: {exc!r}".encode()
        return r.status_code, r.headers.get("Content-Type", "application/json"), r.content

    def _put(self, route: _Route, member: str, **values: Any) -> str | None:
        """A command to the real device; the error text, or None."""
        form = {
            "ClientID": "1",
            "ClientTransactionID": "0",
            **{k: str(v) for k, v in values.items()},
        }
        try:
            r = self._client().put(f"{route.upstream}/{member}", data=form)
        except httpx.HTTPError as exc:
            return repr(exc)
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:200]}"
        try:
            d = r.json()
        except ValueError:
            return f"not JSON: {r.text[:200]}"
        return None if not d.get("ErrorNumber") else str(d.get("ErrorMessage"))

    def _get(self, route: _Route, member: str) -> Any:
        # The same ClientID as the guard's commands: a robofast device server
        # keeps Connected per client (the preflight connected this one).
        r = self._client().get(
            f"{route.upstream}/{member}", params={"ClientID": "1", "ClientTransactionID": "0"}
        )
        r.raise_for_status()
        d = r.json()
        if d.get("ErrorNumber"):
            raise RuntimeError(d.get("ErrorMessage"))
        return d.get("Value")

    # -- the intercepted commands -------------------------------------------------------

    def park(self, route: _Route) -> str | None:
        """Stop where it is: AbortSlew and Tracking off; AtPark reads True."""
        errs = [
            e
            for e in (self._put(route, "abortslew"), self._put(route, "tracking", Tracking=False))
            if e
        ]
        with self._lock:
            self._parked[route.name] = True
            self._pending.pop(route.name, None)
        self.events.append((time.monotonic(), route.name, "park"))
        log.warning("%s: Park -> tracking off, left where it is (rehearsal)", route.name)
        return "; ".join(errs) or None

    def unpark(self, route: _Route) -> None:
        with self._lock:
            self._parked[route.name] = False
        self.events.append((time.monotonic(), route.name, "unpark"))

    def check_slew(self, route: _Route, form: dict[str, str]) -> tuple[int, str] | None:
        """``(ASCOM error, why)`` if the slew is refused."""
        if self._parked[route.name]:
            return INVALID_WHILE_PARKED, "parked (Unpark first)"
        try:
            ra, dec = float(form["rightascension"]), float(form["declination"])
        except (KeyError, ValueError):
            return INVALID_VALUE, f"bad coordinates {form}"
        alt = altitude(ra, dec, lst_hours(self.clock(), self.site[1]), self.site[0])
        lo, hi = self.limits
        if not lo <= alt <= hi:
            return (
                INVALID_VALUE,
                f"RA {ra:.4f} h Dec {dec:.3f} is at altitude {alt:.1f}: outside {lo}..{hi}",
            )
        window = self.az_windows.get(route.name)
        if window is not None:
            az = azimuth(ra, dec, lst_hours(self.clock(), self.site[1]), self.site[0])
            if not window[0] <= az <= window[1]:
                return (
                    INVALID_VALUE,
                    f"RA {ra:.4f} h Dec {dec:.3f} is at azimuth {az:.1f}:"
                    f" outside {route.name}'s window {window[0]:g}..{window[1]:g}",
                )
        form["_alt"] = f"{alt:.3f}"
        return None

    def slew_started(self, route: _Route, form: dict[str, str], data: bytes) -> None:
        try:
            ok = not json.loads(data).get("ErrorNumber")
        except ValueError:
            ok = False
        if not ok:
            return
        rec = {
            "telescope": route.name,
            "ra": float(form["rightascension"]),
            "dec": float(form["declination"]),
            "alt": float(form.get("_alt", "nan")),
            "utc": self.clock().isoformat(),
            "t": time.monotonic(),
        }
        with self._lock:
            self.slews.append(rec)
            self._pending[route.name] = rec
            self._seen_slewing[route.name] = False
        self.events.append((rec["t"], route.name, "slew"))

    def slew_status(self, route: _Route, data: bytes) -> None:
        """A ``Slewing`` read: a False after a True, or a False with the
        mount on the target (module doc), ends the slew (records the mount's
        position, the pointing error and the slew's duration)."""
        try:
            d = json.loads(data)
        except ValueError:
            return
        value = d.get("Value")
        if d.get("ErrorNumber") or not isinstance(value, bool):
            return
        with self._lock:
            rec = self._pending.get(route.name)
            if rec is None:
                return
            if value:
                self._seen_slewing[route.name] = True
                return
            seen = self._seen_slewing.get(route.name, False)
        end = time.monotonic()
        pos: tuple[float, float] | None = None
        if not seen:
            # Not seen moving yet: done only if it is there already.
            try:
                pos = (
                    float(self._get(route, "rightascension")),
                    float(self._get(route, "declination")),
                )
            except Exception:
                return  # read again at the next Slewing
            if separation(rec["ra"], rec["dec"], *pos) > self.arrive_arcsec:
                return
        with self._lock:
            if self._pending.get(route.name) is not rec:
                return  # another read ended it, or a new slew replaced it
            del self._pending[route.name]
        rec["seconds"] = round(end - rec["t"], 2)
        if pos is None:
            try:
                pos = (
                    float(self._get(route, "rightascension")),
                    float(self._get(route, "declination")),
                )
            except Exception as exc:  # the report says so; the night goes on
                rec["error"] = repr(exc)
                return
        ra, dec = pos
        rec.update(
            mount_ra=ra,
            mount_dec=dec,
            error_arcsec=round(separation(rec["ra"], rec["dec"], ra, dec), 1),
        )

    def record_recovery(self, route: _Route, what: str) -> None:
        rec = {"telescope": route.name, "what": what, "t": time.monotonic()}
        with self._lock:
            self.recovery_commands.append(rec)
        self.events.append((rec["t"], route.name, "recovery"))
        log.warning("%s: recovery: %s forwarded to the real mount", route.name, what)

    def record_refusal(self, route: _Route, member: str, form: dict[str, str], why: str) -> None:
        rec = {
            "telescope": route.name,
            "device": route.kind,
            "member": member,
            "params": {k: v for k, v in form.items() if not k.startswith("client")},
            "why": why,
            "t": time.monotonic(),
        }
        with self._lock:
            self.refused.append(rec)
        log.warning("%s %s: refused %s (%s)", route.name, route.kind, member, why)
