import logging
import datetime
import requests


class Weather:

    def __init__(self,config_file):

        self.logger = logging.getLogger()
        self.rain_change_date = datetime.datetime.utcnow() - datetime.timedelta(hours=2.0)
        self.coldest_temp = 100.0
        self.last_rain = 0.0

        # make sure all required keys are present
        self.required_keys = ['totalRain', 'wxt510Rain', 'barometer', 'windGustSpeed',
                         'outsideHumidity', 'outsideDewPt', 'outsideTemp',
                         'windSpeed', 'windDirectionDegrees']
        #'date', 'sun_altitude','cloud_date', 'mearth_cloud', 'hat_cloud', 'aurora_cloud', 'minerva_cloud']

        # these belong somewhere else. dome? dome_daemon? another class?
        self.open_limit = config["open_limit"]
        self.close_limit = config["close_limit"]

        self.cloud_override = False
        self.sun_override = False
        self.timeout_override = False
        self.last_close = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        self.obs = ephem.Observer()
        self.obs.lat = ephem.degrees(str(self.latitude)) # N
        self.obs.lon = ephem.degrees(str(self.longitude)) # E
        self.obs.elevation = self.elevation # meters

        # this belongs in the higher level code
        self.mailSent = False

    def get_boltwood(self, weather=None):
        if weather is None:
            weather = {}

        # the URL for the machine readable weather page for the Ridge
        url = "http://linmax.sao.arizona.edu/weather/weather.cur_cond"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Python script)"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        lines = response.text.split('\n')

        if lines[0] == '':
            return weather

        # convert the date into a datetime object
        weather = {
            'date':datetime.datetime.strptime(lines[0],'%Y, %m, %d, %H, %M, %S, %f')}

        # populate the weather dictionary from the webpage
        for line in data[1:-1]:
            if "=" in line:
                key,val = line.split("=")
                weather[key,strip()] = float(val.strip())

        required_keys = ["outsideTemp", "windSpeed", "barometer"]

        missing = [k for k in required_keys if k not in weather]
        if missing:
            raise KeyError(f"Missing required keys: {missing}")

        return weather

    def get_sky_temps(self, weather=None):
        if weather is None:
            weather = {}

        url = 'http://linmax.sao.arizona.edu/temps/sky_temps_now'
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Python script)"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        cloudstr = response.text.split()
        weather['cloud_date'] = datetime.datetime.strptime(" ".join(cloudstr[0:2]),'%b-%d-%Y %H:%M:%S') + datetime.timedelta(hours=7)

        weather['mearth_cloud'] = float(cloudstr[2])
        if weather['mearth_cloud'] == 0.0: weather['mearth_cloud'] = 999
        weather['aurora_cloud'] = float(cloudstr[3])
        if weather['aurora_cloud'] == 0.0: weather['aurora_cloud'] = 999
        weather['minerva_cloud'] = float(cloudstr[4])
        if weather['minerva_cloud'] == 0.0: weather['minerva_cloud'] = 999
        weather['hat_cloud'] = 999

        # add in the Sun Altitude
        weather['sun_altitude'] = self.sunalt()

    def oktoopen(self, domeid, domeopen=False, ignoreSun=False):

        retval = True
        decisionFile = self.base_directory + '/manualDecision.txt'

        # get the current weather, timestamp, and Sun's position
        self.getWeather()
        while self.weather == -1:
            time.sleep(1)
            self.getWeather()

        # conditions have necessitated a manual decision to open the domes
        if os.path.exists(decisionFile):
            f = open(decisionFile,'r')
            try: date = datetime.datetime.strptime(f.readline().strip(),'%Y-%m-%d %H:%M:%S.%f')
            except: date = datetime.datetime.utcnow() - datetime.timedelta(days=1.1)
            f.close()

            if (datetime.datetime.utcnow() - date).total_seconds() > 86400.0:
                if not self.mailSent:
                    mail.send("Possible snow/ice on enclosures; manual inspection required",
                          "Dear benevolent humans,\n\n"+
                          "Recent conditions have been wet and cold (" + str(self.coldestTemp) + " C), which means ice and/or snow is likely. "+
                          "I have disabled operations until someone can check the camera (http://minervacam.sao.arizona.edu) "+
                          "to ensure there is no snow or ice on the roof and the snow is not more than 2 inches deep "+
                          "(which will stall the roof. There are presets on the camera for 'A1 Snow line' and 'A2 Snow line'. The you must "+
                          "be able to see the red line below the black line for it to be safe to open. If the snow on the ground "+
                          "is too deep, please email the site staff to ask them to shovel.\n\n"+
                          "If everything looks good, either delete the '/home/minerva/minerva-control/manualDecision.txt' file "+
                          "(if current conditions will not trip this warning again) or edit the date in that file to UTC now (" +
                          str(datetime.datetime.utcnow()) + "). Note that this warning will be tripped again 24 hours after the "+
                          "date in that file.\n\n"
                          "Love,\nMINERVA",level='serious')
                    self.mailSent = True
                self.logger.info("Not OK to open -- manual decision required")
                return False
        if self.mailSent:
            mail.send("Snow/ice conditions have been manually checked and OK'ed",
                  "Resuming normal operations",level='serious')
            self.mailSent = False

        # if it's open, use the limits to close
        if domeopen:
            self.logger.debug("Enclosure open; using the close limits")
            weatherLimits = copy.deepcopy(self.closeLimits)
        else:
            self.logger.debug("Enclosure _is_closed; using the open limits")
            weatherLimits = copy.deepcopy(self.openLimits)

        # change it during execution
        self.sunOverride = os.path.exists(self.base_directory + '/minerva_library/sunOverride.' + domeid + '.txt') or ignoreSun
        self.cloudOverride = os.path.exists(self.base_directory + '/minerva_library/cloudOverride.' + domeid + '.txt')
        self.timeoutOverride = os.path.exists(self.base_directory + '/minerva_library/timeoutOverride.' + domeid + '.txt')

        if self.sunOverride: weatherLimits['sunAltitude'] = [-90,90]
        else: weatherLimits['sunAltitude'] = [-90,6]

        if self.cloudOverride:
            weatherLimits['MearthCloud'] = [-999,999]
            weatherLimits['HATCloud'] = [-999,999]
            weatherLimits['AuroraCloud'] = [-999,999]
            weatherLimits['MINERVACloud'] = [-999,999]
        else:
            if domeopen:
                weatherLimits['MearthCloud'] = self.closeLimits['MearthCloud']
                weatherLimits['HATCloud'] = self.closeLimits['HATCloud']
                weatherLimits['AuroraCloud'] = self.closeLimits['AuroraCloud']
                weatherLimits['MINERVACloud'] = self.closeLimits['MINERVACloud']
            else:
                weatherLimits['MearthCloud'] = self.openLimits['MearthCloud']
                weatherLimits['HATCloud'] = self.openLimits['HATCloud']
                weatherLimits['AuroraCloud'] = self.openLimits['AuroraCloud']
                weatherLimits['MINERVACloud'] = self.openLimits['MINERVACloud']



        if weatherLimits['sunAltitude'][1] == 90:
            if not ignoreSun: self.logger.info("Sun override in place!")
        if self.closeLimits['sunAltitude'][1] == 90:
            self.logger.info("close limits have been modified; this shouldn't happen!")
        if self.openLimits['sunAltitude'][1] == 90:
            self.logger.info("open limits have been modified; this shouldn't happen!")

        # MearthCloud reports 998.0 when it's raining and is much more reliable than wxt510Rain
        if self.weather['MearthCloud'] == 998.0:
            self.lastRain += 0.001
            self.rainChangeDate = datetime.datetime.utcnow()

        # wxt510Rain uses an impact sensor and can be triggered by wind (unreliable)
