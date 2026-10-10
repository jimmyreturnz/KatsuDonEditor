"""Every vertical scroller glides; a value widget inside one keeps its wheel.

Offscreen reports reduced motion, so the glide lands at once and the
positions can be read straight after the event."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from ui.smooth_scroll import SmoothScroller

app = QApplication.instance() or QApplication([])


def wheel(widget, notches: int) -> None:
    event = QWheelEvent(
        QPointF(5, 5), QPointF(widget.mapToGlobal(QPoint(5, 5))), QPoint(0, 0),
        QPoint(0, 120 * notches), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)


class ScrollAreaTests(unittest.TestCase):
    def setUp(self):
        self.area = QScrollArea()
        self.area.resize(200, 200)
        body = QWidget()
        column = QVBoxLayout(body)
        self.spin = QSpinBox()
        column.addWidget(self.spin)
        column.addSpacing(2000)
        self.area.setWidget(body)
        self.scroller = SmoothScroller(self.area)
        self.area.show()
        app.processEvents()

    def tearDown(self):
        self.area.close()

    def test_the_wheel_on_the_viewport_scrolls_down_a_step(self):
        wheel(self.area.viewport(), -1)
        self.assertEqual(self.area.verticalScrollBar().value(), self.scroller._step())

    def test_the_wheel_on_the_scroll_bar_glides_too(self):
        wheel(self.area.verticalScrollBar(), -2)
        self.assertEqual(self.area.verticalScrollBar().value(), 2 * self.scroller._step())

    def test_a_spin_box_inside_keeps_its_wheel(self):
        self.spin.setFocus()
        wheel(self.spin, 1)
        self.assertEqual(self.spin.value(), 1)
        self.assertEqual(self.area.verticalScrollBar().value(), 0)


if __name__ == "__main__":
    unittest.main()
