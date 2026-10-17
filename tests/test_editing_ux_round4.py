"""Fourth owner feedback round, after using the SV editor and copy/paste:

- a red line stacked with a green one shows the green line's SV, not 1.0x
- the SV graph autoscales to what is on screen and draws a curve through the
  points instead of a staircase
- pasting snaps to the grid instead of landing on the raw playhead
- SV lines can be rubber-band selected, and the view scrolls when the drag
  runs past its edge
- a plain left click on empty space deselects (a click on an object
  selects it -- see ChartDeselectTests)
- generated SV carries the kiai state it was dropped into
- spinners show a grey band between start and end
- "Drumroll"/"Denden" are called "Slider"/"Spinner"
- the language switch offers to restart, in the newly chosen language
- function mode prefills the range's current SV and hits the final rate
  exactly on the last generated point
"""
from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui import gui
from ui import sv_editor
from ui import chart_views
from ui import dialogs
from osu_io.timing import TimingPoint
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _press(view, x, y=100.0, button=Qt.MouseButton.LeftButton):
    view.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(x, y), button, button,
        Qt.KeyboardModifier.NoModifier,
    ))


def _move(view, x, y=100.0):
    view.mouseMoveEvent(QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(x, y), Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    ))


def _release(view, x, y=100.0):
    view.mouseReleaseEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(x, y), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    ))


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
        return next(f for f in self.window._editor_views if f.view_type == "chart").chart_view

    def _sv_view(self) -> gui.SVEditorView:
        return next(f for f in self.window._editor_views if f.view_type == "sv").sv_view


# -- Fancy Arranger: applied notes stay where they were shown -----------------


class FancyArrangerApplyTests(WindowTestCase):
    """Apply commits what the canvas shows, and the canvas keeps showing it.

    The owner's report: a transformation plus a canvas drag, Apply, Apply to
    original -- then any click re-ran the preview and the notes snapped back.
    """

    def _prepare(self, transformation: str):
        keys = [n.original_index for n in self.state.document.hit_objects[:6]]
        self.window._selection_changed(keys)
        self.window.timeline.selected = set(keys)
        combo = self.window._transform_page_refs["all"]["combo"]
        combo.setCurrentIndex(combo.findData(transformation))
        self.window.update_preview()
        self.window._canvas_dragged("don", 40, 25)
        return keys[1], self.window.canvas.positions[keys[1]]

    def test_a_drag_with_no_transformation_is_committed(self):
        key, shown = self._prepare("")
        self.window.apply_selection()
        self.assertEqual(self.window.applied_positions[key], shown)

    def test_applied_notes_stay_when_the_preview_runs_again(self):
        key, shown = self._prepare("text")
        self.window.apply_selection()
        self.window.update_preview()  # what the next click schedules
        self.assertEqual(self.window.applied_positions[key], shown)
        self.assertEqual(self.window.canvas.positions[key], shown)

    def test_a_centred_transformation_is_not_moved_twice(self):
        """A drag nudges Center X/Y as well as offsetting the notes; the shape
        was rebuilt around the moved centre *and* offset again."""
        key, shown = self._prepare("circle")
        self.window.preview_cache.clear()
        self.window.update_preview()
        self.assertEqual(self.window.canvas.positions[key], shown)
        self.window.apply_selection()
        self.assertEqual(self.window.applied_positions[key], shown)

    def _assert_on_playfield(self, keys):
        for key in keys:
            x, y = self.window.applied_positions[key]
            self.assertTrue(0 <= x <= 512 and 0 <= y <= 384, (key, x, y))

    def test_a_stale_offset_never_carries_notes_off_the_playfield(self):
        """The offset is clamped against the shape when it is set, then stored
        and re-added to whatever later parameters build (Text, Margin X 0:
        a note at y=-1). Set past the edge directly, as a stale one is."""
        keys = [n.original_index for n in self.state.document.hit_objects[:6]]
        self._prepare("text")
        self.window.preview_offsets["all"] = [512.0, -384.0]
        self.window.preview_cache.clear()
        self.window.apply_selection()
        self._assert_on_playfield(keys)

    def test_a_centre_at_the_edge_never_puts_the_shape_past_it(self):
        """Center X 500 with the largest radius reached x=690."""
        keys = [n.original_index for n in self.state.document.hit_objects[:6]]
        self._prepare("circle")
        self.window.controls["all"]["center_x"].set_value(500)
        self.window.controls["all"]["radius"].set_value(190)
        self.window.preview_cache.clear()
        self.window.apply_selection()
        self._assert_on_playfield(keys)


# -- effective SV per timestamp ---------------------------------------------


class SVSeriesTests(unittest.TestCase):
    def _view(self, points):
        view = gui.SVEditorView()
        view.set_timing_points(points)
        return view

    def test_a_green_line_on_a_red_line_wins(self):
        view = self._view([
            TimingPoint(time=1000.0, beat_length=400.0),                        # 150 BPM, resets SV
            TimingPoint(time=1000.0, beat_length=-50.0, uninherited_flag=0),    # 2.00x on top of it
        ])
        self.assertEqual(view.sv_series(), [(1000.0, 2.0)])

    def test_it_wins_regardless_of_file_order(self):
        view = self._view([
            TimingPoint(time=1000.0, beat_length=-50.0, uninherited_flag=0),
            TimingPoint(time=1000.0, beat_length=400.0),
        ])
        self.assertEqual(view.sv_series(), [(1000.0, 2.0)])

    def test_a_lone_red_line_still_reads_as_1x(self):
        view = self._view([TimingPoint(time=1000.0, beat_length=400.0)])
        self.assertEqual(view.sv_series(), [(1000.0, 1.0)])

    def test_one_entry_per_timestamp_in_time_order(self):
        view = self._view([
            TimingPoint(time=3000.0, beat_length=-200.0, uninherited_flag=0),
            TimingPoint(time=1000.0, beat_length=400.0),
            TimingPoint(time=2000.0, beat_length=-100.0, uninherited_flag=0),
        ])
        self.assertEqual([time_ms for time_ms, _sv in view.sv_series()], [1000.0, 2000.0, 3000.0])


