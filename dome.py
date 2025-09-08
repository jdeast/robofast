import importlib
import yaml
import logging
from pathlib import Path

from robofast import telescope

"""
This is a generic dome class that controls any dome via a custom hardware abstraction layer

This class is used by dome_daemon.py to independently control the dome, monitoring weather, site, and user inputs
"""


class DomeBase:

    def __init__(self, config_file):
        with open(config_file) as f:
            config = yaml.safe_load(f)

        self.logger = logging.getLogger()

        root_dir = Path(__file__).resolve().parent
        self.telescope = []
        for telescope_config_file_base in config["telescope"]:
            telescope_config_file = root_dir / "config" / telescope_config_file_base
            self.telescope.append(telescope.load_telescope(telescope_config_file))

    def get_header(self):
        self.header = {}
        self.header["DOMESTAT"] = (dome.status, "Dome status")


def load_dome(config_file):
    with open(config_file) as f:
        config = yaml.safe_load(f)

    pkg_name = __package__
    hal_module_name = config["hal_module"]
    hal_class_name = config["hal_class"]
    full_module_name = f"{pkg_name}.hal.{hal_module_name}"

    # import the hardware abstraction layer
    mod = importlib.import_module(full_module_name)
    hal_class = getattr(mod, hal_class_name)

    class Dome(DomeBase, hal_class):
        def __init__(self):
            DomeBase.__init__(self, config_file)
            hal_class.__init__(self, config_file)

    return Dome()
