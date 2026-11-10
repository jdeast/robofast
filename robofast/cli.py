"""``robofast`` and ``python -m robofast``: the subcommands.

::

    robofast                   # this help: the subcommands and the services
    robofast first-light ...   # robofast.first_light (docs/first_light.md)

Nothing runs without a subcommand: first light moves a telescope, so it is
asked for by name. The services keep their own commands
(``robofast-supervisor``, ``robofast-sequencer``, ...: pyproject.toml).
"""

from __future__ import annotations

import importlib
import sys

#: name -> (module with main(argv), one line)
COMMANDS = {
    "first-light": (
        "robofast.first_light",
        "from a telescope plugged into this computer to a solved image",
    ),
}

HELP = """usage: robofast COMMAND [ARGS...]

commands:
{commands}

`robofast COMMAND --help` for a command's options. The services are their own
commands: robofast-supervisor, robofast-sequencer, robofast-discover, ...
(docs/design.md).
"""


def _help() -> str:
    width = max(len(n) for n in COMMANDS)
    lines = [f"  {n:<{width}}  {line}" for n, (_, line) in COMMANDS.items()]
    return HELP.format(commands="\n".join(lines))


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_help(), end="")
        return
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        print(f"robofast: unknown command {name!r}\n\n{_help()}", end="", file=sys.stderr)
        sys.exit(2)
    importlib.import_module(COMMANDS[name][0]).main(rest)


if __name__ == "__main__":
    main()