# -- autoscale ---------------------------------------------------------------


class AutoscaleTests(unittest.TestCase):
    def _view(self, svs, spacing_ms=100.0):
        view = gui.SVEditorView()
        view.resize(800, 200)
        view.window_ms = 10000.0
        view.current_time = 5000.0
        view.set_timing_points([
            TimingPoint(time=1000.0 + spacing_ms * i, beat_length=-100.0 / sv, uninherited_flag=0)
            for i, sv in enumerate(svs)
        ])
        return view

    def test_a_flat_section_keeps_the_default_range(self):
        """Nothing near the ceiling and a fixed floor: 0.1x-2.5x."""
        view = self._view([1.0, 1.0, 1.0])
        low, high = view.update_scale()
        self.assertAlmostEqual(low, sv_editor.SV_VISUAL_MIN, places=6)
        self.assertAlmostEqual(high, sv_editor.SV_VISUAL_MAX, places=6)

    def test_a_narrow_band_still_keeps_the_default_range(self):
        view = self._view([0.9, 1.05, 1.1])
        low, high = view.update_scale()
        self.assertAlmostEqual(low, sv_editor.SV_VISUAL_MIN, places=6)
        self.assertAlmostEqual(high, sv_editor.SV_VISUAL_MAX, places=6)

    def test_a_very_slow_section_sits_on_the_floor_instead_of_moving_it(self):
        view = self._view([0.02, 1.0])
        low, _high = view.update_scale()
        self.assertAlmostEqual(low, sv_editor.SV_VISUAL_MIN, places=6)
        top, bottom = view._graph_top(), view._graph_bottom()
        self.assertAlmostEqual(view._sv_to_y(0.02, top, bottom), bottom, places=6)

    def test_the_range_covers_every_value_with_headroom(self):
        view = self._view([0.5, 1.0, 3.0])
        low, high = view.update_scale()
        self.assertLess(low, 0.5)
        self.assertGreater(high, 3.0)

    def test_it_follows_values_beyond_the_old_fixed_window(self):
        """0.25x-4x was hard-coded; an 8x sweep used to clip flat at the top."""
        view = self._view([1.0, 8.0])
        _low, high = view.update_scale()
        self.assertGreater(high, 8.0)

    def test_a_point_far_outside_the_window_still_counts(self):
        """The ceiling is a property of the document, not of what is on screen
        -- that is what stops it moving as the playhead scrolls."""
        view = self._view([1.0, 3.0], spacing_ms=90000.0)
        view.current_time = 0.0
        _low, high = view.update_scale()
        self.assertGreater(high, 3.0)

    def test_disabling_autoscale_freezes_the_range(self):
        view = self._view([1.0, 8.0])
        view.autoscale = False
        view.scale_min, view.scale_max = 0.25, 4.0
        self.assertEqual(view.update_scale(), (0.25, 4.0))

    def test_sv_to_y_and_back_still_round_trips_on_the_fitted_scale(self):
        view = self._view([0.5, 2.0])
        view.update_scale()
        top, bottom = view._graph_top(), view._graph_bottom()
        for sv in (0.6, 1.0, 1.8):
            self.assertAlmostEqual(view._y_to_sv(view._sv_to_y(sv, top, bottom), top, bottom), sv, places=3)


class BoundLadderTests(unittest.TestCase):
    def test_bounds_are_round_numbers_around_a_value(self):
        self.assertAlmostEqual(sv_editor.sv_bound_below(0.9), 0.5, places=6)
        self.assertAlmostEqual(sv_editor.sv_bound_above(1.1), 1.5, places=6)

    def test_a_value_sitting_exactly_on_a_step_is_still_enclosed(self):
        """Strictly outside, so the line is never drawn along the graph edge."""
        self.assertLess(sv_editor.sv_bound_below(1.5), 1.5)
        self.assertGreater(sv_editor.sv_bound_above(1.5), 1.5)
        self.assertLess(sv_editor.sv_bound_below(0.5), 0.5)

    def test_the_step_gets_finer_near_zero(self):
        self.assertGreater(sv_editor.sv_bound_below(0.3), 0.0)
        self.assertGreater(sv_editor.sv_bound_below(0.05), 0.0)
        self.assertGreaterEqual(sv_editor.sv_bound_below(0.02), sv_editor.SV_SCALE_FLOOR)

    def test_bounds_stay_within_the_absolute_limits(self):
        self.assertLessEqual(sv_editor.sv_bound_above(500.0), sv_editor.SV_SCALE_CEILING)
        self.assertGreaterEqual(sv_editor.sv_bound_below(0.001), sv_editor.SV_SCALE_FLOOR)


