import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
import logging
import ipdb

# my local imports
from robofast import dome

# to run directly (respecting print/ipdb), type this from the directory above robofast
# python -m robofast.tests.test_dome
# to run the actual unit tests, from the robofast directory, type:
# pytest

root_dir = Path(__file__).resolve().parent.parent
config_file = root_dir / "config" / "dome_aqawan1.yaml"
d = dome.load_dome(config_file)

status = d.status()

#ipdb.set_trace()

# ----- Tests -----
def test_low_level_status():
    d = dome.load_dome(config_file)
    status = d._status()
    required_keys = ['Shutter1', 'Shutter2', 'SWVersion', 'EnclHumidity',
                     'EntryDoor1', 'EntryDoor2', 'PanelDoor', 'Heartbeat',
                     'SystemUpTime', 'Fault', 'Error', 'PanelExhaustTemp',
                     'EnclTemp', 'EnclExhaustTemp', 'EnclIntakeTemp', 'LightsOn']
    assert set(required_keys).issubset(status.keys())

def test_high_level_status():
    d = dome.load_dome(config_file)
    status = d.status()
    required_keys = ['open','tracking']
    assert set(required_keys).issubset(status.keys())

def test_hal_methods():
    d = dome.load_dome(config_file)
    d.close()
    assert d.is_closed()

def test_logs_recording(caplog):
    d = dome.load_dome(config_file)
    with caplog.at_level(logging.INFO):
        d.logger.info("Test message")
    assert "Test message" in caplog.text
