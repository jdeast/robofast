import argparse
import datetime
import threading
import time
import sys
import os
import argparse

import asyncio

import ipdb
from filelock import FileLock
from pathlib import Path

# Our Dependencies
import utils
import mail
import env

from robofast import observatory


# check weather condition;
# close if bad, open and send heartbeat if good; update dome status
def dome_daemon(dome):
    dome.logger.info("Starting dome_daemon")
    sky_limit = -38
    lastskyupdate = datetime.datetime(2000, 1, 1)

    while True:
        t0 = datetime.datetime.utcnow()

        if (t0 - lastskyupdate).total_seconds() > 1800:
            dome.logger.info('Updating sky limit')
            lastskyupdate = t0

            try:
                old_sky_limit = sky_limit
                # sky_limit = minerva.site.getSkyLimit() #

                #   test_site.getWeatherRidge()
                sky_limit = test_site.getSkyLimit()

                if not sky_limit:
                    sky_limit = old_sky_limit
                    dome.logger.error('Error updating sky limit')

            except:
                dome.logger.error('Error updating sky limit')

        open_requested = os.path.isfile(dome.id + '.request.txt')
        sun_override = os.path.isfile(dome.id + '.sun_override.txt')
        timeout_override = os.path.exists(dome.id + '.timeout_override.txt')

        if not dome.ok_to_open():
            dome.logger.info('Weather not ok to open; ' \
                             'resetting 30 minute timeout')

            # only want to do this for weather, not Sun
            dome.last_close = datetime.datetime.utcnow()

            # regardless, make sure the dome is _is_closed.
            dome.close()

        elif (datetime.datetime.utcnow() -
              date.lastClose).total_seconds() < (30.0 * 60.0) \
                and not timeout_override:
            dome.logger.info('Conditions must be favorable for 30 minutes\
             before opening; last bad weather at ' + str(dome.last_close))
            dome.both()  # should already be _is_closed, but for good measure...

        elif not open_requested:
            dome.logger.info("Weather is ok, but domes are not requested to be open")
            dome.both()

        else:
            dome.logger.debug('Weather is good, starting thread to open ' + dome.id)
            openthread = threading.Thread(target=dome.open)
            openthread.start()
            dome.open()

            # only send heartbeats when we want it to open.
            dome.heartbeat()

        # write a file that makes it quicker to check dome status
        filename = dome.id + '.stat'
        lock_file_path = filename + ".lock"
        # ipdb.set_trace()
        with FileLock(lock_file_path):
            with open(filename, 'w') as fh:
                fh.write(str(datetime.datetime.utcnow()) + ' ' + str(dome.is_open))

        # ensure 4 heartbeats before timeout.
        sleeptime = max(14.0 -
                        (datetime.datetime.utcnow() -
                         t0).total_seconds(), 0)
        time.sleep(sleeptime)

def dome_daemon_catch(dome, directory):
    try:
        dome_daemon(dome, directory=directory)

    except Exception as e:
        dome.logger.exception(dome.id + ' dome_daemon died: ' + str(e.message))
        body = "Dear benevolent humans,\n\n" + \
               "I have encountered an unhandled exception which has killed the " + \
               dome.id + "dome_daemon. The error message is:\n\n" + \
               str(e.message) + "\n\n" + \
               "Check " + dome.logger_name + " for additional information. Please " + \
               "investigate, consider adding additional error handling, and " + \
               "restart 'dome_daemon.py'. The heartbeat *should* close the domes, " + \
               "but this is an unhandled problem and it may not close." + \
               "Please investigate immediately.\n\n" + \
               "Love,\n" + \
               "MINERVA"

        mail.send(dome.id + " dome_daemon died", body, level='critical', directory=directory)
        sys.exit()


def dome_daemon_thread(domes, directory):
    threads = []
    for dome in domes:
        dome.logger.info("Starting dome_daemon for " + str(dome.id))
        thread = threading.Thread(target=dome_daemon_catch, args=(dome, directory,))
        thread.name = dome.id
        threads.append(thread)

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Robotic observations')
    parser.add_argument('--config', dest=config_file, default='observatory_minerva.yaml', help='Configuration file for the observatory')
    opt = parser.parse_args()

    root_dir = Path(__file__).resolve().parent
    config_file = root_dir / "config" / opt.config_file
    observatory.observe(config_file)

    parser = argparse.ArgumentParser(description='Observe with MINERVA')
    parser.add_argument('--red', dest='red', action='store_true',
                        default=False, help='run with MINERVA red configuration')
    parser.add_argument('--south', dest='south', action='store_true',
                        default=False, help='run with MINERVA Australis configuration')
    parser.add_argument('--tunnel', dest='tunnel', action='store_true',
                        default=False, help='run remotely via tunnel')
    opt = parser.parse_args()

    directory = 'credentials/directory.txt'
    root_dir = Path(__file__).resolve().parent
    config_file = root_dir / "config" / "observatory_minerva.yaml"
    observatory = observatory.Observatory(config_file)
    dome_daemon_thread(domes, directory)


    domes = [aqawan.Aqawan('aqawan_1.ini', base_directory, tunnel=opt.tunnel),
             aqawan.Aqawan('aqawan_2.ini', base_directory, tunnel=opt.tunnel)]
    dome_daemon(domes[0], base_directory=base_directory)
    ipdb.set_trace()
