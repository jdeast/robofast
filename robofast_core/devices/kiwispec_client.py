"""Protocol clients for the KiwiSpec spectrograph PC (the legacy servers).

KiwiSpec is driven through **two** TCP services on the spectrograph PC, both
described in ``docs/kiwispec.md`` (from MinervaCollaboration/minerva-control,
``minerva_library/spectrograph.py`` and ``spectrograph_server.py``):

* :class:`SpecServerClient` -- the legacy ``spectrograph_server.py`` (default
  port 44445): one ASCII command per TCP connection, ``"<command> <arg>"``
  (``arg`` is ``None`` when there is none), one reply (``success ...`` or
  ``fail``), then the server closes. It drives the iodine stage, the iodine
  cell heater, the slit-flat LED, the FAU backlight, the exposure meter's
  running total, the pressure gauges and the raw-file bookkeeping.
* :class:`SICameraClient` -- the vendor "SI Image SGL E" camera server
  (default port 2055): binary packets (big-endian), the subset the legacy
  ``si/`` package used (``SetAcquisitionMode``, ``SetAcquisitionType``,
  ``SetExposureTime``, ``Acquire``, ``TerminateAcquisition``,
  ``GetStatusFromCamera``, ``SetReadoutMode``, ``SetCCDFormatParameters``,
  ``SetCooler``).

Unlike the legacy client, every exchange here has a hard bound (connect,
send, reply), the command set is an allowlist (:data:`COMMANDS`; commands the
legacy server dispatches to methods it does not have, and which would kill
its listener, are not in it), replies are parsed into types, only read-only
commands are retried (a bounded number of times, in a loop: no recursion),
and the wire lock is always released (``async with``).

Nothing here connects anywhere but loopback unless the caller passes
``allow_hardware=True`` (see :func:`check_device_address`).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: Legacy defaults (``config/spectrograph.ini``: SERVER_PORT, CAMERA_PORT).
SPEC_PORT = 44445
CAMERA_PORT = 2055

#: The legacy server reads each command with one ``recv(1024)``.
MAX_MESSAGE = 1024
#: ``write_header`` chunk size (legacy ``spectrograph.write_header``).
HEADER_CHUNK = 800
#: Iodine stage travel the server accepts (``i2stage_movef``: 0-150 mm).
STAGE_MIN_MM, STAGE_MAX_MM = 0.0, 150.0


class KiwiSpecError(Exception):
    """The spectrograph PC (or the transport to it) reported an error."""


class KiwiSpecTimeout(KiwiSpecError):
    """An exchange did not complete within its bound."""


class KiwiSpecProtocolError(KiwiSpecError):
    """A reply that is not what the protocol allows (garbled, truncated)."""


class KiwiSpecUnreachable(KiwiSpecError):
    """Could not connect (refused or timed out): nothing was sent, so the
    command may safely be tried again."""


class KiwiSpecFailed(KiwiSpecError):
    """The server answered ``fail``."""


class KiwiSpecRefused(KiwiSpecError):
    """Refused before anything was sent (not allowlisted, bad argument)."""


class HardwareNotAllowed(KiwiSpecError):
    """A non-loopback address without hardware permission."""


def check_device_address(host: str, *, allow_hardware: bool, what: str = "KiwiSpec") -> str:
    """Resolve ``host`` once; return the IP literal to connect to.

    Unless ``allow_hardware``, every address it resolves to must be loopback
    (a simulator); otherwise :class:`HardwareNotAllowed`. Callers connect to
    the returned literal, so a later DNS change cannot redirect them.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HardwareNotAllowed(f"cannot resolve {what} host {host!r}: {exc}") from exc
    addrs = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    if not allow_hardware and not all(a.is_loopback for a in addrs):
        raise HardwareNotAllowed(
            f"{what} host {host!r} ({', '.join(map(str, addrs))}) is not loopback: refusing to"
            " talk to real hardware without allow_hardware in the config AND --hardware"
        )
    return str(addrs[0])


