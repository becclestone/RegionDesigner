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
import scan_path_export
import scan_record_import
import session_state
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
_DEFAULT_GLOBAL_FOCUS_POINTS = 12
# A region scan's archive-to-aggregate-batch move waits for every section's
# reconstruction-off message (see _on_section_reconstructing); _recheck_tracker_files
# is the fallback for a message that never arrives, and re-checks the actual NR
# file on disk rather than guessing a fixed wait time.
_RECON_POLL_INTERVAL_MS = 15 * 1000
# Reconstruction finishes sections in the same order they were scanned (the
# controller processes its capture queue in order), so a tracker's "remaining"
# list is kept in that same order and only its FRONT ever needs checking - once
# it isn't there yet, nothing behind it will be either. Sections normally
# finish no more than about this far apart; once it's been longer than that
# since the last one arrived (live broadcast or found on disk), assume no more
# are coming rather than wait on a fixed total budget that can't adapt to how
# fast reconstruction actually is - see _recheck_tracker_files/
# _on_section_reconstructing for where this is applied, and
# _auto_pipeline_wait_tracker for why it also needs to release Auto Run All
# Regions' hold on the next region rather than potentially wait forever.
_RECON_GAP_TIMEOUT_S = 60

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

        self.calibration: StageCalibration | None = None
        self.region_focus_points: dict[int, list[tuple[float, float, float]]] = {}  # region_id -> [(row,col,z)]
        self.region_planes: dict[int, RegionPlaneFit] = {}  # region_id -> its last Fit Region Plane result
        self.section_z: dict[tuple[int, int], float] = {}
        self._af_worker = None
        self._af_region_id: int | None = None
        self._af_region_items: list = []
        self._af_log_path: str | None = None  # this region's staged autofocus_log.json, see autofocus_log.py
        # True for the whole span of a running AutofocusSequenceWorker, not just
        # while a hardware call is actually in flight (bridge.is_busy() drops
        # back to False in the gaps between the worker's individual calls) - see
        # _focus_points_locked, which needs the former to safely gate
        # double-click-add/right-click-remove against a sequence that's
        # currently indexing into the canvas's point list by position.
        self._af_sequence_active = False
        self._review_dialog: FocusReviewDialog | None = None

        # Global Focus Search (second fitting mode): autofocus a handful of
        # points spread across the WHOLE painted sample (not per-region), then
        # RANSAC-fit one plane across all of them - RANSAC's own inlier/outlier
        # classification (region_plane_fit.RegionPlaneFit.inlier_mask) flags
        # points inconsistent with the sample's overall tilt (a fold, dust,
        # etc.). Those flagged sections are then kept out of the per-region
        # focus-point placement and per-region plane fits below, rather than
        # letting a bad point silently skew one region's local fit.
        self.global_focus_points: list[tuple[float, float, float]] = []  # confirmed (row, col, z)
        self.global_outlier_sections: set[tuple[int, int]] = set()  # grid cells RANSAC flagged as outliers
        self.global_plane: RegionPlaneFit | None = None
        self._global_fit_point_snapshot: dict[tuple[int, int], float] = {}  # cell -> z as of the last fit/update
        self._global_af_worker = None
        self._global_af_items: list = []
        self._global_af_log_path: str | None = None
        self._global_af_sequence_active = False  # see _af_sequence_active above
        self._global_review_dialog: FocusReviewDialog | None = None
        # Set by _on_run_global_focus_plane_fitting_clicked, consumed by
        # _on_global_review_dialog_finished - chains straight into
        # _on_fit_global_plane_clicked once the operator confirms the search,
        # so the one-click button covers all three Global Focus Search steps.
        self._global_auto_fit_after_search = False

        # Manual per-point overrides from shift-clicking a marker (see
        # canvas_view.SectionCanvas.focusPointShiftClicked and
        # _on_focus_point_shift_clicked) - grid cell -> True (force included) /
        # False (force excluded). Always takes priority over the automatic
        # global_outlier_sections flag above; see _is_region_point_excluded /
        # _is_global_point_excluded, the two places that resolve both into one
        # final in-or-out decision for plane fitting.
        self.region_inclusion_override: dict[int, dict[tuple[int, int], bool]] = {}
        self.global_inclusion_override: dict[tuple[int, int], bool] = {}

        # The one PlaneFitReviewDialog open at a time (see
        # _open_plane_review_dialog), and whichever of its points is currently
        # selected/highlighted red on the canvas (see
        # _on_plane_review_selection_changed) - kept here so it can be
        # un-highlighted the moment selection moves on or the dialog closes.
        self._plane_review_dialog: PlaneFitReviewDialog | None = None
        self._plane_review_highlighted_item = None

        self._scan_worker = None
        self._scanning_region_id: int | None = None
        self._scan_existing_run_folders: set[str] = set()
        # "Send Scan Path to Controller" progress - cells still awaiting a
        # sectionScanning off-edge, and the path's total row count for the
        # status label's "N/M" count. None when no full-path send is in flight.
        self._full_path_scan_remaining: set[tuple[int, int]] | None = None
        self._full_path_scan_total: int = 0
        self._aggregate_root: str | None = None
        # Last snap image path (see _on_image_ready) - saved into session.json
        # as a best-effort visual reference for "Open Previous Scan...", since
        # a snap always comes from live hardware and there's no other way to
        # get the same background back after a restart.
        self._last_snap_path: str | None = None
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
        # Set (to a _scan_recon_trackers entry) by _on_region_scan_finished right
        # after a region's scan completes, while the pipeline is active - holds
        # the pipeline from starting the next region's autofocus/confirmation
        # captures until every section in that tracker reports reconstruction-
        # off (see _maybe_archive_tracker, the single place that clears this and
        # advances). Running the next region's hardware ops while the previous
        # one's images are still reconstructing risks a real hardware conflict
        # (same physical stage/camera resources) - see region_scan.py's
        # module docstring for why reconstruction can otherwise lag behind.
        self._auto_pipeline_wait_tracker: dict | None = None

        # Redo/correction (post-hoc): autofocus + contrast-confirm state for the
        # single redo focus point (see canvas.redo_focus_point_item), and the Z it
        # confirms to once accepted - mirrors _af_*/region_focus_points, but for
        # exactly one point shared across every section marked for redo, not a
        # per-region plane fit. "Redo" (a string, not an int) is used as the
        # region_id/tracker key everywhere a real region scan would use an int, so
        # it archives into its own Region_Redo/ folder alongside the rest of the
        # active aggregate batch without colliding with any real region's id.
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

        self.snap_focus_points_on_release_check = QCheckBox("Snap on Drag")
        self.snap_focus_points_on_release_check.setToolTip(
            "While checked, releasing a dragged focus point immediately snaps it "
            "to the exact center of its nearest section."
        )
        self.snap_focus_points_on_release_check.toggled.connect(
            self.canvas.set_snap_focus_points_on_release
        )
        toolbar.addWidget(self.snap_focus_points_on_release_check)

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

        self.review_region_plane_fit_btn = QPushButton("Review Region Plane Fit")
        self.review_region_plane_fit_btn.setToolTip(
            "Lists every focus point in the current region - click through them to see each one's focus "
            "curve and how far it sits from the fitted plane, with its marker highlighted red on the "
            "canvas so it can be found on the sample. Also opens (pre-selected to the clicked point) by "
            "shift+clicking any region marker."
        )
        self.review_region_plane_fit_btn.clicked.connect(self._on_review_region_plane_fit_clicked)
        toolbar2.addWidget(self.review_region_plane_fit_btn)

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
            "targeting it. If it has a saved session.json (written automatically at Compile Regions/"
            "confirm/Start New Aggregate Batch), also restores the canvas's painted regions and confirmed "
            "focus points, and marks any region a hardware failure interrupted partway through its scan "
            "as \"partial\" so Scan Region can resume just its missing sections. Older batches without a "
            "saved session.json only get the aggregate-batch retarget, same as before."
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

        # Third row: Global Focus Search - the alternative fitting mode. Its own
        # row since it's a distinct workflow (whole-sample, not per-region), but
        # it doesn't depend on Compile Regions in either direction - it can be
        # run before painting is turned into regions, or any time after (e.g.
        # once the regions are visible and it's clearer where a fold/dust patch
        # actually sits), via either the one-click button or the individual
        # steps below it.
        self.addToolBarBreak()
        toolbar3 = QToolBar("Global Focus Search (RANSAC outlier rejection)", self)
        self.addToolBar(toolbar3)

        toolbar3.addWidget(QLabel(" Global focus points: "))
        self.global_focus_points_spin = QSpinBox()
        self.global_focus_points_spin.setRange(3, 200)
        self.global_focus_points_spin.setValue(_DEFAULT_GLOBAL_FOCUS_POINTS)
        toolbar3.addWidget(self.global_focus_points_spin)

        self.run_global_focus_plane_fitting_btn = QPushButton("Run Global Focus Plane Fitting")
        self.run_global_focus_plane_fitting_btn.setToolTip(
            "One-click version of the three steps to its right: places global focus points (only if none "
            "are placed yet - reuses an existing set otherwise), runs autofocus on them, and - once you "
            "confirm the search in the review dialog - automatically fits the global RANSAC plane and "
            "flags outliers. Safe to run any time, including right after Compile Regions."
        )
        self.run_global_focus_plane_fitting_btn.clicked.connect(self._on_run_global_focus_plane_fitting_clicked)
        toolbar3.addWidget(self.run_global_focus_plane_fitting_btn)

        toolbar3.addSeparator()
        toolbar3.addWidget(QLabel(" Individual steps: "))

        self.place_global_points_btn = QPushButton("Place Global Focus Points")
        self.place_global_points_btn.setToolTip(
            "Spreads the given number of focus points across the WHOLE painted sample (not one region), "
            "the same way Compile Regions places points within a region."
        )
        self.place_global_points_btn.clicked.connect(self._on_place_global_focus_points_clicked)
        toolbar3.addWidget(self.place_global_points_btn)

        self.run_global_focus_search_btn = QPushButton("Run Global Focus Search")
        self.run_global_focus_search_btn.setToolTip(
            "Runs autofocus for every global focus point, then opens the same review dialog used for a "
            "region so each point's Z can be confirmed via scan or manual override."
        )
        self.run_global_focus_search_btn.clicked.connect(self._on_run_global_focus_search_clicked)
        toolbar3.addWidget(self.run_global_focus_search_btn)

        toolbar3.addWidget(QLabel(" Outlier threshold (mm, 0=auto): "))
        self.global_outlier_threshold_spin = QDoubleSpinBox()
        self.global_outlier_threshold_spin.setDecimals(4)
        self.global_outlier_threshold_spin.setRange(0.0, 1.0)
        self.global_outlier_threshold_spin.setSingleStep(0.001)
        self.global_outlier_threshold_spin.setValue(0.0)
        self.global_outlier_threshold_spin.setToolTip(
            "Max perpendicular distance (mm) from the fitted plane for a point to count as an inlier. "
            "0 lets RANSAC pick its own threshold automatically."
        )
        toolbar3.addWidget(self.global_outlier_threshold_spin)

        self.fit_global_plane_btn = QPushButton("Fit Global RANSAC Plane / Find Outliers")
        self.fit_global_plane_btn.setToolTip(
            "RANSAC-fits one plane across every confirmed global focus point and flags (orange) whichever "
            "points don't fit it - candidates for a fold, dust, or other local defect. Confirm the global "
            "focus search first."
        )
        self.fit_global_plane_btn.clicked.connect(self._on_fit_global_plane_clicked)
        toolbar3.addWidget(self.fit_global_plane_btn)

        self.update_global_plane_btn = QPushButton("Update Global Plane")
        self.update_global_plane_btn.setToolTip(
            "Re-fits the global plane the same way Fit Global RANSAC Plane does (RANSAC needs every "
            "current point to produce a valid plane, so the fit itself always uses all of them), but "
            "only pushes a new Z for sections whose focus point is new or changed since the last fit/"
            "update - so a Fit Region Plane result elsewhere isn't clobbered just to account for one "
            "added or moved point. Requires an initial Fit Global RANSAC Plane first."
        )
        self.update_global_plane_btn.clicked.connect(self._on_update_global_plane_clicked)
        toolbar3.addWidget(self.update_global_plane_btn)

        self.review_global_plane_fit_btn = QPushButton("Review Global Plane Fit")
        self.review_global_plane_fit_btn.setToolTip(
            "Lists every point the last Global Focus Search ran on - click through them to see each one's "
            "focus curve and how far it sits from the global plane, with its marker highlighted red on the "
            "canvas so it can be found on the sample. Also opens (pre-selected to the clicked point) by "
            "shift+clicking any global marker."
        )
        self.review_global_plane_fit_btn.clicked.connect(self._on_review_global_plane_fit_clicked)
        toolbar3.addWidget(self.review_global_plane_fit_btn)

        self.exclude_global_outliers_checkbox = QCheckBox("Exclude flagged outliers from region search")
        self.exclude_global_outliers_checkbox.setChecked(True)
        self.exclude_global_outliers_checkbox.setToolTip(
            "When checked, sections flagged as outliers above are skipped when Compile Regions places "
            "per-region focus points, and dropped from any per-region focus point that already landed on "
            "one before Fit Region Plane runs. Shift+click a point's marker to manually include/exclude it "
            "regardless of this setting."
        )
        self.exclude_global_outliers_checkbox.toggled.connect(lambda _checked: self._refresh_focus_point_exclusion_visuals())
        toolbar3.addWidget(self.exclude_global_outliers_checkbox)

        clear_global_points_btn = QPushButton("Clear Global Focus Points")
        clear_global_points_btn.clicked.connect(self._on_clear_global_focus_points_clicked)
        toolbar3.addWidget(clear_global_points_btn)

        toolbar3.addSeparator()
        toolbar3.addWidget(QLabel(
            " Shift+click a focus point to open its plane-fit review list (selecting a point there "
            "highlights it red on the canvas) - double-click empty canvas to add a point, right-click one "
            "to remove it. "
        ))

        # Fourth row: Redo/Correct Scans (post-hoc) - load a previously finalized
        # scan_record.json to get the region layout/section Z back, mark specific
        # sections that need a fresh scan, focus one point for all of them (with
        # the same contrast-confirm review used for a region), then rescan just
        # that marked set as one Lucas path. Independent of every workflow above -
        # it's meant to run well after Finalize Results, possibly in a new session.
        self.addToolBarBreak()
        toolbar4 = QToolBar("Redo / Correct Scans (post-hoc)", self)
        self.addToolBar(toolbar4)

        self.load_scan_record_btn = QPushButton("Load Scan Record...")
        self.load_scan_record_btn.setToolTip(
            "Loads a previously finalized scan_record.json (Finalize Results' output) to repopulate the "
            "region layout and each section's scanned Z, for correcting specific sections after the fact. "
            "Doesn't restore focus points or plane fits - only the layout/Z a targeted redo needs."
        )
        self.load_scan_record_btn.clicked.connect(self._on_load_scan_record_clicked)
        toolbar4.addWidget(self.load_scan_record_btn)

        toolbar4.addSeparator()

        self.mark_redo_btn = QPushButton("Mark Sections for Redo")
        self.mark_redo_btn.setCheckable(True)
        self.mark_redo_btn.setToolTip(
            "While active, click/drag with the brush over already-loaded sections to mark (left button) "
            "or unmark (right button) them for redo - shown with a magenta overlay."
        )
        self.mark_redo_btn.toggled.connect(self.canvas.set_redo_mode)
        toolbar4.addWidget(self.mark_redo_btn)

        clear_redo_marks_btn = QPushButton("Clear Redo Marks")
        clear_redo_marks_btn.clicked.connect(self._on_clear_redo_marks_clicked)
        toolbar4.addWidget(clear_redo_marks_btn)

        toolbar4.addSeparator()
        toolbar4.addWidget(QLabel(" Then either: "))

        self.create_region_from_marks_btn = QPushButton("Create Region from Marked Sections")
        self.create_region_from_marks_btn.setToolTip(
            "For a more accurate redo than one flat Z: carves the marked sections out into a brand-new "
            "region (its own id, selected automatically below) - double-click within it to place several "
            "focus points by hand, then use Run Autofocus for Region / Fit Region Plane / Scan Region on "
            "it exactly like any compiled region, with a proper per-section plane-fit Z. Clears the marks "
            "and turns off 'Mark Sections for Redo' once created."
        )
        self.create_region_from_marks_btn.clicked.connect(self._on_create_region_from_marks_clicked)
        toolbar4.addWidget(self.create_region_from_marks_btn)

        toolbar4.addWidget(QLabel(" or: "))

        self.pick_redo_focus_btn = QPushButton("Set Redo Focus Point")
        self.pick_redo_focus_btn.setCheckable(True)
        self.pick_redo_focus_btn.setToolTip(
            "Quick path: one flat Z for every marked section. While active, click anywhere on the canvas "
            "to place (or move) the single focus point used to focus all of them. Right-click it to "
            "remove it."
        )
        self.pick_redo_focus_btn.toggled.connect(self.canvas.set_pick_redo_focus_mode)
        toolbar4.addWidget(self.pick_redo_focus_btn)

        self.run_redo_autofocus_btn = QPushButton("Run Autofocus for Redo Point")
        self.run_redo_autofocus_btn.setToolTip(
            "Runs autofocus on the redo focus point, then lets you confirm its Z via a real contrast "
            "scan - the same review dialog used to confirm a region's own focus points."
        )
        self.run_redo_autofocus_btn.clicked.connect(self._on_run_redo_autofocus_clicked)
        toolbar4.addWidget(self.run_redo_autofocus_btn)

        self.scan_redo_btn = QPushButton("Scan Redo Sections")
        self.scan_redo_btn.setToolTip(
            "Submits every section marked for redo, all at the confirmed redo focus point's Z, as one "
            "real Lucas-path scan - same open-loop mechanism as Scan Region, just for this ad hoc set."
        )
        self.scan_redo_btn.clicked.connect(self._on_scan_redo_sections_clicked)
        toolbar4.addWidget(self.scan_redo_btn)

    def _on_brush_radius_changed(self, value: int):
        self.canvas.brush_radius = value

    def _on_region_id_changed(self, value: int):
        self.canvas.set_active_region(value)

    def _on_snap_clicked(self):
        self.bridge.request_snap()

    def _on_image_ready(self, image_path: str):
        self.canvas.set_background_image(image_path)
        self._last_snap_path = image_path

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
        if active:
            return
        section = (master_row, master_col)
        for tracker in self._scan_recon_trackers:
            if section in tracker["remaining"]:
                tracker["remaining"].remove(section)
                # Resets the gap clock _recheck_tracker_files measures against -
                # see _RECON_GAP_TIMEOUT_S. Also clears warned so a later stall,
                # after this arrival, can report again rather than staying silent.
                tracker["last_arrival"] = time.monotonic()
                tracker["warned"] = False
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
        self.region_inclusion_override = {}  # every region's points are being replaced wholesale
        self.region_planes = {}

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
        self._save_session()

    def _on_clear_regions_clicked(self):
        self.canvas.clear_regions()
        self.region_id_spin.setMaximum(9999)
        self.region_inclusion_override = {}
        self.region_planes = {}

    def _on_snap_focus_points_clicked(self):
        count = self.canvas.snap_focus_points_to_grid()
        self.status_label.setText(f"Snapped {count} focus point(s) to their nearest section center.")

    def _on_reset_clicked(self):
        if (
            self.bridge.is_busy() or self._af_worker is not None or self._scan_worker is not None
            or self._review_dialog is not None or self._auto_pipeline_active
            or self._redo_af_worker is not None or self._redo_review_dialog is not None
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
        self.region_planes = {}
        self.section_z = {}
        self._af_worker = None
        self._af_region_id = None
        self._af_region_items = []
        self._af_log_path = None
        self._af_sequence_active = False
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
        self.global_inclusion_override = {}
        if self._plane_review_dialog is not None:
            self._plane_review_dialog.close()
        self._plane_review_dialog = None
        self._plane_review_highlighted_item = None
        self._scanning_region_id = None
        self._scan_existing_run_folders = set()
        self._full_path_scan_remaining = None
        self._full_path_scan_total = 0
        self._aggregate_root = None
        self._scan_recon_trackers = []
        self._active_scan_tracker = None
        self._auto_pipeline_region_ids = []
        self._auto_pipeline_index = 0
        self._auto_pipeline_wait_tracker = None
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

    def _save_session(self):
        """Checkpoint save of the current region design (see session_state.py's
        module docstring for exactly what is/isn't persisted) into the active
        aggregate batch - no-op until one exists. Called after every step that
        changes something session_state.save_session actually persists, so a
        crash any time after that step still leaves a reloadable batch."""
        if self._aggregate_root is None:
            return
        session_state.save_session(
            self._aggregate_root,
            painted=self.canvas.painted,
            region_of=self.canvas.region_of,
            region_focus_points=self.region_focus_points,
            region_inclusion_override=self.region_inclusion_override,
            background_image_path=self._last_snap_path,
        )

    def _on_open_previous_scan_clicked(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select a previous aggregate batch folder", confirmation_scan.IMAGES_ROOT,
        )
        if not path:
            return
        self._aggregate_root = path
        session = session_state.load_session(path)
        if session is None:
            self.status_label.setText(
                f"Aggregate batch set to: {os.path.basename(path)} (no saved region design found in it - "
                f"painted regions/focus points were not restored)."
            )
            return
        num_regions, num_sections = self._apply_loaded_session(session)
        self.status_label.setText(
            f"Aggregate batch reloaded: {os.path.basename(path)} - restored {num_sections} section(s) "
            f"across {num_regions} region(s). A region already fully scanned shows \"scanned\"; a region "
            f"scanned only partway through before shows \"partial\" and can be resumed with Scan Region."
        )

    def _apply_loaded_session(self, session: dict) -> tuple[int, int]:
        """Repopulates the canvas/region state from a previously saved
        session.json (see _on_open_previous_scan_clicked). Returns (number of
        regions, number of sections) restored, for the status message."""
        self.canvas.reset()

        region_of = {(row, col): region_id for row, col, region_id in session["region_of"]}
        self.canvas.painted = {(row, col) for row, col in session["painted"]}
        self.canvas.apply_regions(region_of)

        self.region_focus_points = {}
        for region_id, row, col, z in session["region_focus_points"]:
            self.region_focus_points.setdefault(region_id, []).append((row, col, z))

        self.region_inclusion_override = {}
        for region_id, row, col, included in session["region_inclusion_override"]:
            self.region_inclusion_override.setdefault(region_id, {})[(row, col)] = included

        for region_id, points in self.region_focus_points.items():
            for row, col, _z in points:
                self.canvas.add_focus_point(region_id, row, col)

        background_image_path = session.get("background_image_path")
        if background_image_path and os.path.isfile(background_image_path):
            try:
                self.canvas.set_background_image(background_image_path)
                self._last_snap_path = background_image_path
            except Exception:
                pass  # non-critical - operator can Snap again for a fresh reference image

        region_ids = sorted(set(region_of.values()))
        if region_ids:
            self.region_id_spin.setMaximum(max(region_ids))
            self.region_id_spin.setValue(region_ids[0])
            self.canvas.set_active_region(region_ids[0])

        for region_id in region_ids:
            self._restore_region_status(region_id, region_of)

        return len(region_ids), len(region_of)

    def _restore_region_status(self, region_id: int, region_of: dict[tuple[int, int], int]):
        """Recomputes region_planes/section_z (from region_focus_points, via
        the same _fit_plane_for_region a manual Fit Region Plane click uses)
        and this region's status - always from what's actually on disk under
        the aggregate root, never from anything saved, so a stale save can
        never claim more progress than really happened. See
        session_state.completed_master_sections."""
        focus_points = self.region_focus_points.get(region_id)
        if focus_points and len(focus_points) >= 3:
            self._fit_plane_for_region(region_id)

        status = "confirmed" if focus_points else None
        if self._aggregate_root is not None:
            scan_data_dir = os.path.join(self._aggregate_root, f"Region_{region_id}", "Scan_data")
            if os.path.isdir(scan_data_dir) and self.calibration is not None:
                region_sections = [s for s, rid in region_of.items() if rid == region_id]
                done_master = session_state.completed_master_sections(scan_data_dir)
                expected_master = {
                    (int(round(self.calibration.offset_row + row)), int(round(self.calibration.offset_col + col)))
                    for row, col in region_sections
                }
                done_count = len(expected_master & done_master)
                if expected_master and done_count == len(expected_master):
                    status = "scanned"
                else:
                    status = "partial"
                    self.canvas.set_region_progress(region_id, done_count, len(expected_master))
            elif os.path.isdir(scan_data_dir):
                # Scan_data exists but no calibration loaded yet to convert to
                # master-grid coordinates - can't tell what's done, so don't
                # guess; leave it "confirmed" until calibration is available.
                pass
        self.canvas.set_region_status(region_id, status)

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
        self.region_planes = {}
        self.section_z = section_z

        region_ids = sorted(set(region_of.values()))
        for region_id in region_ids:
            self.canvas.set_region_status(region_id, "scanned")
        self.region_id_spin.setMaximum(max(region_ids))
        self.region_id_spin.setValue(region_ids[0])
        self.canvas.set_active_region(region_ids[0])

        # Redo scans re-target the same aggregate batch the record was finalized
        # from (scan_finalize.finalize_aggregate writes it to
        # <aggregate_root>/Finalized/scan_record.json), so a redo's output
        # archives alongside the original scan instead of starting a fresh batch.
        parent_dir = os.path.dirname(path)
        self._aggregate_root = (
            os.path.dirname(parent_dir) if os.path.basename(parent_dir) == scan_finalize.FINALIZED_DIR_NAME
            else parent_dir
        )

        self.status_label.setText(
            f"Loaded scan record: {len(region_of)} section(s) across {len(region_ids)} region(s) from "
            f"{os.path.basename(path)}. Use 'Mark Sections for Redo' to select sections to correct."
        )

    def _on_start_aggregate_clicked(self):
        self._aggregate_root = scan_archive.start_new_aggregate_batch()
        self.status_label.setText(f"New aggregate batch: {os.path.basename(self._aggregate_root)}")
        self._save_session()

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

        start_dir = self._aggregate_root or ""
        out_path, _filter = QFileDialog.getSaveFileName(
            self, "Export Scan Path", start_dir, "Plane path (*.pp)"
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
        self.status_label.setText(f"Sending scan path to controller: 0/{len(rows)} section(s) scanned...")

        worker = PlanePathScanWorker(self.bridge, self.calibration, rows)
        worker.scanFailed.connect(self._on_full_path_scan_failed)
        worker.scanFinished.connect(self._on_full_path_scan_finished)
        self._scan_worker = worker  # keep alive for the duration of the scan
        worker.start()

    def _on_full_path_scan_finished(self):
        self._set_hardware_controls_enabled(True)
        total = self._full_path_scan_total
        done = total - len(self._full_path_scan_remaining or ())
        self._full_path_scan_remaining = None
        self._scan_worker = None
        self.status_label.setText(f"Scan path complete: {done}/{total} section(s) scanned.")

    def _on_full_path_scan_failed(self, message: str):
        self._set_hardware_controls_enabled(True)
        self._full_path_scan_remaining = None
        self._scan_worker = None
        QMessageBox.warning(self, "Send Scan Path", f"Scan path failed: {message}")
        self.status_label.setText("Scan path failed.")

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
        # Skips a point _is_region_point_excluded currently considers excluded -
        # the Global Focus Search's RANSAC flag (if the checkbox is on) or a
        # manual shift-click override - so a point already known to sit on a
        # fold/dust doesn't burn real hardware time on a fresh autofocus sweep
        # and contrast/Auto-Search confirmation, only to be dropped at Fit
        # Region Plane anyway. A point removed outright (right-click) is simply
        # absent from canvas.focus_point_items already, so it's covered too.
        region_items = [
            item for item in self.canvas.focus_point_items
            if item.region_id == region_id and not self._is_region_point_excluded(region_id, item.grid_cell())
        ]
        points = [item.section() for item in region_items]
        if not points:
            all_region_items = [item for item in self.canvas.focus_point_items if item.region_id == region_id]
            if all_region_items:
                QMessageBox.information(
                    self, "Run Autofocus",
                    f"Every focus point in region {region_id} is currently excluded from plane fitting - "
                    f"nothing to autofocus. Shift+click a point to re-include it, or add a new one.",
                )
            else:
                QMessageBox.information(self, "Run Autofocus", f"No focus points found for region {region_id}.")
            return False

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
        self._af_sequence_active = True
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
            self._af_region_items[index].fit = fit  # for a later shift-click review, see _on_focus_point_shift_clicked
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
        self.run_global_focus_search_btn.setEnabled(enabled)
        self.run_global_focus_plane_fitting_btn.setEnabled(enabled)
        self.send_scan_path_btn.setEnabled(enabled)
        self.run_redo_autofocus_btn.setEnabled(enabled)
        self.scan_redo_btn.setEnabled(enabled)

    def _on_autofocus_sequence_finished(self, region_id: int, worker: AutofocusSequenceWorker):
        self._af_sequence_active = False
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
            # Fresh confirmation - this region's prior manual include/exclude
            # overrides (if any) no longer necessarily apply to the same points,
            # and any previously fit plane is now stale until Fit Region Plane
            # is run again (see PlaneFitReviewDialog's plane-error display).
            self.region_inclusion_override[region_id] = {}
            self.region_planes.pop(region_id, None)
            self._record_confirmed_z(
                [it for it in self.canvas.focus_point_items if it.region_id == region_id], dialog,
            )
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
            self._save_session()
        else:
            # Operator declined the fit (or closed the dialog) - go back to
            # unmarked/pending so a retry is unambiguous.
            self.canvas.set_region_status(region_id, None)

        if self._review_dialog is dialog:
            self._review_dialog = None

        self._refresh_focus_point_exclusion_visuals()

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

        # Drop any point _is_region_point_excluded now considers excluded - the
        # Global Focus Search's RANSAC flag (if the checkbox is on) or a manual
        # shift-click override, whichever applies - unless that would leave
        # fewer than 3 points (RegionPlaneFit's minimum), in which case keep the
        # original set rather than failing the fit outright.
        filtered = [
            (row, col, z) for row, col, z in focus_points
            if not self._is_region_point_excluded(region_id, (int(round(row)), int(round(col))))
        ]
        if len(filtered) >= 3:
            focus_points = filtered

        try:
            plane = RegionPlaneFit(focus_points)
        except ValueError as e:
            QMessageBox.warning(self, "Fit Region Plane", str(e))
            return False

        self.region_planes[region_id] = plane
        self.section_z.update(plane.fill_sections(region_sections))
        residuals = plane.residuals()
        self.status_label.setText(
            f"Region {region_id}: plane fit over {len(region_sections)} section(s), "
            f"max focus-point residual = {max(residuals):.4f}mm."
        )
        return True

    # ---- Global Focus Search (second fitting mode) ----
    def _on_run_global_focus_plane_fitting_clicked(self):
        """One-click chain of Run Global Focus Search -> Fit Global RANSAC
        Plane, the last step firing automatically once the operator confirms
        the search (see _on_global_review_dialog_finished).

        If Compile Regions has already placed per-region focus points, THOSE
        are reused as the point set (across every region at once) rather than
        computing an independent whole-sample distribution - the operator
        already chose those positions, and a second, differently-placed set
        would just be redundant. Only when no region points exist yet (e.g.
        run before Compile Regions) does this fall back to placing a dedicated
        set of global points itself."""
        if self.calibration is None:
            QMessageBox.warning(self, "Global Focus Search", "Load a calibration file first.")
            return
        if self.bridge.is_busy():
            QMessageBox.warning(self, "Global Focus Search", "Another hardware operation is already in progress.")
            return

        if self.canvas.focus_point_items:
            items = list(self.canvas.focus_point_items)
        else:
            if not self.canvas.global_focus_point_items:
                self._on_place_global_focus_points_clicked()
            items = list(self.canvas.global_focus_point_items)
            if not items:
                return  # nothing painted yet - _on_place_global_focus_points_clicked already warned

        self._global_auto_fit_after_search = True
        self._run_global_focus_search(items)

    def _on_place_global_focus_points_clicked(self):
        sections = list(self.canvas.painted)
        if not sections:
            QMessageBox.information(self, "Global Focus Search", "Paint an area first.")
            return

        interior_sections = clustering.sections_away_from_edge(sections)
        candidates = [s for s in sections if s in interior_sections] or sections

        self.canvas.clear_global_focus_points()
        self.global_focus_points = []
        self.global_outlier_sections = set()
        self.global_inclusion_override = {}
        self.global_plane = None
        self._global_fit_point_snapshot = {}

        num_points = self.global_focus_points_spin.value()
        for row, col in clustering.place_focus_points(candidates, num_points):
            self.canvas.add_global_focus_point(row, col)

        self.status_label.setText(
            f"Placed {len(self.canvas.global_focus_point_items)} global focus point(s) across the whole sample."
        )

    def _on_run_global_focus_search_clicked(self):
        """Manual 'Run Global Focus Search' button - always operates on the
        dedicated global point set (Place Global Focus Points), unlike the
        one-click 'Run Global Focus Plane Fitting' button above, which prefers
        reusing region points when they exist. Use this one when you
        specifically want a standalone global sweep instead."""
        if self.calibration is None:
            QMessageBox.warning(self, "Global Focus Search", "Load a calibration file first.")
            return
        if self.bridge.is_busy():
            QMessageBox.warning(self, "Global Focus Search", "Another hardware operation is already in progress.")
            return
        if not self.canvas.global_focus_point_items:
            QMessageBox.information(self, "Global Focus Search", "Place global focus points first.")
            return
        self._run_global_focus_search(list(self.canvas.global_focus_point_items))

    def _run_global_focus_search(self, items: list):
        """Runs autofocus over `items` (any mix of region-owned and/or global
        FocusPointItems), then opens the review dialog to confirm them - shared
        by the manual 'Run Global Focus Search' button and the one-click 'Run
        Global Focus Plane Fitting' button, which differ only in which items
        they pass in.

        `items` is sorted by region number first (region-owned points last-
        placed by Compile Regions are already in that order, but this stays
        correct even for a caller that isn't) so the hardware collects focus
        points region by region, in ascending region order; any dedicated
        global points (region_id is None) are collected last."""
        items = sorted(items, key=lambda it: (it.region_id is None, it.region_id))
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
            # longer necessarily apply to the same points.
            self.global_inclusion_override = {}
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
            self._on_fit_global_plane_clicked()

    def _apply_global_search_results(self, items: list, dialog: FocusReviewDialog):
        """Common tail of a confirmed Global Focus Search run, whatever its
        point set was - already-placed region focus points reused by
        _on_run_global_focus_plane_fitting_clicked, or a dedicated whole-sample
        set from Place Global Focus Points. Matches each of dialog.fits back to
        the marker it came from by object identity (the same FocusFit set on
        item.fit when its sweep completed - see _on_global_af_point_fitted),
        records its confirmed Z there, and:
        - for any region-owned point, feeds that region's own confirmed points
          straight back into region_focus_points - so Fit Region Plane can use
          them right away, without a separate per-region autofocus pass just to
          populate that dict - and clears its stale plane/manual overrides,
          same as a normal per-region confirmation would.
        - builds self.global_focus_points as the full combined set (every
          region's points together, plus any dedicated global ones) for
          _on_fit_global_plane_clicked's RANSAC fit to run over."""
        fit_id_to_item = {id(it.fit): it for it in items if it.fit is not None}
        by_region: dict[int | None, list[tuple[float, float, float]]] = {}
        for i, fit in enumerate(dialog.fits):
            item = fit_id_to_item.get(id(fit))
            z = dialog.confirmed_z.get(i)
            if item is None or z is None:
                continue
            item.z = z
            by_region.setdefault(item.region_id, []).append((fit.row, fit.col, z))

        for region_id, points in by_region.items():
            if region_id is None:
                continue
            self.region_focus_points[region_id] = points
            self.region_inclusion_override[region_id] = {}
            self.region_planes.pop(region_id, None)

        self.global_focus_points = [pt for points in by_region.values() for pt in points]

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

    def _gather_global_focus_points(self) -> list[tuple[int | None, float, float, float]]:
        """Every point currently available to fit a whole-sample plane over -
        (origin_region_id_or_None, row, col, z) - gathered FRESH each time Fit
        Global RANSAC Plane runs, rather than relying on a snapshot that only a
        Global Focus Search run populates: every compiled region's own
        confirmed focus points count too, however they got confirmed (normal
        per-region autofocus, or reused via Run Global Focus Plane Fitting) -
        not just points from a standalone global sweep. This is what makes
        pressing Fit Global RANSAC Plane directly, after just running the
        regions' own autofocus normally, actually do something."""
        points: list[tuple[int | None, float, float, float]] = []
        covered_cells: set[tuple[int, int]] = set()
        for region_id, region_points in self.region_focus_points.items():
            for row, col, z in region_points:
                points.append((region_id, row, col, z))
                covered_cells.add((int(round(row)), int(round(col))))

        for item in self.canvas.global_focus_point_items:
            if item.fit is None or item.z is None:
                continue
            cell = item.grid_cell()
            if cell in covered_cells:
                continue  # already covered by a region's own confirmed point at this cell
            points.append((None, item.fit.row, item.fit.col, item.z))
            covered_cells.add(cell)

        return points

    def _is_manually_force_excluded(self, region_id: int | None, grid_cell: tuple[int, int]) -> bool:
        """Unlike _is_region_point_excluded/_is_global_point_excluded (which
        also fold in the automatic RANSAC outlier flag), this checks ONLY an
        explicit manual override - used to decide what's fed into a FRESH
        RANSAC fit itself, so a point a PRIOR fit flagged as an outlier still
        gets reconsidered each time rather than being permanently locked out."""
        override = (
            self.global_inclusion_override.get(grid_cell) if region_id is None
            else self.region_inclusion_override.get(region_id, {}).get(grid_cell)
        )
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

        # A manually force-excluded point (from either a region's or the
        # global plane review dialog) is kept out of the RANSAC input
        # entirely, not just hidden from the result - it shouldn't get to skew
        # the fitted plane's math either. Everything else - including a point
        # a PRIOR fit flagged as an outlier - stays in the input, so RANSAC
        # gets to reconsider it fresh each time;
        # _is_region_point_excluded/_is_global_point_excluded (used below, and
        # for canvas visuals) are what fold that flag back in afterward.
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

        # Fit Region Plane already fills section_z (the actual Z a real Scan
        # Region submits) from its own per-region plane - do the same here from
        # the global one, over every currently compiled section (or every
        # painted one, if regions haven't been compiled yet), so this plane's
        # result is actually usable for scanning and not just an outlier
        # report. A later per-region Fit Region Plane still overwrites its own
        # region's sections with that more locally-fit result, same as running
        # it twice always would.
        sections_to_fill = list(self.canvas.region_of.keys()) or list(self.canvas.painted)
        if sections_to_fill:
            self.section_z.update(plane.fill_sections(sections_to_fill))

        self._refresh_focus_point_exclusion_visuals()

        residuals = plane.residuals()
        excluded_count = sum(
            1 for region_id, row, col, _z in gathered
            if (
                self._is_global_point_excluded((int(round(row)), int(round(col)))) if region_id is None
                else self._is_region_point_excluded(region_id, (int(round(row)), int(round(col))))
            )
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
        every other section's Z (including one a later Fit Region Plane already
        refined) untouched."""
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

    def _on_clear_global_focus_points_clicked(self):
        self.canvas.clear_global_focus_points()
        self.global_focus_points = []
        self.global_outlier_sections = set()
        self.global_inclusion_override = {}
        self.global_plane = None
        self._global_fit_point_snapshot = {}
        self.status_label.setText("Cleared global focus points.")

    # ---- shared exclusion logic (Global Focus Search RANSAC flag + manual override) ----
    def _focus_points_excluding_flagged(self) -> dict[int, list[tuple[float, float, float]]]:
        """self.region_focus_points, minus any point _is_region_point_excluded currently
        considers excluded (a manual shift-click override, or - if the checkbox is on - a
        Global Focus Search RANSAC outlier flag). Same predicate _fit_plane_for_region
        already applies before fitting a region's plane; scan_path_export.build_scan_path/
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

    def _is_global_point_excluded(self, grid_cell: tuple[int, int]) -> bool:
        override = self.global_inclusion_override.get(grid_cell)
        if override is not None:
            return not override
        return grid_cell in self.global_outlier_sections

    def _refresh_focus_point_exclusion_visuals(self):
        for item in self.canvas.global_focus_point_items:
            item.set_excluded(self._is_global_point_excluded(item.grid_cell()))
        for item in self.canvas.focus_point_items:
            item.set_excluded(self._is_region_point_excluded(item.region_id, item.grid_cell()))

    # ---- Plane fit review (list-based, styled like FocusReviewDialog) ----
    def _on_focus_point_shift_clicked(self, item):
        """Shift+click a marker - opens the plane review dialog scoped to that
        point's own group (its region's points, or the global set for a global
        point), pre-selected to it. See canvas_view.SectionCanvas.
        focusPointShiftClicked and plane_fit_review_dialog.PlaneFitReviewDialog."""
        if item.region_id is None:
            group = list(self.canvas.global_focus_point_items)
            title = "Review Plane Fit - Global Focus Search"
        else:
            group = [it for it in self.canvas.focus_point_items if it.region_id == item.region_id]
            title = f"Review Plane Fit - Region {item.region_id}"
        self._open_plane_review_dialog(group, title, preselect=item)

    def _on_review_region_plane_fit_clicked(self):
        region_id = self.region_id_spin.value()
        group = [it for it in self.canvas.focus_point_items if it.region_id == region_id]
        self._open_plane_review_dialog(group, f"Review Plane Fit - Region {region_id}")

    def _on_review_global_plane_fit_clicked(self):
        # _global_af_items is whatever point set the last Global Focus Search
        # actually ran on - region points reused by the one-click button, or a
        # dedicated global set - which is exactly what the global RANSAC plane
        # was fit over. Falls back to a freshly-placed-but-not-yet-searched
        # global set so it's still something to look at (just with nothing to
        # plot per point yet).
        group = self._global_af_items or list(self.canvas.global_focus_point_items)
        group = sorted(group, key=lambda it: (it.region_id is None, it.region_id))
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
        is_global = item.region_id is None
        grid_cell = item.grid_cell()
        excluded = (
            self._is_global_point_excluded(grid_cell) if is_global
            else self._is_region_point_excluded(item.region_id, grid_cell)
        )
        row, col = item.section()
        region_tag = "global" if is_global else f"R{item.region_id}"
        label = f"[{'EXCL' if excluded else 'OK'}] {region_tag} ({row:.1f}, {col:.1f})"

        if item.fit is None:
            return {
                "label": label, "excluded": excluded, "fit": None,
                "unavailable": "This point hasn't completed an autofocus sweep yet.",
            }

        # The plane actually in effect for this point's section right now - its
        # own region's last Fit Region Plane result if it has one, otherwise
        # (same fallback _on_fit_global_plane_clicked itself uses when filling
        # section_z) the global RANSAC plane, since that may be what actually
        # last set this section's Z if Fit Region Plane was never separately
        # run for it. A global point always uses the global plane. None if
        # neither has ever been fit yet (or the region one was invalidated by a
        # later reconfirm).
        plane = None if is_global else self.region_planes.get(item.region_id)
        if plane is None:
            plane = self.global_plane
        plane_z = plane.z_at(item.fit.row, item.fit.col) if plane is not None else None
        return {"label": label, "excluded": excluded, "fit": item.fit, "z": item.z, "plane_z": plane_z}

    def _on_plane_review_toggle(self, item):
        grid_cell = item.grid_cell()
        is_global = item.region_id is None
        currently_excluded = (
            self._is_global_point_excluded(grid_cell) if is_global
            else self._is_region_point_excluded(item.region_id, grid_cell)
        )
        # Flips the effective state: the dict stores "force included" (True) /
        # "force excluded" (False), so the new override is just the OLD
        # excluded flag itself (excluded=True -> force include=True, and vice
        # versa).
        override_value = currently_excluded
        if is_global:
            self.global_inclusion_override[grid_cell] = override_value
        else:
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
            self.bridge.is_busy() or self._af_sequence_active or self._global_af_sequence_active
            or self._redo_af_sequence_active
            or self._review_dialog is not None or self._global_review_dialog is not None
            or self._redo_review_dialog is not None
        )

    def _on_focus_point_add_requested(self, region_id, row: float, col: float):
        """Double-click on empty canvas - see canvas_view.SectionCanvas.
        mouseDoubleClickEvent. region_id is None for a section not (yet)
        assigned to a compiled region, which adds a global point instead."""
        if self._focus_points_locked():
            QMessageBox.warning(
                self, "Add Focus Point",
                "Finish or close out the current autofocus/review operation before adding a focus point.",
            )
            return

        if region_id is None:
            self.canvas.add_global_focus_point(row, col)
            self.status_label.setText(
                f"Added global focus point at ({row:.1f}, {col:.1f}) - run Global Focus Search to focus it."
            )
        else:
            self.canvas.add_focus_point(region_id, row, col)
            self.status_label.setText(
                f"Added a focus point to region {region_id} at ({row:.1f}, {col:.1f}) - "
                f"run autofocus for the region to focus it."
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
        is_global = item.region_id is None
        region_id = item.region_id
        self.canvas.remove_focus_point(item)

        if is_global:
            self.global_focus_points = [
                (r, c, z) for r, c, z in self.global_focus_points if (int(round(r)), int(round(c))) != grid_cell
            ]
            self.global_inclusion_override.pop(grid_cell, None)
            label = "Global"
        else:
            if region_id in self.region_focus_points:
                self.region_focus_points[region_id] = [
                    (r, c, z) for r, c, z in self.region_focus_points[region_id]
                    if (int(round(r)), int(round(c))) != grid_cell
                ]
            self.region_inclusion_override.get(region_id, {}).pop(grid_cell, None)
            label = f"Region {region_id}"

        self.status_label.setText(f"{label} focus point at {grid_cell} removed.")

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

        # Resume support: if this region already has a Scan_data folder in the
        # active aggregate batch (from a prior session that got interrupted by
        # a hardware failure - see _apply_loaded_session/_restore_region_status),
        # drop whichever sections already have their NR image on disk and only
        # resubmit what's actually missing. The new (smaller) run folder still
        # archives into that same Scan_data/ folder alongside the earlier one -
        # scan_finalize.finalize_aggregate already merges every run folder
        # found there, so nothing else needs to change for that to work.
        num_already_done = 0
        if self._aggregate_root is not None and self.calibration is not None:
            scan_data_dir = os.path.join(self._aggregate_root, f"Region_{region_id}", "Scan_data")
            done_master = session_state.completed_master_sections(scan_data_dir)
            if done_master:
                remaining = [
                    (row, col, z) for row, col, z in sections_with_z
                    if (int(round(self.calibration.offset_row + row)), int(round(self.calibration.offset_col + col)))
                    not in done_master
                ]
                num_already_done = len(sections_with_z) - len(remaining)
                sections_with_z = remaining

        if num_already_done and not sections_with_z:
            self.canvas.set_region_status(region_id, "scanned")
            self.status_label.setText(
                f"Region {region_id}: already fully scanned in this aggregate batch - nothing to resume."
            )
            return False

        if confirm:
            resume_note = (
                f"\n\n{num_already_done} of {num_already_done + len(sections_with_z)} section(s) were already "
                f"scanned in a previous session - only the remaining {len(sections_with_z)} will be scanned now."
                if num_already_done else ""
            )
            reply = QMessageBox.question(
                self, "Scan Region",
                f"Scan region {region_id} now? This submits a real {len(sections_with_z)}-section scan to the "
                f"controller - the stage will move for real.\n\n"
                f"Note: this scan is open-loop (drives straight to the Fit Region Plane Z, no live refocus), "
                f"unlike DOVER_UI's own Image-Path Scan button which live-autofocuses during the scan - so "
                f"results can look softer if the plane fit didn't perfectly capture the tissue's tilt/drift."
                f"{resume_note}",
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
        # offset RegionScanWorker itself applies internally. Kept as a LIST in
        # the exact order sections_with_z (this same scan's own serpentine
        # path order) gives them - see _RECON_GAP_TIMEOUT_S's docstring for why
        # reconstruction is expected to finish them in this same order.
        master_sections_ordered = [
            (int(round(self.calibration.offset_row + row)), int(round(self.calibration.offset_col + col)))
            for row, col, _z in sections_with_z
        ]
        tracker = {
            "region_id": region_id, "remaining": master_sections_ordered, "folders": None,
            "ready": False, "warned": False,
        }
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
        self.canvas.set_region_status(region_id, "scanned")
        self._scanning_region_id = None

        tracker = self._active_scan_tracker
        self._active_scan_tracker = None
        waiting_on_recon = False
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
                    # Only sets it if a live arrival (see _on_section_reconstructing)
                    # didn't already, for a section that finished reconstructing
                    # during the scan itself - that's still a more accurate baseline
                    # than "now" for the gap clock below.
                    tracker.setdefault("last_arrival", time.monotonic())
                    timer = QTimer(self)
                    timer.timeout.connect(lambda t=tracker: self._recheck_tracker_files(t))
                    tracker["timer"] = timer
                    timer.start(_RECON_POLL_INTERVAL_MS)
                    if self._auto_pipeline_active:
                        # Hold the pipeline here - see _auto_pipeline_wait_tracker's
                        # docstring for why starting the next region's hardware ops
                        # before this region's images finish reconstructing is
                        # unsafe. _maybe_archive_tracker (called just below, and
                        # again from _on_section_reconstructing/
                        # _recheck_tracker_files as more sections finish) is what
                        # actually clears this and advances, once tracker["remaining"]
                        # is finally empty - which can happen synchronously in the
                        # call just below, if reconstruction already finished for
                        # every section during the scan itself.
                        self._auto_pipeline_wait_tracker = tracker
                        waiting_on_recon = True
                    self._maybe_archive_tracker(tracker)
                else:
                    self._scan_recon_trackers.remove(tracker)
            else:
                self._scan_recon_trackers.remove(tracker)  # no aggregate active - nothing to archive

        if waiting_on_recon:
            # _maybe_archive_tracker just above may already have resolved this
            # (and advanced the pipeline) synchronously if every section had
            # already finished reconstructing - only report "waiting" if it's
            # actually still waiting.
            if self._auto_pipeline_wait_tracker is tracker:
                self.status_label.setText(
                    f"Region {region_id}: scan complete - waiting for its images to finish reconstructing "
                    f"before starting the next region (avoids a hardware conflict)..."
                )
        else:
            self.status_label.setText(f"Region {region_id}: scan complete.")
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

        if self._auto_pipeline_wait_tracker is tracker:
            # This region's images are now fully reconstructed - safe to start
            # the next region's autofocus/confirmation captures. See
            # _auto_pipeline_wait_tracker's docstring.
            self._auto_pipeline_wait_tracker = None
            if self._auto_pipeline_active:
                self._advance_auto_pipeline()

    def _recheck_tracker_files(self, tracker: dict):
        """Periodic fallback for a region scan's archive-wait: re-checks the
        actual NR file on disk, in case a reconstruction-off broadcast (see
        _on_section_reconstructing) was ever missed. tracker["remaining"] is
        kept in the exact order this region was scanned, and reconstruction is
        expected to finish sections in that same order - so it's enough to
        check from the front and stop at the first one not there yet, rather
        than testing every remaining section on every poll.

        Sections are expected no more than _RECON_GAP_TIMEOUT_S apart; once
        that long has passed since the last one arrived (live or found here),
        conclude no more are coming rather than wait on a fixed total budget
        that can't adapt to how fast reconstruction actually is - and, if Auto
        Run All Regions is holding on this tracker, release it so the next
        region isn't blocked forever. The tracker itself is left running (still
        polling, still eligible to archive) in case a genuinely late straggler
        still shows up - this only gives up on WAITING for it."""
        if tracker not in self._scan_recon_trackers or not tracker["ready"]:
            timer = tracker.get("timer")
            if timer is not None:
                timer.stop()
            return

        while tracker["remaining"]:
            master_row, master_col = tracker["remaining"][0]
            filename = f"s-{master_row}-{master_col}_nr_float32.tif"
            if not any(os.path.isfile(os.path.join(folder, "NR", filename)) for folder in tracker["folders"]):
                break
            tracker["remaining"].pop(0)
            tracker["last_arrival"] = time.monotonic()
            tracker["warned"] = False
            self._set_section_activity(master_row, master_col, "reconstructed")

        if not tracker["remaining"]:
            self._maybe_archive_tracker(tracker)
            return

        gap = time.monotonic() - tracker["last_arrival"]
        if gap <= _RECON_GAP_TIMEOUT_S:
            return

        if not tracker["warned"]:
            tracker["warned"] = True
            blocked_note = (
                " Auto Run All Regions is proceeding to the next region rather than wait indefinitely."
                if self._auto_pipeline_wait_tracker is tracker else ""
            )
            self.status_label.setText(
                f"Region {tracker['region_id']}: no new reconstructed image in over "
                f"{_RECON_GAP_TIMEOUT_S} s ({len(tracker['remaining'])} section(s) still unconfirmed) - "
                f"assuming no more are coming.{blocked_note}"
            )

        if self._auto_pipeline_wait_tracker is tracker:
            self._auto_pipeline_wait_tracker = None
            if self._auto_pipeline_active:
                self._advance_auto_pipeline()

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
            self._save_session()

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
            f"focus points, then Run Autofocus for Region to focus and confirm them."
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
            if self._aggregate_root is not None:
                for focus_index, captures in dialog.captures_by_index().items():
                    if captures:
                        scan_archive.archive_focus_point(self._aggregate_root, "Redo", focus_index, captures)
                if self._redo_af_log_path is not None:
                    scan_archive.archive_autofocus_log(self._aggregate_root, "Redo", self._redo_af_log_path)
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
        self.status_label.setText(f"Scanning {len(sections_with_z)} redo section(s)...")
        self._scanning_region_id = "Redo"
        self._scan_existing_run_folders = confirmation_scan.list_run_folders()

        # Tracked from scan start, same convention _start_region_scan uses for a
        # real region scan - see its own tracker comment for why (reconstruction
        # for early sections can finish well before the whole path does).
        master_sections_ordered = [
            (int(round(self.calibration.offset_row + row)), int(round(self.calibration.offset_col + col)))
            for row, col in ordered_sections
        ]
        tracker = {
            "region_id": "Redo", "remaining": master_sections_ordered, "folders": None,
            "ready": False, "warned": False,
        }
        self._scan_recon_trackers.append(tracker)
        self._active_scan_tracker = tracker

        worker = RegionScanWorker(self.bridge, self.calibration, sections_with_z)
        worker.scanFailed.connect(self._on_region_scan_failed)
        worker.scanFinished.connect(self._on_region_scan_finished)
        self._scan_worker = worker  # keep alive for the duration of the scan
        worker.start()

    def closeEvent(self, event):
        self.bridge.shutdown()
        super().closeEvent(event)
