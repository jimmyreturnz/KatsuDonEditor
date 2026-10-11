"""M3 note editing: place/delete via a chart view's tool row.

Covers the TimelineGameplay <-> MainWindow contract (signals in, no
MainWindow reference held by the widget), the four placed note shapes,
multi-view refresh on edit, undo/redo, and a real write-to-disk round trip
so [HitObjects] regeneration (M1) is exercised for genuinely inserted notes,
not just parsed ones.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication

from ui import gui
from ui import chart_views
from osu_io.parser import parse_osu
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _click(view: gui.TimelineGameplay, x: float, button: Qt.MouseButton) -> None:
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(x, 100),
        button, button, Qt.KeyboardModifier.NoModifier,
    )
    view.mousePressEvent(event)


class NoteEditingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")

        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.state = self.window.state

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _chart_view(self) -> gui.TimelineGameplay:
        frame = next(f for f in self.window._editor_views if f.view_type == "chart")
        return frame.chart_view

    # -- default state / isolation from the shared Fancy Arranger timeline --

    def test_fancy_arranger_timeline_has_no_tool_row_and_stays_select(self):
        self.assertEqual(self.window.timeline.tool, "select")
        # Nothing wires note_place_requested for the shared timeline, so
        # emitting it (as a stray keypress/click never could, since its
        # tool never leaves "select") must not mutate the document.
        before = len(self.state.document.hit_objects)
        self.window.timeline.note_place_requested.emit("don", 12345.0, False, False)
        self.assertEqual(len(self.state.document.hit_objects), before)

    def test_editor_activation_gives_chart_view_default_tool(self):
        frame = next(f for f in self.window._editor_views if f.view_type == "chart")
        self.assertEqual(frame.chart_view.tool, "select")

    def test_new_chart_view_becomes_the_global_tool_rows_target(self):
        frame = next(f for f in self.window._editor_views if f.view_type == "chart")
        self.assertIs(self.window._active_chart_view, frame.chart_view)
        self.assertTrue(self.window.global_tool_row.isEnabled())

    def test_global_tool_row_drives_the_active_view(self):
        view = self._chart_view()
        self.assertIs(self.window._active_chart_view, view)
        self.window.tool_buttons["kat"].setChecked(True)
        self.assertEqual(view.tool, "kat")
        self.window.new_combo_button.setChecked(True)
        self.assertTrue(view.new_combo)

    # -- placement, one per shape --------------------------------------------

    def test_place_don(self):
        view = self._chart_view()
        before = len(self.state.document.hit_objects)
        view.note_place_requested.emit("don", 12345.0, False, False)
        self.assertEqual(len(self.state.document.hit_objects), before + 1)
        note = next(n for n in self.state.document.hit_objects if n.time == 12345)
        self.assertEqual(note.note_kind, "don")
        self.assertFalse(note.is_new_combo)

    def test_place_kat_with_new_combo(self):
        view = self._chart_view()
        view.note_place_requested.emit("kat", 20000.0, True, False)
        note = next(n for n in self.state.document.hit_objects if n.time == 20000)
        self.assertEqual(note.note_kind, "kat")
        self.assertTrue(note.is_new_combo)

    def test_place_drumroll_and_denden(self):
        view = self._chart_view()
        view.note_place_with_duration_requested.emit("slider", 30000.0, 30500.0, False, False)
        view.note_place_with_duration_requested.emit("spinner", 40000.0, 41000.0, False, False)
        drumroll = next(n for n in self.state.document.hit_objects if n.time == 30000)
        denden = next(n for n in self.state.document.hit_objects if n.time == 40000)
        self.assertEqual(drumroll.note_kind, "drumroll")
        self.assertEqual(denden.note_kind, "denden")
        self.assertEqual(denden.end_time, 41000)

    def test_placed_notes_default_to_playfield_center(self):
        view = self._chart_view()
        view.note_place_requested.emit("don", 5000.0, False, False)
        note = next(n for n in self.state.document.hit_objects if n.time == 5000)
        self.assertEqual((note.x, note.y), (gui.PLAYFIELD_WIDTH // 2, gui.PLAYFIELD_HEIGHT // 2))

    # -- deletion, undo/redo --------------------------------------------------

    def test_delete_via_signal(self):
        view = self._chart_view()
        view.note_place_requested.emit("don", 12345.0, False, False)
        note = next(n for n in self.state.document.hit_objects if n.time == 12345)
        before = len(self.state.document.hit_objects)
        view.note_delete_requested.emit(note.uid)
        self.assertEqual(len(self.state.document.hit_objects), before - 1)
        self.assertNotIn(note.uid, {n.uid for n in self.state.document.hit_objects})

    def test_place_and_delete_are_undoable(self):
        view = self._chart_view()
        before = len(self.state.document.hit_objects)
        view.note_place_requested.emit("don", 12345.0, False, False)
        self.assertTrue(self.state.history.can_undo())
        self.window.undo()
        self.assertEqual(len(self.state.document.hit_objects), before)
        self.window.redo()
        self.assertEqual(len(self.state.document.hit_objects), before + 1)

    # -- real mouse events, not just direct signal emission -------------------

    def test_real_left_click_places_and_right_click_deletes(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 2000.0
        view.current_time = 12000.0
        view.tool = "don"

        before = len(self.state.document.hit_objects)
        _click(view, view.x_for_time(12000.0), Qt.MouseButton.LeftButton)
        self.assertEqual(len(self.state.document.hit_objects), before + 1)

        placed = max(self.state.document.hit_objects, key=lambda n: n.uid)
        view.tool = "select"
        _click(view, view.x_for_time(placed.time), Qt.MouseButton.RightButton)
        self.assertNotIn(placed.uid, {n.uid for n in self.state.document.hit_objects})

    # -- multi-view refresh -----------------------------------------------------

    def test_edit_refreshes_every_open_view_of_the_difficulty(self):
        view = self._chart_view()
        self.window._add_editor_view("chart", self.path)
        second_view = [f for f in self.window._editor_views if f.view_type == "chart"][-1].chart_view

        view.note_place_requested.emit("don", 12345.0, False, False)

        self.assertTrue(any(n.time == 12345 for n in second_view.notes))
        self.assertTrue(any(n.time == 12345 for n in self.window.timeline.notes))
        # The cursor and selection an edit shouldn't disturb.
        self.assertEqual(second_view.selected, set())

    # -- write-to-disk round trip -----------------------------------------------

    def test_placed_notes_survive_a_write_round_trip(self):
        view = self._chart_view()
        view.note_place_requested.emit("don", 5000.0, False, False)
        view.note_place_with_duration_requested.emit("slider", 30000.0, 30500.0, False, False)
        view.note_place_with_duration_requested.emit("spinner", 40000.0, 41000.0, False, False)

        self.window.save_all_states()

        reparsed = parse_osu(self.path)
        times = {n.time for n in reparsed.hit_objects}
        self.assertTrue({5000, 30000, 40000} <= times)
        drumroll = next(n for n in reparsed.hit_objects if n.time == 30000)
        denden = next(n for n in reparsed.hit_objects if n.time == 40000)
        self.assertEqual(drumroll.note_kind, "drumroll")
        self.assertEqual(denden.note_kind, "denden")
        self.assertEqual(denden.end_time, 41000)

    # -- locking disables editing -------------------------------------------

    def test_locking_the_active_view_disables_content_and_global_tool_row(self):
        frame = next(f for f in self.window._editor_views if f.view_type == "chart")
        self.assertIs(self.window._active_chart_view, frame.chart_view)
        frame.lock_button.setChecked(True)
        self.assertTrue(frame.locked)
        self.assertFalse(frame.content.isEnabled())
        self.assertFalse(self.window.global_tool_row.isEnabled())


class EmptyChartTests(unittest.TestCase):
    """A timed difficulty with no notes is what a mapper starts from.

    _ensure_state used to raise on an empty [HitObjects], which _load_map_path
    turned into an "Open failed" dialog -- so the one chart you most need to
    open was the one chart that could not be opened.
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "empty_chart")

        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _chart_view(self) -> gui.TimelineGameplay:
        frame = next(f for f in self.window._editor_views if f.view_type == "chart")
        return frame.chart_view

    def test_the_map_opens_at_all(self):
        self.assertIsNotNone(self.window.state)
        self.assertEqual(self.window.state.source_path, self.path)
        self.assertEqual(self.window.state.document.hit_objects, [])

    def test_the_playhead_starts_where_the_chart_view_does(self):
        # TimelineGameplay.load_document parks at 0 with no notes; the two have
        # to agree or activating the state seeks the audio away from the view.
        self.assertEqual(self.window.state.playhead_ms, 0.0)
        self.assertEqual(self._chart_view().current_time, 0.0)

    def test_the_default_views_open_and_paint(self):
        views = [f for f in self.window._editor_views]
        self.assertTrue(views)
        for frame in views:
            view = getattr(frame, "chart_view", None) or frame.sv_view
            view.resize(600, 180)
            pixmap = QPixmap(view.size())
            view.render(pixmap)
            self.assertFalse(pixmap.isNull(), frame.view_type)

    def test_the_gimmick_page_builds_on_an_empty_chart(self):
        self.window._show_page(gui.PAGE_GIMMICK)
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_GIMMICK)
        for frame in self.window._gimmick_views:
            view = getattr(frame, "chart_view", None) or frame.sv_view
            view.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
            pixmap = QPixmap(view.size())
            view.render(pixmap)
            self.assertFalse(pixmap.isNull(), frame.gimmick_layer)

    def test_the_first_note_can_be_placed_and_saved(self):
        # The whole point: an empty chart exists to be mapped into.
        self._chart_view().note_place_requested.emit("don", 2000.0, False, False)
        self.assertEqual(len(self.window.state.document.hit_objects), 1)

        self.window.save_all_states()
        reparsed = parse_osu(self.path)
        self.assertEqual([n.time for n in reparsed.hit_objects], [2000])
        self.assertEqual(reparsed.hit_objects[0].note_kind, "don")


