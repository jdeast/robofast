"""Alpaca Dome server for an Aqawan two-shutter roll-off enclosure.

One Alpaca **Dome** (device 0) stands for the whole Aqawan, whose PAC
controller it drives through :class:`~robofast_core.devices.aqawan_pac.PACClient`.

Members
-------
* ``OpenShutter`` opens both shutters in the configured order, the legacy
  way (``minerva_library/aqawan.py`` in MinervaCollaboration/minerva-control,
  ``open_both``/``open_shutter``, lines 188-256): lights off, then for each
  shutter in ``open_order`` -- unless it is already ``OPEN`` -- send
  ``OPEN_SHUTTER_n``, require ``Success=TRUE``, and wait (polling ``STATUS``)
  while it reports ``OPENING``, up to ``shutter_open_timeout``; it must end
  ``OPEN`` before the next one is commanded. Any failure stops the sequence
  (the second shutter is never opened after the first failed). Legacy MINERVA
  used order ``[2, 1]`` for aqawan1 ("reverse", ``domeControl.py:67``) and
  ``[1, 2]`` for aqawan2. The interlock checks and the *first* open command
  run inside the call, so a PAC refusal (``Heartbeat timer expired``, ``Estop
  active``, ...) is returned to the caller; lights-off (after that first
  command, unlike legacy, so the call returns quickly) and the rest run in
  the background. ``Slewing`` is true while either shutter moves (IDomeV3).
* ``CloseShutter`` cancels any open sequence, then sends ``close_command``
  (default ``CLOSE_SEQUENTIAL``, the legacy close, ``aqawan.py:281``). Close
  and ``STOP`` pre-empt any routine PAC exchange in flight.
* ``ShutterStatus`` is the shutters' *position*; a fault is a separate axis
  (JDE, 2026-10-31: "closed and fault" is two states, and the dangerous one
  is "open and fault"). ``closed`` only if both report ``CLOSED``, fault or
  not; ``closing`` if either is closing; ``shutterError`` for a PAC
  ``Fault``/``Error`` with a shutter open or part-way open, a failed open
  sequence, an unexpected state string, or one open and one closed with no
  sequence running; ``open`` if both are open; else ``opening``. (Closing
  wins over Fault/Error: the enclosure is getting safe, and the supervisor
  re-commands a close that takes longer than its ``shutter_timeout``.) So
  "open and fault" reads ``shutterError`` and the supervisor closes, while
  "closed and fault" reads ``closed`` and nothing is re-sent. When the
  Fault/Error flags appear, the PAC's ``GET_FAULTS``/``GET_ERRORS`` text is
  logged. A shutter string the server does not know (the PAC's state after
  ``STOP`` or an E-stop mid-travel is not yet known) is ``shutterError``,
  and the raw string is logged once.
* ``AbortSlew`` cancels the open sequence, sends ``STOP``, then re-reads
  ``STATUS`` and logs the shutter strings the PAC reports.
* ``Action("Heartbeat", "")`` sends ``HEARTBEAT`` (the contract is in
  :mod:`robofast_core.heartbeat_watchdog`; the PAC itself closes the
  shutters when the pings stop, and refuses to open until they resume; its
  timeout is 60 s, confirmed by JDE 2026-10-31).
  ``Action("Status", "")`` returns the parsed PAC status as JSON;
  ``GetFaults``/``GetErrors`` return the PAC's text.
* ``Action("Faults", "")`` is the fault channel (read-only): JSON
  ``{"fault": bool, "error": bool, "faults": str|null, "errors": str|null,
  "shutters": [s1, s2]}``, from a STATUS at most ``status_max_age`` old
  (``faults``/``errors`` are the PAC's text, once fetched). The safety
  supervisor polls it (``SafetyConfig.fault_action``) and alarms.
* ``Action("ClearFaults", note)`` sends ``CLEAR_FAULTS`` then
  ``CLEAR_ERRORS``, logged with ``note`` (who and why). It is refused unless
  both shutters are reported ``CLOSED`` and no open sequence runs. It is for
  an operator, through the supervisor (``POST /faults/clear``); the dome
  owner makes it privileged (``"Action:ClearFaults"`` in
  :data:`robofast_core.safety.ENCLOSURE_MEMBERS`), so a sequencer cannot
  call it. Nothing clears faults automatically (not decided, 2026-10-31).
* ``OpenShutter`` is refused (``InvalidOperationException``) while the PAC
  reports a Fault or Error, or an operating mode other than ``AUTO``, and
  (only if ``interlock_doors`` names them) a door that is not ``CLOSED``.
  Door state does not matter for observing (JDE, 2026-10-31), so the default
  interlocks no doors.
* No azimuth, altitude, park, home or slaving: those members raise
  ``NotImplementedException``; the ``Can*`` flags say so.

``Connected`` is tracked per Alpaca ``ClientID``, so one client
disconnecting (ConformU, an operator tool) never disconnects the safety
supervisor. Connecting reads the PAC's status once.

Safety
------
The PAC address must be loopback (a simulator) unless the config says
``"allow_hardware": true`` **and** the command line passes ``--hardware``.
The address is resolved once at startup and checked; the server connects to
that literal address only.

Running
-------
::

    python -m robofast_core.devices.aqawan_server --config aqawan.json [--hardware]

with a JSON config (``deploy/aqawan.example.json``; every key except ``pac``
is optional)::

    {"name": "aqawan1", "listen": "127.0.0.1:11113",
     "pac": {"host": "127.0.0.1", "port": 22004},
     "open_order": [2, 1]}

It prints one JSON line, ``{"url": ..., "pac": ...}``, once listening.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import dataclasses
import datetime
import json
import logging
import socket
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from robofast_core.devices.aqawan_pac import (
    SHUTTER_STATES,
    HardwareNotAllowed,
    PACClient,
    PACError,
    PACRefused,
    PACStatus,
    PACTimeout,
    check_pac_address,
)

log = logging.getLogger(__name__)

DRIVER_VERSION = "0.1.0"
OPEN, CLOSED, OPENING, CLOSING, ERROR = 0, 1, 2, 3, 4  # ASCOM ShutterState

# Alpaca error numbers.
NOT_IMPLEMENTED = 0x400
INVALID_VALUE = 0x401
NOT_CONNECTED = 0x407
INVALID_OPERATION = 0x40B
ACTION_NOT_IMPLEMENTED = 0x40C
DRIVER_ERROR = 0x500
PAC_REFUSED = 0x501
PAC_TIMEOUT = 0x502


class AlpacaException(Exception):
    def __init__(self, number: int, message: str) -> None:
        super().__init__(message)
        self.number = number


# -- configuration ----------------------------------------------------------------


@dataclass
class PACConfig:
    host: str = "127.0.0.1"
    port: int = 22004
    connect_timeout: float = 2.0
    command_timeout: float = 4.0
    resend_after: float = 0.5
    max_attempts: int = 3
    idle_end: float = 0.3


@dataclass
class AqawanConfig:
    pac: PACConfig = field(default_factory=PACConfig)
    #: Must be true (and ``--hardware`` given) for a non-loopback PAC.
    allow_hardware: bool = False
    name: str = "aqawan"
    listen: str = "127.0.0.1:11113"
    #: Shutters in opening order; legacy aqawan1 used [2, 1], aqawan2 [1, 2].
    open_order: tuple[int, int] = (1, 2)
    close_command: str = "CLOSE_SEQUENTIAL"
    lights_off_before_open: bool = True
    #: Per shutter, while it reports OPENING (legacy: 180 s, aqawan.py:195).
    shutter_open_timeout: float = 180.0
    #: After a move is accepted, how long the PAC may still report the old
    #: state; meanwhile ShutterStatus reports the commanded direction.
    start_grace: float = 5.0
    #: STATUS polling period while an open sequence runs.
    poll_interval: float = 1.0
    #: ShutterStatus reuses a STATUS this recent.
    status_max_age: float = 1.0
    #: Bound on each Alpaca request (the supervisor's dome_timeout is 5 s).
    request_timeout: float = 4.5
    #: Doors that must report CLOSED before opening (STATUS keys, e.g.
    #: EntryDoor1, EntryDoor2, PanelDoor). None by default: door state does
    #: not matter for observing (JDE, 2026-10-31).
    interlock_doors: tuple[str, ...] = ()
    #: Refuse to open unless EnclOpMode (when reported) is AUTO.
    require_auto: bool = True
    #: UDP port for Alpaca discovery (32227 is standard); None: off.
    discovery_port: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AqawanConfig:
        d = dict(d)
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        pac = d.pop("pac", {})
        pac_names = {f.name for f in dataclasses.fields(PACConfig)}
        if set(pac) - pac_names:
            raise ValueError(f"unknown pac keys: {sorted(set(pac) - pac_names)}")
        for key in ("open_order", "interlock_doors"):
            if key in d:
                d[key] = tuple(d[key])
        cfg = cls(pac=PACConfig(**pac), **d)
        if sorted(cfg.open_order) != [1, 2]:
            raise ValueError("open_order must be [1, 2] or [2, 1]")
        if cfg.close_command not in ("CLOSE_SEQUENTIAL", "CLOSE_SHUTTERS"):
            raise ValueError("close_command must be CLOSE_SEQUENTIAL or CLOSE_SHUTTERS")
        return cfg


# -- the enclosure ------------------------------------------------------------------


def aggregate(status: PACStatus, *, sequence: bool, failed: str | None) -> int:
    """One ASCOM ShutterState for the two shutters (see the module doc)."""
    s = status.shutters
    if s == ("CLOSED", "CLOSED"):
        return CLOSED
    if any(x not in SHUTTER_STATES for x in s):
        return ERROR
    if "CLOSING" in s:
        return CLOSING
    if status.fault or status.error or failed:
        return ERROR
    if s == ("OPEN", "OPEN"):
        return OPEN
    if "OPENING" in s or sequence:
        return OPENING
    return ERROR  # one open, one closed, nothing running


class Aqawan:
    """The enclosure logic over a :class:`PACClient`. Runs on one event loop."""

    def __init__(self, pac: PACClient, config: AqawanConfig) -> None:
        self.pac = pac
        self.config = config
        self._status: PACStatus | None = None
        self._status_at = -1e9
        self._status_lock = asyncio.Lock()
        self._sequence: asyncio.Task | None = None
        self._failed: str | None = None
        self._commanded: tuple[int, float] | None = None  # (OPENING/CLOSING, when)
        self._flags = (False, False)
        #: The PAC's GET_FAULTS / GET_ERRORS text while the flag is set.
        self._pac_text: dict[str, str | None] = {"GET_FAULTS": None, "GET_ERRORS": None}
        self._odd_shutters: tuple[str, str] | None = None
        self._background: set[asyncio.Task] = set()

    # -- status -------------------------------------------------------------

    async def status(self, max_age: float | None = None) -> PACStatus:
        max_age = self.config.status_max_age if max_age is None else max_age
        async with self._status_lock:
            if self._status is None or time.monotonic() - self._status_at > max_age:
                t = time.monotonic()
                st = await self.pac.status()
                self._status, self._status_at = st, t
                self._note_flags(st)
                self._note_shutters(st)
            return self._status

    def _note_flags(self, st: PACStatus) -> None:
        flags = (st.fault, st.error)
        if flags != self._flags:
            self._flags = flags
            if not st.fault:
                self._pac_text["GET_FAULTS"] = None
            if not st.error:
                self._pac_text["GET_ERRORS"] = None
            if st.fault or st.error:
                log.warning(
                    "PAC reports Fault=%s Error=%s (shutters %s)", st.fault, st.error, st.shutters
                )
                self._spawn(self._log_pac_text(st.fault, st.error))
            else:
                log.info("PAC Fault and Error cleared")

    def _note_shutters(self, st: PACStatus) -> None:
        """Log, once per distinct value, a shutter string we do not know."""
        odd = st.shutters if any(x not in SHUTTER_STATES for x in st.shutters) else None
        if odd is not None and odd != self._odd_shutters:
            log.warning(
                "PAC reports unrecognised shutter state Shutter1=%r Shutter2=%r"
                " (treated as shutterError); raw STATUS: %s",
                st.raw.get("Shutter1"),
                st.raw.get("Shutter2"),
                st.raw,
            )
        self._odd_shutters = odd

    async def _log_pac_text(self, fault: bool, error: bool) -> None:
        for flag, command in ((fault, "GET_FAULTS"), (error, "GET_ERRORS")):
            if flag:
                try:
                    text = await self.pac.command(command)
                except PACError as exc:
                    log.warning("PAC %s failed: %s", command, exc)
                    continue
                log.warning("PAC %s: %s", command, text)
                if self._flags[0 if command == "GET_FAULTS" else 1]:
                    self._pac_text[command] = text

    async def faults(self) -> dict[str, Any]:
        """The fault channel: Fault/Error flags, the PAC's text, the shutters."""
        st = await self.status()
        return {
            "fault": st.fault,
            "error": st.error,
            "faults": self._pac_text["GET_FAULTS"] if st.fault else None,
            "errors": self._pac_text["GET_ERRORS"] if st.error else None,
            "shutters": list(st.shutters),
        }

    async def clear_faults(self, note: str) -> str:
        """Operator only (privileged at the owner): clear the PAC's faults and
        errors, but only with the enclosure closed. Never automatic."""
        st = await self.status(max_age=0)
        if st.shutters != ("CLOSED", "CLOSED") or self.sequence_running:
            raise AlpacaException(
                INVALID_OPERATION,
                f"not clearing faults: enclosure not closed (shutters {st.shutters})",
            )
        log.warning(
            "ClearFaults requested (%s); PAC Fault=%s Error=%s",
            note or "no note",
            st.fault,
            st.error,
        )
        try:
            replies = [await self.pac.action(c) for c in ("CLEAR_FAULTS", "CLEAR_ERRORS")]
        finally:
            self._invalidate()
        st = await self.status(max_age=0)
        log.warning("after ClearFaults: PAC Fault=%s Error=%s", st.fault, st.error)
        return "; ".join(replies)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _invalidate(self) -> None:
        self._status_at = -1e9

    @property
    def sequence_running(self) -> bool:
        return self._sequence is not None and not self._sequence.done()

    async def shutter_status(self) -> int:
        st = await self.status()
        state = aggregate(st, sequence=self.sequence_running, failed=self._failed)
        if self._commanded is not None:
            direction, at = self._commanded
            target = "OPENING" if direction == OPENING else "CLOSING"
            if (
                time.monotonic() - at > self.config.start_grace
                or target in st.shutters
                or state == (OPEN if direction == OPENING else CLOSED)
            ):
                # Expired, or the PAC has acknowledged the move: from now on
                # its own report counts (so a late, contrary motion shows).
                self._commanded = None
            elif direction == OPENING and state == CLOSED:
                return OPENING  # accepted; the PAC has not reported it yet
            elif direction == CLOSING and state in (OPEN, OPENING):
                return CLOSING
        return state

    # -- commands -----------------------------------------------------------

    def _refusal(self, st: PACStatus) -> str | None:
        if st.fault or st.error:
            return f"PAC reports Fault={st.fault} Error={st.error}"
        for door in self.config.interlock_doors:
            value = st.raw.get(door, "")
            if value.upper() != "CLOSED":
                return f"{door} is {value or 'unknown'}"
        if self.config.require_auto and st.op_mode is not None and st.op_mode != "AUTO":
            return f"enclosure in {st.op_mode} mode"
        if any(x not in SHUTTER_STATES for x in st.shutters):
            return f"unexpected shutter state {st.shutters}"
        return None

    async def open(self) -> None:
        if self.sequence_running:
            return
        st = await self.status()  # at most status_max_age old
        refusal = self._refusal(st)
        if refusal:
            raise AlpacaException(INVALID_OPERATION, f"not opening: {refusal}")
        if st.shutters == ("OPEN", "OPEN"):
            return
        self._failed = None
        first = next(n for n in self.config.open_order if st.shutter(n) != "OPEN")
        if st.shutter(first) != "OPENING":
            await self._command_open(first)
        self._commanded = (OPENING, time.monotonic())
        self._sequence = asyncio.ensure_future(self._open_sequence(first))

    async def _command_open(self, n: int) -> None:
        log.info("opening shutter %d", n)
        try:
            await self.pac.action(f"OPEN_SHUTTER_{n}")
        finally:
            self._invalidate()

    async def _open_sequence(self, first: int) -> None:
        try:
            if self.config.lights_off_before_open:
                # Legacy turned the lights off before opening (aqawan.py:243);
                # here it follows the first open command, so OpenShutter
                # returns quickly. A failure is logged, not fatal (as legacy).
                try:
                    await self.pac.action("LIGHTS_OFF")
                except PACError as exc:
                    log.error("could not turn off the lights: %s", exc)
            for n in self.config.open_order:
                if n != first:
                    st = await self._poll_status()
                    if st.shutter(n) == "OPEN":
                        continue
                    if st.shutter(n) != "OPENING":
                        await self._command_open(n)
                await self._wait_open(n)
            log.info("both shutters open")
        except asyncio.CancelledError:
            log.info("open sequence cancelled")
            raise
        except Exception as exc:
            self._failed = str(exc)
            log.error("open sequence failed: %s", exc)

    async def _poll_status(self) -> PACStatus:
        """A fresh STATUS, retried (bounded by the caller's deadline)."""
        while True:
            try:
                return await self.status(max_age=0)
            except PACError as exc:
                log.warning("STATUS during open sequence failed: %s", exc)
                await asyncio.sleep(self.config.poll_interval)

    async def _wait_open(self, n: int) -> None:
        cfg = self.config
        t0 = time.monotonic()
        async with asyncio.timeout(cfg.shutter_open_timeout):
            while True:
                st = await self._poll_status()
                state = st.shutter(n)
                if st.fault or st.error:
                    raise PACError(f"PAC Fault/Error while opening shutter {n}")
                if state == "OPEN":
                    log.info("shutter %d open after %.1f s", n, time.monotonic() - t0)
                    return
                if state == "CLOSED" and time.monotonic() - t0 < cfg.start_grace:
                    pass  # not started yet
                elif state != "OPENING":
                    raise PACError(f"shutter {n} is {state} while opening")
                await asyncio.sleep(cfg.poll_interval)

    async def _cancel_sequence(self) -> None:
        task, self._sequence = self._sequence, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 1.0)

    async def close(self) -> None:
        await self._cancel_sequence()
        self._failed = None
        try:
            await self.pac.action(self.config.close_command)
        except PACRefused:
            st = await self.status(max_age=0)
            if st.shutters == ("CLOSED", "CLOSED"):
                log.info("%s refused, but both shutters are closed", self.config.close_command)
                return
            raise
        finally:
            self._invalidate()
        self._commanded = (CLOSING, time.monotonic())
        log.info("closing (%s)", self.config.close_command)

    async def abort(self) -> None:
        await self._cancel_sequence()
        try:
            await self.pac.action("STOP")
        finally:
            self._invalidate()
            self._commanded = None
        # What a stopped shutter reports is not known yet: read it back and
        # log it (an unknown string is shutterError, and logged as such).
        try:
            st = await self.status(max_age=0)
        except PACError as exc:
            log.warning("STATUS after STOP failed: %s", exc)
        else:
            log.info("after STOP: Shutter1=%r Shutter2=%r", st.shutter1, st.shutter2)

    async def heartbeat(self) -> str:
        return await self.pac.action("HEARTBEAT")

    async def shutdown(self) -> None:
        await self._cancel_sequence()
        for t in list(self._background):
            t.cancel()


