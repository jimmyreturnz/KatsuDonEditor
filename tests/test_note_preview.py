import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QComboBox, QSlider
from PySide6.QtCore import Qt

from ui import gui
from ui.settings_dialog import _NoteOpacityPreview


class NoteOpacityPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _preview(self):
        slider = QSlider(Qt.Horizontal)
        slider.setRange(10, 100)
        slider.setValue(70)
        combo = QComboBox()
        combo.addItem("Built-in", "")
        combo.addItem("Fancy", "Fancy")
        preview = _NoteOpacityPreview(slider, combo)
        preview.resize(400, 64)
        return preview, slider, combo

    def test_the_injected_painter_gets_the_combos_skin_and_the_slider(self):
        preview, slider, combo = self._preview()
        calls = []
        preview.paint_notes = lambda painter, rect, name, percent: calls.append((name, percent))
        preview.grab()
        combo.setCurrentIndex(1)
        slider.setValue(40)
        preview.grab()
        self.assertEqual(calls[0], ("", 70))
        self.assertEqual(calls[-1], ("Fancy", 40))

    def test_a_combo_change_schedules_a_repaint(self):
        preview2, _s, combo2 = self._preview()
        seen = []
        preview2.paint_notes = lambda *a: seen.append(a[2])
        preview2.show()
        self.app.processEvents()
        seen.clear()
        combo2.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(seen, ["Fancy"])
        preview2.close()

    def test_the_shared_drawing_paints_notes_and_a_spinner(self):
        from library.skin import TaikoSkin
        image = QImage(400, 64, QImage.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        gui.paint_note_preview(painter, image.rect(), TaikoSkin(None), 70)
        painter.end()
        # A note column and the spinner band both landed pixels.
        self.assertNotEqual(image.pixelColor(40, 32).alpha(), 0)
        self.assertNotEqual(image.pixelColor(40 + 4 * 62 + 40, 32).alpha(), 0)


if __name__ == "__main__":
    unittest.main()