# -- the spectrograph server (ASCII) ------------------------------------------------

#: What a command does to the hardware (docs/kiwispec.md, "Hardware actions").
READ, STATE, MOVES, POWERS, FILES = "read", "state", "moves", "powers", "files"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    #: Seconds to wait for the reply once the command is sent.
    reply_timeout: float
    #: What it does: READ (nothing), STATE (software/heater state), MOVES,
    #: POWERS (switches power), FILES (moves/edits files on the PC).
    action: str
    #: The argument: "none", "float", "position", "filename", "text".
    arg: str = "none"

    @property
    def retry_safe(self) -> bool:
        return self.action == READ


#: The allowlist. Reply timeouts are the legacy client's, except where the
#: server is known to block longer (noted).
COMMANDS: dict[str, CommandSpec] = {
    c.name: c
    for c in (
        CommandSpec("cell_heater_temp", 10, READ),
        CommandSpec("cell_heater_get_set_temp", 10, READ),
        CommandSpec("get_spec_pressure", 5, READ),
        CommandSpec("get_pump_pressure", 5, READ),
        CommandSpec("i2stage_get_pos", 10, READ),
        CommandSpec("get_expmeter_total", 10, READ),
        CommandSpec("get_index", 5, READ),
        CommandSpec("cell_heater_on", 10, POWERS),
        CommandSpec("cell_heater_off", 10, POWERS),
        CommandSpec("cell_heater_set_temp", 10, STATE, "float"),
        CommandSpec("reset_expmeter_total", 10, STATE),
        # stop waits for the exposure meter's high voltage to go off.
        CommandSpec("stop_log_expmeter", 30, POWERS),
        CommandSpec("start_log_expmeter", 10, POWERS),
        # The move starts on a server thread; the reply does not wait for it.
        CommandSpec("i2stage_move", 10, MOVES, "position"),
        CommandSpec("i2stage_movef", 10, MOVES, "float"),
        # Blocks for the whole homing move (legacy re-sent it for up to 60 s).
        CommandSpec("i2stage_home", 90, MOVES),
        # Power-cycles the stage (10 s off, 10 s on), homes, moves out then in.
        # Legacy waited 30 s, which is shorter than the server's own sleeps.
        CommandSpec("i2stage_connect", 180, POWERS),
        CommandSpec("i2stage_disconnect", 10, STATE),
        # LED on a PDU outlet (slit flat); also logs the lamp's on-time.
        CommandSpec("led_turn_on", 10, POWERS),
        CommandSpec("led_turn_off", 10, POWERS),
        # Dynamixel servo swings the backlight over the fiber, LED on/off;
        # the server turns it off by itself after 60 s.
        CommandSpec("backlight_on", 10, MOVES),
        CommandSpec("backlight_off", 10, MOVES),
        CommandSpec("set_file_name", 30, FILES, "filename"),
        CommandSpec("save_image", 30, FILES),
        CommandSpec("write_header", 3, FILES, "text"),
        CommandSpec("write_header_done", 10, FILES, "text"),
    )
}

#: Commands the legacy server routes to methods it does not define (or that
#: are not routed at all). Sending one of the first group raises
#: AttributeError in its single-threaded accept loop, which kills the
#: listener until someone restarts the server. Never sent.
FORBIDDEN: dict[str, str] = {
    **{
        name: "dispatched to a method spectrograph_server.py does not define (kills the listener)"
        for name in (
            "thar_turn_on",
            "thar_turn_off",
            "flat_turn_on",
            "flat_turn_off",
            "expose",
            "set_camera_param",
            "set_data_path",
            "get_status",
            "get_fits_header",
            "get_filter_name",
            "set_binning",
            "set_size",
        )
    },
    "time_tracker_check": (
        "raises UnboundLocalError in the server's accept loop when the lamp is off or"
        " its log is missing (kills the listener); the service tracks lamp time itself"
    ),
    "start_si_image": "starts the camera software on the PC: operator only",
    "kill_si_image": "kills the camera software (the CCD cooler stops): operator only",
    "safe_close": "not routed by the server (always 'fail')",
    "backlight_power_cycle": "not routed by the server (always 'fail')",
}

