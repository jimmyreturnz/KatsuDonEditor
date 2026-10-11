import tempfile
import unittest
from pathlib import Path

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from chart.image_to_drawing import TraceOptions, trace_image, trace_loops

_APP = QApplication.instance() or QApplication([])


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

    def test_speck_example_matches_the_tracer(self):
        """Ignore Specks' example fades a speck by `speck_outline_points`; it
        has to be the length trace_loops really gives that square, or the
        example shows a speck surviving that the import drops."""
        from ui.image_trace_dialog import SPECK_SIDES, speck_outline_points
        for side in SPECK_SIDES:
            image = QImage(side + 4, side + 4, QImage.Format_RGBA8888)
            image.fill(QColor(255, 255, 255))
            for y in range(2, side + 2):
                for x in range(2, side + 2):
                    image.setPixelColor(x, y, QColor(0, 0, 0))
            (loop,) = trace_loops(image, TraceOptions())
            self.assertEqual(len(loop), speck_outline_points(side), side)

    def test_dialog_refines_without_retracing(self):
        """Specks and smoothing reuse the outlines; only mode, threshold and
        invert pay for the slow pass again."""
        from ui import image_trace_dialog
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dark.png"
            self._save(path, QColor(255, 255, 255, 255), QColor(0, 0, 0, 255))
            dialog = image_trace_dialog.ImageTraceDialog()
            dialog.open_image(path)
            self.assertTrue(dialog.accepted_strokes)
            self.assertTrue(dialog.import_button.isEnabled())
            loops = dialog._loops
            dialog.minimum.setValue(500)
            self.assertIs(dialog._loops, loops)
            self.assertEqual(dialog.accepted_strokes, [])
            self.assertFalse(dialog.import_button.isEnabled())
            dialog.threshold.setValue(10)
            self.assertIsNone(dialog._loops)
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
