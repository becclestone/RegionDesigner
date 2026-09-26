"""Runs the live per-point autofocus sequence for a region: one absolute-XY
cCALCULATE_AUTOFOCUS_MSG per focus point, strictly one at a time (see
controller_bridge.run_autofocus's docstring for why), each fitted immediately via
focus_fitting.FocusFit. Runs on a background thread so the Qt event loop never
blocks on the (synchronous, hardware-bound) ControllerBridge calls.
"""
from threading import Thread

from PySide6.QtCore import QObject, Signal

import focus_fitting


class AutofocusSequenceWorker(QObject):
    pointStarted = Signal(int, int)             # index, total
    pointFitted = Signal(int, object)           # index, FocusFit
    pointFailed = Signal(int, str)              # index, error message
    sequenceFinished = Signal()

    def __init__(self, bridge, calibration, points: list[tuple[float, float]],
                 z_start: float, z_step: float, num_layers: int):
        super().__init__()
        self.bridge = bridge
        self.calibration = calibration
        self.points = points
        self.z_start = z_start
        self.z_step = z_step
        self.num_layers = num_layers
        self.fits: list[focus_fitting.FocusFit] = []

    def start(self):
        Thread(target=self._run, daemon=True).start()

    def _run(self):
        total = len(self.points)
        for index, (row, col) in enumerate(self.points):
            self.pointStarted.emit(index, total)
            try:
                x, y = self.calibration.section_to_absolute_xy(row, col)
                metric_values = self.bridge.run_autofocus(x, y, self.z_start, self.z_step, self.num_layers)
                z_values = [self.z_start + i * self.z_step for i in range(len(metric_values))]

                fit = focus_fitting.FocusFit(row, col, z_values, metric_values)
                fit.fit()
                self.fits.append(fit)
                self.pointFitted.emit(index, fit)
            except Exception as e:
                self.pointFailed.emit(index, str(e))

        focus_fitting.finalize_region(self.fits)
        self.sequenceFinished.emit()