_FILENAME = re.compile(r"^[A-Za-z0-9_.+-]{1,120}$")
_POSITION = re.compile(r"^[A-Za-z]{1,20}$")


def _check_text(text: str) -> None:
    """What survives the server's ``repr(data).strip("'")`` unchanged.

    The legacy server parses ``repr()`` of the bytes it received: a quote
    character switches repr to double quotes (so the command token gains a
    leading ``"``), and a backslash or non-printable byte is escaped.
    """
    if not text.isascii() or not text.isprintable() or "'" in text or "\\" in text:
        raise KiwiSpecRefused(f"argument has characters the server mangles: {text[:40]!r}")


def format_command(name: str, arg: Any = None) -> bytes:
    """Validate and encode one command; :class:`KiwiSpecRefused` if not allowed."""
    if name in FORBIDDEN:
        raise KiwiSpecRefused(f"{name}: {FORBIDDEN[name]}")
    spec = COMMANDS.get(name)
    if spec is None:
        raise KiwiSpecRefused(f"{name!r} is not an allowlisted KiwiSpec command")
    if spec.arg == "none":
        if arg is not None:
            raise KiwiSpecRefused(f"{name} takes no argument")
        text = "None"
    elif spec.arg == "float":
        value = float(arg)
        if value != value or value in (float("inf"), float("-inf")):
            raise KiwiSpecRefused(f"{name}: {arg!r} is not finite")
        if name == "i2stage_movef" and not STAGE_MIN_MM <= value <= STAGE_MAX_MM:
            raise KiwiSpecRefused(f"{name}: {value} mm is outside {STAGE_MIN_MM}-{STAGE_MAX_MM}")
        text = repr(value)
    elif spec.arg == "position":
        text = str(arg)
        if not _POSITION.match(text):
            raise KiwiSpecRefused(f"{name}: bad position name {text!r}")
    elif spec.arg == "filename":
        text = str(arg)
        if not _FILENAME.match(text):
            raise KiwiSpecRefused(f"{name}: bad file name {text!r}")
    else:  # text
        text = str(arg)
        if not text:
            raise KiwiSpecRefused(f"{name}: empty text")
        _check_text(text)
    msg = f"{name} {text}".encode("ascii")
    if len(msg) > MAX_MESSAGE:
        raise KiwiSpecRefused(f"{name}: {len(msg)} bytes is more than the server reads")
    return msg


def header_chunks(text: str, size: int = HEADER_CHUNK) -> list[str]:
    """Split a header into chunks of at most ``size`` characters, none
    starting with whitespace: the server splits each command with
    ``split(None, 1)``, which drops the argument's leading whitespace (the
    legacy client's fixed 800-character chunks could lose a space that way)."""
    chunks: list[str] = []
    i = 0
    while i < len(text):
        j = min(i + size, len(text))
        while j < len(text) and text[j].isspace() and j > i + 1:
            j -= 1
        chunks.append(text[i:j])
        i = j
    return chunks


