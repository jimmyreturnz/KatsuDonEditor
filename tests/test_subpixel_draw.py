"""Scrolling sprites are placed to a quarter pixel, not snapped to a whole one."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from ui import common

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _centroid(x: float) -> float:
    """Where the sprite's coverage is centred, drawn at `x`."""
    sprite = QPixmap(8, 8)
    sprite.fill(QColor(255, 255, 255))
    image = QImage(40, 10, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    common.draw_pixmap_subpixel(painter, sprite, x, 1.0)
    painter.end()
    weights = [image.pixelColor(column, 4).alphaF() for column in range(40)]
    return sum((column + 0.5) * w for column, w in enumerate(weights)) / sum(weights)


class SubpixelDrawTests(unittest.TestCase):
    def test_a_quarter_pixel_step_moves_the_sprite_a_quarter_pixel(self) -> None:
        # Within 0.06px: the blend is 8-bit, so a quarter step lands at 0.20.
        # Snapped, as before, every step under half a pixel was 0.
        base = _centroid(10.0)
        for step in (0.25, 0.5, 0.75, 1.0):
            self.assertAlmostEqual(_centroid(10.0 + step) - base, step, delta=0.06)

    def test_whole_pixel_positions_draw_the_sprite_unchanged(self) -> None:
        self.assertAlmostEqual(_centroid(10.0), 14.0, places=3)


if __name__ == "__main__":
    unittest.main()
