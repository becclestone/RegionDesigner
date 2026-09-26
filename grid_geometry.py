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


def point_to_section(x: float, y: float, anchor: tuple[float, float], zoom: float) -> tuple[int, int]:
    col = int((x - anchor[0]) / (WIDTH_STEP * zoom))
    row = int((y - anchor[1]) / (HEIGHT_STEP * zoom))
    return row, col
