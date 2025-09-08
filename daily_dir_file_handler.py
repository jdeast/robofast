import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import os
from datetime import datetime

class DailyDirTimedRotatingFileHandler(TimedRotatingFileHandler):
    """
    Rotates logs daily, storing each day's logs in a subdirectory named by date (YYYY-MM-DD)
    """
    def __init__(self, base_dir, filename=__package__ + ".log", **kwargs):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Initial directory for today
        today_dir = self.base_dir / datetime.utcnow().strftime("%Y-%m-%d")
        today_dir.mkdir(exist_ok=True)
        filepath = today_dir / filename
        super().__init__(str(filepath), **kwargs)

    def doRollover(self):
        """
        Override rollover to move to new directory
        """
        self.stream.close()
        # Determine new day's directory
        today_dir = self.base_dir / datetime.utcnow().strftime("%Y-%m-%d")
        today_dir.mkdir(exist_ok=True)
        self.baseFilename = str(today_dir) + "/" + Path(self.baseFilename).name
        self.stream = self._open()