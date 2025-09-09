from pathlib import Path
import logging
import ipdb

# local imports
from robofast import camera

# to run directly (respecting print/ipdb), type this from the directory above robofast
# python -m robofast.tests.test_camera
# to run the actual unit tests, from the robofast directory, type:
# pytest
# or
# pytest tests/test_camera.py::test_name

root_dir = Path(__file__).resolve().parent.parent
config_file = root_dir / "config" / "camera_apogee1.yaml"

c = camera.load_camera(config_file)

ipdb.set_trace()
