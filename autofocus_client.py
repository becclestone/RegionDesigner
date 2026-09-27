"""Runs the live per-point autofocus sequence for a region: for each focus point,
a hierarchical coarse -> medium -> fine sweep of row/col-addressed
cCALCULATE_AUTOFOCUS_MSG calls, strictly one at a time (see
controller_bridge.run_autofocus's docstring for why). Runs on a background thread
so the Qt event loop never blocks on the (synchronous, hardware-bound)
ControllerBridge calls.

Since the controller's autofocus command only actually positions correctly on a
whole master-grid row/column (see run_autofocus's docstring), each focus point's
possibly-fractional row/col is handled by temporarily shifting the controller's
anchor (StageCalibration.shifted_anchor_for_focus) so that its *rounded*
row/col lands exactly on the point's true location, running that point's full
coarse/medium/fine sweep, then restoring the real calibration before moving on.

Stage roles:
- coarse: one wide, coarse-step sweep on the region's first point only, to locate
  the tissue surface before anything narrower can be trusted to be centered on it.
- medium: a narrower sweep centered on the previous stage's max-sharpness location,
  run at every point. Also re-centers each point's search on where the *previous
  point* ended up, since neighboring points shouldn't need a fresh coarse search.
- fine: the narrowest, finest-step sweep, centered on medium's result. This is the
  only stage fitted via focus_fitting.FocusFit (surface_calc's curve fit), and its
  fitted z_opt is both the point's reported focus and the center fed to the next
  point's medium stage.

Stage sizes (num_layers, z_step) are loaded from autofocus_config.json so they can
be tuned without a code change; see _DEFAULT_STAGE_CONFIG for the fallback/expected
shape if that file is missing or incomplete.
"""
import json
import os
from threading import Thread

from PySide6.QtCore import QObject, Signal

import focus_fitting

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autofocus_config.json")

_DEFAULT_STAGE_CONFIG = {
    "coarse": {"num_layers": 31, "z_step": 0.005},
    "medium": {"num_layers": 15, "z_step": 0.005},
    "fine": {"num_layers": 29, "z_step": 0.001},
}


def _load_stage_config(path: str = _CONFIG_PATH) -> dict:
    """Falls back to _DEFAULT_STAGE_CONFIG (per-stage) if the file is missing or a
    stage/key is absent from it, so a partial or missing config still runs."""
    try:
        with open(path, "r") as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}
    return {
        stage: {**defaults, **config.get(stage, {})}
        for stage, defaults in _DEFAULT_STAGE_CONFIG.items()
    }


def _centered_z_start(center: float, z_step: float, num_layers: int) -> float:
    return center - (num_layers // 2) * z_step


def _max_sharpness_z(z_start: float, z_step: float, metric_values: list) -> float:
    best_index = max(range(len(metric_values)), key=metric_values.__getitem__)
    return z_start + best_index * z_step


class AutofocusSequenceWorker(QObject):
    pointStarted = Signal(int, int)             # index, total
    pointFitted = Signal(int, object)           # index, FocusFit
    pointFailed = Signal(int, str)              # index, error message
    sequenceFinished = Signal()

    def __init__(self, bridge, calibration, points: list[tuple[float, float]], z_start: float):
        super().__init__()
        self.bridge = bridge
        self.calibration = calibration
        self.points = points
        self.z_start = z_start
        self.fits: list[focus_fitting.FocusFit] = []
        # One dict per point, in point order, for autofocus_log.py - coarse/medium
        # sweep data plus (once finalize_region() has run) the fine stage's
        # to_dict(). Populated even for a point whose sweeps raised partway
        # through (status/error fields), unlike self.fits.
        self.records: list[dict] = []
        self.stage_config = _load_stage_config()

    def start(self):
        Thread(target=self._run, daemon=True).start()

    def _run(self):
        total = len(self.points)
        z_center = self.z_start
        for index, (row, col) in enumerate(self.points):
            self.pointStarted.emit(index, total)
            record = {
                "focus_index": index, "row": row, "col": col,
                "status": "ok", "error": None,
                "coarse": None, "medium": None,
            }
            fit = None
            try:
                anchor, master_row, master_col = self.calibration.shifted_anchor_for_focus(row, col)
                self.bridge.set_anchor(**anchor)
                try:
                    if index == 0:
                        coarse = self._run_sweep(master_row, master_col, z_center, self.stage_config["coarse"])
                        record["coarse"] = coarse
                        z_center = coarse["chosen_z"]

                    medium = self._run_sweep(master_row, master_col, z_center, self.stage_config["medium"])
                    record["medium"] = medium
                    z_center = medium["chosen_z"]

                    fit = self._run_fine(master_row, master_col, row, col, z_center)
                    z_center = fit.z_opt if fit.z_opt is not None else fit.max_loc

                    self.fits.append(fit)
                    self.pointFitted.emit(index, fit)
                finally:
                    # Always restore the real calibration, even if this point's
                    # sweeps failed partway through - a shifted anchor must never
                    # be left active once this point is done with it.
                    self.bridge.set_anchor(**self.calibration.anchor_payload())
            except Exception as e:
                record["status"] = "failed"
                record["error"] = str(e)
                self.pointFailed.emit(index, str(e))
            finally:
                # Stashed as a raw FocusFit for now - fine stage's is_right z_opt
                # isn't resolved until finalize_region() runs below, after every
                # point in the region has been attempted.
                record["_fit"] = fit
                self.records.append(record)

        focus_fitting.finalize_region(self.fits)
        for record in self.records:
            fit = record.pop("_fit")
            record["fine"] = fit.to_dict() if fit is not None else None
        self.sequenceFinished.emit()

    def _run_sweep(self, row: int, col: int, center: float, stage: dict) -> dict:
        """Runs one coarse/medium sweep and returns its raw sweep data plus
        max-sharpness location (no curve fit - that's reserved for the fine
        stage)."""
        z_step = stage["z_step"]
        num_layers = stage["num_layers"]
        z_start = _centered_z_start(center, z_step, num_layers)
        metric_values = list(self.bridge.run_autofocus(row, col, z_start, z_step, num_layers))
        z_values = [z_start + i * z_step for i in range(len(metric_values))]
        chosen_z = _max_sharpness_z(z_start, z_step, metric_values)
        return {"z_values": z_values, "metric_values": metric_values, "chosen_z": chosen_z}

    def _run_fine(self, row: int, col: int, orig_row: float, orig_col: float, center: float) -> focus_fitting.FocusFit:
        stage = self.stage_config["fine"]
        z_step = stage["z_step"]
        num_layers = stage["num_layers"]
        z_start = _centered_z_start(center, z_step, num_layers)
        metric_values = self.bridge.run_autofocus(row, col, z_start, z_step, num_layers)
        z_values = [z_start + i * z_step for i in range(len(metric_values))]

        # orig_row/orig_col (the point's true fractional location) are what get
        # recorded, not the rounded master row/col used only to address the
        # controller - see StageCalibration.shifted_anchor_for_focus.
        fit = focus_fitting.FocusFit(orig_row, orig_col, z_values, metric_values)
        fit.fit()
        return fit
