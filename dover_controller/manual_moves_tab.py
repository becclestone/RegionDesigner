"""Manual Moves tab - ports DOVER_UI's "Manual Moves" tab (DOVER_UI/Windows/
main_window_layouts.py:38-130, main_window.py:585-720,781-790). Absolute
(programmed) per-axis moves only - DOVER_UI's jog-style "Pre-programmed Moves" tab
(pc.MOVES_TAB, start_compound_moves/start_x/y/z_moves) is commented out of its own
make_main_layout() (main_window_layouts.py:1253), so it's dead/unreachable in the
live app and intentionally not ported (see controller_bridge.py's note above
move_axis_absolute).

DOVER_UI's float sliders (arbitrary resolution, e.g. 0.001mm) have no direct Qt
equivalent (QSlider is integer-only) - QDoubleSpinBox is used instead throughout
dover_controller/, matching RegionDesigner's own existing toolbar convention
(main_window.py's QDoubleSpinBox/QSpinBox fields), with the same range/resolution/
defaults as DOVER_UI's sliders.
"""
from PySide6.QtWidgets import QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from Constants import implementation_constants as ic
import IsMsgPy.shared_rmq_constants as src
from controller_bridge import ControllerBridge
from Utilities.time_utilities import execution_time_stamp

# (min, max, default) - main_window_layouts.py:39-60
_AXIS_RANGES = {"x": (-25.0, 25.0, 0.0), "y": (-137.0, 137.0, -137.0), "z": (-2.5, 2.5, 0.0)}


class ManualMovesTab(QWidget):
    def __init__(self, bridge: ControllerBridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._pos_spin: dict[str, QDoubleSpinBox] = {}
        self._speed_spin: dict[str, QDoubleSpinBox] = {}
        self._reported_label: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
        for axis in ("x", "y", "z"):
            layout.addWidget(self._build_axis_group(axis))

        move_all_btn = QPushButton("Move All Stages")
        move_all_btn.clicked.connect(self._on_move_all_clicked)
        layout.addWidget(move_all_btn)

        layout.addWidget(QLabel("Executed moves log:"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        clear_log_btn = QPushButton("Clear log")
        clear_log_btn.clicked.connect(self.log.clear)
        layout.addWidget(clear_log_btn)

        bridge.replyReceived.connect(self._on_reply)

    def _build_axis_group(self, axis: str) -> QGroupBox:
        lo, hi, default = _AXIS_RANGES[axis]
        group = QGroupBox(f" {axis.upper()} Moves ")
        group_layout = QVBoxLayout(group)

        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel(f"{axis.upper()} Absolute Position ({lo} to {hi}):"))
        pos_spin = QDoubleSpinBox()
        pos_spin.setRange(lo, hi)
        pos_spin.setDecimals(3)
        pos_spin.setSingleStep(0.001)
        pos_spin.setValue(default)
        pos_row.addWidget(pos_spin, 1)
        group_layout.addLayout(pos_row)
        self._pos_spin[axis] = pos_spin

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel(f"{axis.upper()} Velocity (2-100):"))
        speed_spin = QDoubleSpinBox()
        speed_spin.setRange(2.0, 100.0)
        speed_spin.setDecimals(2)
        speed_spin.setSingleStep(0.01)
        speed_spin.setValue(100.0)
        speed_row.addWidget(speed_spin, 1)
        group_layout.addLayout(speed_row)
        self._speed_spin[axis] = speed_spin

        move_row = QHBoxLayout()
        move_btn = QPushButton(f"Move {axis.upper()} Stage")
        move_btn.clicked.connect(lambda _checked=False, a=axis: self._on_move_axis_clicked(a))
        move_row.addWidget(move_btn)
        move_row.addWidget(QLabel("Reported position:"))
        reported = QLabel("")
        move_row.addWidget(reported)
        move_row.addStretch(1)
        group_layout.addLayout(move_row)
        self._reported_label[axis] = reported

        return group

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(execution_time_stamp() + text)

    def _on_move_axis_clicked(self, axis: str) -> None:
        self.window.set_motion_locked(True)
        self._append_log(f"Moving {axis.upper()} stage...")
        self.bridge.move_axis_absolute(axis, self._pos_spin[axis].value(), self._speed_spin[axis].value())

    def _on_move_all_clicked(self) -> None:
        self.window.set_motion_locked(True)
        self._append_log("Moving X Y Z stages...")
        self.bridge.move_xyz_absolute(
            self._pos_spin["x"].value(), self._speed_spin["x"].value(),
            self._pos_spin["y"].value(), self._speed_spin["y"].value(),
            self._pos_spin["z"].value(), self._speed_spin["z"].value(),
        )

    def _on_reply(self, reply_type: str, payload: dict) -> None:
        # Payload shapes confirmed against DOVER_UI/MessageHandler.py's
        # handle_programmed_moves_reply/handle_position_reply/handle_reset_reply.
        if reply_type in (ic.cX_MOVE_PM_MSG, ic.cY_MOVE_PM_MSG, ic.cZ_MOVE_PM_MSG):
            axis = {ic.cX_MOVE_PM_MSG: "x", ic.cY_MOVE_PM_MSG: "y", ic.cZ_MOVE_PM_MSG: "z"}[reply_type]
            pos = payload[ic.cPOSITIONS]
            self._reported_label[axis].setText(str(pos[1]))
            self._append_log(f"{axis.upper()}: {pos[0]} -> {pos[1]}")
            self.window.set_motion_locked(False)
        elif reply_type == ic.cMOVE_PM_MSG:
            pos = payload[ic.cPOSITIONS]
            self._reported_label["x"].setText(str(pos[1]))
            self._reported_label["y"].setText(str(pos[3]))
            self._reported_label["z"].setText(str(pos[5]))
            self._append_log(f"X: {pos[0]} -> {pos[1]}  Y: {pos[2]} -> {pos[3]}  Z: {pos[4]} -> {pos[5]}")
            self.window.set_motion_locked(False)
        elif reply_type == ic.cPOSITION_UPDATE_MSG:
            self._reported_label["x"].setText(str(payload[ic.cX_POSITION_READ]))
            self._reported_label["y"].setText(str(payload[ic.cY_POSITION_READ]))
            self._reported_label["z"].setText(str(payload[ic.cZ_POSITION_READ]))
        elif reply_type == ic.cRESET_STAGE_MSG:
            self._append_log(payload.get(src.cERROR_STR, "Stage reset."))
