"""A map folder's own skin images win over the chosen skin's, as in osu!:
katacheh-style gimmick maps hide the roll body with see-through ones."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from library.skin import TaikoSkin

app = QApplication.instance() or QApplication([])


def _png(path: Path, colour: QColor, size: int = 8) -> None:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(colour)
    image.save(str(path))


class BeatmapSkinTests(unittest.TestCase):
    def test_the_maps_images_override_the_skins_and_the_rest_stay(self):
        with tempfile.TemporaryDirectory() as skin_dir, tempfile.TemporaryDirectory() as map_dir:
            skin, beatmap = Path(skin_dir), Path(map_dir)
            for name in ("taikohitcircle", "taiko-roll-end"):
                _png(skin / f"{name}.png", QColor("white"))
            _png(beatmap / "taiko-roll-end.png", QColor(0, 0, 0, 0), size=4)

            loaded = TaikoSkin(skin, beatmap)
            self.assertEqual(loaded.size("taiko-roll-end"), (4, 4))
            self.assertEqual(loaded.size("taikohitcircle"), (8, 8))
            self.assertEqual(TaikoSkin(skin).size("taiko-roll-end"), (8, 8))

    def test_only_the_maps_own_spinner_warning_counts_as_the_gimmick(self):
        with tempfile.TemporaryDirectory() as skin_dir, tempfile.TemporaryDirectory() as map_dir:
            skin, beatmap = Path(skin_dir), Path(map_dir)
            _png(skin / "spinner-warning.png", QColor("white"))
            self.assertFalse(TaikoSkin(skin, beatmap).from_beatmap("spinner-warning"))
            _png(beatmap / "spinner-warning.png", QColor("red"))
            self.assertTrue(TaikoSkin(skin, beatmap).from_beatmap("spinner-warning"))

    def test_a_map_skin_works_without_a_chosen_skin(self):
        with tempfile.TemporaryDirectory() as map_dir:
            _png(Path(map_dir) / "taiko-roll-middle.png", QColor(0, 0, 0, 0))
            self.assertTrue(TaikoSkin(None, Path(map_dir)).has("taiko-roll-middle"))


if __name__ == "__main__":
    unittest.main()
