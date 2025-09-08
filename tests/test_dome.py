import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
import ipdb

# my local imports
from mearth import dome

# to run directly (respecting print/ipdb), type this from the directory above mearth
# python -m mearth.tests.test_dome
# to run the actual unit tests, type:
# 

root_dir = Path(__file__).resolve().parent.parent
config_file = root_dir / "config" / "aqawan1.yaml"
dome = dome.load_dome(config_file)

status = dome.status()

ipdb.set_trace()

# ----- Tests -----
def test_initialization():
    d = load_dome(config_file)
    assert d.site_name == "TestSite"
    assert d.status == "idle"
    assert d.logs == []

def test_hal_methods():
    d = load_dome(config_file)
    d.park()
    d.unpark()
    assert t.parked is True
    assert t.unparked is True

def test_dynamic_loader_calls():
    """Use a MagicMock to ensure HAL init is called."""
    mock_hal = MagicMock()
    t = load_telescope(mock_hal)
    # __init__ of HAL should have been called
    assert mock_hal.__init__.called
    # Methods delegated correctly
    t.slew_to(1, 2)
    t.park()
    t.unpark()
    assert mock_hal.slew_to.called
    assert mock_hal.park.called
    assert mock_hal.unpark.called

def test_logs_recording():
    t = load_telescope(DummyHAL)
    t.log("Test message")
    assert t.logs == ["Test message"]