def parse_reply(name: str, data: bytes) -> list[str]:
    """``success [fields...]`` -> the fields; ``fail`` -> :class:`KiwiSpecFailed`."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise KiwiSpecProtocolError(f"{name}: non-ASCII reply {data[:40]!r}") from None
    tokens = text.split()
    if not tokens:
        raise KiwiSpecFailed(f"{name}: empty reply (the server's 'command failed')")
    if tokens[0] == "fail":
        raise KiwiSpecFailed(f"{name}: fail")
    if tokens[0] != "success" or not text.isprintable():
        raise KiwiSpecProtocolError(f"{name}: garbled reply {text[:60]!r}")
    return tokens[1:]


def _float(name: str, fields: list[str], i: int = 0) -> float:
    try:
        return float(fields[i])
    except (IndexError, ValueError):
        raise KiwiSpecProtocolError(f"{name}: expected a number, got {fields!r}") from None


@dataclass(frozen=True)
class StagePosition:
    mm: float
    #: The configured position it matches to 0.1 mm, else "unknown".
    name: str


class SpecServerClient:
    """Async client for ``spectrograph_server.py``.

    One command at a time (the server is single-threaded with a listen
    backlog of one); each on its own connection. The bound on one attempt is
    ``connect_timeout + send_timeout + reply_timeout``; a command whose spec
    is read-only is retried up to ``max_attempts`` times in all (only on a
    timeout, a dropped connection or a garbled reply; ``fail`` is final).
    """

    def __init__(
        self,
        host: str,
        port: int = SPEC_PORT,
        *,
        allow_hardware: bool = False,
        connect_timeout: float = 3.0,
        send_timeout: float = 5.0,
        max_attempts: int = 2,
        retry_delay: float = 0.5,
        reply_timeouts: dict[str, float] | None = None,
        max_reply_timeout: float | None = None,
    ) -> None:
        self.host = check_device_address(host, allow_hardware=allow_hardware, what="spectrograph")
        self.port = port
        self.connect_timeout = connect_timeout
        self.send_timeout = send_timeout
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay = retry_delay
        self.reply_timeouts = dict(reply_timeouts or {})
        self.max_reply_timeout = max_reply_timeout
        self._lock = asyncio.Lock()
        #: (monotonic time, command, outcome) of the last exchange, for status.
        self.last: tuple[float, str, str] | None = None

    def reply_timeout(self, name: str) -> float:
        t = self.reply_timeouts.get(name, COMMANDS[name].reply_timeout)
        return t if self.max_reply_timeout is None else min(t, self.max_reply_timeout)

    def bound(self, name: str) -> float:
        """The longest one attempt at ``name`` can take (excluding the queue)."""
        return self.connect_timeout + self.send_timeout + self.reply_timeout(name)

    async def call(self, name: str, arg: Any = None, *, timeout: float | None = None) -> list[str]:
        """Send one command; return the reply's fields after ``success``.

        ``timeout`` bounds the whole call including waiting for the wire
        (default: every attempt's bound, plus retry delays, plus one bound
        for a command queued ahead of it).
        """
        msg = format_command(name, arg)
        spec = COMMANDS[name]
        attempts = self.max_attempts if spec.retry_safe else 1
        if timeout is None:
            timeout = (attempts + 1) * self.bound(name) + attempts * self.retry_delay
        try:
            async with asyncio.timeout(timeout):
                return await self._call(name, msg, attempts)
        except TimeoutError:
            self.last = (time.monotonic(), name, "timeout")
            raise KiwiSpecTimeout(f"{name}: no answer within {timeout:.1f} s") from None

    async def _call(self, name: str, msg: bytes, attempts: int) -> list[str]:
        async with self._lock:
            for attempt in range(1, attempts + 1):
                try:
                    data = await self._exchange(name, msg)
                    fields = parse_reply(name, data)
                except KiwiSpecFailed:
                    self.last = (time.monotonic(), name, "fail")
                    raise
                except (KiwiSpecUnreachable, KiwiSpecTimeout, KiwiSpecProtocolError) as exc:
                    self.last = (time.monotonic(), name, type(exc).__name__)
                    if attempt >= attempts:
                        raise
                    log.warning("%s attempt %d failed (%r); retrying", name, attempt, exc)
                    await asyncio.sleep(self.retry_delay)
                    continue
                self.last = (time.monotonic(), name, "success")
                return fields
        raise AssertionError("unreachable")  # pragma: no cover

    async def _exchange(self, name: str, msg: bytes) -> bytes:
        try:
            async with asyncio.timeout(self.connect_timeout):
                reader, writer = await asyncio.open_connection(self.host, self.port)
        except TimeoutError:
            raise KiwiSpecUnreachable(f"{name}: connect timed out") from None
        except OSError as exc:
            raise KiwiSpecUnreachable(f"{name}: cannot connect: {exc}") from None
        try:
            try:
                async with asyncio.timeout(self.send_timeout):
                    writer.write(msg)
                    await writer.drain()
            except TimeoutError:
                raise KiwiSpecTimeout(f"{name}: send timed out") from None
            except OSError as exc:
                raise KiwiSpecProtocolError(f"{name}: connection lost: {exc}") from None
            # The server sends its reply and closes: read to EOF, bounded.
            try:
                async with asyncio.timeout(self.reply_timeout(name)):
                    data = await reader.read(MAX_MESSAGE + 1)
                    while data and len(data) <= MAX_MESSAGE:
                        more = await reader.read(MAX_MESSAGE + 1 - len(data))
                        if not more:
                            break
                        data += more
            except TimeoutError:
                raise KiwiSpecTimeout(
                    f"{name}: no reply within {self.reply_timeout(name)} s"
                ) from None
            except OSError as exc:
                raise KiwiSpecProtocolError(f"{name}: connection lost: {exc}") from None
            return data
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(1.0):
                    await writer.wait_closed()

    # -- typed commands ---------------------------------------------------------

    async def cell_heater_temp(self) -> float:
        return _float("cell_heater_temp", await self.call("cell_heater_temp"))

    async def cell_heater_get_set_temp(self) -> float:
        return _float("cell_heater_get_set_temp", await self.call("cell_heater_get_set_temp"))

    async def cell_heater_set_temp(self, temp: float) -> float:
        return _float("cell_heater_set_temp", await self.call("cell_heater_set_temp", temp))

    async def cell_heater_on(self) -> None:
        await self.call("cell_heater_on")

    async def cell_heater_off(self) -> None:
        await self.call("cell_heater_off")

    async def spec_pressure(self) -> float:
        return _float("get_spec_pressure", await self.call("get_spec_pressure"))

    async def pump_pressure(self) -> float:
        return _float("get_pump_pressure", await self.call("get_pump_pressure"))

    async def stage_position(self) -> StagePosition:
        fields = await self.call("i2stage_get_pos")
        if len(fields) != 2:
            raise KiwiSpecProtocolError(f"i2stage_get_pos: expected 'mm name', got {fields!r}")
        return StagePosition(_float("i2stage_get_pos", fields), fields[1])

    async def stage_move(self, position: str) -> None:
        await self.call("i2stage_move", position)

    async def stage_move_mm(self, mm: float) -> None:
        await self.call("i2stage_movef", mm)

    async def stage_home(self) -> None:
        await self.call("i2stage_home")

    async def expmeter_total(self) -> float:
        return _float("get_expmeter_total", await self.call("get_expmeter_total"))

    async def reset_expmeter(self) -> None:
        await self.call("reset_expmeter_total")

    async def get_index(self) -> int:
        return int(_float("get_index", await self.call("get_index")))

    async def write_header(self, text: str) -> None:
        """Send a header (JSON text) in legacy-sized chunks, then ``_done``."""
        if not text or text[0].isspace():
            raise KiwiSpecRefused("header text must not be empty or start with a space")
        _check_text(text)
        chunks = header_chunks(text)
        for chunk in chunks[:-1]:
            await self.call("write_header", chunk)
        await self.call("write_header_done", chunks[-1])


# -- the SI camera server (binary) --------------------------------------------------

# Packet ids and data types (legacy si/packet.py, si/packets/*.py).
PKT_COMMAND, PKT_ACK, PKT_DATA, PKT_IMAGE = 128, 129, 131, 132
DATA_STATUS, DATA_DONE, DATA_IMAGE_HEADER, DATA_PARAMS = 2002, 2007, 2006, 2009
# Function numbers (legacy si/commands/camera.py).
FN_GET_STATUS = 1011
FN_INQUIRE_STATUS = 1017
FN_TERMINATE = 1018
FN_SET_ACQ_MODE = 1034
FN_SET_EXPTIME = 1035
FN_SET_ACQ_TYPE = 1036
FN_ACQUIRE = 1037
FN_SET_READOUT_MODE = 1042
FN_SET_CCD_FORMAT = 1043
FN_SET_COOLER = 1046

HEADER_FMT = ">IBB"  # length, packet id, camera id
COMMAND_FMT = ">IBBHH"  # + function number, parameter length
ACK_FMT = ">IBBH"  # + accept
DATA_FMT = ">IBBiHH"  # + error code, data type, byte length
DONE_FMT = DATA_FMT + "H"  # + function number
#: Longest packet accepted (a status text is a few kB at most).
MAX_PACKET = 1 << 20


def si_command(func: int, params: bytes = b"") -> bytes:
    """One command packet (camera id 1, as the legacy client always sent)."""
    head = struct.calcsize(COMMAND_FMT)
    return struct.pack(COMMAND_FMT, head + len(params), PKT_COMMAND, 1, func, len(params)) + params


def acquire_params() -> bytes:
    """``Acquire``'s fixed parameters, as legacy sent them (buffer 1)."""
    return struct.pack(">HHHBB", 4, 1, 0, 73, 0)


