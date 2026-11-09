"""Find the Alpaca devices on the network and write a starter config.

::

    robofast-discover                          # list every Alpaca device that answers
    robofast-discover --write observatory.json # ... and write a starter config
    robofast-discover --server 127.0.0.1:11111 # ask one server (a tunnel: no UDP)

Discovery is the ASCOM Alpaca one: ``alpacadiscovery1`` to UDP port 32227
(``--address``, default the IPv4 broadcast address ``255.255.255.255``;
``127.0.0.1`` for servers on this machine), every answer
``{"AlpacaPort": N}`` collected until ``--timeout``. Each server found is
asked its devices on its management API
(``GET /management/v1/configureddevices``). Nothing else is sent: no device
is connected, and no device member is read or set.

The starter config (``--write``, never over an existing file) is an
enclosure config (:mod:`robofast.config`) with what the devices said:

* ``telescopes``: ``t1``, ``t2``, ... one per Telescope, each with the
  Camera, FilterWheel, Focuser and Rotator of the same device number on the
  same server, or, for a lone telescope, the only one of that type;
* ``images``: frames saved under ``data/`` next to the config (without
  ``images`` the sequencer saves nothing);
* ``plan``: one bias (``{"type": "calibration", "biases": 1}``), so the
  first exposure opens nothing and needs no lease.

It never writes ``devices`` (the dome, the safety monitor, the weather),
``limits``, ``site``, ``mode`` or ``safety``: those are the operator's
(SAFETY.md). A Dome, SafetyMonitor or ObservingConditions that answered is
named in the config's ``_note`` with the line to add; the supervisor
refuses to start until ``devices.dome`` and ``devices.safetymonitor`` are
there. Anything it could not pair is named in ``_note`` too.
docs/discover.md walks a first exposure from it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robofast import alpaca_client
from robofast.owner import DeviceError

#: The Alpaca discovery port and message (ASCOM Alpaca API, "Discovery").
DISCOVERY_PORT = 32227
DISCOVERY_MESSAGE = b"alpacadiscovery1"

#: Per telescope, the device types paired with it, and their config keys.
PAIRED = {
    "Camera": "camera",
    "FilterWheel": "filterwheel",
    "Focuser": "focuser",
    "Rotator": "rotator",
}

#: Device types the starter config never wires: each is named in ``_note``.
OPERATOR = ("Dome", "SafetyMonitor", "ObservingConditions")


@dataclass(frozen=True)
class Device:
    """One device a server listed: its ``host:port``, type and number."""

    server: str
    type: str
    number: int
    name: str = ""

    @property
    def spec(self) -> str:
        """The config's ``host:port/N``."""
        return f"{self.server}/{self.number}"


def discover(
    address: str = "255.255.255.255", port: int = DISCOVERY_PORT, timeout: float = 2.0
) -> list[str]:
    """Every Alpaca server that answers discovery at ``address`` within
    ``timeout`` s, as ``host:port`` (the answer's sender and its
    ``AlpacaPort``), in the order they answered, each once."""
    found: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(DISCOVERY_MESSAGE, (address, port))
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            s.settimeout(left)
            try:
                data, (host, _) = s.recvfrom(1024)
            except TimeoutError:
                break
            try:
                server = f"{host}:{int(json.loads(data)['AlpacaPort'])}"
            except (ValueError, KeyError, TypeError):
                continue  # not an Alpaca answer
            if server not in found:
                found.append(server)
    return found


def configured_devices(server: str, timeout: float = 5.0) -> list[Device]:
    """The devices ``server`` (``host:port``) lists on its management API,
    asked through robofast's Alpaca client
    (:func:`robofast.alpaca_client.configured_devices`)."""
    listed = asyncio.run(alpaca_client.configured_devices(server, timeout=timeout))
    return [
        Device(server, d["DeviceType"], int(d["DeviceNumber"]), d.get("DeviceName", ""))
        for d in listed
    ]


def _partner(scope: Device, kind: str, devices: list[Device], alone: bool) -> Device | None:
    """The ``kind`` device for ``scope``: same server and number, else, for
    the only telescope (``alone``), the only one of its type; else None."""
    of_kind = [d for d in devices if d.type == kind]
    same = [d for d in of_kind if d.server == scope.server and d.number == scope.number]
    if same:
        return same[0]
    return of_kind[0] if alone and len(of_kind) == 1 else None


