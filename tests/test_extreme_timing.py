"""Maps whose timing points sit far outside anything a human plays.

Gimmick maps carry BPM values in the millions and, at the other end, sections
so slow that one beat is longer than the song. Both reach the snap grid as a
`beat_length`, and both used to break it *inside* paintEvent -- where Qt
swallows the exception, leaves the frame half-drawn and makes the map look like
it vanished rather than like it crashed.

`_draw_snap_grid` is called directly here for that reason: `view.grab()` cannot
fail these tests, because the exception never reaches the caller.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from ui import gui
from osu_io.timing import TimingPoint

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class SnapGridExtremeBpmTests(unittest.TestCase):
    def _grid(self, beat_length: float, *, divisor: int = 4) -> None:
        view = gui.TimelineGameplay()
        view.resize(800, 200)
        view.timing_points = [
            TimingPoint(time=0.0, beat_length=beat_length),
            TimingPoint(time=6000.0, beat_length=beat_length),
        ]
        view._timing_times = [point.time for point in view.timing_points]
        view.snap_divisor = divisor
        # Deliberately off the timing point: a playhead sitting exactly on one
        # divides zero by the beat length and misses both hazards.
        view.current_time = 2000.0
        pixmap = QPixmap(800, 200)
        painter = QPainter(pixmap)
        try:
            view._draw_snap_grid(painter, 100)
        finally:
            painter.end()

    def test_a_beat_longer_than_the_song_does_not_overflow_drawline(self):
        """0.000006 BPM puts the next tick billions of pixels off-screen, and
        QPainter.drawLine takes a C int."""
        self._grid(1e12)

    def test_an_even_slower_section_does_not_overflow_either(self):
        self._grid(1e15)

    def test_a_million_bpm_does_not_divide_the_window_to_infinity(self):
        """beat_length near zero made (start_time - point.time) // snap_length
        overflow to inf, which int() refuses."""
        self._grid(60000.0 / 1_000_000)
        self._grid(1e-320)

    def test_ordinary_bpm_still_draws(self):
        self._grid(500.0)
        self._grid(500.0, divisor=1)


class SnapGridDensityTests(unittest.TestCase):
    """A grid finer than its own pen is a filled rectangle drawn a line at a time.

    nbt-hwt's hidden anti-barline runs a 12345 BPM wall: at 1/4 over a 2s
    window that is a tick every 1.15px, 1422 of them, 15.3ms of a 16.3ms chart
    paint against an 8.33ms budget. `_draw_snap_grid` coarsens to the finest
    division of the setting that is still wide enough to see.
    """

    def _drawn(self, beat_length: float, *, divisor: int = 4, window_ms: float = 2000.0):
        view = gui.TimelineGameplay()
        view.resize(1882, 180)
        view.timing_points = [
            TimingPoint(time=0.0, beat_length=beat_length),
            TimingPoint(time=60000.0, beat_length=beat_length),
        ]
        view._timing_times = [point.time for point in view.timing_points]
        view.snap_divisor = divisor
        view.window_ms = window_ms
        view.current_time = 20000.0
        recorded: list[float] = []
        original = view.x_for_time

        def spy(time_ms):
            recorded.append(original(time_ms))
            return recorded[-1]

        pixmap = QPixmap(view.size())
        painter = QPainter(pixmap)
        view.x_for_time = spy
        try:
            view._draw_snap_grid(painter, 90)
        finally:
            view.x_for_time = original
            painter.end()
        return view, sorted(recorded)

    def test_a_12345_bpm_wall_is_coarsened_to_what_can_be_seen(self):
        view, xs = self._drawn(60000.0 / 12345.0)
        self.assertTrue(xs, "the grid drew nothing to check")
        gaps = [b - a for a, b in zip(xs, xs[1:]) if b > a]
        self.assertGreaterEqual(min(gaps), view._min_tick_spacing_px)

    def test_an_ordinary_section_keeps_every_division(self):
        """The coarsening is for grids nobody can see, and must not reach a
        1/4 grid at 185 BPM -- 38px a tick, which is the normal case."""
        beat = 60000.0 / 185.0
        _view, xs = self._drawn(beat)
        gaps = [round(b - a, 3) for a, b in zip(xs, xs[1:]) if b > a]
        self.assertAlmostEqual(min(gaps), beat / 4 * 1882 / 2000.0, delta=1.0)


class BarlineGimmickTests(unittest.TestCase):
    """A barline gimmick is thousands of millisecond-long absurd-BPM sections.

    The notes in them travel far too fast to see, which is the point -- the
    barlines are the visualization. So the barline the playhead is sitting on
    has to survive, however many off-screen sections precede it. There is no
    line-count cap to survive any more: the bound is the screen, since two lines
    rounding to the same pixel column are one line.
    """

    def _viewer(self, count: int, *, spacing: float = 5.0, beat_length: float = 0.0001):
        view = gui.GameplayViewerView()
        view.resize(800, 200)
        view.timing_points = [TimingPoint(time=0.0, beat_length=500.0)] + [
            TimingPoint(time=1000.0 + index * spacing, beat_length=beat_length)
            for index in range(count)
        ]
        view.beat_points = [p for p in view.timing_points if p.uninherited and p.beat_length > 0]
        view._beat_times = [point.time for point in view.beat_points]
        view._rebuild_velocities()
        view.current_time = 1000.0 + spacing * (count - 1)
        return view

    def _visible(self, view) -> list[float]:
        times = view.barline_times(*view.visible_time_range())
        # One line per pixel column at most, so the widget's width is the ceiling.
        self.assertLessEqual(len(times), view.width() + 4)
        self.assertEqual(len(times), len(set(times)))
        return [t for t in times if -1.0 <= view.x_for_time(t) <= view.width() + 1.0]

    def test_the_barline_under_the_playhead_survives_thousands_of_sections(self):
        for count in (200, 3000):
            with self.subTest(sections=count):
                view = self._viewer(count)
                self.assertIn(view.current_time, view.barline_times(*view.visible_time_range()))
                self.assertTrue(self._visible(view))

    def test_it_survives_a_million_bpm_gimmick_too(self):
        view = self._viewer(3000, beat_length=60000.0 / 1_000_000)
        self.assertTrue(self._visible(view))

    def test_past_60000_bpm_a_section_still_fills_the_screen_with_its_own_lines(self):
        """The "static" barline effect: one dense section whose measures land a
        few dozen pixels apart, ( | | | | | ). Walking it from the left edge of
        a range sized for the map's slowest section spent the whole budget
        off-screen and drew none of them."""
        view = gui.GameplayViewerView()
        view.resize(800, 200)
        view.timing_points = [
            TimingPoint(time=0.0, beat_length=500.0),
            # 120000 BPM, held for 200ms, with a green line slowing the scroll
            # so its measures spread out instead of blurring past.
            TimingPoint(time=10000.0, beat_length=0.5),
            TimingPoint(time=10000.0, beat_length=-2000.0, uninherited_flag=0),
            TimingPoint(time=10200.0, beat_length=500.0),
        ]
        view.beat_points = [p for p in view.timing_points if p.uninherited and p.beat_length > 0]
        view._beat_times = [point.time for point in view.beat_points]
        view._rebuild_velocities()
        view.current_time = 10100.0
        visible = self._visible(view)
        self.assertGreater(len(visible), 5, "expected a picket fence, got %r" % (visible,))

    def test_an_ordinary_map_still_gets_its_barlines(self):
        view = gui.GameplayViewerView()
        # Wide enough to reach 2s either side at the preview's real scale
        # (pixels per beat follow the height, ~0.63 px/ms here).
        view.resize(3000, 200)
        view.timing_points = [TimingPoint(time=0.0, beat_length=500.0)]
        view.beat_points = list(view.timing_points)
        view._beat_times = [0.0]
        view._rebuild_velocities()
        view.current_time = 2000.0
        # 500ms beat, meter 4 -> a barline every 2000ms.
        self.assertEqual(view.barline_times(0.0, 5000.0), [0.0, 2000.0, 4000.0])


if __name__ == "__main__":
    unittest.main()
