"""Command line for the catalog importers (docs/catalogs.md).

::

    python -m robofast_core.catalogs import CATALOG [--to-requests] [-o OUT]
    python -m robofast_core.catalogs check CATALOG...
    python -m robofast_core.catalogs export FORM.json [-o OUT]

``import`` prints the intermediate form (JSON) of a ``.mct``, ``.off`` or
``.sec`` file; with ``--to-requests``, request groups for a ``.mct``
(``{"groups": [...]}``, loadable by the dispatch scheduler and the request
API), validated by :mod:`robofast_core.request_model` before they are
written. ``check`` only validates, printing warnings. ``export`` writes the
intermediate form back as a telshell catalog. Exit status 0 on success, 2 on
a refused catalog (the message names the file and line).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from robofast_core.catalogs import model, tres
from robofast_core.request_model import load_request_groups


def _write(text: str, out: str | None) -> None:
    if out:
        Path(out).write_text(text)
    else:
        sys.stdout.write(text)


def _import(args: argparse.Namespace) -> int:
    obj = tres.load(args.catalog)
    if isinstance(obj, model.TargetList) and args.private:
        obj.private = True
    for w in getattr(obj, "warnings", []):
        print(f"warning: {w}", file=sys.stderr)
    if not args.to_requests:
        _write(json.dumps(obj.to_dict(), indent=2) + "\n", args.output)
        return 0
    if isinstance(obj, model.PrivateCatalog):
        print(
            f"{args.catalog}: an encrypted catalog; robofast does not decrypt it. Have the "
            "program's P.I. export it as a .mct and import that with --private.",
            file=sys.stderr,
        )
        return 2
    if not isinstance(obj, model.TargetList):
        print(f"{args.catalog}: an offset catalog has no targets", file=sys.stderr)
        return 2
    groups = model.to_requests(
        obj,
        telescope=args.telescope,
        instrument=args.instrument,
        exposure_time=args.exposure_time,
        exposure_count=args.exposure_count,
        include_private=args.include_private,
    )
    load_request_groups(groups)  # refuse anything the request model would
    doc = {
        "_note": [
            f"Imported from TRES catalog {Path(args.catalog).name} by robofast_core.catalogs.",
            f"exposure_time {args.exposure_time} s x {args.exposure_count}: "
            "set per program; the catalog has none.",
        ],
        "groups": groups,
    }
    _write(json.dumps(doc, indent=2) + "\n", args.output)
    return 0


def _check(args: argparse.Namespace) -> int:
    status = 0
    for path in args.catalogs:
        try:
            obj = tres.load(path)
        except tres.CatalogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            status = 2
            continue
        for w in getattr(obj, "warnings", []):
            print(f"warning: {w}", file=sys.stderr)
        if isinstance(obj, model.TargetList):
            print(f"{path}: {len(obj.targets)} targets, program {obj.program.name!r}")
        elif isinstance(obj, model.OffsetPattern):
            rep = ", repeating" if obj.repeat else ""
            print(f"{path}: {len(obj.offsets)} offsets{rep}")
        else:
            print(f"{path}: sealed ({obj.size} bytes, container {obj.container})")
    return status


def _export(args: argparse.Namespace) -> int:
    obj = model.from_dict(json.loads(Path(args.form).read_text()))
    text = tres.write_off(obj) if isinstance(obj, model.OffsetPattern) else tres.write_mct(obj)
    _write(text, args.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m robofast_core.catalogs", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="a catalog to the intermediate form or requests")
    imp.add_argument("catalog")
    imp.add_argument("--to-requests", action="store_true", help="emit request groups")
    imp.add_argument("--telescope", default=model.DEFAULT_TELESCOPE)
    imp.add_argument("--instrument", default=model.DEFAULT_INSTRUMENT)
    imp.add_argument("--exposure-time", type=float, default=model.DEFAULT_EXPOSURE_TIME)
    imp.add_argument("--exposure-count", type=int, default=1)
    imp.add_argument("--private", action="store_true", help="mark the list private")
    imp.add_argument(
        "--include-private", action="store_true", help="emit requests for a private list"
    )
    imp.add_argument("-o", "--output")
    chk = sub.add_parser("check", help="validate catalogs")
    chk.add_argument("catalogs", nargs="+")
    exp = sub.add_parser("export", help="intermediate form back to .mct / .off")
    exp.add_argument("form")
    exp.add_argument("-o", "--output")
    args = ap.parse_args(argv)
    try:
        return {"import": _import, "check": _check, "export": _export}[args.cmd](args)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
