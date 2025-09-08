class Irf90Rotator:
    def __init__(self, config):
        pass


    def initialize(self,tracking=True, derotate=True):

        telescopeStatus = self.getStatus()

        # connect to the mount if not connected
        if telescopeStatus.mount.connected != 'True':
            self.logger.info('Connecting to mount')
            if not self.mountConnect(): return False
            time.sleep(10.00)
            telescopeStatus = self.getStatus()

        # enable motors if not enabled
        if telescopeStatus.mount.alt_enabled != 'True' or telescopeStatus.mount.azm_enabled != 'True':
            self.logger.info('Enabling motors')
            if not self.mountEnableMotors(): return False
            time.sleep(5.25)
            telescopeStatus = self.getStatus()

        # connect to the focuser if not connected
        if telescopeStatus.focuser1.connected != 'True' or telescopeStatus.rotator1.connected != 'True':
            self.logger.info('Connecting to focuser')
            if not self.focuserConnect('1'): return False
            time.sleep(5.25)
            telescopeStatus = self.getStatus()

        # connect to the focuser if not connected
        if telescopeStatus.focuser2.connected != 'True' or telescopeStatus.rotator2.connected != 'True':
            self.logger.info('Connecting to focuser')
            if not self.focuserConnect('2'): return False
            time.sleep(5.25)
            telescopeStatus = self.getStatus()

        # reload the pointing model
        self.logger.info('re-loading pointing model for the current port')
        m3port = telescopeStatus.m3.port
        self.m3port_switch(m3port,force=True)
        telescopeStatus = self.getStatus()

        # turning on/off mount tracking, rotator tracking if not already on/off
        if tracking:
            if telescopeStatus.mount.tracking != 'True':
                self.logger.info('Turning mount tracking on')
                self.mountTrackingOn()
        else:
            if telescopeStatus.mount.tracking != 'False':
                self.logger.info('Turning mount tracking off')
                self.mountTrackingOff()

        if derotate:
            if telescopeStatus.rotator.altaz_derotate <> 'True':
                self.logger.info('Turning rotator tracking on')
                self.rotatorStartDerotating(m3port)
        else:
            if telescopeStatus.rotator.altaz_derotate <> 'False':
                self.logger.info('Turning rotator tracking off')
                self.rotatorStopDerotating(m3port)

        return self.isInitialized(tracking=tracking,derotate=derotate)