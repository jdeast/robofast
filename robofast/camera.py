import importlib
import yaml
import logging
from pathlib import Path
from abc import ABC, abstractmethod
import datetime
import time
import numpy as np
import math

# this is more for the camera simulator
from astroquery.vizier import Vizier

# local imports
#import filterwheel, focuser, ao, pdu

class CameraBase(ABC):

    def __init__(self, config):

        self.logger = logging.getLogger()

        # load other hardware
        #self.filterwheel = filterwheel.load_filterwheel(config['filterwheel'])
        #self.focuser = focuser.load_focuser(config['focuser'])
        #self.ao = ao.load_ao(config['ao'])
        #self.pdu = pdu.load_pdu(config['pdu'])

        root_dir = Path(__file__).resolve().parent

    def starcat(self, ra, dec, width, height):
        result = Vizier.query_region(coord.SkyCoord(ra=ra, dec=dec,unit=(u.deg, u.deg),frame='icrs'),width="30m",catalog=["I/337/gaia"])
        ra = result[0]['RA_ICRS']
        dec = result[0]['DE_ICRS']
        mag = result[0]['__Gmag_']
        x = 0
        y = 0
        flux = 10**(-0.4*mag)

    '''                                                                                                                                                       
    this creates a simple simulated image of a star field                                                                                                     
    the idea is to be able to test guide performance, acquisition, etc, without being on sky                                                                                     
    x -- an array of X centroids of the stars (only integers tested!)                                                                                         
    y -- an array of Y centroids of the stars (only integers tested!)                                                                                         
    flux -- an array of fluxes of the stars (e-)                                                                                                       
    fwhm -- the fwhm of the stars (arcsec)                                                                                                                    
    background -- the sky background of the image, in ADU
    exptime -- the exposure time, in seconds. This will be written to the header, but will not impact the runtime unless wait=True
    noise -- readnoise of the image, in ADU
    wait -- boolean; wait for exptime to elapse
    '''
    def simulate_star_image(self,x,y,flux,fwhm=1.0,background=300.0,exptime=1.0,noise=10.0, wait=False, ra=None, dec=None):

        t0 = datetime.datetime.utcnow()
        self.exptime = exptime
        self.dateobs = datetime.datetime.utcnow()

        # query a catalog to simulate a star field
        if ra !=None and dec != None:
                pass

        xwidth = self.x2-self.x1
        ywidth = self.y2-self.y1
        image = np.zeros((ywidth,xwidth),dtype=np.float64) + background + np.random.normal(scale=noise,size=(ywidth,xwidth))

        # add a guide star?
        sigma = fwhm/self.platescale
        mu = 0.0
        boxsize = math.ceil(sigma*10.0)

        # make sure it's even to make the indices/centroids come out right
        if boxsize % 2 == 1: boxsize+=1

        xgrid,ygrid = np.meshgrid(np.linspace(-boxsize,boxsize,2*boxsize+1), np.linspace(-boxsize,boxsize,2*boxsize+1))
        d = np.sqrt(xgrid*xgrid+ygrid*ygrid)
        g = np.exp(-( (d-mu)**2 / ( 2.0 * sigma**2 ) ) )
        g = g/np.sum(g) # normalize the gaussian

        # add each of the stars
        for ii in range(len(x)):

            xii = x[ii]-self.x1+1
            yii = y[ii]-self.y1+1

            # make sure the stamp fits on the image (if not, truncate the stamp)
            if xii >= boxsize:
                x1 = xii-boxsize
                x1stamp = 0
            else:
                x1 = 0
                x1stamp = boxsize-xii
            if xii <= (xwidth-boxsize):
                x2 = xii+boxsize+1
                x2stamp = 2*boxsize+1
            else:
                x2 = xwidth
                x2stamp = xwidth - xii + boxsize
            if yii >= boxsize:
                y1 = yii-boxsize
                y1stamp = 0
            else:
                y1 = 0
                y1stamp = boxsize-yii
            if yii <= (ywidth-boxsize):
                y2 = yii+boxsize+1
                y2stamp = 2*boxsize+1
            else:
                y2 = ywidth
                y2stamp = ywidth - yii + boxsize

            if (y2-y1) > 0 and (x2-x1) > 0:
                # normalize the star to desired flux
                star = g[y1stamp:y2stamp,x1stamp:x2stamp]*flux[ii]

                # add Poisson noise; convert to ADU
                noise = np.random.normal(size=(y2stamp-y1stamp,x2stamp-x1stamp))
                noisystar = (star + np.sqrt(star)*noise)/self.gain

                # add the star to the image
                image[y1:y2,x1:x2] += noisystar
            else: self.logger.warning("star off image (" + str(xii) + "," + str(yii) + "); ignoring")

            # simulate the exposure time, too
            if wait:
                    sleeptime = (datetime.datetime.utcnow() - t0).total_seconds() - exptime
                    if sleeptime > 0: time.sleep(sleeptime)

            # now convert to 16 bit int
            self.image = image.astype(np.int16)
            self.ready = True

    def cool(self, temp=None, wait=False, settleTime=1200.0, oscillationTime=120.0, maxdiff = 1.0):
        if not self.camera.CanSetCCDTemperature:
            self.logger.error("Camera does not support cooling")
            return False

        if temp != None:
            self.camera.SetCCDTemperature = temp

        self.camera.CoolerOn = True

        if not wait: return

        t0 = datetime.datetime.utcnow()
        elapsedTime = (datetime.datetime.utcnow() - t0).total_seconds()
        lastTimeNotAtTemp = datetime.datetime.utcnow() - datetime.timedelta(seconds=oscillationTime)
        elapsedTimeAtTemp = oscillationTime
        currentTemp = self.camera.CCDTemperature
        setTemp = self.camera.SetCCDTemperature

        while elapsedTime < settleTime and ((abs(setTemp - currentTemp) > maxdiff) or elapsedTimeAtTemp < oscillationTime):
            self.logger.info('Current temperature (' + str(currentTemp) +
                             ') not at setpoint (' + str(setTemp) +
                             '); waiting for CCD Temperature to stabilize (Elapsed time: '
                             + str(elapsedTime) + ' seconds)')

            # has to maintain temp within range for 1 minute
            if (abs(setTemp - currentTemp) > self.maxdiff):
                                lastTimeNotAtTemp = datetime.datetime.utcnow()
            elapsedTimeAtTemp = (datetime.datetime.utcnow() - lastTimeNotAtTemp).total_seconds()

            time.sleep(10)
            #S update the temperature
            currentTemp = self.temperature
            elapsedTime = (datetime.datetime.utcnow() - start).total_seconds()

        # Failed to reach setpoint
        if (abs(setTemp - currentTemp)) > maxdiff:
            self.logger.error('The camera was unable to reach its setpoint (' +
                              str(setTemp) + ') in the elapsed time (' +
                              str(elapsedTime) + ' seconds)')
            return False

        return True

