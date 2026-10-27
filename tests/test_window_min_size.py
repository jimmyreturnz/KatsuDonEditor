"""Only the page on show sets how small the window can go.

QStackedLayout takes the largest minimum of every page, hidden ones included,
so every page was held at the Fancy page's 1338px and the window could not be
snapped to half a 1080p screen. Pixel targets are measured on the real
platform (`tools/measure_window_fit.py`): offscreen draws tofu glyphs, so
widths here are not the real ones -- but which page they come from is.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from ui import gui

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _minimum(widget):
    return widget.minimumSizeHint().expandedTo(widget.minimumSize())


class StackFollowsCurrentPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = gui.MainWindow()
        self.window.show()

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_stack_minimum_is_the_current_pages_own(self):
        stack = self.window.page_stack
        for index in range(stack.count()):
            with self.subTest(page=index):
                stack.setCurrentIndex(index)
                QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
                self.assertEqual(_minimum(stack), _minimum(stack.widget(index)))

    def test_a_narrow_page_is_not_held_open_by_a_wide_one(self):
        """Proves the test above can fail: the pages differ, so a stack that
        took the largest would not equal the library's own minimum."""
        stack = self.window.page_stack
        stack.setCurrentIndex(gui.PAGE_MISC)
        QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        misc = _minimum(stack).width()
        stack.setCurrentIndex(gui.PAGE_LIBRARY)
        QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        self.assertNotEqual(misc, _minimum(stack).width())


if __name__ == "__main__":
    unittest.main()
