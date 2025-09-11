import logging
from pathlib import Path
import win32com.client
import win32api
from astropy.io import fits
import datetime
import time
import ipdb
import atexit

# local imports
from robofast import camera


class AscomCamera:

    def __init__(self, config, driver=None):

        self.logger = logging.getLogger()
        self.host = config['host']
        self.port = int(config['port'])

        self.header_buffer = ''
        self.gain = config['gain']
        self.platescale = config['platescale']

        self.x1 = None
        self.x2 = None
        self.y1 = None
        self.y2 = None

        # if you don't know what your driver is called, use the ASCOM Chooser
        # this will give you a GUI to select it
        if config["driver"] is None:
            x = win32com.client.Dispatch("ASCOM.Utilities.Chooser")
            x.DeviceType = 'Camera'
            driver = x.Choose(None)
            self.logger.info("The driver is " + driver)
            config["driver"] = driver

        # initialize the camera
        try:
            self._driver = win32com.client.Dispatch(driver)
        except:
            x = win32com.client.Dispatch("ASCOM.Utilities.Chooser")
            x.DeviceType = 'Camera'
            driver = x.Choose(None)
            self.logger.info("The driver is " + driver)

        self._driver = win32com.client.Dispatch(driver)

        win32api.SetConsoleCtrlHandler(self.safe_close, True)
        atexit.register(self.safe_close, 'signal_argument')

    def __getattr__(self, name):
        """
        Delegate attribute/method access to the COM object
        if it's not found on Camera itself.
        """
        return getattr(self._driver, name)

    def power_on(self):
        self.pdu.on()

    def power_off(self):
        self.pdu.off()

    def power_cycle(self, downtime=30):
        self.power_off()
        time.sleep(downtime)
        self.power_on()

    def initialize(self):
        self.connect()
        self.cool()
        self.set_roi(full_frame=True)
        self.set_bin(1)

    def connect(self):
        self._driver.connected = True
        return self._driver.connected

    def disconnect(self):
        self._driver.connected = False
        return not self._driver.connected

    # if we don't disconnect before exiting Python,
    # it crashes the DLL and we need to power cycle the camera
    def safe_close(self, signal):
        self.logger.info("Disconnecting before exit")
        self.disconnect()

    @property
    def temperature(self):
        return self._driver.CCDTemperature

    def set_bin(self, xbin, ybin=None):
        if ybin is None:
            ybin = xbin

        if xbin != ybin:
            if not self._driver.CanAsymmetricBin:
                self.logger.error('The camera cannot bin asymmetrically')
                return False

        self._driver.BinX = xbin
        self._driver.BinY = ybin
        return True

    def set_roi(self, x1=None, x2=None, y1=None, y2=None, full_frame=False):

        if full_frame:
            x1 = 1
            x2 = self._driver.CameraXSize
            y1 = 1
            y2 = self._driver.CameraYSize

        if x1 is not None:
            self._driver.StartX = x1
        else:
            x1 = self._driver.StartX
        self.x1 = x1

        if x2 is not None:
            self._driver.NumX = x2-x1
        else:
            x2 = self._driver.StartX+x1
        self.x2 = x2

        if y1 is not None:
            self._driver.StartY = y1
        else:
            y1 = self._driver.StartY
        self.y1 = y1

        if y2 is not None:
            self._driver.NumY = y2-y1
        else:
            y2 = self._driver.StartY+y1
        self.y2 = y2
        return True

    def get_header_keys(self, hdr):

        ''' get hardware-specific camera header keywords '''


        # hdr['DATE-OBS'] = (self.dateobs.strftime('%Y-%m-%dT%H:%M:%S.%f'), 'Observation start, UTC')
        # hdr['EXPTIME'] = (self.exptime, 'Exposure time in seconds')
        # hdr['CCDSUM'] = (str(self.xbin) + ' ' + str(self.ybin), 'CCD on-chip binning')
        # datasec = '[' + str(self.x1) + ':' + str(self.x2) + ',' + str(self.y1) + ':' + str(self.y2) + ']'
        # hdr['DATASEC'] = (datasec, 'Region of CCD read')
        # hdr['CCDTEMP'] = (self._driver.CCDTemperature, 'CCD Temperature (C)')
        # hdr['SETTEMP'] = (self._driver.SetCCDTemperature, 'CCD Set Temperature (C)')
        # hdr['GAIN'] = (self.gain, 'Gain (e/ADU)')

        return hdr

    def save_image(self, filename, timeout=10, hdr=None, overwrite=False):

        t0 = datetime.datetime.utcnow()
        elapsed_time = (t0 - datetime.datetime.utcnow()).total_seconds()
        while elapsed_time < timeout and not self._driver.ImageReady and not self.ready:
            time.sleep(0.05)
            elapsed_time = (t0 - datetime.datetime.utcnow()).total_seconds()
        if not self._driver.ImageReady and not self.ready:
            return False

        hdr = self.get_header_keys(hdr)

        if self.image is not None:
            # this is a simulated image
            hdu = fits.PrimaryHDU(self.image, header=hdr)
            self.image = None
            self.ready = False
        else:
            hdu = fits.PrimaryHDU(self._driver.ImageArray, header=hdr)

        hdu.writeto(filename, overwrite=overwrite)

        return True

    def expose(self, exptime, open_shutter=True):
        self.exptime = exptime
        self.dateobs = datetime.datetime.utcnow()
        self._driver.StartExposure(exptime, open_shutter)


if __name__ == '__main__':
    root_dir = Path(__file__).resolve().parent.parent
    config_file = root_dir / "config" / "camera_apogee1.yaml"
    c = camera.load_camera(config_file)
    ipdb.set_trace()
