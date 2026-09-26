import glob
import os

from PySide6.QtWidgets import (
    QMainWindow, QToolBar, QSpinBox, QDoubleSpinBox, QPushButton, QLabel, QMessageBox, QFileDialog
)

from canvas_view import SectionCanvas
from controller_bridge import ControllerBridge
import region_clustering as clustering
from stage_calibration import StageCalibration
from autofocus_client import AutofocusSequenceWorker
from focus_review_dialog import FocusReviewDialog
from region_plane_fit import RegionPlaneFit

_DEFAULT_TARGET_REGION_SIZE = 75
_DEFAULT_FOCUS_POINTS_PER_REGION = 4
_DEFAULT_AF_Z_START = -0.010

# DOVER_UI is a sibling checkout that already carries the operator's calibration
# (Stage Calibration tab, saved to its saved-calibration/ folder). Auto-loading its
# most recent save here means the region designer starts out already calibrated,
# since the calibration is stage state that rarely changes and shouldn't need to be
# re-picked every session.
_DOVER_UI_CALIBRATION_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "DOVER_UI", "saved-calibration"
)


def _find_default_calibration_path() -> str | None:
    candidates = glob.glob(os.path.join(_DOVER_UI_CALIBRATION_DIR, "*.json"))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


class RegionDesignerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Region && Focus Point Designer")
        self.resize(1200, 800)

        self.canvas = SectionCanvas(self)
        self.setCentralWidget(self.canvas)

        self.bridge = ControllerBridge()
        self.bridge.imageReady.connect(self._on_image_ready)
        self.bridge.commandError.connect(self._on_command_error)

        self.calibration: StageCalibration | None = None
        self.region_focus_points: dict[int, list[tuple[float, float, float]]] = {}  # region_id -> [(row,col,z)]
        self.section_z: dict[tuple[int, int], float] = {}
        self._af_worker = None

        self._build_toolbar()
        self.status_label = QLabel("No calibration loaded.")
        self.statusBar().addWidget(self.status_label)

        self._load_default_calibration()

        self.bridge.connect_to_controller()

    def _load_default_calibration(self):
        path = _find_default_calibration_path()
        if path is None:
            return
        try:
            self.calibration = StageCalibration.load(path)
            self.status_label.setText(f"Calibration loaded (default from DOVER_UI): {os.path.basename(path)}")
        except Exception:
            pass  # keep "No calibration loaded."; the toolbar button still lets the user load one manually

    def _build_toolbar(self):
        toolbar = QToolBar("Tools", self)
        self.addToolBar(toolbar)

        toolbar.addWidget(QLabel(" Brush radius (sections): "))
        self.brush_spin = QSpinBox()
        self.brush_spin.setRange(0, 25)
        self.brush_spin.setValue(self.canvas.brush_radius)
        self.brush_spin.valueChanged.connect(self._on_brush_radius_changed)
        toolbar.addWidget(self.brush_spin)

        toolbar.addSeparator()

        snap_btn = QPushButton("Snap Image")
        snap_btn.clicked.connect(self._on_snap_clicked)
        toolbar.addWidget(snap_btn)

        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" Target sections/region: "))
        self.region_size_spin = QSpinBox()
        self.region_size_spin.setRange(1, 2000)
        self.region_size_spin.setValue(_DEFAULT_TARGET_REGION_SIZE)
        toolbar.addWidget(self.region_size_spin)

        toolbar.addWidget(QLabel(" Focus points/region: "))
        self.focus_points_spin = QSpinBox()
        self.focus_points_spin.setRange(1, 50)
        self.focus_points_spin.setValue(_DEFAULT_FOCUS_POINTS_PER_REGION)
        toolbar.addWidget(self.focus_points_spin)

        compile_btn = QPushButton("Compile Regions")
        compile_btn.clicked.connect(self._on_compile_regions_clicked)
        toolbar.addWidget(compile_btn)

        clear_regions_btn = QPushButton("Clear Regions")
        clear_regions_btn.clicked.connect(self._on_clear_regions_clicked)
        toolbar.addWidget(clear_regions_btn)

        toolbar.addSeparator()

        load_cal_btn = QPushButton("Load Calibration...")
        load_cal_btn.clicked.connect(self._on_load_calibration_clicked)
        toolbar.addWidget(load_cal_btn)

        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" Region (scan order): "))
        self.region_id_spin = QSpinBox()
        self.region_id_spin.setRange(0, 9999)
        self.region_id_spin.valueChanged.connect(self._on_region_id_changed)
        toolbar.addWidget(self.region_id_spin)

        prev_region_btn = QPushButton("< Prev")
        prev_region_btn.clicked.connect(lambda: self.region_id_spin.stepBy(-1))
        toolbar.addWidget(prev_region_btn)

        next_region_btn = QPushButton("Next >")
        next_region_btn.clicked.connect(lambda: self.region_id_spin.stepBy(1))
        toolbar.addWidget(next_region_btn)

        toolbar.addWidget(QLabel(" AF Z start (initial guess): "))
        self.af_z_start_spin = QDoubleSpinBox()
        self.af_z_start_spin.setDecimals(4)
        self.af_z_start_spin.setRange(-10.0, 10.0)
        self.af_z_start_spin.setSingleStep(0.001)
        self.af_z_start_spin.setValue(_DEFAULT_AF_Z_START)
        toolbar.addWidget(self.af_z_start_spin)

        self.run_autofocus_btn = QPushButton("Run Autofocus for Region")
        self.run_autofocus_btn.clicked.connect(self._on_run_autofocus_clicked)
        toolbar.addWidget(self.run_autofocus_btn)

        self.fit_plane_btn = QPushButton("Fit Region Plane")
        self.fit_plane_btn.clicked.connect(self._on_fit_plane_clicked)
        toolbar.addWidget(self.fit_plane_btn)

    def _on_brush_radius_changed(self, value: int):
        self.canvas.brush_radius = value

    def _on_region_id_changed(self, value: int):
        self.canvas.set_active_region(value)

    def _on_snap_clicked(self):
        self.bridge.request_snap()

    def _on_image_ready(self, image_path: str):
        self.canvas.set_background_image(image_path)

    def _on_command_error(self, message: str):
        QMessageBox.warning(self, "Controller error", message)

    def _on_compile_regions_clicked(self):
        sections = list(self.canvas.painted)
        if not sections:
            QMessageBox.information(self, "Compile Regions", "Paint an area first.")
            return

        target_size = self.region_size_spin.value()
        region_of = clustering.assign_regions(sections, target_size)

        by_region: dict[int, list[tuple[int, int]]] = {}
        for section, region_id in region_of.items():
            by_region.setdefault(region_id, []).append(section)

        self.canvas.apply_regions(region_of)
        self.canvas.clear_focus_points()

        # Tissue is usually overselected a bit, so keep focus points off the outer
        # ~1mm rim of the painted area; fall back to the full region if that leaves
        # it with nothing (e.g. a region that sits entirely on that rim).
        interior_sections = clustering.sections_away_from_edge(sections)

        num_points = self.focus_points_spin.value()
        for region_id, region_sections in by_region.items():
            candidates = [s for s in region_sections if s in interior_sections] or region_sections
            for row, col in clustering.place_focus_points(candidates, num_points):
                self.canvas.add_focus_point(region_id, row, col)

        # region_id already runs 0..N-1 in the clustering's serpentine scan order
        # (region_clustering.assign_regions) - bound the spinbox to it and default
        # to region 0 so the operator starts at the beginning of that order.
        self.region_id_spin.setMaximum(max(by_region.keys()))
        self.region_id_spin.setValue(0)
        self.canvas.set_active_region(0)

    def _on_clear_regions_clicked(self):
        self.canvas.clear_regions()
        self.region_id_spin.setMaximum(9999)

    def _on_load_calibration_clicked(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select a calibration data file", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            self.calibration = StageCalibration.load(path)
            self.status_label.setText(f"Calibration loaded: {path}")
        except Exception as e:
            QMessageBox.warning(self, "Load Calibration", f"Could not load calibration: {e}")

    def _on_run_autofocus_clicked(self):
        if self.calibration is None:
            QMessageBox.warning(self, "Run Autofocus", "Load a calibration file first.")
            return
        if self.bridge.is_busy():
            QMessageBox.warning(self, "Run Autofocus", "Another hardware operation is already in progress.")
            return

        region_id = self.region_id_spin.value()
        points = self.canvas.focus_points_by_region().get(region_id)
        if not points:
            QMessageBox.information(self, "Run Autofocus", f"No focus points found for region {region_id}.")
            return

        self.run_autofocus_btn.setEnabled(False)
        self.status_label.setText(f"Running autofocus for region {region_id}: 0/{len(points)}")

        worker = AutofocusSequenceWorker(
            self.bridge, self.calibration, points,
            z_start=self.af_z_start_spin.value(),
        )
        worker.pointStarted.connect(self._on_autofocus_point_started)
        worker.pointFailed.connect(self._on_autofocus_point_failed)
        worker.sequenceFinished.connect(lambda: self._on_autofocus_sequence_finished(region_id, worker))
        self._af_worker = worker  # keep alive for the duration of the sequence
        worker.start()

    def _on_autofocus_point_started(self, index: int, total: int):
        self.status_label.setText(f"Running autofocus: point {index + 1}/{total}")

    def _on_autofocus_point_failed(self, index: int, message: str):
        QMessageBox.warning(self, "Autofocus failed", f"Point {index}: {message}")

    def _on_autofocus_sequence_finished(self, region_id: int, worker: AutofocusSequenceWorker):
        self.run_autofocus_btn.setEnabled(True)
        self.status_label.setText(f"Autofocus done for region {region_id}: {len(worker.fits)} point(s) fitted.")

        if not worker.fits:
            return

        dialog = FocusReviewDialog(self.bridge, region_id, worker.fits, parent=self)
        if dialog.exec() == FocusReviewDialog.DialogCode.Accepted:
            self.region_focus_points[region_id] = dialog.result_focus_points()
            self.status_label.setText(
                f"Region {region_id}: {len(self.region_focus_points[region_id])} focus point(s) confirmed."
            )

    def _on_fit_plane_clicked(self):
        region_id = self.region_id_spin.value()
        focus_points = self.region_focus_points.get(region_id)
        if not focus_points:
            QMessageBox.information(
                self, "Fit Region Plane",
                f"Run autofocus and confirm region {region_id}'s focus points first.",
            )
            return

        region_sections = [s for s, rid in self.canvas.region_of.items() if rid == region_id]
        if not region_sections:
            QMessageBox.information(self, "Fit Region Plane", f"No painted sections found for region {region_id}.")
            return

        try:
            plane = RegionPlaneFit(focus_points)
        except ValueError as e:
            QMessageBox.warning(self, "Fit Region Plane", str(e))
            return

        self.section_z.update(plane.fill_sections(region_sections))
        residuals = plane.residuals()
        self.status_label.setText(
            f"Region {region_id}: plane fit over {len(region_sections)} section(s), "
            f"max focus-point residual = {max(residuals):.4f}mm."
        )

    def closeEvent(self, event):
        self.bridge.shutdown()
        super().closeEvent(event)
