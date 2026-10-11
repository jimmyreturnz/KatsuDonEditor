"""osu!'s editor keys (ui/editor_keys.py), driven by real key presses.

The fixture: notes at 1000 (don), 1500 (big don), 2000 (kat), 2500, 3000,
a drumroll at 3500, a 500ms beat in 4/4, and bookmarks at 2000 and 4000.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from chart.time_axis import wheel_seek_time
from model.hit_object import HITSOUND_CLAP, HITSOUND_FINISH, HITSOUND_WHISTLE, TYPE_NEW_COMBO
from tests.osu_fixtures import write_fixture
from ui import gui
from ui.editor_keys import reversed_times, takes_editor_keys_from, toggled_bits

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class PureRulesTests(unittest.TestCase):
    def test_reverse_mirrors_inside_the_span(self):
        self.assertEqual(reversed_times([100.0, 200.0, 400.0]), [400.0, 300.0, 100.0])
        self.assertEqual(reversed_times([]), [])

    def test_toggle_sets_unless_every_one_has_it(self):
        self.assertEqual(toggled_bits([0, 2], 2), [2, 2])
        self.assertEqual(toggled_bits([2, 10], 2), [0, 8])


class EditorKeysTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")
        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.window.activateWindow()
        QTest.qWaitForWindowActive(self.window, 1000)
        self.view = next(f for f in self.window._editor_views if f.view_type == "chart").chart_view
        self.view.setFocus()
        self.window._editor_keys._set_snap(4)
        self._settle()
        self.window.seek_audio(1000.0)
        self._settle()

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _settle(self) -> None:
        for _ in range(4):
            QApplication.processEvents()

    def _key(self, key, modifiers=Qt.NoModifier, widget=None) -> None:
        QTest.keyClick(widget or self.view, key, modifiers)
        self._settle()

    def _now(self) -> float:
        return self.window.audio_anchor_position

    def _select(self, *times) -> None:
        self.view.selected = {n.original_index for n in self.view.notes if n.time in times}
        self.view.selection_changed.emit(set(self.view.selected))

    def _note_at(self, time):
        return next(n for n in self.window.state.document.hit_objects if n.time == time)

    # -- seeking ---------------------------------------------------------------

    def test_right_steps_one_snap_exactly_as_a_wheel_notch_does(self):
        expected = wheel_seek_time(self.view.snap_points, 1000.0, 1, 4)
        self._key(Qt.Key_Right)
        self.assertEqual(self._now(), expected)
        self._key(Qt.Key_Left)
        self.assertEqual(self._now(), 1000.0)

    def test_shift_steps_four_snaps(self):
        self._key(Qt.Key_Right, Qt.ShiftModifier)
        self.assertEqual(self._now(), 1500.0)

    def test_ctrl_arrows_go_note_to_note(self):
        self._key(Qt.Key_Right, Qt.ControlModifier)
        self.assertEqual(self._now(), 1500.0)
        self._key(Qt.Key_Left, Qt.ControlModifier)
        self.assertEqual(self._now(), 1000.0)

    def test_alt_arrows_go_bookmark_to_bookmark(self):
        self._key(Qt.Key_Right, Qt.AltModifier)
        self.assertEqual(self._now(), 2000.0)
        self._key(Qt.Key_Right, Qt.AltModifier)
        self.assertEqual(self._now(), 4000.0)
        self._key(Qt.Key_Left, Qt.AltModifier)
        self.assertEqual(self._now(), 2000.0)

    def test_shift_digit_sets_the_snap(self):
        # As a synthetic event carries it; a physical Shift+3 arrives as "#"
        # on a US layout, which Qt's Windows key mapper also offers as Shift+3.
        self._key(Qt.Key_3, Qt.ShiftModifier)
        self.assertEqual(self.window.editor_snap_combo.currentData(), 3)
        self.assertEqual(self.view.snap_divisor, 3)

    def test_arrows_belong_to_a_focused_spin_box(self):
        spin = self.window.editor_view_height_spin
        spin.setFocus()
        self._settle()
        before = self._now()
        self._key(Qt.Key_Right, widget=spin)
        self.assertEqual(self._now(), before)
        self.assertTrue(takes_editor_keys_from(spin))
        self.assertFalse(takes_editor_keys_from(self.view))

    # -- editing the selection ---------------------------------------------------

    def test_w_toggles_whistle_as_one_undo_step(self):
        self._select(1000)
        self._key(Qt.Key_W)
        self.assertEqual(self._note_at(1000).note_kind, "kat")
        self._key(Qt.Key_W)
        self.assertEqual(self._note_at(1000).note_kind, "don")
        self.window.undo()
        self.assertEqual(self._note_at(1000).hit_sound, HITSOUND_WHISTLE)

    def test_w_on_a_clap_kat_leaves_it_a_kat(self):
        self._select(2000)
        self._key(Qt.Key_W)
        # The whistle is added on top of the clap, not swapped for it.
        self.assertEqual(self._note_at(2000).hit_sound, HITSOUND_CLAP | HITSOUND_WHISTLE)
        self.assertEqual(self._note_at(2000).note_kind, "kat")

    def test_e_toggles_big_and_q_new_combo(self):
        self._select(1000)
        self._key(Qt.Key_E)
        self.assertTrue(self._note_at(1000).hit_sound & HITSOUND_FINISH)
        self._key(Qt.Key_Q)
        self.assertTrue(self._note_at(1000).type & TYPE_NEW_COMBO)

    def test_nothing_selected_changes_nothing(self):
        before = [(n.time, n.hit_sound, n.type) for n in self.window.state.document.hit_objects]
        self._key(Qt.Key_W)
        after = [(n.time, n.hit_sound, n.type) for n in self.window.state.document.hit_objects]
        self.assertEqual(after, before)

    def test_ctrl_g_reverses_the_selection_in_time(self):
        self._select(1000, 1500, 2000)
        self._key(Qt.Key_G, Qt.ControlModifier)
        self.assertEqual(self._note_at(1000).note_kind, "kat")
        self.assertEqual(self._note_at(1500).note_kind, "big_don")
        self.assertEqual(self._note_at(2000).note_kind, "don")

    def test_ctrl_x_copies_then_removes(self):
        self._select(1000)
        self._key(Qt.Key_X, Qt.ControlModifier)
        self.assertFalse(any(n.time == 1000 for n in self.window.state.document.hit_objects))
        self.assertTrue(self.window._note_clipboard)

    def test_ctrl_d_clones_one_measure_after_the_last_selected(self):
        self._select(1000, 1500)
        self._key(Qt.Key_D, Qt.ControlModifier)
        # 1500 + one 4/4 measure of 500ms beats.
        self.assertEqual(self._note_at(3500).note_kind, "don")
        self.assertEqual(self._note_at(4000).note_kind, "big_don")

    # -- views -------------------------------------------------------------------

    def test_down_and_up_move_focus_between_views(self):
        keys = self.window._editor_keys
        frames = keys._frames()
        if len(frames) < 2:
            self.skipTest("the default Editor layout has one time-axis view")
        frames[0].chart_view.setFocus() if hasattr(frames[0], "chart_view") else None
        self._settle()
        self._key(Qt.Key_Down, widget=QApplication.focusWidget())
        self.assertTrue(frames[1].focused)
        self._key(Qt.Key_Up, widget=QApplication.focusWidget())
        self.assertTrue(frames[0].focused)


if __name__ == "__main__":
    unittest.main()
