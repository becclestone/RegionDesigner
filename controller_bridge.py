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
# corresponding queue - _drain() below clears it immediately before the next send,
# so that call's own .get() can't mistake the stale reply for its own.
_DEFAULT_AUTOFOCUS_TIMEOUT_S = 60.0
_DEFAULT_SCAN_TIMEOUT_S = 120.0

# MEMS (scanning-mirror) sine-drive defaults - DOVER_UI/Windows/main_window.py:349-362,
# same values as DOVER_UI/Windows/main_window_layouts.py's cX_AMPLITUDE_RESET etc. The
# controller (dover_ctl2/src/MsgHandler.cpp:1166-1192) applies these directly to the
# galvo/mirror driver and gates real scans on this having been sent at least once
# (mems_profile_loaded); DOVER_UI sends it automatically on cDISCOVER_MEMS reply.
# RegionDesigner has no discover-MEMS flow, so this is pushed directly instead.
_MEMS_Y_VOLTAGE = 0.0
_MEMS_X_AMPLITUDE = 0.78
_MEMS_X_FREQUENCY = 2265
_MEMS_SAMPLING_RATE = 20000
_MEMS_SAMPLE_POINTS = 20000
_MEMS_V_DIFFERENCE = 25
_MEMS_CUTOFF = 2750


def _drain(q: Queue) -> None:
    """Discards any stale reply left behind by a previous timed-out call, so the
    .get() that follows is guaranteed to consume the reply to *this* request
    rather than a leftover one (see the _busy docstring note above)."""
    while True:
        try:
            q.get_nowait()
        except Empty:
            return


