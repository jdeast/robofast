"""An APC switched rack PDU over SNMP, and a simulator of one.

The legacy ``minerva_library/pdu.py`` (MinervaCollaboration/minerva-control)
drives APC AP79xx PDUs with pysnmp: ``sPDUOutletCtl`` (PowerNet-MIB,
``1.3.6.1.4.1.318.1.1.4.4.2.1.3.<n>``) reads 1 = on, 2 = off and is set to
1 (on), 2 (off) or 3 (reboot: off, then on after the PDU's reboot
duration); ``sPDUOutletCtlName`` (``...4.4.2.1.4.<n>``) is the outlet's
name; ``sPDUMasterState`` (``...4.2.2.0``) is every outlet at once. The
legacy code had the communities in the source; here they come from a
credentials file (:func:`load_credentials`) and nothing has a default.

:class:`APCPDU` is the client (:class:`~robofast_core.devices.snmp.SNMPClient`
underneath); :class:`APCPDUSim` is a UDP SNMP agent that behaves like one
(outlet states and names, a delayed reboot, community checks), for tests
and ``pdu_server --simulate``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robofast_core.devices.snmp import (
    GET,
    RESPONSE,
    SET,
    Message,
    SNMPClient,
    SNMPError,
    decode_message,
    encode_message,
)

log = logging.getLogger(__name__)

__all__ = ["APCPDU", "APCPDUSim", "Credentials", "load_credentials"]

OUTLET_CTL = "1.3.6.1.4.1.318.1.1.4.4.2.1.3"
OUTLET_NAME = "1.3.6.1.4.1.318.1.1.4.4.2.1.4"
MASTER_STATE = "1.3.6.1.4.1.318.1.1.4.2.2.0"
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
ON, OFF, REBOOT = 1, 2, 3


@dataclass(frozen=True)
class Credentials:
    read: str
    write: str

    def __repr__(self) -> str:  # never in a log
        return "Credentials(read=***, write=***)"


def load_credentials(path: str | Path) -> Credentials:
    """``{"read": "...", "write": "..."}`` from a file that only its owner
    can read (refused if group or others may)."""
    p = Path(path)
    mode = p.stat().st_mode
    if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"{p}: SNMP credentials must not be readable by others (chmod 600)")
    d = json.loads(p.read_text())
    if not isinstance(d, dict) or not d.get("read") or not d.get("write"):
        raise ValueError(f"{p}: needs non-empty 'read' and 'write' communities")
    return Credentials(str(d["read"]), str(d["write"]))


class APCPDU:
    """One APC PDU (module doc). Outlets are numbered from 1."""

    def __init__(
        self,
        host: str,
        port: int,
        credentials: Credentials,
        *,
        timeout: float = 2.0,
        retries: int = 2,
    ) -> None:
        self.snmp = SNMPClient(
            host,
            port,
            read_community=credentials.read,
            write_community=credentials.write,
            timeout=timeout,
            retries=retries,
        )

    async def describe(self) -> str:
        return str(await self.snmp.get(SYS_DESCR))

    async def state(self, outlet: int) -> bool:
        v = await self.snmp.get(f"{OUTLET_CTL}.{int(outlet)}")
        if v not in (ON, OFF, REBOOT):
            raise SNMPError(f"outlet {outlet}: unexpected state {v!r}")
        return v == ON

    async def name(self, outlet: int) -> str:
        return str(await self.snmp.get(f"{OUTLET_NAME}.{int(outlet)}"))

    async def set(self, outlet: int, on: bool) -> None:
        await self.snmp.set(f"{OUTLET_CTL}.{int(outlet)}", ON if on else OFF)

    async def reboot(self, outlet: int) -> None:
        """Off, then on after the PDU's own reboot duration."""
        await self.snmp.set(f"{OUTLET_CTL}.{int(outlet)}", REBOOT)


# -- the simulator ------------------------------------------------------------------------


