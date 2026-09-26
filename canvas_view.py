"""Paintbrush canvas: freehand section painting + region/focus-point display.

Painted sections are NOT rendered as one QGraphicsItem each (could be thousands
for a large brushed area and would bog the scene down). Instead a single offscreen
QImage mask is redrawn on each brush stroke and shown through one QGraphicsPixmapItem.
"""
from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QMouseEvent
from PySide6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsPixmapItem

import grid_geometry as geom
from focus_point_item import FocusPointItem

Section = tuple[int, int]

_PAINTED_COLOR = QColor(30, 144, 255, 110)
_REGION_PALETTE = [
    QColor(230, 25, 75, 150), QColor(60, 180, 75, 150), QColor(255, 225, 25, 150),
    QColor(0, 130, 200, 150), QColor(245, 130, 48, 150), QColor(145, 30, 180, 150),
    QColor(70, 240, 240, 150), QColor(240, 50, 230, 150),
]


class SectionCanvas(QGraphicsView):
    sectionsChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setMouseTracking(True)

        self.anchor: tuple[float, float] = (0.0, 0.0)
        self.zoom: float = 1.0

        self.background_item: QGraphicsPixmapItem | None = None
        self.mask_image: QImage | None = None
        self.mask_item: QGraphicsPixmapItem | None = None

        self.painted: set[Section] = set()
        self.region_of: dict[Section, int] = {}

        self.brush_radius = 2  # radius in section units
        self._painting = False
        self._erase_mode = False

        self.focus_point_items: list[FocusPointItem] = []

    # ---- background image ----
    def set_background_image(self, path: str):
        pixmap = QPixmap(path)
        if self.background_item is not None:
            self._scene.removeItem(self.background_item)
        self.background_item = QGraphicsPixmapItem(pixmap)
        self.background_item.setZValue(-10)
        self._scene.addItem(self.background_item)
        self._scene.setSceneRect(QRectF(self.background_item.boundingRect()))
        self._reset_mask(pixmap.width(), pixmap.height())

    def _reset_mask(self, width: int, height: int):
        self.mask_image = QImage(max(width, 1), max(height, 1), QImage.Format.Format_ARGB32_Premultiplied)
        self.mask_image.fill(Qt.GlobalColor.transparent)
        if self.mask_item is not None:
            self._scene.removeItem(self.mask_item)
        self.mask_item = QGraphicsPixmapItem(QPixmap.fromImage(self.mask_image))
        self.mask_item.setZValue(0)
        self._scene.addItem(self.mask_item)

    # ---- mouse handling: paint, or forward to Qt's item-drag for focus points ----
    def mousePressEvent(self, event: QMouseEvent):
        if self.mask_image is None:
            super().mousePressEvent(event)
            return

        clicked_item = self.itemAt(event.position().toPoint())
        if isinstance(clicked_item, FocusPointItem):
            super().mousePressEvent(event)
            return

        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self._erase_mode = event.button() == Qt.MouseButton.RightButton
            self._painting = True
            self._paint_at(event.position())
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._painting:
            self._paint_at(event.position())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._painting:
            self._painting = False
            self.sectionsChanged.emit()
            return
        super().mouseReleaseEvent(event)

    def _paint_at(self, view_pos):
        scene_pos = self.mapToScene(view_pos.toPoint())
        center_row, center_col = geom.point_to_section(scene_pos.x(), scene_pos.y(), self.anchor, self.zoom)

        changed = False
        radius_sq = self.brush_radius ** 2
        for r in range(center_row - self.brush_radius, center_row + self.brush_radius + 1):
            for c in range(center_col - self.brush_radius, center_col + self.brush_radius + 1):
                if (r - center_row) ** 2 + (c - center_col) ** 2 > radius_sq:
                    continue
                key = (r, c)
                if self._erase_mode:
                    if key in self.painted:
                        self.painted.discard(key)
                        self.region_of.pop(key, None)
                        changed = True
                elif key not in self.painted:
                    self.painted.add(key)
                    changed = True

        if changed:
            self._redraw_mask()

    def _redraw_mask(self):
        self.mask_image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(self.mask_image)
        for (row, col) in self.painted:
            x, y, w, h = geom.section_to_pixel_rect(row, col, self.anchor, self.zoom)
            region_id = self.region_of.get((row, col))
            color = _REGION_PALETTE[region_id % len(_REGION_PALETTE)] if region_id is not None else _PAINTED_COLOR
            painter.fillRect(QRectF(x, y, w, h), color)
        painter.end()
        self.mask_item.setPixmap(QPixmap.fromImage(self.mask_image))

    # ---- regions / focus points ----
    def apply_regions(self, region_of: dict[Section, int]):
        self.region_of = region_of
        self._redraw_mask()

    def clear_focus_points(self):
        for item in self.focus_point_items:
            self._scene.removeItem(item)
        self.focus_point_items.clear()

    def add_focus_point(self, region_id: int, row: int, col: int) -> FocusPointItem:
        item = FocusPointItem(region_id, row, col, self)
        self._scene.addItem(item)
        self.focus_point_items.append(item)
        return item

    def focus_points_by_region(self) -> dict[int, list[Section]]:
        out: dict[int, list[Section]] = {}
        for item in self.focus_point_items:
            out.setdefault(item.region_id, []).append(item.section())
        return out
