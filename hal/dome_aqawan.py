import telnetlib3
import asyncio
import logging
import time
import datetime

#from robofast import mail

class Aqawan:

    def __init__(self, config):

        self.logger = logging.getLogger()
        # self.logger = logging.getLogger(__name__)

        self.host = config["host"]
        self.port = config["port"]

        self.id = config["id"]
        self.num = str(config["num"])

        self._is_closed = True

        self.loop = asyncio.get_event_loop()

        self.allowed_messages = ['HEARTBEAT', 'STOP', 'OPEN_SHUTTERS', 'CLOSE_SHUTTERS',
                                 'CLOSE_SEQUENTIAL', 'OPEN_SHUTTER_1', 'CLOSE_SHUTTER_1',
                                 'OPEN_SHUTTER_2', 'CLOSE_SHUTTER_2', 'LIGHTS_ON',
                                 'LIGHTS_OFF', 'ENC_FANS_HI', 'ENC_FANS_MED',
                                 'ENC_FANS_LOW', 'ENC_FANS_OFF', 'PANEL_LED_GREEN',
                                 'PANEL_LED_YELLOW', 'PANEL_LED_RED', 'PANEL_LED_OFF',
                                 'DOOR_LED_GREEN', 'DOOR_LED_YELLOW', 'DOOR_LED_RED',
                                 'DOOR_LED_OFF', 'SON_ALERT_ON', 'SON_ALERT_OFF',
                                 'LED_STEADY', 'LED_BLINK', 'MCB_RESET_POLE_FANS',
                                 'MCB_RESET_TAIL_FANS', 'MCB_RESET_OTA_BLOWER',
                                 'MCB_RESET_PANEL_FANS', 'MCB_TRIP_POLE_FANS',
                                 'MCB_TRIP_TAIL_FANS', 'MCB_TRIP_PANEL_FANS',
                                 'STATUS', 'GET_ERRORS', 'GET_FAULTS', 'CLEAR_ERRORS',
                                 'CLEAR_FAULTS', 'RESET_PAC']

    async def _send(self, message):
        if message not in self.allowed_messages:
            self.logger.error("Command " + message + " not allowed")
            return ""

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

    def _get_errors(self):
        response = self.loop.run_until_complete(self._send('GET_ERRORS'))
        return response

    def _clear_errors(self):
        response = self.loop.run_until_complete(self._send('CLEAR_ERRORS'))
        return response

    def _get_faults(self):
        response = self.loop.run_until_complete(self._send('GET_FAULTS'))
        return response

    def _clear_faults(self):
        response = self.loop.run_until_complete(self._send('CLEAR_FAULTS'))
        return response

    def _status(self):
        required_keys = ['Shutter1', 'Shutter2', 'SWVersion', 'EnclHumidity',
                         'EntryDoor1', 'EntryDoor2', 'PanelDoor', 'Heartbeat',
                         'SystemUpTime', 'Fault', 'Error', 'PanelExhaustTemp',
                         'EnclTemp', 'EnclExhaustTemp', 'EnclIntakeTemp', 'LightsOn']

        response_raw = self.loop.run_until_complete(self._send('STATUS'))
        # self.logger.debug("Raw response:", repr(response_raw))

        response = response_raw.split(',')
        status = {}

        for entry in response:
            if '=' in entry:
                key, value = entry.split('=', 1)
                status[key.strip()] = value.strip()

        missing_keys = [k for k in required_keys if k not in status]
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

        response = self.loop.run_until_complete(self._send('CLOSE_SEQUENTIAL'))
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
        response = self.loop.run_until_complete(self._send('OPEN_SHUTTER_' + str(shutter)))
        self.logger.info(response)

        if 'Success = TRUE' not in response:
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
        response = self._open_shutter(first)
        if response == -1:
            return -1

        self.logger.debug('Shutter ' + str(first) + ' open')
        self.logger.debug('Opening shutter ' + str(second))
        response = self._open_shutter(second)
        if response == -1:
            return -1

        self.logger.debug('Shutter ' + str(second) + ' open')

    """ These low-level functions are required for the higher-level interface """

    # open the dome
    def open(self):
        status = self.status()
        self._is_closed = not status["open"]

    # close the dome
    def close(self):
        self._close_both()
        status = self.status()
        self._is_closed = not status["open"]

    def heartbeat(self):
        return self.loop.run_until_complete(self._send('HEARTBEAT'))

    def add_header_keys(self, hdr):
        """ add enclosure specific keywords """
        status = self._status()
        hdr['AQSOFTV' + self.num] = (status['SWVersion'], "Aqawan software version number")
        hdr['AQSHUT1' + self.num] = (status['Shutter1'], "Aqawan shutter 1 state")
        hdr['AQSHUT2' + self.num] = (status['Shutter2'], "Aqawan shutter 2 state")
        hdr['INHUMID' + self.num] = (float(status['EnclHumidity']), "Humidity inside enclosure")
        hdr['DOOR1' + self.num] = (status['EntryDoor1'], "Door 1 into aqawan state")
        hdr['DOOR2' + self.num] = (status['EntryDoor2'], "Door 2 into aqawan state")
        hdr['PANELDR' + self.num] = (status['PanelDoor'], "Aqawan control panel door state")
        hdr['HRTBEAT' + self.num] = (int(status['Heartbeat']), "Heartbeat timer")
        hdr['AQPACUP' + self.num] = (status['SystemUpTime'], "PAC uptime (seconds)")
        hdr['AQFAULT' + self.num] = (status['Fault'], "Aqawan fault present?")
        hdr['AQERROR' + self.num] = (status['Error'], "Aqawan error present?")
        hdr['PANLTMP' + self.num] = (float(status['PanelExhaustTemp']), "Aqawan control panel exhaust temp (C)")
        hdr['AQTEMP' + self.num] = (float(status['EnclTemp']), "Enclosure temperature (C)")
        hdr['AQEXTMP' + self.num] = (float(status['EnclExhaustTemp']), "Enclosure exhaust temperature (C)")
        hdr['AQINTMP' + self.num] = (float(status['EnclIntakeTemp']), "Enclosure intake temperature (C)")
        hdr['AQLITON' + self.num] = (status['LightsOn'], "Aqawan lights on?")
        return hdr

    # prepare the dome for observing (turn lights off, fans on, etc)
    def prep_for_observing(self):
        # aqawan_status = self._status()
        self._lights_off()
        # return not self.in_error_state

    # slave the dome to the telescope
    def slave(self):
        # aqawans are clamshells with no tracking necessary; it's always slaved
        return self.is_open

    # a procedure to autonomously recover from an error state
    def recover(self):
        return False

    @property
    def is_open(self):
        return not self._is_closed

    @property
    def is_closed(self):
        self.logger.info(str(self._is_closed))
        return self._is_closed

    # is the dome ready to observe? (lights off, fans on, etc)
    @property
    def ready_to_observe(self):
        return True

    # identify if the dome is in an error state
    @property
    def in_error_state(self):
        status = self._status()
        return (status['Fault'] == "TRUE") or (status['Error'] == "TRUE")

    # return a standard status dictionary 
    def status(self):

        aqawan_status = self._status()

        status = {"open": (aqawan_status["Shutter1"] == "OPEN" and aqawan_status["Shutter2"] == "OPEN"),
                  "tracking": "Not Enabled"}

        return status

    # get header keywords to capture dome status in image meta data
    def get_header(self):
        self.header = {}
        return self.header
