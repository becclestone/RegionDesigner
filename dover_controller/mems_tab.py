"""MEMS tab - ports DOVER_UI's "MEMS" tab (DOVER_UI/Windows/main_window_layouts.py:
230-336, main_window.py:794-908,1704-1768). Interactive sine/DC-drive sliders for the
scanning-mirror driver, a red-laser-only test pattern, and the same "Set MEMS run
profile" send controller_bridge.py's send_mems_run_profile already uses at scan time
(now with this tab's live values instead of the fixed scan-time defaults).

DOVER_UI's "Reset MEMS params"/"Load stored MEMS profile" buttons are stub handlers
(both just `pass` in main_window.py:896-900) - no functionality exists to port, so
they're omitted here rather than added as non-functional UI.
"""
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from Constants import implementation_constants as ic
import IsMsgPy.shared_rmq_constants as src
from controller_bridge import ControllerBridge
from Utilities.time_utilities import execution_time_stamp

# Defaults - controller_bridge.py's own _MEMS_* module constants (used for the fixed
# scan-time profile); the interactive sliders here start from the same values.
_Y_VOLTAGE_DEFAULT = 0.0
_X_AMPLITUDE_DEFAULT = 0.78
_X_FREQUENCY_DEFAULT = 2265
_SAMPLING_RATE_DEFAULT = 20000
_SAMPLE_POINTS_DEFAULT = 20000
_V_DIFFERENCE_DEFAULT = 25
_CUTOFF = 2750  # not user-adjustable in DOVER_UI either (its own slider is commented out)