@dataclass(frozen=True)
class SIPacket:
    kind: int  # PKT_ACK / PKT_DATA / PKT_IMAGE
    accept: bool = False
    err_code: int = 0
    data_type: int = 0
    func: int = 0
    payload: bytes = b""


def parse_si_packet(raw: bytes) -> SIPacket:
    length, pid, _cam = struct.unpack_from(HEADER_FMT, raw)
    if pid == PKT_ACK:
        if length != struct.calcsize(ACK_FMT):
            raise KiwiSpecProtocolError(f"SI ack of length {length}")
        return SIPacket(PKT_ACK, accept=bool(struct.unpack(ACK_FMT, raw)[3]))
    if pid == PKT_DATA:
        if length < struct.calcsize(DATA_FMT):
            raise KiwiSpecProtocolError(f"SI data packet of length {length}")
        _, _, _, err, dtype, _blen = struct.unpack_from(DATA_FMT, raw)
        if dtype == DATA_DONE:
            if length != struct.calcsize(DONE_FMT):
                raise KiwiSpecProtocolError(f"SI done packet of length {length}")
            func = struct.unpack(DONE_FMT, raw)[6]
            return SIPacket(PKT_DATA, err_code=err, data_type=dtype, func=func)
        payload = raw[struct.calcsize(DATA_FMT) :].rstrip(b"\x00")
        return SIPacket(PKT_DATA, err_code=err, data_type=dtype, payload=payload)
    if pid == PKT_IMAGE:
        return SIPacket(PKT_IMAGE, payload=raw)
    raise KiwiSpecProtocolError(f"SI packet id {pid}")


