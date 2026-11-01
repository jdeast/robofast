"""Client for the Aqawan enclosure's PAC controller (telnet-style line protocol).

What the protocol is, as far as the legacy code shows
-----------------------------------------------------
Everything here comes from the public legacy driver,
``MinervaCollaboration/minerva-control``, file ``minerva_library/aqawan.py``
(cited below as ``aqawan.py:N``), and its callers. There is no PAC manual in
either repository, so anything not in that code is marked *assumed*.

* TCP to the PAC (legacy port 22004, ``config/aqawan_N.ini``), one connection
  per command, closed after the reply (``aqawan.py:110-130``). Commands are
  serialized by a lock (``aqawan.py:106``).
* Telnet framing: the legacy client used ``telnetlib``, which refuses every
  telnet option the server offers, then writes ``vt100\\r\\n`` as plain text
  (``aqawan.py:116``), presumably answering a terminal-type prompt.
* Then the command plus ``\\r\\n``. The legacy loop re-sends the command
  every 0.5 s until *any* bytes come back, unboundedly (``aqawan.py:120-124``,
  commented "repeatedly?! why is this necessary?"). Its terminator is spelled
  ``b"/r/n/r/n#>"`` -- forward slashes -- so it never matches, and every read
  simply returns whatever arrived within 0.5 s. Two consequences: (1) a
  command the PAC does not answer within 0.5 s is sent *again*; (2) a reply
  longer than 0.5 s to arrive is truncated, which is a likely cause of the
  legacy "Required key ... not present; trying again" retries (and their
  unbounded recursion, ``aqawan.py:152-155``). The intended prompt was
  presumably ``\\r\\n\\r\\n#>``. *Assumed*: the PAC drops a command that
  arrives too soon after the terminal-type line; that would explain why the
  loop was always needed, and the simulator models it.
* Replies to action commands contain ``Success=TRUE`` or ``Success=FALSE,
  <reason>`` (``aqawan.py:207,282``); reasons seen in the legacy log filters
  (``control.py``) are ``Estop active``, ``Heartbeat timer expired`` and
  ``Unknown command``; ``aqawan.py:210`` mentions ``Enclosure not in AUTO``.
* ``STATUS`` returns comma-separated ``Key=Value`` pairs (``aqawan.py:139-150``).

This client
-----------
* Only the legacy command set (:data:`COMMANDS`) can be sent.
* Every exchange has a hard deadline (``command_timeout``) covering connect,
  negotiation, re-sends and the reply. Re-sends are bounded
  (``max_attempts``), and happen only while *nothing* has come back, on the
  same connection. Legacy re-sent "because it was necessary sometimes and
  didn't seem to hurt" (JDE, 2026-10-31). Why a re-send is safe: every
  command the server sends names an absolute target (``OPEN_SHUTTER_n``,
  the closes, ``STOP``, ``LIGHTS_OFF``), is a query, or restarts a timer
  (``HEARTBEAT``), so a duplicate asks for what is already happening, and
  the PAC presumably ignores it (*assumed*; the simulator does). The one
  hazard is a duplicate ``OPEN_SHUTTER_n`` that a slow PAC executes *after*
  a later close. That is no worse than any other unexpected motion: the
  server stops reporting "closing" as soon as the PAC has acknowledged the
  close, so the reopening shows up, and the supervisor re-closes on its
  next poll. :data:`NO_RESEND` commands (``RESET_PAC``, which is not
  idempotent) are sent exactly once.
* A reply ends at the ``#>`` prompt, or, since the prompt is not confirmed,
  after ``idle_end`` seconds of silence once some text has arrived. Bare
  prompts and echoed command lines are skipped.
* "Urgent" commands (closing, STOP) pre-empt a routine exchange in flight, so
  a close never waits behind a slow ``STATUS``.
* :func:`check_pac_address` refuses any non-loopback PAC unless hardware use
  is explicitly allowed (the server requires a config flag *and* a CLI flag).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field

from robofast_core.cancel import stop_task
from robofast_core.owner import DeviceError, DeviceTimeout

log = logging.getLogger(__name__)

#: The legacy command set (``aqawan.py:84-96``); nothing else is ever sent.
COMMANDS = frozenset(
    {
        "HEARTBEAT",
        "STOP",
        "OPEN_SHUTTERS",
        "CLOSE_SHUTTERS",
        "CLOSE_SEQUENTIAL",
        "OPEN_SHUTTER_1",
        "CLOSE_SHUTTER_1",
        "OPEN_SHUTTER_2",
        "CLOSE_SHUTTER_2",
        "LIGHTS_ON",
        "LIGHTS_OFF",
        "ENC_FANS_HI",
        "ENC_FANS_MED",
        "ENC_FANS_LOW",
        "ENC_FANS_OFF",
        "PANEL_LED_GREEN",
        "PANEL_LED_YELLOW",
        "PANEL_LED_RED",
        "PANEL_LED_OFF",
        "DOOR_LED_GREEN",
        "DOOR_LED_YELLOW",
        "DOOR_LED_RED",
        "DOOR_LED_OFF",
        "SON_ALERT_ON",
        "SON_ALERT_OFF",
        "LED_STEADY",
        "LED_BLINK",
        "MCB_RESET_POLE_FANS",
        "MCB_RESET_TAIL_FANS",
        "MCB_RESET_OTA_BLOWER",
        "MCB_RESET_PANEL_FANS",
        "MCB_TRIP_POLE_FANS",
        "MCB_TRIP_TAIL_FANS",
        "MCB_TRIP_PANEL_FANS",
        "STATUS",
        "GET_ERRORS",
        "GET_FAULTS",
        "CLEAR_ERRORS",
        "CLEAR_FAULTS",
        "RESET_PAC",
    }
)

#: Commands that pre-empt a routine exchange in flight.
URGENT = frozenset(
    {"STOP", "CLOSE_SHUTTERS", "CLOSE_SEQUENTIAL", "CLOSE_SHUTTER_1", "CLOSE_SHUTTER_2"}
)

#: Commands never re-sent: repeating them is not harmless.
NO_RESEND = frozenset({"RESET_PAC"})

#: Keys the legacy code requires in a STATUS reply (``aqawan.py:147-150``).
REQUIRED_KEYS = (
    "Shutter1",
    "Shutter2",
    "SWVersion",
    "EnclHumidity",
    "EntryDoor1",
    "EntryDoor2",
    "PanelDoor",
    "Heartbeat",
    "SystemUpTime",
    "Fault",
    "Error",
    "PanelExhaustTemp",
    "EnclTemp",
    "EnclExhaustTemp",
    "EnclIntakeTemp",
    "LightsOn",
)

#: Shutter states the legacy code compares against (``aqawan.py:199,221,270``,
#: plus ``CLOSING``, which it implies). Anything else is unexpected.
SHUTTER_STATES = frozenset({"OPEN", "CLOSED", "OPENING", "CLOSING"})

PROMPT = b"#>"
TERMINAL_TYPE = b"vt100"


# -- errors -------------------------------------------------------------------


class PACError(DeviceError):
    """Talking to the PAC failed."""


class PACTimeout(PACError, DeviceTimeout):
    """No complete reply within the command's deadline."""


