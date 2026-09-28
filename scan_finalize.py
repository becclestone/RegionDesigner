"""Consolidates every scanned region's raw output into one merged directory,
plus a synthesized scan_record.json - so a RegionDesigner session (many
per-region real scans, each its own cPATH_MSG call) can be handed to whatever
post-processing pipeline a single continuous DOVER_UI scan's output normally
feeds. See Examples/scan_record.json and dover_ctl2/src/Util/scan_record_utils.cpp
(build_region_path_json) for the schema this matches field-for-field.

RegionDesigner's scan is open-loop (one precomputed plane-fit Z per section,
not live per-section autofocus with donor propagation) - so unlike a real
scan, only the sections that were ACTUALLY measured (a region's confirmed
focus points) are true donors (is_donor=True, with real focus_info/focus_val
from that region's autofocus_log.json). Every other section in the region
borrows its Z from the plane fit and is marked is_donor=False, focus_val=null,
focus_owner pointing at the array index of whichever donor in the merged
scan_record is spatially nearest within the same region.

Deliberately NOT regenerated: the Stitcher's own re-assembled mosaic files
(MLI_Image.ome.tif, sect_map.ome.tif, slide.tif, tag.tif) - producing those
means actually re-running stitching over the combined bounding box, not just
moving/renaming files, so this leaves them out on the assumption that whatever
consumes the merged output re-stitches from the raw per-section tiles plus
scan_record.json itself.
"""
import json
import os
import shutil

import autofocus_log

FINALIZED_DIR_NAME = "Finalized"
SCAN_RECORD_FILENAME = "scan_record.json"

# Per-section raw image subfolders a real scan's run folder holds (see
# scan_archive.py's module docstring / confirmation_scan.py) - copied as-is
# into the merged directory. path.json/stitcher_log.json are merged
# separately below since they're per-section dicts, not folders of files.
_MERGED_IMAGE_SUBDIRS = ("NR", "R1", "R2", "R3", "R4", "SC")
_MERGED_JSON_FILES = ("path.json", "stitcher_log.json")

Section = tuple[int, int]


def _merge_json_dict(src_path: str, dest_path: str) -> None:
    with open(src_path, "r") as f:
        src = json.load(f)
    if os.path.exists(dest_path):
        with open(dest_path, "r") as f:
            dest = json.load(f)
    else:
        dest = {}
    dest.update(src)
    with open(dest_path, "w") as f:
        json.dump(dest, f, indent=2)


def _merge_run_folder_into(run_folder: str, dest_dir: str) -> None:
    """Merges one real scan's run folder into dest_dir. Sections never overlap
    between regions, so image files are just unioned; a re-scanned region's
    later run folder (see finalize_aggregate's sorted order) overwrites an
    earlier one's files for any section they do share."""
    for name in os.listdir(run_folder):
        src = os.path.join(run_folder, name)
        if name in _MERGED_IMAGE_SUBDIRS and os.path.isdir(src):
            dst_dir = os.path.join(dest_dir, name)
            os.makedirs(dst_dir, exist_ok=True)
            for fname in os.listdir(src):
                shutil.copy2(os.path.join(src, fname), os.path.join(dst_dir, fname))
        elif name in _MERGED_JSON_FILES:
            _merge_json_dict(src, os.path.join(dest_dir, name))


def _load_focus_log_points(aggregate_root: str, region_id: int) -> list[dict]:
    """Reads Region_{region_id}/autofocus_log.json (already archived, see
    main_window._on_review_dialog_finished) - or, if the region was confirmed
    before an aggregate batch existed, its not-yet-archived staging copy (see
    autofocus_log.py). Returns [] if neither exists, so a region predating
    this feature (or one whose log failed to write) still finalizes, just
    without real focus_info."""
    archived_path = os.path.join(aggregate_root, f"Region_{region_id}", "autofocus_log.json")
    path = archived_path if os.path.isfile(archived_path) else autofocus_log.staging_path_for_region(region_id)
    if not os.path.isfile(path):
        return []
    with open(path, "r") as f:
        return json.load(f)["points"]


def _focus_info_from_log_point(log_point: dict) -> list[dict]:
    focus_info = []
    for stage_key, focus_type in (("coarse", "c"), ("medium", "m"), ("fine", "f")):
        stage = log_point.get(stage_key)
        if not stage:
            continue
        z_values = stage["z_values"]
        step_z = z_values[1] - z_values[0] if len(z_values) > 1 else 0.0
        focus_info.append({
            "focus_type": focus_type,
            "plot_data": stage["metric_values"],
            "start_z": z_values[0],
            "step_z": step_z,
            "z_planes": len(z_values),
        })
    return focus_info


