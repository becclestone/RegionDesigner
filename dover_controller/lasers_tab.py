"""Lasers tab - ports DOVER_UI's "Lasers" tab (DOVER_UI/Windows/main_window_layouts.py:
339-422, main_window.py:1557-1651). Two independent laser subsystems: OXXIUS (over the
same RabbitMQ bus as everything else) and the Amplitude/GOJI detection laser (direct
Modbus TCP, see Utilities/goji_controller.py) - live temperature/frequency/amplifier-
current readouts and the scheduled auto warmup/shutdown come from GojiScheduler
(goji_scheduler.py), which runs independently of whether this tab/window is open.
"""
from threading import Thread

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QCheckBox, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget

from Constants import project_constants as pc
from controller_bridge import ControllerBridge
from goji_scheduler import GojiScheduler
from Utilities import goji_controller as goji


class _GojiActionWorker(QObject):
    """goji_controller's functions are blocking Modbus calls - run them off the GUI
    thread, same reasoning as goji_scheduler.py's own background thread."""
    finished = Signal(int)

    def __init__(self, action):
        super().__init__()
        self._action = action

    def start(self) -> None:
        Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        self.finished.emit(self._action())


class LasersTab(QWidget):
    def __init__(self, bridge: ControllerBridge, goji_scheduler: GojiScheduler, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.goji_scheduler = goji_scheduler
        self.window = window
        self._goji_workers: list[_GojiActionWorker] = []

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_oxxius_group())
        if pc.USE_DETECTION_LASER:
            layout.addWidget(self._build_goji_group())
        layout.addStretch(1)

    def _build_oxxius_group(self) -> QGroupBox:
        group = QGroupBox(" OXXIUS Laser Control ")
        layout = QVBoxLayout(group)

        start_stop_row = QHBoxLayout()
        start_btn = QPushButton("Start")
        start_btn.clicked.connect(self.bridge.oxxius_laser_on)
        start_stop_row.addWidget(start_btn)
        stop_btn = QPushButton("Stop")
        stop_btn.clicked.connect(self.bridge.oxxius_laser_off)
        start_stop_row.addWidget(stop_btn)
        layout.addLayout(start_stop_row)

        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("Scan power (1-55):"))
        self.scan_power_spin = QSpinBox()
        self.scan_power_spin.setRange(1, 55)
        self.scan_power_spin.setValue(50)
        scan_row.addWidget(self.scan_power_spin, 1)
        set_scan_btn = QPushButton("Set SCAN power")
        set_scan_btn.clicked.connect(self._on_set_power_clicked)
        scan_row.addWidget(set_scan_btn)
        layout.addLayout(scan_row)

        focus_row = QHBoxLayout()
        focus_row.addWidget(QLabel("Focus power (1-50):"))
        self.focus_power_spin = QSpinBox()
        self.focus_power_spin.setRange(1, 50)
        self.focus_power_spin.setValue(40)
        focus_row.addWidget(self.focus_power_spin, 1)
        set_focus_btn = QPushButton("Set FOCUS power")
        set_focus_btn.clicked.connect(self._on_set_power_clicked)
        focus_row.addWidget(set_focus_btn)
        layout.addLayout(focus_row)

        return group

    def _on_set_power_clicked(self) -> None:
        # DOVER_UI's oxxius_set_power always reads both sliders regardless of which
        # of the two buttons was clicked (main_window.py:1567-1574) - same here.
        self.bridge.set_oxxius_power(self.scan_power_spin.value(), self.focus_power_spin.value())

    def _build_goji_group(self) -> QGroupBox:
        group = QGroupBox(" Amplitude Laser Control ")
        layout = QVBoxLayout(group)

        start_stop_row = QHBoxLayout()
        start_btn = QPushButton("Start")
        start_btn.clicked.connect(lambda: self._run_goji_action(goji.set_goji_on))
        start_stop_row.addWidget(start_btn)
        self.goji_stop_btn = QPushButton("Stop")
        self.goji_stop_btn.clicked.connect(lambda: self._run_goji_action(goji.set_goji_off))
        start_stop_row.addWidget(self.goji_stop_btn)
        layout.addLayout(start_stop_row)

        self.auto_start_cb = QCheckBox("Use automatic start for GOJI laser (weekdays @ 06:30)")
        self.auto_start_cb.setChecked(True)
        self.auto_start_cb.toggled.connect(self._on_auto_start_toggled)
        layout.addWidget(self.auto_start_cb)
        self.auto_stop_cb = QCheckBox("Stop GOJI laser at 6pm (weekdays @ 18:00)")
        self.auto_stop_cb.setChecked(True)
        self.auto_stop_cb.toggled.connect(self._on_auto_stop_toggled)
        layout.addWidget(self.auto_stop_cb)

        for label_text, attr in (("Amplifier LD Temperature:", "temp_label"),
                                  ("Pulse Picker Frequency:", "freq_label"),
                                  ("Amplifier current:", "amp_label")):
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            value_label = QLabel("??")
            setattr(self, attr, value_label)
            row.addWidget(value_label)
            row.addStretch(1)
            layout.addLayout(row)

        self.goji_scheduler.gojiInfoUpdated.connect(self._on_goji_info_updated)
        self.goji_scheduler.gojiStatusChanged.connect(self._on_goji_status_changed)
        return group

    def _on_auto_start_toggled(self, checked: bool) -> None:
        self.goji_scheduler.auto_start_enabled = checked

    def _on_auto_stop_toggled(self, checked: bool) -> None:
        self.goji_scheduler.auto_stop_enabled = checked

    def _run_goji_action(self, action) -> None:
        worker = _GojiActionWorker(action)
        worker.finished.connect(lambda status: self._on_goji_status_changed(status == 1))
        self._goji_workers.append(worker)
        worker.start()

    def _on_goji_info_updated(self, temp: str, freq: str, amp: str) -> None:
        self.temp_label.setText(f"{temp} °C" if temp else "??")
        self.freq_label.setText(f"{freq} kHz" if freq else "??")
        self.amp_label.setText(f"{amp} mA" if amp else "??")

    def _on_goji_status_changed(self, is_on: bool) -> None:
        self.goji_stop_btn.setStyleSheet("background-color: red;" if is_on else "")