class ControllerBridge(QObject):
    imageReady = Signal(str)
    commandError = Signal(str)
    # Live per-section scan/reconstruction progress - master-grid row/col, and
    # whether the section just entered (True) or left (False) that state. Same
    # cPATH_SCAN_ON/OFF_MSG and cRECON_ON/OFF_MSG broadcasts DOVER_UI's own
    # demo_control_a.py uses to color its scan-path canvas per section; unlike
    # cPATH_UPDATE_MSG (see run_path_scan's docstring), these fire once per
    # section, independent of whether the path itself is open- or closed-loop.
    sectionScanning = Signal(int, int, bool)        # master_row, master_col, is_scanning
    sectionReconstructing = Signal(int, int, bool)  # master_row, master_col, is_reconstructing

    def __init__(self):
        super().__init__()
        self._rmq_queue: Queue = Queue(maxsize=100)
        self._autofocus_reply_queue: Queue = Queue()
        self._path_reply_queue: Queue = Queue()
        self._anchor_reply_queue: Queue = Queue()
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

        self.send_mems_run_profile()

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
        elif msg_type in (ic.cPATH_SCAN_ON_MSG, ic.cPATH_SCAN_OFF_MSG):
            payload = msg.get_msg_payload()
            self.sectionScanning.emit(
                payload[ic.cSECTION_ROW], payload[ic.cSECTION_COL], msg_type == ic.cPATH_SCAN_ON_MSG
            )
        elif msg_type in (ic.cRECON_ON_MSG, ic.cRECON_OFF_MSG):
            payload = msg.get_msg_payload()
            self.sectionReconstructing.emit(
                payload[ic.cSECTION_ROW], payload[ic.cSECTION_COL], msg_type == ic.cRECON_ON_MSG
            )
        elif msg_type == src.cREPLY_MSG and msg.get_msg_reply_type() == ic.cPATH_MSG:
            self._path_reply_queue.put(("ack", msg.get_msg_payload()))
        elif msg_type == src.cREPLY_MSG and msg.get_msg_reply_type() == ic.cSET_ANCHOR_POINT_MSG:
            self._anchor_reply_queue.put(msg.get_msg_payload())

    def send_mems_run_profile(self) -> None:
        """Fire-and-forget, mirrors DOVER_UI/Windows/main_window.py:889-894
        (send_mems_run_profile). Programs the scanning-mirror sine driver on the
        controller and marks mems_profile_loaded there; without this, a real scan
        either rides on whatever profile a previously-run DOVER_UI instance left
        behind, or is rejected outright (cMEMS_PROFILE_NOT_LOADED) on a fresh
        controller process. No reply is defined for this message on the controller
        side, so this doesn't block waiting for one."""
        payload = {
            ic.cTAB_SENDER: "region_designer",
            ic.cENABLE_DIGITAL_OUT: False,
            ic.cX_SIGNAL_FORM: True,  # sine, not DC
            ic.cY_VOLTAGE: _MEMS_Y_VOLTAGE,
            ic.cX_AMPLITUDE: _MEMS_X_AMPLITUDE,
            ic.cX_FREQUENCY: _MEMS_X_FREQUENCY,
            ic.cCUTOFF: _MEMS_CUTOFF,
            ic.cV_DIFFERENCE: _MEMS_V_DIFFERENCE,
            ic.cSAMPLING_RATE: _MEMS_SAMPLING_RATE,
            ic.cSAMPLE_POINTS: _MEMS_SAMPLE_POINTS,
            ic.cRED_LASER_ON: False,
            ic.cZ_POSITION: 0.0,
        }
        cmd = TIsMsg.create_cmd_msg(ic.cSET_MEMS_RUN_PROFILE_MSG, ic.CTL_TARGET)
        cmd.add_msg_payload(payload)
        cmd.send_q_destroy()

    def request_snap(self):
        """Mirrors DOVER_UI/Windows/demo_control_a.py's Snap button sequence exactly."""
        sm.safemon_action_snap()
        sleep(0.3)
        move_to_snap_position()

    def is_busy(self) -> bool:
        return self._busy

    def run_autofocus(self, row: int, col: int, z_start: float, z_step: float, num_layers: int,
                       timeout: float = _DEFAULT_AUTOFOCUS_TIMEOUT_S) -> list:
        """Blocking - call from a worker thread, never the GUI thread. Returns the
        raw focus-metric curve (cPLOT_FOCUS_VALUES_PARAM); z per index is
        z_start + i*z_step (not echoed back by the controller, so we reconstruct it).

        row/col must already be final master-grid values (same convention as
        run_single_section_scan below). Uses the row/col branch, not absolute X/Y:
        reading dover_ctl2 MsgHandler.cpp:1803-1820 this session found that the
        absolute-XY branch passes its X straight through as the sweep's start
        position, skipping the FocusXPositionStart offset the row/col branch
        applies - a real alignment bug, and consistent with it never having been
        exercised by any client (DOVER_UI's own Python client only ever sends
        row/col). For sub-cell precision despite only-whole-row/col addressing,
        autofocus_client.py temporarily shifts the controller's anchor via
        set_anchor() below before calling this."""
        if self._busy:
            raise RuntimeError("Another hardware operation is already in progress.")
        self._busy = True
        try:
            payload = {
                ic.cCALCULATION_POSITION_TYPE_PARAM: False,
                ic.cMASTER_SECTION_ROW_PARAM: row,
                ic.cMASTER_SECTION_COL_PARAM: col,
                ic.cNUMBER_OF_FOCUS_LAYERS_PARAM: num_layers,
                ic.cSTARTING_Z_FOCUS_CALCULATION_PARAM: z_start,
                ic.cFOCUS_CALCULATION_STEP_PARAM: z_step,
            }
            msg = TIsMsg.create_cmd_msg(ic.cCALCULATE_AUTOFOCUS_MSG, ic.CTL_TARGET)
            msg.add_msg_payload(payload)
            _drain(self._autofocus_reply_queue)
            msg.send_q_destroy()

            reply_payload = self._autofocus_reply_queue.get(timeout=timeout)
            return reply_payload[ic.cPLOT_FOCUS_VALUES_PARAM]
        finally:
            self._busy = False

    def set_anchor(self, x: float, y: float, z: float, z_offset: float,
                    timeout: float = _DEFAULT_AUTOFOCUS_TIMEOUT_S) -> None:
        """Blocking - sets the controller's master-grid anchor (g_anchor_master_grid_X/Y,
        g_scan_position_z, g_focus_offset_correction) via cSET_ANCHOR_POINT_MSG, the
        same message DOVER_UI's Stage Calibration tab sends. There's no query message
        for this state (see stage_calibration.py's module docstring), so every call
        here must pass all four values - a partial update isn't possible without
        risking clobbering Z/Z-offset with a stale value. Used by autofocus_client.py
        to temporarily shift X/Y for one focus point's autofocus call, then restore
        the real calibration immediately after; also used as a safety net to
        (re)assert the real calibration right before a scan."""
        if self._busy:
            raise RuntimeError("Another hardware operation is already in progress.")
        self._busy = True
        try:
            payload = {
                ic.cTAB_SENDER: "region_designer",
                ic.cX_POS_PM: x,
                ic.cY_POS_PM: y,
                ic.cZ_POS_PM: z,
                ic.cZO_POS: z_offset,
            }
            msg = TIsMsg.create_cmd_msg(ic.cSET_ANCHOR_POINT_MSG, ic.CTL_TARGET)
            msg.add_msg_payload(payload)
            _drain(self._anchor_reply_queue)
            msg.send_q_destroy()

            reply_payload = self._anchor_reply_queue.get(timeout=timeout)
            if reply_payload.get(src.cERROR) != src.cSUCCESS:
                raise RuntimeError(reply_payload.get(src.cERROR_STR, "Anchor point rejected by controller."))
        finally:
            self._busy = False

    def send_scan_section_binding_rect(self, tl: tuple[int, int], br: tuple[int, int]) -> None:
        """Fire-and-forget, mirrors DOVER_UI's Utilities/path_utilites.py:
        send_scan_section_binding_rect. Tells the Stitcher the master-grid
        row/col bounding box (inclusive) of the sections about to be scanned,
        which it needs to allocate the final stitched output image - must be
        sent before run_path_scan below, not after, since run_path_scan here
        blocks until the whole path finishes (unlike DOVER_UI's own
        fire-and-forget path send)."""
        payload = {
            ic.cRECTANGLE_TL_ROW_PARAM: tl[0],
            ic.cRECTANGLE_TL_COL_PARAM: tl[1],
            ic.cRECTANGLE_BR_ROW_PARAM: br[0],
            ic.cRECTANGLE_BR_COL_PARAM: br[1],
        }
        msg = TIsMsg.create_cmd_msg(ic.cIMAGE_RECTANGLE_SIZE_CMD, ic.STITCHER_TARGET)
        msg.add_msg_payload(payload)
        msg.send_q_destroy()

    def run_single_section_scan(self, row: int, col: int, z: float,
                                 timeout: float = _DEFAULT_SCAN_TIMEOUT_S) -> None:
        """Blocking - call from a worker thread, never the GUI thread. Single-
        section special case of run_path_scan (see its docstring for the
        row/col convention and the cPATH_MSG mechanism itself)."""
        self.run_path_scan([(row, col, z)], timeout=timeout)

    def run_path_scan(self, sections: list[tuple[int, int, float]], timeout: float | None = None) -> None:
        """Blocking - call from a worker thread, never the GUI thread. Submits a
        multi-entry Lucas path [[row, col, z, False, 0], ...] - one entry per
        (row, col, z) in `sections` - via the normal cPATH_MSG mechanism
        (validated against a real single-section scan example this session; a
        multi-entry path is the same mechanism, just longer). Each row/col here
        must already be the FINAL master-grid values (i.e. caller adds the
        calibration's offset_row/offset_col) - cSECTION_ROW/cSECTION_COL are sent
        as 0 to pin the controller's own anchor offset to zero for this call, so
        physical positioning and the resulting output filenames depend only on
        the row/col given here, not on whatever anchor state the controller
        happens to hold at the moment (see stage_calibration.py).

        Tracing MsgHandler.cpp's scan_completed() (the only place cPATH_UPDATE_MSG
        is built) and ProcessingTask.cpp's operator()/cmf_operator this session
        confirmed it fires exactly ONCE per submitted path, after every section in
        it has been scanned - never once per section. So there's no per-section
        progress to report here, and `timeout` (default: _DEFAULT_SCAN_TIMEOUT_S
        per section, to scale with a path this long taking proportionally longer)
        is a budget for the WHOLE path, not one section.

        KNOWN TRADEOFF vs DOVER_UI's own "Image-Path" Scan button: cIS_LUCAS_PATH:
        True below routes the controller to ProcessingTask.cpp's
        calculated_path_operator - an open-loop operator that drives straight to
        the given z with no live refinement. DOVER_UI's real Scan button instead
        sends cIS_LUCAS_PATH: False (Utilities/path_utilites.py's send_path),
        which routes to cmf_operator/calculated_plane_operator - a closed-loop
        operator that live-autofocuses on designated "focus-owner" sections
        during the scan itself and propagates that measured Z to neighboring
        sections via a donor scheme (section_utilities.py's find_focus_donor).
        So a Lucas-path scan's image sharpness depends entirely on how accurate
        the given z already is (RegionDesigner's own autofocus+confirm+plane-fit
        pipeline) - it will never self-correct drift/tilt the way a real Scan
        does. Confirmed by tracing MsgHandler.cpp/ProcessingTask.cpp/
        section_utilities.py this session; not something this method can fix by
        itself (would require sending the focus-owner/donor path structure
        instead of a literal z per section, i.e. a different feature)."""
        if self._busy:
            raise RuntimeError("Another hardware operation is already in progress.")
        if not sections:
            raise ValueError("run_path_scan needs at least one (row, col, z) section.")
        if timeout is None:
            timeout = _DEFAULT_SCAN_TIMEOUT_S * len(sections)
        self._busy = True
        try:
            # Tell the Stitcher the output image's tile-grid bounding box before
            # driving the scan, for every path length including a single (1x1)
            # section - this call blocks until the whole path completes, so the
            # Stitcher must learn the size up front rather than after.
            rows = [row for row, col, z in sections]
            cols = [col for row, col, z in sections]
            self.send_scan_section_binding_rect((min(rows), min(cols)), (max(rows), max(cols)))

            sm.safemon_action_scan()
            sleep(0.3)

            path_json = json.dumps([[row, col, z, False, 0] for row, col, z in sections])
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
                # dover_ctl2/src/MsgHandler.cpp:932-933 (handle_path) reads these two
                # unconditionally for every cPATH_MSG, regardless of scan_type - a
                # missing field auto-vivifies as JSON null on the controller side,
                # which throws "type must be boolean, but is null" there. This is
                # never actually a resumed scan here, so False/"" (DOVER_UI's own
                # defaults - see Utilities/path_utilites.py's send_execute_path_msg).
                ic.cIS_RESUMED_SCAN_PATH: False,
                ic.cIS_RESUME_DIRECTORY_NAME: "",
            }
            msg = TIsMsg.create_cmd_msg(ic.cPATH_MSG, ic.CTL_TARGET)
            msg.add_msg_payload(payload)
            _drain(self._path_reply_queue)
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
