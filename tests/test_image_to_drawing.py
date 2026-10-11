import tempfile
import unittest
from pathlib import Path

from PySide6.QtGui import QColor, QImage

from chart.image_to_drawing import TraceOptions, trace_image


class FastImageTracingTests(unittest.TestCase):
    def _save(self, path, background, foreground):
        image = QImage(64, 48, QImage.Format_RGBA8888)
        image.fill(background)
        for y in range(12, 36):
            for x in range(16, 48):
                image.setPixelColor(x, y, foreground)
        self.assertTrue(image.save(str(path)))

    def test_dark_lines_are_default_and_fit_playfield(self):
        self.assertEqual(TraceOptions().mode, "dark")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dark.png"
            self._save(path, QColor(255, 255, 255, 255), QColor(0, 0, 0, 255))
            strokes = trace_image(path, TraceOptions(threshold=128, minimum_component=4))
            self.assertTrue(strokes)
            self.assertTrue(all(0 <= x <= 512 and 0 <= y <= 384 for stroke in strokes for x, y in stroke))

    def test_alpha_outline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alpha.png"
            self._save(path, QColor(0, 0, 0, 0), QColor(255, 255, 255, 255))
            strokes = trace_image(path, TraceOptions(mode="alpha", threshold=1, minimum_component=4))
            self.assertTrue(strokes)

    def test_separate_shapes_remain_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "two.png"
            image = QImage(64, 48, QImage.Format_RGBA8888)
            image.fill(QColor(255, 255, 255, 255))
            for y in range(8, 20):
                for x in range(8, 20):
                    image.setPixelColor(x, y, QColor(0, 0, 0, 255))
            for y in range(28, 40):
                for x in range(40, 52):
                    image.setPixelColor(x, y, QColor(0, 0, 0, 255))
            self.assertTrue(image.save(str(path)))
            strokes = trace_image(path, TraceOptions(minimum_component=4))
            self.assertGreaterEqual(len(strokes), 2)


if __name__ == "__main__":
    unittest.main()
