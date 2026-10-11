"""The shared time axis: the snap list, and what a seek does to the playhead."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from osu_io.timing import TimingPoint
from chart.time_axis import SNAP_DIVISORS, TimeAxisMixin, snap_time

# wheelEvent reads QApplication.keyboardModifiers(); without an instance that
# call returns nothing useful and the Shift case silently stops being tested.
_APP = QApplication.instance() or QApplication([])


class _Axis(TimeAxisMixin):
    """The smallest host `set_time` needs: it only repaints and reads a clock."""

    def __init__(self, current_time: float = 0.0) -> None:
        self.current_time = current_time
        self.last_rendered_time = -1.0
        self.updates = 0

    def update(self) -> None:
        self.updates += 1


class SnapDivisorTests(unittest.TestCase):
    def test_the_list_is_sorted_so_alt_wheel_steps_one_way(self):
        self.assertEqual(list(SNAP_DIVISORS), sorted(SNAP_DIVISORS))

    def test_one_sixty_fourth_is_offered(self):
        self.assertIn(64, SNAP_DIVISORS)

    def test_a_sixty_fourth_of_a_beat_is_where_it_should_be(self):
        points = [TimingPoint.uninherited_at(0, 180.0)]
        # 180 BPM is a 333.33ms beat, so 1/64 of one is 5.208ms.
        self.assertAlmostEqual(snap_time(points, 5.2, 64), 60000.0 / 180.0 / 64)


class PlayheadPrecisionTests(unittest.TestCase):
    """A snap is rarely a whole millisecond. The playhead is carried as a float
    from the view that moved it out to every other view, so a view adopts what
    it is handed rather than keeping a fraction of its own -- keeping its own
    was what left the six gimmick layers disagreeing about where "now" is, each
    by a different fraction, which at deep zoom is a visible offset per band."""

    def test_a_fractional_position_is_kept(self):
        axis = _Axis(10000.0)
        axis.set_time(10000.4)
        self.assertEqual(axis.current_time, 10000.4)

    def test_a_view_does_not_keep_a_fraction_of_its_own(self):
        axis = _Axis(10000.4)
        axis.set_time(10000.0)
        self.assertEqual(axis.current_time, 10000.0)

    def test_a_real_move_still_lands(self):
        axis = _Axis(10000.4)
        axis.set_time(10333)
        self.assertEqual(axis.current_time, 10333.0)

    def test_playback_still_advances_the_playhead(self):
        axis = _Axis(0.0)
        for position in range(0, 100, 7):
            axis.set_time(position)
        self.assertEqual(axis.current_time, 98.0)

    def test_a_negative_position_is_still_clamped(self):
        axis = _Axis(500.0)
        axis.set_time(-20)
        self.assertEqual(axis.current_time, 0.0)


if __name__ == "__main__":
    unittest.main()


class _Point:
    def __init__(self, x=0, y=0):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y

    def isNull(self):
        return self._x == 0 and self._y == 0


class _Wheel:
    """Enough of QWheelEvent for `TimeAxisMixin.wheelEvent`, so the real method
    runs rather than a copy of the one line under test."""

    def __init__(self, delta=120, modifiers=Qt.KeyboardModifier.NoModifier):
        self._angle = _Point(0, delta)
        self._modifiers = modifiers
        self.accepted = False

    def angleDelta(self):
        return self._angle

    def pixelDelta(self):
        return _Point(0, 0)

    def modifiers(self):
        return self._modifiers

    def accept(self):
        self.accepted = True


class _WheelAxis(_Axis):
    """`wheelEvent` also emits and repaints; both are recorded, not stubbed
    away, so a change that stops seeking fails here too."""

    def __init__(self) -> None:
        super().__init__(0.0)
        self.window_ms = 2000.0
        self.snap_divisor = 16
        self.seeks: list[float] = []
        self.seek_requested = SimpleNamespace(emit=self.seeks.append)
        self.snap_changed_by_wheel = SimpleNamespace(emit=lambda _d: None)
        self.zoom_changed = SimpleNamespace(emit=lambda _w: None)
        self.wheel_accumulator = 0.0
        # 120 BPM from zero, so a 1/1 step is 500ms and a 1/16 step is 31.25ms.
        # `snap_points` is a property over these two.
        self.base_timing = [TimingPoint(time=0.0, beat_length=500.0, meter=4)]
        self.timing_points = self.base_timing


class WheelStepWhilePlayingTests(unittest.TestCase):
    """A wheel notch moves a whole beat while the song is running.

    At a fine snap a notch is a few milliseconds, so following along during
    playback meant spinning the wheel while the music ran away -- scrolling
    during playback is looking around, not editing. The snap divisor governs
    again the moment playback stops, because that is what you edit on.
    """

    def _step(self, playing: bool, modifiers=Qt.KeyboardModifier.NoModifier) -> float:
        axis = _WheelAxis()
        axis.is_playing = playing
        axis.wheelEvent(_Wheel(delta=-120, modifiers=modifiers))
        self.assertEqual(len(axis.seeks), 1)
        return axis.seeks[0]

    def test_playing_steps_a_whole_beat(self):
        self.assertAlmostEqual(self._step(playing=True), 500.0, places=3)

    def test_shift_steps_a_whole_beat_while_playing(self):
        self.assertAlmostEqual(
            self._step(playing=True, modifiers=Qt.KeyboardModifier.ShiftModifier),
            500.0, places=3)

    def test_paused_steps_the_snap_divisor(self):
        # 31 rather than 31.25: a snapped millisecond is truncated, not
        # rounded, because osu!stable casts a fractional beat position to int.
        self.assertAlmostEqual(self._step(playing=False), 31.0, places=3)

    def test_shift_still_steps_a_beat_while_paused(self):
        self.assertAlmostEqual(
            self._step(playing=False, modifiers=Qt.KeyboardModifier.ShiftModifier),
            500.0, places=3)

    def test_every_view_has_the_flag_without_setting_it(self):
        """A class attribute on the mixin, so a view that never had one still
        answers -- the SV and gimmick pages scroll their own views and none of
        their constructors knows about playback."""
        self.assertFalse(TimeAxisMixin.is_playing)
