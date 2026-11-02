This code is (will be) a generic robotic observatory code that autonomously executes 
astronomical observations. 

New observatories must write new configuration files (see config/* for examples) 
and Hardware Abstraction Layer (HAL) classes (see hal/*.py for examples) that 
translate the specific hardware (e.g., Paramount, Cdk700) into generic commands 
(slew). As such, these HAL classes must expose standardized function names, which
are enforced by the high-level wrappers (e.g., telescope.py). See existing codes 
for examples.

After that, they can take advantage of the generalized robotic code and all of its 
infrastructure.

If you write code to support new hardware, please consider a pull request or sending
it to me so other users can use (and improve) it.

## Install

The new core, `robofast_core/`, is the installable package (distribution
name `robofast`, Python 3.12 or later):

```
python3.12 -m venv venv && . venv/bin/activate
pip install .                        # or: pip install ".[guider-hardware]" on a guide computer
python -c "import robofast_core"
robofast-supervisor --help           # every service and device server is a robofast-* command
```

Each `robofast-*` command (listed under `[project.scripts]` in
[pyproject.toml](pyproject.toml)) is the same program as its
`python -m robofast_core...` spelling, which also works uninstalled from
the repository root. For development and the tests, install the test
dependencies and run from the root (pip 25.1 or later):

```
pip install --group test
python -m pytest
```

The legacy `robofast/` directory (the code this README's first paragraphs
describe) stays in the repository for reference; it is not packaged or
installed.

To see the new core (`robofast_core/`) run a whole simulated MINERVA RV night
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
