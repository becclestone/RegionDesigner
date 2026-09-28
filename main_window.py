import glob
import os
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QMainWindow, QToolBar, QSpinBox, QDoubleSpinBox, QPushButton, QLabel, QMessageBox, QFileDialog, QCheckBox
)

from canvas_view import SectionCanvas
from controller_bridge import ControllerBridge
import confirmation_scan
import autofocus_log
import region_clustering as clustering
import scan_archive
import scan_finalize
from stage_calibration import StageCalibration
from autofocus_client import AutofocusSequenceWorker
from focus_review_dialog import FocusReviewDialog
from region_plane_fit import RegionPlaneFit
from region_scan import RegionScanWorker

_DEFAULT_TARGET_REGION_SIZE = 100
_DEFAULT_FOCUS_POINTS_PER_REGION = 4
_DEFAULT_AF_Z_START = -0.010
# A region scan's archive-to-aggregate-batch move waits for every section's
# reconstruction-off message (see _on_section_reconstructing); _recheck_tracker_files
# is the fallback for a message that never arrives, and re-checks the actual NR
# file on disk rather than guessing a fixed wait time - real reconstruction lag
# varies too much (single-section vs. large multi-section backlog, hardware
# load, etc.) for any fixed number to be both safe and not-premature, so the
# ground truth (the file's real presence) is what gates archiving, not a timer.
_RECON_POLL_INTERVAL_MS = 15 * 1000
# Purely informational - keeps waiting either way, never forces an archive.
_RECON_STALL_WARNING_S = 10 * 60

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
        self._af_log_path: str | None = None  # this region's staged autofocus_log.json, see autofocus_log.py
        self._review_dialog: FocusReviewDialog | None = None
        self._scan_worker = None
        self._scanning_region_id: int | None = None
        self._scan_existing_run_folders: set[str] = set()
        self._aggregate_root: str | None = None
        # One entry per region scan whose output is still being archived (or is
        # eligible to be, once its aggregate batch is known) - see
        # _start_region_scan/_on_region_scan_finished/_on_section_reconstructing/
        # _maybe_archive_tracker. More than one can be in flight at once, since
        # reconstruction for one region's last sections can still be catching up
        # (see run_path_scan's docstring) after the next region's scan has
        # already started.
        self._scan_recon_trackers: list[dict] = []
        self._active_scan_tracker: dict | None = None

        # "Auto Run All Regions": autofocus -> confirm -> fit plane -> scan,
        # then advance to the next region, repeated unattended for every region
        # in region_ids order - see _run_auto_pipeline_region/_advance_auto_pipeline.
        self._auto_pipeline_active = False
        self._auto_pipeline_region_ids: list[int] = []
        self._auto_pipeline_index: int = 0

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

        snap_focus_points_btn = QPushButton("Snap Focus Points to Grid")
        snap_focus_points_btn.setToolTip(
            "Resets every focus point (in every region) back to the exact center of "
            "whichever section it's currently nearest to, undoing any manual drag."
        )
        snap_focus_points_btn.clicked.connect(self._on_snap_focus_points_clicked)
        toolbar.addWidget(snap_focus_points_btn)

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

        # Second row: the autofocus/plane-fit/scan pipeline controls - kept on
        # their own toolbar (via addToolBarBreak) rather than crammed onto the
        # first row, which was getting too wide to fit on screen.
        self.addToolBarBreak()
        toolbar2 = QToolBar("Autofocus/Scan Pipeline", self)
        self.addToolBar(toolbar2)

        toolbar2.addWidget(QLabel(" AF Z start (initial guess): "))
        self.af_z_start_spin = QDoubleSpinBox()
        self.af_z_start_spin.setDecimals(4)
        self.af_z_start_spin.setRange(-10.0, 10.0)
        self.af_z_start_spin.setSingleStep(0.001)
        self.af_z_start_spin.setValue(_DEFAULT_AF_Z_START)
        toolbar2.addWidget(self.af_z_start_spin)

        self.run_autofocus_btn = QPushButton("Run Autofocus for Region")
        self.run_autofocus_btn.clicked.connect(self._on_run_autofocus_clicked)
        toolbar2.addWidget(self.run_autofocus_btn)

        self.auto_run_autofocus_btn = QPushButton("Auto Run Autofocus for Region")
        self.auto_run_autofocus_btn.setToolTip(
            "Runs autofocus for the region, then automatically confirms every point via a real scan "
            "(Auto Search, picking each point's highest-contrast capture) using the fitted peak as the starting Z."
        )
        self.auto_run_autofocus_btn.clicked.connect(self._on_auto_run_autofocus_clicked)
        toolbar2.addWidget(self.auto_run_autofocus_btn)

        self.auto_finish_checkbox = QCheckBox("Auto-finish when confirmed")
        self.auto_finish_checkbox.setToolTip(
            "Checked: Auto Run also finishes the region automatically once every point is confirmed.\n"
            "Unchecked: Auto Run stops there so you can do a final manual review before clicking "
            "'Done Reviewing This Region' yourself."
        )
        toolbar2.addWidget(self.auto_finish_checkbox)

        self.fit_plane_btn = QPushButton("Fit Region Plane")
        self.fit_plane_btn.clicked.connect(self._on_fit_plane_clicked)
        toolbar2.addWidget(self.fit_plane_btn)

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
        toolbar2.addWidget(self.scan_region_btn)

        toolbar2.addSeparator()

        self.auto_run_all_regions_btn = QPushButton("Auto Run All Regions")
        self.auto_run_all_regions_btn.setToolTip(
            "Fully automated: for every region (in order), runs autofocus, auto-confirms every point "
            "via Auto Search, fits the region plane, then submits a real region scan - then moves on "
            "to the next region and repeats until all regions are done. No per-region confirmation "
            "prompts once started. Use 'Stop After Current Region' to halt between regions."
        )
        self.auto_run_all_regions_btn.clicked.connect(self._on_auto_run_all_regions_clicked)
        toolbar2.addWidget(self.auto_run_all_regions_btn)

        self.stop_auto_pipeline_btn = QPushButton("Stop After Current Region")
        self.stop_auto_pipeline_btn.setEnabled(False)
        self.stop_auto_pipeline_btn.clicked.connect(self._on_stop_auto_pipeline_clicked)
        toolbar2.addWidget(self.stop_auto_pipeline_btn)

        toolbar2.addSeparator()

        self.reset_btn = QPushButton("Reset for New Scan")
        self.reset_btn.setToolTip(
            "Clears the canvas (background image, painted sections, regions, focus points), all "
            "plane-fit Z values, and forgets the current aggregate batch, so a brand new sample can be "
            "started from scratch. Nothing already written to disk is deleted or affected."
        )
        self.reset_btn.clicked.connect(self._on_reset_clicked)
        toolbar2.addWidget(self.reset_btn)

        self.open_previous_scan_btn = QPushButton("Open Previous Scan...")
        self.open_previous_scan_btn.setToolTip(
            "Picks an existing <timestamp>_aggregate folder under IMAGES_ROOT and makes it the active "
            "aggregate batch again, so further scanning/archiving or Finalize Results can resume "
            "targeting it. Does not restore the canvas's painted regions/focus points."
        )
        self.open_previous_scan_btn.clicked.connect(self._on_open_previous_scan_clicked)
        toolbar2.addWidget(self.open_previous_scan_btn)

        toolbar2.addSeparator()

        self.start_aggregate_btn = QPushButton("Start New Aggregate Batch")
        self.start_aggregate_btn.setToolTip(
            "Creates a new timestamped folder under IMAGES_ROOT and, from then on, files each "
            "region's confirmed autofocus captures and full-region scan output into it as "
            "Region_N/Focus_M and Region_N/Scan_data. Nothing is archived until this is clicked."
        )
        self.start_aggregate_btn.clicked.connect(self._on_start_aggregate_clicked)
        toolbar2.addWidget(self.start_aggregate_btn)

        self.finalize_results_btn = QPushButton("Finalize Results")
        self.finalize_results_btn.setToolTip(
            "Merges every scanned region's raw output into one Finalized/ folder under the aggregate "
            "batch, with one combined scan_record.json - so this session's several per-region scans "
            "can be handed to the post-processing pipeline as if they were one continuous scan. "
            "Regions not yet scanned/confirmed are left out; run this again after scanning more."
        )
        self.finalize_results_btn.clicked.connect(self._on_finalize_results_clicked)
        toolbar2.addWidget(self.finalize_results_btn)

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
        # Unlike scanning, recon-off leaves a persistent "reconstructed" marker
        # (a different, darker color - see canvas_view._SECTION_ACTIVITY_COLORS)
        # rather than clearing back to no overlay, so completed sections stay
        # visibly distinguishable as reconstruction works through the rest.
        self._set_section_activity(master_row, master_col, "reconstructing" if active else "reconstructed")
        if active:
            return
        section = (master_row, master_col)
        for tracker in self._scan_recon_trackers:
            if section in tracker["remaining"]:
                tracker["remaining"].discard(section)
                if tracker["ready"]:
                    self._maybe_archive_tracker(tracker)

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

    def _on_snap_focus_points_clicked(self):
        count = self.canvas.snap_focus_points_to_grid()
        self.status_label.setText(f"Snapped {count} focus point(s) to their nearest section center.")

    def _on_reset_clicked(self):
        if (
            self.bridge.is_busy() or self._af_worker is not None or self._scan_worker is not None
            or self._review_dialog is not None or self._auto_pipeline_active
        ):
            QMessageBox.warning(
                self, "Reset",
                "Finish or close out the current autofocus/scan/review operation first.",
            )
            return

        reply = QMessageBox.question(
            self, "Reset for New Scan",
            "This clears the canvas (background image, painted sections, regions, focus points), all "
            "plane-fit Z values, and forgets the current aggregate batch. Nothing already written to "
            "disk is deleted. Continue?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.canvas.reset()
        self.region_focus_points = {}
        self.section_z = {}
        self._af_worker = None
        self._af_region_id = None
        self._af_region_items = []
        self._af_log_path = None
        self._scanning_region_id = None
        self._scan_existing_run_folders = set()
        self._aggregate_root = None
        self._scan_recon_trackers = []
        self._active_scan_tracker = None
        self._auto_pipeline_region_ids = []
        self._auto_pipeline_index = 0
        self.region_id_spin.setMaximum(9999)
        self.region_id_spin.setValue(0)
        self.status_label.setText("Reset - ready for a new scan.")

    def _on_open_previous_scan_clicked(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select a previous aggregate batch folder", confirmation_scan.IMAGES_ROOT,
        )
        if not path:
            return
        self._aggregate_root = path
        self.status_label.setText(f"Aggregate batch set to: {os.path.basename(path)}")

    def _on_start_aggregate_clicked(self):
        self._aggregate_root = scan_archive.start_new_aggregate_batch()
        self.status_label.setText(f"New aggregate batch: {os.path.basename(self._aggregate_root)}")

    def _on_finalize_results_clicked(self):
        if self._aggregate_root is None:
            QMessageBox.warning(self, "Finalize Results", "Start a new aggregate batch first.")
            return
        try:
            dest_dir, num_sections = scan_finalize.finalize_aggregate(
                self._aggregate_root, self.canvas.region_of, self.section_z, self.region_focus_points,
            )
        except OSError as e:
            QMessageBox.warning(self, "Finalize Results", f"Could not finalize results: {e}")
            return
        if num_sections == 0:
            QMessageBox.information(
                self, "Finalize Results", "No scanned, confirmed regions found yet - nothing to finalize."
            )
            return
        self.status_label.setText(f"Finalized {num_sections} section(s) into {dest_dir}")

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

    def _start_autofocus_run(self, auto_confirm: bool, auto_finish: bool) -> bool:
        """Returns True once the autofocus worker has actually been started,
        False if it couldn't be (missing calibration/points, or hardware busy)
        - used by the fully-automated pipeline to detect a region it can't
        proceed with and stop cleanly rather than hang."""
        if self.calibration is None:
            QMessageBox.warning(self, "Run Autofocus", "Load a calibration file first.")
            return False
        if self.bridge.is_busy():
            QMessageBox.warning(self, "Run Autofocus", "Another hardware operation is already in progress.")
            return False

        region_id = self.region_id_spin.value()
        points = self.canvas.focus_points_by_region().get(region_id)
        if not points:
            QMessageBox.information(self, "Run Autofocus", f"No focus points found for region {region_id}.")
            return False

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
        return True

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
        self.auto_run_all_regions_btn.setEnabled(enabled)

    def _on_autofocus_sequence_finished(self, region_id: int, worker: AutofocusSequenceWorker):
        self._set_hardware_controls_enabled(True)
        self.status_label.setText(f"Autofocus done for region {region_id}: {len(worker.fits)} point(s) fitted.")

        if self._review_dialog is not None:
            self._review_dialog.finish_collecting()

        if not worker.fits:
            self.canvas.set_region_status(region_id, "failed")

        # Written now (coarse/medium/fine sweep data, no confirmed Z yet) so a
        # region's raw autofocus data survives even if the operator never
        # confirms it; _on_review_dialog_finished fills in confirmed_z/source
        # into this same file once (if) they do. See autofocus_log.py.
        try:
            self._af_log_path = autofocus_log.write_region_log(region_id, worker.records)
        except OSError as e:
            self._af_log_path = None
            self.status_label.setText(f"Autofocus done for region {region_id}, but could not write its log: {e}")

    def _on_review_dialog_finished(self, region_id: int, dialog: FocusReviewDialog, result: int):
        if not dialog.fits:
            pass  # already marked "failed" in _on_autofocus_sequence_finished; nothing to confirm
        elif result == FocusReviewDialog.DialogCode.Accepted:
            self.region_focus_points[region_id] = dialog.result_focus_points()
            self.status_label.setText(
                f"Region {region_id}: {len(self.region_focus_points[region_id])} focus point(s) confirmed."
            )
            self.canvas.set_region_status(region_id, "confirmed")
            if self._af_log_path is not None:
                autofocus_log.update_confirmed(self._af_log_path, dialog.confirmed_z_with_source())
            if self._aggregate_root is not None:
                for focus_index, captures in dialog.captures_by_index().items():
                    if captures:
                        scan_archive.archive_focus_point(self._aggregate_root, region_id, focus_index, captures)
                if self._af_log_path is not None:
                    scan_archive.archive_autofocus_log(self._aggregate_root, region_id, self._af_log_path)
        else:
            # Operator declined the fit (or closed the dialog) - go back to
            # unmarked/pending so a retry is unambiguous.
            self.canvas.set_region_status(region_id, None)

        if self._review_dialog is dialog:
            self._review_dialog = None

        if self._auto_pipeline_active:
            self._continue_auto_pipeline(region_id, confirmed=result == FocusReviewDialog.DialogCode.Accepted)

    def _on_fit_plane_clicked(self):
        self._fit_plane_for_region(self.region_id_spin.value())

    def _fit_plane_for_region(self, region_id: int) -> bool:
        """Returns True on success. Shared by the manual 'Fit Region Plane'
        button and the fully-automated pipeline."""
        focus_points = self.region_focus_points.get(region_id)
        if not focus_points:
            QMessageBox.information(
                self, "Fit Region Plane",
                f"Run autofocus and confirm region {region_id}'s focus points first.",
            )
            return False

        region_sections = [s for s, rid in self.canvas.region_of.items() if rid == region_id]
        if not region_sections:
            QMessageBox.information(self, "Fit Region Plane", f"No painted sections found for region {region_id}.")
            return False

        try:
            plane = RegionPlaneFit(focus_points)
        except ValueError as e:
            QMessageBox.warning(self, "Fit Region Plane", str(e))
            return False

        self.section_z.update(plane.fill_sections(region_sections))
        residuals = plane.residuals()
        self.status_label.setText(
            f"Region {region_id}: plane fit over {len(region_sections)} section(s), "
            f"max focus-point residual = {max(residuals):.4f}mm."
        )
        return True

    def _on_scan_region_clicked(self):
        if self.calibration is None:
            QMessageBox.warning(self, "Scan Region", "Load a calibration file first.")
            return
        if self.bridge.is_busy():
            QMessageBox.warning(self, "Scan Region", "Another hardware operation is already in progress.")
            return
        self._start_region_scan(self.region_id_spin.value(), confirm=True)

    def _start_region_scan(self, region_id: int, confirm: bool) -> bool:
        """Returns True once the scan worker has actually been started.
        confirm=False (used by the fully-automated pipeline) skips the "are you
        sure, real hardware will move" prompt, since nobody's there to click it."""
        region_sections = [s for s, rid in self.canvas.region_of.items() if rid == region_id]
        if not region_sections:
            QMessageBox.information(self, "Scan Region", f"No painted sections found for region {region_id}.")
            return False

        missing_z = [s for s in region_sections if s not in self.section_z]
        if missing_z:
            QMessageBox.warning(
                self, "Scan Region",
                f"{len(missing_z)} of region {region_id}'s section(s) have no Z yet - "
                f"run Fit Region Plane for this region first.",
            )
            return False

        ordered_sections = clustering.serpentine_order(region_sections)
        sections_with_z = [(row, col, self.section_z[(row, col)]) for row, col in ordered_sections]

        if confirm:
            reply = QMessageBox.question(
                self, "Scan Region",
                f"Scan region {region_id} now? This submits a real {len(sections_with_z)}-section scan to the "
                f"controller - the stage will move for real.\n\n"
                f"Note: this scan is open-loop (drives straight to the Fit Region Plane Z, no live refocus), "
                f"unlike DOVER_UI's own Image-Path Scan button which live-autofocuses during the scan - so "
                f"results can look softer if the plane fit didn't perfectly capture the tissue's tilt/drift.",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False

        self._set_hardware_controls_enabled(False)
        self.status_label.setText(f"Scanning region {region_id}: {len(sections_with_z)} section(s)...")
        self.canvas.set_region_status(region_id, "scanning")
        self._scanning_region_id = region_id
        # Snapshotted regardless of whether an aggregate batch is active (cheap -
        # just an os.listdir), so _on_region_scan_finished can diff it either way.
        self._scan_existing_run_folders = confirmation_scan.list_run_folders()

        # Tracked from scan start (not scan finish) - reconstruction for early
        # sections in the path can complete well before the whole path does, and
        # this must catch those too, or the tracker would wait forever for
        # reconstruction-off events that already happened. Same master-grid
        # offset RegionScanWorker itself applies internally.
        master_sections = {
            (int(round(self.calibration.offset_row + row)), int(round(self.calibration.offset_col + col)))
            for row, col, _z in sections_with_z
        }
        tracker = {"region_id": region_id, "remaining": set(master_sections), "folders": None, "ready": False}
        self._scan_recon_trackers.append(tracker)
        self._active_scan_tracker = tracker

        worker = RegionScanWorker(self.bridge, self.calibration, sections_with_z)
        worker.scanFailed.connect(self._on_region_scan_failed)
        worker.scanFinished.connect(self._on_region_scan_finished)
        self._scan_worker = worker  # keep alive for the duration of the scan
        worker.start()
        return True

    def _on_region_scan_finished(self):
        region_id = self._scanning_region_id
        self._set_hardware_controls_enabled(True)
        self.status_label.setText(f"Region {region_id}: scan complete.")
        self.canvas.set_region_status(region_id, "scanned")
        self._scanning_region_id = None

        tracker = self._active_scan_tracker
        self._active_scan_tracker = None
        if tracker is not None:
            if self._aggregate_root is not None:
                new_folder_names = confirmation_scan.list_run_folders() - self._scan_existing_run_folders
                new_run_folders = {
                    os.path.join(confirmation_scan.IMAGES_ROOT, name) for name in new_folder_names
                }
                if new_run_folders:
                    # Not moved yet - only once every section in tracker["remaining"]
                    # has reported reconstruction-off (see _on_section_reconstructing/
                    # _maybe_archive_tracker), so a section still being reconstructed
                    # when the path itself completes can't have its run folder moved
                    # out from under the writer still finishing it.
                    tracker["folders"] = new_run_folders
                    tracker["ready"] = True
                    tracker["started"] = time.monotonic()
                    tracker["warned"] = False
                    timer = QTimer(self)
                    timer.timeout.connect(lambda t=tracker: self._recheck_tracker_files(t))
                    tracker["timer"] = timer
                    timer.start(_RECON_POLL_INTERVAL_MS)
                    self._maybe_archive_tracker(tracker)
                else:
                    self._scan_recon_trackers.remove(tracker)
            else:
                self._scan_recon_trackers.remove(tracker)  # no aggregate active - nothing to archive

        if self._auto_pipeline_active:
            self._advance_auto_pipeline()

    def _maybe_archive_tracker(self, tracker: dict):
        if not tracker["ready"] or tracker["remaining"]:
            return  # scan not finished yet, or some section(s) still reconstructing
        timer = tracker.get("timer")
        if timer is not None:
            timer.stop()
        scan_archive.archive_region_scan(self._aggregate_root, tracker["region_id"], tracker["folders"])
        if tracker in self._scan_recon_trackers:
            self._scan_recon_trackers.remove(tracker)

    def _recheck_tracker_files(self, tracker: dict):
        """Periodic fallback for a region scan's archive-wait: re-checks the
        actual NR file for every section still in tracker["remaining"] directly
        on disk, in case its reconstruction-off message (see
        _on_section_reconstructing) was ever missed. The file's real presence is
        ground truth, so this resolves a stuck tracker without ever guessing a
        fixed wait time - and, critically, can never archive while a file it
        expects is actually still missing, however long that takes."""
        if tracker not in self._scan_recon_trackers or not tracker["ready"]:
            timer = tracker.get("timer")
            if timer is not None:
                timer.stop()
            return

        found = set()
        for master_row, master_col in tracker["remaining"]:
            filename = f"s-{master_row}-{master_col}_nr_float32.tif"
            if any(os.path.isfile(os.path.join(folder, "NR", filename)) for folder in tracker["folders"]):
                found.add((master_row, master_col))
        if found:
            tracker["remaining"] -= found
            for master_row, master_col in found:
                self._set_section_activity(master_row, master_col, "reconstructed")

        if not tracker["remaining"]:
            self._maybe_archive_tracker(tracker)
            return

        elapsed = time.monotonic() - tracker["started"]
        if elapsed > _RECON_STALL_WARNING_S and not tracker["warned"]:
            tracker["warned"] = True
            self.status_label.setText(
                f"Region {tracker['region_id']}: still waiting on {len(tracker['remaining'])} section(s) to "
                f"reconstruct after {elapsed / 60:.1f} min - scan data won't be archived until they're all done."
            )

    def _on_region_scan_failed(self, message: str):
        region_id = self._scanning_region_id
        self._set_hardware_controls_enabled(True)
        self.status_label.setText(f"Region {region_id}: scan failed.")
        self.canvas.set_region_status(region_id, "failed")
        self._scanning_region_id = None
        tracker = self._active_scan_tracker
        self._active_scan_tracker = None
        if tracker is not None and tracker in self._scan_recon_trackers:
            self._scan_recon_trackers.remove(tracker)  # scan failed - nothing to archive
        if self._auto_pipeline_active:
            self._end_auto_pipeline(
                f"Auto Run All Regions: stopped - region {region_id} scan failed.",
                f"Region {region_id} scan failed - stopping the automated run: {message}",
            )
        else:
            QMessageBox.warning(self, "Scan Region", f"Scan failed: {message}")

    def _on_auto_run_all_regions_clicked(self):
        if self.calibration is None:
            QMessageBox.warning(self, "Auto Run All Regions", "Load a calibration file first.")
            return
        if self.bridge.is_busy() or self._auto_pipeline_active:
            QMessageBox.warning(self, "Auto Run All Regions", "Another hardware operation is already in progress.")
            return

        region_ids = sorted(set(self.canvas.region_of.values()))
        if not region_ids:
            QMessageBox.information(self, "Auto Run All Regions", "Compile regions first.")
            return

        reply = QMessageBox.question(
            self, "Auto Run All Regions",
            f"Run autofocus, confirm, fit plane, and scan for all {len(region_ids)} region(s) automatically, "
            f"one after another, with no further per-region confirmation? Real hardware scans will run "
            f"unattended - use 'Stop After Current Region' to halt between regions.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if self._aggregate_root is None:
            self._aggregate_root = scan_archive.start_new_aggregate_batch()
            self.status_label.setText(f"New aggregate batch: {os.path.basename(self._aggregate_root)}")

        self._auto_pipeline_active = True
        self._auto_pipeline_region_ids = region_ids
        self._auto_pipeline_index = 0
        self.stop_auto_pipeline_btn.setEnabled(True)
        self._run_auto_pipeline_region()

    def _on_stop_auto_pipeline_clicked(self):
        self._auto_pipeline_active = False
        self.stop_auto_pipeline_btn.setEnabled(False)
        self.status_label.setText("Auto Run All Regions: stopping after the current region finishes.")

    def _run_auto_pipeline_region(self):
        region_id = self._auto_pipeline_region_ids[self._auto_pipeline_index]
        self.region_id_spin.setValue(region_id)
        self.status_label.setText(
            f"Auto Run All Regions ({self._auto_pipeline_index + 1}/{len(self._auto_pipeline_region_ids)}): "
            f"region {region_id} - running autofocus..."
        )
        if not self._start_autofocus_run(auto_confirm=True, auto_finish=True):
            self._end_auto_pipeline(f"Auto Run All Regions: stopped - region {region_id} autofocus could not start.")

    def _continue_auto_pipeline(self, region_id: int, confirmed: bool):
        """Called from _on_review_dialog_finished once a region's points are
        either confirmed (auto_finish=True accepted the dialog) or not - picks
        up the pipeline with fit-plane + scan, or stops it."""
        if not self._auto_pipeline_active:
            return  # stopped by the user while this region's autofocus/review was running
        if not confirmed:
            self._end_auto_pipeline(
                f"Auto Run All Regions: stopped - region {region_id} was not confirmed.",
                f"Region {region_id}'s focus points were not confirmed (autofocus failed, or every "
                f"capture in the review failed) - stopping the automated run.",
            )
            return
        if not self._fit_plane_for_region(region_id):
            self._end_auto_pipeline(f"Auto Run All Regions: stopped - region {region_id} plane fit failed.")
            return
        if not self._start_region_scan(region_id, confirm=False):
            self._end_auto_pipeline(f"Auto Run All Regions: stopped - region {region_id} scan could not start.")

    def _advance_auto_pipeline(self):
        if not self._auto_pipeline_active:
            return  # stopped by the user while this region's scan was running
        self._auto_pipeline_index += 1
        if self._auto_pipeline_index >= len(self._auto_pipeline_region_ids):
            self._end_auto_pipeline(
                f"Auto Run All Regions: all {len(self._auto_pipeline_region_ids)} region(s) finished."
            )
            return
        self._run_auto_pipeline_region()

    def _end_auto_pipeline(self, status_message: str, warning_message: str | None = None):
        self._auto_pipeline_active = False
        self.stop_auto_pipeline_btn.setEnabled(False)
        self.status_label.setText(status_message)
        if warning_message:
            QMessageBox.warning(self, "Auto Run All Regions", warning_message)

    def closeEvent(self, event):
        self.bridge.shutdown()
        super().closeEvent(event)