def load_camera(config_file):
    with open(config_file) as f:
        config = yaml.safe_load(f)

    pkg_name = __package__
    hal_module_name = config["hal_module"]
    hal_class_name = config["hal_class"]
    full_module_name = f"{pkg_name}.hal.{hal_module_name}"

    # import the hardware abstraction layer
    hal_module = importlib.import_module(full_module_name)
    hal_class = getattr(hal_module, hal_class_name)

    class Camera(CameraBase, hal_class):
        def __init__(self):
            self._hal = hal_class(config)
            CameraBase.__init__(self, config)
            hal_class.__init__(self, config)

        def get_header_keys(self,hdr=None):

            if hdr is None: hdr = fits.Header()
            hdr['DATE-OBS'] = (self.dateobs.strftime('%Y-%m-%dT%H:%M:%S.%f'), 'Observation start, UTC')
            hdr['EXPTIME'] = (self.exptime, 'Exposure time in seconds')
            hdr['CCDSUM'] = (str(self.xbin) + ' ' + str(self.ybin), 'CCD on-chip binning')
            datasec = '[' + str(self.x1) + ':' + str(self.x2) + ',' + str(self.y1) + ':' + str(self.y2) + ']'
            hdr['DATASEC'] = (datasec, 'Region of CCD read')
            hdr['CCDTEMP'] = (self.temperature, 'CCD Temperature (C)')
            hdr['SETTEMP'] = (self.set_temperature, 'CCD Set Temperature (C)')

            # now get hardward-specific header keywords
            hdr = self._hal.get_header_keys(hdr)
            return hdr

    return Camera()