class StableAxisTests(unittest.TestCase):
    """The axis is decided per document, not per frame.

    Fitting it to the visible window made it move every time a fast point
    crossed the view edge, which is what the owner saw as the SV value
    flickering while scrolling. Sticky bounds only damped that; not refitting
    at all removes it.
    """

    def _view(self, svs):
        view = gui.SVEditorView()
        view.resize(800, 200)
        view.window_ms = 2000.0
        view.set_timing_points([
            TimingPoint(time=1000.0 + 4000.0 * i, beat_length=-100.0 / sv, uninherited_flag=0)
            for i, sv in enumerate(svs)
        ])
        return view

    def test_scrolling_past_a_fast_point_does_not_move_the_axis(self):
        view = self._view([1.0, 3.0, 1.0])
        view.current_time = 0.0
        first = view.update_scale()
        for time_ms in range(0, 14000, 250):
            view.current_time = float(time_ms)
            self.assertEqual(view.update_scale(), first)

    def test_repeated_paints_do_not_move_the_axis(self):
        view = self._view([0.8, 1.2])
        first = view.update_scale()
        for _ in range(5):
            view.grab()
            self.assertEqual(view.update_scale(), first)

    def test_an_edit_that_raises_the_maximum_raises_the_ceiling(self):
        view = self._view([1.0, 1.2])
        self.assertAlmostEqual(view.update_scale()[1], sv_editor.SV_VISUAL_MAX, places=6)
        view.timing_points[1].set_sv(6.0)
        view.set_timing_points(view.timing_points)
        self.assertGreater(view.update_scale()[1], 6.0)

    def test_the_ceiling_never_drops_below_the_default(self):
        view = self._view([0.2, 0.3])
        self.assertAlmostEqual(view.update_scale()[1], sv_editor.SV_VISUAL_MAX, places=6)

    def test_which_points_get_a_value_label_does_not_depend_on_scrolling(self):
        """Chaining the spacing rule from the last *labelled* point made the
        set depend on where the window started, so labels blinked while
        scrolling. It is a property of each pair of neighbours instead."""
        view = gui.SVEditorView()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.set_timing_points([
            TimingPoint(time=1000.0 + 60.0 * i, beat_length=-100.0 / (1.0 + i * 0.05),
                        uninherited_flag=0)
            for i in range(40)
        ])

        def labelled(current_time):
            view.current_time = current_time
            series = view.sv_series()
            xs = [view.x_for_time(time_ms) for time_ms, _sv in series]
            return {
                round(time_ms)
                for i, (time_ms, _sv) in enumerate(series)
                if i and xs[i] - xs[i - 1] >= chart_views.SV_LABEL_MIN_SPACING_PX
            }

        self.assertEqual(labelled(1500.0), labelled(2300.0))


class LinearGraphTests(unittest.TestCase):
    def test_the_smoothing_helper_is_gone(self):
        """Reverted: straight segments only, no invented curvature."""
        self.assertFalse(hasattr(gui, "catmull_rom_path"))


class SVGraphPaintTests(WindowTestCase):
    def test_the_graph_paints_without_raising(self):
        view = self._sv_view()
        view.resize(600, 220)
        view.grab()

    def test_it_paints_with_no_timing_points_at_all(self):
        view = gui.SVEditorView()
        view.resize(600, 220)
        view.set_timing_points([])
        view.grab()

    def test_a_dense_sweep_paints_without_raising(self):
        """Hundreds of points in one screen is the normal result of "every
        snap" generation, and the label-spacing guard runs only here."""
        view = gui.SVEditorView()
        view.resize(600, 220)
        view.window_ms = 4000.0
        view.current_time = 2000.0
        view.timing_points = [
            TimingPoint(time=float(t), beat_length=-100.0 / (1.0 + t / 4000.0), uninherited_flag=0)
            for t in range(0, 4000, 10)
        ]
        view.grab()


# -- paste snapping ----------------------------------------------------------


class PasteSnapTests(WindowTestCase):
    def test_pasted_notes_land_on_the_snap_grid(self):
        view = self._chart_view()
        self.window._editor_view_focus_changed(None, view)
        view.set_snap_divisor(4)
        view.selected = {n.original_index for n in self.state.document.hit_objects if n.time == 1000}
        self.window.copy_selection()

        # Playhead parked off-grid, the way audio playback leaves it. The
        # fixture's last section starts at 6000 with a 400ms beat, so 1/4
        # snaps land every 100ms.
        view.current_time = 30037.0
        self.window.paste_clipboard()

        pasted = max(self.state.document.hit_objects, key=lambda n: n.uid)
        self.assertEqual(pasted.time, 30000)

    def test_pasted_sv_points_land_on_the_snap_grid(self):
        sv_view = self._sv_view()
        self.window._editor_view_focus_changed(None, sv_view)
        sv_view.set_snap_divisor(4)
        sv_view.selected_uids = {
            next(p.uid for p in sv_view.timing_points if not p.uninherited)
        }
        self.window.copy_selection()

        sv_view.current_time = 30037.0
        self.window.paste_clipboard()

        pasted = max(self.state.document.timing_points, key=lambda p: p.uid)
        self.assertEqual(pasted.time, 30000.0)

    def test_relative_spacing_survives_the_snap(self):
        view = self._chart_view()
        self.window._editor_view_focus_changed(None, view)
        view.set_snap_divisor(4)
        view.selected = {
            n.original_index for n in self.state.document.hit_objects if n.time in (1000, 1500)
        }
        self.window.copy_selection()
        view.current_time = 30037.0
        self.window.paste_clipboard()

        times = {n.time for n in self.state.document.hit_objects}
        self.assertIn(30000, times)
        self.assertIn(30500, times)


# -- SV rubber-band selection ------------------------------------------------


