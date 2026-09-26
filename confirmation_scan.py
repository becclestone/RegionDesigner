"""Runs the 'confirm via a real scan' step: single-section Lucas-path captures at
chosen-Z, chosen-Z-1um and chosen-Z+1um, scored by 99th-percentile contrast on each
capture's NR reconstruction, for the user to visually compare and pick from.

There's no standalone controller command for "capture + view at one forced Z" -
confirmed by research this session that FocusCalibrationTask does pure motion (no
capture at all) and AutoFocusTask's own capture is a decimated 1D trace, not a
viewable reconstruction. So this reuses the real scan pipeline instead, via
ControllerBridge.run_single_section_scan - the exact same mechanism a normal scan
uses, just for one section at a time.
"""
import os
import time
from dataclasses import dataclass
from threading import Thread

import numpy as np
import tifffile
from PySide6.QtCore import QObject, Signal

IMAGES_ROOT = os.path.expanduser("~/Development/ILLUMISONICS/Gander/IMAGES")
_MICRON_MM = 0.001


@dataclass
class ScanCapture:
    z: float
    image_path: str
    contrast_score: float


def _list_run_folders() -> set:
    if not os.path.isdir(IMAGES_ROOT):
        return set()
    return {name for name in os.listdir(IMAGES_ROOT) if os.path.isdir(os.path.join(IMAGES_ROOT, name))}


def _wait_for_new_run_folder(existing: set, timeout: float = 30.0, poll_interval: float = 0.5) -> str:
    """Each scan run gets its own timestamped folder (verified against a real scan
    example), so the run just triggered is whichever folder wasn't there before."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        new_folders = _list_run_folders() - existing
        if new_folders:
            return os.path.join(IMAGES_ROOT, max(new_folders))
        time.sleep(poll_interval)
    raise TimeoutError("Scan completed but no new output folder appeared under IMAGES_ROOT.")


def score_nr_image(run_folder: str, row: int, col: int) -> tuple:
    image_path = os.path.join(run_folder, "NR", f"s-{row}-{col}_nr_float32.tif")
    image = tifffile.imread(image_path)
    return image_path, float(np.percentile(image, 99))


class ConfirmationScanWorker(QObject):
    captureReady = Signal(object)       # ScanCapture
    captureFailed = Signal(float, str)  # z, error message
    sequenceFinished = Signal()

    def __init__(self, bridge, row: int, col: int, chosen_z: float):
        super().__init__()
        self.bridge = bridge
        self.row = row
        self.col = col
        self.z_values = [chosen_z - _MICRON_MM, chosen_z, chosen_z + _MICRON_MM]

    def start(self):
        Thread(target=self._run, daemon=True).start()

    def _run(self):
        for z in self.z_values:
            try:
                existing = _list_run_folders()
                self.bridge.run_single_section_scan(self.row, self.col, z)
                run_folder = _wait_for_new_run_folder(existing)
                image_path, score = score_nr_image(run_folder, self.row, self.col)
                self.captureReady.emit(ScanCapture(z=z, image_path=image_path, contrast_score=score))
            except Exception as e:
                self.captureFailed.emit(z, str(e))
        self.sequenceFinished.emit()
