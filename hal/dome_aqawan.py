import telnetlib3
import telnetlib
import yaml
import asyncio
import logging
import time

class Aqawan:

    def __init__(self, config_file):

        self.logger = logging.getLogger() 
        #self.logger = logging.getLogger(__name__) 


        with open(config_file) as f:
            config = yaml.safe_load(f)

        self.host = config["host"]
        self.port = config["port"]

        self.loop = asyncio.get_event_loop()

    async def _send(self, message):
        reader, writer = await telnetlib3.open_connection(
            host=self.host,
            port=self.port,
            shell=None,  # We control what gets sent
            term="vt100"
        )

        try:
            writer.write(message + "\r\n")
            await writer.drain()

            response = ""
            no_data_timeout = 0.5
            last_received = time.monotonic()

            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(256), timeout=0.1)
                    if chunk:
                        response += chunk
                        last_received = time.monotonic()
                except asyncio.TimeoutError:
                    if time.monotonic() - last_received > no_data_timeout:
                        break

            self.logger.debug("Received response: %r", response)
            return response

        finally:
            writer.close()

    def _status(self):
        requiredKeys = ['Shutter1', 'Shutter2', 'SWVersion', 'EnclHumidity',
                        'EntryDoor1', 'EntryDoor2', 'PanelDoor', 'Heartbeat',
                        'SystemUpTime', 'Fault', 'Error', 'PanelExhaustTemp',
                        'EnclTemp', 'EnclExhaustTemp', 'EnclIntakeTemp', 'LightsOn']

        response_raw = self.loop.run_until_complete(self._send('STATUS'))
        #self.logger.debug("Raw response:", repr(response_raw))

        response = response_raw.split(',')
        status = {}

        for entry in response:
            if '=' in entry:
                key, value = entry.split('=', 1)
                status[key.strip()] = value.strip()

        missing_keys = [k for k in requiredKeys if k not in status]
        if not missing_keys:
            return status

        return {}

    def _lights_off(self):
        response = self.loop.run_until_complete(self._send('LIGHTS_OFF'))
        if response == 'error':
            self.logger.error('Could not turn off lights')
        return response


        # close both shutters
    def _close_both(self):
        timeout = 500
        elapsedTime = 0

        response = self.loop.run_until_complete(self.send('CLOSE_SEQUENTIAL'))
        if 'Success=TRUE' not in response: return True
        return False

    def _open_shutter(self, shutter):

        # make sure this is an allowed shutter
        if shutter not in [1, 2]:
            self.logger.error('Invalid shutter specified (' + str(shutter) + ')')
            return False

        status = self.status()
        timeout = 180.0
        elapsedTime = 0.0

        # if it's already open, return
        if status['Shutter' + str(shutter)] == 'OPEN':
            self.logger.debug('Shutter ' + str(shutter) + ' already open')
            return

        # open the shutter
        start = datetime.datetime.utcnow()
        # response = asyncio.run(self.send('OPEN_SHUTTER_' + str(shutter)))
        # response = await self.send('OPEN_SHUTTER_' + str(shutter))
        response = self.loop.run_until_complete(self._send('OPEN_SHUTTER_' + str(shutter)))
        self.logger.info(response)

        if not 'Success = TRUE' in response:
            # did the command fail?
            self.logger.warning('Failed to open shutter ' +
                                str(shutter) +
                                ': ' +
                                response)

            # need to reset the PAC? ("Enclosure not in AUTO"?)
            if "Estop active" in response:
                if not self.mailsent:
                    mail.send("Aqawan " +
                              str(self.num) +
                              " Estop has been pressed!",
                              self.estopmail,
                              level='serious')
                    self.mailsent = True
            return -1

        # Wait for it to open
        self.logger.info('Waiting for shutter ' + str(shutter) + ' to open')
        status = self.status()
        while status['Shutter' +
                     str(shutter)] == 'OPENING' and elapsedTime < timeout:
            status = self.status()
            elapsedTime = (datetime.datetime.utcnow() - start).total_seconds()
            time.sleep(15.0)  # make sure we don't block heartbeats

        # Did it fail to open?
        if status['Shutter' + str(shutter)] != 'OPEN':
            self.logger.error('Error opening Shutter ' +
                              str(shutter) +
                              ', status=' +
                              status['Shutter' + str(shutter)])
            return -1

        self.logger.info('Shutter ' + str(shutter) + ' open')

    # open both shutters
    def _open_both(self, reverse=False):
        if reverse:
            first = 2
            second = 1

        else:
            first = 1
            second = 2

        response = self.loop.run_until_complete(self._send('LIGHTS_OFF'))
        if response == 'error':
            self.logger.error('Could not turn off lights')

        self.logger.debug('Opening shutter ' + str(first))
        response = self.open_shutter(first)
        if response == -1:
            return -1

        self.logger.debug('Shutter ' + str(first) + ' open')
        self.logger.debug('Opening shutter ' + str(second))
        response = self.open_shutter(second)
        if response == -1:
            return -1

        self.logger.debug('Shutter ' + str(second) + ' open')

    #### These low-level functions are required for the higher-level interface ###

    # identify if the dome is in an error state
    def in_error_state(self):
        aqawan_status = self._status()

    # a procedure to autonomously recover from an error state
    def recover(self):
        return False

    # is the dome ready to observe? (lights off, fans on, etc)
    def ready_to_observe(self):
        pass

    # prepare the dome for observing (turn lights off, fans on, etc)
    def prep_for_observing(self):
        aqawan_status = self._status()
        self._lights_off()

    # open the dome
    def open(self):
        pass

    # close the dome
    def close(self):
        self._close_both()

    # slave the dome to the telescope
    def slave(self):
        # aqawans are clamshells with no tracking necessary; it's always slaved
        return self.is_open()

    # return a standard status dictionary 
    def status(self):

        aqawan_status = self._status()

        status = {}
        status["open"] = (aqawan_status["Shutter1"] == "OPEN"  and aqawan_status["Shutter2"] == "OPEN")
        status["tracking"] = "Not Enabled"

        return status

    # get header keywords to capture dome status in image meta data
    def get_header(self):
        self.header = {}
        return self.header