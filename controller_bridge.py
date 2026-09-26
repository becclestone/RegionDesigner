"""Qt-signal bridge over the existing IsMsgPy/RabbitMQ-STOMP layer.

IsMsgPy (rmq_setup.py, IsListener.py, IsMsg.py) has no FreeSimpleGUI dependency,
so it's reused as-is (as its own git submodule here, same as in DOVER_UI). The
only thing rebuilt is the dispatch step: instead of DOVER_UI/MessageHandler.py's
habit of stuffing live sg.Window references into a queue the GUI-thread event loop
polls, a background thread reads the same thread-safe queue.Queue and re-emits the
small set of message types this tool cares about as Qt signals. Qt automatically
marshals a signal emitted from a non-GUI thread to the receiving QObject's (GUI)
thread, so no extra locking is needed here.
"""
import json
from queue import Queue, Empty
from threading import Thread
from time import sleep

from PySide6.QtCore import QObject, Signal

from Constants import implementation_constants as ic, rmq_credentials
import IsMsgPy.shared_rmq_constants as src
from IsMsgPy.IsMsg import TIsMsg
from IsMsgPy.rmq_setup import LoginCredentials, rmq_setup, rmg_log_hb_setup
from Utilities import safemon_msg_senders as sm
from Utilities.controller_commands import move_to_snap_position

# cCALCULATE_AUTOFOCUS_MSG and cPATH_MSG replies (cCALCULATED_FOCUS_VALUES_MSG,
# cPATH_UPDATE_MSG) carry no per-request correlation id - they're fresh broadcast
# COMM messages, not TBR-routed replies (confirmed by reading MsgHandler.cpp). So
# only one such hardware-affecting request may be in flight at a time; _busy below
# enforces that. A request that times out can leave one stale reply behind in the
# corresponding queue for the next call to (harmlessly) drain first.
_DEFAULT_AUTOFOCUS_TIMEOUT_S = 60.0
_DEFAULT_SCAN_TIMEOUT_S = 120.0


