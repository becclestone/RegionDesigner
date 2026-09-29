"""Persists enough of a RegionDesigner session's design state (painted
sections, region boundaries, confirmed focus points, manual plane-fit
inclusion overrides) into the active aggregate batch folder that "Open
Previous Scan..." can reload it, instead of only repointing where future
archiving/finalizing goes.

Deliberately NOT persisted: region_planes/section_z (recomputed from
region_focus_points + region_inclusion_override via main_window's own
_fit_plane_for_region on load - same inputs always produce the same RANSAC
fit) and region_status (recomputed from what's actually on disk under the
aggregate root - see completed_master_sections - so a stale or missing save
can never claim a region is scanned when its output isn't really there).

Flat lists of tuples are used instead of dicts, since JSON object keys must
be strings and (row, col) / region_id keys would need lossy stringify/parse
round-tripping otherwise.
"""
import glob
import json
import os
import re

SESSION_FILENAME = "session.json"
_SESSION_VERSION = 1

Section = tuple[int, int]

_NR_FILENAME_RE = re.compile(r"^s-(-?\d+)-(-?\d+)_nr_float32\.tif$")


def _session_path(aggregate_root: str) -> str:
    return os.path.join(aggregate_root, SESSION_FILENAME)


def save_session(
    aggregate_root: str,
    *,
    painted: set[Section],
    region_of: dict[Section, int],
    region_focus_points: dict[int, list[tuple[float, float, float]]],
    region_inclusion_override: dict[int, dict[Section, bool]],
    background_image_path: str | None,
) -> None:
    data = {
        "version": _SESSION_VERSION,
        "background_image_path": background_image_path,
        "painted": [[row, col] for row, col in painted],
        "region_of": [[row, col, region_id] for (row, col), region_id in region_of.items()],
        "region_focus_points": [
            [region_id, row, col, z]
            for region_id, points in region_focus_points.items()
            for row, col, z in points
        ],
        "region_inclusion_override": [
            [region_id, row, col, included]
            for region_id, overrides in region_inclusion_override.items()
            for (row, col), included in overrides.items()
        ],
    }
    with open(_session_path(aggregate_root), "w") as f:
        json.dump(data, f, indent=2)


def load_session(aggregate_root: str) -> dict | None:
    """Returns the raw parsed dict (still in flat-list form - see the module
    docstring), or None if this aggregate batch predates this feature (or its
    session.json is otherwise missing). Callers reconstruct their own
    dict/set shapes from the flat lists themselves - this stays a thin,
    format-only layer."""
    path = _session_path(aggregate_root)
    if not os.path.isfile(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def completed_master_sections(scan_data_dir: str) -> set[Section]:
    """Scans every run folder under a region's Scan_data/ directory for
    already-written NR images (s-{row}-{col}_nr_float32.tif, master-grid
    coordinates - same convention _recheck_tracker_files in main_window.py
    already relies on for its own on-disk fallback) and returns the set of
    sections found. This is the single source of truth for "what's already
    scanned" - used both to report a reloaded region's status and to filter
    which sections a resumed 'Scan Region' actually needs to resubmit, so the
    two can never disagree with each other."""
    done: set[Section] = set()
    if not os.path.isdir(scan_data_dir):
        return done
    for nr_path in glob.glob(os.path.join(scan_data_dir, "*", "NR", "s-*_nr_float32.tif")):
        match = _NR_FILENAME_RE.match(os.path.basename(nr_path))
        if match:
            done.add((int(match.group(1)), int(match.group(2))))
    return done
