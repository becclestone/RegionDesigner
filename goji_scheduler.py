"""Background scheduled jobs DOVER_UI's MessageHandler.run_message_handler runs
alongside the Dover Controller window (DOVER_UI/MessageHandler.py:757-787): a GOJI
laser info poll every 2 minutes, plus scheduled auto-start (weekdays 06:30) and
auto-stop (weekdays 18:00) - the exact literal times DOVER_UI uses, confirmed from
run_message_handler's own goji_start_time/goji_stop_time. Ported as its own always-
running background thread so these keep working whether or not the operator has the
Dover Controller window open, same as DOVER_UI (where this is one shared thread
independent of which window has focus).

DOVER_UI's daily temperature-log rotation (main_window.py's _CURRENT_TEMP_LOG_FILE)
is reproduced here simply by naming the log file after the current date
(execution_date_stamp()) - a new file starts automatically when the date changes, so
no separate rotation step is needed.
"""
import csv
import os
import threading
import time

from PySide6.QtCore import QObject, Signal

from Constants import project_constants as pc
from controller_bridge import ControllerBridge
from Utilities import goji_controller as goji
from Utilities.time_utilities import execution_date_stamp, execution_time_stamp

try:
    import schedule
except ImportError:
    schedule = None

_GOJI_START_TIME = "06:30"
_GOJI_STOP_TIME = "18:00"
_POLL_INTERVAL_MINUTES = 2
_TEMP_LOG_DIR = "TEMP"
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


class GojiScheduler(QObject):
    gojiInfoUpdated = Signal(str, str, str)  # temperature, frequency, amplifier current
    gojiStatusChanged = Signal(bool)  # True = laser on

    def __init__(self, bridge: ControllerBridge):
        super().__init__()
        self._bridge = bridge
        self._running = False
        self._thread: threading.Thread | None = None
        # Mirrors DOVER_UI's DETECTION_AUTO_MORNING_START_CB/DETECTION_SHUTDOWN_CB
        # checkboxes (both default-checked) - set directly by
        # dover_controller/lasers_tab.py rather than through a callback.
        self.auto_start_enabled = True
        self.auto_stop_enabled = True

    def start(self) -> None:
        if not pc.USE_DETECTION_LASER or schedule is None:
            return
        os.makedirs(_TEMP_LOG_DIR, exist_ok=True)
        for day in _WEEKDAYS:
            getattr(schedule.every(), day).at(_GOJI_START_TIME).do(self._scheduled_start)
            getattr(schedule.every(), day).at(_GOJI_STOP_TIME).do(self._scheduled_stop)
        schedule.every(_POLL_INTERVAL_MINUTES).minutes.do(self._poll_info)

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        # Matches DOVER_UI's own MessageHandler thread: the initial poll and every
        # later scheduled firing (including a Modbus round-trip) run synchronously on
        # this one background thread, never the GUI thread.
        self._poll_info()
        self._poll_status()
        while self._running:
            schedule.run_pending()
            time.sleep(1)

    def _scheduled_start(self) -> None:
        if not self.auto_start_enabled:
            return
        if self._bridge.is_busy():
            # A simpler, single-process equivalent of DOVER_UI's cross-window "don't
            # stop during a scan" notification (main_window.py:1605-1626), which
            # doesn't apply here since there's no separate Path/Image-Path window -
            # just skip this firing rather than collide with an in-flight scan/
            # autofocus; the operator can always start it manually from the Lasers tab.
            return
        status = goji.set_goji_on()
        self.gojiStatusChanged.emit(status == 1)

    def _scheduled_stop(self) -> None:
        if not self.auto_stop_enabled:
            return
        if self._bridge.is_busy():
            return
        status = goji.set_goji_off()
        self.gojiStatusChanged.emit(status == 1)

    def _poll_info(self) -> None:
        temp, freq, amp = goji.get_goji_info()
        self.gojiInfoUpdated.emit(temp, freq, amp)
        self._log_temperature(temp)

    def _poll_status(self) -> None:
        status = goji.get_goji_on_status()
        self.gojiStatusChanged.emit(status == 1)

    def _log_temperature(self, temp: str) -> None:
        if not temp:
            return
        path = os.path.join(_TEMP_LOG_DIR, f"{execution_date_stamp()}.csv")
        with open(path, "a", newline="") as f:
            csv.writer(f).writerow([execution_time_stamp(), temp])