class ShiftMustBeHeldTests(unittest.TestCase):
    """A tap of Shift used to turn every later note into a finisher.

    The press read `event.modifiers() | QApplication.keyboardModifiers()`, and
    the second half of that is the modifier state as of the last event Qt
    processed -- which sticks when a Shift release lands while the window is
    unfocused or a shortcut eats it. The event's own modifiers cannot go stale,
    so they are now the only thing a click reads.
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")
        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.state = self.window.state
        self.view = next(
            f for f in self.window._editor_views if f.view_type == "chart").chart_view
        self.view.resize(900, 180)
        self.view.window_ms = 4000.0
        self.window._editor_view_focus_changed(None, self.view)

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _place(self, time_ms: int, shift: bool) -> bool:
        before = {note.uid for note in self.state.document.hit_objects}
        self.view.current_time = float(time_ms)
        x = self.view.x_for_time(time_ms)
        modifiers = (
            Qt.KeyboardModifier.ShiftModifier if shift
            else Qt.KeyboardModifier.NoModifier
        )
        for event_type, handler in (
            (QMouseEvent.Type.MouseButtonPress, self.view.mousePressEvent),
            (QMouseEvent.Type.MouseButtonRelease, self.view.mouseReleaseEvent),
        ):
            handler(QMouseEvent(
                event_type, QPointF(x, 90.0), Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton, modifiers,
            ))
        placed = [n for n in self.state.document.hit_objects if n.uid not in before]
        self.assertEqual(len(placed), 1, f"{time_ms}: expected exactly one note")
        return placed[0].is_finisher

    def test_a_click_reads_its_own_shift_and_not_a_stale_one(self):
        """The regression: with the cached global stuck on Shift, every one of
        these came back a finisher."""
        self.view.set_tool("don")
        with patch.object(
            gui.QApplication, "keyboardModifiers",
            staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
        ):
            self.assertFalse(self._place(25000, shift=False))
            self.assertTrue(self._place(25500, shift=True))
            self.assertFalse(self._place(26000, shift=False))

    def test_shift_still_places_a_finisher_don_and_kat(self):
        self.view.set_tool("don")
        self.assertTrue(self._place(27000, shift=True))
        self.view.set_tool("kat")
        self.assertTrue(self._place(27500, shift=True))
        kat = next(n for n in self.state.document.hit_objects if n.time == 27500)
        self.assertTrue(kat.is_kat, "a big kat keeps its clap bit")

    def test_outside_an_event_the_platform_is_asked_not_qts_cache(self):
        """Paint time and hit tests have no event to read. `keyboardModifiers`
        is the state as of the last event Qt processed and is exactly what
        sticks; `queryKeyboardModifiers` asks the platform."""
        with patch.object(
            gui.QApplication, "queryKeyboardModifiers",
            staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
        ):
            self.assertTrue(chart_views.shift_is_held())
        with patch.object(
            gui.QApplication, "queryKeyboardModifiers",
            staticmethod(lambda: Qt.KeyboardModifier.NoModifier),
        ):
            # Stale cache says Shift; the live state says otherwise and wins.
            with patch.object(
                gui.QApplication, "keyboardModifiers",
                staticmethod(lambda: Qt.KeyboardModifier.ShiftModifier),
            ):
                self.assertFalse(chart_views.shift_is_held())

    def test_the_ghost_follows_shift_being_held(self):
        self.view.set_tool("don")
        self.view._hover_time = 28000.0
        radii = {}
        for held in (False, True):
            with patch.object(chart_views, "shift_is_held", staticmethod(lambda h=held: h)):
                pixmap = QPixmap(self.view.size())
                self.view.render(pixmap)
                radii[held] = self.view.note_radii()[1 if held else 0]
        self.assertGreater(radii[True], radii[False])


class NewComboDoesNotLatchTests(unittest.TestCase):
    """Pressing New Combo once used to mark every note placed afterwards.

    It sits outside the tool row's exclusive group, so nothing ever turned it
    off again -- through any number of tool changes, with the flag riding on
    the view. The gimmick row fixed its own version of this (see
    `_set_gimmick_tool`); this one had not.
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")
        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.state = self.window.state
        self.view = next(
            f for f in self.window._editor_views if f.view_type == "chart").chart_view
        self.window._editor_view_focus_changed(None, self.view)

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _place(self, time_ms: int) -> bool:
        self.view.note_place_requested.emit(
            "don", float(time_ms), self.view.new_combo, False)
        note = next(
            n for n in self.state.document.hit_objects if n.time == time_ms)
        return note.is_new_combo

    def test_a_plain_placement_starts_no_combo(self):
        self.assertFalse(self._place(30000))
        self.assertFalse(self.window.new_combo_button.isChecked())

    def test_it_applies_while_it_is_on(self):
        self.window.new_combo_button.setChecked(True)
        self.assertTrue(self._place(30500))

    def test_choosing_another_tool_clears_it(self):
        """The regression, in both halves: the flag on the view and the
        button that is supposed to be showing it."""
        self.window.new_combo_button.setChecked(True)
        self.window.tool_buttons["don"].setChecked(True)
        self.assertFalse(self.view.new_combo)
        self.assertFalse(self.window.new_combo_button.isChecked())
        self.assertFalse(self._place(31000))

    def test_a_run_of_combos_still_works(self):
        """Cleared by a tool change, not by a placement -- several notes that
        each start a combo is one press, not one press per note."""
        self.window.new_combo_button.setChecked(True)
        self.assertTrue(self._place(31500))
        self.assertTrue(self._place(32000))


class GhostRepaintTests(unittest.TestCase):
    def test_changing_the_tool_redraws_the_ghost_under_a_still_cursor(self):
        """Owner, 2026-10-08: picking another object to place over a still
        cursor showed the old one until the mouse moved."""
        for view in (gui.TimelineGameplay(), gui.SVEditorView()):
            repaints = []
            view.update = lambda *args, repaints=repaints: repaints.append(args)
            view.set_tool("kat" if isinstance(view, gui.TimelineGameplay) else "green_line")
            self.assertTrue(repaints, type(view).__name__)


if __name__ == "__main__":
    unittest.main()
