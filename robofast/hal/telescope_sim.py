import time

class TelescopeSimulator:

    def __init__(self):
        self.is_connected = False
        self.is_home = False

    # connect the telescope
    def connect(self, time_to_connect=1, fail=False) -> bool:
        time.sleep(time_to_connect)
        self.is_connected = (not fail)
        return self.is_connected

    def is_connected(self) -> bool:
        return self.is_connected

    # home the telescope
    def home(self, time_to_home=10.0, fail=False) -> bool:
        time.sleep(time_to_home)
        self.is_home = (not fail)
        return self.is_home

    def get_azalt(self) -> Tuple[float,float]:
        return self.az, self.alt