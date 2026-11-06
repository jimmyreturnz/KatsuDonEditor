"""Bookmarks from the timing bar's bookmark toolbar and Ctrl+B / Ctrl+Shift+B (owner, 2026-11-04)."""
from __future__ import annotations

import unittest

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QEnterEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui.chart_views import editor_timeline_metadata
from tests.test_editing_ux_round3 import WindowTestCase


def setUpModule() -> None:
    QApplication.instance() or QApplication([])


class BookmarkTests(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.bar = self.window.timing_bar
        self.bar.set_duration(100000)
        # The layout sizes the bar on the first event pass; a resize() here
        # would be undone by it mid-click.
        QApplication.processEvents()

    def _bookmarks(self):
        return editor_timeline_metadata(self.state.document)[0]

    def _x(self, ms: float) -> int:
        return round(ms / self.bar.duration_ms * self.bar.width())

    def _click(self, x: int, y: int) -> None:
        QTest.mouseMove(self.bar, QPoint(x, y))
        QTest.mouseClick(self.bar, Qt.LeftButton, Qt.NoModifier, QPoint(x, y))

    def _button(self, index: int):
        from PySide6.QtWidgets import QPushButton
        return self.bar.bookmark_toolbar().findChildren(QPushButton)[index]

    def test_the_toolbar_floats_over_the_bars_left_end_and_moves_nothing(self):
        """Owner, 2026-11-04: osu!'s row, fixed just above the bar, shown on
        hover and outside every layout."""
        self.window.resize(1600, 900); self.window.show(); QApplication.processEvents()
        geometry = self.bar.geometry()
        QApplication.sendEvent(self.bar, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        toolbar = self.bar.bookmark_toolbar()
        self.assertTrue(toolbar.isVisible())
        self.assertIs(toolbar.parentWidget(), self.window)
        corner = self.bar.mapTo(self.window, QPoint(0, 0))
        self.assertEqual(toolbar.x(), corner.x())
        self.assertLess(toolbar.geometry().bottom(), corner.y(), "above the bar, not over it")
        self.assertEqual(self.bar.geometry(), geometry)

    def test_the_toolbar_goes_the_moment_the_cursor_leaves_it(self):
        """Owner, 2026-11-04: no lingering. It sits flush on the bar, so the
        cursor can cross from one to the other without it closing."""
        from unittest.mock import patch
        self.window.resize(1600, 900); self.window.show(); QApplication.processEvents()
        QApplication.sendEvent(self.bar, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        toolbar = self.bar.bookmark_toolbar()
        self.assertEqual(toolbar.geometry().bottom() + 1, self.bar.mapTo(self.window, QPoint(0, 0)).y())
        on_toolbar = toolbar.mapToGlobal(QPoint(5, 5))
        with patch("ui.chart_views.QCursor.pos", return_value=on_toolbar):
            QApplication.sendEvent(self.bar, QEvent(QEvent.Leave))
        self.assertTrue(toolbar.isVisible(), "crossing onto the row keeps it")
        away = self.window.mapToGlobal(QPoint(800, 5))
        with patch("ui.chart_views.QCursor.pos", return_value=away):
            QApplication.sendEvent(toolbar, QEvent(QEvent.Leave))
        self.assertFalse(toolbar.isVisible(), "gone at once, no timer")

    def test_the_toolbar_goes_with_the_page(self):
        """Owner, 2026-11-05: back to song select with the cursor still on
        the bar, the row stayed floating over the library. It is the
        window's child, so it needs telling."""
        from ui import gui
        self.window.resize(1600, 900); self.window.show()
        self.window._show_page(gui.PAGE_EDITOR); QApplication.processEvents()
        QApplication.sendEvent(self.bar, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
        toolbar = self.bar.bookmark_toolbar()
        self.assertTrue(toolbar.isVisible())
        self.window._show_page(gui.PAGE_LIBRARY); QApplication.processEvents()
        self.assertFalse(toolbar.isVisible())

    def test_plus_adds_at_the_playhead_snapped_and_undo_takes_it_away(self):
        before = self._bookmarks()
        self.window.timeline.current_time = 37700.0
        self._button(0).click()
        expected = self.window._snap_bookmark_ms(37700.0)
        self.assertEqual(self._bookmarks(), sorted({*before, expected}))
        self.assertIn(expected, self.bar.bookmarks, "the bar shows it at once")
        self.window.undo()
        self.assertEqual(self._bookmarks(), before)

    def test_reset_removes_every_bookmark(self):
        self.window._set_bookmarks(self.state, [1000, 50000])
        self._button(4).click()
        self.assertEqual(self._bookmarks(), [])
        self.assertFalse(any(line.startswith("Bookmarks:") for line in self.state.document.lines),
                         "no empty Bookmarks: line, as osu! writes none")

    def test_previous_and_next_seek_to_the_neighbouring_bookmarks(self):
        self.window._set_bookmarks(self.state, [1000, 5000, 9000])
        seeks = []
        self.bar.seek_requested.connect(seeks.append)
        self.bar.set_time(5000)
        self._button(2).click()
        self.bar.set_time(5000)
        self._button(3).click()
        self.assertEqual(seeks, [1000, 9000])

    def test_a_click_on_the_bar_still_seeks(self):
        before = self._bookmarks()
        seeks = []
        self.bar.seek_requested.connect(seeks.append)
        self._click(self._x(30000), 14)
        self.assertEqual(self._bookmarks(), before)
        self.assertTrue(seeks)

    def test_ctrl_shift_b_removes_the_nearest_on_either_side(self):
        self.window._set_bookmarks(self.state, [1000, 5000, 9000])
        self.window.timeline.current_time = 6000.0
        self.window._bookmark_at_playhead(False)
        self.assertEqual(self._bookmarks(), [1000, 9000])
        self.window.timeline.current_time = 8000.0
        self.window._bookmark_at_playhead(False)
        self.assertEqual(self._bookmarks(), [1000])

    def test_ctrl_b_adds_at_the_playhead_snapped(self):
        self.window._set_bookmarks(self.state, [])
        self.window.timeline.current_time = 1234.5
        self.window._bookmark_at_playhead(True)
        self.assertEqual(self._bookmarks(), [self.window._snap_bookmark_ms(1234.5)])


if __name__ == "__main__":
    unittest.main()
