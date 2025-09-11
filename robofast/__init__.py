import logging
from pathlib import Path
import time

from robofast.daily_dir_file_handler import DailyDirTimedRotatingFileHandler

log_root = Path(__file__).resolve().parent / "logs"
logger = logging.getLogger()

# Create daily-directory rotating handler
handler = DailyDirTimedRotatingFileHandler(
    base_dir=log_root,
    when="midnight",
    interval=1,
    utc=True,
    backupCount=30
)

fmt = "{asctime} [{filename}:{lineno} - {funcName}()] {levelname}: {threadName}: {message}"

datefmt = "%Y-%m-%dT%H:%M:%S"
formatter = logging.Formatter(fmt,style="{",datefmt=datefmt)
formatter.converter = time.gmtime

handler.setFormatter(formatter)

console = logging.StreamHandler()
console.setFormatter(formatter)
console.setLevel(logging.INFO)

# add a separate logger for the terminal (don't display debug-level messages)
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)
logger.addHandler(console)

