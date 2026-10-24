"""README backlog #5: an object keeps its own snap, and Resnap puts strays back.

A note on a 1/3 grid dragged with 1/4 selected used to land on 1/4 at the
first pixel of the drag; it now moves along 1/3. Resnap moves an object that is
a millisecond or two off its own grid and leaves everything else alone.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui import gui
from osu_io.timing import TimingPoint
from tests.osu_fixtures import write_fixture
from chart.time_axis import own_divisor, resnap_time

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


# 120 BPM from 0: a 1/3 line at 333.33ms is written by osu! as 333.
POINTS = [TimingPoint(time=0.0, beat_length=500.0)]


class OwnDivisorTests(unittest.TestCase):
    def test_coarsest_grid_wins(self):
        self.assertEqual(own_divisor(POINTS, 1000), 1)
        self.assertEqual(own_divisor(POINTS, 1250), 2)
        self.assertEqual(own_divisor(POINTS, 1125), 4)

    def test_a_truncated_third_is_a_third(self):
        self.assertEqual(own_divisor(POINTS, 333), 3)

    def test_off_every_grid_is_none(self):
        self.assertIsNone(own_divisor(POINTS, 1050))

    def test_a_one_millisecond_beat_says_nothing(self):
        """A 60000 BPM gimmick line puts a gridline on every millisecond, so
        every object would 'match' it; that is not information."""
        self.assertIsNone(own_divisor([TimingPoint(time=0.0, beat_length=1.0)], 7))


class ResnapTimeTests(unittest.TestCase):
    def test_one_and_two_milliseconds_off_come_back(self):
        self.assertEqual(resnap_time(POINTS, 1001), 1000.0)
        self.assertEqual(resnap_time(POINTS, 335), 333.0)

    def test_on_the_grid_stays(self):
        self.assertEqual(resnap_time(POINTS, 333), 333.0)

    def test_deliberately_off_is_left_alone(self):
        self.assertIsNone(resnap_time(POINTS, 1050))


class _Window(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        path = write_fixture(directory, "full_v14")
        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(path, refresh_difficulties=True)
        self.state = self.window.state
        self.view = next(f for f in self.window._editor_views if f.view_type == "chart").chart_view
        self.view.resize(1200, 200)
        self.view.window_ms = 4000.0
        self.view.current_time = 1500.0

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _note_at(self, time_ms: int):
        return next(n for n in self.state.document.hit_objects if n.time == time_ms)


class OwnGridDragTests(_Window):
    def _drag(self, note, to_ms: float) -> float:
        self.view.set_snap_divisor(4)
        x0 = self.view.x_for_time(note.time)
        x1 = x0 + self.view.x_for_time(to_ms) - self.view.x_for_time(note.time)
        self.view._begin_move(note, None, x0)
        self.view._update_move(x1)
        delta = self.view._move_delta
        self.view.releaseMouse()
        return note.time + delta

    def test_a_third_lands_on_the_current_snap(self):
        """Owner, 2026-10-24: a note on another grid moved along it, so
        changing the snap could never move a note onto the new one."""
        note = self._note_at(1500)
        note.time = 1333
        # The nearest 1/4 line to 1640 is 1625; the nearest 1/3 line is 1666.67.
        self.assertEqual(self._drag(note, 1640), 1625)

    def test_a_note_on_the_current_grid_moves_as_before(self):
        """1000 is on 1/1, which 1/4 contains: the setting governs, or a note on
        a beat could only ever move by whole beats."""
        self.assertEqual(self._drag(self._note_at(1000), 1640), 1625)


class ResnapCommandTests(_Window):
    def setUp(self) -> None:
        super().setUp()
        self.window._active_chart_view = self.view

    def test_resnap_fixes_strays_and_is_one_undo_step(self):
        stray = self._note_at(1000)
        stray.time = 1001
        fake = next(n for n in self.state.document.hit_objects if self.window.is_fake_slider(n))
        fake_time = fake.time
        steps = len(self.state.history.undo_stack)

        self.window.resnap_selection()

        self.assertEqual(stray.time, 1000)
        self.assertEqual(fake.time, fake_time, "a fake slider lives off the grid on purpose")
        self.assertEqual(len(self.state.history.undo_stack), steps + 1)
        self.window.undo()
        self.assertEqual(stray.time, 1001)

    def test_never_lands_on_an_occupied_millisecond(self):
        stray = self._note_at(1500)
        stray.time = 1001  # 1000 already holds a note
        self.window.resnap_selection()
        self.assertEqual(stray.time, 1001)

    def test_only_the_selection_when_there_is_one(self):
        first, second = self._note_at(1000), self._note_at(2000)
        first.time, second.time = 1001, 2001
        self.view.selected = {second.original_index}
        self.window.resnap_selection()
        self.assertEqual((first.time, second.time), (1001, 2000))


if __name__ == "__main__":
    unittest.main()