class SVRangeSelectionTests(WindowTestCase):
    def _prepared_view(self):
        view = self._sv_view()
        view.resize(800, 200)
        view.window_ms = 8000.0
        view.current_time = 3000.0
        view.tool = "select"
        return view

    def test_dragging_empty_space_selects_every_green_line_in_range(self):
        view = self._prepared_view()
        inherited = [p for p in view.timing_points if not p.uninherited]
        self.assertGreaterEqual(len(inherited), 2)

        # Start well clear of any point so the press begins a range drag.
        _press(view, view.x_for_time(500.0))
        _move(view, view.x_for_time(5000.0))
        _release(view, view.x_for_time(5000.0))

        expected = {p.uid for p in inherited if 500.0 <= p.time <= 5000.0}
        self.assertEqual(view.selected_uids, expected)
        self.assertGreaterEqual(len(expected), 2, "the fixture must exercise a multi-point selection")

    def test_a_range_drag_never_selects_an_uninherited_point(self):
        view = self._prepared_view()
        _press(view, view.x_for_time(500.0))
        _move(view, view.x_for_time(7000.0))
        _release(view, view.x_for_time(7000.0))
        uninherited = {p.uid for p in view.timing_points if p.uninherited}
        self.assertEqual(view.selected_uids & uninherited, set())

    def test_dragging_past_the_right_edge_scrolls_the_view(self):
        view = self._prepared_view()
        _press(view, view.x_for_time(500.0))
        before = view.current_time
        view.select_mouse_x = view.width() + 120.0
        view._auto_scroll_selection()
        self.assertGreater(view.current_time, before)

    def test_dragging_past_the_left_edge_scrolls_the_other_way(self):
        view = self._prepared_view()
        _press(view, view.x_for_time(3000.0))
        before = view.current_time
        view.select_mouse_x = -120.0
        view._auto_scroll_selection()
        self.assertLess(view.current_time, before)

    def test_auto_scroll_stops_once_the_drag_ends(self):
        view = self._prepared_view()
        _press(view, view.x_for_time(500.0))
        self.assertTrue(view.auto_scroll_timer.isActive())
        _release(view, view.x_for_time(1000.0))
        view._auto_scroll_selection()
        self.assertFalse(view.auto_scroll_timer.isActive())

    def test_a_plain_click_on_empty_space_deselects(self):
        view = self._prepared_view()
        view.selected_uids = {p.uid for p in view.timing_points if not p.uninherited}
        _press(view, view.x_for_time(500.0))
        _release(view, view.x_for_time(500.0))
        self.assertEqual(view.selected_uids, set())


class ChartDeselectTests(WindowTestCase):
    def test_a_left_click_on_empty_space_clears_the_chart_selection(self):
        """Clear of every selected note's *column*, not just of the note row:
        a press anywhere in a selected object's column grabs it, which is what
        makes dragging a whole selection by one member work. The old version
        of this test clicked x=400, y=100 -- the middle of the view, which is
        the baseline of an 800x200 chart and lands on a note. That used to
        clear; a click on an object now selects it (see below).

        1250ms is 250ms from the notes either side, 50px at this zoom, clear
        of even a finisher's radius.
        """
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 2000.0
        view.selected = {n.original_index for n in self.state.document.hit_objects[:3]}
        x = view.x_for_time(1250.0)
        _press(view, x)
        _release(view, x)
        self.assertEqual(view.selected, set())

    def test_a_click_on_a_note_selects_it_here_too(self):
        """Same widget and the same yellow `selected_note_pen` as a gimmick
        layer, so the same gesture has to mean the same thing: a click on the
        object selects the object. The Editor page used to clear instead, so a
        note here could only be selected by boxing it and the selection
        outline never appeared from a click."""
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 2000.0
        note = next(n for n in self.state.document.hit_objects if n.time == 1000)
        x = view.x_for_time(1000.0)
        _press(view, x, y=view._baseline_y())
        _release(view, x, y=view._baseline_y())
        self.assertEqual(view.selected, {note.original_index})

    def test_the_fancy_arranger_timeline_deselects_too(self):
        timeline = self.window.timeline
        timeline.resize(800, 200)
        timeline.selected = {n.original_index for n in self.state.document.hit_objects[:3]}
        _press(timeline, 400.0)
        _release(timeline, 400.0)
        self.assertEqual(timeline.selected, set())

    def test_a_drag_off_the_objects_still_selects(self):
        """Clear of the note row, so the press is not on anything. A press on
        an object's own circle grabs it instead -- see
        test_a_press_on_a_note_moves_it_without_selecting_first."""
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 2000.0
        above = view._baseline_y() - max(view.note_radii()) - 5.0
        _press(view, view.x_for_time(900.0), y=above)
        _move(view, view.x_for_time(2600.0), y=above)
        _release(view, view.x_for_time(2600.0), y=above)
        self.assertTrue(view.selected)

    def test_a_press_on_a_note_moves_it_without_selecting_first(self):
        """Grabbing a thing means grabbing it. `move_requires_selection` still
        governs a press *beside* a note, which is what keeps the rubber band."""
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 2000.0
        note = next(n for n in self.state.document.hit_objects if n.time == 1000)
        self.assertNotIn(note.original_index, view.selected)

        _press(view, view.x_for_time(1000.0), y=view._baseline_y())
        _move(view, view.x_for_time(1600.0), y=view._baseline_y())
        _release(view, view.x_for_time(1600.0), y=view._baseline_y())

        moved = next(n for n in self.state.document.hit_objects if n.uid == note.uid)
        self.assertAlmostEqual(moved.time, 1600, delta=125)

    def test_dragging_an_already_selected_note_moves_it(self):
        """A note in the current selection is relocated by a drag that grabs
        it anywhere in its column, not only on its circle -- which is what
        makes dragging a whole selection by one of its members work."""
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 2000.0
        note = next(n for n in self.state.document.hit_objects if n.time == 1000)
        view.selected = {note.original_index}

        _press(view, view.x_for_time(1000.0))
        _move(view, view.x_for_time(1600.0))
        _release(view, view.x_for_time(1600.0))

        moved = next(n for n in self.state.document.hit_objects if n.uid == note.uid)
        self.assertNotEqual(moved.time, 1000)
        self.assertAlmostEqual(moved.time, 1600, delta=125)


