"""Autofocus tab - ports DOVER_UI's "Autofocus" tab (DOVER_UI/Windows/
main_window_layouts.py:569-666, main_window.py:1002-1186). Runs a focus-metric sweep
around a master-grid section and shows the controller's suggested best Z.

DOVER_UI's "Use absolute XY coordinates" checkbox is intentionally NOT ported: it
routes through dover_ctl2's absolute-XY autofocus branch, which controller_bridge.py's
own run_autofocus docstring documents as having a confirmed alignment bug (skips the
FocusXPositionStart offset the row/col branch applies) - RegionDesigner's existing
autofocus pipeline (autofocus_client.py) already deliberately avoids this branch, and
this tab follows the same policy rather than exposing a known-bad mode for real
hardware.
"""
from threading import Thread

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QRadioButton, QSpinBox, QVBoxLayout, QWidget,
)

from Constants import implementation_constants as ic
from controller_bridge import ControllerBridge

# label -> step size (mm) - main_window_layouts.py:613-624
_STEP_SIZES_MM = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.05]
_DEFAULT_STEP_MM = 0.002  # actual runtime default - main_window.py:325/421 override the layout's own default
_DEFAULT_Z_START = -0.98
_DEFAULT_PLANE_COUNT = 15


class _AutofocusWorker(QObject):
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, bridge: ControllerBridge, row: int, col: int, z_start: float, z_step: float, num_layers: int):
        super().__init__()
        self.bridge, self.row, self.col = bridge, row, col
        self.z_start, self.z_step, self.num_layers = z_start, z_step, num_layers

    def start(self) -> None:
        Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            result = self.bridge.run_autofocus_full(self.row, self.col, self.z_start, self.z_step, self.num_layers)
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.finished.emit(result)


