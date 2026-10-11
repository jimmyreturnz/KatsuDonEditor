"""The tap-to-calibrate maths, without a sound card.

`click_error_ms` and `suggest_offset` are module functions rather than dialog
methods precisely so this file can exist: the arithmetic that decides what
number gets written to the user's settings is worth pinning down, and none of
it needs a window.
"""
from __future__ import annotations

import os
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from audio.offset_calibration import (
    CLICK_INTERVAL_MS, LEAD_IN_MS, MAXIMUM_SPREAD_MS, MINIMUM_TAPS,
    SAMPLE_RATE, TRACK_SECONDS, WARMUP_TAPS, OffsetCalibrationDialog,
    click_error_ms, is_tap_key, suggest_offset, write_click_track,
)

_APP = None


def setUpModule() -> None:
    global _APP
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP = QApplication.instance() or QApplication([])


class ClickErrorTests(unittest.TestCase):
    def test_a_tap_exactly_on_a_click_measures_zero(self):
        for index in range(4):
            with self.subTest(index=index):
                on_the_click = LEAD_IN_MS + index * CLICK_INTERVAL_MS
                self.assertEqual(click_error_ms(on_the_click), 0.0)

    def test_a_late_tap_is_positive(self):
        """The sign matters: an uncompensated output latency makes the sound
        arrive after the position says it did, so the taps land late and the
        offset that corrects it is positive."""
        self.assertAlmostEqual(click_error_ms(LEAD_IN_MS + 30), 30.0)

    def test_an_early_tap_is_negative(self):
        self.assertAlmostEqual(
            click_error_ms(LEAD_IN_MS + CLICK_INTERVAL_MS - 20), -20.0)

    def test_a_tap_is_attributed_to_the_nearest_click_either_side(self):
        just_after = LEAD_IN_MS + CLICK_INTERVAL_MS + 10
        just_before = LEAD_IN_MS + CLICK_INTERVAL_MS - 10
        self.assertAlmostEqual(click_error_ms(just_after), 10.0)
        self.assertAlmostEqual(click_error_ms(just_before), -10.0)

    def test_the_worst_case_error_is_half_an_interval(self):
        """Nothing can be misattributed to the wrong click, because there is
        nowhere further from a click to tap."""
        for offset in range(0, CLICK_INTERVAL_MS * 3, 7):
            self.assertLessEqual(
                abs(click_error_ms(LEAD_IN_MS + offset)), CLICK_INTERVAL_MS / 2)


class SuggestionTests(unittest.TestCase):
    def test_too_few_taps_suggests_nothing(self):
        offset, _spread, reason = suggest_offset([30.0] * (MINIMUM_TAPS - 1))
        self.assertIsNone(offset)
        self.assertEqual(reason, "more")

    def test_the_warmup_taps_do_not_count_toward_the_answer(self):
        """The first taps are someone finding the beat. Counting them would let
        two wild ones drag a calibration that is otherwise consistent."""
        errors = [400.0] * WARMUP_TAPS + [25.0] * MINIMUM_TAPS
        offset, spread, _reason = suggest_offset(errors)
        self.assertEqual(offset, 25.0)
        self.assertEqual(spread, 0.0)

    def test_an_inconsistent_run_is_refused_rather_than_averaged(self):
        """Quaver gates on the same thing. A calibration nobody could tap
        consistently is not a number worth writing into settings."""
        errors = [0.0] * WARMUP_TAPS + [
            (-1) ** i * MAXIMUM_SPREAD_MS * 3 for i in range(MINIMUM_TAPS)]
        offset, spread, reason = suggest_offset(errors)
        self.assertIsNone(offset)
        self.assertEqual(reason, "spread")
        self.assertGreater(spread, MAXIMUM_SPREAD_MS)

    def test_one_missed_tap_does_not_move_the_answer(self):
        """Median, not mean, and a robust spread to match: a single fumbled tap
        in an otherwise steady run would shift a mean by several milliseconds,
        and a standard-deviation gate would reject the run outright. The whole
        point of this dialog is milliseconds."""
        errors = [0.0] * WARMUP_TAPS + [40.0] * MINIMUM_TAPS + [-200.0]
        offset, spread, reason = suggest_offset(errors)
        self.assertEqual(reason, "")
        self.assertEqual(offset, 40.0)
        self.assertLess(spread, MAXIMUM_SPREAD_MS)


