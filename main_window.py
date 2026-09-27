import glob
import os

from PySide6.QtWidgets import (
    QMainWindow, QToolBar, QSpinBox, QDoubleSpinBox, QPushButton, QLabel, QMessageBox, QFileDialog, QCheckBox
)

from canvas_view import SectionCanvas
from controller_bridge import ControllerBridge
import region_clustering as clustering
from stage_calibration import StageCalibration
from autofocus_client import AutofocusSequenceWorker
from focus_review_dialog import FocusReviewDialog
from region_plane_fit import RegionPlaneFit
from region_scan import RegionScanWorker

_DEFAULT_TARGET_REGION_SIZE = 100
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
        self.bridge.sectionScanning.connect(self._on_section_scanning)
        self.bridge.sectionReconstructing.connect(self._on_section_reconstructing)

        self.calibration: StageCalibration | None = None
        self.region_focus_points: dict[int, list[tuple[float, float, float]]] = {}  # region_id -> [(row,col,z)]
        self.section_z: dict[tuple[int, int], float] = {}
        self._af_worker = None
        self._af_region_id: int | None = None
        self._af_region_items: list = []
        self._review_dialog: FocusReviewDialog | None = None
        self._scan_worker = None
        self._scanning_region_id: int | None = None

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

        self.show_grid_btn = QPushButton("Show Grid")
        self.show_grid_btn.setCheckable(True)
        self.show_grid_btn.toggled.connect(self.canvas.set_show_grid)
        toolbar.addWidget(self.show_grid_btn)

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

        self.auto_run_autofocus_btn = QPushButton("Auto Run Autofocus for Region")
        self.auto_run_autofocus_btn.setToolTip(
            "Runs autofocus for the region, then automatically confirms every point via a real scan "
            "(Auto Search, picking each point's highest-contrast capture) using the fitted peak as the starting Z."
        )
        self.auto_run_autofocus_btn.clicked.connect(self._on_auto_run_autofocus_clicked)
        toolbar.addWidget(self.auto_run_autofocus_btn)

        self.auto_finish_checkbox = QCheckBox("Auto-finish when confirmed")
        self.auto_finish_checkbox.setToolTip(
            "Checked: Auto Run also finishes the region automatically once every point is confirmed.\n"
            "Unchecked: Auto Run stops there so you can do a final manual review before clicking "
            "'Done Reviewing This Region' yourself."
        )
        toolbar.addWidget(self.auto_finish_checkbox)

        self.fit_plane_btn = QPushButton("Fit Region Plane")
        self.fit_plane_btn.clicked.connect(self._on_fit_plane_clicked)
        toolbar.addWidget(self.fit_plane_btn)

        self.scan_region_btn = QPushButton("Scan Region")
        self.scan_region_btn.setToolTip(
            "Submits every painted section in this region, with its Fit Region Plane Z, as one real "
            "multi-section scan. Run Fit Region Plane for this region first.\n"
            "Known tradeoff: this scan is open-loop (drives straight to the precomputed Z, no live "
            "refocus), unlike DOVER_UI's own Image-Path Scan button which live-autofocuses during the "
            "scan - so results can look softer than a real DOVER_UI scan if the plane fit didn't "
            "perfectly capture the tissue's tilt/drift. See region_scan.py's module docstring."
        )
        self.scan_region_btn.clicked.connect(self._on_scan_region_clicked)
        toolbar.addWidget(self.scan_region_btn)

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

    def _on_section_scanning(self, master_row: int, master_col: int, active: bool):
        self._set_section_activity(master_row, master_col, "scanning" if active else None)

    def _on_section_reconstructing(self, master_row: int, master_col: int, active: bool):
        self._set_section_activity(master_row, master_col, "reconstructing" if active else None)

    def _set_section_activity(self, master_row: int, master_col: int, activity: str | None):
        """master_row/master_col are master-grid (absolute) coordinates, as the
        controller reports them - convert back to the canvas's local (unshifted)
        section coordinates via the calibration's offset, the inverse of
        RegionScanWorker's local -> master conversion."""
        if self.calibration is None:
            return
        row = int(round(master_row - self.calibration.offset_row))
        col = int(round(master_col - self.calibration.offset_col))
        self.canvas.set_section_activity(row, col, activity)

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
        self._start_autofocus_run(auto_confirm=False, auto_finish=False)

    def _on_auto_run_autofocus_clicked(self):
        self._start_autofocus_run(auto_confirm=True, auto_finish=self.auto_finish_checkbox.isChecked())

    def _start_autofocus_run(self, auto_confirm: bool, auto_finish: bool):
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

        # focus_points_by_region() iterates canvas.focus_point_items in the same
        # order it builds `points` in, so this list lines up index-for-index with
        # the worker's pointStarted/pointFitted/pointFailed indices.
        region_items = [item for item in self.canvas.focus_point_items if item.region_id == region_id]
        for item in region_items:
            item.set_status(None)

        self._set_hardware_controls_enabled(False)
        self.status_label.setText(f"Running autofocus for region {region_id}: 0/{len(points)}")
        self.canvas.set_region_status(region_id, "focusing")
        self.canvas.set_region_progress(region_id, 0, len(points))

        # Opened now (rather than after the sequence finishes) and non-modally
        # (show(), not exec()) so the operator can watch each point's focus
        # curve appear as it's fitted, without it blocking the canvas.
        dialog = FocusReviewDialog(self.bridge, self.calibration, region_id, [], parent=self)
        dialog.set_auto_confirm(auto_confirm, auto_finish=auto_finish)
        dialog.finished.connect(lambda result, d=dialog, rid=region_id: self._on_review_dialog_finished(rid, d, result))
        dialog.show()
        self._review_dialog = dialog

        worker = AutofocusSequenceWorker(
            self.bridge, self.calibration, points,
            z_start=self.af_z_start_spin.value(),
        )
        worker.pointStarted.connect(self._on_autofocus_point_started)
        worker.pointFitted.connect(self._on_autofocus_point_fitted)
        worker.pointFailed.connect(self._on_autofocus_point_failed)
        worker.sequenceFinished.connect(lambda: self._on_autofocus_sequence_finished(region_id, worker))
        self._af_region_id = region_id
        self._af_region_items = region_items
        self._af_worker = worker  # keep alive for the duration of the sequence
        worker.start()

    def _on_autofocus_point_started(self, index: int, total: int):
        self.status_label.setText(f"Running autofocus: point {index + 1}/{total}")
        if index < len(self._af_region_items):
            self._af_region_items[index].set_status("focusing")
        self.canvas.set_region_progress(self._af_region_id, index, total)

    def _on_autofocus_point_fitted(self, index: int, fit):
        if index < len(self._af_region_items):
            self._af_region_items[index].set_status("done")
        self.canvas.set_region_progress(self._af_region_id, index + 1, len(self._af_region_items))
        if self._review_dialog is not None:
            self._review_dialog.add_fit(fit)

    def _on_autofocus_point_failed(self, index: int, message: str):
        if index < len(self._af_region_items):
            self._af_region_items[index].set_status("failed")
        QMessageBox.warning(self, "Autofocus failed", f"Point {index}: {message}")

    def _set_hardware_controls_enabled(self, enabled: bool):
        """The bridge only allows one hardware-affecting operation in flight at a
        time (see controller_bridge.py's _busy), so every button that starts one
        - autofocus, auto-run, and a real region scan - is disabled together
        while any one of them is running."""
        self.run_autofocus_btn.setEnabled(enabled)
        self.auto_run_autofocus_btn.setEnabled(enabled)
        self.scan_region_btn.setEnabled(enabled)

    def _on_autofocus_sequence_finished(self, region_id: int, worker: AutofocusSequenceWorker):
        self._set_hardware_controls_enabled(True)
        self.status_label.setText(f"Autofocus done for region {region_id}: {len(worker.fits)} point(s) fitted.")

        if self._review_dialog is not None:
            self._review_dialog.finish_collecting()

        if not worker.fits:
            self.canvas.set_region_status(region_id, "failed")

    def _on_review_dialog_finished(self, region_id: int, dialog: FocusReviewDialog, result: int):
        if not dialog.fits:
            pass  # already marked "failed" in _on_autofocus_sequence_finished; nothing to confirm
        elif result == FocusReviewDialog.DialogCode.Accepted:
            self.region_focus_points[region_id] = dialog.result_focus_points()
            self.status_label.setText(
                f"Region {region_id}: {len(self.region_focus_points[region_id])} focus point(s) confirmed."
            )
            self.canvas.set_region_status(region_id, "confirmed")
        else:
            # Operator declined the fit (or closed the dialog) - go back to
            # unmarked/pending so a retry is unambiguous.
            self.canvas.set_region_status(region_id, None)

        if self._review_dialog is dialog:
            self._review_dialog = None

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

    def _on_scan_region_clicked(self):
        if self.calibration is None:
            QMessageBox.warning(self, "Scan Region", "Load a calibration file first.")
            return
        if self.bridge.is_busy():
            QMessageBox.warning(self, "Scan Region", "Another hardware operation is already in progress.")
            return

        region_id = self.region_id_spin.value()
        region_sections = [s for s, rid in self.canvas.region_of.items() if rid == region_id]
        if not region_sections:
            QMessageBox.information(self, "Scan Region", f"No painted sections found for region {region_id}.")
            return

        missing_z = [s for s in region_sections if s not in self.section_z]
        if missing_z:
            QMessageBox.warning(
                self, "Scan Region",
                f"{len(missing_z)} of region {region_id}'s section(s) have no Z yet - "
                f"run Fit Region Plane for this region first.",
            )
            return

        ordered_sections = clustering.serpentine_order(region_sections)
        sections_with_z = [(row, col, self.section_z[(row, col)]) for row, col in ordered_sections]

        reply = QMessageBox.question(
            self, "Scan Region",
            f"Scan region {region_id} now? This submits a real {len(sections_with_z)}-section scan to the "
            f"controller - the stage will move for real.\n\n"
            f"Note: this scan is open-loop (drives straight to the Fit Region Plane Z, no live refocus), "
            f"unlike DOVER_UI's own Image-Path Scan button which live-autofocuses during the scan - so "
            f"results can look softer if the plane fit didn't perfectly capture the tissue's tilt/drift.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._set_hardware_controls_enabled(False)
        self.status_label.setText(f"Scanning region {region_id}: {len(sections_with_z)} section(s)...")
        self.canvas.set_region_status(region_id, "scanning")
        self._scanning_region_id = region_id

        worker = RegionScanWorker(self.bridge, self.calibration, sections_with_z)
        worker.scanFailed.connect(self._on_region_scan_failed)
        worker.scanFinished.connect(self._on_region_scan_finished)
        self._scan_worker = worker  # keep alive for the duration of the scan
        worker.start()

    def _on_region_scan_finished(self):
        region_id = self._scanning_region_id
        self._set_hardware_controls_enabled(True)
        self.status_label.setText(f"Region {region_id}: scan complete.")
        self.canvas.set_region_status(region_id, "scanned")
        self._scanning_region_id = None

    def _on_region_scan_failed(self, message: str):
        region_id = self._scanning_region_id
        self._set_hardware_controls_enabled(True)
        self.status_label.setText(f"Region {region_id}: scan failed.")
        self.canvas.set_region_status(region_id, "failed")
        self._scanning_region_id = None
        QMessageBox.warning(self, "Scan Region", f"Scan failed: {message}")

    def closeEvent(self, event):
        self.bridge.shutdown()
        super().closeEvent(event)