def parse_camera_status(text: str) -> dict[str, str]:
    """``GetStatusFromCamera`` text -> {name: value}.

    The real format is not in the legacy code (it only ever printed it); this
    accepts lines of ``name,value`` or ``group,name,value`` (the format of
    ``GetCameraParameter``, which legacy parsed that way). See the open
    questions in ``docs/kiwispec.md``.
    """
    out: dict[str, str] = {}
    for line in text.replace("\r", "\n").split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[-2]:
            out[parts[-2]] = parts[-1]
    return out


def ccd_temperature(status: dict[str, str]) -> float:
    for key, value in status.items():
        k = key.lower()
        if "ccd" in k and "temp" in k and "set" not in k:
            try:
                return float(value)
            except ValueError:
                break
    raise KiwiSpecProtocolError(f"no CCD temperature in camera status {sorted(status)}")


class SIAcquisition:
    """One ``Acquire`` in flight: :meth:`wait` for its ``Done``."""

    def __init__(self, client: SICameraClient, done: asyncio.Future) -> None:
        self._client = client
        self._done = done
        self.started = time.monotonic()

    @property
    def finished(self) -> bool:
        return self._done.done()

    async def wait(self, timeout: float) -> None:
        """Wait for the camera's ``Done`` (exposure end + readout), bounded."""
        try:
            async with asyncio.timeout(timeout):
                err = await asyncio.shield(self._done)
        except TimeoutError:
            raise KiwiSpecTimeout(f"SI acquisition not done within {timeout:.1f} s") from None
        if err:
            raise KiwiSpecFailed(f"SI acquisition ended with error code {err}")


