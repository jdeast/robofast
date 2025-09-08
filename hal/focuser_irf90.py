import telcom
import yaml

class Irf90Focus:
    def __init__(self, config):

        port = config["port"]
        self.telcom = telcom.Telcom(config["host"], config["port"])

    def status(self):
        pass

    def connect(self):
        """
        Connect to the focuser on the specified Nasmyth port (1 or 2).
        """
        return self.telcom.pwiRequestAndParse(device="focuser"+self.port, cmd="connect")

    def disconnect(self):
        """
        Disconnect from the focuser on the specified Nasmyth port (1 or 2).
        """
        return self.telcom.pwiRequestAndParse(device="focuser"+self.port, cmd="disconnect")

    def move(self, position):
        """
        Move the focuser to the specified position in microns
        """
        return self.telcom.pwiRequestAndParse(device="focuser"+self.port, cmd="move", position=position)

    def home(self):
        """
        home the focuser
        """
        return self.telcom.pwiRequestAndParse(device="focuser" + str(m3port), cmd="findhome")

    def stop(self):
        """
        Halt any motion on the focuser
        """
        return self.telcom.pwiRequestAndParse(device="focuser"+self.port, cmd="stop")


