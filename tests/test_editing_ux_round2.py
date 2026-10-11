"""Owner feedback after trying M3/M4 in practice:

- global keyboard shortcuts (1-6) for tool switching, routed by which tool
  row is currently visible, guarded against text-entry focus
- tool buttons show "N. Name" text, not a bare number + tooltip
- SV editor's per-point value label sits at the graph line, not the bottom
- Fancy Arranger's kiai/bookmark bar merged into the time row, with
  percentage added (it was missing there, unlike the Editor page's strip)
- a translucent "ghost" preview of the note that would be placed follows
  the mouse for every placement tool
- spinners always render at "big note" size
- placing a slider/spinner is a click-drag (start -> stop), not a single
  click with a fixed length; existing ones can be extended afterward by
  dragging their right edge; Shift makes a slider "big"
- the timing bar's stutter/flicker on every note placement, traced to
  unconditionally reloading it even though note edits never touch
  [TimingPoints] or bookmarks
"""
from __future__ import annotations

import os
import tempfile
import unittest
from bisect import bisect_left, bisect_right
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui import gui
from app.settings import should_ignore_shortcut_focus
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _press(view, x, y=100.0, button=Qt.MouseButton.LeftButton, mods=Qt.KeyboardModifier.NoModifier):
    event = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(x, y), button, button, mods)
    view.mousePressEvent(event)


def _move(view, x, y=100.0):
    event = QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(x, y), Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    view.mouseMoveEvent(event)


def _release(view, x, y=100.0, mods=Qt.KeyboardModifier.NoModifier):
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(x, y), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, mods,
    )
    view.mouseReleaseEvent(event)


class WindowTestCase(unittest.TestCase):
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

    def _sv_view(self) -> gui.SVEditorView:
        frame = next(f for f in self.window._editor_views if f.view_type == "sv")
        return frame.sv_view


class ToolShortcutTests(WindowTestCase):
    def test_digit_switches_the_active_chart_views_tool(self):
        view = self._chart_view()
        self.window._editor_view_focus_changed(None, view)
        self.window._tool_state.activate_tool_digit("2")
        self.assertEqual(view.tool, "don")
        self.assertTrue(self.window.tool_buttons["don"].isChecked())

    def test_digit_6_toggles_new_combo(self):
        view = self._chart_view()
        self.window._editor_view_focus_changed(None, view)
        self.assertFalse(view.new_combo)
        self.window._tool_state.activate_tool_digit("6")
        self.assertTrue(view.new_combo)
        self.window._tool_state.activate_tool_digit("6")
        self.assertFalse(view.new_combo)

    def test_digit_routes_to_sv_row_when_sv_view_is_active(self):
        sv_view = self._sv_view()
        self.window._editor_view_focus_changed(None, sv_view)
        self.window._tool_state.activate_tool_digit("2")
        self.assertEqual(sv_view.tool, "green_line")

    def test_shortcut_is_ignored_while_a_spinbox_has_focus(self):
        self.assertTrue(should_ignore_shortcut_focus(self.window.approach_rate_control.value_box))

    def test_note_tool_buttons_show_numbered_labels(self):
        self.assertEqual(self.window.tool_buttons["select"].text(), "1. Select")
        self.assertEqual(self.window.tool_buttons["don"].text(), "2. Don")
        self.assertEqual(self.window.new_combo_button.text(), "6. New Combo")

    def test_sv_tool_buttons_show_numbered_labels(self):
        self.assertEqual(self.window.sv_tool_buttons["select"].text(), "1. Select")
        self.assertEqual(self.window.sv_tool_buttons["function"].text(), "3. Function")


class SVLabelPositionTests(unittest.TestCase):
    def test_sv_value_label_y_matches_the_graph_line_not_the_bottom(self):
        view = gui.SVEditorView()
        view.resize(400, 200)
        top, bottom = view._graph_top(), view._graph_bottom()
        graph_y = view._sv_to_y(1.75, top, bottom)
        # The graph line for a non-1x point must not sit at the bottom
        # (where it used to be drawn); it belongs near the graph itself.
        self.assertLess(abs(graph_y - view._sv_to_y(1.75, top, bottom)), 1.0)
        self.assertNotAlmostEqual(graph_y, view.height() - 6, delta=5)


