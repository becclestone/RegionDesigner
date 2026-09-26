"""Shared per-region color palette (region_id -> color), used by both the canvas's
region outlines and the focus point markers so a region's outline and its
autofocus points visually match."""
from PySide6.QtGui import QColor

REGION_PALETTE = [
    QColor(230, 25, 75), QColor(60, 180, 75), QColor(255, 225, 25),
    QColor(0, 130, 200), QColor(245, 130, 48), QColor(145, 30, 180),
    QColor(70, 240, 240), QColor(240, 50, 230),
]


def region_color(region_id: int) -> QColor:
    return REGION_PALETTE[region_id % len(REGION_PALETTE)]