# -- kiai preservation -------------------------------------------------------


class KiaiPreservationTests(WindowTestCase):
    def _params(self, **overrides):
        params = {
            "initial_rate": 1.0, "final_rate": 2.0, "placement": "notes", "snap_divisor": 4,
            "position_offset": 0, "omit_barline": False, "relative_to_final_bpm": False,
            "function": "linear",
        }
        params.update(overrides)
        return params

    def test_generated_points_keep_the_kiai_they_land_in(self):
        """The fixture turns kiai on at 2000 and off at 4000."""
        before_ids = {p.uid for p in self.state.document.timing_points}
        self.window._generate_sv(self.path, 2000.0, 3500.0, self._params())
        generated = [p for p in self.state.document.timing_points if p.uid not in before_ids]
        self.assertTrue(generated)
        self.assertTrue(all(p.kiai for p in generated), "a sweep inside kiai must not switch it off")

    def test_generated_points_outside_kiai_stay_off(self):
        before_ids = {p.uid for p in self.state.document.timing_points}
        self.window._generate_sv(self.path, 4000.0, 5000.0, self._params())
        generated = [p for p in self.state.document.timing_points if p.uid not in before_ids]
        self.assertTrue(generated)
        self.assertFalse(any(p.kiai for p in generated))

    def test_a_range_crossing_a_kiai_boundary_is_read_per_point(self):
        before_ids = {p.uid for p in self.state.document.timing_points}
        self.window._generate_sv(self.path, 1000.0, 5000.0, self._params())
        generated = sorted(
            (p for p in self.state.document.timing_points if p.uid not in before_ids),
            key=lambda p: p.time,
        )
        self.assertFalse(generated[0].kiai, "before 2000: kiai off")
        self.assertTrue(any(p.kiai for p in generated), "2000-4000: kiai on")
        self.assertFalse(generated[-1].kiai, "after 4000: kiai off again")

    def test_a_hand_placed_green_line_keeps_the_kiai_it_lands_in(self):
        view = self._sv_view()
        view.point_add_requested.emit(3000.0, 1.5)
        added = next(p for p in self.state.document.timing_points if p.time == 3000 and not p.uninherited)
        self.assertTrue(added.kiai)


# -- spinner extent ----------------------------------------------------------


class SpinnerExtentTests(WindowTestCase):
    def test_the_band_brush_is_grey_not_a_note_colour(self):
        view = self._chart_view()
        self.assertNotEqual(view.spinner_band_brush, view.don_brush)
        self.assertNotEqual(view.spinner_band_brush, view.kat_brush)
        self.assertNotEqual(view.spinner_band_brush, view.slider_brush)
        colour = view.spinner_band_brush
        self.assertAlmostEqual(colour.red(), colour.green(), delta=20)
        self.assertAlmostEqual(colour.green(), colour.blue(), delta=20)

    def test_painting_a_spinner_with_an_end_time_does_not_raise(self):
        view = self._chart_view()
        view.resize(800, 200)
        view.window_ms = 6000.0
        view.current_time = 6000.0
        spinner = next(n for n in view.notes if n.is_spinner)
        self.assertIsNotNone(view._note_end_time(spinner))
        view.grab()


# -- renamed tools -----------------------------------------------------------


class ToolNameTests(WindowTestCase):
    def test_slider_and_spinner_replace_drumroll_and_denden(self):
        self.assertEqual(self.window.tool_buttons["slider"].text(), "4. Slider")
        self.assertEqual(self.window.tool_buttons["spinner"].text(), "5. Spinner")

    def test_the_internal_tool_ids_are_unchanged(self):
        """Renaming is a label change; the ids are what the .osu type bits mean."""
        self.assertIn("slider", self.window.tool_buttons)
        self.assertIn("spinner", self.window.tool_buttons)


class ToolNameTranslationTests(unittest.TestCase):
    def test_japanese_keeps_the_old_word_and_adds_katakana(self):
        from app import i18n

        class FakeSettings:
            def string_value(self, key, default=""):
                return "ja" if key == "language/current" else default

        self.assertEqual(i18n.install_translator(_APP, FakeSettings()), "ja")
        try:
            slider = i18n.tr("MainWindow", "4. Slider")
            spinner = i18n.tr("MainWindow", "5. Spinner")
            self.assertIn("連打", slider)
            self.assertIn("スライダー", slider)
            self.assertIn("風船", spinner)
            self.assertIn("スピナー", spinner)
        finally:
            class English:
                def string_value(self, key, default=""):
                    return "en" if key == "language/current" else default

            i18n.install_translator(_APP, English())


# -- language restart prompt -------------------------------------------------


class LanguageRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        from ui import settings_dialog

        self.module = settings_dialog
        self.window = gui.MainWindow()

    def tearDown(self) -> None:
        self.window.close()
        from app import i18n

        class English:
            def string_value(self, key, default=""):
                return "en" if key == "language/current" else default

        i18n.install_translator(_APP, English())

    class FakeSettings:
        def __init__(self, language="en"):
            self.values = {"language/current": language}

        def string_value(self, key, default=""):
            return str(self.values.get(key, default))

        def bool_value(self, key, default=False):
            return bool(self.values.get(key, default))

        def int_value(self, key, default=0):
            # The Audio page reads volumes and the hitsound offset through
            # this; without it, building the dialog raises before any of
            # these tests reach what they are actually about.
            try:
                return int(float(self.values.get(key, default)))
            except (TypeError, ValueError):
                return default

        def value(self, key, default=None):
            return self.values.get(key, default)

        def set_value(self, key, value):
            self.values[key] = value

        def sync(self):
            pass

        def storage_name(self):
            return "fake"

    def _dialog(self, language="en"):
        from app.settings import ShortcutRegistry

        settings = self.FakeSettings(language)
        return self.module.SettingsDialog(settings, ShortcutRegistry(settings), self.window), settings

    def test_changing_the_language_prompts_for_a_restart(self):
        dialog, _settings = self._dialog("en")
        prompts = []
        dialog._prompt_language_restart = lambda: prompts.append(True)
        dialog.language_combo.setCurrentIndex(dialog.language_combo.findData("ja"))
        self.assertTrue(dialog.apply())
        self.assertEqual(len(prompts), 1)
        dialog.deleteLater()

    def test_keeping_the_language_does_not_prompt(self):
        dialog, _settings = self._dialog("en")
        prompts = []
        dialog._prompt_language_restart = lambda: prompts.append(True)
        self.assertTrue(dialog.apply())
        self.assertEqual(prompts, [])
        dialog.deleteLater()

    def test_the_prompt_is_built_in_the_newly_chosen_language(self):
        """It must not ask in the language the user is leaving."""
        dialog, settings = self._dialog("en")
        dialog.language_combo.setCurrentIndex(dialog.language_combo.findData("ja"))

        captured = {}

        class FakeMessageBox:
            AcceptRole = self.module.QMessageBox.AcceptRole
            RejectRole = self.module.QMessageBox.RejectRole
            Information = self.module.QMessageBox.Information

            def __init__(self, _parent=None):
                self._buttons = []

            def setIcon(self, _icon):
                pass

            def setWindowTitle(self, text):
                captured["title"] = text

            def setText(self, text):
                captured["text"] = text

            def addButton(self, text, _role):
                captured.setdefault("buttons", []).append(text)
                return object()

            def exec(self):
                return 0

            def clickedButton(self):
                return None

        original = self.module.QMessageBox
        self.module.QMessageBox = FakeMessageBox
        try:
            dialog.apply()
        finally:
            self.module.QMessageBox = original

        self.assertEqual(settings.string_value("language/current"), "ja")
        self.assertNotEqual(captured["title"], "Restart required", "still English")
        self.assertIn("再起動", captured["title"])
        self.assertTrue(any("再起動" in label for label in captured["buttons"]))
        dialog.deleteLater()

    def test_declining_the_restart_does_not_relaunch(self):
        dialog, _settings = self._dialog("en")
        restarts = []
        dialog._restart_application = lambda: restarts.append(True)
        dialog.language_combo.setCurrentIndex(dialog.language_combo.findData("ja"))

        class FakeMessageBox:
            AcceptRole = self.module.QMessageBox.AcceptRole
            RejectRole = self.module.QMessageBox.RejectRole
            Information = self.module.QMessageBox.Information

            def __init__(self, _parent=None):
                pass

            def setIcon(self, _icon):
                pass

            def setWindowTitle(self, _text):
                pass

            def setText(self, _text):
                pass

            def addButton(self, _text, _role):
                return object()

            def exec(self):
                return 0

            def clickedButton(self):
                return None  # neither button

        original = self.module.QMessageBox
        self.module.QMessageBox = FakeMessageBox
        try:
            dialog.apply()
        finally:
            self.module.QMessageBox = original
        self.assertEqual(restarts, [])
        dialog.deleteLater()


# -- function mode: prefill and exact endpoints ------------------------------


class FunctionPrefillTests(WindowTestCase):
    def test_the_dialog_opens_prefilled_with_the_ranges_current_sv(self):
        opened = {}

        class FakeDialog(gui.SVFunctionDialog):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                opened["initial"] = self.initial_rate_spin.value()
                opened["final"] = self.final_rate_spin.value()

            def exec(self):
                return gui.QDialog.DialogCode.Rejected

        original = gui.SVFunctionDialog
        gui.SVFunctionDialog = FakeDialog
        try:
            # The fixture is 0.75x from 2000 and 2.00x from 4000. Read at the
            # range's first and last objects (2500, the spinner at 5000), not
            # its edges: an object is what the sweep writes onto.
            self.window._open_sv_function_dialog(self.path, 2500.0, 5500.0)
        finally:
            gui.SVFunctionDialog = original

        self.assertAlmostEqual(opened["initial"], 0.75, places=2)
        self.assertAlmostEqual(opened["final"], 2.0, places=2)

    def test_an_untouched_generate_is_a_no_op_on_the_endpoint_values(self):
        params = {
            "initial_rate": 0.75, "final_rate": 0.75, "placement": "notes", "snap_divisor": 4,
            "position_offset": 0, "omit_barline": False, "relative_to_final_bpm": False,
            "function": "linear",
        }
        before_ids = {p.uid for p in self.state.document.timing_points}
        self.window._generate_sv(self.path, 2000.0, 3500.0, params)
        generated = [p for p in self.state.document.timing_points if p.uid not in before_ids]
        self.assertTrue(generated)
        for point in generated:
            self.assertAlmostEqual(point.sv_multiplier, 0.75, places=2)


