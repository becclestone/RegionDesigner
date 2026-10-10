"""Paintbrush canvas: freehand section painting + region/focus-point display.

Painted sections are NOT rendered as one QGraphicsItem each (could be thousands
for a large brushed area and would bog the scene down). Instead a single offscreen
QImage mask is redrawn on each brush stroke and shown through one QGraphicsPixmapItem.

The brush itself paints a free-form circular stroke in continuous scene (image)
space while the mouse is down - the stroke is only converted into the discrete
painted-section set once the mouse button is released.
"""
import math

from PIL import Image as PILImage
from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import (
    QImage, QPixmap, QPainter, QColor, QPen, QFont, QMouseEvent, QWheelEvent,
    QPainterPath, QPainterPathStroker,
)
from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsPathItem, QGraphicsEllipseItem,
)

from Constants import project_constants as pc
import grid_geometry as geom
from focus_point_item import FocusPointItem
from region_colors import region_color

Section = tuple[int, int]

# Mirrors DOVER_UI/Windows/demo_control_a.py's load_image_at_path image prep - minus its
# 0.5x scale-down (RegionDesigner keeps the full-resolution image) - so a section painted
# here lines up with the physical grid the same way DOVER_UI's does: correct the fixed
# camera-mount tilt, flip to the grid's row-down convention, then crop to the sensor's
# usable region. The crop box is 2x demo_control_a.py's _CROP_LEFT/_CROP_TOP/_CROP_RIGHT/
# _CROP_BOTTOM, which are defined in its half-resolution display image's pixel space.
_CROP_BOX = (42, 1050, 4918, 2950)  # (left, top, right, bottom)


def _load_registered_image(path: str) -> PILImage.Image:
    image = PILImage.open(path).rotate(pc.cCAM_CCW_ROT_DEG)
    image = image.transpose(PILImage.Transpose.FLIP_TOP_BOTTOM)
    return image.crop(_CROP_BOX)


def _pil_to_qpixmap(image: PILImage.Image) -> QPixmap:
    image = image.convert("RGB")
    data = image.tobytes("raw", "RGB")
    qimage = QImage(data, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888)
    qimage = qimage.copy()  # detach from the PIL buffer before it's garbage collected
    return QPixmap.fromImage(qimage)


_PAINTED_COLOR = QColor(30, 144, 255, 110)
_REGION_OUTLINE_WIDTH = 3
_ACTIVE_REGION_OUTLINE_WIDTH = 6
_ACTIVE_REGION_OUTLINE_COLOR = QColor(255, 255, 255)

_STROKE_PAINT_COLOR = QColor(30, 144, 255, 150)
_STROKE_ERASE_COLOR = QColor(220, 60, 60, 150)
_BRUSH_CURSOR_COLOR = QColor(255, 255, 255, 220)

_LABEL_TEXT_COLOR = QColor(255, 255, 255)
_LABEL_BG_COLOR = QColor(0, 0, 0, 170)

_GRID_LINE_COLOR = QColor(255, 255, 0, 70)

# Region-progress overlay: keyed by SectionCanvas.region_status's values
# ("focusing" / "confirmed" / "failed" / "scanning" / "scanned"), drawn on top of
# the region's normal outline so progress is visible without opening the focus
# review dialog.
_STATUS_FILL_COLORS = {
    "focusing": QColor(255, 200, 0, 40),
    "confirmed": QColor(60, 220, 90, 45),
    "failed": QColor(230, 60, 60, 55),
    "scanning": QColor(60, 140, 220, 45),
    "scanned": QColor(30, 160, 160, 50),
    # Reloaded from a previous aggregate batch (see main_window._restore_region_status)
    # whose Scan_data folder has some, but not all, of this region's sections'
    # NR images on disk yet - a hardware failure interrupted its scan partway
    # through. Distinct purple so it doesn't get confused with "scanning"
    # (blue, actively in flight right now) or "failed" (red).
    "partial": QColor(160, 90, 220, 50),
}
_STATUS_LABEL_BG_COLORS = {
    "focusing": QColor(200, 140, 0, 220),
    "confirmed": QColor(30, 140, 60, 220),
    "failed": QColor(180, 40, 40, 220),
    "scanning": QColor(30, 90, 170, 220),
    "scanned": QColor(20, 110, 110, 220),
    "partial": QColor(110, 50, 170, 220),
}