class ControllerBridge(QObject):
    imageReady = Signal(str)
    commandError = Signal(str)

    def __init__(self):
        super().__init__()
        self._rmq_queue: Queue = Queue(maxsize=100)
        self._autofocus_reply_queue: Queue = Queue()
        self._path_reply_queue: Queue = Queue()
        self._running = False
        self._busy = False
        self._listener_thread: Thread | None = None

    def connect_to_controller(self):
        credentials = LoginCredentials(
            login=rmq_credentials.USER_NAME,
            passcode=rmq_credentials.USER_PASSCODE,
            host=rmq_credentials.HOST,
            vhost=rmq_credentials.VHOST_NAME,
            port=rmq_credentials.PORT,
        )
        # Reuses GUI_TARGET ("dover_ui", == rmq_credentials.USER_NAME) as the sender
        # signature: the controller addresses its image-ready reply to that fixed
        # target rather than to a per-requester id, so this tool must answer to the
        # same name to receive it (a second, independent subscriber - this does not
        # take messages away from the existing FreeSimpleGUI app if it's also running).
        rmq_setup(msg_signature=rmq_credentials.USER_NAME, q=self._rmq_queue, credentials=credentials)
        rmg_log_hb_setup(credentials)

        self._running = True
        self._listener_thread = Thread(target=self._dispatch_loop, daemon=True)
        self._listener_thread.start()

    def _dispatch_loop(self):
        while self._running:
            try:
                msg = self._rmq_queue.get(True, 0.05)
            except Empty:
                continue
            self._handle_message(msg)

    def _handle_message(self, msg):
        if msg.get_msg_class() != src.cCOMM_MSG_CLASS:
            return
        msg_type = msg.get_msg_type()
        if msg_type == ic.cIMAGE_READY_MSG:
            payload = msg.get_msg_payload()
            self.imageReady.emit(payload[ic.cIMAGE_PATH])
        elif msg_type == ic.cCALCULATED_FOCUS_VALUES_MSG:
            self._autofocus_reply_queue.put(msg.get_msg_payload())
        elif msg_type == ic.cPATH_UPDATE_MSG:
            self._path_reply_queue.put(("update", msg.get_msg_payload()))
        elif msg_type == src.cREPLY_MSG and msg.get_msg_reply_type() == ic.cPATH_MSG:
            self._path_reply_queue.put(("ack", msg.get_msg_payload()))

    def request_snap(self):
        """Mirrors DOVER_UI/Windows/demo_control_a.py's Snap button sequence exactly."""
        sm.safemon_action_snap()
        sleep(0.3)
        move_to_snap_position()

    def is_busy(self) -> bool:
        return self._busy

    def run_autofocus(self, x: float, y: float, z_start: float, z_step: float, num_layers: int,
                       timeout: float = _DEFAULT_AUTOFOCUS_TIMEOUT_S) -> list:
        """Blocking - call from a worker thread, never the GUI thread. Returns the
        raw focus-metric curve (cPLOT_FOCUS_VALUES_PARAM); z per index is
        z_start + i*z_step (not echoed back by the controller, so we reconstruct it).
        Uses absolute X/Y (dover_ctl2 MsgHandler.cpp:1803-1808 supports this server-side
        even though DOVER_UI's own Python client never actually sends this branch)."""
        if self._busy:
            raise RuntimeError("Another hardware operation is already in progress.")
        self._busy = True
        try:
            payload = {
                ic.cCALCULATION_POSITION_TYPE_PARAM: True,
                ic.cABSOLUTE_X_PARAM: x,
                ic.cABSOLUTE_Y_PARAM: y,
                ic.cNUMBER_OF_FOCUS_LAYERS_PARAM: num_layers,
                ic.cSTARTING_Z_FOCUS_CALCULATION_PARAM: z_start,
                ic.cFOCUS_CALCULATION_STEP_PARAM: z_step,
            }
            msg = TIsMsg.create_cmd_msg(ic.cCALCULATE_AUTOFOCUS_MSG, ic.CTL_TARGET)
            msg.add_msg_payload(payload)
            msg.send_q_destroy()

            reply_payload = self._autofocus_reply_queue.get(timeout=timeout)
            return reply_payload[ic.cPLOT_FOCUS_VALUES_PARAM]
        finally:
            self._busy = False

    def run_single_section_scan(self, row: int, col: int, z: float,
                                 timeout: float = _DEFAULT_SCAN_TIMEOUT_S) -> None:
        """Blocking - call from a worker thread, never the GUI thread. Submits a
        single-entry Lucas path [[row, col, z, False, 0]] via the normal cPATH_MSG
        mechanism (validated against a real single-section scan example this
        session - a 1-entry path is a completely normal operation). row/col here
        must already be the FINAL master-grid values (i.e. caller adds the
        calibration's offset_row/offset_col) - cSECTION_ROW/cSECTION_COL are sent
        as 0 to pin the controller's own anchor offset to zero for this call, so
        physical positioning and the resulting output filename both depend only on
        the row/col given here, not on whatever anchor state the controller
        happens to hold at the moment (see stage_calibration.py)."""
        if self._busy:
            raise RuntimeError("Another hardware operation is already in progress.")
        self._busy = True
        try:
            sm.safemon_action_scan()
            sleep(0.3)

            path_json = json.dumps([[row, col, z, False, 0]])
            payload = {
                ic.cTAB_SENDER: "region_designer",
                ic.cPATH_SCAN_TYPE: True,
                ic.cSELECTED_MOTION_PROFILE: 2,
                ic.cSCAN_ACCELERATION: 200,
                ic.cSCAN_JERK: 0,
                ic.cFOCUS_ACCELERATION: 150,
                ic.cFOCUS_JERK: 0,
                ic.cJOG_ACCELERATION: 400,
                ic.cJOG_JERK: 0,
                ic.cIGNORE_TIMING: True,
                ic.cRUN_SCAN_TRACE: False,
                ic.cSECTION_ROW: 0,
                ic.cSECTION_COL: 0,
                ic.cPATH: path_json,
                ic.cIS_LUCAS_PATH: True,
            }
            msg = TIsMsg.create_cmd_msg(ic.cPATH_MSG, ic.CTL_TARGET)
            msg.add_msg_payload(payload)
            msg.send_q_destroy()

            kind, reply_payload = self._path_reply_queue.get(timeout=timeout)
            if kind == "ack":
                if reply_payload.get(src.cERROR) != src.cSUCCESS:
                    raise RuntimeError(reply_payload.get(src.cERROR_STR, "Path rejected by controller."))
                # Accepted - now wait for the actual completion notice.
                kind, reply_payload = self._path_reply_queue.get(timeout=timeout)

            if reply_payload.get(src.cERROR) != src.cSUCCESS:
                raise RuntimeError(reply_payload.get(src.cERROR_STR, "Scan failed."))
        finally:
            self._busy = False

    def shutdown(self):
        self._running = False