def starter_config(devices: list[Device], name: str = "observatory") -> dict[str, Any]:
    """The starter enclosure config for ``devices`` (see the module doc)."""
    notes = [
        "Written by robofast-discover from what the devices reported (docs/discover.md).",
        "Not written, the operator's (SAFETY.md): devices (dome, safetymonitor,"
        " observingconditions), limits, site, mode, safety.",
    ]
    scopes = [d for d in devices if d.type == "Telescope"]
    telescopes: dict[str, dict[str, str]] = {}
    used: set[Device] = set(scopes)
    for i, scope in enumerate(scopes, 1):
        entry = {"telescope": scope.spec}
        for kind, key in PAIRED.items():
            partner = _partner(scope, kind, devices, alone=len(scopes) == 1)
            if partner is not None:
                entry[key] = partner.spec
                used.add(partner)
        telescopes[f"t{i}"] = entry
    if not scopes:
        notes.append("No Telescope answered: add telescopes.t1 by hand.")
    for t, entry in telescopes.items():
        if "camera" not in entry:
            notes.append(f"{t}: no camera paired: add telescopes.{t}.camera by hand.")
    for d in devices:
        if d.type in OPERATOR:
            key = d.type.lower()
            notes.append(
                f"{d.type} {d.spec} answered ({d.name}). If it is this enclosure's,"
                f' add "devices": {{"{key}": "{d.spec}"}}; robofast does not choose it.'
            )
        elif d not in used and d.type in PAIRED:
            notes.append(f"{d.type} {d.spec} ({d.name}) not paired with a telescope.")
    if not any(d.type in ("Dome", "SafetyMonitor") for d in devices):
        notes.append(
            "No Dome or SafetyMonitor answered: the supervisor needs devices.dome and"
            " devices.safetymonitor before it starts."
        )
    return {
        "_note": notes,
        "name": name,
        "listen": "127.0.0.1:8100",
        "telescopes": telescopes,
        "images": {"data_root": "data", "site": name},
        "plan": {"type": "calibration", "biases": 1},
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--address",
        default="255.255.255.255",
        help="where to send discovery (default: broadcast; 127.0.0.1: this machine)",
    )
    ap.add_argument("--port", type=int, default=DISCOVERY_PORT, help="discovery UDP port")
    ap.add_argument("--timeout", type=float, default=2.0, help="seconds to wait for answers")
    ap.add_argument(
        "--server",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help="ask this Alpaca server directly, without UDP discovery (repeatable)",
    )
    ap.add_argument("--write", metavar="FILE", help="write a starter config here")
    ap.add_argument(
        "--name", default="observatory", help="the config's name (default: %(default)s)"
    )
    args = ap.parse_args(argv)
    if args.write and Path(args.write).exists():
        ap.error(f"{args.write} exists: not overwritten")
    servers = list(args.server) or discover(args.address, args.port, args.timeout)
    if not servers:
        print(
            f"no Alpaca server answered discovery at {args.address}:{args.port}"
            f" within {args.timeout:g} s",
            file=sys.stderr,
        )
        sys.exit(1)
    devices: list[Device] = []
    for server in servers:
        try:
            devices += configured_devices(server)
        except (DeviceError, ValueError, KeyError, TypeError) as exc:
            print(f"{server}: no device list ({exc!r})", file=sys.stderr)
    width = max([len(d.type) for d in devices] + [4])
    for d in devices:
        print(f"{d.type:<{width}}  {d.spec:<24} {d.name}")
    counts = Counter(d.type for d in devices)
    print(
        f"{len(devices)} device(s) on {len(servers)} server(s): "
        + ", ".join(f"{n} {t}" for t, n in sorted(counts.items()))
    )
    if args.write:
        config = starter_config(devices, args.name)
        Path(args.write).write_text(json.dumps(config, indent=2) + "\n")
        print(f"wrote {args.write}")
        for line in config["_note"][2:]:
            print(f"  {line}")


if __name__ == "__main__":
    main()