class AutofocusTab(QWidget):
    def __init__(self, bridge: ControllerBridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._worker: _AutofocusWorker | None = None
        self._suggested_z: float | None = None

        layout = QVBoxLayout(self)

        top_row = QHBoxLayout()
        section_group = QGroupBox(" Section in Master Grid ")
        section_layout = QVBoxLayout(section_group)
        row_row = QHBoxLayout()
        row_row.addWidget(QLabel("Row (0-50):"))
        self.row_spin = QSpinBox()
        self.row_spin.setRange(0, 50)
        row_row.addWidget(self.row_spin, 1)
        section_layout.addLayout(row_row)
        col_row = QHBoxLayout()
        col_row.addWidget(QLabel("Col (0-304):"))
        self.col_spin = QSpinBox()
        self.col_spin.setRange(0, 304)
        col_row.addWidget(self.col_spin, 1)
        section_layout.addLayout(col_row)
        top_row.addWidget(section_group)

        self.calculate_btn = QPushButton("Calculate focus")
        self.calculate_btn.clicked.connect(self._on_calculate_clicked)
        top_row.addWidget(self.calculate_btn)
        layout.addLayout(top_row)

        z_row = QHBoxLayout()
        z_group = QGroupBox(" Z values ")
        z_layout = QHBoxLayout(z_group)
        z_layout.addWidget(QLabel("Z start:"))
        self.z_start_spin = QDoubleSpinBox()
        self.z_start_spin.setRange(-2.5, 2.5)
        self.z_start_spin.setDecimals(4)
        self.z_start_spin.setValue(_DEFAULT_Z_START)
        self.z_start_spin.editingFinished.connect(self._on_z_start_changed)
        z_layout.addWidget(self.z_start_spin)
        z_layout.addWidget(QLabel("Z center plane:"))
        self.z_center_spin = QDoubleSpinBox()
        self.z_center_spin.setRange(-2.5, 2.5)
        self.z_center_spin.setDecimals(4)
        self.z_center_spin.setValue(_DEFAULT_Z_START + _DEFAULT_STEP_MM * (_DEFAULT_PLANE_COUNT // 2))
        self.z_center_spin.editingFinished.connect(self._on_z_center_changed)
        z_layout.addWidget(self.z_center_spin)
        z_layout.addWidget(QLabel("# focus planes (7-51):"))
        self.plane_count_spin = QSpinBox()
        self.plane_count_spin.setRange(7, 51)
        self.plane_count_spin.setSingleStep(2)
        self.plane_count_spin.setValue(_DEFAULT_PLANE_COUNT)
        self.plane_count_spin.valueChanged.connect(self._on_plane_count_changed)
        z_layout.addWidget(self.plane_count_spin)
        z_row.addWidget(z_group)

        self.set_z_btn = QPushButton("Set controller Z start")
        self.set_z_btn.setEnabled(False)
        self.set_z_btn.clicked.connect(self._on_set_z_clicked)
        z_row.addWidget(self.set_z_btn)
        layout.addLayout(z_row)

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Focus step:"))
        self.step_buttons = QButtonGroup(self)
        self._step_for_button: dict[QRadioButton, float] = {}
        for mm in _STEP_SIZES_MM:
            rb = QRadioButton(f"{mm} mm")
            if mm == _DEFAULT_STEP_MM:
                rb.setChecked(True)
            rb.toggled.connect(self._on_step_toggled)
            self.step_buttons.addButton(rb)
            self._step_for_button[rb] = mm
            step_row.addWidget(rb)
        step_row.addStretch(1)
        layout.addLayout(step_row)

        self.range_label = QLabel("")
        self.range_label.setVisible(False)
        layout.addWidget(self.range_label)

        self.figure = Figure(figsize=(5, 3))
        self.canvas = FigureCanvasQTAgg(self.figure)
        layout.addWidget(self.canvas, 1)

        self.suggested_label = QLabel("<Available after the first autofocus calculation>")
        layout.addWidget(self.suggested_label)

        self._autofocus_step = _DEFAULT_STEP_MM

    def _current_step(self) -> float:
        return self._step_for_button[self.step_buttons.checkedButton()]

    def _check_enable_set_z(self) -> None:
        # Mirrors check_if_enable_z_start_btn (main_window.py:1094-1098).
        self.set_z_btn.setEnabled(self._current_step() == 0.0005 and self.plane_count_spin.value() == 7)

    def _on_z_start_changed(self) -> None:
        self._display_range_from_start(self.z_start_spin.value())

    def _on_z_center_changed(self) -> None:
        self._display_range_from_center(self.z_center_spin.value())

    def _on_plane_count_changed(self, _value: int) -> None:
        if self._suggested_z is not None:
            self._display_range_from_center(self._suggested_z)
        self._check_enable_set_z()

    def _on_step_toggled(self, checked: bool) -> None:
        if not checked:
            return
        self._autofocus_step = self._current_step()
        if self._suggested_z is not None:
            self._display_range_from_center(self._suggested_z)
        self._check_enable_set_z()

    def _display_range_from_start(self, start_pos: float) -> None:
        auto_pts = self.plane_count_spin.value() // 2
        center_pos = round(start_pos + auto_pts * self._autofocus_step, 4)
        self.z_center_spin.setValue(center_pos)
        self._suggested_z = center_pos
        self._update_range_label(start_pos, round(start_pos + self._autofocus_step * self.plane_count_spin.value(), 4))

    def _display_range_from_center(self, suggested: float) -> None:
        self._suggested_z = suggested
        auto_pts = self.plane_count_spin.value() // 2
        start_pos = round(suggested - auto_pts * self._autofocus_step, 4)
        end_pos = round(suggested + auto_pts * self._autofocus_step, 4)
        self.z_start_spin.setValue(start_pos)
        self._update_range_label(start_pos, end_pos)

    def _update_range_label(self, start_pos: float, end_pos: float) -> None:
        self.range_label.setText(f"Focus search range: ({start_pos}) <-> ({end_pos}) @ {self._autofocus_step}")
        self.range_label.setVisible(True)

    def _on_calculate_clicked(self) -> None:
        self.window.update_status_bar("Calculating autofocus...")
        self.bridge.safemon_autofocus_start()
        self.window.set_motion_locked(True)
        self.calculate_btn.setEnabled(False)
        worker = _AutofocusWorker(
            self.bridge, self.row_spin.value(), self.col_spin.value(),
            self.z_start_spin.value(), self._autofocus_step, self.plane_count_spin.value(),
        )
        worker.finished.connect(self._on_calculate_finished)
        worker.failed.connect(self._on_calculate_failed)
        self._worker = worker
        worker.start()

    def _on_calculate_finished(self, result: dict) -> None:
        self.bridge.safemon_idle()
        self.window.set_motion_locked(False)
        self.calculate_btn.setEnabled(True)
        self._worker = None

        plot_data = result[ic.cPLOT_FOCUS_VALUES_PARAM]
        num_layers = result.get(ic.cNUMBER_OF_FOCUS_LAYERS_PARAM, self.plane_count_spin.value())
        if num_layers != self.plane_count_spin.value():
            self.plane_count_spin.blockSignals(True)
            self.plane_count_spin.setValue(num_layers)
            self.plane_count_spin.blockSignals(False)
        z_start = result.get(ic.cSTARTING_Z_FOCUS_CALCULATION_PARAM, self.z_start_spin.value())
        focus_index = result[ic.cCALCULATED_FOCUS_PARAM]
        z_pos = [z_start + i * self._autofocus_step for i in range(num_layers)]

        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.plot(z_pos, plot_data, "o-")
        if 0 <= focus_index < len(z_pos):
            ax.axvline(z_pos[focus_index], color="r", linestyle="--")
        ax.set_xlabel("Z position")
        ax.set_ylabel("Focus metric")
        self.canvas.draw()

        suggestion = z_start + focus_index * self._autofocus_step
        self.suggested_label.setText(str(suggestion))
        self._display_range_from_center(suggestion)
        self.window.update_status_bar("Autofocus calculation complete.")

    def _on_calculate_failed(self, message: str) -> None:
        self.bridge.safemon_idle()
        self.window.set_motion_locked(False)
        self.calculate_btn.setEnabled(True)
        self._worker = None
        self.z_start_spin.setValue(_DEFAULT_Z_START)
        self.window.update_status_bar(f"Autofocus calculation failed: {message}")

    def _on_set_z_clicked(self) -> None:
        # Mirrors MainWindow.set_controller_z_start (main_window.py:1100-1105) - pushes
        # the suggested Z start into the Calibration tab and (re)asserts the anchor.
        z = self.z_start_spin.value()
        self.window.calibration_tab.set_z_and_apply(z)
