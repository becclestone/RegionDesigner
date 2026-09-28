"""Draggable marker representing one autofocus point."""
from PySide6.QtGui import QBrush, QPen, QColor
from PySide6.QtWidgets import QGraphicsEllipseItem

import grid_geometry as geom
from region_colors import region_color

_RADIUS_PX = 3.0
_FILL_COLOR = QColor(255, 255, 255, 120)
_BORDER_WIDTH = 2.0
_BORDER_ALPHA = 150

# Per-point focusing status, set from the main window as an AutofocusSequenceWorker
# sweeps through a region's points, so progress is visible point-by-point rather
# than only once the whole region's sequence finishes.
_STATUS_FILL_COLORS = {
    "focusing": QColor(255, 210, 40, 210),
    "done": QColor(80, 230, 120, 210),
    "failed": QColor(230, 70, 70, 210),
}


class FocusPointItem(QGraphicsEllipseItem):
    def __init__(self, region_id: int, row: int, col: int, canvas):
        super().__init__(-_RADIUS_PX, -_RADIUS_PX, _RADIUS_PX * 2, _RADIUS_PX * 2)
        self.region_id = region_id
        self.canvas = canvas
        self._row = row
        self._col = col

        border_color = QColor(region_color(region_id))
        border_color.setAlpha(_BORDER_ALPHA)

        self.setBrush(QBrush(_FILL_COLOR))
        self.setPen(QPen(border_color, _BORDER_WIDTH))
        self.setZValue(10)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)

        self._sync_position_from_section()

    def set_status(self, status: str | None):
        """status is None (pending), "focusing", "done", or "failed"."""
        self.setBrush(QBrush(_STATUS_FILL_COLORS.get(status, _FILL_COLOR)))

    def section(self) -> tuple[float, float]:
        """Continuous (row, col) - not snapped to a grid cell. Use this for
        autofocus/physical positioning: stage_calibration.StageCalibration accepts
        fractional row/col directly, temporarily shifting the controller's anchor
        to hit the exact fractional location despite the controller only actually
        addressing whole master-grid rows/columns (see autofocus_client.py)."""
        return self._row, self._col

    def grid_cell(self) -> tuple[int, int]:
        """Nearest whole grid cell - use this wherever an actual scannable section
        identity is needed (e.g. the single-section confirmation scan)."""
        return int(round(self._row)), int(round(self._col))

    def _sync_position_from_section(self):
        x, y = geom.section_center(self._row, self._col, self.canvas.anchor, self.canvas.zoom)
        self.setPos(x, y)

    def snap_to_grid(self):
        """Rounds this point back to the exact center of whichever section it's
        currently nearest to, undoing any free-form drag - see
        SectionCanvas.snap_focus_points_to_grid."""
        self._row = round(self._row)
        self._col = round(self._col)
        self._sync_position_from_section()

    def itemChange(self, change, value):
        if change == QGraphicsEllipseItem.GraphicsItemChange.ItemPositionHasChanged:
            # Dragging is free/continuous (no snapping to painted cells, and no
            # rounding to a whole section either); the point's exact position is
            # simply recomputed live.
            self._row, self._col = geom.point_to_section_center_f(
                value.x(), value.y(), self.canvas.anchor, self.canvas.zoom)
        return super().itemChange(change, value)
