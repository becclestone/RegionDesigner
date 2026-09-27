"""Writes/updates the per-region autofocus JSON log: every focus point's
coarse/medium/fine sweep data (see AutofocusSequenceWorker.records), plus -
once the operator has reviewed the region - the confirmed Z.

Written as soon as a region's autofocus sequence finishes (main_window's
sequenceFinished handler), before the operator has decided whether to accept
or decline the region, so it lives under a staging root rather than directly
in an aggregate batch (which may not exist yet - see scan_archive.py). Once
the region is confirmed, scan_archive.archive_autofocus_log moves this same
file into that batch's Region_{region_id}/ folder, mirroring how
archive_focus_point moves confirmation-scan images on confirm.
"""
import json
import os

from confirmation_scan import IMAGES_ROOT

STAGING_ROOT = os.path.join(IMAGES_ROOT, "_autofocus_staging")


def staging_path_for_region(region_id: int) -> str:
    return os.path.join(STAGING_ROOT, f"Region_{region_id}", "autofocus_log.json")


def write_region_log(region_id: int, records: list[dict]) -> str:
    """Writes this region's raw autofocus records (AutofocusSequenceWorker.records)
    to its staging file, overwriting any previous run's log for the same
    region - a retried region should replace its old log, not accumulate
    alongside it. Returns the path written."""
    points = []
    for record in records:
        point = dict(record)
        point.setdefault("confirmed_z", None)
        point.setdefault("confirmed_source", None)
        points.append(point)

    path = staging_path_for_region(region_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"region_id": region_id, "points": points}, f, indent=2)
    return path


def update_confirmed(path: str, confirmed: dict[int, tuple[float, str]]) -> None:
    """Fills in confirmed_z/confirmed_source for each focus_index in
    `confirmed` ({focus_index: (z, source)}, see
    FocusReviewDialog.confirmed_z_with_source) into an already-written log."""
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        data = json.load(f)
    by_index = {point["focus_index"]: point for point in data["points"]}
    for focus_index, (z, source) in confirmed.items():
        point = by_index.get(focus_index)
        if point is not None:
            point["confirmed_z"] = z
            point["confirmed_source"] = source
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