def _donor_sections_for_region(
    focus_points: list[tuple[float, float, float]], log_points: list[dict]
) -> dict[Section, dict]:
    """focus_points: this region's confirmed (row, col, z) - see
    main_window.region_focus_points. Matches each to its autofocus_log.json
    record by exact (row, col) - both trace back to the same
    AutofocusSequenceWorker point, so the floats are identical, not just
    close. Keyed by the point's nearest whole section, since that's the key
    space region_of/section_z (and so the merged scan_record) use."""
    log_by_rc = {(p["row"], p["col"]): p for p in log_points}
    donors: dict[Section, dict] = {}
    for row, col, z in focus_points:
        section = (int(round(row)), int(round(col)))
        log_point = log_by_rc.get((row, col))
        donors[section] = {
            "confirmed_z": z,
            "focus_info": _focus_info_from_log_point(log_point) if log_point else [],
        }
    return donors


def _nearest_donor_index(section: Section, region_id: int, donor_indices: dict[tuple[int, Section], int]) -> int | None:
    row, col = section
    best_index = None
    best_dist = None
    for (rid, (d_row, d_col)), index in donor_indices.items():
        if rid != region_id:
            continue
        dist = (d_row - row) ** 2 + (d_col - col) ** 2
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_index = index
    return best_index


def finalize_aggregate(
    aggregate_root: str,
    region_of: dict[Section, int],
    section_z: dict[Section, float],
    region_focus_points: dict[int, list[tuple[float, float, float]]],
) -> tuple[str, int]:
    """Builds aggregate_root/Finalized/: merged per-section image tiles,
    path.json/stitcher_log.json, and one scan_record.json spanning every
    region that's actually been scanned (has a Region_N/Scan_data folder) AND
    confirmed (has region_focus_points) - a region missing either is skipped,
    not errored on, so a partial session can still be finalized. Returns
    (the Finalized folder's path, number of sections written)."""
    dest_dir = os.path.join(aggregate_root, FINALIZED_DIR_NAME)
    os.makedirs(dest_dir, exist_ok=True)

    scanned_region_ids = sorted(
        rid for rid in region_focus_points
        if os.path.isdir(os.path.join(aggregate_root, f"Region_{rid}", "Scan_data"))
    )

    # (entry dict, region_id, section) per row, built in region order - a
    # non-donor's focus_owner is resolved in a second pass below, once every
    # donor's final index in this same list is known.
    rows: list[tuple[dict, int, Section]] = []
    donor_indices: dict[tuple[int, Section], int] = {}

    for region_id in scanned_region_ids:
        scan_data_dir = os.path.join(aggregate_root, f"Region_{region_id}", "Scan_data")
        for run_folder_name in sorted(os.listdir(scan_data_dir)):
            run_folder = os.path.join(scan_data_dir, run_folder_name)
            if os.path.isdir(run_folder):
                _merge_run_folder_into(run_folder, dest_dir)

        log_points = _load_focus_log_points(aggregate_root, region_id)
        donors = _donor_sections_for_region(region_focus_points[region_id], log_points)

        region_sections = sorted(s for s, rid in region_of.items() if rid == region_id)
        for section in region_sections:
            if section not in section_z:
                continue  # painted but never got a Z (shouldn't happen once scanned) - skip defensively
            donor = donors.get(section)
            entry = {
                "row": section[0], "col": section[1],
                "region": region_id + 1,  # 1-based, matching a real scan's own convention (see Examples)
                "scan_z": section_z[section],
                "focus_val": donor["confirmed_z"] if donor else None,
                "is_donor": donor is not None,
                "scan_temp": 0.0,
                "focus_temp": 0.0,
                "focus_owner": None,  # resolved below once every row's final index is known
                "focus_info": donor["focus_info"] if donor else [],
            }
            if donor is not None:
                donor_indices[(region_id, section)] = len(rows)
            rows.append((entry, region_id, section))

    for entry, region_id, section in rows:
        if entry["is_donor"]:
            entry["focus_owner"] = donor_indices[(region_id, section)]
        else:
            entry["focus_owner"] = _nearest_donor_index(section, region_id, donor_indices)

    scan_record = [entry for entry, _rid, _section in rows]
    with open(os.path.join(dest_dir, SCAN_RECORD_FILENAME), "w") as f:
        json.dump(scan_record, f, indent=2)

    return dest_dir, len(scan_record)