class FunctionChooserTests(unittest.TestCase):
    def test_every_function_gets_its_own_chip(self):
        dialog = gui.SVFunctionDialog(1000.0, 2000.0)
        expected = {
            "linear", "sin_in", "sin_out", "exp1.3", "exp1.6", "true_exp", "sin",
            # The same exp curve with its exponent typed rather than fixed.
            "exp_x",
        }
        self.assertEqual(set(dialog.function_buttons), expected)
        for button in dialog.function_buttons.values():
            self.assertTrue(button.isCheckable())
            self.assertFalse(button.icon().isNull(), "a chip shows its curve")
        dialog.deleteLater()

    def test_the_drop_down_is_gone(self):
        dialog = gui.SVFunctionDialog(1000.0, 2000.0)
        self.assertFalse(hasattr(dialog, "function_combo"))
        dialog.deleteLater()

    def test_exactly_one_is_selected_and_linear_is_the_default(self):
        dialog = gui.SVFunctionDialog(1000.0, 2000.0)
        checked = [f for f, b in dialog.function_buttons.items() if b.isChecked()]
        self.assertEqual(checked, ["linear"])
        self.assertEqual(dialog.parameters()["function"], "linear")
        dialog.deleteLater()

    def test_picking_one_deselects_the_others_and_reaches_parameters(self):
        dialog = gui.SVFunctionDialog(1000.0, 2000.0)
        dialog.function_buttons["true_exp"].setChecked(True)
        checked = [f for f, b in dialog.function_buttons.items() if b.isChecked()]
        self.assertEqual(checked, ["true_exp"])
        self.assertEqual(dialog.parameters()["function"], "true_exp")
        dialog.deleteLater()

    def test_the_preview_follows_the_selected_function(self):
        dialog = gui.SVFunctionDialog(1000.0, 2000.0)
        dialog.function_buttons["sin"].setChecked(True)
        self.assertEqual(dialog.preview.function_id, "sin")
        dialog.deleteLater()

    def test_the_exp_tile_carries_the_typed_exponent(self):
        """One tile is any exponent: the number rides in the id, so the
        generator and a stored choice take the string they always did."""
        dialog = gui.SVFunctionDialog(1000.0, 2000.0)
        dialog.exp_spin.setValue(3.0)
        dialog.function_buttons["exp_x"].setChecked(True)
        self.assertEqual(dialog.selected_function(), "exp3")
        self.assertEqual(dialog.parameters()["function"], "exp3")
        self.assertEqual(dialog.preview.function_id, "exp3")
        dialog.deleteLater()

    def test_a_stored_exponent_comes_back_into_the_box(self):
        dialog = gui.SVFunctionDialog(1000.0, 2000.0)
        dialog.set_selected_function("exp2.5")
        self.assertTrue(dialog.function_buttons["exp_x"].isChecked())
        self.assertAlmostEqual(dialog.exp_spin.value(), 2.5)
        self.assertEqual(dialog.selected_function(), "exp2.5")
        dialog.deleteLater()

    def test_the_exp_tile_re_plots_when_the_exponent_changes(self):
        """The graph under the tile has to be the curve that number makes,
        or it is a picture of a different function."""
        dialog = gui.SVFunctionDialog(1000.0, 2000.0, initial_rate=1.0, final_rate=2.0)
        dialog.exp_spin.setValue(1.1)
        before = dialog.function_buttons["exp_x"].icon().pixmap(128, 110).toImage()
        dialog.exp_spin.setValue(8.0)
        after = dialog.function_buttons["exp_x"].icon().pixmap(128, 110).toImage()
        self.assertNotEqual(before, after)
        dialog.deleteLater()

    def test_a_flat_range_still_shows_each_curve(self):
        """At the default 1.00x -> 1.00x the old tiles all drew the same flat
        line. A chip draws the curve's shape, so they differ."""
        dialog = gui.SVFunctionDialog(1000.0, 2000.0, initial_rate=1.0, final_rate=1.0)
        images = {
            key: button.icon().pixmap(30, 20).toImage()
            for key, button in dialog.function_buttons.items()
        }
        self.assertNotEqual(images["linear"], images["sin_in"])
        self.assertNotEqual(images["sin_in"], images["sin_out"])
        dialog.deleteLater()

    def test_the_result_is_plotted_once_in_the_dialog(self):
        dialog = gui.SVFunctionDialog(1000.0, 2000.0, initial_rate=1.1, final_rate=0.9)
        self.assertIs(dialog.preview.window(), dialog)
        self.assertGreaterEqual(dialog.preview.height(), 150, "the plot is the point of the dialog")
        dialog.deleteLater()

    def test_the_tiles_re_plot_when_the_rates_change(self):
        """A tile drawn for the old range would show the wrong direction."""
        dialog = gui.SVFunctionDialog(1000.0, 2000.0, initial_rate=1.0, final_rate=1.0)
        before = dialog.function_buttons["true_exp"].icon().pixmap(128, 110).toImage()
        dialog.final_rate_spin.setValue(3.0)
        after = dialog.function_buttons["true_exp"].icon().pixmap(128, 110).toImage()
        self.assertNotEqual(before, after)
        dialog.deleteLater()

    def test_the_plot_comes_before_the_numbers(self):
        """Result first, then the curve chips, then the numbers that drive it."""
        dialog = gui.SVFunctionDialog(1000.0, 2000.0)
        column = dialog.sheet.column.widget()

        def top(widget):
            return widget.mapTo(column, gui.QPointF(0, 0)).y()

        dialog.resize(980, 700)
        dialog.show()
        self.assertLess(top(dialog.preview), top(dialog.function_buttons["linear"]))
        self.assertLess(top(dialog.function_buttons["linear"]), top(dialog.initial_rate_spin))
        for control in (
            dialog.initial_rate_spin, dialog.final_rate_spin, dialog.position_offset_spin,
            dialog.omit_barline_check, dialog.relative_to_final_bpm_check,
        ):
            self.assertTrue(column.isAncestorOf(control))
        dialog.close()
        dialog.deleteLater()


