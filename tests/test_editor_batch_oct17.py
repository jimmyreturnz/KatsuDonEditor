"""The owner's 2026-10-17 batch: osu!'s Z/X/C, a view's header selecting it,
the Generate SV dialog's type-and-Enter workflow, the mods chip off song
select, and a spinner grabbable along its whole band.

Fixture: `full_v14` -- notes at 1000..3500, a spinner 5000-7000.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from app.settings import SettingsManager
from osu_io.timing import TimingPoint
from tests.osu_fixtures import write_fixture
from ui import gui

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _settle() -> None:
    for _ in range(4):
        QApplication.processEvents()


class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.directory = Path(self._temp.name)
        (self.directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(self.directory, "full_v14")
        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.window.activateWindow()
        QTest.qWaitForWindowActive(self.window, 1000)
        self.view = next(f for f in self.window._editor_views if f.view_type == "chart").chart_view
        self.view.setFocus()
        _settle()
        self.window.seek_audio(2000.0)
        _settle()
        self.toggles: list[int] = []
        self.window.toggle_playback = lambda: self.toggles.append(1)

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _key(self, key) -> None:
        QTest.keyClick(self.view, key)
        _settle()

    # -- Z / X / C ---------------------------------------------------------------

    def test_z_seeks_to_the_first_note(self):
        self._key(Qt.Key_Z)
        self.assertEqual(self.window.audio_anchor_position, 1000.0)
        self.assertEqual(self.toggles, [])

    def test_x_goes_to_zero_and_plays(self):
        self._key(Qt.Key_X)
        self.assertEqual(self.window.audio_anchor_position, 0.0)
        self.assertEqual(self.toggles, [1])

    def test_c_is_play_pause(self):
        self._key(Qt.Key_C)
        self.assertEqual(self.toggles, [1])

    # -- the header ----------------------------------------------------------------

    def test_header_press_focuses_its_view(self):
        frame = next(f for f in self.window._editor_views if f.view_type == "sv")
        self.assertFalse(frame.focused)
        QTest.mousePress(frame.header, Qt.LeftButton)
        QTest.mouseRelease(frame.header, Qt.LeftButton)
        _settle()
        self.assertTrue(frame.focused)

    # -- mods ----------------------------------------------------------------------

    def test_mods_chip_is_off_song_select(self):
        self.window._set_mods({"HD"})
        self.window._show_page(gui.PAGE_LIBRARY)
        self.assertTrue(self.window.mods_chip.isHidden())
        self.window._show_page(gui.PAGE_EDITOR)
        self.assertFalse(self.window.mods_chip.isHidden())
        self.window._set_mods(())

    def test_a_different_difficulty_from_song_select_clears_mods(self):
        self.window._set_mods({"HD"})
        self.window._show_page(gui.PAGE_LIBRARY)
        self.window._load_map_path(self.path)
        self.assertEqual(self.window._mods, frozenset({"HD"}))
        other = self.directory / "other.osu"
        shutil.copy(self.path, other)
        self.window._show_page(gui.PAGE_LIBRARY)
        self.window._load_map_path(other.resolve())
        self.assertEqual(self.window._mods, frozenset())

    # -- spinner -------------------------------------------------------------------

    def test_spinner_band_is_grabbable(self):
        self.window.seek_audio(6000.0)
        _settle()
        middle = self.view.x_for_time(6000.0)
        note = self.view._note_near_x(middle)
        self.assertIsNotNone(note)
        self.assertTrue(note.is_spinner)
        y = self.view.note_center_y(note, self.view._baseline_y(), self.view.note_radii()[1])
        self.assertTrue(self.view._press_is_on_note_body(note, middle, y))

    # -- Generate SV -----------------------------------------------------------------

    def _green(self, time, sv):
        point = TimingPoint.inherited_at(time, sv)
        self.window.state.document.timing_points.append(point)
        return point

    def test_prefill_reads_the_lines_inside_the_selection(self):
        # svselection1: each note's line 5ms ahead of it, the drag starting
        # just before the first -- the line before the run (2.0) is not it.
        self._green(800, 2.0)
        self._green(995, 2.7)
        self._green(1495, 2.0)
        self.window.state.document.timing_points.sort(key=lambda point: point.time)
        seen = {}

        class Spy(gui.SVFunctionDialog):
            def exec(inner):
                seen["initial"] = inner.initial_rate_spin.value()
                seen["final"] = inner.final_rate_spin.value()
                return QDialog.Rejected

        original = gui.SVFunctionDialog
        gui.SVFunctionDialog = Spy
        try:
            self.window._open_sv_function_dialog(self.path, 990.0, 1700.0)
        finally:
            gui.SVFunctionDialog = original
        self.assertAlmostEqual(seen["initial"], 2.7)
        self.assertAlmostEqual(seen["final"], 2.0)

    def test_replace_existing_clears_the_range_in_one_step(self):
        stray = self._green(1250, 3.0)
        params = {
            "initial_rate": 1.0, "final_rate": 1.0, "placement": "notes", "snap_divisor": 4,
            "position_offset": -5, "omit_barline": False, "relative_to_final_bpm": False,
            "function": "linear", "oscillate": "", "replace_existing": True,
        }
        state = self.window.state
        self.window._generate_sv(self.path, 900.0, 2100.0, params)
        uids = {point.uid for point in state.document.timing_points}
        self.assertNotIn(stray.uid, uids)
        state.history.undo(state)
        self.assertIn(stray.uid, {point.uid for point in state.document.timing_points})


class DialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SettingsManager()
        for key in ("multiply_initial", "multiply_final", "remember_curve", "curve",
                    "scroll_step", "initial_factor", "final_factor"):
            self.settings._settings.remove(f"sv_generate/{key}")

    def _dialog(self, **kwargs):
        dialog = gui.SVFunctionDialog(1000.0, 2000.0, settings=self.settings, **kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_typing_a_rate_and_enter_generates(self):
        dialog = self._dialog(initial_rate=1.0, final_rate=1.0)
        dialog.show()
        dialog.activateWindow()
        QTest.qWaitForWindowActive(dialog, 1000)
        _settle()
        self.assertTrue(dialog.initial_rate_spin.hasFocus())
        QTest.keyClicks(dialog.initial_rate_spin, "2.5")
        QTest.keyClick(dialog.initial_rate_spin, Qt.Key_Return)
        _settle()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertAlmostEqual(dialog.parameters()["initial_rate"], 2.5)

    def test_scroll_step_defaults_to_a_hundredth_and_is_kept(self):
        dialog = self._dialog()
        self.assertAlmostEqual(dialog.initial_rate_spin.singleStep(), 0.01)
        dialog.scroll_step_spin.setValue(0.1)
        self.assertAlmostEqual(dialog.final_rate_spin.singleStep(), 0.1)
        self.assertAlmostEqual(self._dialog().initial_rate_spin.singleStep(), 0.1)

    def test_multiplier_scales_the_prefill_and_toggles_back(self):
        self.settings.set_value("sv_generate/multiply_final", True)
        self.settings.set_value("sv_generate/final_factor", 1.2)
        dialog = self._dialog(initial_rate=2.0, final_rate=2.0)
        self.assertAlmostEqual(dialog.initial_rate_spin.value(), 2.0)
        self.assertAlmostEqual(dialog.final_rate_spin.value(), 2.4)
        dialog.multiply_final_check.setChecked(False)
        self.assertAlmostEqual(dialog.final_rate_spin.value(), 2.0)
        dialog.multiply_initial_check.setChecked(True)
        self.assertAlmostEqual(dialog.initial_rate_spin.value(), 2.2)
        dialog.initial_factor_spin.setValue(1.5)
        self.assertAlmostEqual(dialog.initial_rate_spin.value(), 3.0)

    def test_remember_last_curve(self):
        dialog = self._dialog()
        dialog.remember_curve_check.setChecked(True)
        dialog.set_selected_function("sin_out")
        dialog.accept()
        self.assertEqual(self._dialog().selected_function(), "sin_out")


if __name__ == "__main__":
    unittest.main()
