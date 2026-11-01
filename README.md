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