# -- Alpaca over HTTP ------------------------------------------------------------------

COMMON_NAMES = frozenset(
    {
        "connected",
        "connecting",
        "connect",
        "disconnect",
        "description",
        "driverinfo",
        "driverversion",
        "interfaceversion",
        "name",
        "supportedactions",
    }
)
CAN = {
    "canfindhome": False,
    "canpark": False,
    "cansetaltitude": False,
    "cansetazimuth": False,
    "cansetpark": False,
    "cansetshutter": True,
    "canslave": False,
    "cansyncazimuth": False,
}
NOT_IMPLEMENTED_GET = frozenset({"altitude", "azimuth"})
NOT_IMPLEMENTED_PUT = frozenset(
    {
        "findhome",
        "park",
        "setpark",
        "slewtoaltitude",
        "slewtoazimuth",
        "synctoazimuth",
        "commandblind",
        "commandbool",
        "commandstring",
    }
)
ACTIONS = ("Heartbeat", "Status", "Faults", "GetFaults", "GetErrors", "ClearFaults")
UINT32_MAX = 2**32 - 1


def _alpaca(value: Any = None, *, ctid: int = 0, stid: int = 0, err: int = 0, msg: str = ""):
    body: dict[str, Any] = {
        "ClientTransactionID": ctid,
        "ServerTransactionID": stid,
        "ErrorNumber": err,
        "ErrorMessage": msg,
    }
    if value is not None:
        body["Value"] = value
    return body


