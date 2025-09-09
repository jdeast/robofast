import time
import sys

#from roboobs.hal import _telcom_minerva
#import _telcom_minerva

sys.dont_write_bytecode = True

import yaml


class Status:
    """
    Contains a node (and possible sub-nodes) in the parsed XML status tree.
    Properties are added to the class by the elementTreeToObject function.
    """

    def __str__(self):
        result = ""
        for k, v in self.__dict__.items():
            result += "%s: %s\n" % (k, str(v))

        return result


class Cdk700:
    def __init__(self, config):

        pass

    def _mountConnect(self):
        status = self.pwiRequestAndParse(device="mount", cmd="connect")
        if status == False: return False
        if status.mount.connected == 'False':
            # after a power cycle, this takes longer than PWI allows -- try again
            time.sleep(5.0)
            status = self.pwiRequestAndParse(device="mount", cmd="connect")
            if status.mount.connected == 'False':
                self.logger.error('Failed to connect to mount')
                return False
        return True

    def _mountEnableMotors(self):
        status = self.pwiRequestAndParse(device="mount", cmd="enable")
        if status.mount.azm_enabled == 'False' or status.mount.alt_enabled == 'False':
            # after a power cycle, this takes longer than PWI allows -- try again
            time.sleep(5.0)
            status = self.pwiRequestAndParse(device="mount", cmd="enable")
            if status.mount.azm_enabled == 'False' or status.mount.alt_enabled == 'False':
                self.logger.error('Failed to enable motors')
                return False

    def _mountDisconnect(self):
        return self.pwiRequestAndParse(device="mount", cmd="disconnect")

    def connect(self):

        telescopeStatus = self.getStatus()
        if telescopeStatus.mount.connected != 'True':
            self.logger.info('Connecting to mount')
            if not self._mountConnect(): return False
            time.sleep(10.00)
            telescopeStatus = self.getStatus()
