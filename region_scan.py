"""Runs 'Scan Region': submits every one of a region's painted sections, each with
its plane-fitted Z (region_plane_fit.RegionPlaneFit.fill_sections), as one real
multi-section Lucas path scan via ControllerBridge.run_path_scan.

There's no per-section progress message for a multi-entry path - MsgHandler.cpp's
scan_completed() (the only place cPATH_UPDATE_MSG is built) fires exactly once,
after the whole path finishes, confirmed by tracing ProcessingTask.cpp's
operator()/cmf_operator this session - so this worker can only report started/
failed/finished, not per-section progress.

KNOWN TRADEOFF: this is an OPEN-LOOP scan - every section drives straight to its
precomputed plane-fit Z with no live refinement. DOVER_UI's own "Image-Path" Scan
button instead runs CLOSED-LOOP (cIS_LUCAS_PATH: False), live-autofocusing
designated "focus-owner" sections during the scan itself and propagating that
result to neighbors - see run_path_scan's docstring for the full trace. So a
region scanned here can look softer than the same region scanned from DOVER_UI if
the plane fit didn't perfectly capture the tissue's real tilt/drift. Matching
DOVER_UI's closed-loop behavior would mean sending the focus-owner/donor path
structure (section_utilities.py's find_focus_donor) instead of a literal Z per
section - a materially different (and much slower, since it live-autofocuses)
feature, not implemented here by design for now.
"""
from threading import Thread

from PySide6.QtCore import QObject, Signal


class RegionScanWorker(QObject):
    scanFailed = Signal(str)  # error message
    scanFinished = Signal()   # only emitted on success - see scanFailed for errors

    def __init__(self, bridge, calibration, sections: list[tuple[int, int, float]]):
        """sections: [(local_row, local_col, z), ...] - local (unshifted) section
        coordinates, same convention as canvas.region_of/RegionPlaneFit - this
        worker adds the calibration's own offset_row/offset_col to get the FINAL
        master-grid row/col run_path_scan needs (see stage_calibration.py's
        section_to_absolute_xy/shifted_anchor_for_focus, which do the same add)."""
        super().__init__()
        self.bridge = bridge
        self.calibration = calibration
        self.sections = sections

    def start(self):
        Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            # Safety net: a prior autofocus/confirmation-scan run temporarily
            # shifts the controller's anchor and restores it right after, but a
            # real region scan must never run against a shifted anchor - so
            # re-assert the operator's actual calibration here regardless of
            # whatever state the controller was left in (mirrors
            # confirmation_scan.py's own safety net).
            self.bridge.set_anchor(**self.calibration.anchor_payload())

            master_sections = [
                (
                    int(round(self.calibration.offset_row + row)),
                    int(round(self.calibration.offset_col + col)),
                    z,
                )
                for row, col, z in self.sections
            ]
            self.bridge.run_path_scan(master_sections)
        except Exception as e:
            self.scanFailed.emit(str(e))
            return
        self.scanFinished.emit()
