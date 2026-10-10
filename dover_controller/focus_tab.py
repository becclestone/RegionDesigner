"""Focus tab - ports DOVER_UI's "Focus" tab (DOVER_UI/Windows/main_window_layouts.py:
425-566, main_window.py:140-200,942-998). Jogs Z while optionally streaming live
position-adjustment commands, either addressed by master-grid section or by absolute
X/Y, at a selectable step size and motion type.
"""
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

from Constants import implementation_constants as ic
import IsMsgPy.shared_rmq_constants as src
from controller_bridge import ControllerBridge
from Utilities.time_utilities import execution_time_stamp

# label -> step size (mm) - main_window_layouts.py:477-498. DOVER_UI uses 12 radio
# buttons for this; a combo box is used here instead (same values/default, less
# visual clutter) - a presentational simplification only, see dover_controller/
# manual_moves_tab.py's module docstring for the same QDoubleSpinBox-for-QSlider
# rationale applied to widget choice generally.
_STEP_SIZES_MM = [0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.1]
_DEFAULT_STEP_MM = 0.002

_MOTION_FOCUS, _MOTION_SCAN, _MOTION_STATIONARY = 0, 1, 2


class FocusAdjustmentTab(QWidget):
    def __init__(self, bridge: ControllerBridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._moving = False

        layout = QVBoxLayout(self)

        motion_method_row = QHBoxLayout()
        self.section_motion_rb = QRadioButton("Section motion")
        self.section_motion_rb.setChecked(True)
        self.absolute_position_rb = QRadioButton("Absolute position")
        motion_method_row.addWidget(self.section_motion_rb)
        motion_method_row.addWidget(self.absolute_position_rb)
        motion_method_row.addStretch(1)
        layout.addLayout(motion_method_row)

        section_group = QGroupBox(" Section ")
        section_layout = QVBoxLayout(section_group)
        row_row = QHBoxLayout()
        row_row.addWidget(QLabel("Row (0-20):"))
        self.row_spin = QSpinBox()
        self.row_spin.setRange(0, 20)
        row_row.addWidget(self.row_spin, 1)
        section_layout.addLayout(row_row)
        col_row = QHBoxLayout()
        col_row.addWidget(QLabel("Column (0-55):"))
        self.col_spin = QSpinBox()
        self.col_spin.setRange(0, 55)
        col_row.addWidget(self.col_spin, 1)
        section_layout.addLayout(col_row)
        zv_row = QHBoxLayout()
        zv_row.addWidget(QLabel("Z velocity (5-100):"))
        self.z_velocity_spin = QDoubleSpinBox()
        self.z_velocity_spin.setRange(5.0, 100.0)
        self.z_velocity_spin.setValue(100.0)
        zv_row.addWidget(self.z_velocity_spin, 1)
        section_layout.addLayout(zv_row)
        layout.addWidget(section_group)

        absolute_group = QGroupBox(" Absolute position ")
        absolute_layout = QVBoxLayout(absolute_group)
        x_row = QHBoxLayout()
        x_row.addWidget(QLabel("X (-25.0 to 25.0):"))
        self.x_spin = QDoubleSpinBox()
        self.x_spin.setRange(-25.0, 25.0)
        self.x_spin.setDecimals(3)
        self.x_spin.setValue(10.607)
        x_row.addWidget(self.x_spin, 1)
        absolute_layout.addLayout(x_row)
        y_row = QHBoxLayout()
        y_row.addWidget(QLabel("Y (-137.0 to 137.0):"))
        self.y_spin = QDoubleSpinBox()
        self.y_spin.setRange(-137.0, 137.0)
        self.y_spin.setDecimals(3)
        self.y_spin.setValue(28.994)
        y_row.addWidget(self.y_spin, 1)
        absolute_layout.addLayout(y_row)
        layout.addWidget(absolute_group)

        self.section_motion_rb.toggled.connect(self._on_position_method_toggled)
        self._on_position_method_toggled(True)

        focus_row = QHBoxLayout()
        focus_row.addWidget(QLabel("Focus step:"))
        self.step_combo = QComboBox()
        for mm in _STEP_SIZES_MM:
            self.step_combo.addItem(f"{mm} mm", mm)
        self.step_combo.setCurrentIndex(_STEP_SIZES_MM.index(_DEFAULT_STEP_MM))
        self.step_combo.currentIndexChanged.connect(self._on_step_changed)
        focus_row.addWidget(self.step_combo)
        layout.addLayout(focus_row)

        z_group = QGroupBox(" Focus Position ")
        z_layout = QVBoxLayout(z_group)
        self.z_spin = QDoubleSpinBox()
        self.z_spin.setRange(-2.5, 2.5)
        self.z_spin.setDecimals(4)
        self.z_spin.setSingleStep(_DEFAULT_STEP_MM)
        self.z_spin.setValue(-2.276)
        z_layout.addWidget(self.z_spin)
        zero_row = QHBoxLayout()
        self.zero_btn = QPushButton("Zero")
        self.zero_btn.clicked.connect(self._on_zero_clicked)
        zero_row.addWidget(self.zero_btn)
        self.force_z_btn = QPushButton("Send Update")
        self.force_z_btn.setEnabled(False)
        self.force_z_btn.clicked.connect(self._on_send_update_clicked)
        zero_row.addWidget(self.force_z_btn)
        z_layout.addLayout(zero_row)
        layout.addWidget(z_group)

        motion_group = QGroupBox(" Motion Selector ")
        motion_layout = QVBoxLayout(motion_group)
        self._motion_buttons = QButtonGroup(self)
        self.focus_motion_rb = QRadioButton("Focus Motion")
        self.focus_motion_rb.setChecked(True)
        self.scan_motion_rb = QRadioButton("Scan Motion")
        self.stationary_rb = QRadioButton("Stationary")
        for rb in (self.focus_motion_rb, self.scan_motion_rb, self.stationary_rb):
            self._motion_buttons.addButton(rb)
            motion_layout.addWidget(rb)
        layout.addWidget(motion_group)

        activation_row = QHBoxLayout()
        self.live_adjustments_cb = QCheckBox("Live adjustments")
        self.live_adjustments_cb.setChecked(True)
        self.live_adjustments_cb.toggled.connect(self._on_live_adjustments_toggled)
        activation_row.addWidget(self.live_adjustments_cb)
        self.execute_btn = QPushButton("START X Axis Focus Moves")
        self.execute_btn.clicked.connect(self._on_execute_clicked)
        activation_row.addWidget(self.execute_btn)
        activation_row.addStretch(1)
        layout.addLayout(activation_row)

        # Any slider/radio change streams a live position-adjustment command while
        # moving and "Live adjustments" is checked (main_window.py:946-958).
        for widget_signal in (
            self.row_spin.valueChanged, self.col_spin.valueChanged, self.z_velocity_spin.valueChanged,
            self.x_spin.valueChanged, self.y_spin.valueChanged, self.z_spin.valueChanged,
            self.focus_motion_rb.toggled, self.scan_motion_rb.toggled, self.stationary_rb.toggled,
        ):
            widget_signal.connect(self._maybe_stream_live_adjustment)

        layout.addWidget(QLabel("Focus calibration log:"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        clear_log_btn = QPushButton("Clear log")
        clear_log_btn.clicked.connect(self.log.clear)
        layout.addWidget(clear_log_btn)

        bridge.replyReceived.connect(self._on_reply)
        bridge.broadcastReceived.connect(self._on_broadcast)

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(execution_time_stamp() + text)

    def _on_position_method_toggled(self, section_checked: bool) -> None:
        for w in (self.row_spin, self.col_spin, self.z_velocity_spin):
            w.setEnabled(section_checked)
        for w in (self.x_spin, self.y_spin):
            w.setEnabled(not section_checked)

    def _on_step_changed(self, index: int) -> None:
        self.z_spin.setSingleStep(_STEP_SIZES_MM[index])

    def _current_motion(self) -> int:
        if self.scan_motion_rb.isChecked():
            return _MOTION_SCAN
        if self.stationary_rb.isChecked():
            return _MOTION_STATIONARY
        return _MOTION_FOCUS

    def _build_payload(self) -> dict:
        section = self.section_motion_rb.isChecked()
        payload = {
            ic.cTAB_SENDER: "region_designer",
            ic.cZ_POSITION: self.z_spin.value(),
            ic.cZ_SPEED_VAL: self.z_velocity_spin.value(),
            ic.cMOTION_SELECTION: self._current_motion(),
            ic.cPOSITION_SELECTION: section,
        }
        if section:
            payload[ic.cX_POS_PM] = 0
            payload[ic.cY_POS_PM] = 0
            payload[ic.cSECTION_ROW] = self.row_spin.value()
            payload[ic.cSECTION_COL] = self.col_spin.value()
        else:
            payload[ic.cX_POS_PM] = self.x_spin.value()
            payload[ic.cY_POS_PM] = self.y_spin.value()
            payload[ic.cSECTION_ROW] = -1
            payload[ic.cSECTION_COL] = -1
        return payload

    def _maybe_stream_live_adjustment(self, *_args) -> None:
        if self._moving and self.live_adjustments_cb.isChecked():
            self.bridge.send_focus_position_adjustment(self._build_payload())

    def _on_live_adjustments_toggled(self, checked: bool) -> None:
        self.force_z_btn.setEnabled(not checked)

    def _on_zero_clicked(self) -> None:
        # Mirrors DOVER_UI's ZERO_SLIDER_BTN handler exactly: while moving, the
        # adjustment is sent using the CURRENT (pre-zero) Z value - rebasing the
        # physical position reached just now as the new zero reference - and only
        # then is the displayed Z spin reset to 0.0 (cosmetic; not re-sent).
        if self._moving:
            self.bridge.send_focus_position_adjustment(self._build_payload())
        self.z_spin.blockSignals(True)
        self.z_spin.setValue(0.0)
        self.z_spin.blockSignals(False)

    def _on_send_update_clicked(self) -> None:
        if self._moving:
            self.bridge.send_focus_position_adjustment(self._build_payload())

    def _on_execute_clicked(self) -> None:
        if self._moving:
            self._moving = False
            self.execute_btn.setText("START X Axis Focus Moves")
            self.window.set_motion_locked(False)
            self.bridge.stop_focus_adjustment_motion()
        else:
            self._moving = True
            self.execute_btn.setText("STOP X Axis Focus Moves")
            self.window.set_motion_locked(True)
            self.bridge.start_focus_adjustment_motion(self._build_payload())

    def _on_reply(self, reply_type: str, payload: dict) -> None:
        if reply_type in (ic.cSTART_FOCUS_ADJUSTMENT, ic.cSTOP_FOCUS_ADJUSTMENT):
            self._append_log(payload.get(src.cERROR_STR, ""))

    def _on_broadcast(self, msg_type: str, payload: dict) -> None:
        if msg_type == ic.cUPDATE_Z_POSITION:
            self._append_log(payload.get(src.cERROR_STR, ""))
