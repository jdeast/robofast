import importlib
import yaml
import logging
from pathlib import Path
from abc import ABC, abstractmethod

from robofast import telescope

"""
This is a generic dome class that controls any dome via a custom hardware abstraction layer

This class is used by dome_daemon.py to independently control the dome, monitoring weather, site, and user inputs
"""


class DomeBase(ABC):

    def __init__(self, config):

        self.logger = logging.getLogger()

        root_dir = Path(__file__).resolve().parent

        # create a list (dictionary?) of telescopes
        self.telescope = []
        for telescope_config_basename in config["telescope"]:
            telescope_config_file = root_dir / "config" / telescope_config_basename
            self.telescope.append(telescope.load_telescope(telescope_config_file))

    @abstractmethod
    def open(self): pass

    @abstractmethod
    def close(self): pass

    @abstractmethod
    def add_header_keys(self, f): pass

    @abstractmethod
    def heartbeat(self): pass

    @abstractmethod
    def prep_for_observing(self): pass

    @abstractmethod
    def slave(self): pass

    @abstractmethod
    def recover(self): pass

    @abstractmethod
    def is_open(self): pass

    @abstractmethod
    def is_closed(self): pass

    @property
    @abstractmethod
    def ready_to_observe(self): pass

    @property
    @abstractmethod
    def in_error_state(self): pass


def load_dome(config_file):
    with open(config_file) as f:
        config = yaml.safe_load(f)

    pkg_name = __package__
    hal_module_name = config["hal_module"]
    hal_class_name = config["hal_class"]
    full_module_name = f"{pkg_name}.hal.{hal_module_name}"

    # import the hardware abstraction layer
    hal_module = importlib.import_module(full_module_name)
    hal_class = getattr(hal_module, hal_class_name)

    class Dome(DomeBase, hal_class):
        def __init__(self):
            self._hal = hal_class(config)
            DomeBase.__init__(self, config)
            hal_class.__init__(self, config)

        # normally these definitions would be handled by ABC,
        # but the dynamically loaded class causes problems
        # we must write our own pass-through definitions here
        # for required functions.
        #
        # We can also wrap them in general logging and error handling
        def open(self):
            if not self.is_open:
                self.logger.info("Opening the dome")
                return self._hal.open()
            else:
                self.logger.info("Dome already open")

        def close(self):
            return self._hal.close()

        def add_header_keys(self, hdr):
            # add generic dome keywords

            # add hardware specific dome keywords
            hdr = self._hal.add_header_keys(hdr)
            return hdr

        def heartbeat(self):
            return self._hal.heartbeat()

        def prep_for_observing(self):
            return self._hal.prep_for_observing()

        def slave(self):
            return self._hal.slave()

        def recover(self):
            return self._hal.recover()

        @property
        def is_open(self):
            return self._hal.is_open

        @property
        def is_closed(self):
            return self._hal.is_closed

        @property
        def ready_to_observe(self):
            return self._hal.ready_to_observe

        @property
        def in_error_state(self):
            return self._hal.in_error_state

    return Dome()