class FunctionPreviewTests(unittest.TestCase):
    def test_a_falling_sweep_is_drawn_falling(self):
        """1.10x -> 0.90x must descend, whatever the easing does."""
        preview = dialogs.SVFunctionPreview()
        preview.set_range(1.1, 0.9)
        for function_id in ("linear", "sin_in", "sin_out", "exp1.3", "exp1.6", "true_exp", "sin"):
            with self.subTest(function_id=function_id):
                preview.set_function(function_id)
                self.assertGreater(preview.rate_at(0.0), preview.rate_at(1.0))
                self.assertAlmostEqual(preview.rate_at(0.0), 1.1, places=6)
                self.assertAlmostEqual(preview.rate_at(1.0), 0.9, places=6)
        preview.deleteLater()

    def test_a_rising_sweep_is_drawn_rising(self):
        preview = dialogs.SVFunctionPreview()
        preview.set_range(0.9, 1.1)
        preview.set_function("true_exp")
        self.assertLess(preview.rate_at(0.0), preview.rate_at(1.0))
        preview.deleteLater()

    def test_it_plots_real_rates_not_normalised_progress(self):
        preview = dialogs.SVFunctionPreview()
        preview.set_range(1.1, 0.9)
        preview.set_function("linear")
        self.assertAlmostEqual(preview.rate_at(0.5), 1.0, places=6)
        preview.deleteLater()

    def test_a_flat_sweep_paints_without_dividing_by_zero(self):
        preview = dialogs.SVFunctionPreview()
        preview.set_range(1.0, 1.0)
        preview.grab()
        preview.deleteLater()

    def test_the_dialogs_rate_spins_drive_the_preview(self):
        dialog = gui.SVFunctionDialog(1000.0, 2000.0, initial_rate=1.1, final_rate=0.9)
        self.assertAlmostEqual(dialog.preview.initial_rate, 1.1, places=6)
        self.assertAlmostEqual(dialog.preview.final_rate, 0.9, places=6)
        dialog.final_rate_spin.setValue(2.0)
        self.assertAlmostEqual(dialog.preview.final_rate, 2.0, places=6)
        dialog.deleteLater()

    def test_the_preview_paints_for_every_function(self):
        preview = dialogs.SVFunctionPreview()
        preview.set_range(1.5, 0.4)
        for function_id in ("linear", "sin_in", "sin_out", "exp1.3", "exp1.6", "true_exp", "sin"):
            preview.set_function(function_id)
            preview.grab()
        preview.deleteLater()


class FunctionEndpointTests(WindowTestCase):
    def _params(self, **overrides):
        params = {
            "initial_rate": 1.0, "final_rate": 2.5, "placement": "notes", "snap_divisor": 4,
            "position_offset": -5, "omit_barline": False, "relative_to_final_bpm": False,
            "function": "linear",
        }
        params.update(overrides)
        return params

    def test_the_last_generated_point_hits_the_final_rate_exactly(self):
        """The dragged range runs past the last note, and the old code
        normalised progress by the *drag* duration -- so the last point stopped
        short of the requested final rate."""
        before_ids = {p.uid for p in self.state.document.timing_points}
        # Notes exist at 1000..3500; the drag deliberately ends at 5000.
        self.window._generate_sv(self.path, 1000.0, 5000.0, self._params())
        generated = sorted(
            (p for p in self.state.document.timing_points if p.uid not in before_ids),
            key=lambda p: p.time,
        )
        self.assertAlmostEqual(generated[0].sv_multiplier, 1.0, places=2)
        self.assertAlmostEqual(generated[-1].sv_multiplier, 2.5, places=2)

    def test_every_easing_function_reaches_both_endpoints(self):
        for function_id in ("linear", "sin_in", "sin_out", "exp1.3", "exp1.6", "true_exp", "sin"):
            with self.subTest(function_id=function_id):
                before_ids = {p.uid for p in self.state.document.timing_points}
                self.window._generate_sv(self.path, 1000.0, 5000.0, self._params(function=function_id))
                generated = sorted(
                    (p for p in self.state.document.timing_points if p.uid not in before_ids),
                    key=lambda p: p.time,
                )
                self.assertAlmostEqual(generated[0].sv_multiplier, 1.0, places=2)
                self.assertAlmostEqual(generated[-1].sv_multiplier, 2.5, places=2)
                self.window.undo()

    def test_a_single_generated_point_takes_the_initial_rate(self):
        before_ids = {p.uid for p in self.state.document.timing_points}
        # Exactly one note (5000) inside this range.
        self.window._generate_sv(self.path, 4500.0, 5500.0, self._params())
        generated = [p for p in self.state.document.timing_points if p.uid not in before_ids]
        self.assertEqual(len(generated), 1)
        self.assertAlmostEqual(generated[0].sv_multiplier, 1.0, places=2)

    def test_the_curve_is_still_monotonic_between_the_endpoints(self):
        before_ids = {p.uid for p in self.state.document.timing_points}
        self.window._generate_sv(self.path, 1000.0, 5000.0, self._params(function="true_exp"))
        values = [
            p.sv_multiplier
            for p in sorted(
                (p for p in self.state.document.timing_points if p.uid not in before_ids),
                key=lambda p: p.time,
            )
        ]
        self.assertEqual(values, sorted(values))
        self.assertTrue(any(not math.isclose(a, b) for a, b in zip(values, values[1:])))


if __name__ == "__main__":
    unittest.main()
