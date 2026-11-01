"""A minimal SNMP v1/v2c client and agent codec (GET and SET of scalars).

Enough for an APC switched PDU (:mod:`robofast_core.devices.apc_pdu`) and
its simulator, with no third-party dependency: BER encoding of INTEGER,
OCTET STRING, NULL, OBJECT IDENTIFIER and SEQUENCE, the GetRequest /
SetRequest / Response PDUs, and an asyncio UDP client with a per-request
timeout and retries. Not a general SNMP stack (no v3, no bulk, no traps).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "GET",
    "RESPONSE",
    "SET",
    "Message",
    "SNMPClient",
    "SNMPError",
    "SNMPTimeout",
    "decode_message",
    "encode_message",
]

INTEGER, OCTETS, NULL, OID, SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
COUNTER32, GAUGE32, TIMETICKS = 0x41, 0x42, 0x43
NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB = 0x80, 0x81, 0x82
GET, GETNEXT, RESPONSE, SET = 0xA0, 0xA1, 0xA2, 0xA3
#: error-status values (RFC 3416).
ERRORS = {
    0: "noError",
    1: "tooBig",
    2: "noSuchName",
    3: "badValue",
    4: "readOnly",
    5: "genErr",
    6: "noAccess",
    7: "wrongType",
    10: "wrongValue",
    16: "authorizationError",
    17: "notWritable",
}


class SNMPError(Exception):
    """The agent answered with an error, or the answer made no sense."""


class SNMPTimeout(SNMPError):
    """No answer within the timeout, after every retry."""


def parse_oid(oid: str | Sequence[int]) -> tuple[int, ...]:
    if isinstance(oid, str):
        return tuple(int(x) for x in oid.strip(".").split("."))
    return tuple(int(x) for x in oid)


def oid_str(oid: Sequence[int]) -> str:
    return ".".join(str(x) for x in oid)


# -- BER -------------------------------------------------------------------------------


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def enc_int(v: int, tag: int = INTEGER) -> bytes:
    n = max(1, (v.bit_length() + 8) // 8)
    return _tlv(tag, int(v).to_bytes(n, "big", signed=True))


def enc_oid(oid: str | Sequence[int]) -> bytes:
    o = parse_oid(oid)
    if len(o) < 2:
        raise ValueError(f"OID {o} too short")
    out = bytearray([40 * o[0] + o[1]])
    for sub in o[2:]:
        chunk = [sub & 0x7F]
        sub >>= 7
        while sub:
            chunk.append(0x80 | (sub & 0x7F))
            sub >>= 7
        out.extend(reversed(chunk))
    return _tlv(OID, bytes(out))


def enc_value(v: Any) -> bytes:
    if v is None:
        return _tlv(NULL, b"")
    if isinstance(v, bool):
        raise TypeError("SNMP has no boolean")
    if isinstance(v, int):
        return enc_int(v)
    if isinstance(v, str):
        return _tlv(OCTETS, v.encode())
    if isinstance(v, bytes):
        return _tlv(OCTETS, v)
    raise TypeError(f"cannot encode {type(v).__name__} in SNMP")


def _read(data: bytes, i: int) -> tuple[int, bytes, int]:
    """(tag, body, next index) of the TLV at ``i``."""
    if i + 2 > len(data):
        raise SNMPError("truncated SNMP message")
    tag = data[i]
    n = data[i + 1]
    j = i + 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(data[j : j + k], "big")
        j += k
    if j + n > len(data):
        raise SNMPError("truncated SNMP message")
    return tag, data[j : j + n], j + n


def _dec_oid(body: bytes) -> tuple[int, ...]:
    out = [body[0] // 40, body[0] % 40]
    sub = 0
    for b in body[1:]:
        sub = (sub << 7) | (b & 0x7F)
        if not b & 0x80:
            out.append(sub)
            sub = 0
    return tuple(out)


def _dec_value(tag: int, body: bytes) -> Any:
    if tag in (INTEGER,):
        return int.from_bytes(body, "big", signed=True)
    if tag in (COUNTER32, GAUGE32, TIMETICKS):
        return int.from_bytes(body, "big", signed=False)
    if tag == OCTETS:
        return body.decode("utf-8", "replace")
    if tag == NULL:
        return None
    if tag == OID:
        return oid_str(_dec_oid(body))
    if tag in (NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB):
        return _Missing(tag)
    return body


@dataclass(frozen=True)
class _Missing:
    tag: int

    def __str__(self) -> str:
        return {0x80: "noSuchObject", 0x81: "noSuchInstance"}.get(self.tag, "endOfMibView")


@dataclass
class Message:
    version: int  # 0 = v1, 1 = v2c
    community: str
    pdu: int
    request_id: int
    error_status: int = 0
    error_index: int = 0
    #: (OID string, value) pairs.
    varbinds: list[tuple[str, Any]] | None = None


def encode_message(m: Message) -> bytes:
    vbs = b"".join(_tlv(SEQUENCE, enc_oid(o) + enc_value(v)) for o, v in (m.varbinds or []))
    pdu = _tlv(
        m.pdu,
        enc_int(m.request_id)
        + enc_int(m.error_status)
        + enc_int(m.error_index)
        + _tlv(SEQUENCE, vbs),
    )
    return _tlv(SEQUENCE, enc_int(m.version) + _tlv(OCTETS, m.community.encode()) + pdu)


def decode_message(data: bytes) -> Message:
    try:
        tag, body, _ = _read(data, 0)
        if tag != SEQUENCE:
            raise SNMPError("not an SNMP message")
        _, v, i = _read(body, 0)
        version = int.from_bytes(v, "big", signed=True)
        _, c, i = _read(body, i)
        pdu_tag, pdu, _ = _read(body, i)
        out = []
        j = 0
        nums = []
        for _ in range(3):
            _t, v, j = _read(pdu, j)
            nums.append(int.from_bytes(v, "big", signed=True))
        _, vbs, _ = _read(pdu, j)
        k = 0
        while k < len(vbs):
            _, vb, k = _read(vbs, k)
            _, ob, m = _read(vb, 0)
            t2, val, _ = _read(vb, m)
            out.append((oid_str(_dec_oid(ob)), _dec_value(t2, val)))
    except (IndexError, ValueError) as exc:
        raise SNMPError(f"malformed SNMP message: {exc}") from None
    return Message(version, c.decode("utf-8", "replace"), pdu_tag, nums[0], nums[1], nums[2], out)


# -- the client ------------------------------------------------------------------------------


class _Proto(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.waiting: dict[int, asyncio.Future] = {}

    def datagram_received(self, data: bytes, addr: Any) -> None:
        try:
            m = decode_message(data)
        except SNMPError as exc:
            log.warning("SNMP: %s", exc)
            return
        fut = self.waiting.pop(m.request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(m)

    def error_received(self, exc: Exception) -> None:
        for fut in self.waiting.values():
            if not fut.done():
                fut.set_exception(SNMPError(f"SNMP transport: {exc}"))
        self.waiting.clear()


_ids = itertools.count(1)


class SNMPClient:
    """GET and SET over UDP to one agent (v2c by default). Communities
    come from the caller (a credentials file), never from code."""

    def __init__(
        self,
        host: str,
        port: int = 161,
        *,
        read_community: str,
        write_community: str | None = None,
        timeout: float = 2.0,
        retries: int = 2,
        version: int = 1,
    ) -> None:
        self.host, self.port = host, int(port)
        self.read_community = read_community
        self.write_community = write_community
        self.timeout = timeout
        self.retries = retries
        self.version = version

    async def _request(self, pdu: int, community: str, varbinds: list[tuple[str, Any]]) -> Message:
        loop = asyncio.get_running_loop()
        transport, proto = await loop.create_datagram_endpoint(
            _Proto, remote_addr=(self.host, self.port)
        )
        try:
            for _ in range(self.retries + 1):
                rid = next(_ids) & 0x7FFFFFFF
                fut: asyncio.Future = loop.create_future()
                proto.waiting[rid] = fut
                transport.sendto(
                    encode_message(Message(self.version, community, pdu, rid, varbinds=varbinds))
                )
                try:
                    return await asyncio.wait_for(fut, self.timeout)
                except TimeoutError:
                    proto.waiting.pop(rid, None)
            raise SNMPTimeout(
                f"SNMP {self.host}:{self.port}: no answer in {self.retries + 1} x {self.timeout} s"
            )
        finally:
            transport.close()

    @staticmethod
    def _check(m: Message) -> list[tuple[str, Any]]:
        if m.pdu != RESPONSE:
            raise SNMPError(f"unexpected PDU 0x{m.pdu:02x}")
        if m.error_status:
            what = ERRORS.get(m.error_status, m.error_status)
            raise SNMPError(f"SNMP error {what} at varbind {m.error_index}")
        for oid, v in m.varbinds or []:
            if isinstance(v, _Missing):
                raise SNMPError(f"{oid}: {v}")
        return list(m.varbinds or [])

    async def get(self, oid: str | Sequence[int]) -> Any:
        m = await self._request(GET, self.read_community, [(oid_str(parse_oid(oid)), None)])
        return self._check(m)[0][1]

    async def set(self, oid: str | Sequence[int], value: int | str) -> Any:
        if self.write_community is None:
            raise SNMPError("no write community: this client is read-only")
        m = await self._request(SET, self.write_community, [(oid_str(parse_oid(oid)), value)])
        return self._check(m)[0][1]
