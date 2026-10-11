"""SnapSlider stands in for the snap QComboBox on three pages, so it has to
answer the calls those combos got -- and a caller's blockSignals has to keep
meaning a quiet set, which is how the wheel handlers move it."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui import gui

app = QApplication.instance() or QApplication([])


class SnapSliderTests(unittest.TestCase):
    def setUp(self):
        self.snap = gui.SnapSlider(4)
        self.seen = []
        self.snap.currentIndexChanged.connect(self.seen.append)

    def test_it_covers_every_divisor_and_starts_where_asked(self):
        self.assertEqual(self.snap.count(), len(gui.SNAP_DIVISORS))
        self.assertEqual(self.snap.currentData(), 4)
        self.assertEqual(self.snap.value_label.text(), "1/4")

    def test_find_and_set_move_the_value_and_announce_it(self):
        self.snap.setCurrentIndex(self.snap.findData(12))
        self.assertEqual(self.snap.currentData(), 12)
        self.assertEqual(self.seen, [self.snap.findData(12)])
        self.assertEqual(self.snap.findData(17), -1)

    def test_blocked_signals_set_quietly(self):
        self.snap.blockSignals(True)
        self.snap.setCurrentIndex(self.snap.findData(16))
        self.snap.blockSignals(False)
        self.assertEqual(self.snap.currentData(), 16)
        self.assertEqual(self.seen, [])

    def test_dragging_the_slider_is_a_change(self):
        self.snap.slider.setValue(self.snap.findData(8))
        self.assertEqual(self.snap.currentData(), 8)
        self.assertEqual(self.snap.value_label.text(), "1/8")
        self.assertEqual(len(self.seen), 1)


if __name__ == "__main__":
    unittest.main()
