
import importlib
import yaml
import logging
from pathlib import Path

"""
This is a generic dome class that controls any dome via a custom hardware abstraction layer


This class is used by dome_daemon.py to independently control the dome, monitoring weather and user inputs
"""
class domeBase:

    def __init__(self):
        self.logger = logging.getLogger()

    def get_header(self):
        self.header = {}
        self.header["DOMESTAT"] = (dome.status,"Dome status")


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

    class Dome(domeBase, hal_class):
        def __init__(self):
            domeBase.__init__(self)
            hal_class.__init__(self,config_file)

    return Dome()
