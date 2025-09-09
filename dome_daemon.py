import argparse
import datetime
import threading
import time
import sys
import os
import asyncio

import ipdb
from filelock import FileLock

# Our Dependencies
import utils
import mail
import aqawan
import env


# check weather condition;
# close if bad, open and send heartbeat if good; update dome status
def domeControl(dome, base_directory='/home/minerva/minerva-control'):
    dome.logger.info("Starting domeControl")
    lastnight = ''
    sky_limit = -38
    lastskyupdate = datetime.datetime(2000, 1, 1)
    print(base_directory)

    # (!) I edited this to exclude the dependency on control | check it works.
    test_site = env.site('site_mtHopkins.ini', base_directory)

    while True:
        t0 = datetime.datetime.utcnow()

        # roll over the logs to a new day
        thisnight = datetime.datetime.strftime(t0, 'n%Y%m%d')
        if thisnight != lastnight:
            dome.logger.info("Updating log path")
            # lives in control | move into utils  (done | added too much from control?)
            utils.update_logpath(dome, 'log/' + thisnight)  # should be in utils | (!) should be in utils | isn't yet.
            utils.update_logpath(test_site, 'log/' + thisnight)
            lastnight = thisnight

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

        openRequested = os.path.isfile(dome.base_directory +
                                       '/minerva_library/' +
                                       dome.id +
                                       '.request.txt')
        day = os.path.isfile(dome.base_directory +
                             '/minerva_library/sunOverride.' +
                             dome.id +
                             '.txt')

        # if the weather says it's not ok to open
        timeoutOverride = os.path.exists(dome.base_directory +
                                         '/minerva_library/timeoutOverride.' +
                                         dome.id +
                                         '.txt')
        dome.logger.debug('Checking if ok to open for ' + dome.id)

        # this double cal to oktoopen creates two identical messages in the
        # log when it's not ok to open...
        # (!) I edited this to exclude the dependency on control | check it works.
        if not test_site.oktoopen(dome.id,
                                  domeopen=dome.isOpen(),
                                  sky_limit=sky_limit):
            if not test_site.oktoopen(dome.id,
                                      domeopen=dome.isOpen(),
                                      ignoreSun=True,
                                      sky_limit=sky_limit):
                # If not ok to open if ignored sun, reset the timeout.
                dome.logger.info('Weather not ok to open; resetting\
                 30 minute timeout')
                test_site.lastClose = datetime.datetime.utcnow()

            # otherwise, just log it.
            else:
                dome.logger.info('Weather not ok to open')

            # regardless, make sure the dome is _is_closed.
            dome.close_both()

        elif (datetime.datetime.utcnow() -
              test_site.lastClose).total_seconds() < (30.0 * 60.0) \
                and not timeoutOverride:
            dome.logger.info('Conditions must be favorable for 30 minutes\
             before opening; last bad weather at ' + str(test_site.lastClose))
            dome.close_both()  # should already be _is_closed, but for good measure...

        elif not openRequested:
            dome.logger.info("Weather is ok, but domes are not requested to be open")
            dome.close_both()

        else:
            dome.logger.debug('Weather is good, opening dome')

            reverse = (dome.id == 'aqawan1')

            if day and dome.id != 'astrohaven1':
                openthread = threading.Thread(target=dome.open_shutter, args=(1,))

            else:
                openthread = threading.Thread(target=dome.open_both, args=(reverse,))

            openthread.name = dome.id + '_OPEN'
            dome.logger.debug('Starting thread to open ' + dome.id)
            openthread.start()

            # only send heartbeats when we want it to open.
            dome.heartbeat()

        if dome.id == 'aqawan1' or dome.id == 'aqawan2':
            status = dome.status()

        else:
            status = dome.status

        isOpen = (status['Shutter1'] == 'OPEN') and (status['Shutter2'] == 'OPEN')
        filename = dome.base_directory + \
                   '/minerva_library/' + \
                   dome.id + \
                   '.stat'
        lock_file_path = filename + ".lock"
        # ipdb.set_trace()
        with FileLock(lock_file_path):
            with open(filename, 'w') as fh:
                fh.write(str(datetime.datetime.utcnow()) + ' ' + str(isOpen))

        # check for E-stops (aqawans only)
        if dome.id == 'aqawan1' or dome.id == 'aqawan2':
            # response = asyncio.run(dome.send('CLEAR_FAULTS'))
            # response = await dome.send('CLEAR_FAULTS')
            response = dome.loop.run_until_complete(dome.send('CLEAR_FAULTS'))
            if 'Estop' in response:
                if not dome.estopmailsent:
                    mail.send("Aqawan " +
                              str(dome.id) +
                              " Estop has been pressed!",
                              dome.estopmail, level='serious')
                    dome.estopmailsent = True

            else:
                if dome.estopmailsent:
                    mail.send("Aqawan " +
                              str(dome.id) +
                              " Estop has been cleared.",
                              "", level='serious')
                    dome.estopmailsent = False

        # ensure 4 heartbeats before timeout.
        sleeptime = max(14.0 -
                        (datetime.datetime.utcnow() -
                         t0).total_seconds(), 0)
        time.sleep(sleeptime)


def domeControl_catch(dome, directory, base_directory):
    try:
        domeControl(dome, base_directory=base_directory)

    except Exception as e:
        dome.logger.exception('DomeControl thread died: ' + str(e.message))
        body = "Dear benevolent humans,\n\n" + \
               "I have encountered an unhandled exception which has killed the " + \
               "dome control thread. The error message is:\n\n" + \
               str(e.message) + "\n\n" + \
               "Check " + dome.logger_name + " for additional information. Please " + \
               "investigate, consider adding additional error handling, and " + \
               "restart 'domeControl.py'. The heartbeat *should* close the domes, " + \
               "but this is an unhandled problem and it may not close." + \
               "Please investigate immediately.\n\n" + \
               "Love,\n" + \
               "MINERVA"
        # (!) changed directory | check compatibility w/ other observatories
        mail.send("DomeControl thread died", body, level='critical', directory=directory)
        sys.exit()


def domeControlThread(domes, directory, base_directory):
    threads = []
    for dome in domes:
        dome.logger.info("Starting dome control thread for " + str(dome.id))
        thread = threading.Thread(target=domeControl_catch, args=(dome, directory, base_directory,))
        thread.name = dome.id
        threads.append(thread)

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Observe with MINERVA')
    parser.add_argument('--red', dest='red', action='store_true',
                        default=False, help='run with MINERVA red configuration')
    parser.add_argument('--south', dest='south', action='store_true',
                        default=False, help='run with MINERVA Australis configuration')
    parser.add_argument('--tunnel', dest='tunnel', action='store_true',
                        default=False, help='run remotely via tunnel')
    opt = parser.parse_args()

    # python bug work around -- strptime not thread safe. Must call this once before starting threads
    junk = datetime.datetime.strptime('2000-01-01 00:00:00', '%Y-%m-%d %H:%M:%S')

    # base_directory = '/home/minerva/minerva-control'
    base_directory = 'C:/Users/adria/AppData/Roaming/JetBrains/PyCharmCE2022.3/minerva-control'
    if opt.red:
        pass
    elif opt.south:
        pass
    else:
        directory = 'credentials/directory.txt'
        domes = [aqawan.Aqawan('aqawan_1.ini', base_directory, tunnel=opt.tunnel),
                 aqawan.Aqawan('aqawan_2.ini', base_directory, tunnel=opt.tunnel)]
        domeControl(domes[0], base_directory=base_directory)
        ipdb.set_trace()
        domeControlThread(domes, directory, base_directory=base_directory)