class PACProtocolError(PACError):
    """A reply that cannot be understood (garbled, truncated, unexpected)."""


class PACRefused(PACError):
    """The PAC answered ``Success=FALSE``; ``reason`` is its text."""

    def __init__(self, command: str, reason: str) -> None:
        super().__init__(f"PAC refused {command}: {reason or '(no reason given)'}")
        self.command = command
        self.reason = reason


class PACPreempted(PACError):
    """An urgent command (a close or STOP) cut this exchange short."""


class HardwareNotAllowed(Exception):
    """A non-loopback PAC address without explicit permission."""


# -- addresses ------------------------------------------------------------------


def check_pac_address(host: str, *, allow_hardware: bool) -> str:
    """Resolve ``host`` once and return the IP literal to connect to.

    Unless ``allow_hardware``, every address ``host`` resolves to must be
    loopback; otherwise :class:`HardwareNotAllowed`. The caller connects to
    the returned literal, so a later DNS change cannot redirect it.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HardwareNotAllowed(f"cannot resolve PAC host {host!r}: {exc}") from exc
    addrs = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    if not allow_hardware and not all(a.is_loopback for a in addrs):
        raise HardwareNotAllowed(
            f"PAC host {host!r} ({', '.join(map(str, addrs))}) is not loopback: refusing to"
            " talk to real hardware without allow_hardware in the config AND --hardware"
        )
    return str(addrs[0])


# -- reply parsing --------------------------------------------------------------

_IAC, _DONT, _DO, _WONT, _WILL, _SB, _SE = 255, 254, 253, 252, 251, 250, 240


class TelnetFilter:
    """Strip telnet commands from a byte stream; collect our refusals.

    Like ``telnetlib``'s defaults: every ``DO x`` is answered ``WONT x`` and
    every ``WILL x`` ``DONT x``; subnegotiations are dropped.
    """

    def __init__(self) -> None:
        self._state = "data"
        self.replies = bytearray()

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        for b in data:
            st = self._state
            if st == "data":
                if b == _IAC:
                    self._state = "iac"
                else:
                    out.append(b)
            elif st == "iac":
                if b == _IAC:
                    out.append(b)
                    self._state = "data"
                elif b in (_DO, _DONT, _WILL, _WONT):
                    self._state = {_DO: "do", _DONT: "dont", _WILL: "will", _WONT: "wont"}[b]
                elif b == _SB:
                    self._state = "sb"
                else:
                    self._state = "data"
            elif st in ("do", "will"):
                self.replies += bytes([_IAC, _WONT if st == "do" else _DONT, b])
                self._state = "data"
            elif st in ("dont", "wont"):
                self._state = "data"
            elif st == "sb":
                if b == _IAC:
                    self._state = "sb_iac"
            elif st == "sb_iac":
                self._state = "data" if b == _SE else "sb"
        return bytes(out)


def _clean(segment: str, command: str) -> str:
    lines = [ln.strip() for ln in segment.replace("\r", "\n").split("\n")]
    drop = (command, TERMINAL_TYPE.decode())
    return "\n".join(ln for ln in lines if ln and ln not in drop)


def split_reply(text: str, command: str) -> tuple[str | None, bool]:
    """The reply in ``text`` (telnet-filtered, decoded) and whether it is complete.

    Segments end at the ``#>`` prompt; echoed command and terminal-type lines
    are dropped, and the first non-empty segment is the reply. A reply
    followed by a prompt is complete; one without (the trailing segment) is
    not yet, and the caller decides by silence.
    """
    *terminated, trailing = text.split(PROMPT.decode())
    for seg in terminated:
        reply = _clean(seg, command)
        if reply:
            return reply, True
    reply = _clean(trailing, command)
    return (reply or None), False


_SUCCESS = re.compile(r"Success\s*=\s*(TRUE|FALSE)\s*,?\s*(.*)", re.IGNORECASE | re.DOTALL)


def check_success(command: str, reply: str) -> str:
    """Raise :class:`PACRefused` on ``Success=FALSE``; return the reply.

    A reply with no ``Success=`` at all is a :class:`PACProtocolError`: the
    outcome is unknown.
    """
    m = _SUCCESS.search(reply)
    if m is None:
        raise PACProtocolError(f"{command}: no Success= in reply {reply[:200]!r}")
    if m.group(1).upper() != "TRUE":
        raise PACRefused(command, m.group(2).strip())
    return reply


def _bool(key: str, value: str) -> bool:
    v = value.strip().upper()
    if v in ("TRUE", "1", "YES", "ON"):
        return True
    if v in ("FALSE", "0", "NO", "OFF"):
        return False
    raise PACProtocolError(f"STATUS {key}={value!r} is not a boolean")


def _float(key: str, value: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise PACProtocolError(f"STATUS {key}={value!r} is not a number") from None


@dataclass(frozen=True)
class PACStatus:
    """A parsed ``STATUS`` reply. ``raw`` keeps every key, typed or not."""

    shutter1: str
    shutter2: str
    fault: bool
    error: bool
    heartbeat: float
    lights_on: bool
    entry_door1: str
    entry_door2: str
    panel_door: str
    sw_version: str
    uptime: float
    humidity: float
    temperatures: dict[str, float]
    op_mode: str | None
    raw: dict[str, str] = field(repr=False)

    def shutter(self, n: int) -> str:
        return self.shutter1 if n == 1 else self.shutter2

    @property
    def shutters(self) -> tuple[str, str]:
        return (self.shutter1, self.shutter2)

    def as_dict(self) -> dict[str, object]:
        return {
            "shutter1": self.shutter1,
            "shutter2": self.shutter2,
            "fault": self.fault,
            "error": self.error,
            "heartbeat": self.heartbeat,
            "lights_on": self.lights_on,
            "entry_door1": self.entry_door1,
            "entry_door2": self.entry_door2,
            "panel_door": self.panel_door,
            "op_mode": self.op_mode,
            "sw_version": self.sw_version,
            "uptime": self.uptime,
            "humidity": self.humidity,
            "temperatures": dict(self.temperatures),
            "raw": dict(self.raw),
        }


def parse_status(reply: str) -> PACStatus:
    """Parse a STATUS reply; :class:`PACProtocolError` if anything is missing."""
    raw: dict[str, str] = {}
    for entry in reply.replace("\n", ",").split(","):
        if "=" in entry:
            key, value = entry.split("=", 1)
            raw[key.strip()] = value.strip()
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        raise PACProtocolError(f"STATUS reply missing {missing}: {reply[:200]!r}")
    temps = {
        k: _float(k, raw[k])
        for k in ("PanelExhaustTemp", "EnclTemp", "EnclExhaustTemp", "EnclIntakeTemp")
    }
    return PACStatus(
        shutter1=raw["Shutter1"].upper(),
        shutter2=raw["Shutter2"].upper(),
        fault=_bool("Fault", raw["Fault"]),
        error=_bool("Error", raw["Error"]),
        heartbeat=_float("Heartbeat", raw["Heartbeat"]),
        lights_on=_bool("LightsOn", raw["LightsOn"]),
        entry_door1=raw["EntryDoor1"].upper(),
        entry_door2=raw["EntryDoor2"].upper(),
        panel_door=raw["PanelDoor"].upper(),
        sw_version=raw["SWVersion"],
        uptime=_float("SystemUpTime", raw["SystemUpTime"]),
        humidity=_float("EnclHumidity", raw["EnclHumidity"]),
        temperatures=temps,
        op_mode=raw.get("EnclOpMode", "").upper() or None,
        raw=raw,
    )


# -- client ---------------------------------------------------------------------


class PACClient:
    """One PAC at ``host:port``. Use from one event loop.

    ``host`` must already have passed :func:`check_pac_address` (the server
    does that); the constructor re-checks that it is an IP literal and,
    unless ``allow_hardware``, loopback.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        allow_hardware: bool = False,
        connect_timeout: float = 2.0,
        command_timeout: float = 4.0,
        resend_after: float = 0.5,
        max_attempts: int = 3,
        idle_end: float = 0.3,
    ) -> None:
        addr = ipaddress.ip_address(host)  # ValueError for a hostname
        if not allow_hardware and not addr.is_loopback:
            raise HardwareNotAllowed(f"PAC {host} is not loopback")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.resend_after = resend_after
        self.max_attempts = max_attempts
        self.idle_end = idle_end
        self._lock = asyncio.Lock()
        self._inflight: tuple[str, asyncio.Task] | None = None
        #: Exchanges that needed a re-send, and exchanges done (diagnostics).
        self.resends = 0
        self.exchanges = 0

    async def command(self, command: str, *, timeout: float | None = None) -> str:
        """Send ``command``; return the reply text (not checked for success)."""
        if command not in COMMANDS:
            raise ValueError(f"{command!r} is not an allowed PAC command")
        timeout = self.command_timeout if timeout is None else timeout
        urgent = command in URGENT
        if urgent and self._inflight is not None:
            other, task = self._inflight
            if other not in URGENT and not task.done():
                log.info("%s pre-empts %s", command, other)
                task.cancel()
        t0 = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                async with self._lock:
                    inner = asyncio.ensure_future(self._exchange(command))
                    self._inflight = (command, inner)
                    try:
                        return await asyncio.shield(inner)
                    except asyncio.CancelledError:
                        inner.cancel()
                        me = asyncio.current_task()
                        if inner.cancelled() and me is not None and not me.cancelling():
                            raise PACPreempted(f"{command}: pre-empted") from None
                        raise
                    finally:
                        try:
                            if not inner.done():
                                # Bounded, passing our own cancellation on.
                                await stop_task(inner, self.connect_timeout)
                        finally:
                            self._inflight = None
        except TimeoutError:
            raise PACTimeout(
                f"PAC {command}: no complete reply within {time.monotonic() - t0:.1f} s"
            ) from None

    async def _exchange(self, command: str) -> str:
        try:
            async with asyncio.timeout(self.connect_timeout):
                reader, writer = await asyncio.open_connection(self.host, self.port)
        except TimeoutError:
            raise PACTimeout(f"PAC {command}: connect timed out") from None
        except OSError as exc:
            raise PACError(f"PAC {command}: cannot connect: {exc}") from exc
        self.exchanges += 1
        telnet = TelnetFilter()
        text = b""
        try:
            writer.write(TERMINAL_TYPE + b"\r\n")
            line = command.encode() + b"\r\n"
            attempts = 0
            max_attempts = 1 if command in NO_RESEND else self.max_attempts
            sent_at = 0.0
            last_rx = time.monotonic()
            while True:
                now = time.monotonic()
                reply, complete = split_reply(text.decode("ascii", "replace"), command)
                if reply is not None and (complete or now - last_rx >= self.idle_end):
                    return reply
                if reply is None and (attempts == 0 or now - sent_at >= self.resend_after):
                    if attempts >= max_attempts:
                        raise PACTimeout(f"PAC {command}: no reply after {attempts} attempts")
                    if attempts:
                        self.resends += 1
                        log.debug("PAC %s: no reply in %.1f s; re-sending", command, now - sent_at)
                    writer.write(line)
                    attempts += 1
                    sent_at = now
                if telnet.replies:
                    writer.write(bytes(telnet.replies))
                    telnet.replies.clear()
                await writer.drain()
                try:
                    async with asyncio.timeout(0.05):
                        chunk = await reader.read(4096)
                except TimeoutError:
                    continue
                if not chunk:  # the PAC hung up
                    reply, _ = split_reply(text.decode("ascii", "replace"), command)
                    if reply is not None:
                        return reply
                    raise PACProtocolError(f"PAC {command}: connection closed without a reply")
                text += telnet.feed(chunk)
                last_rx = time.monotonic()
        except OSError as exc:  # reset or broken pipe mid-exchange
            raise PACError(f"PAC {command}: connection lost: {exc!r}") from exc
        finally:
            writer.close()
            try:
                async with asyncio.timeout(0.5):
                    await writer.wait_closed()
            except (TimeoutError, OSError):
                pass

    # -- typed helpers ---------------------------------------------------------

    async def status(self, *, timeout: float | None = None) -> PACStatus:
        return parse_status(await self.command("STATUS", timeout=timeout))

    async def action(self, command: str, *, timeout: float | None = None) -> str:
        """Send an action command; raise :class:`PACRefused` on ``Success=FALSE``."""
        return check_success(command, await self.command(command, timeout=timeout))
