import glob
import os

from PySide6.QtWidgets import (
    QMainWindow, QToolBar, QSpinBox, QDoubleSpinBox, QPushButton, QLabel, QMessageBox, QFileDialog, QCheckBox
)

from canvas_view import SectionCanvas
from controller_bridge import ControllerBridge
from dover_controller.controller_window import DoverControllerWindow
from goji_scheduler import GojiScheduler
import confirmation_scan
import autofocus_log
import region_clustering as clustering
import scan_path_export
import scan_record_import
from stage_calibration import StageCalibration
from autofocus_client import AutofocusSequenceWorker
from focus_review_dialog import FocusReviewDialog
from plane_fit_review_dialog import PlaneFitReviewDialog
from plane_path_scan import PlanePathScanWorker
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
        self.canvas.focusPointShiftClicked.connect(self._on_focus_point_shift_clicked)
        self.canvas.focusPointAddRequested.connect(self._on_focus_point_add_requested)
        self.canvas.focusPointRemoveRequested.connect(self._on_focus_point_remove_requested)
        self.canvas.redoFocusPointSetRequested.connect(self._on_redo_focus_point_set_requested)
        self.canvas.redoFocusPointRemoveRequested.connect(self._on_redo_focus_point_remove_requested)

        self.bridge = ControllerBridge()
        self.bridge.imageReady.connect(self._on_image_ready)
        self.bridge.commandError.connect(self._on_command_error)
        self.bridge.sectionScanning.connect(self._on_section_scanning)
        self.bridge.sectionReconstructing.connect(self._on_section_reconstructing)
        # Must happen before _dover_window.show() below - its showEvent sends
        # request_internal_config() immediately, which needs rmq_setup's
        # destination registration (see ControllerBridge.connect_to_controller)
        # to have already run or TIsMsg.send_q_destination raises.
        self.bridge.connect_to_controller()

        self._goji_scheduler = GojiScheduler(self.bridge)
        self._dover_window = DoverControllerWindow(self.bridge, self._goji_scheduler, parent=self)
        self._dover_window.calibrationLoaded.connect(self._on_dover_calibration_loaded)
        self._dover_window.show()

        self.calibration: StageCalibration | None = None
        self.region_focus_points: dict[int, list[tuple[float, float, float]]] = {}  # region_id -> [(row,col,z)]
        self.section_z: dict[tuple[int, int], float] = {}

        # Global Focus Search: autofocus every compiled region's focus points
        # (or just the newly-added/unconfirmed ones - see
        # _on_run_autofocus_new_points_clicked), then RANSAC-fit ONE plane
        # across all of them - RANSAC's own inlier/outlier classification
        # (region_plane_fit.RegionPlaneFit.inlier_mask) flags points
        # inconsistent with the sample's overall tilt (a fold, dust, etc.).
        # Those flagged sections are then kept out of the per-region
        # focus-point placement, rather than letting a bad point silently skew
        # the fit. Requires Compile Regions to have already placed the points
        # this runs over.
        self.global_focus_points: list[tuple[float, float, float]] = []  # confirmed (row, col, z)
        self.global_outlier_sections: set[tuple[int, int]] = set()  # grid cells RANSAC flagged as outliers
        self.global_plane: RegionPlaneFit | None = None
        self._global_fit_point_snapshot: dict[tuple[int, int], float] = {}  # cell -> z as of the last fit/update
        self._global_af_worker = None
        self._global_af_items: list = []
        self._global_af_log_path: str | None = None
        # True for the whole span of a running AutofocusSequenceWorker, not just
        # while a hardware call is actually in flight (bridge.is_busy() drops
        # back to False in the gaps between the worker's individual calls) - see
        # _focus_points_locked, which needs this to safely gate
        # double-click-add/right-click-remove against a sequence that's
        # currently indexing into the canvas's point list by position.
        self._global_af_sequence_active = False
        self._global_review_dialog: FocusReviewDialog | None = None
        # Set by _on_run_global_focus_plane_fitting_clicked/
        # _on_run_autofocus_new_points_clicked, consumed by
        # _on_global_review_dialog_finished - chains straight into fitting/
        # updating the global plane once the operator confirms the search, so
        # the one-click buttons cover both Global Focus Search steps.
        self._global_auto_fit_after_search = False

        # Manual per-point overrides from shift-clicking a marker (see
        # canvas_view.SectionCanvas.focusPointShiftClicked and
        # _on_focus_point_shift_clicked) - grid cell -> True (force included) /
        # False (force excluded). Always takes priority over the automatic
        # global_outlier_sections flag above; see _is_region_point_excluded, the
        # single place that resolves both into one final in-or-out decision for
        # plane fitting.
        self.region_inclusion_override: dict[int, dict[tuple[int, int], bool]] = {}

        # The one PlaneFitReviewDialog open at a time (see
        # _open_plane_review_dialog), and whichever of its points is currently
        # selected/highlighted red on the canvas (see
        # _on_plane_review_selection_changed) - kept here so it can be
        # un-highlighted the moment selection moves on or the dialog closes.
        self._plane_review_dialog: PlaneFitReviewDialog | None = None
        self._plane_review_highlighted_item = None

        self._scan_worker = None
        self._scanning_region_id: int | str | None = None
        # "Send Scan Path to Controller" progress - cells still awaiting a
        # sectionScanning off-edge, and the path's total row count for the
        # status label's "N/M" count. None when no full-path send is in flight.
        self._full_path_scan_remaining: set[tuple[int, int]] | None = None
        self._full_path_scan_total: int = 0

        # Redo/correction (post-hoc): autofocus + contrast-confirm state for the
        # single redo focus point (see canvas.redo_focus_point_item), and the Z it
        # confirms to once accepted - mirrors region_focus_points, but for
        # exactly one point shared across every section marked for redo, not a
        # per-region plane fit. "Redo" (a string, not an int) is used as the
        # region_id key everywhere a real region scan would use an int.
        self._redo_af_worker = None
        self._redo_af_item = None  # the canvas.redo_focus_point_item this run is for
        self._redo_af_log_path: str | None = None
        self._redo_af_sequence_active = False
        self._redo_review_dialog: FocusReviewDialog | None = None
        self._redo_confirmed_z: float | None = None

        self._build_toolbar()
        self.status_label = QLabel("No calibration loaded.")
        self.statusBar().addWidget(self.status_label)

        self._load_default_calibration()

        self._goji_scheduler.start()

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

        self.snap_focus_points_on_release_check = QCheckBox("Snap on Drag")
        self.snap_focus_points_on_release_check.setToolTip(
            "While checked, releasing a dragged focus point immediately snaps it "
            "to the exact center of its nearest section."
        )
        self.snap_focus_points_on_release_check.toggled.connect(
            self.canvas.set_snap_focus_points_on_release
        )
        self.snap_focus_points_on_release_check.setChecked(True)
        toolbar.addWidget(self.snap_focus_points_on_release_check)

        toolbar.addSeparator()

        load_cal_btn = QPushButton("Load Calibration...")
        load_cal_btn.clicked.connect(self._on_load_calibration_clicked)
        toolbar.addWidget(load_cal_btn)

        dover_controller_btn = QPushButton("Dover Controller...")
        dover_controller_btn.setToolTip(
            "Opens the Dover Controller window - manual jog, section moves, calibration, "
            "focus jog, autofocus, MEMS, lasers, and system config, the same raw hardware "
            "controls DOVER_UI's own Dover Controller window provides."
        )
        dover_controller_btn.clicked.connect(self._on_dover_controller_clicked)
        toolbar.addWidget(dover_controller_btn)

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

        # Second row: the scan-planning pipeline. The whole per-region
        # autofocus/plane-fit/scan workflow (and the aggregate-batch archiving
        # it fed) was removed in favor of a single global flow: compile
        # regions, run the global focus search to filter/fit points across the
        # whole sample, add/autofocus any new points as needed, re-review, then
        # send the resulting scan path to the controller for one real
        # closed-loop scan.
        self.addToolBarBreak()
        toolbar2 = QToolBar("Scan Planning Pipeline", self)
        self.addToolBar(toolbar2)

        toolbar2.addWidget(QLabel(" AF Z start (initial guess): "))
        self.af_z_start_spin = QDoubleSpinBox()
        self.af_z_start_spin.setDecimals(4)
        self.af_z_start_spin.setRange(-10.0, 10.0)
        self.af_z_start_spin.setSingleStep(0.001)
        self.af_z_start_spin.setValue(_DEFAULT_AF_Z_START)
        toolbar2.addWidget(self.af_z_start_spin)

        self.run_global_focus_plane_fitting_btn = QPushButton("Run Global Focus Plane Fitting")
        self.run_global_focus_plane_fitting_btn.setToolTip(
            "One-click: runs autofocus over every compiled region's focus points, then - once you "
            "confirm the search in the review dialog - automatically fits (or, if a plane's already "
            "been fit, updates) the global RANSAC plane and flags outliers. Requires Compile Regions first."
        )
        self.run_global_focus_plane_fitting_btn.clicked.connect(self._on_run_global_focus_plane_fitting_clicked)
        toolbar2.addWidget(self.run_global_focus_plane_fitting_btn)

        self.run_autofocus_new_points_btn = QPushButton("Autofocus New Points")
        self.run_autofocus_new_points_btn.setToolTip(
            "Runs autofocus on only the focus points that haven't been confirmed yet - new points you "
            "added (double-click a compiled region) after a Global Focus Plane Fitting run, or a point "
            "that previously failed. Once confirmed, automatically fits (first time) or updates (if a "
            "plane already exists) the global plane - review the result with 'Review Global Plane Fit'."
        )
        self.run_autofocus_new_points_btn.clicked.connect(self._on_run_autofocus_new_points_clicked)
        toolbar2.addWidget(self.run_autofocus_new_points_btn)

        toolbar2.addSeparator()
        toolbar2.addWidget(QLabel(" Outlier threshold (mm, 0=auto): "))
        self.global_outlier_threshold_spin = QDoubleSpinBox()
        self.global_outlier_threshold_spin.setDecimals(4)
        self.global_outlier_threshold_spin.setRange(0.0, 1.0)
        self.global_outlier_threshold_spin.setSingleStep(0.001)
        self.global_outlier_threshold_spin.setValue(0.0)
        self.global_outlier_threshold_spin.setToolTip(
            "Max perpendicular distance (mm) from the fitted plane for a point to count as an inlier. "
            "0 lets RANSAC pick its own threshold automatically."
        )
        toolbar2.addWidget(self.global_outlier_threshold_spin)

        self.fit_global_plane_btn = QPushButton("Fit Global RANSAC Plane / Find Outliers")
        self.fit_global_plane_btn.setToolTip(
            "RANSAC-fits one plane across every confirmed focus point and flags (orange) whichever "
            "points don't fit it - candidates for a fold, dust, or other local defect. Confirm a focus "
            "search first. Updates every compiled section's Z from the fit."
        )
        self.fit_global_plane_btn.clicked.connect(self._on_fit_global_plane_clicked)
        toolbar2.addWidget(self.fit_global_plane_btn)

        self.update_global_plane_btn = QPushButton("Update Global Plane")
        self.update_global_plane_btn.setToolTip(
            "Re-fits the global plane the same way Fit Global RANSAC Plane does (RANSAC needs every "
            "current point to produce a valid plane, so the fit itself always uses all of them), but "
            "only pushes a new Z for sections whose focus point is new or changed since the last fit/"
            "update - so the rest of the sample's Z isn't disturbed just to account for a few added or "
            "moved points. Requires an initial Fit Global RANSAC Plane first."
        )
        self.update_global_plane_btn.clicked.connect(self._on_update_global_plane_clicked)
        toolbar2.addWidget(self.update_global_plane_btn)

        self.review_global_plane_fit_btn = QPushButton("Review Global Plane Fit")
        self.review_global_plane_fit_btn.setToolTip(
            "Lists every compiled focus point - click through them to see each one's focus curve and "
            "how far it sits from the global plane, with its marker highlighted red on the canvas so it "
            "can be found on the sample. Also opens (pre-selected to the clicked point) by shift+clicking "
            "any marker."
        )
        self.review_global_plane_fit_btn.clicked.connect(self._on_review_global_plane_fit_clicked)
        toolbar2.addWidget(self.review_global_plane_fit_btn)

        self.exclude_global_outliers_checkbox = QCheckBox("Exclude flagged outliers from region search")
        self.exclude_global_outliers_checkbox.setChecked(True)
        self.exclude_global_outliers_checkbox.setToolTip(
            "When checked, sections flagged as outliers above are skipped when Compile Regions places "
            "per-region focus points, and dropped from the plane fit if one already landed on one. "
            "Shift+click a point's marker to manually include/exclude it regardless of this setting."
        )
        self.exclude_global_outliers_checkbox.toggled.connect(lambda _checked: self._refresh_focus_point_exclusion_visuals())
        toolbar2.addWidget(self.exclude_global_outliers_checkbox)

        toolbar2.addSeparator()

        self.export_scan_path_btn = QPushButton("Export Scan Path...")
        self.export_scan_path_btn.setToolTip(
            "Writes the compiled regions and confirmed focus points out as a San_Path_Planning-"
            "format Plane Path (.pp) for Dover UI to load and run - donor_idx marks each section's "
            "focus location, same convention as San_Path_Planning/region_planner_V2.export_path. "
            "Requires Compile Regions and a Fit Global RANSAC Plane / Find Outliers pass first, so "
            "the focus plan has been sanity-checked for outliers before export."
        )
        self.export_scan_path_btn.clicked.connect(self._on_export_scan_path_clicked)
        toolbar2.addWidget(self.export_scan_path_btn)

        self.send_scan_path_btn = QPushButton("Send Scan Path to Controller")
        self.send_scan_path_btn.setToolTip(
            "Sends the same compiled-regions/focus-points plan as Export Scan Path directly to the "
            "controller over the message bus, instead of writing a .pp file for Dover UI to load - a "
            "real CLOSED-LOOP Plane Path scan (NOT a Lucas path): the stage moves for real and the "
            "controller live-autofocuses each region's donor sections during the scan itself. Requires "
            "a loaded calibration, Compile Regions, and a Fit Global RANSAC Plane / Find Outliers pass."
        )
        self.send_scan_path_btn.clicked.connect(self._on_send_scan_path_to_controller_clicked)
        toolbar2.addWidget(self.send_scan_path_btn)

        self.cancel_scan_btn = QPushButton("Cancel Scan")
        self.cancel_scan_btn.setToolTip(
            "Aborts whichever scan (Send Scan Path to Controller / Scan Redo Sections) is currently "
            "running - sends the cancel command to both the controller and reconstruction, same as "
            "DOVER_UI's Image-Path window's Cancel button, and immediately re-enables the other hardware "
            "controls rather than waiting for the controller's own reply."
        )
        self.cancel_scan_btn.setEnabled(False)
        self.cancel_scan_btn.clicked.connect(self._on_cancel_scan_clicked)
        toolbar2.addWidget(self.cancel_scan_btn)

        toolbar2.addSeparator()
        toolbar2.addWidget(QLabel(
            " Shift+click a focus point to open its plane-fit review list (selecting a point there "
            "highlights it red on the canvas) - double-click a compiled region to add a point, "
            "right-click one to remove it. "
        ))

        toolbar2.addSeparator()

        self.reset_btn = QPushButton("Reset for New Scan")
        self.reset_btn.setToolTip(
            "Clears the canvas (background image, painted sections, regions, focus points) and all "
            "plane-fit Z values, so a brand new sample can be started from scratch. Nothing already "
            "written to disk is deleted or affected."
        )
        self.reset_btn.clicked.connect(self._on_reset_clicked)
        toolbar2.addWidget(self.reset_btn)

        # Third row: Redo/Correct Scans (post-hoc) - load a previously finalized
        # scan_record.json to get the region layout/section Z back, mark specific
        # sections that need a fresh scan, focus one point for all of them (with
        # the same contrast-confirm review used above), then rescan just that
        # marked set as one Lucas path. Independent of the pipeline above - it's
        # meant to run well after a scan, possibly in a new session.
        self.addToolBarBreak()
        toolbar3 = QToolBar("Redo / Correct Scans (post-hoc)", self)
        self.addToolBar(toolbar3)

        self.load_scan_record_btn = QPushButton("Load Scan Record...")
        self.load_scan_record_btn.setToolTip(
            "Loads a previously finalized scan_record.json to repopulate the region layout and each "
            "section's scanned Z, for correcting specific sections after the fact. Doesn't restore "
            "focus points or plane fits - only the layout/Z a targeted redo needs."
        )
        self.load_scan_record_btn.clicked.connect(self._on_load_scan_record_clicked)
        toolbar3.addWidget(self.load_scan_record_btn)

        toolbar3.addSeparator()

        self.mark_redo_btn = QPushButton("Mark Sections for Redo")
        self.mark_redo_btn.setCheckable(True)
        self.mark_redo_btn.setToolTip(
            "While active, click/drag with the brush over already-loaded sections to mark (left button) "
            "or unmark (right button) them for redo - shown with a magenta overlay."
        )
        self.mark_redo_btn.toggled.connect(self.canvas.set_redo_mode)
        toolbar3.addWidget(self.mark_redo_btn)

        clear_redo_marks_btn = QPushButton("Clear Redo Marks")
        clear_redo_marks_btn.clicked.connect(self._on_clear_redo_marks_clicked)
        toolbar3.addWidget(clear_redo_marks_btn)

        toolbar3.addSeparator()
        toolbar3.addWidget(QLabel(" Then either: "))

        self.create_region_from_marks_btn = QPushButton("Create Region from Marked Sections")
        self.create_region_from_marks_btn.setToolTip(
            "For a more accurate redo than one flat Z: carves the marked sections out into a brand-new "
            "region (its own id, selected automatically below) - double-click within it to place several "
            "focus points by hand, then use the same Global Focus Search pipeline above on it, with a "
            "proper per-section plane-fit Z. Clears the marks and turns off 'Mark Sections for Redo' "
            "once created."
        )
        self.create_region_from_marks_btn.clicked.connect(self._on_create_region_from_marks_clicked)
        toolbar3.addWidget(self.create_region_from_marks_btn)

        toolbar3.addWidget(QLabel(" or: "))

        self.pick_redo_focus_btn = QPushButton("Set Redo Focus Point")
        self.pick_redo_focus_btn.setCheckable(True)
        self.pick_redo_focus_btn.setToolTip(
            "Quick path: one flat Z for every marked section. While active, click anywhere on the canvas "
            "to place (or move) the single focus point used to focus all of them. Right-click it to "
            "remove it."
        )
        self.pick_redo_focus_btn.toggled.connect(self.canvas.set_pick_redo_focus_mode)
        toolbar3.addWidget(self.pick_redo_focus_btn)

        self.run_redo_autofocus_btn = QPushButton("Run Autofocus for Redo Point")
        self.run_redo_autofocus_btn.setToolTip(
            "Runs autofocus on the redo focus point, then lets you confirm its Z via a real contrast "
            "scan - the same review dialog used to confirm a region's own focus points."
        )
        self.run_redo_autofocus_btn.clicked.connect(self._on_run_redo_autofocus_clicked)
        toolbar3.addWidget(self.run_redo_autofocus_btn)

        self.scan_redo_btn = QPushButton("Scan Redo Sections")
        self.scan_redo_btn.setToolTip(
            "Submits every section marked for redo, all at the confirmed redo focus point's Z, as one "
            "real open-loop Lucas-path scan, for this ad hoc set."
        )
        self.scan_redo_btn.clicked.connect(self._on_scan_redo_sections_clicked)
        toolbar3.addWidget(self.scan_redo_btn)

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
        if not active and self._full_path_scan_remaining is not None:
            self._full_path_scan_remaining.discard((master_row, master_col))
            done = self._full_path_scan_total - len(self._full_path_scan_remaining)
            self.status_label.setText(
                f"Sending scan path to controller: {done}/{self._full_path_scan_total} section(s) scanned..."
            )

    def _on_section_reconstructing(self, master_row: int, master_col: int, active: bool):
        # Unlike scanning, recon-off leaves a persistent "reconstructed" marker
        # (a different, darker color - see canvas_view._SECTION_ACTIVITY_COLORS)
        # rather than clearing back to no overlay, so completed sections stay
        # visibly distinguishable as reconstruction works through the rest.
        self._set_section_activity(master_row, master_col, "reconstructing" if active else "reconstructed")

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
        self.region_inclusion_override = {}  # every region's points are being replaced wholesale

        # Tissue is usually overselected a bit, so keep focus points off the outer
        # ~1mm rim of the painted area; fall back to the full region if that leaves
        # it with nothing (e.g. a region that sits entirely on that rim).
        interior_sections = clustering.sections_away_from_edge(sections)

        exclude_outliers = self.exclude_global_outliers_checkbox.isChecked() and self.global_outlier_sections

        num_points = self.focus_points_spin.value()
        for region_id in sorted(by_region.keys()):
            region_sections = by_region[region_id]
            candidates = [s for s in region_sections if s in interior_sections] or region_sections
            if exclude_outliers:
                # Keep a region's focus-point candidates off sections the Global
                # Focus Search flagged (a fold, dust, ...) - fall back to the
                # unfiltered candidates if that would leave this region with
                # nothing to pick from, rather than failing it outright.
                candidates = [s for s in candidates if s not in self.global_outlier_sections] or candidates
            for row, col in clustering.place_focus_points(candidates, num_points):
                self.canvas.add_focus_point(region_id, row, col)

        # region_id already runs 0..N-1 in the clustering's serpentine scan order
        # (region_clustering.assign_regions) - bound the spinbox to it and default
        # to region 0 so the operator starts at the beginning of that order.
        self.region_id_spin.setMaximum(max(by_region.keys()))
        self.region_id_spin.setValue(0)
        self.canvas.set_active_region(0)
        self._refresh_focus_point_exclusion_visuals()

    def _on_clear_regions_clicked(self):
        self.canvas.clear_regions()
        self.region_id_spin.setMaximum(9999)
        self.region_inclusion_override = {}

    def _on_snap_focus_points_clicked(self):
        count = self.canvas.snap_focus_points_to_grid()
        self.status_label.setText(f"Snapped {count} focus point(s) to their nearest section center.")

    def _on_reset_clicked(self):
        if (
            self.bridge.is_busy() or self._scan_worker is not None
            or self._global_review_dialog is not None
            or self._redo_af_worker is not None or self._redo_review_dialog is not None
        ):
            QMessageBox.warning(
                self, "Reset",
                "Finish or close out the current autofocus/scan/review operation first.",
            )
            return

        reply = QMessageBox.question(
            self, "Reset for New Scan",
            "This clears the canvas (background image, painted sections, regions, focus points) and all "
            "plane-fit Z values. Nothing already written to disk is deleted. Continue?",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.canvas.reset()
        self.region_focus_points = {}
        self.section_z = {}
        self.global_focus_points = []
        self.global_outlier_sections = set()
        self.global_plane = None
        self._global_fit_point_snapshot = {}
        self._global_af_worker = None
        self._global_af_items = []
        self._global_af_log_path = None
        self._global_af_sequence_active = False
        self._global_review_dialog = None
        self._global_auto_fit_after_search = False
        self.region_inclusion_override = {}
        if self._plane_review_dialog is not None:
            self._plane_review_dialog.close()
        self._plane_review_dialog = None
        self._plane_review_highlighted_item = None
        self._scanning_region_id = None
        self._full_path_scan_remaining = None
        self._full_path_scan_total = 0
        self._redo_af_worker = None
        self._redo_af_item = None
        self._redo_af_log_path = None
        self._redo_af_sequence_active = False
        if self._redo_review_dialog is not None:
            self._redo_review_dialog.close()
        self._redo_review_dialog = None
        self._redo_confirmed_z = None
        self.mark_redo_btn.setChecked(False)
        self.pick_redo_focus_btn.setChecked(False)
        self.region_id_spin.setMaximum(9999)
        self.region_id_spin.setValue(0)
        self.status_label.setText("Reset - ready for a new scan.")

    # ---- Redo/correction (post-hoc): load a finalized scan_record.json ----
    def _on_load_scan_record_clicked(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a finalized scan_record.json", confirmation_scan.IMAGES_ROOT, "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            entries = scan_record_import.load_scan_record(path)
            region_of, section_z = scan_record_import.region_layout_from_scan_record(entries)
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "Load Scan Record", f"Could not load scan record: {e}")
            return
        if not region_of:
            QMessageBox.information(self, "Load Scan Record", "No sections found in that scan record.")
            return

        self.canvas.reset()
        self.canvas.painted = set(region_of.keys())
        self.canvas.apply_regions(region_of)

        self.region_focus_points = {}
        self.section_z = section_z

        region_ids = sorted(set(region_of.values()))
        for region_id in region_ids:
            self.canvas.set_region_status(region_id, "scanned")
        self.region_id_spin.setMaximum(max(region_ids))
        self.region_id_spin.setValue(region_ids[0])
        self.canvas.set_active_region(region_ids[0])

        self.status_label.setText(
            f"Loaded scan record: {len(region_of)} section(s) across {len(region_ids)} region(s) from "
            f"{os.path.basename(path)}. Use 'Mark Sections for Redo' to select sections to correct."
        )

    def _on_export_scan_path_clicked(self):
        if not self.canvas.region_of:
            QMessageBox.warning(self, "Export Scan Path", "Compile regions first.")
            return
        if self.global_plane is None:
            QMessageBox.warning(
                self, "Export Scan Path",
                "Run Fit Global RANSAC Plane / Find Outliers first, so the focus plan has been "
                "checked for outliers before it's handed to Dover UI.",
            )
            return

        out_path, _filter = QFileDialog.getSaveFileName(
            self, "Export Scan Path", "", "Plane path (*.pp)"
        )
        if not out_path:
            return
        if not out_path.lower().endswith(".pp"):
            out_path += ".pp"

        try:
            num_sections = scan_path_export.export_scan_path(
                out_path, self.canvas.region_of, self._focus_points_excluding_flagged(),
            )
        except ValueError as e:
            QMessageBox.warning(self, "Export Scan Path", str(e))
            return
        self.status_label.setText(f"Exported {num_sections} section(s) to {out_path}")

    def _on_send_scan_path_to_controller_clicked(self):
        if self.calibration is None:
            QMessageBox.warning(self, "Send Scan Path", "Load a calibration file first.")
            return
        if self.bridge.is_busy() or self._scan_worker is not None:
            QMessageBox.warning(self, "Send Scan Path", "Another hardware operation is already in progress.")
            return
        if not self.canvas.region_of:
            QMessageBox.warning(self, "Send Scan Path", "Compile regions first.")
            return
        if self.global_plane is None:
            QMessageBox.warning(
                self, "Send Scan Path",
                "Run Fit Global RANSAC Plane / Find Outliers first, so the focus plan has been "
                "checked for outliers before it's sent to the controller.",
            )
            return

        try:
            rows = scan_path_export.build_scan_path(self.canvas.region_of, self._focus_points_excluding_flagged())
        except ValueError as e:
            QMessageBox.warning(self, "Send Scan Path", str(e))
            return

        reply = QMessageBox.question(
            self, "Send Scan Path",
            f"Send this {len(rows)}-section scan path to the controller now? This submits a real "
            f"CLOSED-LOOP Plane Path scan (same mechanism as DOVER_UI's own Scan/Load Plane Path "
            f"button, NOT a Lucas path) - the stage will move for real and the controller will "
            f"live-autofocus each region's donor sections during the scan.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        master_cells = {
            (int(round(self.calibration.offset_row + row)), int(round(self.calibration.offset_col + col)))
            for row, col, _donor_idx, _region in rows
        }
        self._full_path_scan_remaining = master_cells
        self._full_path_scan_total = len(rows)

        self._set_hardware_controls_enabled(False)
        self.cancel_scan_btn.setEnabled(True)
        self.status_label.setText(f"Sending scan path to controller: 0/{len(rows)} section(s) scanned...")

        worker = PlanePathScanWorker(self.bridge, self.calibration, rows)
        worker.scanFailed.connect(self._on_full_path_scan_failed)
        worker.scanFinished.connect(self._on_full_path_scan_finished)
        self._scan_worker = worker  # keep alive for the duration of the scan
        worker.start()

    def _on_full_path_scan_finished(self):
        self._set_hardware_controls_enabled(True)
        self.cancel_scan_btn.setEnabled(False)
        total = self._full_path_scan_total
        done = total - len(self._full_path_scan_remaining or ())
        self._full_path_scan_remaining = None
        self._scan_worker = None
        self.status_label.setText(f"Scan path complete: {done}/{total} section(s) scanned.")

    def _on_full_path_scan_failed(self, message: str):
        self._set_hardware_controls_enabled(True)
        self.cancel_scan_btn.setEnabled(False)
        self._full_path_scan_remaining = None
        self._scan_worker = None
        if message == "Scan cancelled.":
            self.status_label.setText("Scan path cancelled.")
        else:
            QMessageBox.warning(self, "Send Scan Path", f"Scan path failed: {message}")
            self.status_label.setText("Scan path failed.")

    def _on_load_calibration_clicked(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select a calibration data file", "", "JSON Files (*.json)")
        if not path:
            return
        self._load_calibration_from_path(path)

    def _load_calibration_from_path(self, path: str) -> None:
        try:
            self.calibration = StageCalibration.load(path)
            self.status_label.setText(f"Calibration loaded: {path}")
        except Exception as e:
            QMessageBox.warning(self, "Load Calibration", f"Could not load calibration: {e}")

    def _on_dover_controller_clicked(self) -> None:
        self._dover_window.show()
        self._dover_window.raise_()
        self._dover_window.activateWindow()

    def _on_dover_calibration_loaded(self, path: str) -> None:
        # The Dover Controller window's Calibration tab (Save or Load) just wrote/read
        # this same calibration file - reload it here too so this window's own
        # section_to_absolute_xy math never silently disagrees with that window's
        # manual-jog readouts. See dover_controller/controller_window.py's docstring.
        self._load_calibration_from_path(path)

    def _set_hardware_controls_enabled(self, enabled: bool):
        """The bridge only allows one hardware-affecting operation in flight at a
        time (see controller_bridge.py's _busy), so every button that starts one
        is disabled together while any one of them is running."""
        self.run_global_focus_plane_fitting_btn.setEnabled(enabled)
        self.run_autofocus_new_points_btn.setEnabled(enabled)
        self.send_scan_path_btn.setEnabled(enabled)
        self.run_redo_autofocus_btn.setEnabled(enabled)
        self.scan_redo_btn.setEnabled(enabled)

    def _on_cancel_scan_clicked(self):
        self.bridge.cancel_current_scan()

    # ---- Global Focus Search: fits one plane across every compiled region's
    # focus points (RANSAC-flagging outliers), rather than one plane per region ----
    def _on_run_global_focus_plane_fitting_clicked(self):
        """One-click chain of autofocus -> confirm -> fit/update the global
        plane, the last step firing automatically once the operator confirms
        the search (see _on_global_review_dialog_finished). Always runs over
        every compiled region's focus points - Compile Regions must be run
        first. To autofocus just the points added/failed since the last run,
        use 'Autofocus New Points' instead."""
        if self.calibration is None:
            QMessageBox.warning(self, "Global Focus Search", "Load a calibration file first.")
            return
        if self.bridge.is_busy():
            QMessageBox.warning(self, "Global Focus Search", "Another hardware operation is already in progress.")
            return
        if not self.canvas.focus_point_items:
            QMessageBox.information(self, "Global Focus Search", "Compile regions first.")
            return

        self._global_auto_fit_after_search = True
        self._run_global_focus_search(list(self.canvas.focus_point_items))

    def _on_run_autofocus_new_points_clicked(self):
        """Autofocuses only the focus points that have never been confirmed -
        points added (double-click a compiled region) after a prior Global
        Focus Plane Fitting run, or a point whose previous confirmation never
        completed. Confirming them merges their Z into region_focus_points
        (see _apply_global_search_results) without disturbing any other
        already-confirmed point, then fits (first time) or updates (if a
        plane already exists) the global plane - same chaining as the
        one-click full-search button."""
        if self.calibration is None:
            QMessageBox.warning(self, "Autofocus New Points", "Load a calibration file first.")
            return
        if self.bridge.is_busy():
            QMessageBox.warning(self, "Autofocus New Points", "Another hardware operation is already in progress.")
            return
        new_items = [item for item in self.canvas.focus_point_items if item.z is None]
        if not new_items:
            QMessageBox.information(
                self, "Autofocus New Points",
                "No new/unconfirmed focus points - add one by double-clicking in a compiled region first.",
            )
            return

        self._global_auto_fit_after_search = True
        self._run_global_focus_search(new_items)

    def _run_global_focus_search(self, items: list):
        """Runs autofocus over `items` (every compiled region's focus points),
        then opens the review dialog to confirm them - used by the one-click
        'Run Global Focus Plane Fitting' button.

        `items` is sorted by region number so the hardware collects focus
        points region by region, in ascending region order."""
        items = sorted(items, key=lambda it: it.region_id)
        points = [item.section() for item in items]
        for item in items:
            item.set_status(None)

        self._set_hardware_controls_enabled(False)
        self.status_label.setText(f"Running global focus search: 0/{len(points)}")

        # Non-modal, same as the per-region run - lets the operator watch each
        # point's curve appear live via add_fit().
        dialog = FocusReviewDialog(
            self.bridge, self.calibration, -1, [], parent=self,
            title="Review Focus Points - Global Focus Search",
            region_ids=[item.region_id for item in items],
        )
        dialog.finished.connect(lambda result, d=dialog: self._on_global_review_dialog_finished(d, result))
        dialog.show()
        self._global_review_dialog = dialog

        worker = AutofocusSequenceWorker(self.bridge, self.calibration, points, z_start=self.af_z_start_spin.value())
        worker.pointStarted.connect(self._on_global_af_point_started)
        worker.pointFitted.connect(self._on_global_af_point_fitted)
        worker.pointFailed.connect(self._on_global_af_point_failed)
        worker.sequenceFinished.connect(lambda: self._on_global_af_sequence_finished(worker))
        self._global_af_items = items
        self._global_af_worker = worker  # keep alive for the duration of the sequence
        self._global_af_sequence_active = True
        worker.start()

    def _on_global_af_point_started(self, index: int, total: int):
        self.status_label.setText(f"Global focus search: point {index + 1}/{total}")
        if index < len(self._global_af_items):
            self._global_af_items[index].set_status("focusing")

    def _on_global_af_point_fitted(self, index: int, fit):
        if index < len(self._global_af_items):
            self._global_af_items[index].set_status("done")
            self._global_af_items[index].fit = fit  # for a later shift-click review
        if self._global_review_dialog is not None:
            self._global_review_dialog.add_fit(fit)

    def _on_global_af_point_failed(self, index: int, message: str):
        if index < len(self._global_af_items):
            self._global_af_items[index].set_status("failed")
        QMessageBox.warning(self, "Global focus search failed", f"Point {index}: {message}")

    def _on_global_af_sequence_finished(self, worker: AutofocusSequenceWorker):
        self._global_af_sequence_active = False
        self._set_hardware_controls_enabled(True)
        self.status_label.setText(f"Global focus search done: {len(worker.fits)} point(s) fitted.")

        if self._global_review_dialog is not None:
            self._global_review_dialog.finish_collecting()

        try:
            self._global_af_log_path = autofocus_log.write_region_log(-1, worker.records)
        except OSError as e:
            self._global_af_log_path = None
            self.status_label.setText(f"Global focus search done, but could not write its log: {e}")

    def _on_global_review_dialog_finished(self, dialog: FocusReviewDialog, result: int):
        # Consumed here regardless of outcome, so a declined/failed confirmation
        # can't leave it set for some later, unrelated manual search to pick up.
        auto_fit = self._global_auto_fit_after_search
        self._global_auto_fit_after_search = False

        confirmed = bool(dialog.fits) and result == FocusReviewDialog.DialogCode.Accepted
        if confirmed:
            # Fresh confirmation - the prior manual include/exclude overrides no
            # longer necessarily apply to the same points (_apply_global_search_results
            # resets each affected region's region_inclusion_override).
            self._apply_global_search_results(self._global_af_items, dialog)
            self.status_label.setText(
                f"Global focus search: {len(self.global_focus_points)} point(s) confirmed"
                + ("" if auto_fit else " - run 'Fit Global RANSAC Plane / Find Outliers' next.")
            )
            if self._global_af_log_path is not None:
                autofocus_log.update_confirmed(self._global_af_log_path, dialog.confirmed_z_with_source())
        else:
            self.status_label.setText("Global focus search: not confirmed.")

        if self._global_review_dialog is dialog:
            self._global_review_dialog = None

        self._refresh_focus_point_exclusion_visuals()

        if auto_fit and confirmed:
            # First confirmation ever fits the plane from scratch; a later one
            # (e.g. after Autofocus New Points) updates it instead, so the rest
            # of the sample's already-fit Z isn't disturbed just to account for
            # a few added/retried points.
            if self.global_plane is None:
                self._on_fit_global_plane_clicked()
            else:
                self._on_update_global_plane_clicked()

    def _apply_global_search_results(self, items: list, dialog: FocusReviewDialog):
        """Common tail of a confirmed Global Focus Search run - matches each of
        dialog.fits back to the marker it came from by object identity (the
        same FocusFit set on item.fit when its sweep completed - see
        _on_global_af_point_fitted), records its confirmed Z there, and merges
        each by its grid cell into region_focus_points - a cell `items` didn't
        cover (e.g. an "Autofocus New Points" run only touching some of a
        region's points) keeps whatever it already had, rather than being
        dropped. self.global_focus_points is then rebuilt from the merged
        region_focus_points for _on_fit_global_plane_clicked's RANSAC fit to
        run over."""
        fit_id_to_item = {id(it.fit): it for it in items if it.fit is not None}
        by_region: dict[int, list[tuple[tuple[int, int], float, float, float]]] = {}
        for i, fit in enumerate(dialog.fits):
            item = fit_id_to_item.get(id(fit))
            z = dialog.confirmed_z.get(i)
            if item is None or z is None:
                continue
            item.z = z
            grid_cell = (int(round(fit.row)), int(round(fit.col)))
            by_region.setdefault(item.region_id, []).append((grid_cell, fit.row, fit.col, z))

        for region_id, entries in by_region.items():
            merged = {
                (int(round(row)), int(round(col))): (row, col, z)
                for row, col, z in self.region_focus_points.get(region_id, [])
            }
            for grid_cell, row, col, z in entries:
                merged[grid_cell] = (row, col, z)
                # A freshly (re)confirmed point's manual include/exclude
                # override no longer necessarily applies - let it be
                # reconsidered by the next fit.
                self.region_inclusion_override.get(region_id, {}).pop(grid_cell, None)
            self.region_focus_points[region_id] = list(merged.values())

        self.global_focus_points = [
            (row, col, z) for points in self.region_focus_points.values() for row, col, z in points
        ]

    @staticmethod
    def _record_confirmed_z(items: list, dialog: FocusReviewDialog):
        """Writes each confirmed point's chosen Z onto its own canvas marker
        (item.z), matching dialog.fits[i] back to the marker it came from by
        object identity (the exact same FocusFit set on item.fit when its
        autofocus sweep completed - see _on_autofocus_point_fitted/
        _on_global_af_point_fitted). Lets a later shift-click review
        (PlaneFitReviewDialog) show the actual confirmed Z, which can
        differ from the fit's own automatic z_opt (confirmed via scan or
        manual override)."""
        fit_id_to_item = {id(it.fit): it for it in items if it.fit is not None}
        for i, fit in enumerate(dialog.fits):
            item = fit_id_to_item.get(id(fit))
            if item is not None:
                item.z = dialog.confirmed_z.get(i)

    def _gather_global_focus_points(self) -> list[tuple[int, float, float, float]]:
        """Every point currently available to fit a whole-sample plane over -
        (region_id, row, col, z) - gathered FRESH each time Fit Global RANSAC
        Plane runs: every compiled region's own confirmed focus points,
        however they got confirmed (normal per-region autofocus, or via Run
        Global Focus Plane Fitting). This is what makes pressing Fit Global
        RANSAC Plane directly, after just running the regions' own autofocus
        normally, actually do something."""
        points: list[tuple[int, float, float, float]] = []
        for region_id, region_points in self.region_focus_points.items():
            for row, col, z in region_points:
                points.append((region_id, row, col, z))
        return points

    def _is_manually_force_excluded(self, region_id: int, grid_cell: tuple[int, int]) -> bool:
        """Unlike _is_region_point_excluded (which also folds in the automatic
        RANSAC outlier flag), this checks ONLY an explicit manual override -
        used to decide what's fed into a FRESH RANSAC fit itself, so a point a
        PRIOR fit flagged as an outlier still gets reconsidered each time
        rather than being permanently locked out."""
        override = self.region_inclusion_override.get(region_id, {}).get(grid_cell)
        return override is False

    def _fit_global_plane_or_warn(self):
        """Shared by Fit Global RANSAC Plane and Update Global Plane: gathers every
        confirmed point, drops manually force-excluded ones, and RANSAC-fits one
        plane over the result. Returns (plane, gathered, input_points), or None -
        having already shown the relevant warning/information dialog - if there's
        nothing fittable yet."""
        gathered = self._gather_global_focus_points()
        if len(gathered) < 3:
            QMessageBox.information(
                self, "Global Focus Search",
                "Need at least 3 confirmed focus points to fit a plane - run autofocus for some regions "
                "and/or confirm a Global Focus Search first.",
            )
            return None

        # A manually force-excluded point (from the global plane review
        # dialog) is kept out of the RANSAC input entirely, not just hidden
        # from the result - it shouldn't get to skew the fitted plane's math
        # either. Everything else - including a point a PRIOR fit flagged as
        # an outlier - stays in the input, so RANSAC gets to reconsider it
        # fresh each time; _is_region_point_excluded (used below, and for
        # canvas visuals) is what folds that flag back in afterward.
        input_points = [
            (row, col, z) for region_id, row, col, z in gathered
            if not self._is_manually_force_excluded(region_id, (int(round(row)), int(round(col))))
        ]
        if len(input_points) < 3:
            QMessageBox.warning(
                self, "Global Focus Search",
                "Fewer than 3 points remain after manual exclusions - include some back before fitting.",
            )
            return None

        threshold = self.global_outlier_threshold_spin.value()
        try:
            plane = RegionPlaneFit(input_points, residual_threshold=threshold if threshold > 0 else None)
        except ValueError as e:
            QMessageBox.warning(self, "Global Focus Search", str(e))
            return None

        return plane, gathered, input_points

    def _on_fit_global_plane_clicked(self):
        result = self._fit_global_plane_or_warn()
        if result is None:
            return
        plane, gathered, input_points = result

        self.global_focus_points = [(row, col, z) for _region_id, row, col, z in gathered]
        self.global_plane = plane
        inlier_mask = plane.inlier_mask()
        self.global_outlier_sections = {
            (int(round(row)), int(round(col)))
            for (row, col, _z), is_inlier in zip(input_points, inlier_mask)
            if not is_inlier
        }
        self._global_fit_point_snapshot = {
            (int(round(row)), int(round(col))): z for row, col, z in input_points
        }

        # Fills section_z (the actual Z Send Scan Path to Controller submits)
        # from this plane, over every currently compiled section (or every
        # painted one, if regions haven't been compiled yet), so this plane's
        # result is actually usable for scanning and not just an outlier report.
        sections_to_fill = list(self.canvas.region_of.keys()) or list(self.canvas.painted)
        if sections_to_fill:
            self.section_z.update(plane.fill_sections(sections_to_fill))

        self._refresh_focus_point_exclusion_visuals()

        residuals = plane.residuals()
        excluded_count = sum(
            1 for region_id, row, col, _z in gathered
            if self._is_region_point_excluded(region_id, (int(round(row)), int(round(col))))
        )
        self.status_label.setText(
            f"Global RANSAC plane: {len(input_points)} point(s) fit ({len(gathered)} total known), "
            f"Z updated for {len(sections_to_fill)} section(s), "
            f"{excluded_count} excluded overall, max residual = {max(residuals):.4f}mm."
        )

    def _on_update_global_plane_clicked(self):
        """Refits the global plane the same way Fit Global RANSAC Plane does (RANSAC
        needs every current point to produce a valid plane, so nothing about the fit
        itself is partial) - but only pushes a new section_z for sections whose focus
        point is new or whose confirmed Z changed since the last fit/update, leaving
        every other section's Z untouched."""
        if self.global_plane is None:
            QMessageBox.information(
                self, "Global Focus Search",
                "No global plane fit yet - run Fit Global RANSAC Plane / Find Outliers first.",
            )
            return

        result = self._fit_global_plane_or_warn()
        if result is None:
            return
        plane, gathered, input_points = result

        changed_cells = {
            (int(round(row)), int(round(col)))
            for row, col, z in input_points
            if self._global_fit_point_snapshot.get((int(round(row)), int(round(col)))) != z
        }

        self.global_focus_points = [(row, col, z) for _region_id, row, col, z in gathered]
        self.global_plane = plane
        inlier_mask = plane.inlier_mask()
        self.global_outlier_sections = {
            (int(round(row)), int(round(col)))
            for (row, col, _z), is_inlier in zip(input_points, inlier_mask)
            if not is_inlier
        }
        self._global_fit_point_snapshot = {
            (int(round(row)), int(round(col))): z for row, col, z in input_points
        }

        if changed_cells:
            self.section_z.update(plane.fill_sections(list(changed_cells)))

        self._refresh_focus_point_exclusion_visuals()

        residuals = plane.residuals()
        self.status_label.setText(
            f"Global RANSAC plane updated: {len(input_points)} point(s) fit, "
            f"Z updated for {len(changed_cells)} added/moved section(s), "
            f"max residual = {max(residuals):.4f}mm."
        )

    # ---- shared exclusion logic (Global Focus Search RANSAC flag + manual override) ----
    def _focus_points_excluding_flagged(self) -> dict[int, list[tuple[float, float, float]]]:
        """self.region_focus_points, minus any point _is_region_point_excluded currently
        considers excluded (a manual shift-click override, or - if the checkbox is on - a
        Global Focus Search RANSAC outlier flag). scan_path_export.build_scan_path/
        export_scan_path take region_focus_points as a plain, unfiltered dict, so a
        caller building a scan path to export or send to the controller must pass this
        filtered view instead, or an untrustworthy point kept out of the plane fit would
        still end up as a donor/focus location in the scan itself."""
        return {
            region_id: [
                (row, col, z) for row, col, z in points
                if not self._is_region_point_excluded(region_id, (int(round(row)), int(round(col))))
            ]
            for region_id, points in self.region_focus_points.items()
        }

    def _is_region_point_excluded(self, region_id: int, grid_cell: tuple[int, int]) -> bool:
        override = self.region_inclusion_override.get(region_id, {}).get(grid_cell)
        if override is not None:
            return not override
        return self.exclude_global_outliers_checkbox.isChecked() and grid_cell in self.global_outlier_sections

    def _refresh_focus_point_exclusion_visuals(self):
        for item in self.canvas.focus_point_items:
            item.set_excluded(self._is_region_point_excluded(item.region_id, item.grid_cell()))

    # ---- Plane fit review (list-based, styled like FocusReviewDialog) ----
    def _on_focus_point_shift_clicked(self, item):
        """Shift+click a marker - opens the plane review dialog scoped to that
        point's own region, pre-selected to it. See canvas_view.SectionCanvas.
        focusPointShiftClicked and plane_fit_review_dialog.PlaneFitReviewDialog."""
        group = [it for it in self.canvas.focus_point_items if it.region_id == item.region_id]
        title = f"Review Plane Fit - Region {item.region_id}"
        self._open_plane_review_dialog(group, title, preselect=item)

    def _on_review_global_plane_fit_clicked(self):
        # Always every compiled focus point, not just whatever the last search
        # ran on - so re-reviewing after an incremental "Autofocus New Points"
        # run still shows the whole sample, not just the newly-added subset.
        group = sorted(self.canvas.focus_point_items, key=lambda it: it.region_id)
        self._open_plane_review_dialog(group, "Review Plane Fit - Global Focus Search")

    def _open_plane_review_dialog(self, items: list, title: str, preselect=None):
        if not items:
            QMessageBox.information(self, "Review Plane Fit", "No focus points to review yet.")
            return
        if self._plane_review_dialog is not None:
            self._plane_review_dialog.close()  # replace any dialog already open, re-scoped to this group

        dialog = PlaneFitReviewDialog(
            title, items,
            get_info=self._plane_review_info,
            on_toggle=self._on_plane_review_toggle,
            on_selection_changed=self._on_plane_review_selection_changed,
            parent=self,
        )
        dialog.finished.connect(lambda _result, d=dialog: self._on_plane_review_dialog_finished(d))
        self._plane_review_dialog = dialog
        dialog.show()
        if preselect is not None:
            dialog.select_item(preselect)

    def _on_plane_review_dialog_finished(self, dialog: "PlaneFitReviewDialog"):
        if self._plane_review_dialog is dialog:
            self._plane_review_dialog = None

    def _plane_review_info(self, item) -> dict:
        """get_info callback for PlaneFitReviewDialog - see its docstring for
        the expected keys."""
        grid_cell = item.grid_cell()
        excluded = self._is_region_point_excluded(item.region_id, grid_cell)
        row, col = item.section()
        label = f"[{'EXCL' if excluded else 'OK'}] R{item.region_id} ({row:.1f}, {col:.1f})"

        if item.fit is None:
            return {
                "label": label, "excluded": excluded, "fit": None,
                "unavailable": "This point hasn't completed an autofocus sweep yet.",
            }

        plane = self.global_plane
        plane_z = plane.z_at(item.fit.row, item.fit.col) if plane is not None else None
        return {"label": label, "excluded": excluded, "fit": item.fit, "z": item.z, "plane_z": plane_z}

    def _on_plane_review_toggle(self, item):
        grid_cell = item.grid_cell()
        currently_excluded = self._is_region_point_excluded(item.region_id, grid_cell)
        # Flips the effective state: the dict stores "force included" (True) /
        # "force excluded" (False), so the new override is just the OLD
        # excluded flag itself (excluded=True -> force include=True, and vice
        # versa).
        override_value = currently_excluded
        self.region_inclusion_override.setdefault(item.region_id, {})[grid_cell] = override_value

        self._refresh_focus_point_exclusion_visuals()
        if self._plane_review_dialog is not None:
            self._plane_review_dialog.refresh()
        self.status_label.setText(
            f"Focus point ({grid_cell[0]}, {grid_cell[1]}): "
            f"{'included in' if override_value else 'excluded from'} plane fitting."
        )

    def _on_plane_review_selection_changed(self, item):
        """Highlights the newly selected point's marker in red (see
        focus_point_item.FocusPointItem.set_highlighted) and un-highlights
        whatever was selected before, so exactly one marker (or none) is ever
        highlighted - including when the dialog closes (item=None)."""
        if self._plane_review_highlighted_item is not None and self._plane_review_highlighted_item is not item:
            self._plane_review_highlighted_item.set_highlighted(False)
        if item is not None:
            item.set_highlighted(True)
        self._plane_review_highlighted_item = item

    def _focus_points_locked(self) -> bool:
        """True while adding/removing a focus point could land in the middle of
        something that indexes into the canvas's current point list by position
        (a running autofocus sequence - bridge.is_busy() alone isn't enough here,
        since it drops back to False in the gaps between a sequence's individual
        hardware calls) or that's mid-review (a review dialog still open) - see
        _on_focus_point_add_requested/_on_focus_point_remove_requested."""
        return (
            self.bridge.is_busy() or self._global_af_sequence_active
            or self._redo_af_sequence_active
            or self._global_review_dialog is not None
            or self._redo_review_dialog is not None
        )

    def _on_focus_point_add_requested(self, region_id, row: float, col: float):
        """Double-click on empty canvas - see canvas_view.SectionCanvas.
        mouseDoubleClickEvent. region_id is None for a section not (yet)
        assigned to a compiled region - compile regions first."""
        if self._focus_points_locked():
            QMessageBox.warning(
                self, "Add Focus Point",
                "Finish or close out the current autofocus/review operation before adding a focus point.",
            )
            return

        if region_id is None:
            QMessageBox.information(self, "Add Focus Point", "Compile regions first.")
            return

        self.canvas.add_focus_point(region_id, row, col)
        self.status_label.setText(
            f"Added a focus point to region {region_id} at ({row:.1f}, {col:.1f}) - "
            f"run 'Autofocus New Points' to focus it."
        )
        self._refresh_focus_point_exclusion_visuals()

    def _on_focus_point_remove_requested(self, item):
        """Right-click on a focus point marker - see canvas_view.SectionCanvas.
        mousePressEvent. Also drops the point's confirmed Z (if any) and any
        manual include/exclude override for its section, so removing a bad
        point doesn't leave stale data behind for the next plane fit."""
        if self._focus_points_locked():
            QMessageBox.warning(
                self, "Remove Focus Point",
                "Finish or close out the current autofocus/review operation before removing a focus point.",
            )
            return

        grid_cell = item.grid_cell()
        region_id = item.region_id
        self.canvas.remove_focus_point(item)

        if region_id in self.region_focus_points:
            self.region_focus_points[region_id] = [
                (r, c, z) for r, c, z in self.region_focus_points[region_id]
                if (int(round(r)), int(round(c))) != grid_cell
            ]
        self.region_inclusion_override.get(region_id, {}).pop(grid_cell, None)

        self.status_label.setText(f"Region {region_id} focus point at {grid_cell} removed.")

    def _on_region_scan_finished(self):
        """Shared scanFinished handler for RegionScanWorker - only Scan Redo
        Sections creates one now (region_id is then the string "Redo")."""
        region_id = self._scanning_region_id
        self._set_hardware_controls_enabled(True)
        self.cancel_scan_btn.setEnabled(False)
        self._scan_worker = None
        self.canvas.set_region_status(region_id, "scanned")
        self._scanning_region_id = None
        self.status_label.setText(f"Region {region_id}: scan complete.")

    def _on_region_scan_failed(self, message: str):
        region_id = self._scanning_region_id
        cancelled = message == "Scan cancelled."
        self._set_hardware_controls_enabled(True)
        self.cancel_scan_btn.setEnabled(False)
        self._scan_worker = None
        self.status_label.setText(f"Region {region_id}: scan cancelled." if cancelled else f"Region {region_id}: scan failed.")
        self.canvas.set_region_status(region_id, "failed")
        self._scanning_region_id = None
        if not cancelled:
            QMessageBox.warning(self, "Scan", f"Scan failed: {message}")

    # ---- Redo/correction (post-hoc): marking, focus point, autofocus, scan ----
    def _on_clear_redo_marks_clicked(self):
        self.canvas.clear_redo_marks()
        self.status_label.setText("Cleared redo marks.")

    def _on_redo_focus_point_set_requested(self, row: float, col: float):
        if self._focus_points_locked():
            QMessageBox.warning(
                self, "Set Redo Focus Point",
                "Finish or close out the current autofocus/review operation first.",
            )
            return
        self.canvas.set_redo_focus_point(row, col)
        self._redo_af_item = None
        self._redo_confirmed_z = None
        self.status_label.setText(f"Redo focus point set at ({row:.1f}, {col:.1f}) - run autofocus for it next.")

    def _on_redo_focus_point_remove_requested(self):
        if self._focus_points_locked():
            QMessageBox.warning(
                self, "Remove Redo Focus Point",
                "Finish or close out the current autofocus/review operation before removing it.",
            )
            return
        self.canvas.clear_redo_focus_point()
        self._redo_af_item = None
        self._redo_confirmed_z = None
        self.status_label.setText("Redo focus point removed.")

    def _on_create_region_from_marks_clicked(self):
        if not self.canvas.redo_sections:
            QMessageBox.information(
                self, "Create Region", "Mark at least one section (via 'Mark Sections for Redo') first.",
            )
            return
        count = len(self.canvas.redo_sections)
        region_id = self.canvas.assign_new_region(self.canvas.redo_sections)
        self.mark_redo_btn.setChecked(False)  # done marking - avoid a stray toggle re-marking sections
        self.region_id_spin.setMaximum(max(self.region_id_spin.maximum(), region_id))
        self.region_id_spin.setValue(region_id)
        self.canvas.set_active_region(region_id)
        self.status_label.setText(
            f"Created region {region_id} from {count} marked section(s) - double-click within it to add "
            f"focus points, then Autofocus New Points to focus and confirm them."
        )

    def _on_run_redo_autofocus_clicked(self):
        if self.calibration is None:
            QMessageBox.warning(self, "Redo Autofocus", "Load a calibration file first.")
            return
        if self.bridge.is_busy() or self._redo_af_sequence_active:
            QMessageBox.warning(self, "Redo Autofocus", "Another hardware operation is already in progress.")
            return
        item = self.canvas.redo_focus_point_item
        if item is None:
            QMessageBox.information(
                self, "Redo Autofocus",
                "Enable 'Set Redo Focus Point' and click a section to place one first.",
            )
            return
        if not self.canvas.redo_sections:
            QMessageBox.information(self, "Redo Autofocus", "Mark at least one section for redo first.")
            return

        item.set_status(None)
        self._redo_confirmed_z = None
        point = item.section()

        self._set_hardware_controls_enabled(False)
        self.status_label.setText("Running autofocus for the redo focus point...")

        # Non-modal, same as the per-region/global runs - lets the operator watch
        # this point's curve appear live via add_fit() once it's fitted.
        dialog = FocusReviewDialog(self.bridge, self.calibration, "Redo", [], parent=self,
                                    title="Review Redo Focus Point")
        dialog.finished.connect(lambda result, d=dialog: self._on_redo_review_dialog_finished(d, result))
        dialog.show()
        self._redo_review_dialog = dialog

        worker = AutofocusSequenceWorker(self.bridge, self.calibration, [point], z_start=self.af_z_start_spin.value())
        worker.pointStarted.connect(self._on_redo_af_point_started)
        worker.pointFitted.connect(self._on_redo_af_point_fitted)
        worker.pointFailed.connect(self._on_redo_af_point_failed)
        worker.sequenceFinished.connect(lambda: self._on_redo_af_sequence_finished(worker))
        self._redo_af_item = item
        self._redo_af_worker = worker  # keep alive for the duration of the sequence
        self._redo_af_sequence_active = True
        worker.start()

    def _on_redo_af_point_started(self, index: int, total: int):
        self.status_label.setText("Running autofocus for the redo focus point...")
        if self._redo_af_item is not None:
            self._redo_af_item.set_status("focusing")

    def _on_redo_af_point_fitted(self, index: int, fit):
        if self._redo_af_item is not None:
            self._redo_af_item.set_status("done")
            self._redo_af_item.fit = fit  # for a later shift-click review, see FocusPointItem.fit
        if self._redo_review_dialog is not None:
            self._redo_review_dialog.add_fit(fit)

    def _on_redo_af_point_failed(self, index: int, message: str):
        if self._redo_af_item is not None:
            self._redo_af_item.set_status("failed")
        QMessageBox.warning(self, "Redo autofocus failed", message)

    def _on_redo_af_sequence_finished(self, worker: AutofocusSequenceWorker):
        self._redo_af_sequence_active = False
        self._set_hardware_controls_enabled(True)
        self.status_label.setText(f"Redo autofocus done: {len(worker.fits)} point(s) fitted.")

        if self._redo_review_dialog is not None:
            self._redo_review_dialog.finish_collecting()

        try:
            self._redo_af_log_path = autofocus_log.write_region_log("Redo", worker.records)
        except OSError as e:
            self._redo_af_log_path = None
            self.status_label.setText(f"Redo autofocus done, but could not write its log: {e}")

    def _on_redo_review_dialog_finished(self, dialog: FocusReviewDialog, result: int):
        if not dialog.fits:
            pass  # already marked "failed" in _on_redo_af_sequence_finished; nothing to confirm
        elif result == FocusReviewDialog.DialogCode.Accepted:
            z = dialog.confirmed_z.get(0)
            self._redo_confirmed_z = z
            if self._redo_af_item is not None:
                self._redo_af_item.z = z
            self.status_label.setText(
                f"Redo focus point confirmed at Z={z:.4f} - ready to scan the marked section(s)."
            )
            if self._redo_af_log_path is not None:
                autofocus_log.update_confirmed(self._redo_af_log_path, dialog.confirmed_z_with_source())
        else:
            # Operator declined the fit (or closed the dialog) - go back to
            # unconfirmed so a retry is unambiguous.
            self._redo_confirmed_z = None
            self.status_label.setText("Redo focus point not confirmed.")

        if self._redo_review_dialog is dialog:
            self._redo_review_dialog = None

    def _on_scan_redo_sections_clicked(self):
        if self.calibration is None:
            QMessageBox.warning(self, "Scan Redo Sections", "Load a calibration file first.")
            return
        if self.bridge.is_busy() or self._scan_worker is not None:
            QMessageBox.warning(self, "Scan Redo Sections", "Another hardware operation is already in progress.")
            return
        if not self.canvas.redo_sections:
            QMessageBox.information(self, "Scan Redo Sections", "Mark at least one section for redo first.")
            return
        if self._redo_confirmed_z is None:
            QMessageBox.warning(
                self, "Scan Redo Sections",
                "Run autofocus for the redo focus point and confirm a Z first.",
            )
            return

        z = self._redo_confirmed_z
        ordered_sections = clustering.serpentine_order(list(self.canvas.redo_sections))
        sections_with_z = [(row, col, z) for row, col in ordered_sections]

        reply = QMessageBox.question(
            self, "Scan Redo Sections",
            f"Rescan these {len(sections_with_z)} marked section(s) now, all at the confirmed redo focus "
            f"point's Z ({z:.4f})? This submits a real open-loop Lucas-path scan - the stage will move "
            f"for real.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._set_hardware_controls_enabled(False)
        self.cancel_scan_btn.setEnabled(True)
        self.status_label.setText(f"Scanning {len(sections_with_z)} redo section(s)...")
        self._scanning_region_id = "Redo"

        worker = RegionScanWorker(self.bridge, self.calibration, sections_with_z)
        worker.scanFailed.connect(self._on_region_scan_failed)
        worker.scanFinished.connect(self._on_region_scan_finished)
        self._scan_worker = worker  # keep alive for the duration of the scan
        worker.start()

    def closeEvent(self, event):
        if self._scan_worker is not None:
            self.bridge.cancel_current_scan()
        self._dover_window.send_quit_on_exit()
        self._goji_scheduler.stop()
        self.bridge.shutdown()
        super().closeEvent(event)