class ClickTrackTests(unittest.TestCase):
    def test_the_generated_track_is_the_format_the_engine_decodes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "click.wav"
            write_click_track(path)
            with wave.open(str(path), "rb") as handle:
                self.assertEqual(handle.getnchannels(), 2)
                self.assertEqual(handle.getsampwidth(), 2)
                self.assertEqual(handle.getframerate(), SAMPLE_RATE)
                self.assertEqual(handle.getnframes(), SAMPLE_RATE * TRACK_SECONDS)

    def test_the_lead_in_is_silent_so_the_first_click_is_the_first_sound(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "click.wav"
            write_click_track(path)
            with wave.open(str(path), "rb") as handle:
                lead_in = handle.readframes(int(SAMPLE_RATE * LEAD_IN_MS / 1000))
            self.assertEqual(set(lead_in), {0})


class TapKeyTests(unittest.TestCase):
    def test_any_ordinary_key_taps(self):
        for key in (Qt.Key_Space, Qt.Key_K, Qt.Key_1, Qt.Key_Return, Qt.Key_F):
            with self.subTest(key=key):
                self.assertTrue(is_tap_key(key))

    def test_escape_and_the_bare_modifiers_do_not(self):
        for key in (Qt.Key_Escape, Qt.Key_Shift, Qt.Key_Control, Qt.Key_Tab):
            with self.subTest(key=key):
                self.assertFalse(is_tap_key(key))


class DialogKeyTests(unittest.TestCase):
    """The bug this pins: the spacebar paused the click track instead of
    tapping, because clicking Play focused the Play button and a focused
    QPushButton activates itself on Space -- before the dialog's own key
    handling ever runs."""

    def setUp(self) -> None:
        self.dialog = OffsetCalibrationDialog()
        self.dialog.show()
        QApplication.processEvents()

    def tearDown(self) -> None:
        self.dialog.done(0)

    def test_no_button_can_hold_focus(self):
        for button in self.dialog.findChildren(QPushButton):
            with self.subTest(button=button.text()):
                self.assertEqual(button.focusPolicy(), Qt.NoFocus)

    def test_space_taps_instead_of_pausing_after_play_was_clicked(self):
        QTest.mouseClick(self.dialog.play_button, Qt.LeftButton)
        QApplication.processEvents()
        playing = self.dialog.play_button.text()
        QTest.keyClick(self.dialog, Qt.Key_Space)
        QApplication.processEvents()
        self.assertEqual(len(self.dialog._errors), 1)
        self.assertEqual(self.dialog.play_button.text(), playing)

    def test_the_dialog_holds_focus_when_opened_from_a_focused_button(self):
        """Every button is NoFocus, so unless the dialog takes focus itself on
        show, nothing has it and taps went nowhere until a click. Opened the
        way Settings opens it -- from a window whose button holds focus --
        because a bare dialog is handed focus by the offscreen platform anyway
        and passes with or without the fix."""
        self.dialog.done(0)
        # Held on self: a collected parent deletes the dialog before tearDown.
        self._parent = parent = QDialog()
        button = QPushButton("Calibrate", parent)
        parent.show()
        button.setFocus()
        QApplication.processEvents()
        self.dialog = OffsetCalibrationDialog(parent)
        self.dialog.show()
        QApplication.processEvents()
        self.assertIs(QApplication.focusWidget(), self.dialog)

    def test_a_tap_before_play_is_ignored(self):
        """Nothing is being measured yet, and a position of zero would read as
        a wildly early tap."""
        QTest.keyClick(self.dialog, Qt.Key_Space)
        self.assertEqual(self.dialog._errors, [])


if __name__ == "__main__":
    unittest.main()