#       if self.weather['wxt510Rain'] > self.lastRain:
#           self.lastRain = self.weather['wxt510Rain']
#           self.rainChangeDate = datetime.datetime.utcnow()

        # if it has rained in the last hour, it's not ok to open
        if (datetime.datetime.utcnow() - self.rainChangeDate).total_seconds() < 3600.0:
            self.logger.info('Not OK to open: it last rained at ' + str(self.rainChangeDate) + ", which is less than 1 hour ago")
            retval = False

        # if it has (or might have) snowed in the last 24 hours, we need manual approval to open
        if ((datetime.datetime.utcnow() - self.rainChangeDate).total_seconds() < 86400.0 and self.coldestTemp < 1.0) or os.path.exists(decisionFile):
            if os.path.exists(decisionFile):
                f = open(decisionFile,'r')
                date = datetime.datetime.strptime(f.readline().strip(),'%Y-%m-%d %H:%M:%S.%f')
                if (datetime.datetime.utcnow() - date).total_seconds() > 86400.0:
                    with open(decisionFile,"w") as fh:
                        fh.write(str(datetime.datetime.utcnow() - datetime.timedelta(days=1)))
                        self.logger.info('Not OK to open: there has been precipitation in the last 24 hours and it has been freezing. Manual inspection for snow/ice required')
                        return False
                else:
                    self.logger.info('There has been precipitation in the last 24 hours and it has been freezing, but it has been manually approved to open until ' + str(date))

            else:
                with open(decisionFile,"w") as fh:
                    fh.write(str(datetime.datetime.utcnow() - datetime.timedelta(days=1)))
                    self.logger.info('Not OK to open: there has been precipitation in the last 24 hours and it has been freezing. Manual inspection for snow/ice required')
                    return False

        #S External temperature check, want to use Mearth, then HAT if Mearth not available, and then
        #S Aurora, and finally MINERVA. Currently, we are assuming a value of 999 means disconnected
        #S for any of the four sensors.
        if self.weather['MearthCloud'] <> 999:
            key = 'MearthCloud'
            if self.weather[key] < weatherLimits[key][0] or self.weather[key] > weatherLimits[key][1]:
                self.logger.info('Not OK to open: ' + key + '=' + str(self.weather[key]) + '; Limits are ' + str(weatherLimits[key][0]) + ',' + str(weatherLimits[key][1]))
                retval = False
        elif self.weather['HATCloud'] <> 999:
            key = 'HATCloud'
            if self.weather[key] < weatherLimits[key][0] or self.weather[key] > weatherLimits[key][1]:
                self.logger.info('Not OK to open: ' + key + '=' + str(self.weather[key]) + '; Limits are ' + str(weatherLimits[key][0]) + ',' + str(weatherLimits[key][1]))
                retval = False
        elif self.weather['AuroraCloud'] <> 999:
            key = 'AuroraCloud'
            if self.weather[key] < weatherLimits[key][0] or self.weather[key] > weatherLimits[key][1]:
                self.logger.info('Not OK to open: ' + key + '=' + str(self.weather[key]) + '; Limits are ' + str(weatherLimits[key][0]) + ',' + str(weatherLimits[key][1]))
                retval = False
        elif self.weather['MINERVACloud'] <> 999:
            key = 'HATCloud'
            if self.weather[key] < weatherLimits[key][0] or self.weather[key] > weatherLimits[key][1]:
                self.logger.info('Not OK to open: ' + key + '=' + str(self.weather[key]) + '; Limits are ' + str(weatherLimits[key][0]) + ',' + str(weatherLimits[key][1]))
                retval = False
        else:
            self.logger.info('Not OK to open: all cloud sensors down')
            retval = False

        # make sure each parameter is within the limits for safe observing
        for key in weatherLimits:
            if 'Cloud' not in key and (self.weather[key] < weatherLimits[key][0] or self.weather[key] > weatherLimits[key][1]):
                keyname = key
                self.logger.info('Not OK to open: ' + keyname + '=' + str(self.weather[key]) + '; Limits are ' + str(weatherLimits[key][0]) + ',' + str(weatherLimits[key][1]))
                retval = False

        if retval: self.logger.debug('OK to open')
        return retval


    def sunrise(self, horizon=0, start=None):
        if start == None: start = self.startNightTime
        self.obs.horizon = str(horizon)
        sunrise = self.obs.next_rising(ephem.Sun(), start=start, use_center=True).datetime()
        return sunrise

    def sunset(self, horizon=0, start=None):
        if start == None: start = self.startNightTime
        self.obs.horizon = str(horizon)
        sunset = self.obs.next_setting(ephem.Sun(), start=start, use_center=True).datetime()
        return sunset

    def NautTwilBegin(self, horizon=-8):
        self.obs.horizon = str(horizon)
        NautTwilBegin = self.obs.next_rising(ephem.Sun(), start=self.startNightTime, use_center=True).datetime()
        return NautTwilBegin

    def NautTwilEnd(self, horizon=-12):
        self.obs.horizon = str(horizon)
        NautTwilEnd = self.obs.next_setting(ephem.Sun(), start=self.startNightTime, use_center=True).datetime()
        return NautTwilEnd

    def sunalt(self):

        self.obs.date = datetime.datetime.utcnow()
        sun = ephem.Sun()
        sun.compute(self.obs)
        return float(sun.alt)*180.0/math.pi

    def sunaz(self):

        self.obs.date = datetime.datetime.utcnow()
        sun = ephem.Sun()
        sun.compute(self.obs)
        return float(sun.az)*180.0/math.pi

if __name__ == '__main__':

    config_file =
