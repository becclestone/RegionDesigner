"""Row/col grid <-> pixel coordinate math.

Ported from DOVER_UI/Windows/demo_control_a.py's section_to_grid_rect/grid_point_to_section
so sections painted here land on the exact same physical grid the rest of the
system (and the controller) assumes: col maps to screen X, row maps to screen Y.
Kept free of any GUI-toolkit dependency so it can be reused/tested standalone.
"""
from Constants import project_constants as pc

WIDTH_STEP = pc.cSCALED_SECTION_WIDTH_TO_OVERLAP_PIXEL_COUNT
HEIGHT_STEP = pc.cSCALED_SECTION_HEIGHT_TO_OVERLAP_PIXEL_COUNT

MASTER_ROW_COUNT = pc.cMASTER_ROW_COUNT
MASTER_COL_COUNT = pc.cMASTER_COL_COUNT


def section_to_pixel_rect(row: int, col: int, anchor: tuple[float, float], zoom: float
                          ) -> tuple[float, float, float, float]:
    """Returns (x, y, width, height) of a section's rect in scene pixel space."""
    x = anchor[0] + col * WIDTH_STEP * zoom
    y = anchor[1] + row * HEIGHT_STEP * zoom
    return x, y, WIDTH_STEP * zoom, HEIGHT_STEP * zoom


def section_center(row: int, col: int, anchor: tuple[float, float], zoom: float) -> tuple[float, float]:
    x, y, w, h = section_to_pixel_rect(row, col, anchor, zoom)
    return x + w / 2.0, y + h / 2.0


def point_to_section_f(x: float, y: float, anchor: tuple[float, float], zoom: float) -> tuple[float, float]:
    """Continuous (non-truncated) row/col - used for focus points, which the user
    wants free to sit anywhere rather than snapped to a grid cell (stage_calibration
    accepts a fractional row/col directly, by temporarily shifting the controller's
    anchor - see autofocus_client.py)."""
    col = (x - anchor[0]) / (WIDTH_STEP * zoom)
    row = (y - anchor[1]) / (HEIGHT_STEP * zoom)
    return row, col


def point_to_section(x: float, y: float, anchor: tuple[float, float], zoom: float) -> tuple[int, int]:
    """Truncated to a whole grid cell - used for the paintbrush, which paints
    discrete sections."""
    row, col = point_to_section_f(x, y, anchor, zoom)
    return int(row), int(col)


def point_to_section_center_f(x: float, y: float, anchor: tuple[float, float], zoom: float
                              ) -> tuple[float, float]:
    """Like point_to_section_f, but measured from a cell's CENTER rather than its
    top-left corner - so a point sitting exactly at a cell's rendered center (as a
    freshly-placed, undragged focus point does) maps back to that cell's exact
    integer row/col, rather than landing exactly on a +0.5 rounding boundary where
    ordinary round()/floating-point noise could resolve to either neighboring cell."""
    row, col = point_to_section_f(x, y, anchor, zoom)
    return row - 0.5, col - 0.5
