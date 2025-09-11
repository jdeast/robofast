import yaml
import ephem


class Observer:

    def __init__(self, config_file):
        with open(config_file) as f:
            config = yaml.safe_load(f)

        self.obs = ephem.Observer()
        self.obs.lat = config['latitude']
        self.obs.lon = config['longitude']
        self.obs.elevation = config['elevation']