class _BadRequest(Exception):
    pass


def _uint32(text: str | None) -> int:
    try:
        n = int(text or 0)
    except ValueError:
        return 0
    return n if 0 <= n <= UINT32_MAX else 0


def _bool_param(params: dict[str, str], name: str) -> bool:
    if name not in params:
        raise _BadRequest(f"missing parameter {name}")
    v = params[name].strip().lower()
    if v not in ("true", "false"):
        raise _BadRequest(f"{name}={params[name]!r} is not a boolean")
    return v == "true"


class _Handler(BaseHTTPRequestHandler):
    server: AqawanServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
        data = (json.dumps(body) if ctype == "application/json" else str(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._dispatch()

    def do_PUT(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query, keep_blank_values=True)
        if self.command == "GET":
            # GET parameter names are case-insensitive.
            params = {k.lower(): v[0] for k, v in query.items()}
        else:
            # PUT form parameter names are case-sensitive (Alpaca spec).
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            params = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        parts = [p for p in url.path.split("/") if p]
        lower = [p.lower() for p in parts]
        srv = self.server
        if lower[:1] == ["management"] and self.command == "GET":
            ctid = _uint32(params.get("clienttransactionid"))
            value = srv.management(lower[1:])
            if value is None:
                self._send(400, f"unknown management path {url.path}", "text/plain")
            else:
                self._send(200, _alpaca(value, ctid=ctid, stid=srv.next_stid()))
            return
        if lower[:1] == ["setup"]:
            self._send(200, srv.setup_page(), "text/html")
            return
        # Alpaca URL elements are lower case; anything else is a bad request.
        if parts != lower or len(lower) != 5 or lower[:3] != ["api", "v1", "dome"]:
            self._send(400, f"unknown path {url.path}", "text/plain")
            return
        if lower[3] != "0":
            self._send(400, f"no dome device number {parts[3]}", "text/plain")
            return
        member = lower[4]
        get = (lambda k: params.get(k.lower())) if self.command == "GET" else params.get
        ctid = _uint32(get("ClientTransactionID"))
        client = _uint32(get("ClientID"))
        stid = srv.next_stid()
        try:
            value = srv.member(self.command, member, params, client)
        except _BadRequest as exc:
            self._send(400, str(exc), "text/plain")
            return
        except AlpacaException as exc:
            self._send(200, _alpaca(ctid=ctid, stid=stid, err=exc.number, msg=str(exc)))
            return
        except Exception as exc:  # never a traceback to the client
            log.exception("%s %s", self.command, member)
            self._send(200, _alpaca(ctid=ctid, stid=stid, err=DRIVER_ERROR, msg=repr(exc)))
            return
        self._send(200, _alpaca(value, ctid=ctid, stid=stid))


class AqawanServer(ThreadingHTTPServer):
    """The Alpaca server. The PAC client and enclosure logic live on a
    private event loop thread; HTTP requests (threads) submit to it."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: AqawanConfig, *, hardware: bool = False) -> None:
        allow = config.allow_hardware and hardware
        if config.allow_hardware != hardware:
            log.info(
                "hardware use needs allow_hardware in the config AND --hardware;"
                " only a loopback PAC is allowed now"
            )
        self.pac_ip = check_pac_address(config.pac.host, allow_hardware=allow)
        if allow:
            log.warning("HARDWARE MODE: PAC at %s:%d", self.pac_ip, config.pac.port)
        host, _, port = config.listen.rpartition(":")
        super().__init__((host, int(port)), _Handler)
        self.config = config
        self._allow = allow
        self._stid = 0
        self._stid_lock = threading.Lock()
        self._clients: set[int] = set()
        self._clients_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, name="aqawan-loop", daemon=True
        )
        self._http_thread: threading.Thread | None = None
        self._discovery: _Discovery | None = None
        self.aqawan: Aqawan = self._run_sync(self._build())

    async def _build(self) -> Aqawan:
        p = self.config.pac
        pac = PACClient(
            self.pac_ip,
            p.port,
            allow_hardware=self._allow,
            connect_timeout=p.connect_timeout,
            command_timeout=p.command_timeout,
            resend_after=p.resend_after,
            max_attempts=p.max_attempts,
            idle_end=p.idle_end,
        )
        return Aqawan(pac, self.config)

    def _run_sync(self, coro: Awaitable[Any]) -> Any:
        if not self._loop_thread.is_alive():
            self._loop_thread.start()
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(10)  # type: ignore[arg-type]

    @property
    def address(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    def next_stid(self) -> int:
        with self._stid_lock:
            self._stid = self._stid % UINT32_MAX + 1
            return self._stid

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)

    # -- calls onto the loop ---------------------------------------------------

    def _call(self, fn: Callable[[], Awaitable[Any]], what: str) -> Any:
        fut = asyncio.run_coroutine_threadsafe(fn(), self._loop)  # type: ignore[arg-type]
        try:
            return fut.result(self.config.request_timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise AlpacaException(
                PAC_TIMEOUT, f"{what}: no answer within {self.config.request_timeout} s"
            ) from None
        except AlpacaException:
            raise
        except PACRefused as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(PAC_REFUSED, str(exc)) from None
        except PACTimeout as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(PAC_TIMEOUT, str(exc)) from None
        except PACError as exc:
            log.error("%s: %s", what, exc)
            raise AlpacaException(DRIVER_ERROR, str(exc)) from None
        except concurrent.futures.CancelledError:
            raise AlpacaException(DRIVER_ERROR, f"{what}: cancelled") from None

    # -- management ---------------------------------------------------------------

    def management(self, tail: list[str]) -> Any:
        if tail == ["apiversions"]:
            return [1]
        if tail == ["v1", "description"]:
            return {
                "ServerName": f"robofast Aqawan ({self.config.name})",
                "Manufacturer": "robofast",
                "ManufacturerVersion": DRIVER_VERSION,
                "Location": self.config.name,
            }
        if tail == ["v1", "configureddevices"]:
            return [
                {
                    "DeviceName": f"{self.config.name} enclosure",
                    "DeviceType": "Dome",
                    "DeviceNumber": 0,
                    "UniqueID": f"robofast-aqawan-{self.config.name}-dome-0",
                }
            ]
        return None

    def setup_page(self) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>Aqawan</title></head>"
            f"<body><h1>robofast Aqawan: {self.config.name}</h1>"
            "<p>Configured from its JSON file; nothing to set here.</p></body></html>"
        )

    # -- device members ------------------------------------------------------------

    def connected(self, client: int) -> bool:
        with self._clients_lock:
            return client in self._clients

    def _set_connected(self, client: int, value: bool) -> None:
        if value:
            if not self.connected(client):
                self._call(lambda: self.aqawan.status(max_age=0), "Connect")
                with self._clients_lock:
                    self._clients.add(client)
        else:
            with self._clients_lock:
                self._clients.discard(client)

    def member(self, verb: str, member: str, params: dict[str, str], client: int) -> Any:
        cfg = self.config
        if verb == "PUT":
            if member == "connected":
                self._set_connected(client, _bool_param(params, "Connected"))
                return None
            if member in ("connect", "disconnect"):
                self._set_connected(client, member == "connect")
                return None
        elif member in COMMON_NAMES and member not in ("connect", "disconnect"):
            return {
                "connected": self.connected(client),
                "connecting": False,
                "description": f"Aqawan roll-off enclosure {cfg.name} (two shutters, PAC)",
                "driverinfo": "robofast_core.devices.aqawan_server: Alpaca Dome for the Aqawan PAC",
                "driverversion": DRIVER_VERSION,
                "interfaceversion": 3,
                "name": f"robofast Aqawan {cfg.name}",
                "supportedactions": list(ACTIONS),
            }[member]
        if member in COMMON_NAMES:
            raise _BadRequest(f"{verb} {member} is not allowed")
        if verb == "GET" and member in CAN:
            return CAN[member]
        if member in ("connect", "disconnect", "action") and verb == "GET":
            raise _BadRequest(f"{member} is a PUT method")

        if not self.connected(client):
            raise AlpacaException(NOT_CONNECTED, f"{member}: not connected")
        a = self.aqawan
        if verb == "GET":
            if member == "shutterstatus":
                return self._call(a.shutter_status, "ShutterStatus")
            if member in ("athome", "atpark", "slaved"):
                return False
            if member == "slewing":
                # IDomeV3: true while any part moves, the shutter included.
                return self._call(a.shutter_status, "Slewing") in (OPENING, CLOSING)
            if member == "devicestate":
                return self._device_state()
            if member in NOT_IMPLEMENTED_GET:
                raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
            return self._unknown(verb, member)
        # PUT
        if member == "openshutter":
            return self._call(a.open, "OpenShutter")
        if member == "closeshutter":
            return self._call(a.close, "CloseShutter")
        if member == "abortslew":
            return self._call(a.abort, "AbortSlew")
        if member == "slaved":
            if _bool_param(params, "Slaved"):
                raise AlpacaException(NOT_IMPLEMENTED, "Slaved: this enclosure cannot slave")
            return None
        if member == "action":
            return self._action(params.get("Action", ""), params.get("Parameters", ""), client)
        if member in NOT_IMPLEMENTED_PUT:
            raise AlpacaException(NOT_IMPLEMENTED, f"{member} is not implemented")
        return self._unknown(verb, member)

    @staticmethod
    def _unknown(verb: str, member: str) -> Any:
        raise _BadRequest(f"{verb} {member}: no such member")

    def _action(self, name: str, parameters: str, client: int) -> str:
        a = self.aqawan
        key = name.lower()
        if key == "heartbeat":
            return self._call(a.heartbeat, "Heartbeat")
        if key == "status":
            st = self._call(lambda: a.status(max_age=0), "Status")
            return json.dumps(st.as_dict())
        if key == "faults":
            return json.dumps(self._call(a.faults, "Faults"))
        if key == "clearfaults":
            note = f"ClientID {client}: {parameters.strip() or 'no reason given'}"
            return self._call(lambda: a.clear_faults(note), "ClearFaults")
        if key in ("getfaults", "geterrors"):
            command = "GET_FAULTS" if key == "getfaults" else "GET_ERRORS"
            return self._call(lambda: a.pac.command(command), name)
        raise AlpacaException(ACTION_NOT_IMPLEMENTED, f"action {name!r} is not supported")

    def _device_state(self) -> list[dict[str, Any]]:
        state: list[dict[str, Any]] = [
            {"Name": "AtHome", "Value": False},
            {"Name": "AtPark", "Value": False},
        ]
        try:
            shutter = self._call(self.aqawan.shutter_status, "ShutterStatus")
            state.append({"Name": "ShutterStatus", "Value": shutter})
            state.append({"Name": "Slewing", "Value": shutter in (OPENING, CLOSING)})
        except AlpacaException as exc:
            log.warning("DeviceState without ShutterStatus: %s", exc)
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
        state.append({"Name": "TimeStamp", "Value": stamp})
        return state

    # -- lifecycle --------------------------------------------------------------------

    def start(self) -> AqawanServer:
        self._http_thread = threading.Thread(
            target=self.serve_forever, name="aqawan-http", daemon=True
        )
        self._http_thread.start()
        if self.config.discovery_port is not None:
            host = self.server_address[0]
            self._discovery = _Discovery(host, self.config.discovery_port, self.server_address[1])
            self._discovery.start()
        return self

    def stop(self) -> None:
        if self._discovery is not None:
            self._discovery.stop()
        self.shutdown()
        self.server_close()
        if self._http_thread is not None:
            self._http_thread.join(timeout=5)
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self.aqawan.shutdown(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)


class _Discovery:
    """Alpaca UDP discovery: answer ``alpacadiscovery1`` with our HTTP port."""

    def __init__(self, host: str, port: int, http_port: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.reply = json.dumps({"AlpacaPort": http_port}).encode()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aqawan-discovery", daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, peer = self.sock.recvfrom(1024)
            except TimeoutError:
                continue
            except OSError:
                return
            if data.startswith(b"alpacadiscovery1"):
                with contextlib.suppress(OSError):
                    self.sock.sendto(self.reply, peer)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True, help="JSON config file (see module doc)")
    ap.add_argument(
        "--hardware",
        action="store_true",
        help="allow a non-loopback PAC (the config must also say allow_hardware)",
    )
    ap.add_argument("--listen", help="host:port, overriding the config")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(args.config) as f:
        config = AqawanConfig.from_dict(json.load(f))
    if args.listen:
        config.listen = args.listen
    try:
        server = AqawanServer(config, hardware=args.hardware)
    except HardwareNotAllowed as exc:
        log.error("%s", exc)
        sys.exit(2)
    server.start()
    print(
        json.dumps(
            {"url": f"http://{server.address}", "pac": f"{server.pac_ip}:{config.pac.port}"}
        ),
        flush=True,
    )
    stop = threading.Event()
    import signal

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.stop()


if __name__ == "__main__":
    main()
