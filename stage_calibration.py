"""Row/col -> absolute stage XY, using the operator's existing calibration.

Ports dover_ctl2/src/Position-Homing-Saving-Configuration/positioning_utilities.cpp's
SectionXPosition/SectionYPosition. Those depend on two pieces of controller-internal
state (g_anchor_master_grid_X/Y, g_path_grid_anchor_section_row/col) that the
controller has no query message for - but they're exactly the values the operator
already sets via DOVER_UI's Stage Calibration tab and can save to a JSON file
(DOVER_UI/Windows/main_window.py:251-258 prepare_calibration_data, keys X/Y/Z/Row/Col).
Loading that same file - rather than re-deriving these values some other way - is
the only way to get a physical XY that's guaranteed to agree with the controller's
own addressing.

X_TRANSITION_MM/Y_TRANSITION_MM are the static per-section physical step sizes
(row step / col step after overlap) - not calibration state, so they're safe to
hardcode from Constants/project_constants.py's cSECTION_HEIGHT/WIDTH_TO_OVERLAP_MM,
which are the same 0.48mm/0.18mm values as core_system_config.json's
SECTION_X_SIZE_MM/SECTION_Y_SIZE_MM minus OPTICAL_OVERLAP_MM.
"""
import json

from Constants import project_constants as pc

X_TRANSITION_MM = pc.cSECTION_HEIGHT_TO_OVERLAP_MM  # row step
Y_TRANSITION_MM = pc.cSECTION_WIDTH_TO_OVERLAP_MM   # col step


class StageCalibration:
    def __init__(self, anchor_x: float, anchor_y: float, offset_row: int, offset_col: int):
        self.anchor_x = anchor_x
        self.anchor_y = anchor_y
        self.offset_row = offset_row
        self.offset_col = offset_col

    @classmethod
    def load(cls, calibration_json_path: str) -> "StageCalibration":
        with open(calibration_json_path, "r") as f:
            cal = json.load(f)
        return cls(
            anchor_x=cal[pc.cX],
            anchor_y=cal[pc.cY],
            offset_row=cal[pc.cROW],
            offset_col=cal[pc.cCOL],
        )

    def section_to_absolute_xy(self, row: float, col: float) -> tuple[float, float]:
        """Mirrors positioning_utilities.cpp's SectionXPosition/SectionYPosition exactly,
        including the Y axis being inverted relative to col. row/col may be
        non-integer - the formula is linear so a continuous focus-point position
        (not snapped to a grid cell) still maps to a valid physical XY."""
        x = self.anchor_x + (self.offset_row + row) * X_TRANSITION_MM
        y = self.anchor_y - (self.offset_col + col) * Y_TRANSITION_MM
        return x, y