# Redo-correction overlay (see SectionCanvas.redo_sections) - a distinct magenta so
# it can't be confused with either the region-status fill above or the live scan
# activity overlay below; drawn on top of both, since a marked section may also be
# "scanned"/mid-rescan at the same time.
_REDO_MARK_COLOR = QColor(255, 0, 255, 90)

# Live per-section scan/reconstruction overlay (see SectionCanvas.section_activity
# and set_section_activity) - drawn per painted section, on top of the coarser
# region-level tint above, as ControllerBridge's sectionScanning/
# sectionReconstructing signals report individual sections starting/finishing.
# Mirrors DOVER_UI's own colored-box scan/recon overlay (demo_control_a.py):
# solid gold/red there since it draws opaque PNG tiles, translucent here since
# this canvas fills directly over the section's normal appearance instead of
# replacing it.
# "reconstructed" is a persistent marker (not just an instant): main_window.py
# sets it once a section's cRECON_OFF_MSG arrives, instead of clearing back to
# no overlay, so a completed section stays visibly distinct from one still
# awaiting reconstruction - a darker/desaturated green vs. "reconstructing"'s
# brighter one, so the two are easy to tell apart at a glance.
_SECTION_ACTIVITY_COLORS = {
    "scanning": QColor(255, 220, 0, 130),
    "reconstructing": QColor(60, 220, 90, 130),
    "reconstructed": QColor(20, 90, 70, 120),
}


