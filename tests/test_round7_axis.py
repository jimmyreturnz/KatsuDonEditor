"""Round 7: millisecond-exact snapping, the kiai flash surviving a gimmick
underneath it, and the refusal toast.

Kept in its own file (rather than folded into test_time_axis.py or
test_kiai_pulse.py) so it runs on its own in seconds -- see
gimmick-qt-tests-run-per-class in memory for why a shared file with the
heavier gimmick suites is worth avoiding.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ui import gui
from osu_io.timing import uninherited_points
from osu_io.parser import parse_osu
from library import skin
from chart import time_axis
from model.hit_object import TYPE_SPINNER, HitObject
from osu_io.timing import TimingPoint
from chart.time_axis import TimeAxisMixin, snap_time, wheel_seek_time

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _Axis(TimeAxisMixin):
    """The smallest host `snap_ms` needs -- see test_time_axis.py's own."""

    def __init__(self, timing_points, snap_divisor: int = 4, current_time: float = 0.0) -> None:
        self.current_time = current_time
        self.timing_points = timing_points
        self.snap_divisor = snap_divisor
        self.last_rendered_time = -1.0

    def update(self) -> None:
        pass


class SnapMsTests(unittest.TestCase):
    def setUp(self) -> None:
        # 185 BPM: a 1/4 snap is 81.081081...ms, never a whole millisecond.
        self.points = [TimingPoint.uninherited_at(0, 185.0)]

    def test_it_rounds_to_a_whole_millisecond(self) -> None:
        axis = _Axis(self.points, snap_divisor=4)
        result = axis.snap_ms(81.081081)
        self.assertEqual(result, 81.0)
        self.assertEqual(result, round(result))

    def test_it_is_clamped_at_zero(self) -> None:
        axis = _Axis(self.points, snap_divisor=4)
        self.assertEqual(axis.snap_ms(-500.0), 0.0)

    def test_ctrl_bypasses_the_grid_and_just_rounds(self) -> None:
        axis = _Axis(self.points, snap_divisor=4)
        # Off the grid on purpose: nearest 1/4 division (162.162...) is not
        # what a plain round(200.7) gives (201), so the two branches disagree
        # and the test actually distinguishes them.
        grid_result = axis.snap_ms(200.7)
        self.assertEqual(grid_result, round(snap_time(self.points, 200.7, 4)))
        with patch.object(time_axis.QApplication, "keyboardModifiers", return_value=Qt.ControlModifier):
            ctrl_result = axis.snap_ms(200.7)
        self.assertEqual(ctrl_result, 201.0)
        self.assertNotEqual(ctrl_result, grid_result)


class WheelSeekTests(unittest.TestCase):
    def test_a_wheel_notch_lands_on_a_whole_millisecond(self) -> None:
        points = [TimingPoint.uninherited_at(0, 185.0)]
        result = wheel_seek_time(points, 100.0, 1, 4)
        self.assertEqual(result, round(result))
        # 60000/185/4 = 81.081081...ms per 1/4 division; one snap step past
        # the nearest gridline to 100.0 lands at 162.162162...ms, rounded.
        self.assertEqual(result, 162.0)

    def test_a_notch_stops_on_an_off_grid_red_line_both_ways(self) -> None:
        points = [TimingPoint.uninherited_at(0, 120.0), TimingPoint.uninherited_at(1237, 120.0)]
        # 1/4 at 120 BPM is 125ms: the old grid runs 1000, 1125, 1250...
        self.assertEqual(wheel_seek_time(points, 1125.0, 1, 4), 1237.0)
        self.assertEqual(wheel_seek_time(points, 1237.0, 1, 4), 1362.0)
        self.assertEqual(wheel_seek_time(points, 1362.0, -1, 4), 1237.0)
        # Back off the line lands on the *previous* section's grid.
        self.assertEqual(wheel_seek_time(points, 1237.0, -1, 4), 1125.0)


