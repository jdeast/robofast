robofast is open-source control software for robotic observatories. It drives
telescopes, enclosures, cameras and instruments as ASCOM Alpaca devices, and
runs a safety supervisor, a sequencer and a scheduler on top of them, so an
observatory can observe autonomously.

A new observatory describes itself in a site configuration (see `sites/` for
examples) and, where its hardware has no Alpaca driver yet, adds a device
server for it (see `robofast/devices/`); site-specific behavior plugs in
through `robofast.plugins` ([docs/plugins.md](docs/plugins.md)).

If you write code to support new hardware, please consider a pull request or sending
it to me so other users can use (and improve) it.

**robofast moves real hardware and is not safety-certified: read
[SAFETY.md](SAFETY.md) before deploying it.**

## First light

With a telescope and camera plugged into this computer (ASCOM Alpaca):

```
pip install robofast
python -m robofast --site LAT,LON --no-enclosure
```

finds them, reads their plate scale, writes the config, calibrates the
pointing from three plate solves and takes one plate-solved light frame:
[docs/first_light.md](docs/first_light.md).

## Install

robofast installs as a Python package (Python 3.12 or later):

```
python3.12 -m venv venv && . venv/bin/activate
pip install .                        # or: pip install ".[guider-hardware]" on a guide computer
python -c "import robofast"
robofast-supervisor --help           # every service and device server is a robofast-* command
```

Each `robofast-*` command (listed under `[project.scripts]` in
[pyproject.toml](pyproject.toml)) is the same program as its
`python -m robofast...` spelling, which also works uninstalled from
the repository root. For development and the tests, install the test
dependencies and run from the root (pip 25.1 or later):

```
pip install --group test
python -m pytest
```

The code this repository started from (the earlier, Windows-era `robofast/`
package) was removed on 2026-11-04; its last tree is the git tag
`legacy-robofast`.

To find the Alpaca devices on your network and write a starter config
for a first exposure (one bias), run `robofast-discover --write
observatory.json`; see [docs/discover.md](docs/discover.md).

To see robofast run a whole simulated MINERVA RV night
on your machine, see [docs/demo.md](docs/demo.md).

To deploy it on a control host (users, systemd units, secrets, firewall,
time, backups, upgrades), see [docs/deploy.md](docs/deploy.md); for alarm
notifications by email, chat or a pager hook, [docs/notifier.md](docs/notifier.md).

## License

BSD 3-Clause; see [LICENSE](LICENSE). The repository ships no star-catalog
data: Gaia DR3 data (ESA, CC BY-NC 3.0 IGO) are downloaded on first use,
from the Gaia archive or from robofast's Zenodo record; see
[docs/sky.md](docs/sky.md) ("What is downloaded, from where") and
[docs/zenodo.md](docs/zenodo.md).