class SectionCanvas(QGraphicsView):
    sectionsChanged = Signal()
    focusPointShiftClicked = Signal(object)  # FocusPointItem - see mousePressEvent
    # region_id (int, or None if that section isn't assigned to a compiled
    # region yet - main_window then asks to compile regions first), row, col -
    # see mouseDoubleClickEvent
    focusPointAddRequested = Signal(object, float, float)
    focusPointRemoveRequested = Signal(object)  # FocusPointItem - see mousePressEvent
    redoFocusPointSetRequested = Signal(float, float)  # see mousePressEvent (pick_redo_focus_mode)
    redoFocusPointRemoveRequested = Signal()  # right-click on the redo focus point marker

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setMouseTracking(True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)

        self.anchor: tuple[float, float] = geom.DEFAULT_ANCHOR_PX
        self.zoom: float = 1.0

        self._view_scale = 1.0
        self._view_scale_min = 0.05
        self._view_scale_max = 40.0
        self._panning = False
        self._pan_last_pos = None

        self.background_item: QGraphicsPixmapItem | None = None
        self.mask_image: QImage | None = None
        self.mask_item: QGraphicsPixmapItem | None = None

        self.painted: set[Section] = set()
        self.region_of: dict[Section, int] = {}
        self.draw_mode = True  # False once regions are compiled - brush is inactive until cleared
        self.active_region_id: int | None = None  # region currently being scanned - drawn highlighted
        self.region_status: dict[int, str] = {}  # region_id -> "focusing" | "confirmed" | "failed" | "scanning" | "scanned"
        self.region_progress: dict[int, tuple[int, int]] = {}  # region_id -> (points done, total)
        self.section_activity: dict[Section, str] = {}  # (row,col) -> "scanning" | "reconstructing" | "reconstructed"
        self.show_grid = False  # overlay of section-grid lines, toggled from the toolbar
        self.snap_focus_points_on_release = False  # see set_snap_focus_points_on_release

        # Redo/correction (post-hoc): sections marked for a targeted rescan, and the
        # brush/pick modes that populate them - see main_window's "Redo / Correct
        # Scans" controls. Marking is restricted to already-painted sections (see
        # _apply_stroke_to_sections) - a redo mark only makes sense on a section
        # that's actually part of the loaded layout.
        self.redo_sections: set[Section] = set()
        self.redo_mode = False  # brush marks/unmarks redo_sections instead of painted
        self.pick_redo_focus_mode = False  # a plain click places the single redo focus point
        self.redo_focus_point_item: FocusPointItem | None = None

        self.brush_radius = 4  # radius in section-width units (true circular radius in scene pixels)
        self._painting = False
        self._erase_mode = False
        self._stroke_path: QPainterPath | None = None
        self._stroke_preview_item: QGraphicsPathItem | None = None
        self._brush_cursor_item: QGraphicsEllipseItem | None = None

        self.focus_point_items: list[FocusPointItem] = []

    # ---- background image ----
    def reset(self):
        """Clears everything back to a blank canvas - no background image, no
        painted sections, no regions, no focus points - so a brand new scan
        starts from the same state the app does on launch. See main_window's
        Reset button."""
        if self.background_item is not None:
            self._scene.removeItem(self.background_item)
            self.background_item = None
        if self.mask_item is not None:
            self._scene.removeItem(self.mask_item)
            self.mask_item = None
        self.mask_image = None
        self._hide_brush_cursor()

        self.painted = set()
        self.region_of = {}
        self.draw_mode = True
        self.active_region_id = None
        self.region_status = {}
        self.region_progress = {}
        self.section_activity = {}
        self.clear_focus_points()
        self.redo_sections = set()
        self.redo_mode = False
        self.pick_redo_focus_mode = False
        self.clear_redo_focus_point()

    def set_background_image(self, path: str):
        pixmap = _pil_to_qpixmap(_load_registered_image(path))
        if self.background_item is not None:
            self._scene.removeItem(self.background_item)
        self.background_item = QGraphicsPixmapItem(pixmap)
        self.background_item.setZValue(-10)
        self._scene.addItem(self.background_item)
        self._scene.setSceneRect(QRectF(self.background_item.boundingRect()))
        self._reset_mask(pixmap.width(), pixmap.height())
        self._redraw_mask()

    def _reset_mask(self, width: int, height: int):
        self.mask_image = QImage(max(width, 1), max(height, 1), QImage.Format.Format_ARGB32_Premultiplied)
        self.mask_image.fill(Qt.GlobalColor.transparent)
        if self.mask_item is not None:
            self._scene.removeItem(self.mask_item)
        self.mask_item = QGraphicsPixmapItem(QPixmap.fromImage(self.mask_image))
        self.mask_item.setZValue(0)
        self._scene.addItem(self.mask_item)

    # ---- zoom (scroll wheel) / pan (middle click drag) ----
    def wheelEvent(self, event: QWheelEvent):
        if self.background_item is None:
            super().wheelEvent(event)
            return

        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            return
        factor = 1.25 ** steps
        new_scale = self._view_scale * factor
        new_scale = max(self._view_scale_min, min(self._view_scale_max, new_scale))
        factor = new_scale / self._view_scale
        if factor == 1.0:
            return
        self._view_scale = new_scale
        self.scale(factor, factor)
        event.accept()

    # ---- mouse handling: paint, or forward to Qt's item-drag for focus points ----
    def mousePressEvent(self, event: QMouseEvent):
        # Shift+click a focus point marker to review/override it - checked first
        # (and unconditionally, regardless of draw_mode/painting) so it always
        # wins over both brush painting and the marker's own drag behavior. See
        # main_window._on_focus_point_shift_clicked.
        if event.button() == Qt.MouseButton.LeftButton and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            clicked_item = self.itemAt(event.position().toPoint())
            if clicked_item is self.redo_focus_point_item:
                event.accept()  # nothing to review for a single redo point
                return
            if isinstance(clicked_item, FocusPointItem):
                self.focusPointShiftClicked.emit(clicked_item)
                event.accept()
                return

        # Right-click a focus point marker to remove it outright - checked next,
        # same unconditional priority as the shift+click case above, so it wins
        # over the brush's own right-click-to-erase behavior when the cursor is
        # actually over a marker. See main_window._on_focus_point_remove_requested.
        # The redo focus point is checked first and routed to its own request
        # signal (it isn't in focus_point_items, so the generic remove-requested
        # path below wouldn't actually find/remove it) -
        # main_window decides whether it's safe to remove right now, same as it
        # does for a normal focus point.
        if event.button() == Qt.MouseButton.RightButton:
            clicked_item = self.itemAt(event.position().toPoint())
            if clicked_item is self.redo_focus_point_item:
                self.redoFocusPointRemoveRequested.emit()
                event.accept()
                return
            if isinstance(clicked_item, FocusPointItem):
                self.focusPointRemoveRequested.emit(clicked_item)
                event.accept()
                return

        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_last_pos = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        # Pick-redo-focus mode: a plain left-click (not on an existing marker)
        # places/moves the single redo focus point - checked before the brush
        # gate below so it works regardless of draw_mode/redo_mode.
        if self.pick_redo_focus_mode and event.button() == Qt.MouseButton.LeftButton and self.mask_image is not None:
            clicked_item = self.itemAt(event.position().toPoint())
            if not isinstance(clicked_item, FocusPointItem):
                scene_pos = self.mapToScene(event.position().toPoint())
                row, col = geom.point_to_section_center_f(scene_pos.x(), scene_pos.y(), self.anchor, self.zoom)
                self.redoFocusPointSetRequested.emit(row, col)
                event.accept()
                return

        if self.mask_image is None or not (self.draw_mode or self.redo_mode):
            super().mousePressEvent(event)
            return

        clicked_item = self.itemAt(event.position().toPoint())
        if isinstance(clicked_item, FocusPointItem):
            super().mousePressEvent(event)
            return

        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self._erase_mode = event.button() == Qt.MouseButton.RightButton
            self._painting = True
            self._start_stroke(event.position())
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """Double-click empty canvas to add a focus point there, into whichever
        region owns the section under the cursor - main_window rejects the
        request (asking to compile regions first) if that section isn't
        painted/assigned to one yet. Double-clicking an existing marker does
        nothing special here (falls through to Qt's normal handling).

        Note: while draw_mode is still True (regions not yet compiled), Qt's own
        double-click sequence still delivers the first press/release as a
        normal click first - see mousePressEvent - so this can also paint or
        erase one section under the cursor as a side effect. Compiling regions
        first (which turns draw_mode off) avoids that."""
        if self.mask_image is not None and event.button() == Qt.MouseButton.LeftButton:
            clicked_item = self.itemAt(event.position().toPoint())
            if not isinstance(clicked_item, FocusPointItem):
                scene_pos = self.mapToScene(event.position().toPoint())
                row, col = geom.point_to_section_center_f(scene_pos.x(), scene_pos.y(), self.anchor, self.zoom)
                region_id = self.region_of.get((int(round(row)), int(round(col))))
                self.focusPointAddRequested.emit(region_id, row, col)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._panning:
            delta = event.position() - self._pan_last_pos
            self._pan_last_pos = event.position()
            h_bar = self.horizontalScrollBar()
            v_bar = self.verticalScrollBar()
            h_bar.setValue(h_bar.value() - int(delta.x()))
            v_bar.setValue(v_bar.value() - int(delta.y()))
            return

        if self._painting:
            self._extend_stroke(event.position())
            return

        self._update_brush_cursor(event.position())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self._pan_last_pos = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return

        if self._painting:
            self._painting = False
            self._finish_stroke()
            self.sectionsChanged.emit()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self._hide_brush_cursor()
        super().leaveEvent(event)

    # ---- free-form circular brush: paint in continuous scene space, convert on release ----
    def _brush_radius_px(self) -> float:
        """True circular radius in scene-pixel space (independent of the row/col
        step asymmetry that made the old grid-space brush paint an ellipse)."""
        return max(self.brush_radius, 0.5) * geom.WIDTH_STEP * self.zoom

    def _hide_brush_cursor(self):
        if self._brush_cursor_item is not None:
            self._brush_cursor_item.setVisible(False)

    def _update_brush_cursor(self, view_pos):
        if self.mask_image is None or not (self.draw_mode or self.redo_mode):
            self._hide_brush_cursor()
            return
        scene_pos = self.mapToScene(view_pos.toPoint())
        radius_px = self._brush_radius_px()
        if self._brush_cursor_item is None:
            self._brush_cursor_item = QGraphicsEllipseItem()
            self._brush_cursor_item.setZValue(5)
            pen = QPen(_BRUSH_CURSOR_COLOR, 1.5)
            pen.setCosmetic(True)
            self._brush_cursor_item.setPen(pen)
            self._brush_cursor_item.setBrush(Qt.BrushStyle.NoBrush)
            self._scene.addItem(self._brush_cursor_item)
        self._brush_cursor_item.setRect(
            scene_pos.x() - radius_px, scene_pos.y() - radius_px, radius_px * 2, radius_px * 2
        )
        self._brush_cursor_item.setVisible(True)

    def _start_stroke(self, view_pos):
        self._hide_brush_cursor()
        scene_pos = self.mapToScene(view_pos.toPoint())
        self._stroke_path = QPainterPath(scene_pos)
        self._update_stroke_preview()

    def _extend_stroke(self, view_pos):
        scene_pos = self.mapToScene(view_pos.toPoint())
        self._stroke_path.lineTo(scene_pos)
        self._update_stroke_preview()

    def _stroke_outline(self, stroke_path: QPainterPath) -> QPainterPath:
        """Filled geometry of the swept brush footprint. QPainterPath collapses a
        zero-length lineTo (a plain click with no drag), so QPainterPathStroker
        can't be relied on to turn that degenerate case into a circle - build the
        circle explicitly instead."""
        radius_px = self._brush_radius_px()
        if stroke_path.elementCount() < 2:
            start = stroke_path.elementAt(0)
            outline = QPainterPath()
            outline.addEllipse(QPointF(start.x, start.y), radius_px, radius_px)
            return outline

        stroker = QPainterPathStroker()
        stroker.setWidth(radius_px * 2)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return stroker.createStroke(stroke_path)

    def _update_stroke_preview(self):
        if self._stroke_preview_item is None:
            self._stroke_preview_item = QGraphicsPathItem()
            self._stroke_preview_item.setZValue(5)
            self._stroke_preview_item.setPen(Qt.PenStyle.NoPen)
            self._scene.addItem(self._stroke_preview_item)
        color = _STROKE_ERASE_COLOR if self._erase_mode else _STROKE_PAINT_COLOR
        self._stroke_preview_item.setBrush(color)
        self._stroke_preview_item.setPath(self._stroke_outline(self._stroke_path))

    def _finish_stroke(self):
        if self._stroke_preview_item is not None:
            self._scene.removeItem(self._stroke_preview_item)
            self._stroke_preview_item = None
        stroke_path = self._stroke_path
        self._stroke_path = None
        if stroke_path is not None:
            self._apply_stroke_to_sections(stroke_path)

    def _apply_stroke_to_sections(self, stroke_path: QPainterPath):
        outline = self._stroke_outline(stroke_path)

        bounds = outline.boundingRect()
        if bounds.isEmpty():
            return

        min_row, min_col = geom.point_to_section(bounds.left(), bounds.top(), self.anchor, self.zoom)
        max_row, max_col = geom.point_to_section(bounds.right(), bounds.bottom(), self.anchor, self.zoom)

        changed = False
        for row in range(min_row - 1, max_row + 2):
            for col in range(min_col - 1, max_col + 2):
                x, y, w, h = geom.section_to_pixel_rect(row, col, self.anchor, self.zoom)
                if not outline.intersects(QRectF(x, y, w, h)):
                    continue
                key = (row, col)
                if self.redo_mode:
                    if key not in self.painted:
                        continue  # only an already-loaded section can be marked for redo
                    if self._erase_mode:
                        if key in self.redo_sections:
                            self.redo_sections.discard(key)
                            changed = True
                    elif key not in self.redo_sections:
                        self.redo_sections.add(key)
                        changed = True
                elif self._erase_mode:
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
        if self.show_grid:
            self._draw_grid(painter)
        region_centroid_sum: dict[int, list[float]] = {}  # region_id -> [sum_x, sum_y, count]
        for (row, col) in self.painted:
            region_id = self.region_of.get((row, col))
            x, y, w, h = geom.section_to_pixel_rect(row, col, self.anchor, self.zoom)

            if region_id is None:
                # Not yet clustered into a region - just show what's been painted.
                painter.fillRect(QRectF(x, y, w, h), _PAINTED_COLOR)
            else:
                entry = region_centroid_sum.setdefault(region_id, [0.0, 0.0, 0])
                entry[0] += x + w / 2.0
                entry[1] += y + h / 2.0
                entry[2] += 1

                fill_color = _STATUS_FILL_COLORS.get(self.region_status.get(region_id))
                if fill_color is not None:
                    painter.fillRect(QRectF(x, y, w, h), fill_color)

                # Leave the interior transparent and only stroke the edges that border
                # a different region (or empty space), so the outline traces the
                # region's outer shape rather than every cell - the active region gets
                # a thicker, distinctly-colored outline instead of any fill, so focus
                # points already placed inside it stay easy to see.
                is_active = region_id == self.active_region_id
                pen = QPen(
                    _ACTIVE_REGION_OUTLINE_COLOR if is_active else region_color(region_id),
                    _ACTIVE_REGION_OUTLINE_WIDTH if is_active else _REGION_OUTLINE_WIDTH,
                )
                painter.setPen(pen)
                for (dr, dc, x1, y1, x2, y2) in (
                    (-1, 0, x, y, x + w, y),          # top
                    (1, 0, x, y + h, x + w, y + h),   # bottom
                    (0, -1, x, y, x, y + h),          # left
                    (0, 1, x + w, y, x + w, y + h),   # right
                ):
                    if self.region_of.get((row + dr, col + dc)) != region_id:
                        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

            activity_color = _SECTION_ACTIVITY_COLORS.get(self.section_activity.get((row, col)))
            if activity_color is not None:
                painter.fillRect(QRectF(x, y, w, h), activity_color)

            if (row, col) in self.redo_sections:
                painter.fillRect(QRectF(x, y, w, h), _REDO_MARK_COLOR)

        for region_id, (sum_x, sum_y, count) in region_centroid_sum.items():
            self._draw_region_label(
                painter, region_id, sum_x / count, sum_y / count,
                self.region_status.get(region_id), self.region_progress.get(region_id),
            )

        painter.end()
        self.mask_item.setPixmap(QPixmap.fromImage(self.mask_image))

    def _draw_grid(self, painter: QPainter):
        """Draws section-grid lines across the whole image, so the operator can see
        how sections line up before/while painting - independent of self.painted."""
        width = self.mask_image.width()
        height = self.mask_image.height()
        w_step = geom.WIDTH_STEP * self.zoom
        h_step = geom.HEIGHT_STEP * self.zoom
        if w_step <= 0 or h_step <= 0:
            return
        anchor_x, anchor_y = self.anchor

        pen = QPen(_GRID_LINE_COLOR, 1)
        pen.setCosmetic(True)
        painter.setPen(pen)

        min_col = math.floor((0 - anchor_x) / w_step) - 1
        max_col = math.ceil((width - anchor_x) / w_step) + 1
        for col in range(min_col, max_col + 1):
            x = anchor_x + col * w_step
            painter.drawLine(QPointF(x, 0), QPointF(x, height))

        min_row = math.floor((0 - anchor_y) / h_step) - 1
        max_row = math.ceil((height - anchor_y) / h_step) + 1
        for row in range(min_row, max_row + 1):
            y = anchor_y + row * h_step
            painter.drawLine(QPointF(0, y), QPointF(width, y))

    def set_show_grid(self, show: bool):
        if show == self.show_grid:
            return
        self.show_grid = show
        if self.mask_image is not None:
            self._redraw_mask()

    def set_snap_focus_points_on_release(self, enabled: bool):
        """Toggled from the toolbar checkbox - while enabled, releasing a dragged
        focus point (see FocusPointItem.mouseReleaseEvent) immediately snaps it
        back to its nearest section center instead of leaving it free-form."""
        self.snap_focus_points_on_release = enabled

    def _draw_region_label(
        self, painter: QPainter, region_id: int, cx: float, cy: float,
        status: str | None = None, progress: tuple[int, int] | None = None,
    ):
        """Draws the region's scan-order number (its region_id, which clustering
        already assigns in serpentine scan order) at the region's centroid, so the
        operator can read the intended scan order straight off the canvas. When a
        focusing run - or a real region scan - has touched this region, the label
        also reports progress ("running autofocus" -> "n/total", then a
        confirmed/failed mark; "scanning" while a real scan is in flight, then
        "scanned") and its background is tinted to match, so status is visible
        without opening the focus review dialog."""
        if status == "focusing" and progress is not None:
            text = f"{region_id} ({progress[0]}/{progress[1]})"
        elif status == "confirmed":
            text = f"{region_id} ✓"
        elif status == "failed":
            text = f"{region_id} ✗"
        elif status == "scanning":
            text = f"{region_id} (scanning)"
        elif status == "scanned":
            text = f"{region_id} (scanned)"
        elif status == "partial" and progress is not None:
            text = f"{region_id} ({progress[0]}/{progress[1]} scanned - resume)"
        elif status == "partial":
            text = f"{region_id} (partial)"
        else:
            text = str(region_id)
        font = QFont()
        font.setPointSizeF(12.0)
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_rect = metrics.boundingRect(text)
        padding = 4
        bg_rect = QRectF(
            cx - text_rect.width() / 2.0 - padding, cy - text_rect.height() / 2.0 - padding,
            text_rect.width() + padding * 2, text_rect.height() + padding * 2,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_STATUS_LABEL_BG_COLORS.get(status, _LABEL_BG_COLOR))
        painter.drawRoundedRect(bg_rect, 3, 3)
        painter.setPen(_LABEL_TEXT_COLOR)
        painter.drawText(bg_rect, Qt.AlignmentFlag.AlignCenter, text)

    def set_active_region(self, region_id: int | None):
        """Marks the given region as the one currently being scanned - drawn with a
        filled highlight and a thicker outline so it stands out from the rest."""
        if region_id == self.active_region_id:
            return
        self.active_region_id = region_id
        if self.mask_image is not None:
            self._redraw_mask()

    def set_region_status(self, region_id: int, status: str | None):
        """Marks a region's progress state ("focusing"/"confirmed"/"failed"/
        "scanning"/"scanned", or None to go back to unmarked/pending) - see
        _draw_region_label and the fill overlay in _redraw_mask for how this is drawn."""
        if status is None:
            self.region_status.pop(region_id, None)
        else:
            self.region_status[region_id] = status
        if self.mask_image is not None:
            self._redraw_mask()

    def set_region_progress(self, region_id: int, done: int, total: int):
        self.region_progress[region_id] = (done, total)
        if self.mask_image is not None:
            self._redraw_mask()

    def set_section_activity(self, row: int, col: int, activity: str | None):
        """Live per-section scan/reconstruction overlay - see ControllerBridge's
        sectionScanning/sectionReconstructing signals (wired in main_window.py).
        activity is "scanning", "reconstructing", "reconstructed" (a persistent
        done marker - see main_window._on_section_reconstructing), or None
        (idle, no marker)."""
        key = (row, col)
        if activity is None:
            if key not in self.section_activity:
                return
            del self.section_activity[key]
        else:
            self.section_activity[key] = activity
        if self.mask_image is not None:
            self._redraw_mask()

    # ---- regions / focus points ----
    def apply_regions(self, region_of: dict[Section, int]):
        self.region_of = region_of
        self.draw_mode = False
        self.region_status = {}
        self.region_progress = {}
        self.section_activity = {}
        self._hide_brush_cursor()
        if self.mask_image is not None:
            self._redraw_mask()

    def clear_regions(self):
        """Undo compilation: drop region assignments/focus points and go back to
        free-form brush painting of self.painted."""
        self.region_of = {}
        self.draw_mode = True
        self.active_region_id = None
        self.region_status = {}
        self.region_progress = {}
        self.section_activity = {}
        self.clear_focus_points()
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

    def remove_focus_point(self, item: FocusPointItem):
        """Removes one focus point marker - see
        main_window._on_focus_point_remove_requested (right-click)."""
        if item not in self.focus_point_items:
            return
        self.focus_point_items.remove(item)
        self._scene.removeItem(item)

    def snap_focus_points_to_grid(self) -> int:
        """Resets every focus point back to the exact center of its nearest
        section, undoing any free-form drag - e.g. after points were dragged
        against a grid anchor that later turned out to be wrong. Returns the
        number of points snapped."""
        for item in self.focus_point_items:
            item.snap_to_grid()
        return len(self.focus_point_items)

    def focus_points_by_region(self) -> dict[int, list[tuple[float, float]]]:
        """Continuous (row, col) per point (see FocusPointItem.section) - not the
        integer Section alias used for painted grid cells."""
        out: dict[int, list[tuple[float, float]]] = {}
        for item in self.focus_point_items:
            out.setdefault(item.region_id, []).append(item.section())
        return out

    def assign_new_region(self, sections: set[Section]) -> int:
        """Carves a brand-new region out of an arbitrary sub-selection of already-
        painted sections (see main_window's 'Create Region from Marked Sections') -
        unlike Compile Regions, this leaves every other existing region's
        membership untouched; a section reassigned here just moves out of
        whatever region (if any) it belonged to before. Returns the new region's
        id: one past the current highest region id (0 if there are none yet -
        same 0-based convention region_clustering.assign_regions uses)."""
        new_region_id = (max(self.region_of.values()) + 1) if self.region_of else 0
        for section in sections:
            self.region_of[section] = new_region_id
            self.redo_sections.discard(section)
        self.draw_mode = False
        self._redraw_mask()
        return new_region_id

    # ---- redo/correction (post-hoc) ----
    def set_redo_mode(self, enabled: bool):
        self.redo_mode = enabled
        if not enabled:
            self._hide_brush_cursor()

    def set_pick_redo_focus_mode(self, enabled: bool):
        self.pick_redo_focus_mode = enabled

    def clear_redo_marks(self):
        self.redo_sections = set()
        if self.mask_image is not None:
            self._redraw_mask()

    def set_redo_focus_point(self, row: float, col: float) -> FocusPointItem:
        """Places (or moves, if one already exists) the single focus point used to
        focus every section currently marked for redo - see main_window's "Set
        Redo Focus Point" control. region_id=None, drawn with a neutral marker
        style since it isn't tied to any one compiled region."""
        if self.redo_focus_point_item is not None:
            self._scene.removeItem(self.redo_focus_point_item)
        item = FocusPointItem(None, row, col, self)
        self._scene.addItem(item)
        self.redo_focus_point_item = item
        return item

    def clear_redo_focus_point(self):
        if self.redo_focus_point_item is not None:
            self._scene.removeItem(self.redo_focus_point_item)
            self.redo_focus_point_item = None
