import redis
import json
import time
import datetime
import logging

# Connect to Redis
r = redis.Redis(host='localhost', port=6379, db=0)

class weatherBase:
    def __init__(self):
        self.logger = logging.getLogger()

    def get_header(self):
        self.header = {}

def load_weather(config_file):
    with open(config_file) as f:
        config = yaml.safe_load(f)

    pkg_name = __package__ 
    hal_module_name = config["hal_module"]
    hal_class_name = config["hal_class"]
    full_module_name = f"{pkg_name}.hal.{hal_module_name}"

    # import the hardware abstraction layer
    mod = importlib.import_module(full_module_name)
    hal_class = getattr(mod, hal_class_name)

    class Weather(weatherBase, hal_class):
        def __init__(self):
            weatherBase.__init__(self)
            hal_class.__init__(self,config_file)

    return Dome()

# the daemon loop
def main(Weather):
    while True:
        try:
            weather = Weather.fetch_weather()

            # Store as JSON
            r.set("weather:latest", json.dumps(weather), ex=300)  # expire after 5 minutes

            # Optionally push to a list for history
            r.lpush("weather:history", json.dumps(weather))
            r.ltrim("weather:history", 0, 1000)  # keep only last 1000 entries

            print("Updated weather:", weather)
        except Exception as e:
            print("Weather update failed:", e)

        time.sleep(15)  # every 15 seconds


def fetch_weather():
    # For now, just wrap your existing weather getter
    weather = get_current_weather()

    # Add timestamp
    weather['timestamp'] = datetime.datetime.utcnow().isoformat()

    return weather

if __name__ == "__main__":
    Weather = load_weather(config_file)
    main(weather)