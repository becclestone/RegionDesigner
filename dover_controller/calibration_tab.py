"""Calibration tab - ports DOVER_UI's "Calibration" tab (DOVER_UI/Windows/
main_window_layouts.py:669-768, main_window.py:203-226,1192-1235). Sets the master
grid's anchor point and path-grid offset on the controller, and saves/loads that
calibration to/from the same JSON shape stage_calibration.StageCalibration already
reads (prepare_calibration_data/load_calibration_data key shape).
"""
from threading import Thread

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget

from Constants import implementation_constants as ic
import IsMsgPy.shared_rmq_constants as src
from controller_bridge import ControllerBridge
from stage_calibration import StageCalibration
from Utilities.time_utilities import execution_time_stamp

# (min, max, default) - main_window_layouts.py:670-685, all resolution 0.0005
_ANCHOR_RANGES = {
    "x": (-25.0, 25.0, 0.5), "y": (-137.0, 137.0, 48.0),
    "z": (-2.5, 2.5, 0.012), "z_offset": (-0.01, 0.02, 0.005),
}


class _SetAnchorWorker(QObject):
    """set_anchor (controller_bridge.py) is blocking - run it off the GUI thread,
    same pattern as RegionDesignerWindow's own scan/autofocus workers."""
    finished = Signal(bool, str)

    def __init__(self, bridge: ControllerBridge, x: float, y: float, z: float, z_offset: float):
        super().__init__()
        self.bridge, self.x, self.y, self.z, self.z_offset = bridge, x, y, z, z_offset

    def start(self) -> None:
        Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self.bridge.set_anchor(self.x, self.y, self.z, self.z_offset)
        except Exception as e:
            self.finished.emit(False, str(e))
            return
        self.finished.emit(True, "")