@dataclass
class _Outlet:
    name: str
    on: bool = True
    #: ``time.monotonic()`` at which a reboot turns it back on.
    back_on: float | None = None


@dataclass
class APCPDUSim:
    """A simulated APC PDU agent on UDP (module doc). ``outlets`` are the
    names (outlet 1 first); ``reboot_delay`` s from off to on."""

    credentials: Credentials
    outlets: list[str] = field(default_factory=lambda: [f"Outlet {i}" for i in range(1, 9)])
    reboot_delay: float = 0.5
    host: str = "127.0.0.1"
    port: int = 0
    #: Every SET: (outlet, value).
    sets: list[tuple[int, int]] = field(default_factory=list)
    #: Ignore every request (a hung PDU).
    silent: bool = False

    def __post_init__(self) -> None:
        self._state = [_Outlet(n) for n in self.outlets]
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> APCPDUSim:
        loop = asyncio.get_running_loop()
        sim = self

        class Proto(asyncio.DatagramProtocol):
            def connection_made(self, transport: Any) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr: Any) -> None:
                out = sim.handle(data)
                if out is not None:
                    self.transport.sendto(out, addr)

        self._transport, _ = await loop.create_datagram_endpoint(
            Proto, local_addr=(self.host, self.port)
        )
        self.port = self._transport.get_extra_info("sockname")[1]
        return self

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def is_on(self, outlet: int) -> bool:
        o = self._state[outlet - 1]
        if o.back_on is not None and time.monotonic() >= o.back_on:
            o.on, o.back_on = True, None
        return o.on

    def handle(self, data: bytes) -> bytes | None:
        try:
            m = decode_message(data)
        except SNMPError:
            return None
        c = self.credentials
        if self.silent or m.community not in (c.read, c.write):
            return None  # as an agent does: a wrong community gets no answer
        if m.pdu == SET and m.community != c.write:
            return self._reply(m, error=16)  # authorizationError (v2c)
        out: list[tuple[str, Any]] = []
        for i, (oid, value) in enumerate(m.varbinds or [], start=1):
            try:
                out.append((oid, self._one(m.pdu, oid, value)))
            except LookupError:
                return self._reply(m, error=2, index=i)  # noSuchName
            except ValueError:
                return self._reply(m, error=3, index=i)  # badValue
        return encode_message(Message(m.version, m.community, RESPONSE, m.request_id, varbinds=out))

    def _reply(self, m: Message, error: int, index: int = 0) -> bytes:
        return encode_message(
            Message(m.version, m.community, RESPONSE, m.request_id, error, index, m.varbinds)
        )

    def _outlet(self, oid: str, base: str) -> int:
        n = int(oid[len(base) + 1 :])
        if not 1 <= n <= len(self._state):
            raise LookupError(oid)
        return n

    def _one(self, pdu: int, oid: str, value: Any) -> Any:
        if pdu == GET and oid == SYS_DESCR:
            return f"APC Web/SNMP Management Card (simulated), {len(self._state)} outlets"
        if pdu == GET and oid == MASTER_STATE:
            n = len(self._state)
            return " ".join("On" if self.is_on(i) else "Off" for i in range(1, n + 1))
        if oid.startswith(OUTLET_NAME + "."):
            n = self._outlet(oid, OUTLET_NAME)
            if pdu == SET:
                self._state[n - 1].name = str(value)
            return self._state[n - 1].name
        if oid.startswith(OUTLET_CTL + "."):
            n = self._outlet(oid, OUTLET_CTL)
            if pdu == SET:
                if value not in (ON, OFF, REBOOT):
                    raise ValueError(value)
                self.sets.append((n, int(value)))
                o = self._state[n - 1]
                if value == REBOOT:
                    o.on, o.back_on = False, time.monotonic() + self.reboot_delay
                else:
                    o.on, o.back_on = value == ON, None
                return value
            return ON if self.is_on(n) else OFF
        raise LookupError(oid)
