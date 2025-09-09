# Implementation of TCS from DFM here (pgs 17-32):
# https://arm.dfmsupport.com/jobs/857/TCSGO%20Manual%20v1.3.pdf
# local copy at ./TCSGO_Manual_v1.3.pdf

import math
import ipdb
import sys
import logging
import time
import socket
import datetime
import ephem
import numpy as np

''' 
this is a telescope class that implements DFM Galil TCS
'''
class Telescope_DFM:

    def __init__(self, config):

        self.logger = logging.getLogger()

        self.server = config['SERVER']
        self.port = int(config['PORT'])
        self.epoch = config['EPOCH']
        self.max_jog = float(config['MAX_JOG'])
        self.min_focus = float(config['MIN_FOCUS'])
        self.max_focus = float(config['MAX_FOCUS'])

        # this goes to a observatory object
        self.sitename = config['SITENAME']
        self.sitelat = float(config['SITELAT'])
        self.sitelong = float(config['SITELONG'])
        self.siteelev = float(config['SITEELEV'])
        self.obs = ephem.Observer()
        self.obs.lat = ephem.degrees(str(self.sitelat)) # N
        self.obs.lon = ephem.degrees(str(self.sitelong)) # E
        self.obs.elevation = self.siteelev # meters

        # this translates from X/Y on the CCD to north/east on the sky
        self.cd11 = float(config['CD1_1'])
        self.cd12 = float(config['CD1_2'])
        self.cd21 = float(config['CD2_1'])
        self.cd22 = float(config['CD2_2'])

    # send a command to the Galil TCS
    def send(self,cmd, timeout=5.0, readback=True):
        s = socket.socket(socket.AF_INET,socket.SOCK_STREAM)

        try:
            s.connect((self.server,self.port))
            self.logger.debug("Connected")
        except:
            self.logger.error("No response from the Galil TCS Server (" + self.server +
                              ':' + str(self.port) + "). Start TCSGalil and try again")
            ipdb.set_trace()

            sys.exit() # too harsh?

            return None

        self.logger.debug("Sending command: " + cmd)
        s.send(cmd.encode('utf-8'))

        if not readback: return None

        data = b''
        t0 = datetime.datetime.utcnow()
        time_elapsed = 0.0
        while True and time_elapsed < timeout:
            time_elapsed = (datetime.datetime.utcnow()-t0).total_seconds()
            data += s.recv(1024)
            try:
                if data.decode()[-1] == ';': break
            except: break

        self.logger.debug("Closing connection")
        s.close()
        data = data.decode()

        if time_elapsed > timeout:
            self.logger.error("Timeout sending command to TCS Galil")

        self.logger.debug("Message received: " + data)

        return data

    def read_tcs_status(self):
        response = self.send("#26;")

        # trim leading "#" and trailing ";", then split by ','
        arr = response[1:-1].split(',')

        status = {}
        if len(arr) == 8:
            byte0 = format(int(arr[0]),'b').zfill(8)
            status['drive_switch_up']      = byte0[-1]=='1'
            status['track_switch_up']      = byte0[-2]=='1'
            status['aux_track_switch_up']  = byte0[-3]=='1'
            status['dome_track_switch_up'] = byte0[-4]=='1'
            status['auto_dome_switch_up']  = byte0[-5]=='1'
            status['amplifiers_switch_up'] = byte0[-6]=='1'

            byte1 = format(int(arr[1]),'b').zfill(8)
            status['north_button_pressed'] = byte1[-1]=='1'
            status['south_button_pressed'] = byte1[-2]=='1'
            status['east_button_pressed']  = byte1[-3]=='1'
            status['west_button_pressed']  = byte1[-4]=='1'
            status['set_button_pressed']   = byte1[-5]=='1'
            status['slew_button_pressed']  = byte1[-6]=='1'
            status['ascom_guiding']        = byte1[-7]=='1'

            byte2 = format(int(arr[2]),'b').zfill(8)
            status['horizon_limit_reached']  = byte2[-1]=='1'
            status['east_travel_limit']      = byte2[-2]=='1'
            status['west_travel_limit']      = byte2[-3]=='1'
            status['north_travel_limit']     = byte2[-4]=='1'
            status['south_travel_limit']     = byte2[-5]=='1'
            status['target_out_of_range']    = byte2[-6]=='1'
            status['approaching_soft_limit'] = byte2[-7]=='1'
            status['reached_soft_limit']     = byte2[-8]=='1'

            byte3 = format(int(arr[3]),'b').zfill(8)
            status['find_home_mode'] = byte3[-1]=='1'
            status['goto_az_mode']   = byte3[-2]=='1'
            status['track_mode']     = byte3[-3]=='1'
            status['rotating_left']  = byte3[-4]=='1'
            status['rotating_right'] = byte3[-5]=='1'
            status['at_home']        = byte3[-6]=='1'
            status['manual_mode']    = byte3[-7]=='1'

            byte4 = format(int(arr[4]),'b').zfill(8)
            status['focus_moving_in']        = byte4[-1]=='1'
            status['focus_moving_out']       = byte4[-2]=='1'
            status['focus_at_outer_limit']   = byte4[-3]=='1'
            status['focus_at_inner_limit']   = byte4[-4]=='1'
            status['instrument_rotator_ccw'] = byte4[-5]=='1'
            status['instrument_rotator_cw']  = byte4[-6]=='1'

            byte5 = format(int(arr[5]),'b').zfill(8)
            status['serial_comm_ready']        = byte5[-1]=='1'
            status['tcpip_connected']          = byte5[-2]=='1'
            status['use_cos_dec']              = byte5[-3]=='1'
            status['use_rate_corrections']     = byte5[-4]=='1'
            status['east_side_pier_operation'] = byte5[-5]=='1'
            status['west_side_pier_operation'] = byte5[-6]=='1'
            status['galil_data_is_stale']      = byte5[-7]=='1'

            byte6 = format(int(arr[6]),'b').zfill(8)
            status['mount_in_chase_target_mode']   = byte6[-1]=='1'
            status['mount_in_slew_to_target_mode'] = byte6[-2]=='1'
            status['mount_in_follow_target_mode']  = byte6[-3]=='1'
            status['mount_in_hold_position_mode']  = byte6[-4]=='1'
            status['target_captured']              = byte6[-5]=='1'

            byte7 = format(int(arr[7]),'b').zfill(8)
            status['dome_upper_shutter_open']   = byte7[-1]=='1'
            status['dome_upper_shutter_closed'] = byte7[-2]=='1'
            status['dome_lower_shutter_open']   = byte7[-3]=='1'
            status['dome_lower_shutter_closed'] = byte7[-4]=='1'
            status['mirror_doors_open']         = byte7[-5]=='1'
            status['mirror_doors_closed']       = byte7[-6]=='1'

        return status

    ################## BEGIN FOCUSER COMMANDS ###################
    def stop_focus_motion(self):
        self.logger.info("Stopping focuser")
        self.send('#72;',readback=False)

    def read_focuser_position(self):
        #self.logger.info("Reading focus position")
        response = self.send('#64;')
        if response != None:
            return float(response[1:-1])

    def set_fast_focus_rate(self, fraction_of_max_rate):
        if fraction_of_max_rate < 0.01 or fraction_of_max_rate > 1:
            self.logger.error("FRACTION_OF_MAX_RATE (" + str(fraction_of_max_rate) + \
                              ") must be between 0.01 and 1.0; no change applied")
            return
        self.send('#55,' + str(fraction_of_max_rate) + ';', readback=False)

    def set_slow_focus_rate(self, fraction_of_max_rate):
        if fraction_of_max_rate < 0.01 or fraction_of_max_rate > 1:
            self.logger.error("FRACTION_OF_MAX_RATE (" + str(fraction_of_max_rate) + \
                              ") must be between 0.01 and 1; no change applied")
            return
        self.send('#54,' + str(fraction_of_max_rate) + ';', readback=False)

    def initialize_focus_encoder(self):
        self.send('#37;', readback=False)

    def initialize_focus_position(self, position):
        if position < self.min_focus or position > self.max_focus:
            self.logger.error("POSITION (" + str(position) + \
                              " must be between " + str(self.min_focus) +\
                              " and " + str(self.max_focus) + "; no change applied")
            return
        self.send('#5,' + str(position) + ';', readback=False)

    def move_focus(self, position):
        if position < self.min_focus or position > self.max_focus:
            self.logger.error("POSITION (" + str(position) + \
                              " must be between " + str(self.min_focus) + \
                              " and " + str(self.max_focus) + "; no change applied")
            return
        self.send('#27,' + str(position) + ';', readback=False)

    # high level focus commands
    def move_focus_and_check(self, position, tolerance=5, timeout=30):
        self.move_focus(position)
        t0 = datetime.datetime.utcnow()
        focpos = self.read_focuser_position()
        elapsedTime = (datetime.datetime.utcnow() - t0).total_seconds()
        while (elapsedTime < timeout) and (abs(focpos-position) > tolerance):
            time.sleep(0.1)
            focpos = self.read_focuser_position()
            elapsedTime = (datetime.datetime.utcnow() - t0).total_seconds()

        return abs(focpos - position) < tolerance

    ################## END FOCUSER COMMANDS ###################


    ################## BEGIN DOME COMMANDS ###################
    def initialize_dome_position(self, azimuth):
        if azimuth < 0 or azimuth > 360:
            self.logger.error("AZIMUTH (" + str(azimuth) +\
                              " must be between 0 and 360; no change applied")
            return
        self.send('#2,' + str(azimuth) + ';', readback=False)

    def set_dome_mode(self,mode):
        if ((mode != 0) and (mode != 1) and (mode != 2) and (mode != 3)):
            self.logger.error("Supplied dome mode (" + str(mode) +
                              ") not allowed; must be 0 (Manual), \
                              1 (Track Telescope), 2 (GoTo Azimuth), \
                              or 3 (Find Home)")
            return False

        return self.send("#20," + str(mode) + ";", readback=False)

    def set_dome_azimuth_target(self, azimuth):
        if azimuth < 0 or azimuth > 360:
            self.logger.error("AZIMUTH (" + str(azimuth) + \
                              " must be between 0 and 360; no change applied")
            return
        self.send('#43,' + str(azimuth) + ';', readback=False)

    def dome_stop_rotation(self):
        self.send('#35;', readback=False)

    def dome_stop_upper_shutter(self):
        self.send('#40;', readback=False)

    def dome_stop_lower_shutter(self):
        self.send('#41;', readback=False)

    def actuate_dome_upper_shutter(self, open):
        if open != 0 and open != 1:
            self.logger.error("Supplied dome mode (" + str(open) +
                              ") not allowed; must be 0 (close) or \
                              1 (open)")
            return False
        self.send('#36' + str(open) + ';', readback=False)

    def actuate_dome_lower_shutter(self, open):
        if open != 0 and open != 1:
            self.logger.error("Supplied dome mode (" + str(open) +
                              ") not allowed; must be 0 (close) or \
                              1 (open)")
            return False
        self.send('#43' + str(open) + ';', readback=False)

    def read_dome_azimuth(self):
        self.logger.info("Reading dome azimuth")
        response = self.send('#62;')
        if response != None:
            return float(response[1:-1])

    def dome_continuous_rotation(self, direction):
        if direction != 1 and direction != -1:
            self.logger.error("Supplied direction (" + str(direction) +
                              ") not allowed; must be -1 (CCW) or \
                              1 (CW)")
            return False
        self.send('#73,' + str(direction) + ';', readback=False)

    ################## END DOME COMMANDS ###################

    ################## BEGIN ROTATOR COMMANDS ###################
    ########### THESE ARE NOT HOOKED INTO THE FLWO 60" ##########
    def initialize_instrument_rotator(self,azimuth):
        self.logger.error("Instrument rotator control through DFM not supported on FLWO 60")
        return False

        if azimuth < 0 or amimuth > 360:
            self.logger.error("Supplied AZIMUTH (" + str(azmiuth) +
                              ") not allowed; 0 <= AZIMUTH <= 360")
            return False
        self.send('#4,' + str(azimuth) + ';', readback=False)
    ################## END ROTATOR COMMANDS ###################

    ################## BEGIN MOUNT COMMANDS ###################
    def initialize_mount_position(self,rahrs,decdeg,equinox=2000.0):
        if rahrs < 0 or rahrs > 24:
            self.logger.error("Supplied RAHRS (" + str(rahrs) +
                              ") not allowed; 0 <= RA <= 24")
            return False
        # stricter lower limit? Is there an upper limit?
        if decdeg < -90 or decdeg > 90:
            self.logger.error("Supplied DECDEG (" + str(decdeg) +
                              ") not allowed; -90 <= DEC <= 90")
            return False
        # example has 2038.5 -- is equinox limited by the 2038 problem?
        if equinox < 1950 or equinox > 2100:
            self.logger.error("Supplied EQUINOX (" + str(equinox) +
                              ") not allowed; 1950 < EQUINOX < 2100")
            return False
        self.send('#3,' + str(rahrs) + ',' + str(decdeg) + ',' + str(equinox) + ';', readback=False)

    def get_objname(self):
        return "test"

    def set_target_object(self, rahrs, decdeg, equinox=2000.0):
        if rahrs < 0 or rahrs > 24:
            self.logger.error("Supplied RAHRS (" + str(rahrs) +
                              ") not allowed; 0 <= RA <= 24")
            return False
        # stricter lower limit? Is there an upper limit?
        if decdeg < -90 or decdeg > 90:
            self.logger.error("Supplied DECDEG (" + str(decdeg) +
                              ") not allowed; -90 <= DEC <= 90")
            return False
        # example has 2038.5 -- is equinox limited by the 2038 problem?
        if equinox < 1950 or equinox > 2100:
            self.logger.error("Supplied EQUINOX (" + str(equinox) +
                              ") not allowed; 1950 < EQUINOX < 2100")
            return False
        self.send('#6,' + str(rahrs) + ',' + str(decdeg) + ',' + str(equinox) + ';', readback=False)

    # this jogs the telescope. Note if it is not in CHASE mode, set_mount_mode must be set to 1(?)
    # if requested jog is out of range, status['target_out_of_range'] will be True
    # this command (#7) is broken on the DFM side. We'll compute our own offset instead and send a set_target_object instead to make it behave as expected
    def offset_target_object(self,east,north):

        # this is a limit imposed by me here. No such limit exists in the telescope
        if abs(east) > self.max_jog or abs(north) > self.max_jog:
            self.logger.error('Cannot jog more than ' + str(self.max_jog) + '" in any direction')

        target_coords = self.read_target_object_coordinates()
        newra = target_coords['mean_ra'] + east/3600.0/15.0/math.cos(target_coords['mean_dec']*math.pi/180.0)
        newdec = target_coords['mean_dec'] + north/3600.0
        return self.set_target_object(newra,newdec,equinox=target_coords['equinox'])

        # this command would replace the above four if the the DFM command #7 worked like it should
        #return self.send("#7," + '{0:.3f}'.format(east) + "," + '{0:.3f}'.format(north) + ";", readback=False)

    # sets the target to the coordinates of the library object stored in TCSGalil.
    def set_target_object_from_library(self):
        self.send('#8;',readback=False)

    def set_target_object_from_table(self, lineno):
        if lineno < 1 or lineno > 40:
            self.logger.error("Supplied LINENO (" + str(lineno) +
                              ") not allowed; 1 <= LINENO <= 40")
            return False

        self.send('#9,' + str(lineno) + ';',readback=False)

    def set_target_object_to_zenith(self):
        self.send('#10;',readback=False)

    # NOTE: no documented command 11

    def set_mount_mode(self, mode):
        if ((mode != 0) and (mode != 1) and (mode != 2) and (mode != 3)):
            self.logger.error("Supplied mount mode (" + str(mode) +
                              ") not allowed; must be 0 (Hold Position), 1 \
                              (Follow Target), 2 (Slew to Target), or 3 (Chase Target)")
            return False

        return self.send("#12," + str(mode) + ";", readback=False)

    def stop_mount(self):
        self.send('#13;',readback=False)

    def set_track_rates(self,ha_rate,dec_rate,aux_ha_rate,aux_dec_rate):
        self.send('#14,' + str(ha_rate) + ',' + str(dec_rate) + ',' +\
                  str(aux_ha_rate) + ',' + str(aux_dec_rate) + ';',readback=False)

    def change_guide_rate(self,guide_rate):
        if guide_rate < 3 or guide_rate > 10:
            self.logger.error("Supplied GUIDE_RATE (" + str(guide_rate) +
                              ") not allowed; 3 <= GUIDE_RATE <= 10")

        self.send('#15,' + str(guide_rate) + ';',readback=False)

    def change_set_rate(self,set_rate):
        if set_rate < 50 or set_rate > 300:
            self.logger.error("Supplied SET_RATE (" + str(set_rate) +
                              ") not allowed; 50 <= GUIDE_RATE <= 300")

        self.send('#16,' + str(set_rate) + ';',readback=False)

    # NOTE: no documented command 17

    def use_cosine_dec(self, on):
        if on != 0 and on != 1:
            self.logger.error("Supplied mode (" + str(on) +
                              ") not allowed; must be 0 (off) or \
                              1 (on)")
            return False
        self.logger.info("Applying cos(dec)")

        self.send('#18' + str(on) + ';', readback=False)

    def use_rate_corrections(self, on):
        if on != 0 and on != 1:
            self.logger.error("Supplied mode (" + str(on) +
                              ") not allowed; must be 0 (off) or \
                              1 (on)")
            return False
        self.logger.info("Using rate corrections")
        self.send('#19' + str(on) + ';', readback=False)

    def set_display_equinox(self,equinox):
        if equinox < 1950 or equinox > 2100:
            self.logger.error("Supplied EQUINOX (" + str(equinox) +
                              ") not allowed; 1950 < EQUINOX < 2100")
            return False
        self.send('#22,' + str(equinox) + ';', readback=False)

    def set_table_object(self,lineno, rahrs, decdeg, equinox):
        if lineno < 1 or lineno > 40:
            self.logger.error("Supplied LINENO (" + str(lineno) +
                              ") not allowed; 1 <= LINENO <= 40")
            return False
        if rahrs < 0 or rahrs > 24:
            self.logger.error("Supplied RAHRS (" + str(rahrs) +
                              ") not allowed; 0 <= RA <= 24")
            return False
        # stricter lower limit? Is there an upper limit?
        if decdeg < -90 or decdeg > 90:
            self.logger.error("Supplied DECDEG (" + str(decdeg) +
                              ") not allowed; -90 <= DEC <= 90")
            return False
        # example has 2038.5 -- is equinox limited by the 2038 problem?
        if equinox < 1950 or equinox > 2100:
            self.logger.error("Supplied EQUINOX (" + str(equinox) +
                              ") not allowed; 1950 < EQUINOX < 2100")
            return False
        self.send('#23,' + str(lineno) + ',' + str(rahrs) + ',' +\
                  str(decdeg) + ',' + str(equinox) + ';', readback=False)

    def read_mount_coordinates(self):
        response = self.send("#25;")

        # trim leading "#" and trailing ";", then split by ','
        arr = response[1:-1].split(',')
        coords = {}
        if len(arr) == 10:
            # convert date and time to a datetime object
            # what is a 'decimal year'? It doesn't change often enough to be precise,
            # nor is it an integer number of days...
            year = int(float(arr[9]))
            seconds = (float(arr[9])-year)*365.25*86400.0
            hours = float(arr[8])
            date = datetime.datetime(year,1,1,0) + datetime.timedelta(seconds=seconds)

            coords['HA'] = float(arr[0])
            coords['RA'] = float(arr[1])
            coords['Dec'] = float(arr[2])
            coords['equinox'] = float(arr[3])
            coords['airmass'] = float(arr[4])
            coords['zenith_distance'] = float(arr[5])
            coords['azimuth'] = float(arr[6])
            coords['sidereal_time'] = float(arr[7])
            coords['date'] = date
        else:
            coords['HA'] = 'UNKNOWN'
            coords['RA'] = 'UNKNOWN'
            coords['Dec'] = 'UNKNOWN'
            coords['equinox'] = 'UNKNOWN'
            coords['zenith_distance'] = 'UNKNOWN'
            coords['azmiuth'] = 'UNKNOWN'
            coords['sidereal_time'] = 'UNKNOWN'
            coords['date'] = 'UNKNOWN'

        return coords

    # hard to see the ultility of this without at least a read coeffs function...
    def change_pointing_coefficients(self,tbar,elevation_misalignment,\
                                     azimuth_misalignment,collimation,\
                                     non_perpendicularity,ha_encoder_eccentricity,\
                                     ha_encoder_eccentricity_phase_angle,\
                                     ha_encoder_eccentricity_x4,\
                                     dec_encoder_eccentricity,\
                                     dec_encoder_eccentricity_phase_angle,\
                                     dec_encoder_eccentricity_x4,\
                                     dec_encoder_eccentricity_phase_angle_x4,\
                                     tube_flexure_sin, tube_flexure_tan, \
                                     fork_flexure, declination_axis_flexure):
        # hard to see the ultility of this without at least a read coeffs function...
        pass

    # documentation wrong? claims there is no data returned, implies the table is sent
    def read_point(self):
        response = self.send('#28;')
        # trim leading "#" and trailing ";", then split by ','
        arr = response[1:-1].split(',')
        point = {}
        if len(arr) == 6:
            point['jd'] = arr[0]
            point['sidereal_time'] = arr[1]
            point['telescope_ha'] = arr[2]
            point['telescope_dec'] = arr[3]
            point['target_object_ra'] = arr[4]
            point['target_object_dec'] = arr[5]
            point['line'] = response
        return point

    def set_encoder_offsets_for_zenith(self):
        self.send("#30;", readback=False)

    def set_encoder_offsets_to_defaults(self):
        self.send("#31;", readback=False)

    def read_target_object_coordinates(self):
        response = self.send("#32;")

        # trim leading "#" and trailing ";", then split by ','
        arr = response[1:-1].split(',')
        target = {}
        if len(arr) == 10:
            target['elapsed_track_time'] = float(arr[0])
            target['apparent_ha'] = float(arr[1])
            target['mean_ra'] = float(arr[2])
            target['mean_dec'] = float(arr[3])
            target['equinox'] = float(arr[4])
            target['mean_ra_velocity'] = float(arr[5])
            target['mean_dec_velocity'] = float(arr[6])
            target['airmass'] = float(arr[7])
            target['zenith_distance'] = float(arr[8])
            target['azimuth'] = float(arr[9])
        return target

    # turns on the pointing model corrections
    def apply_mount_corrections(self, on):
        if on != 0 and on != 1:
            self.logger.error("Supplied mode (" + str(on) +
                              ") not allowed; must be 0 (off) or \
                              1 (on)")
            return False
        self.logger.info("Turning on mount corrections")
        self.send('#66,' + str(on) + ';', readback=False)

    def start_tracking(self):
        self.send('#50;', readback=False)

    def set_tracking_mode(self,mode):
        if mode != 0 and mode != 1:
            self.logger.error("Supplied mode (" + str(mode) +
                              ") not allowed; must be 0 (aux) or \
                              1 (normal)")
            return False
        self.send('#51,' + str(mode) + ';', readback=False)

    # sets the handpaddle slew rate (arcsec/sec)
    def set_slew_rate(self,rate):
        self.send('#53,' + str(rate) + ';', readback=False)

    # direction: 0=North, 1=South, 2=East, 3=West
    # duration in milliseconds
    def pulse_guide(self,direction, duration):
        self.send('#61,'+str(direction)+','+str(duration)+';',readback=False)

    ###### HIGHER LEVEL MOUNT COMMANDS ######
    def jog(self, east, north):

        self.logger.info('Jogging the telescope ' + str(east) + '" east, ' + str(north) + '" north')

        # we want to restore these when we're done
        target = self.read_target_object_coordinates()

        # set target position to where we are + offset
        mount_coords = self.read_mount_coordinates()
        newra = mount_coords['RA'] + east/3600.0/15.0/math.cos(mount_coords['Dec']*math.pi/180.0)
        newdec = mount_coords['Dec'] + north/3600.0
        self.set_target_object(newra, newdec)

        # change to chase target mode (so it'll slew to the target)
        self.set_mount_mode(3)

        # wait until it's done moving
        elapsed_time = 0.0
        t0 = datetime.datetime.utcnow()
        timeout = 10.0
        while True:
            tcs_status = self.read_tcs_status()
            if tcs_status['target_captured'] or elapsed_time > timeout: break
            time.sleep(0.3)
            elapsed_time = (datetime.datetime.utcnow() - t0).total_seconds()

        # set back to follow mode
        self.set_mount_mode(1)

        # reset target coordinates to original
        self.set_target_object(target['mean_ra'],target['mean_dec'], equinox=target['equinox'])

    #### this doesn't seem to work...
    def jog_bad(self,east,north):
        slewrate = 300.0
        self.set_slew_rate(slewrate)
        northduration = north/slewrate*1000.0
        eastduration = east/slewrate*1000.0

        if north > 0: self.pulse_guide(0,northduration)
        else: self.pulse_guide(1,-northduration)

        if east > 0: self.pulse_guide(2,eastduration)
        else: self.pulse_guide(3,-eastduration)

    # untested
    def slew(self, rahrs, decdeg, equinox=2000.0, wait=True, timeout=60.0):

        tcs_status = self.read_tcs_status()
        # change to chase target mode (so it'll slew to the target)
        if not tcs_status['mount_in_chase_target_mode']:
            self.logger.info("Mount not in chase target mode when slew issued;\
                              changing mode")
            self.set_mount_mode(3)

        # make sure the dome is open and slaved to the telescope
        if not tcs_status['dome_upper_shutter_open']:
            self.logger.warning("Dome's upper shutter is not open!")

        if not tcs_status['dome_lower_shutter_open']:
            self.logger.warning("Dome's lower shutter is not open!")

        if not tcs_status['goto_azimuth_mode']:
            pass

        # probably need some other initialization/checks here...
        # switches up?
        # telescope tracking? (automatic?)
        # galil_data_stale?

        # send the slew command
        self.set_target_object(rahrs,decdeg,equinox=equinox)

        # how robust is this status bit? do we need additional safeguards?
        tcs_status = self.read_tcs_status()
        if tcs_status['target_out_of_range']:
            self.logger.error('Target (RA=' + str(rahrs) + ' hrs, Dec=' +\
                              str(decdeg) + ' deg) is not in range')

        # initiate the slew the telescope
        self.logger.info("moving the telescope to RA (J2000)" +\
                         str(raj2000) + ', DEC (J2000):' + str(decj2000))

        if not wait: return

        # wait for status bits to update
        time.sleep(1.0)
        tcs_status = self.read_tcs_status()

        t0 = datetime.datetime.utcnow()
        elapsed_time = (datetime.datetime.utcnow() - t0).total_seconds()
        while tcs_status['target_captured'] and elapsed_time < timeout:
            elapsed_time = (datetime.datetime.utcnow() - t0).total_seconds()
            self.logger.info("Waiting for slew")
            time.sleep(1.0)
            tcs_status = self.read_tcs_status()

        return tcs_status['target_captured']

    ################## END MOUNT COMMANDS ###################