class CalibrationTab(QWidget):
    # Emitted after a successful Save or Load (the file path) - DoverControllerWindow
    # re-emits this up to RegionDesignerWindow, which reloads its own calibration from
    # the same file, so the canvas's section_to_absolute_xy math and this tab's manual-
    # jog readouts never silently disagree. See controller_window.py's own docstring.
    calibrationLoaded = Signal(str)

    def __init__(self, bridge: ControllerBridge, window, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.window = window
        self._anchor_worker: _SetAnchorWorker | None = None
        self._anchor_spin: dict[str, QDoubleSpinBox] = {}

        layout = QVBoxLayout(self)

        anchor_group = QGroupBox(" Absolute position for master grid ANCHOR point of section (0, 0) ")
        anchor_layout = QVBoxLayout(anchor_group)
        for key, label in (("x", "X (-25.0 to 25.0)"), ("y", "Y (-137.0 to 137.0)"),
                           ("z", "Z (-2.5 to 2.5)"), ("z_offset", "Zo (-0.01 to 0.02)")):
            lo, hi, default = _ANCHOR_RANGES[key]
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{label}:"))
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setDecimals(4)
            spin.setSingleStep(0.0005)
            spin.setValue(default)
            row.addWidget(spin, 1)
            anchor_layout.addLayout(row)
            self._anchor_spin[key] = spin
        self.set_anchor_btn = QPushButton("Set location of section (0, 0)")
        self.set_anchor_btn.setToolTip(
            "Note: XY will pre-set position of bottom-left corner of section (0, 0) of the master grid."
        )
        self.set_anchor_btn.clicked.connect(self._on_set_anchor_clicked)
        anchor_layout.addWidget(self.set_anchor_btn)
        layout.addWidget(anchor_group)

        offset_group = QGroupBox(" Path section (0, 0) offset ")
        offset_layout = QVBoxLayout(offset_group)
        row_row = QHBoxLayout()
        row_row.addWidget(QLabel("Rows (0 to 30):"))
        self.offset_rows_spin = QSpinBox()
        self.offset_rows_spin.setRange(0, 30)
        row_row.addWidget(self.offset_rows_spin, 1)
        offset_layout.addLayout(row_row)
        col_row = QHBoxLayout()
        col_row.addWidget(QLabel("Columns (0 to 249):"))
        self.offset_cols_spin = QSpinBox()
        self.offset_cols_spin.setRange(0, 249)
        col_row.addWidget(self.offset_cols_spin, 1)
        offset_layout.addLayout(col_row)
        set_offset_btn = QPushButton("Set path grid offset")
        set_offset_btn.setToolTip(
            "Note: Preset offset of section (0, 0) of the path grid in relation to (0, 0) of the master grid."
        )
        set_offset_btn.clicked.connect(self._on_set_path_offset_clicked)
        offset_layout.addWidget(set_offset_btn)
        layout.addWidget(offset_group)

        save_load_row = QHBoxLayout()
        save_btn = QPushButton("Save calibration")
        save_btn.clicked.connect(self._on_save_clicked)
        save_load_row.addWidget(save_btn)
        load_btn = QPushButton("Load calibration")
        load_btn.clicked.connect(self._on_load_clicked)
        save_load_row.addWidget(load_btn)
        layout.addLayout(save_load_row)

        layout.addWidget(QLabel("Calibration log:"))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        clear_log_btn = QPushButton("Clear log")
        clear_log_btn.clicked.connect(self.log.clear)
        layout.addWidget(clear_log_btn)

        bridge.replyReceived.connect(self._on_reply)

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(execution_time_stamp() + text)

    def _current_calibration(self) -> StageCalibration:
        return StageCalibration(
            anchor_x=self._anchor_spin["x"].value(), anchor_y=self._anchor_spin["y"].value(),
            anchor_z=self._anchor_spin["z"].value(), z_offset_correction=self._anchor_spin["z_offset"].value(),
            offset_row=self.offset_rows_spin.value(), offset_col=self.offset_cols_spin.value(),
        )

    def set_z_and_apply(self, z: float) -> None:
        """Called by AutofocusTab's "Set controller Z start" button (main_window.py's
        set_controller_z_start writes into the Calibration tab's Z slider/input and
        immediately re-sends the anchor with every other field unchanged)."""
        self._anchor_spin["z"].setValue(z)
        self._on_set_anchor_clicked()

    def _on_set_anchor_clicked(self) -> None:
        self.set_anchor_btn.setEnabled(False)
        self._append_log("Setting anchor point...")
        worker = _SetAnchorWorker(
            self.bridge, self._anchor_spin["x"].value(), self._anchor_spin["y"].value(),
            self._anchor_spin["z"].value(), self._anchor_spin["z_offset"].value(),
        )
        worker.finished.connect(self._on_set_anchor_finished)
        self._anchor_worker = worker
        worker.start()

    def _on_set_anchor_finished(self, success: bool, message: str) -> None:
        self.set_anchor_btn.setEnabled(True)
        self._anchor_worker = None
        self._append_log("Anchor point set." if success else f"Anchor point rejected: {message}")

    def _on_set_path_offset_clicked(self) -> None:
        self._append_log(f"Setting path grid offset: rows={self.offset_rows_spin.value()} cols={self.offset_cols_spin.value()}")
        self.bridge.set_path_offset(self.offset_rows_spin.value(), self.offset_cols_spin.value())

    def _on_reply(self, reply_type: str, payload: dict) -> None:
        if reply_type != ic.cSET_PATH_OFFSET_MSG:
            return
        if payload.get(src.cERROR) == src.cSUCCESS:
            self._append_log("Path grid offset set.")
        else:
            self._append_log(f"Path grid offset rejected: {payload.get(src.cERROR_STR, '')}")

    def _on_save_clicked(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save calibration", "", "JSON Files (*.json)")
        if not path:
            return
        self._current_calibration().save(path)
        self._append_log(f"Saved calibration to {path}")
        self.calibrationLoaded.emit(path)

    def _on_load_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load calibration", "", "JSON Files (*.json)")
        if not path:
            return
        cal = StageCalibration.load(path)
        self._anchor_spin["x"].setValue(cal.anchor_x)
        self._anchor_spin["y"].setValue(cal.anchor_y)
        self._anchor_spin["z"].setValue(cal.anchor_z)
        self._anchor_spin["z_offset"].setValue(cal.z_offset_correction)
        self.offset_rows_spin.setValue(cal.offset_row)
        self.offset_cols_spin.setValue(cal.offset_col)
        self._append_log(f"Loaded calibration from {path}")
        self.calibrationLoaded.emit(path)
