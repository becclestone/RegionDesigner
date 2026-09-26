"""Runs the live per-point autofocus sequence for a region: for each focus point,
a hierarchical coarse -> medium -> fine sweep of absolute-XY cCALCULATE_AUTOFOCUS_MSG
calls, strictly one at a time (see controller_bridge.run_autofocus's docstring for
why). Runs on a background thread so the Qt event loop never blocks on the
(synchronous, hardware-bound) ControllerBridge calls.

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
        self.stage_config = _load_stage_config()

    def start(self):
        Thread(target=self._run, daemon=True).start()

    def _run(self):
        total = len(self.points)
        z_center = self.z_start
        for index, (row, col) in enumerate(self.points):
            self.pointStarted.emit(index, total)
            try:
                x, y = self.calibration.section_to_absolute_xy(row, col)

                if index == 0:
                    z_center = self._run_sweep(x, y, z_center, self.stage_config["coarse"])

                z_center = self._run_sweep(x, y, z_center, self.stage_config["medium"])
                fit = self._run_fine(x, y, row, col, z_center)
                z_center = fit.z_opt if fit.z_opt is not None else fit.max_loc

                self.fits.append(fit)
                self.pointFitted.emit(index, fit)
            except Exception as e:
                self.pointFailed.emit(index, str(e))

        focus_fitting.finalize_region(self.fits)
        self.sequenceFinished.emit()

    def _run_sweep(self, x: float, y: float, center: float, stage: dict) -> float:
        """Runs one coarse/medium sweep and returns its raw max-sharpness location
        (no curve fit - that's reserved for the fine stage)."""
        z_step = stage["z_step"]
        num_layers = stage["num_layers"]
        z_start = _centered_z_start(center, z_step, num_layers)
        metric_values = self.bridge.run_autofocus(x, y, z_start, z_step, num_layers)
        return _max_sharpness_z(z_start, z_step, metric_values)

    def _run_fine(self, x: float, y: float, row: float, col: float, center: float) -> focus_fitting.FocusFit:
        stage = self.stage_config["fine"]
        z_step = stage["z_step"]
        num_layers = stage["num_layers"]
        z_start = _centered_z_start(center, z_step, num_layers)
        metric_values = self.bridge.run_autofocus(x, y, z_start, z_step, num_layers)
        z_values = [z_start + i * z_step for i in range(len(metric_values))]

        fit = focus_fitting.FocusFit(row, col, z_values, metric_values)
        fit.fit()
        return fit