class SICameraClient:
    """Async client for the SI Image camera server (one persistent connection).

    A reader task routes the server's packets: acks go to the command that
    is waiting for one (they come in order), ``Done`` packets to the
    command whose function number they carry, other data to the command
    waiting for it. ``TerminateAcquisition`` is sent on the same connection
    while an ``Acquire`` is outstanding, as legacy did. If the connection
    drops, everything waiting fails at once.
    """

    def __init__(
        self,
        host: str,
        port: int = CAMERA_PORT,
        *,
        allow_hardware: bool = False,
        connect_timeout: float = 5.0,
        command_timeout: float = 10.0,
    ) -> None:
        self.host = check_device_address(host, allow_hardware=allow_hardware, what="SI camera")
        self.port = port
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._acks: asyncio.Queue[SIPacket | BaseException] = asyncio.Queue()
        self._done: dict[int, asyncio.Future] = {}
        self._data: dict[int, asyncio.Future] = {}
        self._broken: BaseException | None = None

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done()

    async def connect(self) -> None:
        if self.connected:
            return
        await self.close()
        try:
            async with asyncio.timeout(self.connect_timeout):
                self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        except TimeoutError:
            raise KiwiSpecTimeout("SI camera: connect timed out") from None
        except OSError as exc:
            raise KiwiSpecError(f"SI camera: cannot connect: {exc}") from exc
        self._broken = None
        self._acks = asyncio.Queue()
        self._task = asyncio.get_running_loop().create_task(self._read_loop())

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(1.0):
                    await writer.wait_closed()
        self._fail_all(KiwiSpecError("SI camera connection closed"))

    def _fail_all(self, exc: BaseException) -> None:
        self._broken = exc
        for futs in (self._done, self._data):
            for fut in futs.values():
                if not fut.done():
                    fut.set_exception(exc)
                    fut.exception()  # mark retrieved; the waiter re-raises it
            futs.clear()
        self._acks.put_nowait(exc)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        head = struct.calcsize(HEADER_FMT)
        try:
            while True:
                first = await self._reader.readexactly(head)
                length = struct.unpack(HEADER_FMT, first)[0]
                if not head <= length <= MAX_PACKET:
                    raise KiwiSpecProtocolError(f"SI packet length {length}")
                raw = first + await self._reader.readexactly(length - head)
                pkt = parse_si_packet(raw)
                if pkt.kind == PKT_ACK:
                    self._acks.put_nowait(pkt)
                elif pkt.kind == PKT_DATA and pkt.data_type == DATA_DONE:
                    fut = self._done.pop(pkt.func, None)
                    if fut is not None and not fut.done():
                        fut.set_result(pkt.err_code)
                    else:
                        log.warning("SI: unexpected Done for function %d", pkt.func)
                elif pkt.kind == PKT_DATA:
                    fut = self._data.pop(pkt.data_type, None)
                    if fut is not None and not fut.done():
                        fut.set_result(pkt)
                else:
                    log.warning("SI: ignoring packet kind %d", pkt.kind)
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, OSError) as exc:
            self._fail_all(KiwiSpecError(f"SI camera connection lost: {exc!r}"))
        except KiwiSpecError as exc:
            self._fail_all(exc)

    async def _send(self, func: int, params: bytes = b"") -> None:
        if self._writer is None or not self.connected:
            raise KiwiSpecError("SI camera not connected")
        async with asyncio.timeout(self.command_timeout):
            self._writer.write(si_command(func, params))
            await self._writer.drain()

    async def _ack(self) -> None:
        item = await self._acks.get()
        if isinstance(item, BaseException):
            self._acks.put_nowait(item)  # keep the failure for later waiters
            raise item
        if not item.accept:
            raise KiwiSpecFailed("SI camera did not accept the command")

    async def command(
        self,
        func: int,
        params: bytes = b"",
        *,
        expect: int | None = None,
        timeout: float | None = None,
        ack: bool = True,
    ) -> Any:
        """Send one command; wait for its ack (unless ``ack=False``) and then
        for its ``Done`` (``expect=None``) or the data packet of type
        ``expect``. Bounded by ``timeout`` (default ``command_timeout``)."""
        await self.connect()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        if expect is None:
            self._done[func] = fut
        else:
            self._data[expect] = fut
        try:
            async with asyncio.timeout(timeout or self.command_timeout):
                async with self._send_lock:
                    await self._send(func, params)
                    if ack:
                        await self._ack()
                result = await fut
        except TimeoutError:
            raise KiwiSpecTimeout(f"SI function {func}: no answer") from None
        finally:
            (self._done if expect is None else self._data).pop(
                func if expect is None else expect, None
            )
        if expect is None:
            if result:
                raise KiwiSpecFailed(f"SI function {func}: error code {result}")
            return None
        return result

    # -- typed commands ---------------------------------------------------------

    async def set_readout_mode(self, mode: int) -> None:
        await self.command(FN_SET_READOUT_MODE, struct.pack(">B", mode))

    async def set_ccd_format(self, so: int, sl: int, sb: int, po: int, pl: int, pb: int) -> None:
        await self.command(FN_SET_CCD_FORMAT, struct.pack(">6i", so, sl, sb, po, pl, pb))

    async def set_cooler(self, on: bool) -> None:
        await self.command(FN_SET_COOLER, struct.pack(">B", 1 if on else 0))

    async def status(self) -> dict[str, str]:
        pkt = await self.command(FN_GET_STATUS, expect=DATA_STATUS)
        if pkt.err_code:
            raise KiwiSpecFailed(f"GetStatusFromCamera: error code {pkt.err_code}")
        try:
            text = pkt.payload.decode("ascii")
        except UnicodeDecodeError:
            raise KiwiSpecProtocolError("camera status is not ASCII") from None
        return parse_camera_status(text)

    async def ccd_temperature(self) -> float:
        return ccd_temperature(await self.status())

    async def start_acquisition(self, exptime: float, *, dark: bool) -> SIAcquisition:
        """Single frame, light or dark, ``exptime`` s; returns once ``Acquire``
        is acknowledged (the shutter is then opening)."""
        if not 0.0 <= exptime < 2**32 / 1000:
            raise KiwiSpecRefused(f"exposure time {exptime} out of range")
        await self.command(FN_SET_ACQ_MODE, struct.pack(">B", 0))
        await self.command(FN_SET_ACQ_TYPE, struct.pack(">HB", 1, 1 if dark else 0))
        await self.command(FN_SET_EXPTIME, struct.pack(">I", round(exptime * 1000)))
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._done[FN_ACQUIRE] = fut
        try:
            async with asyncio.timeout(self.command_timeout):
                async with self._send_lock:
                    await self._send(FN_ACQUIRE, acquire_params())
                    await self._ack()
        except TimeoutError:
            self._done.pop(FN_ACQUIRE, None)
            raise KiwiSpecTimeout("SI Acquire: not acknowledged") from None
        except BaseException:
            self._done.pop(FN_ACQUIRE, None)
            raise
        return SIAcquisition(self, fut)

    async def terminate(self, *, timeout: float | None = None) -> None:
        """``TerminateAcquisition`` (no ack, per the legacy si package)."""
        await self.command(FN_TERMINATE, ack=False, timeout=timeout)