class BeatPulseWalkBackTests(unittest.TestCase):
    """A gimmick section's beat_length must not blank the flash for every
    note downstream of it -- only the section actually active decides that,
    and here that section is the real one behind the gimmick line."""

    def test_it_still_pulses_behind_a_60000_bpm_point(self) -> None:
        points = [
            TimingPoint(time=0.0, beat_length=500.0),
            # A gimmick line, e.g. an invisible-note section: nearest to
            # 2100.0, but not a "beat" by KIAI_PULSE_MIN_BEAT_MS's own rule.
            TimingPoint(time=2000.0, beat_length=1.0),
        ]
        pulse = gui.beat_pulse(points, 2100.0)
        # Walked back to the 500ms section: 1.0 - (2100/500 % 1.0) = 0.8.
        self.assertAlmostEqual(pulse, 0.8, places=9)

    def test_it_returns_zero_with_no_real_section_behind_it(self) -> None:
        points = [TimingPoint(time=0.0, beat_length=1.0)]
        self.assertEqual(gui.beat_pulse(points, 2100.0), 0.0)


class SnapDirectionTests(unittest.TestCase):
    """A snapped millisecond goes **down**, which is what osu! writes.

    Measured over 25 installed maps: on a clean single-BPM map every circle
    sits in `(-1, 0]` of its exact fractional beat position and never above it
    -- a flat spread with nothing on the positive side, which is truncation and
    cannot be rounding. `Hiyashi 2014` has a 315.789ms beat and 114 notes a
    full 0.9ms below their own gridline.

    Rounding put every note whose beat position had a fraction of a half or
    more one millisecond above the one osu! writes for the same snap, which is
    what "the snaps are one off" was.
    """

    def test_a_beat_position_truncates(self):
        self.assertEqual(time_axis.osu_snap_ms(268616.5), 268616)
        self.assertEqual(time_axis.osu_snap_ms(268616.999), 268616)
        self.assertEqual(time_axis.osu_snap_ms(268617.0), 268617)

    def test_the_cursor_still_goes_to_the_nearest(self):
        """Ctrl placement is not a beat position -- it is the millisecond the
        pointer is over, and nearest is what a pointer means."""
        self.assertEqual(time_axis.osu_round(268616.5), 268617)
        self.assertEqual(time_axis.osu_round(268616.4), 268616)

    def test_a_snapped_note_matches_what_osu_wrote_for_the_same_snap(self):
        """The whole point, against a real map rather than a constructed one:
        re-snapping every note of a hand-mapped difficulty has to give back the
        milliseconds already in the file."""
        source = Path(r"D:/osu!/Songs/1208497 Hiyashi 2014")
        if not source.is_dir():
            self.skipTest("the reference map is not installed")
        document = parse_osu(next(source.glob("*[[]Oni[]].osu")))
        points = uninherited_points(document.timing_points)
        divisors = (1, 2, 3, 4, 6, 8, 12, 16)
        off = []
        for note in document.hit_objects:
            if not note.is_circle:
                continue
            # The divisor the mapper used is not in the file, so take whichever
            # one puts the note closest to a gridline -- an unsnapped note has
            # no answer here and is skipped rather than counted as a failure.
            best = min(
                (time_axis.snap_time(points, note.time, d) for d in divisors),
                key=lambda exact: abs(note.time - exact),
            )
            if abs(note.time - best) >= 1.0:
                continue
            if time_axis.osu_snap_ms(best) != note.time:
                off.append((note.time, best))
        self.assertEqual(off[:5], [], f"{len(off)} notes re-snap somewhere else")


