import yaml
from pathlib import Path

from robofast import observer
from robofast import dome


class Observatory:

    def __init__(self, config_file):
        with open(config_file) as f:
            config = yaml.safe_load(f)

        self.obs = observer.Observer(config["observer"])
        self.directory = config["directory"]

        # create a dictionary of domes
        self.dome = {}
        dir = Path(__file__).resolve().parent / "config"
        for dome_config in config["dome"]:
            d = dome.load_dome(dir / dome_config, observer=self.obs, directory=self.directory)
            self.dome[d.id] = d

    def observe(self):
        pass
