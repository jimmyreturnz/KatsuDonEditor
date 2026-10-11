"""The hit target's approachcircle at its own size, as ppy/osu draws it."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from ui import gui
from ui import gameplay_view
from library.skin import TaikoSkin

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _ring_width(file_name: str, side: int) -> int:
    with tempfile.TemporaryDirectory() as folder:
        image = QImage(side, side, QImage.Format_ARGB32)
        image.fill(Qt.white)
        image.save(str(Path(folder) / file_name))
        # Taiko note art too, or the folder is not offered as a taiko skin.
        image.save(str(Path(folder) / "taikohitcircle.png"))
        view = gui.GameplayViewerView()
        view.resize(800, 200)
        view.set_skin(TaikoSkin(Path(folder)))
        ring = view._hit_ring()
        return ring.width()


class HitRingSizeTests(unittest.TestCase):
    def test_ring_is_its_own_size_at_the_rulesets_scale(self) -> None:
        # A 200px view is the 200-unit playfield: one unit, one pixel.
        self.assertEqual(_ring_width("approachcircle.png", 192), round(192 * gameplay_view.HIT_TARGET_RING_SCALE))
        self.assertEqual(_ring_width("approachcircle.png", 128), round(128 * gameplay_view.HIT_TARGET_RING_SCALE))

    def test_a_2x_file_counts_half(self) -> None:
        self.assertEqual(_ring_width("approachcircle@2x.png", 252), round(126 * gameplay_view.HIT_TARGET_RING_SCALE))

    def test_the_scale_is_lazers(self) -> None:
        # TaikoLegacyHitTarget: SCALE (0.8) + 0.03.
        self.assertAlmostEqual(gameplay_view.HIT_TARGET_RING_SCALE, 0.83)


if __name__ == "__main__":
    unittest.main()
