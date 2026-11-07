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
(``GET /management/v1/configureddevices``). Listing sends nothing else.

The starter config (``--write``, never over an existing file) is an
enclosure config (:mod:`robofast.config`) with what the devices said:

* ``telescopes``: ``t1``, ``t2``, ... one per Telescope, each with the
  Camera, FilterWheel, Focuser and Rotator of the same device number on the
  same server, or, for a lone telescope, the only one of that type;
* ``images``: frames saved under ``data/`` next to the config (without
  ``images`` the sequencer saves nothing);
* ``plan``: one bias (``{"type": "calibration", "biases": 1}``), so the
  first exposure opens nothing and needs no lease;
* ``images.telescopes.tN``: the plate scale (arcsec per unbinned pixel,
  206.265 x pixel size in um / focal length in mm) and the focal length
  (mm), from the telescope's ``FocalLength`` and the camera's
  ``PixelSizeX`` and ``PixelSizeY`` (:func:`read_optics`). A device that
  cannot report one (an Alpaca error, a focal length of 0 or less, pixels
  that are not square) stops ``--write`` with an error naming the device
  and the member, and nothing is written; ``--no-optics`` writes the config
  without them. To read them, a device that reports itself not connected
  is connected, read and disconnected again; nothing else is set.

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
import json
import socket
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

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

#: Arcsec per radian / 1000: plate scale [arcsec/px] = this * um / mm.
ARCSEC_PER_UM_PER_MM = 206.264806

#: The ClientID robofast-discover reads devices with.
CLIENT_ID = 32227

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
    """The devices ``server`` (``host:port``) lists on its management API."""
    r = httpx.get(
        f"http://{server}/management/v1/configureddevices", timeout=timeout, trust_env=False
    )
    r.raise_for_status()
    return [
        Device(server, d["DeviceType"], int(d["DeviceNumber"]), d.get("DeviceName", ""))
        for d in r.json()["Value"]
    ]


class OpticsUnavailable(RuntimeError):
    """A device could not report a value the plate scale needs."""


def _alpaca(client: httpx.Client, d: Device, member: str, value: Any = None) -> Any:
    """GET (or with ``value``, PUT) ``member`` of ``d``; an Alpaca error is
    :class:`OpticsUnavailable` naming the device and the member."""
    url = f"http://{d.server}/api/v1/{d.type.lower()}/{d.number}/{member.lower()}"
    what = f"{d.type} {d.spec} ({d.name}) {member}"
    try:
        if value is None:
            r = client.get(url, params={"ClientID": CLIENT_ID, "ClientTransactionID": 1})
        else:
            data = {member: value, "ClientID": CLIENT_ID, "ClientTransactionID": 1}
            r = client.put(url, data=data)
        r.raise_for_status()
        body = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OpticsUnavailable(f"{what}: {exc!r}") from exc
    if body.get("ErrorNumber"):
        number, message = body["ErrorNumber"], body.get("ErrorMessage")
        raise OpticsUnavailable(f"{what}: Alpaca error {number}: {message}")
    return body.get("Value")


def _read(client: httpx.Client, d: Device, members: list[str]) -> list[Any]:
    """Read ``members`` of ``d``; a device that reports itself not
    connected is connected for the reads and disconnected after."""
    connect = _alpaca(client, d, "Connected") is not True
    if connect:
        _alpaca(client, d, "Connected", "true")
    try:
        return [_alpaca(client, d, m) for m in members]
    finally:
        if connect:
            _alpaca(client, d, "Connected", "false")


def read_optics(telescope: Device, camera: Device, timeout: float = 5.0) -> dict[str, float]:
    """``{"plate_scale": arcsec per unbinned pixel, "focal_length": mm}``
    from the telescope's ``FocalLength`` (m) and the camera's ``PixelSizeX``
    and ``PixelSizeY`` (um); :class:`OpticsUnavailable` naming the device
    and member that cannot report one."""
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        (focal_m,) = _read(client, telescope, ["FocalLength"])
        px, py = _read(client, camera, ["PixelSizeX", "PixelSizeY"])
    what = f"Telescope {telescope.spec} ({telescope.name}) FocalLength"
    if not isinstance(focal_m, (int, float)) or not focal_m > 0:
        raise OpticsUnavailable(f"{what}: {focal_m!r} m, not a focal length")
    for name, v in (("PixelSizeX", px), ("PixelSizeY", py)):
        if not isinstance(v, (int, float)) or not v > 0:
            raise OpticsUnavailable(f"Camera {camera.spec} ({camera.name}) {name}: {v!r} um")
    if abs(px - py) > 1e-6 * max(px, py):
        raise OpticsUnavailable(
            f"Camera {camera.spec} ({camera.name}): pixels {px} x {py} um are not square;"
            " one plate scale cannot describe them"
        )
    focal_mm = float(focal_m) * 1000.0
    return {
        "plate_scale": round(ARCSEC_PER_UM_PER_MM * float(px) / focal_mm, 5),
        "focal_length": round(focal_mm, 3),
    }


def _partner(scope: Device, kind: str, devices: list[Device], alone: bool) -> Device | None:
    """The ``kind`` device for ``scope``: same server and number, else, for
    the only telescope (``alone``), the only one of its type; else None."""
    of_kind = [d for d in devices if d.type == kind]
    same = [d for d in of_kind if d.server == scope.server and d.number == scope.number]
    if same:
        return same[0]
    return of_kind[0] if alone and len(of_kind) == 1 else None


def starter_config(
    devices: list[Device], name: str = "observatory", *, optics: bool = False
) -> dict[str, Any]:
    """The starter enclosure config for ``devices`` (see the module doc);
    with ``optics``, each paired telescope's plate scale and focal length
    (:func:`read_optics`, which may raise :class:`OpticsUnavailable`)."""
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
    images: dict[str, Any] = {"data_root": "data", "site": name}
    if optics:
        by_spec = {(d.type, d.spec): d for d in devices}
        images["telescopes"] = {
            t: read_optics(
                by_spec["Telescope", entry["telescope"]], by_spec["Camera", entry["camera"]]
            )
            for t, entry in telescopes.items()
            if "camera" in entry
        }
    return {
        "_note": notes,
        "name": name,
        "listen": "127.0.0.1:8100",
        "telescopes": telescopes,
        "images": images,
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
        "--no-optics",
        action="store_true",
        help="with --write: do not read the plate scale from the telescope and camera",
    )
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
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
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
        try:
            config = starter_config(devices, args.name, optics=not args.no_optics)
        except OpticsUnavailable as exc:
            ap.error(f"{exc}; nothing written (--no-optics writes the config without it)")
        Path(args.write).write_text(json.dumps(config, indent=2) + "\n")
        print(f"wrote {args.write}")
        for line in config["_note"][2:]:
            print(f"  {line}")


if __name__ == "__main__":
    main()
