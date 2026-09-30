"""Runs 'Send Scan Path to Controller': submits an already-built San_Path_Planning-format
scan path (scan_path_export.build_scan_path's [row, col, donor_idx, region] rows, local/
unshifted section coordinates) as one real CLOSED-LOOP Plane Path scan via
ControllerBridge.run_plane_path_scan - the same mechanism DOVER_UI's own Scan button and
Load Plane Path use (cIS_LUCAS_PATH: False), NOT RegionScanWorker's open-loop Lucas path
(region_scan.py). donor_idx marks which rows the controller live-autofocuses during the
scan itself versus which borrow that measurement - the controller does this propagation
on its own; there's no z in these rows for this worker to compute at all.

Per-section progress is NOT available from this worker's own signals (cPATH_UPDATE_MSG
fires once for the whole path, at the end - see run_plane_path_scan's docstring); callers
wanting a running count should watch ControllerBridge's existing sectionScanning signal
instead, which fires per-section the same way for this path as it does for a Lucas one.
"""
from threading import Thread

from PySide6.QtCore import QObject, Signal


class PlanePathScanWorker(QObject):
    scanFailed = Signal(str)  # error message
    scanFinished = Signal()   # only emitted on success - see scanFailed for errors

    def __init__(self, bridge, calibration, rows: list[tuple[int, int, int, int]]):
        """rows: [(local_row, local_col, donor_idx, region), ...] - local (unshifted)
        section coordinates, same convention as scan_path_export.build_scan_path's
        output - this worker adds the calibration's own offset_row/offset_col to get
        the FINAL master-grid row/col run_plane_path_scan needs (donor_idx and region
        are array/label values, not coordinates, so they pass through unchanged),
        same as RegionScanWorker does for its own Lucas path."""
        super().__init__()
        self.bridge = bridge
        self.calibration = calibration
        self.rows = rows

    def start(self):
        Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            # Safety net: re-assert the operator's actual calibration before a real
            # scan, same reasoning as RegionScanWorker._run.
            self.bridge.set_anchor(**self.calibration.anchor_payload())

            master_rows = [
                (
                    int(round(self.calibration.offset_row + row)),
                    int(round(self.calibration.offset_col + col)),
                    donor_idx,
                    region,
                )
                for row, col, donor_idx, region in self.rows
            ]
            self.bridge.run_plane_path_scan(master_rows)
        except Exception as e:
            self.scanFailed.emit(str(e))
            return
        self.scanFinished.emit()
