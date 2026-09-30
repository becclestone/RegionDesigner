"""Loads a previously finalized scan_record.json (see scan_finalize.py) back into
a region layout + per-section Z, for correcting/redoing specific sections in post
- see main_window's Load Scan Record / redo-scan controls.

This is a one-way IMPORT of the finalized export format, not the same as
session_state.py's own session.json save/reload (which restores full per-region
focus points too, for resuming an interrupted scan). A scan_record.json only ever
carries a final Z per section, not the focus points that produced it - reloading
it is only ever meant to feed the redo workflow (mark bad sections, pick a fresh
focus point, and rescan just those), not to resume/extend the original scan.
"""
import json

Section = tuple[int, int]


def load_scan_record(path: str) -> list[dict]:
    with open(path, "r") as f:
        return json.load(f)


def region_layout_from_scan_record(entries: list[dict]) -> tuple[dict[Section, int], dict[Section, float]]:
    """(region_of, section_z) from a scan_record.json's flat section list.
    Region ids are stored 1-based there (scan_finalize.finalize_aggregate's own
    convention, matching a real scan's) - converted back to the 0-based ids the
    canvas/region_of convention used elsewhere in RegionDesigner expects."""
    region_of: dict[Section, int] = {}
    section_z: dict[Section, float] = {}
    for entry in entries:
        section = (entry["row"], entry["col"])
        region_of[section] = entry["region"] - 1
        section_z[section] = entry["scan_z"]
    return region_of, section_z