class GridTickTests(unittest.TestCase):
    """The grid must be drawn where an object can actually land.

    osu! stores whole milliseconds; a 1/4 snap at 185 BPM is 81.081081ms. Draw
    the tick at the exact beat and a note placed on it renders up to half a
    millisecond away from its own gridline -- ~30 pixels at the 20ms zoom
    floor. So `_draw_snap_grid` walks in exact beats but draws on `round()`.
    """

    def _view(self) -> gui.TimelineGameplay:
        view = gui.TimelineGameplay()
        view.resize(1200, 180)
        view.timing_points = [TimingPoint.uninherited_at(0, 185.0)]
        view.snap_divisor = 4
        view.current_time = 10000.0
        view.window_ms = 20.0
        return view

    def _drawn_tick_times(self, view) -> list[float]:
        """Every time `_draw_snap_grid` actually converts into an x position."""
        recorded: list[float] = []
        original = view.x_for_time

        def spy(time_ms):
            recorded.append(time_ms)
            return original(time_ms)

        pixmap = QPixmap(view.size())
        painter = QPainter(pixmap)
        view.x_for_time = spy
        try:
            view._draw_snap_grid(painter, view.height() // 2)
        finally:
            view.x_for_time = original
            painter.end()
        return recorded

    def test_every_drawn_tick_is_a_whole_millisecond(self):
        drawn = self._drawn_tick_times(self._view())
        self.assertTrue(drawn, "the grid drew nothing to check")
        self.assertEqual([t for t in drawn if t != int(t)], [])

    def test_the_underlying_beat_grid_is_still_fractional(self):
        """Only the drawn position is rounded -- rounding the walk itself
        would accumulate error across a section."""
        points = [TimingPoint.uninherited_at(0, 185.0)]
        self.assertNotEqual(time_axis.snap_time(points, 10000.0, 4) % 1.0, 0.0)

    def test_a_note_on_a_snap_lands_on_a_drawn_tick(self):
        """The whole point: what the editor writes and what it draws agree."""
        view = self._view()
        placed = view.snap_ms(10000.0)
        self.assertIn(placed, self._drawn_tick_times(view))

    def test_the_grid_restarts_on_an_off_grid_red_line(self):
        """A red line placed between snaps to time a beat the grid missed: the
        ticks after it follow *it*, whichever section the window opened in."""
        view = gui.TimelineGameplay()
        view.resize(1200, 180)
        view.timing_points = [
            TimingPoint.uninherited_at(0, 120.0),     # 500ms beat
            TimingPoint.uninherited_at(1237, 120.0),  # same BPM, off every 1/4 snap
        ]
        view._timing_times = [point.time for point in view.timing_points]
        view.snap_divisor = 1
        view.window_ms = 2000.0
        for centre in (1200.0, 1900.0, 2400.0):
            view.current_time = centre
            drawn = [t for t in self._drawn_tick_times(view) if t >= 1237]
            self.assertTrue(drawn, f"no ticks after the red line at {centre}")
            self.assertEqual([t for t in drawn if (t - 1237) % 500], [], f"at {centre}")
            if centre - 1000 < 1237:
                # The line's own downbeat, which the restart once stepped over.
                self.assertEqual(drawn[0], 1237, f"at {centre}")

    def test_the_grid_reaches_back_before_the_first_red_line(self):
        """A map's first red line is rarely at 0 (Himeringo's Oni: 2629), and a
        view opening before it drew nothing to its left -- the restart above
        took the walk's very first section for a line it had just crossed."""
        view = gui.TimelineGameplay()
        view.resize(1200, 180)
        view.timing_points = [TimingPoint.uninherited_at(2629, 120.0)]
        view._timing_times = [point.time for point in view.timing_points]
        view.snap_divisor = 1
        view.window_ms = 4000.0
        view.current_time = 2629.0
        before = [t for t in self._drawn_tick_times(view) if t < 2629]
        self.assertTrue(before, "no ticks before the first red line")
        self.assertEqual([t for t in before if (2629 - t) % 500], [])


class CursorReadoutTests(unittest.TestCase):
    """The millisecond under the cursor, shown as a number.

    At the 20ms zoom floor you can see that two objects differ and nothing
    said by how much, or where the next click would land."""

    def _view(self):
        view = gui.TimelineGameplay()
        view.resize(600, 180)
        view.timing_points = [TimingPoint.uninherited_at(0, 185.0)]
        view.snap_divisor = 4
        view.current_time = 10000.0
        view.window_ms = 20.0
        return view

    def _drawn(self, view, hovered):
        texts = []
        original = QPainter.drawText

        def spy(self_painter, *args):
            if args and isinstance(args[-1], str):
                texts.append(args[-1])
            return None

        pixmap = QPixmap(view.size())
        painter = QPainter(pixmap)
        with patch.object(QPainter, "drawText", spy), \
                patch.object(gui.TimelineGameplay, "underMouse", return_value=hovered):
            view.draw_cursor_position(painter)
        painter.end()
        return texts

    def test_it_shows_the_millisecond_a_click_would_use(self):
        view = self._view()
        view._hover_time = 10003.4
        self.assertEqual(self._drawn(view, True), [f"{int(view.snap_ms(10003.4))} ms"])

    def test_nothing_is_drawn_with_the_cursor_away(self):
        view = self._view()
        view._hover_time = 10003.4
        self.assertEqual(self._drawn(view, False), [])

    def test_nothing_is_drawn_before_the_cursor_has_ever_been_in(self):
        view = self._view()
        self.assertIsNone(view._hover_time)
        self.assertEqual(self._drawn(view, True), [])

    def test_ctrl_shows_the_raw_millisecond_instead_of_the_snap(self):
        view = self._view()
        view._hover_time = 10003.4
        with patch.object(time_axis.QApplication, "keyboardModifiers",
                          return_value=Qt.ControlModifier):
            self.assertEqual(self._drawn(view, True), ["10003 ms"])


class SelectionOutlineTests(unittest.TestCase):
    """A selected note is outlined **with a skin loaded too**.

    Only the unskinned path bakes the pen into its cached sprite, so with a
    skin the outline was dropped and a selected note was indistinguishable
    from an unselected one.
    """

    def _view(self, skinned):
        view = gui.TimelineGameplay()
        view.resize(600, 180)
        view.timing_points = [TimingPoint.uninherited_at(0, 180.0)]
        view.current_time = 1000.0
        view.window_ms = 2000.0
        note = HitObject(x=256, y=192, time=1000, type=1, hit_sound=0)
        note.original_index = 0
        view.notes = [note]
        view.note_times = [1000.0]
        if skinned:
            for name in ("taikohitcircle", "taikohitcircleoverlay"):
                image = QImage(64, 64, QImage.Format_ARGB32)
                image.fill(QColor("white"))
                image.save(str(self._folder / f"{name}.png"))
            view.skin = skin.TaikoSkin(self._folder)
        return view

    def _outline_pixels(self, skinned, selected):
        view = self._view(skinned)
        if selected:
            view.selected = {0}
        image = view.grab().toImage()
        wanted = view.selected_note_pen.color()
        found = 0
        for y in range(image.height()):
            for x in range(image.width()):
                pixel = image.pixelColor(x, y)
                if (abs(pixel.red() - wanted.red()) < 25
                        and abs(pixel.green() - wanted.green()) < 25
                        and abs(pixel.blue() - wanted.blue()) < 25):
                    found += 1
        view.close()
        return found

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._folder = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_a_skinned_note_is_outlined_when_selected(self):
        self.assertGreater(self._outline_pixels(True, selected=True), 0)

    def test_a_skinned_note_is_not_outlined_otherwise(self):
        """A skin's circle has its own rim; ringing every note would draw a
        selection that is not there."""
        self.assertEqual(self._outline_pixels(True, selected=False), 0)

    def test_the_unskinned_path_is_unchanged(self):
        self.assertGreater(self._outline_pixels(False, selected=True), 0)
        self.assertEqual(self._outline_pixels(False, selected=False), 0)


class NoteGrabAreaTests(unittest.TestCase):
    """A note is grabbable across the circle you can see, not a fixed 20px
    around its centre -- which left the outer two thirds of every note looking
    draggable and refusing to be."""

    def _view(self, finisher=False):
        view = gui.TimelineGameplay()
        view.resize(900, 180)
        view.timing_points = [TimingPoint.uninherited_at(0, 180.0)]
        view.current_time = 1000.0
        view.window_ms = 4000.0
        view.notes = [HitObject(x=256, y=192, time=1000, type=1,
                                hit_sound=4 if finisher else 0)]
        view.note_times = [1000.0]
        return view

    def test_the_reach_is_the_drawn_radius(self):
        """At two heights, so it is the drawn circle being tested and not a
        constant that happens to match it. The reach used to be a flat 20px
        against a note drawn up to 31, which is what left the outer two thirds
        of every note looking draggable; the sizes have since been halved, so
        pinning a number here would only re-fix them."""
        view = self._view()
        try:
            for height in (180, 320):
                view.resize(900, height)
                normal, _finisher = view.note_radii()
                centre = view.x_for_time(1000.0)
                self.assertIsNotNone(view._note_near_x(centre + normal - 1.0), height)
                self.assertIsNone(view._note_near_x(centre + normal + 1.0), height)
        finally:
            view.close()

    def test_a_finisher_is_grabbable_over_its_bigger_body(self):
        view = self._view(finisher=True)
        normal, finisher = view.note_radii()
        self.assertGreater(finisher, normal)
        centre = view.x_for_time(1000.0)
        try:
            self.assertIsNotNone(view._note_near_x(centre + normal + 1.0))
            self.assertIsNone(view._note_near_x(centre + finisher + 1.0))
        finally:
            view.close()

    def test_an_explicit_radius_still_wins(self):
        view = self._view()
        centre = view.x_for_time(1000.0)
        try:
            self.assertIsNone(view._note_near_x(centre + 10.0, radius_px=5.0))
        finally:
            view.close()


class TimingBarMarkerColourTests(unittest.TestCase):
    """A pixel column carrying both an uninherited and an inherited point is
    yellow. The paint loop draws green after red, so before this the red one
    was overpainted and a BPM change read as a plain SV change."""

    def _kinds(self, kinds, width=100, duration=100000):
        bar = gui.TimingOverviewBar()
        bar.duration_ms = duration
        bar.resize(width, 28)
        bar._marker_kinds = kinds
        bar._marker_cache_key = None
        try:
            return dict(bar._marker_line_positions())
        finally:
            bar.close()

    def test_red_and_green_in_one_column_become_yellow(self):
        # 1000ms apart on a 100s bar at 100px wide: the same pixel.
        self.assertEqual(self._kinds([(1000, "red"), (1400, "green")]), {1: "yellow"})

    def test_a_column_of_one_kind_keeps_its_colour(self):
        self.assertEqual(self._kinds([(1000, "red"), (1400, "red")]), {1: "red"})


class TimingBarScrubTests(unittest.TestCase):
    """Right-click the page's timeline bar mid-drag to jump the playhead
    without ending the selection."""

    def setUp(self) -> None:
        self.bar = gui.TimingOverviewBar()
        self.bar.duration_ms = 100000
        self.bar.resize(600, 28)
        self.bar.show()

        self.view = gui.TimelineGameplay()
        self.view.resize(600, 180)
        self.view.timing_points = [TimingPoint.uninherited_at(0, 180.0)]
        self.view.current_time = 1000.0
        self.view.window_ms = 2000.0
        self.view.timing_bar = self.bar
        self.view.show()
        self.seeks: list[float] = []
        self.view.seek_requested.connect(self.seeks.append)

    def tearDown(self) -> None:
        self.view.close()
        self.bar.close()

    def _press(self, button, local, global_pos):
        return QMouseEvent(
            QEvent.MouseButtonPress, QPointF(local), QPointF(global_pos),
            button, button, Qt.NoModifier,
        )

    def _start_drag(self):
        self.view.tool = "select"
        self.view.mousePressEvent(self._press(
            Qt.LeftButton, QPointF(100.0, 90.0),
            self.view.mapToGlobal(QPointF(100.0, 90.0)),
        ))

    def test_a_right_press_on_the_bar_scrubs_instead_of_deleting(self):
        self._start_drag()
        anchor = self.view.drag_anchor_time
        self.assertIsNotNone(anchor, "the drag never started")

        target = self.bar.mapToGlobal(QPointF(300.0, 14.0))
        self.view.mousePressEvent(self._press(Qt.RightButton, QPointF(-50.0, 90.0), target))

        # Halfway along a 100s bar.
        self.assertAlmostEqual(self.view.current_time, 50000.0, delta=200.0)
        self.assertTrue(self.seeks, "the playhead was not moved")
        # ...and the selection is still live, anchored where it started.
        self.assertEqual(self.view.drag_anchor_time, anchor)
        self.assertTrue(self.view._bar_scrubbing)

    def test_a_right_press_inside_the_view_still_means_delete(self):
        """Only a press landing on the bar scrubs; the view's own right-click
        behaviour is untouched."""
        self._start_drag()
        deleted: list[int] = []
        self.view.note_delete_requested.connect(deleted.append)
        self.view.notes = [
            HitObject(x=256, y=192, time=1000, type=1, hit_sound=0),
        ]
        self.view.note_times = [1000]
        inside = self.view.mapToGlobal(QPointF(300.0, 90.0))
        self.view.mousePressEvent(self._press(Qt.RightButton, QPointF(300.0, 90.0), inside))
        self.assertFalse(self.view._bar_scrubbing)

    def test_a_press_on_the_bar_without_a_drag_does_not_scrub(self):
        """The gesture only exists to extend a live selection."""
        target = self.bar.mapToGlobal(QPointF(300.0, 14.0))
        self.view.mousePressEvent(self._press(Qt.RightButton, QPointF(-50.0, 90.0), target))
        self.assertFalse(self.view._bar_scrubbing)


class ToastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = gui.MainWindow()
        self.window.show()

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()

    def test_it_shows_holds_then_fades_out(self) -> None:
        self.window.show_toast("Nothing to do.")
        self.assertTrue(self.window._toast.isVisible())
        self.assertEqual(self.window._toast.text(), "Nothing to do.")
        # Click-through: a refusal must never eat the click that follows it.
        self.assertTrue(self.window._toast.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertTrue(self.window._toast_hide_timer.isActive())

        # Simulate the 3-second hold elapsing without waiting on it for real.
        self.window._start_toast_fade()
        self.assertTrue(self.window._toast.isVisible())

        # ...and let the fade finish. Driven to its end rather than waited on
        # with qWait: a wall-clock wait is only long enough while nothing else
        # is competing for the event loop, and this test failed exactly when it
        # ran after one that leaves a 120Hz frame timer behind. What is being
        # asserted is that finishing hides the label, not how fast Qt gets
        # there.
        fade = self.window._toast_fade
        fade.setCurrentTime(fade.duration())
        QApplication.processEvents()
        self.assertFalse(self.window._toast.isVisible())

    def test_a_second_call_replaces_rather_than_stacks(self) -> None:
        self.window.show_toast("First")
        self.window._start_toast_fade()
        self.window.show_toast("Second")
        # Restarted, not stacked: one label, fully opaque again, new text.
        self.assertEqual(self.window._toast.text(), "Second")
        self.assertEqual(self.window._toast_opacity.opacity(), 1.0)
        self.assertTrue(self.window._toast.isVisible())


class GimmickToolRowWidthTests(unittest.TestCase):
    """The fake-slider layer's nine tools plus Config must not push the
    assembled row wider than a monitor -- see _build_gimmick_row."""

    def setUp(self) -> None:
        self.window = gui.MainWindow()
        self.window.show()

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()

    def test_the_widest_row_stays_under_a_sane_bound(self) -> None:
        row = self.window.gimmick_tool_rows["fake_slider"]
        width = row.sizeHint().width()
        # Not a tight pixel count -- the widest label ("Multiple Fake Slider")
        # is shared across all six layers' buttons by design (see
        # _gimmick_row_buttons), so shrinking the font only shrinks that
        # shared width, it cannot make ten equalized buttons narrower than
        # ten times the widest one. 3200px is loose enough to survive a
        # translation change but tight enough to catch the padding/font-size
        # override being lost and the row falling back to the app-wide
        # 8px/16px button padding (measured ~3474px unshrunk vs. ~2798px
        # with it applied).
        self.assertLess(width, 3200)


class SelectToolTailResizeTests(unittest.TestCase):
    """A slider/spinner's tail must be grabbable with Select, not only with
    the tool that placed it -- see _extendable_note_near_edge."""

    def _view(self):
        view = gui.TimelineGameplay()
        view.resize(1200, 180)
        view.timing_points = [TimingPoint.uninherited_at(0, 180.0)]
        view.current_time = 5000.0
        view.window_ms = 20000.0
        view.tool = "select"
        # A spinner rather than a slider: its end time is a plain field
        # (see HitObject.end_time), so the test doesn't also have to pin down
        # duration_for_slider_length's SV/BPM arithmetic to know where the
        # tail lands.
        note = HitObject(x=256, y=192, time=1000, type=TYPE_SPINNER, hit_sound=0, extras=("5000",))
        view.notes = [note]
        view.note_times = [note.time]
        return view, note

    def _press(self, x: float) -> QMouseEvent:
        return QMouseEvent(
            QEvent.MouseButtonPress, QPointF(x, 90.0), QPointF(x, 90.0),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )

    def test_a_press_near_the_tail_begins_a_resize(self) -> None:
        view, note = self._view()
        tail_x = view.x_for_time(5000.0)
        view.mousePressEvent(self._press(tail_x))
        self.assertIs(view._resizing_note, note)

    def test_a_press_on_the_head_does_not_resize(self) -> None:
        view, _note = self._view()
        head_x = view.x_for_time(1000.0)
        view.mousePressEvent(self._press(head_x))
        self.assertIsNone(view._resizing_note)
        # Falls through to the existing select behaviour instead -- a
        # rubber-band drag starts (move_enabled is off by default, so this is
        # not a move either).
        self.assertIsNotNone(view.drag_anchor_time)


class CtrlPrecisionLineTests(unittest.TestCase):
    """The Ctrl-precision hover line (draw_ctrl_precision_line) must show
    exactly where Ctrl-precision would place, and only while it applies."""

    def _view(self):
        view = gui.TimelineGameplay()
        view.resize(1200, 180)
        view.current_time = 5000.0
        view.window_ms = 20000.0
        view._hover_time = 5123.456
        return view

    def test_drawn_at_the_rounded_millisecond_when_ctrl_is_held_inside_the_view(self) -> None:
        view = self._view()
        view.underMouse = lambda: True
        painter = MagicMock()
        with patch.object(time_axis.QApplication, "keyboardModifiers", return_value=Qt.ControlModifier):
            view.draw_ctrl_precision_line(painter)
        painter.drawLine.assert_called_once()
        top, bottom = painter.drawLine.call_args[0]
        expected_x = view.x_for_time(round(view._hover_time))
        self.assertAlmostEqual(top.x(), expected_x)
        self.assertAlmostEqual(bottom.x(), expected_x)
        self.assertEqual(top.y(), 0)
        self.assertEqual(bottom.y(), view.height())

    def test_not_drawn_without_ctrl(self) -> None:
        view = self._view()
        view.underMouse = lambda: True
        painter = MagicMock()
        with patch.object(time_axis.QApplication, "keyboardModifiers", return_value=Qt.NoModifier):
            view.draw_ctrl_precision_line(painter)
        painter.drawLine.assert_not_called()

    def test_not_drawn_when_the_cursor_is_outside_the_view(self) -> None:
        """A hover time can go stale after the cursor leaves (nothing clears
        it), so this must gate on underMouse(), not on _hover_time alone."""
        view = self._view()
        view.underMouse = lambda: False
        painter = MagicMock()
        with patch.object(time_axis.QApplication, "keyboardModifiers", return_value=Qt.ControlModifier):
            view.draw_ctrl_precision_line(painter)
        painter.drawLine.assert_not_called()


class ShiftPreviewRepaintTests(unittest.TestCase):
    """Shift must resize the placement preview the instant it is pressed or
    released, even with the cursor stationary -- see MainWindow.eventFilter."""

    def setUp(self) -> None:
        self.window = gui.MainWindow()
        self.window.show()
        # self.timeline (Fancy Arranger) rather than an Editor-page view: it
        # exists unconditionally at construction, where an Editor chart view
        # only exists once a difficulty is loaded.
        self.window.page_stack.setCurrentIndex(gui.PAGE_FANCY)

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()

    def _shift_event(self, event_type) -> QKeyEvent:
        modifiers = Qt.ShiftModifier if event_type == QEvent.KeyPress else Qt.NoModifier
        return QKeyEvent(event_type, Qt.Key_Shift, modifiers)

    def test_press_repaints_a_hovered_view(self) -> None:
        view = self.window.timeline
        view.underMouse = lambda: True
        with patch.object(view, "update") as spy:
            self.window.eventFilter(view, self._shift_event(QEvent.KeyPress))
        spy.assert_called_once()

    def test_release_also_repaints(self) -> None:
        view = self.window.timeline
        view.underMouse = lambda: True
        with patch.object(view, "update") as spy:
            self.window.eventFilter(view, self._shift_event(QEvent.KeyRelease))
        spy.assert_called_once()

    def test_a_view_the_cursor_is_not_over_is_left_alone(self) -> None:
        view = self.window.timeline
        view.underMouse = lambda: False
        with patch.object(view, "update") as spy:
            self.window.eventFilter(view, self._shift_event(QEvent.KeyPress))
        spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
