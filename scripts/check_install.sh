#!/usr/bin/env bash
# Check that robofast installs as a package: `pip install .` into a fresh
# venv, then import robofast_core and run a console script, from outside the
# checkout so nothing is picked up from the source tree. CI runs this
# (.github/workflows/tests.yml, job `install`); run it locally the same way:
#
#   PYTHON=python3.12 scripts/check_install.sh /path/to/scratch-venv
set -euo pipefail
venv=${1:?usage: check_install.sh VENV_DIR}
repo=$(cd "$(dirname "$0")/.." && pwd)
rm -rf "$venv"
"${PYTHON:-python3}" -m venv "$venv"
"$venv/bin/python" -m pip install --quiet --upgrade pip
"$venv/bin/python" -m pip install --quiet "$repo"
cd "$venv"
"$venv/bin/python" -c "import robofast_core, sys; print(robofast_core.__file__); sys.exit('robofast_core imported from the checkout' if robofast_core.__file__.startswith('$repo/') else 0)"
"$venv/bin/python" -c "import robofast" 2>/dev/null && { echo "legacy robofast/ was installed" >&2; exit 1; }
"$venv/bin/robofast-supervisor" --help >/dev/null
"$venv/bin/robofast-weather-bridge" --help >/dev/null
echo "install OK"
