"""Draggable marker representing one autofocus point."""
from PySide6.QtGui import QBrush, QPen, QColor
from PySide6.QtWidgets import QGraphicsEllipseItem

import grid_geometry as geom

_RADIUS_PX = 6.0
_FILL_COLOR = QColor(255, 255, 255, 235)
_BORDER_COLOR = QColor(20, 20, 20, 235)


class FocusPointItem(QGraphicsEllipseItem):
    def __init__(self, region_id: int, row: int, col: int, canvas):
        super().__init__(-_RADIUS_PX, -_RADIUS_PX, _RADIUS_PX * 2, _RADIUS_PX * 2)
        self.region_id = region_id
        self.canvas = canvas
        self._row = row
        self._col = col

        self.setBrush(QBrush(_FILL_COLOR))
        self.setPen(QPen(_BORDER_COLOR, 1.5))
        self.setZValue(10)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsEllipseItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)

        self._sync_position_from_section()

    def section(self) -> tuple[int, int]:
        return self._row, self._col

    def _sync_position_from_section(self):
        x, y = geom.section_center(self._row, self._col, self.canvas.anchor, self.canvas.zoom)
        self.setPos(x, y)

    def itemChange(self, change, value):
        if change == QGraphicsEllipseItem.GraphicsItemChange.ItemPositionHasChanged:
            # Dragging is free/continuous (no snapping to painted cells); the
            # section this point currently sits over is simply recomputed live.
            self._row, self._col = geom.point_to_section(value.x(), value.y(), self.canvas.anchor, self.canvas.zoom)
        return super().itemChange(change, value)
