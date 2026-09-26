"""Turns a float32 reconstruction tif (arbitrary range, e.g. NR/s-{row}-{col}_nr_float32.tif)
into something Qt can display, via a percentile-based contrast stretch to 8-bit grayscale.
"""
import numpy as np
from PySide6.QtGui import QImage, QPixmap


def float_image_to_pixmap(image: np.ndarray) -> QPixmap:
    lo, hi = np.percentile(image, [1, 99.5])
    if hi <= lo:
        hi = lo + 1.0
    normalized = np.clip((image - lo) / (hi - lo), 0.0, 1.0)
    eight_bit = np.ascontiguousarray((normalized * 255).astype(np.uint8))

    height, width = eight_bit.shape
    qimage = QImage(eight_bit.data, width, height, width, QImage.Format.Format_Grayscale8)
    qimage = qimage.copy()  # detach from the numpy buffer before it's garbage collected
    return QPixmap.fromImage(qimage)