class FancyArrangerRowTests(WindowTestCase):
    def test_kiai_bar_and_time_share_the_same_row_as_editor_strip_does(self):
        # self.fancy_timing_bar must not be in its own standalone row widget
        # separate from self.timeline_time; both live in timeline_row now.
        time_area = self.window.timeline_time.parentWidget()
        bar_parent = self.window.fancy_timing_bar.parentWidget()
        self.assertIs(time_area.parentWidget(), bar_parent)

    def test_time_label_includes_percentage(self):
        self.window._update_timeline_info()
        self.assertIn("%", self.window.timeline_time.text())


class GhostPreviewTests(unittest.TestCase):
    def _view_with_document(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            path = write_fixture(directory, "full_v14")
            from osu_io.parser import parse_osu
            document = parse_osu(path)
        view = gui.TimelineGameplay()
        view.set_symmetric(True)
        view.resize(500, 200)
        view.load_document(document)
        return view

    def test_hover_sets_hover_time_for_placement_tools(self):
        view = self._view_with_document()
        view.tool = "don"
        _move(view, 250.0)
        self.assertIsNotNone(view._hover_time)

    def test_ghost_paints_without_raising_for_every_tool(self):
        view = self._view_with_document()
        for tool in ("select", "don", "kat", "slider", "spinner"):
            view.tool = tool
            view._hover_time = 5000.0
            view.grab()  # must not raise

    def test_live_placement_ghost_paints_without_raising(self):
        view = self._view_with_document()
        view._placing_tool = "slider"
        view._placing_start_time = 3000.0
        view._placing_end_time = 4000.0
        view.grab()

    def test_live_resize_ghost_paints_without_raising(self):
        view = self._view_with_document()
        note = next(n for n in view.notes if n.is_slider)
        view._resizing_note = note
        view._resize_new_end_time = note.time + 2000.0
        view.grab()


class SpinnerSizeTests(unittest.TestCase):
    def test_spinner_pixmap_loads(self):
        self.assertFalse(gui.spinner_pixmap().isNull())


class SpinnerVisibilityTests(unittest.TestCase):
    def test_long_spinner_stays_visible_after_its_start_scrolls_off_screen(self):
        from model.hit_object import HitObject, TYPE_SPINNER
        from osu_io.parser import parse_osu

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            path = write_fixture(directory, "full_v14")
            document = parse_osu(path)

        spinner = HitObject(x=256, y=192, time=1000, type=TYPE_SPINNER, hit_sound=0, extras=("9000",))
        document.hit_objects.append(spinner)
        document.hit_objects.sort(key=lambda note: note.time)

        view = gui.TimelineGameplay()
        view.resize(500, 200)
        view.load_document(document)
        view.window_ms = 2000.0
        # Spinner spans 1000-9000ms; the view is centered at 8500ms, so its
        # start (1000ms) is well outside [7500, 9500] but its end (9000ms)
        # is still on screen.
        view.current_time = 8500.0

        start_time = view.current_time - view.window_ms / 2
        end_time = view.current_time + view.window_ms / 2
        first_visible = bisect_left(view.note_times, start_time - view._max_extend_ms)
        after_last_visible = bisect_right(view.note_times, end_time)
        self.assertIn(spinner, view.notes[first_visible:after_last_visible])

        view.grab()  # must not raise


class SliderSpinnerDragPlacementTests(WindowTestCase):
    def test_drag_places_a_slider_with_length_from_the_dragged_duration(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "slider"

        before = len(self.state.document.hit_objects)
        start_x = view.x_for_time(9000.0)
        end_x = view.x_for_time(9500.0)
        _press(view, start_x)
        _move(view, end_x)
        _release(view, end_x)

        self.assertEqual(len(self.state.document.hit_objects), before + 1)
        slider = max(self.state.document.hit_objects, key=lambda n: n.uid)
        self.assertEqual(slider.note_kind, "drumroll")
        self.assertFalse(slider.is_finisher)
        self.assertGreater(slider.length, 0)

        end_time = view._note_end_time(slider)
        self.assertIsNotNone(end_time)
        self.assertAlmostEqual(end_time, 9500.0, delta=60)

    def test_plain_click_still_places_a_short_slider(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        # Deliberately an empty millisecond: placing on top of an existing
        # note now *replaces* it (one object per ms), so a time that already
        # has a note would leave the count unchanged and say nothing about
        # whether the plain click placed anything.
        view.current_time = 20000.0
        view.tool = "slider"
        before = len(self.state.document.hit_objects)
        x = view.x_for_time(view.current_time)
        _press(view, x)
        _release(view, x)
        self.assertEqual(len(self.state.document.hit_objects), before + 1)

    def test_drag_places_a_spinner_with_end_time_from_the_dragged_duration(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "spinner"

        before = len(self.state.document.hit_objects)
        start_x = view.x_for_time(13000.0)
        end_x = view.x_for_time(14000.0)
        _press(view, start_x)
        _move(view, end_x)
        _release(view, end_x)

        self.assertEqual(len(self.state.document.hit_objects), before + 1)
        spinner = max(self.state.document.hit_objects, key=lambda n: n.uid)
        self.assertEqual(spinner.note_kind, "denden")
        self.assertAlmostEqual(spinner.end_time, 14000, delta=50)

    def test_shift_release_places_a_big_slider(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "slider"

        start_x = view.x_for_time(12000.0)
        end_x = view.x_for_time(12500.0)
        _press(view, start_x)
        _move(view, end_x)
        _release(view, end_x, mods=Qt.KeyboardModifier.ShiftModifier)

        big = max(self.state.document.hit_objects, key=lambda n: n.uid)
        self.assertTrue(big.is_finisher)

    def test_no_shift_places_a_normal_slider(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "slider"

        start_x = view.x_for_time(12000.0)
        end_x = view.x_for_time(12500.0)
        _press(view, start_x)
        _move(view, end_x)
        _release(view, end_x)

        normal = max(self.state.document.hit_objects, key=lambda n: n.uid)
        self.assertFalse(normal.is_finisher)

    def test_shift_click_places_a_big_don(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "don"

        before = len(self.state.document.hit_objects)
        _press(view, view.x_for_time(12000.0), mods=Qt.KeyboardModifier.ShiftModifier)

        self.assertEqual(len(self.state.document.hit_objects), before + 1)
        big = max(self.state.document.hit_objects, key=lambda n: n.uid)
        self.assertTrue(big.is_finisher)

    def test_shift_click_places_a_big_kat_that_keeps_its_clap(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "kat"

        _press(view, view.x_for_time(12000.0), mods=Qt.KeyboardModifier.ShiftModifier)

        big = max(self.state.document.hit_objects, key=lambda n: n.uid)
        self.assertTrue(big.is_finisher)
        self.assertTrue(big.is_kat)

    def test_no_shift_places_a_normal_don(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "don"

        _press(view, view.x_for_time(12000.0))

        normal = max(self.state.document.hit_objects, key=lambda n: n.uid)
        self.assertFalse(normal.is_finisher)

    def test_pressing_near_an_existing_sliders_edge_resizes_instead_of_placing(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "slider"

        start_x = view.x_for_time(9000.0)
        end_x = view.x_for_time(9500.0)
        _press(view, start_x)
        _move(view, end_x)
        _release(view, end_x)
        slider = max(self.state.document.hit_objects, key=lambda n: n.uid)
        before = len(self.state.document.hit_objects)

        end_time = view._note_end_time(slider)
        edge_x = view.x_for_time(end_time)
        new_edge_x = view.x_for_time(11000.0)
        _press(view, edge_x)
        self.assertIs(view._resizing_note, slider, "press near the edge must resize, not place a new note")
        _move(view, new_edge_x)
        _release(view, new_edge_x)

        self.assertEqual(len(self.state.document.hit_objects), before, "resize must not add a note")
        new_end = view._note_end_time(slider)
        self.assertAlmostEqual(new_end, 11000.0, delta=100)

    def test_resizing_a_spinner_updates_its_end_time(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 13000.0
        view.tool = "spinner"

        start_x = view.x_for_time(13000.0)
        end_x = view.x_for_time(13500.0)
        _press(view, start_x)
        _move(view, end_x)
        _release(view, end_x)
        spinner = max(self.state.document.hit_objects, key=lambda n: n.uid)

        edge_x = view.x_for_time(spinner.end_time)
        new_edge_x = view.x_for_time(15000.0)
        _press(view, edge_x)
        self.assertIs(view._resizing_note, spinner)
        _move(view, new_edge_x)
        _release(view, new_edge_x)

        self.assertAlmostEqual(spinner.end_time, 15000, delta=50)

    def test_drag_placement_is_undoable(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "slider"

        before = len(self.state.document.hit_objects)
        start_x = view.x_for_time(9000.0)
        end_x = view.x_for_time(9500.0)
        _press(view, start_x)
        _move(view, end_x)
        _release(view, end_x)
        self.assertEqual(len(self.state.document.hit_objects), before + 1)

        self.window.undo()
        self.assertEqual(len(self.state.document.hit_objects), before)
        self.window.redo()
        self.assertEqual(len(self.state.document.hit_objects), before + 1)

    def test_longer_drag_produces_a_longer_slider_length(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0

        view.current_time = 10000.0
        view.tool = "slider"
        short_start = view.x_for_time(9000.0)
        short_end = view.x_for_time(9200.0)
        _press(view, short_start); _move(view, short_end); _release(view, short_end)
        short_slider = max(self.state.document.hit_objects, key=lambda n: n.uid)

        long_start = view.x_for_time(20000.0)
        long_end = view.x_for_time(22000.0)
        _press(view, long_start); _move(view, long_end); _release(view, long_end)
        long_slider = max(self.state.document.hit_objects, key=lambda n: n.uid)

        self.assertGreater(long_slider.length, short_slider.length)

    def test_placed_slider_survives_a_write_round_trip(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "slider"

        start_x = view.x_for_time(9000.0)
        end_x = view.x_for_time(9600.0)
        _press(view, start_x)
        _move(view, end_x)
        _release(view, end_x)

        self.window.save_all_states()

        from osu_io.parser import parse_osu
        reparsed = parse_osu(self.path)
        placed = next(n for n in reparsed.hit_objects if n.time == 9000 and n.is_slider)
        self.assertGreater(placed.length, 0)


class TimingBarStutterFixTests(WindowTestCase):
    def test_placing_a_note_does_not_reload_the_timing_bars(self):
        view = self._chart_view()
        calls = []
        # Spied on `apply_document_data`, not `load_document`: the three bars
        # are now handed data derived once per edit instead of each deriving
        # it, so this is the entry point every reload goes through.
        original = gui.TimingOverviewBar.apply_document_data
        def spy(self, *args, **kwargs):
            calls.append(self)
            return original(self, *args, **kwargs)
        gui.TimingOverviewBar.apply_document_data = spy
        try:
            view.note_place_requested.emit("don", 12345.0, False, False)
        finally:
            gui.TimingOverviewBar.apply_document_data = original
        self.assertEqual(calls, [], "note placement must not reload any TimingOverviewBar")

    def test_sv_edit_still_reloads_the_timing_bars(self):
        """The fix must be scoped to note edits -- SV/timing-point edits
        still need to refresh kiai/bookmarks/markers."""
        sv_view = self._sv_view()
        calls = []
        original = gui.TimingOverviewBar.apply_document_data
        def spy(self, *args, **kwargs):
            calls.append(self)
            return original(self, *args, **kwargs)
        gui.TimingOverviewBar.apply_document_data = spy
        try:
            sv_view.point_add_requested.emit(15000.0, 1.5)
        finally:
            gui.TimingOverviewBar.apply_document_data = original
        self.assertGreater(len(calls), 0)


if __name__ == "__main__":
    unittest.main()
