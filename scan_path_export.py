"""Builds a San_Path_Planning-format scan path file from a RegionDesigner session's
regions/focus points, for Dover UI to load as a Plane Path (.pp) and execute for real.

See San_Path_Planning/region_planner_V2.py (plan_region_paths/export_path) for the
reference format this matches field-for-field: a flat JSON list of
[row, col, donor_idx, is_focus, region] rows, region 1-based. donor_idx is what Dover
UI actually keys off of (DOVER_UI/Windows/demo_control_a.py's load_plane_path treats a
row as a donor/focus location iff its donor_idx equals its own position in the list) -
is_focus is always written False, same as region_planner_V2.export_path, since real
focus confirmation happens when Dover UI itself runs the path.

Unlike region_planner_V2.plan_region_paths, regions and focus points are NOT
re-clustered here - RegionDesigner sets both manually (paint + Compile Regions, then
autofocus confirmation), so this just reads self.canvas.region_of/self.region_focus_points
as they already stand.
"""
import json

import region_clustering as clustering

Section = tuple[int, int]

# Same row/col distance weighting region_planner_V2.plan_region_paths uses to pick a
# non-focus section's nearest focus point within its region.
_ROW_WEIGHT = 500
_COL_WEIGHT = 200


def _nearest_focus_index(section: Section, focus_indices: dict[Section, int]) -> int:
    row, col = section
    best_index = None
    best_dist = None
    for (f_row, f_col), index in focus_indices.items():
        dist = ((f_row - row) * _ROW_WEIGHT) ** 2 + ((f_col - col) * _COL_WEIGHT) ** 2
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_index = index
    return best_index


def build_scan_path(
    region_of: dict[Section, int],
    region_focus_points: dict[int, list[tuple[float, float, float]]],
) -> list[list[int]]:
    """Returns rows of [row, col, donor_idx, region] (region 1-based), ordered region by
    region (ascending region_id - already Compile Regions' own serpentine visiting order,
    same as main_window's "region_id already runs 0..N-1 in the clustering's serpentine
    scan order") and, within each region, in clustering.serpentine_order - the same order
    RegionDesigner's own Scan Region pipeline visits sections in - so donor_idx lines up
    with the order Dover UI will actually move through the path.

    Raises ValueError if a compiled region has no confirmed focus points to donate from."""
    rows: list[list[int]] = []
    for region_id in sorted(set(region_of.values())):
        region_sections = [s for s, rid in region_of.items() if rid == region_id]
        ordered_sections = clustering.serpentine_order(region_sections)

        focus_sections = {
            (int(round(r)), int(round(c))) for r, c, _z in region_focus_points.get(region_id, [])
        }
        if not focus_sections:
            raise ValueError(f"Region {region_id + 1} has no confirmed focus points.")

        offset = len(rows)
        focus_indices = {
            section: offset + i for i, section in enumerate(ordered_sections) if section in focus_sections
        }

        for i, section in enumerate(ordered_sections):
            row, col = section
            donor_idx = focus_indices.get(section)
            if donor_idx is None:
                donor_idx = _nearest_focus_index(section, focus_indices)
            rows.append([row, col, donor_idx, region_id + 1])

    return rows


def export_scan_path(
    out_file_path: str,
    region_of: dict[Section, int],
    region_focus_points: dict[int, list[tuple[float, float, float]]],
) -> int:
    """Writes region_planner_V2.export_path's exact format (donor_idx-bearing rows with
    is_focus inserted as False). Returns the number of sections written."""
    scan_list = build_scan_path(region_of, region_focus_points)
    for row in scan_list:
        row.insert(3, False)
    with open(out_file_path, "w") as f:
        json.dump(scan_list, f)
    return len(scan_list)
