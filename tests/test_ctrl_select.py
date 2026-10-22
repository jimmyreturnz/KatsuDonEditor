"""Owner suggestions, 2026-10-22:

- Ctrl+click adds or removes one note from the selection, so the K's of
  `d kdd K d K` can be picked out, copied, or given SV on their own. Ctrl
  still means whole milliseconds for placing and dragging; a click that
  never drags lands nothing, so it is free to select.
- The window reopens at the size it was closed at.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui import gui
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None
CTRL = Qt.KeyboardModifier.ControlModifier
NONE = Qt.KeyboardModifier.NoModifier


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _event(kind, x, y, modifiers, buttons=Qt.MouseButton.LeftButton):
    return QMouseEvent(kind, QPointF(x, y), Qt.MouseButton.LeftButton, buttons, modifiers)


def _click(view, x, y, modifiers=NONE):
    view.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, x, y, modifiers))
    view.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, x, y, modifiers))


class CtrlSelectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")
        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.state = self.window.state
        self.view = next(f for f in self.window._editor_views if f.view_type == "chart").chart_view
        self.view.resize(800, 200)
        self.view.window_ms = 4000.0
        self.view.current_time = 2000.0

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _index(self, time_ms):
        return next(n.original_index for n in self.state.document.hit_objects if n.time == time_ms)

    def _click_note(self, time_ms, modifiers=NONE):
        _click(self.view, self.view.x_for_time(time_ms), self.view._baseline_y(), modifiers)

    def test_ctrl_click_adds_and_removes_one_note(self):
        for time_ms in (1000, 2000, 3000):
            self._click_note(time_ms, CTRL)
        self.assertEqual(self.view.selected, {self._index(t) for t in (1000, 2000, 3000)})
        # On a selected note: only that one goes, not the whole selection the
        # press grabbed for a possible drag.
        self._click_note(2000, CTRL)
        self.assertEqual(self.view.selected, {self._index(1000), self._index(3000)})
        self._click_note(1500)
        self.assertEqual(self.view.selected, {self._index(1500)})

    def test_a_ctrl_band_adds_to_the_selection(self):
        self._click_note(1000)
        y = 5.0  # above the note row: a band, not a grab
        view = self.view
        view.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, view.x_for_time(1900), y, CTRL))
        view.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, view.x_for_time(2600), y, CTRL))
        view.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, view.x_for_time(2600), y, CTRL))
        self.assertEqual(view.selected, {self._index(t) for t in (1000, 2000, 2500)})

    def test_generate_sv_goes_on_the_selected_notes_only(self):
        params = {"placement": "notes"}
        self.assertEqual(
            self.window._sv_generation_times(self.state, 900, 3100, params),
            [1000.0, 1500.0, 2000.0, 2500.0, 3000.0],
        )
        self._click_note(1500, CTRL)
        self._click_note(2500, CTRL)
        self.assertEqual(
            self.window._sv_generation_times(self.state, 900, 3100, params), [1500.0, 2500.0],
        )
        # A selection nowhere in the range is no reason to generate nothing.
        self.assertEqual(
            self.window._sv_generation_times(self.state, 2900, 3100, params), [3000.0],
        )

    def test_close_stores_the_window_geometry(self):
        self.window.close()
        saved = self.window.settings.value("window/geometry")
        self.assertIsInstance(saved, QByteArray)
        self.assertTrue(len(saved))


if __name__ == "__main__":
    unittest.main()