###### begin my commands #######

    def add_header_keys(self,hdr):

        # Site keywords
        hdr['TELESCOP'] = (self.sitename,"Telescope")
        hdr['SITELAT'] = (self.sitelat,"Site Latitude (deg)")
        hdr['SITELONG'] = (-self.sitelong,"Site longitude (deg W of zero)")
        hdr['SITEELEV'] = (self.siteelev,"Site Elevation (m)")

        # Positional keywords
        coords = self.read_mount_coordinates()
        # try again if it failed
        if coords['RA'] == 'UNKNOWN': coords = self.read_mount_coordinates()
        hdr['HA'] = (coords['HA'], "Hour angle (hrs)")
        try: hdr['RA'] = (coords['RA']*15.0, "Mount RA (J2000 deg)")
        except: hdr['RA'] = ('UNKNOWN', "Mount RA (J2000 deg)")
        hdr['DEC'] = (coords['Dec'], "Mount Dec (J2000 deg)")
        hdr['EQUINOX'] = (coords['equinox'],"Equinox of coordinates")

        #stolen from parangle.pro written by Tim Robinshaw
        parang = -180.0/math.pi*math.atan2(-math.sin(coords['HA']*math.pi/12.0),
                                           math.cos(coords['Dec']*math.pi/180.0)*math.tan(float(self.sitelat)*math.pi/180.0)-
                                           math.sin(coords['Dec']*math.pi/180.0)*math.cos(coords['HA']*math.pi/12.0))

        hdr['PARANG'] = (parang, "Parallactic Angle (deg)")
        hdr['AIR'] = (coords['airmass'],'Airmass')
        hdr['ZENITH'] = (coords['zenith_distance'],'Zenith Distances (deg)')
        hdr['AZIMUTH'] = (coords['azimuth'],'Telescope Azimuth (Deg E of N)')
        hdr['ALTITUDE'] = (90 - coords['zenith_distance'],'Telescope Altitude (Deg)')
        hdr['ST'] = (coords['sidereal_time'],'Sidereal Time (HH:MM:SS.fff)')
        hdr['FOCPOS'] = (self.read_focuser_position(),"Focus Position (um)")
        #hdr['PMODEL'] = ('',"Pointing Model File")
        #hdr['ROTPOS'] = (rotpos,"Mechanical rotator position (degrees)")

        sign = np.sign(self.cd11*self.cd22-self.cd12*self.cd21)
        skypa = math.atan2(sign*self.cd21,self.cd11)*180.0/math.pi
        hdr['SKYPA' ] = (skypa,"Position angle on the sky (degrees E of N)")

        # add moon keywords
        moon = ephem.Moon()
        moon.compute(datetime.datetime.utcnow())
        moonpos = (moon.ra,moon.dec)
        moonsep = ephem.separation((coords['RA']*math.pi/180.0,coords['Dec']*math.pi/180.0),(moon.ra,moon.dec))*180.0/math.pi
        hdr['MOONRA'] = (moon.ra*180.0/math.pi, "Moon RA (J2000 deg)")
        hdr['MOONDEC'] =  (moon.dec*180.0/math.pi, "Moon Dec (J2000 deg)")
        hdr['MOONPHAS'] = (moon.phase/100.0, "Moon Phase (Fraction)")
        hdr['MOONDIST'] = (moonsep, "Distance between pointing and moon (deg)")

        # add Sun keywords
        self.obs.date = datetime.datetime.utcnow()
        sun = ephem.Sun()
        sun.compute(self.obs) # needs lat/lon/date
        hdr['SUNALT'] = (sun.alt*180.0/math.pi, "Sun Alt (Deg)")
        hdr['SUNAZ'] =  (sun.az*180.0/math.pi, "Sun Az (Deg E of N)")

if __name__ == '__main__':

    root_dir = Path(__file__).resolve().parent.parent

    # this should work on the 60" or Tierras
    if socket.gethostname() == 'tres-guider':
        config_file = root_dir / "config" / "telescope_flwo60.yaml"
    elif socket.gethostname() == 'flwo60':
        config_file = root_dir / "config" / "telescope_flwo60.yaml"
    elif socket.gethostname() == 'tierras':
        config_file = root_dir / "config" / "telescope_tierras.yaml"
    else:
        print('unsupported system')
        sys.exit()

    with open(config_file) as f:
        config = yaml.safe_load(f)

    telescope = Telescope_DFM(config)

