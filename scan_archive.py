"""Moves finished autofocus-capture and region-scan output folders out of
IMAGES_ROOT into a per-batch aggregate tree, organized by region so a
session's output isn't just a flat pile of timestamped run folders:

    IMAGES_ROOT/
      20260927_143000_aggregate/
        Region_1/
          Focus_1/<run folder>/NR/...
          Focus_2/<run folder>/NR/...
          Scan_data/<run folder>/NR/...
        Region_2/
          ...

A new aggregate folder is only created when the operator explicitly starts one
(main_window's "Start New Aggregate Batch" button) - archiving is a no-op until
then, so nothing moves unless the operator opts in.
"""
import os
import shutil
from datetime import datetime
from threading import Thread

from confirmation_scan import IMAGES_ROOT, ScanCapture

_AGGREGATE_SUFFIX = "_aggregate"
_AGGREGATE_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"


def start_new_aggregate_batch() -> str:
    """Creates and returns a new '<timestamp>_aggregate' folder under
    IMAGES_ROOT. Call once per operator-initiated batch (see main_window's
    toolbar button) - the returned path is what every later archive_* call in
    that batch should be given."""
    name = datetime.now().strftime(_AGGREGATE_TIMESTAMP_FMT) + _AGGREGATE_SUFFIX
    path = os.path.join(IMAGES_ROOT, name)
    os.makedirs(path, exist_ok=True)
    return path


def run_folder_of(image_path: str) -> str:
    """Inverse of confirmation_scan.score_nr_image's path construction:
    IMAGES_ROOT/<run folder>/NR/s-{row}-{col}_nr_float32.tif -> IMAGES_ROOT/<run folder>."""
    return os.path.dirname(os.path.dirname(image_path))


def _move_run_folders(run_folders: set, dest_dir: str) -> None:
    os.makedirs(dest_dir, exist_ok=True)
    for run_folder in run_folders:
        if not os.path.isdir(run_folder):
            continue  # already archived (or never landed) - safe to skip
        dest = os.path.join(dest_dir, os.path.basename(run_folder))
        if os.path.exists(dest):
            continue  # already archived under this exact name - don't clobber
        shutil.move(run_folder, dest)


def archive_focus_point(aggregate_root: str, region_id: int, focus_index: int,
                         captures: list[ScanCapture]) -> None:
    """Moves every run folder behind one focus point's confirmation-scan
    captures into Region_{region_id}/Focus_{focus_index}/ under aggregate_root.
    Runs in a background thread - these are whole-folder moves under
    IMAGES_ROOT, which may be a network path, so this must never block the GUI
    thread."""
    run_folders = {run_folder_of(c.image_path) for c in captures}
    dest_dir = os.path.join(aggregate_root, f"Region_{region_id}", f"Focus_{focus_index}")
    Thread(target=_move_run_folders, args=(run_folders, dest_dir), daemon=True).start()


def archive_region_scan(aggregate_root: str, region_id: int, run_folders: set) -> None:
    """Moves every run folder produced by one region's full scan into
    Region_{region_id}/Scan_data/ under aggregate_root. run_folders should be
    full paths (see main_window._on_region_scan_finished, which diffs
    confirmation_scan.list_run_folders() before/after the scan)."""
    dest_dir = os.path.join(aggregate_root, f"Region_{region_id}", "Scan_data")
    Thread(target=_move_run_folders, args=(run_folders, dest_dir), daemon=True).start()
