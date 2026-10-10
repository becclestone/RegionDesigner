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
from time import monotonic, sleep

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
# Matches DOVER_UI/Utilities/path_utilites.py's _MAX_PATH_FRAGMENT_SIZE - a cPATH_MSG
# longer than this must be pre-loaded in chunks (cPATH_LOAD_FRAGMENT) before the final
# chunk actually starts the scan; see _send_path_msg.
_MAX_PATH_FRAGMENT_SIZE = 25

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
    # Generic dispatch for the Dover Controller window's many tabs (dover_controller/) -
    # avoids ~30 new named signals turning this class into a god-object; mirrors DOVER_UI's
    # own single-dispatcher shape (handle_window_events), just split per-tab instead of one
    # 450-line method. Each tab connects and filters by the (str) key itself.
    replyReceived = Signal(str, dict)       # reply_type, payload - any src.cREPLY_MSG not
                                             # already handled above (see _handle_message)
    broadcastReceived = Signal(str, dict)   # msg_type, payload - any other COMM broadcast
                                             # not already handled above
    temperatureUpdated = Signal(dict)       # payload keyed by ic.cTEMP_SENSOR_1..7

    def __init__(self):
        super().__init__()
        self._rmq_queue: Queue = Queue(maxsize=100)
        self._autofocus_reply_queue: Queue = Queue()
        self._path_reply_queue: Queue = Queue()
        self._anchor_reply_queue: Queue = Queue()
        self._reconstruction_reply_queue: Queue = Queue()
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
            # Mirrors DOVER_UI/Windows/demo_control_a.py's load_snapped_image:
            # the snap sequence (request_snap above) turns the overview camera
            # light on, so it must be turned back off once the image is in -
            # otherwise it's left on indefinitely since nothing else does this.
            sm.safemon_action_camera_light_off()
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
            row, col = payload[ic.cSECTION_ROW], payload[ic.cSECTION_COL]
            is_on = msg_type == ic.cRECON_ON_MSG
            self.sectionReconstructing.emit(row, col, is_on)
            if not is_on:
                self._reconstruction_reply_queue.put((row, col))
        elif msg_type == src.cREPLY_MSG and msg.get_msg_reply_type() == ic.cPATH_MSG:
            self._path_reply_queue.put(("ack", msg.get_msg_payload()))
        elif msg_type == src.cREPLY_MSG and msg.get_msg_reply_type() == ic.cSET_ANCHOR_POINT_MSG:
            self._anchor_reply_queue.put(msg.get_msg_payload())
        elif msg_type == ic.cTEMP_UPDATE_MSG:
            # Dedicated signal (not folded into broadcastReceived below) - every Dover
            # Controller tab/status strip cares about it and it's high-frequency.
            self.temperatureUpdated.emit(msg.get_msg_payload())
        elif msg_type == ic.cINTERNAL_CONFIG_VALUES_MSG:
            # Mirrors MessageHandler.handle_internal_config_msg: the reply's own
            # fSenderSignature (which module answered - CTL_TARGET/RECONSTRUCTION_TARGET/
            # STITCHER_TARGET/SAFE_MON_TARGET) says which fields the payload carries and
            # which tab's widgets to resync, so it's folded into the payload dict here
            # rather than dropped - see request_internal_config's docstring.
            payload = dict(msg.get_msg_payload())
            payload["_sender"] = msg.get_sender_signature()
            self.broadcastReceived.emit(msg_type, payload)
        elif msg_type == src.cREPLY_MSG:
            # Catch-all for every other request/reply pair the Dover Controller window's
            # tabs need (discover stages/MEMS, position update, reset stage, section/axis/
            # compound moves, MEMS start/stop/profile, path-offset, focus-adjustment start) -
            # see controller_window.py and the per-tab widgets, which filter by reply type
            # themselves rather than this class growing one named signal per message.
            self.replyReceived.emit(msg.get_msg_reply_type(), msg.get_msg_payload())
        else:
            # Catch-all for every other COMM broadcast (e.g. none currently routed
            # elsewhere) - same generic-signal rationale as replyReceived above.
            self.broadcastReceived.emit(msg_type, msg.get_msg_payload())

    def send_mems_run_profile(self, payload: dict | None = None) -> None:
        """Fire-and-forget, mirrors DOVER_UI/Windows/main_window.py:889-894
        (send_mems_run_profile). Programs the scanning-mirror sine driver on the
        controller and marks mems_profile_loaded there; without this, a real scan
        either rides on whatever profile a previously-run DOVER_UI instance left
        behind, or is rejected outright (cMEMS_PROFILE_NOT_LOADED) on a fresh
        controller process. No reply is defined for this message on the controller
        side, so this doesn't block waiting for one.

        payload=None (the only way this was ever called before the Dover Controller
        window's MEMS tab existed) keeps today's fixed-default scan-time behavior,
        sent once from connect_to_controller(). The MEMS tab instead passes its own
        live slider values here for its "Set MEMS run profile" button."""
        if payload is None:
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

    # ------------------------------------------------------------------
    # Dover Controller window support (dover_controller/) - fire-and-forget
    # commands for the 8 tabs ported from DOVER_UI/Windows/main_window.py. None
    # of these participate in _busy (DOVER_UI itself doesn't gate most of them
    # on anything but its own in_motion flag, which the tabs track themselves
    # via controller_window.py's set_motion_locked) - see the refactor plan's
    # "accepted risks" section for the reply-correlation caveat this inherits.

    def safemon_autofocus_start(self) -> None:
        """Mirrors the safemon interlock the Autofocus tab's "Calculate focus" button
        sends before the request (main_window.py:1133) - kept as a thin bridge wrapper
        (not called from inside run_autofocus/run_autofocus_full) so the existing
        autofocus_client.py pipeline's behavior is unchanged; only the interactive tab
        opts into it, same as DOVER_UI's own tab-button-scoped timing."""
        sm.safemon_action_autofocus()

    def safemon_idle(self) -> None:
        """Mirrors handle_calculated_focus_values' sm.safemon_action_idle() (main_window.py:1032) -
        see safemon_autofocus_start."""
        sm.safemon_action_idle()

    def send_param(self, msg_type: str, target: str, payload: dict) -> None:
        """Generic fire-and-forget single-message param push, for the many DOVER_UI
        System Config / MEMS-slider / Laser-power commands that are just one
        create_cmd_msg().add_msg_payload().send_q_destroy() each (see
        dover_controller/system_config_tab.py etc.) - keeps TIsMsg construction
        confined to this module without one near-identical named wrapper per
        message. Not for anything that waits for a reply or needs _busy guarding.
        An empty payload is sent with no payload object at all (DOVER_UI's own
        no-parameter commands, e.g. handle_stage_reset, never call add_msg_payload
        either) rather than an empty dict, to match exactly."""
        msg = TIsMsg.create_cmd_msg(msg_type, target)
        if payload:
            msg.add_msg_payload(payload)
        msg.send_q_destroy()

    # --- Manual Moves tab ---
    # Note: DOVER_UI's jog-style "Pre-programmed Moves" tab (pc.MOVES_TAB -
    # start_compound_moves/start_x/y/z_moves, cSTART_COMPOUND_MOVE_MSG/
    # cSTART_X/Y/Z_MOVE_MSG) is commented out of its own make_main_layout()
    # (main_window_layouts.py:1253) - dead code, not reachable in the live app - so
    # it's intentionally not ported here. The real "Manual Moves" tab is absolute/
    # programmed moves only (move_axis_absolute/move_xyz_absolute below).

    def move_axis_absolute(self, axis: str, pos: float, speed: float) -> None:
        """Mirrors MainWindow.move_stage_x_pm/move_stage_y_pm/move_stage_z_pm
        (main_window.py:585-639) - a single-axis programmed (absolute position) move.
        axis is 'x', 'y', or 'z'."""
        msg_type, pos_key, speed_key = {
            "x": (ic.cX_MOVE_PM_MSG, ic.cX_POS_PM, ic.cX_SPEED_VAL),
            "y": (ic.cY_MOVE_PM_MSG, ic.cY_POS_PM, ic.cY_SPEED_VAL),
            "z": (ic.cZ_MOVE_PM_MSG, ic.cZ_POS_PM, ic.cZ_SPEED_VAL),
        }[axis]
        self.send_param(msg_type, ic.CTL_TARGET, {ic.cTAB_SENDER: "region_designer", pos_key: pos, speed_key: speed})

    def move_xyz_absolute(self, x: float, x_speed: float, y: float, y_speed: float,
                           z: float, z_speed: float) -> None:
        """Mirrors MainWindow.move_stages_pm (main_window.py:645-670) - all 3 axes to an
        absolute position at once, each at its own speed."""
        payload = {
            ic.cTAB_SENDER: "region_designer",
            ic.cX_POS_PM: x, ic.cX_SPEED_VAL: x_speed,
            ic.cY_POS_PM: y, ic.cY_SPEED_VAL: y_speed,
            ic.cZ_POS_PM: z, ic.cZ_SPEED_VAL: z_speed,
        }
        self.send_param(ic.cMOVE_PM_MSG, ic.CTL_TARGET, payload)

    def reset_stage(self) -> None:
        """Mirrors MainWindow.handle_stage_reset (main_window.py:774-779) - moves to
        the load position; the "Reset Stage" button."""
        self.send_param(ic.cRESET_STAGE_MSG, ic.CTL_TARGET, {})

    def request_position_update(self) -> None:
        """Mirrors MainWindow.get_current_position_values (main_window.py:229-231) -
        fire-and-forget request; the reply (cPOSITION_UPDATE_MSG, fields
        cX/Y/Z_POSITION_READ) arrives via replyReceived, not returned here."""
        self.send_param(ic.cPOSITION_UPDATE_MSG, ic.CTL_TARGET, {})

    def discover_stages(self) -> None:
        """Mirrors Utilities/utilities.py's discover_stages - part of the "Connect"
        button (DOVER_UI's connect() also calls discover_mems/request_internal_config,
        done as 3 separate bridge calls here instead of one combined method, so each
        tab can trigger just the one it owns if it ever needs to)."""
        self.send_param(ic.cDISCOVER_STAGES_MSG, ic.CTL_TARGET, {})

    def discover_mems(self) -> None:
        """Mirrors Utilities/utilities.py's discover_mems - see discover_stages."""
        self.send_param(ic.cDISCOVER_MEMS, ic.CTL_TARGET, {})

    def request_internal_config(self) -> None:
        """Mirrors Utilities/utilities.py's ask_for_internal_config: fans the same
        cSEND_INTERNAL_CONFIG_UPDATE_MSG request out to all 4 modules that answer it
        (CTL_TARGET, RECONSTRUCTION_TARGET, STITCHER_TARGET, SAFE_MON_TARGET) - each
        replies with its own cINTERNAL_CONFIG_VALUES_MSG broadcast (routed via
        broadcastReceived, keyed by the "_sender" field _handle_message folds in),
        which is how the System Config tab resyncs its checkboxes/radios to whatever
        the backend actually has configured."""
        msg = TIsMsg.create_cmd_msg(ic.cSEND_INTERNAL_CONFIG_UPDATE_MSG, ic.CTL_TARGET)
        msg.send_q()
        msg.add_msg_target(ic.RECONSTRUCTION_TARGET)
        msg.send_q()
        msg.add_msg_target(ic.STITCHER_TARGET)
        msg.send_q()
        msg.add_msg_target(ic.SAFE_MON_TARGET)
        msg.send_q_destroy()

    # --- Section Moves tab ---

    def move_to_section(self, row: int, col: int, position_mode: str, scan_offset: int) -> None:
        """Mirrors MainWindow.execute_section_move (main_window.py:722-745).
        position_mode is one of DOVER_UI's own section-position radio-group values
        (e.g. project_constants.SCAN_START_R/SCAN_END_R/TOP_LEFT_CORNER_R/
        BOTTOM_RIGHT_CORNER_R/FOCUS_START_R/FOCUS_END_R, already present in this
        project's Constants/project_constants.py) - passed straight through
        unmodified, same as DOVER_UI's own self.section_position."""
        payload = {
            ic.cTAB_SENDER: "region_designer",
            ic.cSECTION_ROW: row, ic.cSECTION_COL: col,
            ic.cSECTION_POSITION: position_mode, ic.cSCAN_OFFSET: scan_offset,
        }
        self.send_param(ic.cSECTION_MOVE_MSG, ic.CTL_TARGET, payload)

    # --- Calibration tab ---

    def set_path_offset(self, offset_row: int, offset_col: int) -> None:
        """Mirrors MainWindow.send_path_offset_point (main_window.py:217-226) - the
        Calibration tab's "Set path grid offset" button; distinct message from
        set_anchor (cSET_ANCHOR_POINT_MSG, already implemented above)."""
        payload = {ic.cTAB_SENDER: "region_designer", ic.cSECTION_ROW: offset_row, ic.cSECTION_COL: offset_col}
        self.send_param(ic.cSET_PATH_OFFSET_MSG, ic.CTL_TARGET, payload)

    # --- Focus tab ---

    def send_focus_position_adjustment(self, payload: dict) -> None:
        """Mirrors MainWindow.handle_focus_tab_events' cZ_AXIS_POSITION_ADJUSTMENT sends
        (main_window.py:954-994, live-adjustment-while-in-motion and the "Send Update"/
        "Zero" buttons) - fire-and-forget, streamed while the Focus tab's Z slider moves.
        payload shape matches create_focus_calibration_payload (main_window.py:167-200):
        {cZ_POSITION, cZ_SPEED_VAL, cX_POS_PM, cY_POS_PM, cSECTION_ROW, cSECTION_COL,
        cMOTION_SELECTION, cPOSITION_SELECTION} - built by the tab itself since it alone
        knows whether it's in section-motion or absolute-position mode."""
        self.send_param(ic.cZ_AXIS_POSITION_ADJUSTMENT, ic.CTL_TARGET, payload)

    def start_focus_adjustment_motion(self, payload: dict) -> None:
        """Mirrors the "START X Axis Focus Moves" branch of handle_focus_tab_events
        (main_window.py:980-994) - same payload shape as send_focus_position_adjustment."""
        self.send_param(ic.cSTART_FOCUS_ADJUSTMENT, ic.CTL_TARGET, payload)

    def stop_focus_adjustment_motion(self) -> None:
        """Mirrors the "STOP X Axis Focus Moves" branch of handle_focus_tab_events
        (main_window.py:980-986)."""
        self.send_param(ic.cSTOP_FOCUS_ADJUSTMENT, ic.CTL_TARGET, {})

    # --- MEMS tab ---
    # send_mems_run_profile (above) also serves the MEMS tab's "Set MEMS run profile"
    # button, passing its own live payload instead of the fixed scan-time default.

    def start_mems_test_pattern(self, payload: dict) -> None:
        """Mirrors MainWindow.send_laser_reset_start_stop_msg's start branch
        (main_window.py:828-839) - MEMS testing with the red laser only; distinct
        code path/button from send_mems_run_profile (DOVER_UI has two separate
        frames/buttons for these). payload shape matches MainWindow.payload (main_window.py
        :349-362): cTAB_SENDER, cENABLE_DIGITAL_OUT, cX_SIGNAL_FORM, cY_VOLTAGE,
        cX_AMPLITUDE, cX_FREQUENCY, cCUTOFF, cV_DIFFERENCE, cSAMPLING_RATE,
        cSAMPLE_POINTS, cRED_LASER_ON, cZ_POSITION."""
        self.send_param(ic.cRESET_START_MEMS, ic.CTL_TARGET, payload)

    def stop_mems_test_pattern(self) -> None:
        """Mirrors send_laser_reset_start_stop_msg's stop branch - no payload."""
        self.send_param(ic.cRESET_STOP_MEMS, ic.CTL_TARGET, {})

    def update_mems_y_voltage(self, y_voltage: float, x_amplitude: float) -> None:
        """Mirrors MainWindow.send_y_voltage (main_window.py:794-799) - live push while
        the MEMS test pattern is running; DOVER_UI always sends both current values
        together regardless of which slider moved, reproduced as-is here."""
        self.send_param(ic.cUPDATE_Y_VOLTAGE, ic.CTL_TARGET, {ic.cY_VOLTAGE: y_voltage, ic.cX_AMPLITUDE: x_amplitude})

    def update_mems_x_amplitude(self, y_voltage: float, x_amplitude: float) -> None:
        """Mirrors MainWindow.send_x_voltage (main_window.py:801-806) - see
        update_mems_y_voltage for why both values are always sent together."""
        self.send_param(ic.cUPDATE_X_AMPLITUDE, ic.CTL_TARGET, {ic.cY_VOLTAGE: y_voltage, ic.cX_AMPLITUDE: x_amplitude})

    def update_mems_sampling_rate(self, rate: float) -> None:
        """Mirrors MainWindow.send_sampling_rate (main_window.py:816-820)."""
        self.send_param(ic.cUPDATE_SAMPLING_RATE, ic.CTL_TARGET, {ic.cSAMPLING_RATE: rate})

    def update_mems_v_difference(self, v: float) -> None:
        """Mirrors MainWindow.send_v_difference_rate (main_window.py:822-826)."""
        self.send_param(ic.cUPDATE_V_DIFFERENCE, ic.CTL_TARGET, {ic.cV_DIFFERENCE: v})

    def toggle_red_laser_without_mems(self) -> None:
        """Mirrors module-level toggle_red_laser (main_window.py:131-133) - "Toggle Red
        Laser" button, independent of the MEMS test pattern."""
        self.send_param(ic.cTOGGLE_ON_RED_LASER_WO_MEMS, ic.CTL_TARGET, {})

    # --- Lasers tab (OXXIUS only - GOJI/Amplitude is direct Modbus, see goji_controller.py) ---

    def oxxius_laser_on(self) -> None:
        """Mirrors MainWindow.oxxius_on (main_window.py:1557-1560)."""
        self.send_param(ic.cSTART_OXXIUS_LASER, ic.CTL_TARGET, {})

    def oxxius_laser_off(self) -> None:
        """Mirrors MainWindow.oxxius_off (main_window.py:1562-1565)."""
        self.send_param(ic.cSTOP_OXXIUS_LASER, ic.CTL_TARGET, {})

    def set_oxxius_power(self, scan_power: int, focus_power: int) -> None:
        """Mirrors MainWindow.oxxius_set_power (main_window.py:1567-1574)."""
        payload = {ic.cDETECTION_LASER_SCAN_POWER_LEVEL: scan_power, ic.cDETECTION_LASER_FOCUS_POWER_LEVEL: focus_power}
        self.send_param(ic.cSET_OXXIUS_POWER_MSG, ic.CTL_TARGET, payload)

    # --- System Config tab ---

    def send_quit(self, target: str) -> None:
        """Mirrors MainWindow.stop_module (main_window.py:106-108) - the System Config
        tab's "Stop Now" buttons (and, for whichever modules the "On UI Quit"
        checkboxes have checked, RegionDesignerWindow's own closeEvent) immediately
        quitting one backend module."""
        msg = TIsMsg.create_cmd_msg(src.cQUIT, target)
        msg.send_q_destroy()

    def safemon_motion(self) -> None:
        """Mirrors sm.safemon_action_motion(), sent before Move to Scan/Snap Position
        (main_window.py:1429-1434) - kept as a thin wrapper for the same reason as
        safemon_autofocus_start/safemon_idle above."""
        sm.safemon_action_motion()

    def wait_for_reconstruction_off(self, row: int, col: int, timeout: float) -> bool:
        """Blocks up to timeout for the controller's reconstruction-off
        broadcast for this specific (row, col) section. Returns True if it
        arrived, False on timeout - callers (see confirmation_scan.py's
        _wait_for_image) should treat False as "keep polling for the file
        directly" rather than an error. A reconstruction-off for some other
        section (e.g. a late arrival left over from a previous call) is
        discarded rather than satisfying this wait."""
        deadline = monotonic() + timeout
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            try:
                got_row, got_col = self._reconstruction_reply_queue.get(timeout=remaining)
            except Empty:
                return False
            if (got_row, got_col) == (row, col):
                return True

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
        return self._run_autofocus_request(row, col, z_start, z_step, num_layers, timeout)[ic.cPLOT_FOCUS_VALUES_PARAM]

    def run_autofocus_full(self, row: int, col: int, z_start: float, z_step: float, num_layers: int,
                            timeout: float = _DEFAULT_AUTOFOCUS_TIMEOUT_S) -> dict:
        """Same request as run_autofocus, but returns the full reply payload instead of
        just the curve - the Autofocus tab (dover_controller/autofocus_tab.py) also
        needs cCALCULATED_FOCUS_PARAM (the controller's suggested curve index), which
        run_autofocus's existing callers (autofocus_client.py) have never needed and
        whose return contract (list) stays unchanged for them."""
        return self._run_autofocus_request(row, col, z_start, z_step, num_layers, timeout)

    def _run_autofocus_request(self, row: int, col: int, z_start: float, z_step: float, num_layers: int,
                                timeout: float) -> dict:
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

            return self._autofocus_reply_queue.get(timeout=timeout)
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

            # Discard any reconstruction-off leftover from a previous call before
            # sending this one, same reasoning as _drain(self._path_reply_queue)
            # inside _send_path_msg - otherwise a stale broadcast from a prior scan
            # could satisfy a wait_for_reconstruction_off() call that belongs to
            # this one.
            _drain(self._reconstruction_reply_queue)

            path_rows = [[row, col, z, False, 0] for row, col, z in sections]
            self._send_path_msg(path_rows, is_lucas_path=True, timeout=timeout)
        finally:
            self._busy = False

    def run_plane_path_scan(self, rows: list[tuple[int, int, int, int]],
                             timeout: float | None = None) -> None:
        """Blocking - call from a worker thread, never the GUI thread. Sends `rows`
        (already the final San_Path_Planning [row, col, donor_idx, region] shape -
        see scan_path_export.py - row/col already the FINAL master-grid values, same
        convention as run_path_scan above) as a real CLOSED-LOOP Plane Path scan
        (cIS_LUCAS_PATH: False) - the same mechanism DOVER_UI's own Scan button and
        "Load Plane Path" use (Utilities/path_utilites.py's send_path), NOT this
        class's own run_path_scan above (open-loop Lucas path, literal z per
        section). donor_idx marks which rows the controller live-autofocuses during
        the scan itself (a row whose donor_idx equals its own position in the list)
        versus which borrow that measurement from a neighboring donor - the
        controller performs this propagation itself; there is no z in these rows
        for this method to use client-side at all.

        cPATH_UPDATE_MSG still fires exactly once, at the very end of the whole
        path (same as run_path_scan - see its docstring) - use this bridge's
        sectionScanning/sectionReconstructing signals for live per-section
        progress instead, which already fire the same way for this path as they
        do for a Lucas one."""
        if self._busy:
            raise RuntimeError("Another hardware operation is already in progress.")
        if not rows:
            raise ValueError("run_plane_path_scan needs at least one row.")
        if timeout is None:
            timeout = _DEFAULT_SCAN_TIMEOUT_S * len(rows)
        self._busy = True
        try:
            row_vals = [r[0] for r in rows]
            col_vals = [r[1] for r in rows]
            self.send_scan_section_binding_rect((min(row_vals), min(col_vals)), (max(row_vals), max(col_vals)))

            sm.safemon_action_scan()
            sleep(0.3)

            _drain(self._reconstruction_reply_queue)

            path_rows = [[int(row), int(col), int(donor_idx), False, int(region)]
                         for row, col, donor_idx, region in rows]
            self._send_path_msg(path_rows, is_lucas_path=False, timeout=timeout)
        finally:
            self._busy = False

    def _send_path_msg(self, path_rows: list[list], is_lucas_path: bool, timeout: float) -> None:
        """Fragments (if needed) and sends path_rows via cPATH_MSG, mirroring
        DOVER_UI's Utilities/path_utilites.py send_path's >_MAX_PATH_FRAGMENT_SIZE-
        row chunking: one cPATH_LOAD_FRAGMENT command per 25-row chunk (the
        controller just appends each into an accumulation buffer - dover_ctl2/src/
        MsgHandler.cpp's handle_path), then the final (possibly only) chunk via the
        normal cPATH_MSG scan-execute payload, which both starts the scan and
        terminates the accumulation. Blocks for the ack + completion replies.
        Shared by run_path_scan (Lucas) and run_plane_path_scan (Plane) above -
        they differ only in row shape and is_lucas_path; both already handle the
        binding-rect/safemon/reconstruction-queue steps that must happen first."""
        _drain(self._path_reply_queue)

        if len(path_rows) > _MAX_PATH_FRAGMENT_SIZE:
            frag_msg = TIsMsg.create_cmd_msg(ic.cPATH_LOAD_FRAGMENT, ic.CTL_TARGET)
            num_fragments = len(path_rows) // _MAX_PATH_FRAGMENT_SIZE
            for i in range(num_fragments):
                chunk = path_rows[i * _MAX_PATH_FRAGMENT_SIZE:(i + 1) * _MAX_PATH_FRAGMENT_SIZE]
                frag_msg.add_msg_payload({
                    ic.cTAB_SENDER: "region_designer",
                    ic.cPATH: json.dumps(chunk),
                    ic.cIS_LUCAS_PATH: is_lucas_path,
                })
                frag_msg.send_q()
                sleep(0.02)  # avoid flooding, same as DOVER_UI's send_path
            remainder = path_rows[num_fragments * _MAX_PATH_FRAGMENT_SIZE:]
        else:
            remainder = path_rows

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
            ic.cPATH: json.dumps(remainder),
            ic.cIS_LUCAS_PATH: is_lucas_path,
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
        msg.send_q_destroy()

        kind, reply_payload = self._path_reply_queue.get(timeout=timeout)
        if kind == "ack":
            if reply_payload.get(src.cERROR) != src.cSUCCESS:
                raise RuntimeError(reply_payload.get(src.cERROR_STR, "Path rejected by controller."))
            # Accepted - now wait for the actual completion notice.
            kind, reply_payload = self._path_reply_queue.get(timeout=timeout)

        if reply_payload.get(src.cERROR) != src.cSUCCESS:
            raise RuntimeError(reply_payload.get(src.cERROR_STR, "Scan failed."))

    def cancel_current_scan(self) -> None:
        """Fire-and-forget abort of whatever path scan is currently in flight. Mirrors
        DOVER_UI's Utilities/utilities.py cancel_scan (sends cCANCEL_PATH_MOVES_MSG to both
        CTL_TARGET - stop the stage - and RECONSTRUCTION_TARGET - stop processing in-flight
        images), and - unlike DOVER_UI's PathWindow, which just waits for the controller's own
        cPATH_UPDATE_MSG/error reply to clear its in_motion flag - immediately unblocks this
        bridge's own pending run_path_scan/run_plane_path_scan call by pushing a synthetic
        cancelled result onto _path_reply_queue (the same queue _send_path_msg's blocking
        .get() is waiting on, and the same queue _drain() already treats a stale leftover
        reply on, e.g. after a timeout), rather than depending on the controller eventually
        sending a reply that actually gets routed somewhere _send_path_msg can see it.

        Safe to call even if nothing is running - the synthetic reply just sits in the queue
        for the next call's _drain() to discard, same as an unconsumed timeout leftover today.
        """
        for target in (ic.CTL_TARGET, ic.RECONSTRUCTION_TARGET):
            TIsMsg.create_cmd_msg(ic.cCANCEL_PATH_MOVES_MSG, target).send_q_destroy()
        self._path_reply_queue.put(("cancelled", {src.cERROR: "cancelled", src.cERROR_STR: "Scan cancelled."}))

    def shutdown(self):
        self._running = False