class MemsTab(QWidget):
    def __init__(self, bridge: ControllerBridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._test_pattern_running = False

        layout = QVBoxLayout(self)

        settings_group = QGroupBox(" Settings ")
        settings_layout = QVBoxLayout(settings_group)

        signal_row = QHBoxLayout()
        signal_row.addWidget(QLabel("X-Signal:"))
        self.signal_combo = QComboBox()
        self.signal_combo.addItems(["Sine", "DC"])
        self.signal_combo.currentIndexChanged.connect(self._on_signal_form_changed)
        signal_row.addWidget(self.signal_combo)
        signal_row.addStretch(1)
        settings_layout.addLayout(signal_row)

        self.y_voltage_spin = self._add_row(settings_layout, "Y Voltage (-1.0 to 1.0):", -1.0, 1.0, _Y_VOLTAGE_DEFAULT)
        self.y_voltage_spin.setEnabled(False)  # disabled in Sine mode, same as DOVER_UI's default
        self.x_amplitude_spin = self._add_row(settings_layout, "X Amplitude (0.0 to 1.0):", 0.0, 1.0, _X_AMPLITUDE_DEFAULT)
        self.x_frequency_spin = self._add_row(settings_layout, "X Frequency (10 to 6000):", 10, 6000, _X_FREQUENCY_DEFAULT)
        self.sample_rate_spin = self._add_row(settings_layout, "Sample Rate (200 to 100000):", 200, 100000, _SAMPLING_RATE_DEFAULT)
        self.sample_points_spin = self._add_row(settings_layout, "Sample Points Generation (200 to 100000):", 200, 100000, _SAMPLE_POINTS_DEFAULT)
        self.v_difference_spin = self._add_row(settings_layout, "V difference (1 to 128):", 1, 128, _V_DIFFERENCE_DEFAULT)
        layout.addWidget(settings_group)

        self.y_voltage_spin.valueChanged.connect(lambda v: self._on_live_param_changed("y"))
        self.x_amplitude_spin.valueChanged.connect(lambda v: self._on_live_param_changed("x"))
        self.sample_rate_spin.valueChanged.connect(self._on_sample_rate_changed)
        self.v_difference_spin.valueChanged.connect(self._on_v_difference_changed)

        test_group = QGroupBox(" MEMS testing with red laser ONLY ")
        test_layout = QHBoxLayout(test_group)
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._on_start_stop_clicked)
        test_layout.addWidget(self.start_btn)
        self.red_laser_cb = QCheckBox("Red Laser On with MEMS")
        test_layout.addWidget(self.red_laser_cb)
        reset_btn = QPushButton("Reset")
        reset_btn.clicked.connect(self._on_reset_clicked)
        test_layout.addWidget(reset_btn)
        toggle_red_laser_btn = QPushButton("Toggle Red Laser")
        toggle_red_laser_btn.clicked.connect(self.bridge.toggle_red_laser_without_mems)
        test_layout.addWidget(toggle_red_laser_btn)
        layout.addWidget(test_group)

        profile_group = QGroupBox(" MEMS testing with stages ONLY ")
        profile_layout = QHBoxLayout(profile_group)
        run_profile_btn = QPushButton("Set MEMS run profile")
        run_profile_btn.clicked.connect(self._on_set_run_profile_clicked)
        profile_layout.addWidget(run_profile_btn)
        layout.addWidget(profile_group)

        layout.addWidget(QLabel("MEMS log:"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        clear_log_btn = QPushButton("Clear log")
        clear_log_btn.clicked.connect(self.log.clear)
        layout.addWidget(clear_log_btn)

        self.status_label = QLabel("MEMS Device Module is not running.")
        layout.addWidget(self.status_label)

        bridge.replyReceived.connect(self._on_reply)

    def _add_row(self, parent_layout: QVBoxLayout, label: str, lo, hi, default):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        if isinstance(default, int):
            spin = QSpinBox()
        else:
            spin = QDoubleSpinBox()
            spin.setDecimals(2)
        spin.setRange(lo, hi)
        spin.setValue(default)
        row.addWidget(spin, 1)
        parent_layout.addLayout(row)
        return spin

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(execution_time_stamp() + text)

    def _is_sine(self) -> bool:
        return self.signal_combo.currentText() == "Sine"

    def _build_payload(self) -> dict:
        return {
            ic.cTAB_SENDER: "region_designer",
            ic.cENABLE_DIGITAL_OUT: False,
            ic.cX_SIGNAL_FORM: self._is_sine(),
            ic.cY_VOLTAGE: self.y_voltage_spin.value(),
            ic.cX_AMPLITUDE: self.x_amplitude_spin.value(),
            ic.cX_FREQUENCY: self.x_frequency_spin.value(),
            ic.cCUTOFF: _CUTOFF,
            ic.cV_DIFFERENCE: self.v_difference_spin.value(),
            ic.cSAMPLING_RATE: self.sample_rate_spin.value(),
            ic.cSAMPLE_POINTS: self.sample_points_spin.value(),
            ic.cRED_LASER_ON: self.red_laser_cb.isChecked(),
            ic.cZ_POSITION: 0.0,
        }

    def _on_signal_form_changed(self, _index: int) -> None:
        sine = self._is_sine()
        self.y_voltage_spin.setEnabled(not sine)
        self.x_frequency_spin.setEnabled(sine)
        if sine:
            self.y_voltage_spin.setValue(0.0)

    def _on_live_param_changed(self, _which: str) -> None:
        if not self._test_pattern_running:
            return
        # Mirrors main_window.py:1734-1741 - both values are always sent together
        # regardless of which slider moved (send_y_voltage/send_x_voltage both build
        # the same {cY_VOLTAGE, cX_AMPLITUDE} payload).
        self.bridge.update_mems_y_voltage(self.y_voltage_spin.value(), self.x_amplitude_spin.value())

    def _on_sample_rate_changed(self, value) -> None:
        if self._test_pattern_running:
            self.bridge.update_mems_sampling_rate(value)

    def _on_v_difference_changed(self, value) -> None:
        if self._test_pattern_running:
            self.bridge.update_mems_v_difference(value)

    def _on_start_stop_clicked(self) -> None:
        if self._test_pattern_running:
            self.bridge.stop_mems_test_pattern()
        else:
            self.bridge.start_mems_test_pattern(self._build_payload())
        self._test_pattern_running = not self._test_pattern_running

    def _on_reset_clicked(self) -> None:
        # Mirrors send_laser_reset_event (main_window.py:841-858) - resets the
        # sliders to their defaults client-side; no message is sent.
        self.y_voltage_spin.setValue(0.0)
        self.x_amplitude_spin.setValue(_X_AMPLITUDE_DEFAULT)
        self.signal_combo.setCurrentText("Sine")
        self.v_difference_spin.setValue(_V_DIFFERENCE_DEFAULT)
        self.x_frequency_spin.setValue(_X_FREQUENCY_DEFAULT)
        self.sample_rate_spin.setValue(_SAMPLING_RATE_DEFAULT)

    def _on_set_run_profile_clicked(self) -> None:
        payload = self._build_payload()
        payload[ic.cX_SIGNAL_FORM] = True  # send_mems_run_profile always forces sine - main_window.py:892
        self._append_log("Setting MEMS run profile...")
        self.bridge.send_mems_run_profile(payload)

    def _on_reply(self, reply_type: str, payload: dict) -> None:
        if reply_type == ic.cRESET_START_MEMS:
            text = payload.get(src.cERROR_STR, "")
            self.status_label.setText(text)
            self._append_log(text)
            self.start_btn.setText("Stop")
        elif reply_type == ic.cRESET_STOP_MEMS:
            text = payload.get(src.cERROR_STR, "")
            self.status_label.setText(text)
            self._append_log(text)
            self.start_btn.setText("Start")
        elif reply_type == ic.cSET_MEMS_RUN_PROFILE_MSG:
            self._append_log(payload.get(src.cERROR_STR, ""))
        elif reply_type == ic.cDISCOVER_MEMS:
            if payload.get(src.cERROR) != src.cSUCCESS:
                return
            text = "Device: {}\nFirmware: {}\nPort: {}".format(
                payload.get(ic.cDEVICE_NAME, "?"), payload.get(ic.cFIRMWARE_NAME, "?"), payload.get(ic.cPORT_NAME, "?")
            )
            self.status_label.setText(text)
            self._append_log(text)
