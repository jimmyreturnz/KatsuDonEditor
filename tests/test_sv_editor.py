"""M4 SV editor: add/edit/delete inherited timing points via an SVEditorView,
and the function-mode sweep generator.

Mirrors tests/test_note_editing.py's structure and rigor: the widget <->
MainWindow signal contract, real mouse events, multi-view refresh, undo/redo,
and a real write-to-disk round trip.
"""
from __future__ import annotations

import math
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from ui import gui
from ui import sv_editor
from ui import dialogs
from osu_io.parser import parse_osu
from osu_io.timing import TimingPoint, active_uninherited_at
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _press(view, x: float, y: float = 100.0, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(x, y), button, button, Qt.KeyboardModifier.NoModifier,
    )
    view.mousePressEvent(event)


def _move(view, x: float, y: float = 100.0) -> None:
    event = QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(x, y), Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    view.mouseMoveEvent(event)


def _release(view, x: float, y: float = 100.0) -> None:
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(x, y), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    view.mouseReleaseEvent(event)


class SVCacheSkipTests(unittest.TestCase):
    """set_timing_points skips the rebuild only when nothing it reads changed.

    The skip is what took an SV layer out of every note placement's cost; the
    danger it carries is a layer left drawing stale caches, so each input that
    must still force a rebuild is checked here.
    """

    def _view(self):
        """A view that has refreshed twice, so its skip key is built.

        The first refresh at a new point count rebuilds without a key (that
        is the part that keeps inserts cheap); the second builds it.
        """
        view = gui.SVEditorView()
        points = [
            TimingPoint.uninherited_at(0, 500.0),
            TimingPoint.inherited_at(1000, 1.5),
            TimingPoint.inherited_at(2000, 0.5),
        ]
        view.set_timing_points(points)
        return view, points

    def _rebuilds(self, view, action) -> int:
        with patch.object(view, "_rebuild_caches", wraps=view._rebuild_caches) as spy:
            action()
        return spy.call_count

    def test_unchanged_points_skip_the_rebuild(self) -> None:
        view, points = self._view()
        view.set_timing_points(points)
        self.assertEqual(self._rebuilds(view, lambda: view.set_timing_points(list(points))), 0)

    def test_in_place_field_edit_rebuilds(self) -> None:
        view, points = self._view()
        view.set_timing_points(points)
        points[1].set_sv(3.0)  # no command, no counter: the edit a drag makes
        self.assertEqual(self._rebuilds(view, lambda: view.set_timing_points(points)), 1)
        self.assertIn((1000, 3.0), [(t, round(sv, 6)) for t, sv in view._series])

    def test_owned_milliseconds_change_rebuilds(self) -> None:
        view, points = self._view()
        view.point_times = {1000}
        view.set_timing_points(points)
        view.point_times = {1000, 2000}  # a note placed on 2000 now owns that line
        self.assertEqual(self._rebuilds(view, lambda: view.set_timing_points(points)), 1)
        self.assertEqual([p.time for p in view._visible_points], [1000, 2000])

    def test_added_point_rebuilds(self) -> None:
        view, points = self._view()
        view.set_timing_points(points)
        points.append(TimingPoint.inherited_at(3000, 2.0))
        self.assertEqual(self._rebuilds(view, lambda: view.set_timing_points(points)), 1)


class SVEasingTests(unittest.TestCase):
    """The preview square and the real generator share this function, so its
    correctness matters beyond decoration."""

    def test_endpoints_are_0_and_1_for_every_function(self):
        for function_id in (
            "linear", "sin_in", "sin_out", "exp1.3", "exp1.6", "true_exp", "sin", "exp2.5",
        ):
            with self.subTest(function_id=function_id):
                self.assertAlmostEqual(gui.sv_ease(function_id, 0.0), 0.0, places=6)
                self.assertAlmostEqual(gui.sv_ease(function_id, 1.0), 1.0, places=6)

    def test_a_typed_exponent_is_carried_in_the_id(self):
        """"Exp x" is the same curve with the number exposed, and the number
        rides in the id so no caller needs a second argument for it."""
        for exponent in (1.0, 1.5, 2.0, 3.7, 10.0):
            with self.subTest(exponent=exponent):
                for step in range(11):
                    progress = step / 10
                    self.assertAlmostEqual(
                        gui.sv_ease(f"exp{exponent:g}", progress),
                        progress ** exponent, places=12)

    def test_an_exponent_below_one_is_floored_rather_than_inverted(self):
        """Below 1 the curve bends the other way, which is the shape the two
        fixed exp tiles exist above 1 to avoid."""
        self.assertAlmostEqual(gui.sv_ease("exp0.5", 0.25), 0.25, places=12)

    def test_a_malformed_exponent_falls_back_to_linear(self):
        self.assertAlmostEqual(gui.sv_ease("expfoo", 0.5), 0.5)

    def test_out_of_range_progress_is_clamped(self):
        self.assertEqual(gui.sv_ease("linear", -1.0), 0.0)
        self.assertEqual(gui.sv_ease("linear", 2.0), 1.0)

    def test_unknown_function_falls_back_to_linear(self):
        self.assertAlmostEqual(gui.sv_ease("nonsense", 0.5), 0.5)

    # -- parity with TaikoEditor -------------------------------------------
    # Every curve is transcribed from `SvFunctionLayer` lines 118-128 and
    # 275-287, so the same-named button in either editor writes the same
    # numbers. Spelled out as the Java expressions rather than simplified, so
    # a future edit is diffed against the source it was ported from.

    def test_fixed_curves_match_taikoeditor(self):
        reference = {
            "linear": lambda x: x,
            "exp1.3": lambda x: x ** 1.3,
            "exp1.6": lambda x: x ** 1.6,
            "sin": lambda x: (math.cos((x + 1) * math.pi) + 1) / 2.0,
            "sin_in": lambda x: math.cos(x * math.pi / 2.0 + 1.5 * math.pi),
            "sin_out": lambda x: math.cos(x * math.pi / 2.0 + math.pi) + 1,
        }
        for function_id, expected in reference.items():
            for step in range(21):
                t = step / 20
                with self.subTest(function_id=function_id, t=t):
                    self.assertAlmostEqual(gui.sv_ease(function_id, t), expected(t), places=12)

    def test_sine_pair_follows_taikoeditor_orientation_not_easings_net(self):
        """"Sin In" is fast at the start here, which is the inverse of the
        easings.net convention. The two were swapped, so a mapper porting a
        section between the editors got the mirrored curve from the
        identically-named button -- 0.38 against 0.08 a quarter of the way in.
        """
        self.assertGreater(gui.sv_ease("sin_in", 0.25), 0.25)   # fast start
        self.assertLess(gui.sv_ease("sin_out", 0.25), 0.25)     # slow start
        self.assertAlmostEqual(gui.sv_ease("sin_in", 0.5), math.sqrt(0.5), places=12)

    def test_true_exp_is_a_geometric_sweep_over_its_own_range(self):
        """Unlike every other curve, this one's shape depends on the endpoints:
        interpolating through it must reduce to initial * (final/initial)**t.
        A fixed shape hits both endpoints and still lands every point between
        them wrong, which is how the old `(exp(3t)-1)/(exp(3)-1)` survived.
        """
        for initial, final in ((1.0, 2.0), (1.0, 10.0), (2.0, 1.0), (1.0, 1.1), (0.5, 4.0)):
            for step in range(21):
                t = step / 20
                with self.subTest(initial=initial, final=final, t=t):
                    eased = gui.sv_ease("true_exp", t, initial, final)
                    self.assertAlmostEqual(
                        initial + (final - initial) * eased,
                        initial * (final / initial) ** t,
                        places=12,
                    )

    def test_true_exp_curvature_tracks_the_range(self):
        """A gentle sweep is nearly straight and a violent one is not -- the
        whole point of the curve, and exactly what a fixed shape cannot do."""
        gentle = gui.sv_ease("true_exp", 0.5, 1.0, 1.1)
        steep = gui.sv_ease("true_exp", 0.5, 1.0, 10.0)
        # 1.0 -> 1.1 bends off the straight line by about 0.012; 1.0 -> 10.0
        # by more than 0.25. The gap between those two is the property.
        self.assertLess(abs(gentle - 0.5), 0.02)
        self.assertLess(steep, 0.25)

    def test_true_exp_falls_back_to_linear_on_a_range_with_no_ratio(self):
        """Equal endpoints are 0/0, a zero start has no ratio, and a sign
        change raises a fractional power of a negative -- which in Python is a
        complex number rather than an error, so it has to be refused up front.
        Endpoints omitted entirely is the same case: every non-`true_exp`
        caller passes none.
        """
        for initial, final in ((1.0, 1.0), (0.0, 2.0), (1.0, -1.0), (-1.0, 1.0), (None, None)):
            for t in (0.0, 0.25, 0.5, 1.0):
                with self.subTest(initial=initial, final=final, t=t):
                    eased = gui.sv_ease("true_exp", t, initial, final)
                    self.assertIsInstance(eased, float)
                    self.assertAlmostEqual(eased, t, places=12)


class SVVisualScaleTests(unittest.TestCase):
    def test_default_visual_range_is_0_1x_to_2_5x(self):
        self.assertEqual(sv_editor.SV_VISUAL_MIN, 0.1)
        self.assertEqual(sv_editor.SV_VISUAL_MAX, 2.5)


class SVVisualMappingTests(unittest.TestCase):
    def test_sv_to_y_and_back_round_trips(self):
        view = gui.SVEditorView()
        # Explicit range wide enough for every tested value -- this test is
        # about the log-mapping math, not the widget's default axis.
        view.scale_min, view.scale_max = 0.25, 4.0
        top, bottom = 20.0, 160.0
        for sv in (0.5, 1.0, 1.5, 2.0, 3.0):
            y = view._sv_to_y(sv, top, bottom)
            back = view._y_to_sv(y, top, bottom)
            self.assertAlmostEqual(back, sv, places=3)

    def test_dragging_past_the_graph_reaches_the_real_sv_limits(self):
        """The drawn axis floors at SV_VISUAL_MIN so a slow section does not
        squash the map, but a green line may hold 0.01x. Clamped to the graph,
        the bottom pixel was 0.1x and everything under it was unreachable by
        drag -- and the autoscaling ceiling put anything faster than the map's
        own maximum out of reach the same way."""
        view = gui.SVEditorView()
        view.scale_min, view.scale_max = sv_editor.SV_VISUAL_MIN, sv_editor.SV_VISUAL_MAX
        top, bottom = 20.0, 160.0
        self.assertAlmostEqual(view._y_to_sv(10_000.0, top, bottom), sv_editor.SV_SCALE_FLOOR)
        self.assertAlmostEqual(view._y_to_sv(-10_000.0, top, bottom), sv_editor.SV_SCALE_CEILING)
        # A drag a little below the graph lands between the two floors, not on
        # either -- the scale carries on rather than snapping to the limit.
        just_below = view._y_to_sv(bottom + 20.0, top, bottom)
        self.assertLess(just_below, sv_editor.SV_VISUAL_MIN)
        self.assertGreater(just_below, sv_editor.SV_SCALE_FLOOR)

    def test_volume_stays_clamped_to_its_own_axis(self):
        """0-100% is the whole range, not a window on one."""
        view = gui.SVEditorView()
        view.volume_mode = True
        view.scale_min, view.scale_max = 0.0, 100.0
        self.assertEqual(view._y_to_sv(10_000.0, 20.0, 160.0), 0.0)
        self.assertEqual(view._y_to_sv(-10_000.0, 20.0, 160.0), 100.0)

    def test_higher_sv_is_higher_on_screen(self):
        view = gui.SVEditorView()
        top, bottom = 20.0, 160.0
        self.assertLess(view._sv_to_y(2.0, top, bottom), view._sv_to_y(1.0, top, bottom))


class SVEditorIntegrationTests(unittest.TestCase):
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

    def _sv_view(self) -> gui.SVEditorView:
        frame = next(f for f in self.window._editor_views if f.view_type == "sv")
        return frame.sv_view

    # -- default state / global tool row -------------------------------------

    def test_activation_gives_a_real_sv_view_not_a_placeholder(self):
        frame = next(f for f in self.window._editor_views if f.view_type == "sv")
        self.assertIsInstance(frame.sv_view, gui.SVEditorView)
        self.assertGreater(len(frame.sv_view.timing_points), 0)

    def test_new_sv_view_becomes_the_sv_tool_rows_target_and_note_row_hides(self):
        view = self._sv_view()
        self.assertIs(self.window._active_sv_view, view)
        self.assertTrue(self.window.global_sv_tool_row.isEnabled())
        self.assertTrue(self.window.global_sv_tool_row.isVisible())
        self.assertFalse(self.window.global_tool_row.isVisible())

    def test_sv_tool_row_drives_the_active_view(self):
        view = self._sv_view()
        self.window.sv_tool_buttons["green_line"].setChecked(True)
        self.assertEqual(view.tool, "green_line")

    # -- add / edit / delete ---------------------------------------------------

    def test_add_point_via_signal(self):
        view = self._sv_view()
        before = len(self.state.document.timing_points)
        view.point_add_requested.emit(15000.0, 1.75)
        self.assertEqual(len(self.state.document.timing_points), before + 1)
        added = next(p for p in self.state.document.timing_points if p.time == 15000)
        self.assertFalse(added.uninherited)
        self.assertAlmostEqual(added.sv_multiplier, 1.75, places=2)

    def test_edit_point_time_via_signal_retimes_an_uninherited_point_and_merges_a_drag(self):
        """Dragging a red (BPM) line horizontally retimes it only -- no SV
        field exists on an uninherited point to touch."""
        view = self._sv_view()
        uninherited = next(p for p in self.state.document.timing_points if p.uninherited)
        for time_ms in (1200.0, 1500.0, 1800.0):
            view.point_time_edit_requested.emit(uninherited.uid, time_ms)
        self.assertEqual(len(self.state.history.undo_stack), 1, "a drag is one undo step")
        self.assertAlmostEqual(uninherited.time, 1800.0, places=2)

    def test_edit_point_time_via_signal_retimes_a_green_line_without_touching_its_sv(self):
        view = self._sv_view()
        inherited = next(p for p in self.state.document.timing_points if not p.uninherited)
        original_sv = inherited.sv_multiplier
        for time_ms in (5200.0, 5500.0, 5800.0):
            view.point_time_edit_requested.emit(inherited.uid, time_ms)
        self.assertEqual(len(self.state.history.undo_stack), 1, "a drag is one undo step")
        self.assertAlmostEqual(inherited.time, 5800.0, places=2)
        self.assertAlmostEqual(inherited.sv_multiplier, original_sv, places=2)

    def test_edit_point_sv_via_signal_and_merges_a_drag(self):
        view = self._sv_view()
        inherited = next(p for p in self.state.document.timing_points if not p.uninherited)
        for sv in (1.2, 1.5, 1.8):
            view.point_sv_edit_requested.emit(inherited.uid, sv)
        self.assertEqual(len(self.state.history.undo_stack), 1, "a drag is one undo step")
        self.assertAlmostEqual(inherited.sv_multiplier, 1.8, places=2)

    def test_delete_point_via_signal(self):
        view = self._sv_view()
        inherited = next(p for p in self.state.document.timing_points if not p.uninherited)
        before = len(self.state.document.timing_points)
        view.point_delete_requested.emit(inherited.uid)
        self.assertEqual(len(self.state.document.timing_points), before - 1)

    def test_deleting_an_uninherited_point_is_refused(self):
        """SV editor scope: BPM points aren't its business, and removing the
        only one would leave the map without timing at all."""
        view = self._sv_view()
        uninherited = next(p for p in self.state.document.timing_points if p.uninherited)
        before = len(self.state.document.timing_points)
        view.point_delete_requested.emit(uninherited.uid)
        self.assertEqual(len(self.state.document.timing_points), before)

    def test_add_edit_delete_are_undoable(self):
        view = self._sv_view()
        before = len(self.state.document.timing_points)
        view.point_add_requested.emit(15000.0, 1.75)
        self.assertTrue(self.state.history.can_undo())
        self.window.undo()
        self.assertEqual(len(self.state.document.timing_points), before)
        self.window.redo()
        self.assertEqual(len(self.state.document.timing_points), before + 1)

    def test_graph_paints_without_raising_across_a_large_gap(self):
        """full_v14's two green points sit 2000ms apart at 500ms/beat (4
        beats) -- past the 2-beat threshold where the connecting line breaks
        instead of implying a ramp the document never defined."""
        view = self._sv_view()
        view.resize(500, 200)
        view.window_ms = 8000.0
        view.current_time = 3000.0
        view.grab()  # must not raise

    # -- real mouse events -------------------------------------------------

    def test_real_click_adds_a_point_in_green_line_mode(self):
        view = self._sv_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        view.current_time = 10000.0
        view.tool = "green_line"

        before = len(self.state.document.timing_points)
        _press(view, view.x_for_time(11000.0), y=60.0)
        self.assertEqual(len(self.state.document.timing_points), before + 1)

    def test_real_drag_retimes_a_red_bpm_line_snapped_to_the_grid(self):
        """select tool must reach an uninherited point too, not only green
        lines -- this is the actual mouse path behind point_time_edit_requested."""
        view = self._sv_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        uninherited = next(p for p in self.state.document.timing_points if p.uninherited)
        view.current_time = uninherited.time
        view.tool = "select"
        view.snap_divisor = 1

        start_x = view.x_for_time(uninherited.time)
        _press(view, start_x)
        self.assertIs(view._drag_point, uninherited)
        _move(view, view.x_for_time(uninherited.time + 500.0))
        _release(view, view.x_for_time(uninherited.time + 500.0))

        self.assertAlmostEqual(uninherited.time, 500.0, delta=1.0)

    def test_real_click_on_the_value_dot_adjusts_sv_not_time(self):
        """A click within SV_DOT_HIT_RADIUS_PX of the point's own value dot
        is a vertical (SV) drag -- see _drag_axis_for_click."""
        view = self._sv_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        inherited = next(p for p in self.state.document.timing_points if not p.uninherited)
        view.current_time = inherited.time
        view.tool = "select"

        x = view.x_for_time(inherited.time)
        dot_y = view._sv_to_y(inherited.sv_multiplier, view._graph_top(), view._graph_bottom())
        original_time, original_sv = inherited.time, inherited.sv_multiplier

        _press(view, x, y=dot_y)
        self.assertEqual(view._drag_axis, "value")
        _move(view, x, y=dot_y - 20.0)  # up the screen = higher SV
        _release(view, x, y=dot_y - 20.0)

        self.assertAlmostEqual(inherited.time, original_time, places=2)
        self.assertGreater(inherited.sv_multiplier, original_sv)

    def test_a_drag_quantizes_to_the_step_and_skips_unchanged_moves(self):
        """The lag report: a vertical drag used to emit a new float on every
        mouse-move pixel (8 decimals of it), so `_edit_sv_point`'s full
        refresh ran once per pixel too. Quantizing to SV_DRAG_STEP and
        skipping a move whose quantized value did not change is what lets
        most of those moves cost nothing -- checked here by counting
        emissions, not by re-measuring wall time.
        """
        view = self._sv_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        inherited = next(p for p in self.state.document.timing_points if not p.uninherited)
        view.current_time = inherited.time
        view.tool = "select"

        x = view.x_for_time(inherited.time)
        dot_y = view._sv_to_y(inherited.sv_multiplier, view._graph_top(), view._graph_bottom())

        emitted = []
        view.point_sv_edit_requested.connect(lambda uid, sv: emitted.append(sv))

        _press(view, x, y=dot_y)
        # A run of one-pixel moves inside the same SV_DRAG_STEP bucket: real
        # mouse hardware delivers far more of these than there are distinct
        # 0.01 steps across a typical drag.
        for offset in range(1, 6):
            _move(view, x, y=dot_y - 0.001 * offset)
        _release(view, x, y=dot_y)

        self.assertLessEqual(len(emitted), 1, "no move here crosses a 0.01 step")
        self.assertTrue(
            all(abs(v - round(v, 2)) < 1e-9 for v in emitted), "quantized to 0.01",
        )

    def test_a_genuine_move_still_reaches_the_handler_every_step(self):
        """The dedup above must not eat real movement -- only ones that quantize
        to the same value."""
        view = self._sv_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        inherited = next(p for p in self.state.document.timing_points if not p.uninherited)
        view.current_time = inherited.time
        view.tool = "select"

        x = view.x_for_time(inherited.time)
        dot_y = view._sv_to_y(inherited.sv_multiplier, view._graph_top(), view._graph_bottom())

        emitted = []
        view.point_sv_edit_requested.connect(lambda uid, sv: emitted.append(sv))

        _press(view, x, y=dot_y)
        for step in range(1, 11):
            _move(view, x, y=dot_y - step * 5.0)  # five real pixels each step
        _release(view, x, y=dot_y - 50.0)

        self.assertGreater(len(emitted), 1)
        self.assertGreater(len(set(emitted)), 1, "at least some genuine changes got through")
        # Dragging up the screen only ever raises SV -- never decreases.
        self.assertEqual(emitted, sorted(emitted))

    def test_each_pixel_of_drag_is_one_hundredth(self):
        """The axis is logarithmic, so mapping the cursor to it made a pixel
        worth 0.03x at 1.0x and more above -- most 0.01 steps were unreachable.
        The drag is relative to the press now: one pixel, one step."""
        view = self._sv_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        inherited = next(p for p in self.state.document.timing_points if not p.uninherited)
        view.current_time = inherited.time
        view.tool = "select"
        start = round(inherited.sv_multiplier, 2)

        x = view.x_for_time(inherited.time)
        dot_y = view._sv_to_y(inherited.sv_multiplier, view._graph_top(), view._graph_bottom())
        emitted = []
        view.point_sv_edit_requested.connect(lambda uid, sv: emitted.append(sv))

        _press(view, x, y=dot_y)
        for step in (1, 2, 3, 2):
            _move(view, x, y=dot_y - step)
        _release(view, x, y=dot_y - 2)

        self.assertEqual(emitted, [round(start + d, 2) for d in (0.01, 0.02, 0.03, 0.02)])

    def test_real_click_away_from_the_value_dot_retimes_instead(self):
        """A click on the same line but far from its dot is a horizontal
        (retime) drag, leaving SV untouched."""
        view = self._sv_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        inherited = next(p for p in self.state.document.timing_points if not p.uninherited)
        view.current_time = inherited.time
        view.tool = "select"
        view.snap_divisor = 1

        x = view.x_for_time(inherited.time)
        dot_y = view._sv_to_y(inherited.sv_multiplier, view._graph_top(), view._graph_bottom())
        original_time, original_sv = inherited.time, inherited.sv_multiplier
        far_y = dot_y + sv_editor.SV_DOT_HIT_RADIUS_PX + 40.0

        _press(view, x, y=far_y)
        self.assertEqual(view._drag_axis, "time")
        _move(view, view.x_for_time(inherited.time + 500.0), y=far_y)
        _release(view, view.x_for_time(inherited.time + 500.0), y=far_y)

        self.assertNotAlmostEqual(inherited.time, original_time, places=1)
        self.assertAlmostEqual(inherited.sv_multiplier, original_sv, places=2)

    def test_real_right_click_deletes_nearest_inherited_point(self):
        view = self._sv_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        inherited = next(p for p in self.state.document.timing_points if not p.uninherited)
        view.current_time = inherited.time

        before = len(self.state.document.timing_points)
        _press(view, view.x_for_time(inherited.time), button=Qt.MouseButton.RightButton)
        self.assertEqual(len(self.state.document.timing_points), before - 1)

    # -- multi-view refresh -----------------------------------------------------

    def test_edit_refreshes_every_open_sv_view_of_the_difficulty(self):
        view = self._sv_view()
        self.window._add_editor_view("sv", self.path)
        second_view = [f for f in self.window._editor_views if f.view_type == "sv"][-1].sv_view

        view.point_add_requested.emit(15000.0, 1.75)

        self.assertTrue(any(p.time == 15000 for p in second_view.timing_points))

    # -- function-mode generation -------------------------------------------

    def test_generate_pushes_one_undo_step_covering_every_point(self):
        before = len(self.state.document.timing_points)
        params = {
            "initial_rate": 1.0, "final_rate": 2.0, "placement": "snaps", "snap_divisor": 4, "position_offset": 0,
            "omit_barline": False, "relative_to_final_bpm": False, "function": "linear",
        }
        self.window._generate_sv(self.path, 10000.0, 12000.0, params)

        added = len(self.state.document.timing_points) - before
        self.assertGreater(added, 1)
        self.assertEqual(len(self.state.history.undo_stack), 1, "a sweep is one undo step")

        self.window.undo()
        self.assertEqual(len(self.state.document.timing_points), before)

    def test_generate_endpoints_match_initial_and_final_rate(self):
        before_ids = {p.uid for p in self.state.document.timing_points}
        params = {
            "initial_rate": 1.0, "final_rate": 3.0, "placement": "snaps", "snap_divisor": 4, "position_offset": 0,
            "omit_barline": False, "relative_to_final_bpm": False, "function": "linear",
        }
        self.window._generate_sv(self.path, 10000.0, 12000.0, params)

        generated = sorted(
            (p for p in self.state.document.timing_points if p.uid not in before_ids),
            key=lambda p: p.time,
        )
        self.assertAlmostEqual(generated[0].sv_multiplier, 1.0, places=1)
        self.assertAlmostEqual(generated[-1].sv_multiplier, 3.0, places=1)

    def test_generate_respects_position_offset(self):
        before_ids = {p.uid for p in self.state.document.timing_points}
        params = {
            "initial_rate": 1.0, "final_rate": 1.0, "placement": "snaps", "snap_divisor": 4, "position_offset": 500,
            "omit_barline": False, "relative_to_final_bpm": False, "function": "linear",
        }
        self.window._generate_sv(self.path, 10000.0, 11000.0, params)
        generated = [p for p in self.state.document.timing_points if p.uid not in before_ids]
        self.assertTrue(all(p.time >= 10500 for p in generated))

    def test_generate_keeps_time_of_a_note_that_already_has_a_green_line(self):
        """full_v14 has a green line at 2000ms (0.75x), which is also a note
        time. The position offset exists to land a *new* line slightly
        before the note it governs; a line already sitting there must keep
        its exact time and just take the newly generated speed, not get
        shoved to 1995 beside the untouched original.
        """
        before_ids = {p.uid for p in self.state.document.timing_points}
        params = {
            "initial_rate": 1.0, "final_rate": 2.0, "placement": "notes", "position_offset": -5,
            "omit_barline": False, "relative_to_final_bpm": False, "function": "linear",
        }
        self.window._generate_sv(self.path, 1000.0, 3000.0, params)

        at_2000 = [
            p for p in self.state.document.timing_points
            if not p.uninherited and round(p.time) == 2000
        ]
        self.assertEqual(len(at_2000), 1, "the existing line at 2000 must be updated in place, not duplicated")
        self.assertNotAlmostEqual(at_2000[0].sv_multiplier, 0.75, places=2, msg="speed must be the newly generated one")
        self.assertFalse(
            any(round(p.time) == 1995 for p in self.state.document.timing_points if p.uid not in before_ids),
            "must not also create an offset duplicate beside it",
        )

    def test_generate_omit_barline_applies_only_to_first_point(self):
        before_ids = {p.uid for p in self.state.document.timing_points}
        params = {
            "initial_rate": 1.0, "final_rate": 1.0, "placement": "snaps", "snap_divisor": 4, "position_offset": 0,
            "omit_barline": True, "relative_to_final_bpm": False, "function": "linear",
        }
        self.window._generate_sv(self.path, 10000.0, 11000.0, params)
        generated = sorted(
            (p for p in self.state.document.timing_points if p.uid not in before_ids),
            key=lambda p: p.time,
        )
        self.assertTrue(generated[0].omit_first_barline)
        self.assertTrue(all(not p.omit_first_barline for p in generated[1:]))

    def test_generate_relative_to_final_bpm_scales_with_local_bpm(self):
        """With no BPM change across the range, relative-to-final-BPM must be
        a no-op versus the raw rate (the multiplier it would apply is 1.0)."""
        before_ids = {p.uid for p in self.state.document.timing_points}
        params_absolute = {
            "initial_rate": 1.0, "final_rate": 2.0, "placement": "snaps", "snap_divisor": 4, "position_offset": 0,
            "omit_barline": False, "relative_to_final_bpm": False, "function": "linear",
        }
        self.window._generate_sv(self.path, 10000.0, 11000.0, params_absolute)
        absolute_points = sorted(
            (p.sv_multiplier for p in self.state.document.timing_points if p.uid not in before_ids),
        )

        # Fresh state for a clean comparison.
        with tempfile.TemporaryDirectory() as directory2:
            directory2 = Path(directory2)
            (directory2 / "audio.mp3").write_bytes(b"\x00")
            path2 = write_fixture(directory2, "full_v14")
            window2 = gui.MainWindow()
            window2._load_map_path(path2, refresh_difficulties=True)
            state2 = window2.state
            before_ids2 = {p.uid for p in state2.document.timing_points}
            params_relative = dict(params_absolute, relative_to_final_bpm=True)
            window2._generate_sv(path2, 10000.0, 11000.0, params_relative)
            relative_points = sorted(
                (p.sv_multiplier for p in state2.document.timing_points if p.uid not in before_ids2),
            )
            window2.close()

        for a, r in zip(absolute_points, relative_points):
            self.assertAlmostEqual(a, r, places=2, msg="no BPM change in range -> relative must match absolute")

    def test_relative_to_final_bpm_compensates_against_the_bpm_not_with_it(self):
        """Scroll distance goes as SV * BPM, so holding the perceived speed
        steady across a BPM rise means bringing SV *down*.

        The ratio was `local / start`, which compounded the change instead:
        a doubled BPM turned a requested 1.5x into 3.0 and scrolled four times
        the intended speed. The test above cannot see it -- with no BPM change
        in range the ratio is 1.0 whichever way round it is written -- so this
        one puts a real red line inside the range. TaikoEditor applies the same
        `firstBPM / localBPM` at `SVFunctionTool` lines 488 and 510.
        """
        start_bpm = active_uninherited_at(self.state.document.timing_points, 10000.0).bpm
        self.assertTrue(start_bpm)
        # A doubling halfway through the swept range.
        self.state.document.timing_points.append(
            TimingPoint.uninherited_at(10500.0, start_bpm * 2)
        )

        before_ids = {p.uid for p in self.state.document.timing_points}
        params = {
            "initial_rate": 1.5, "final_rate": 1.5, "placement": "snaps", "snap_divisor": 4,
            "position_offset": 0, "omit_barline": False, "relative_to_final_bpm": True,
            "function": "linear",
        }
        self.window._generate_sv(self.path, 10000.0, 11000.0, params)
        generated = sorted(
            ((p.time, p.sv_multiplier) for p in self.state.document.timing_points
             if p.uid not in before_ids),
        )
        self.assertTrue(generated)

        before = [sv for time, sv in generated if time < 10500.0]
        after = [sv for time, sv in generated if time >= 10500.0]
        self.assertTrue(before and after, "range must straddle the BPM change")
        # Flat 1.5x request: at the original BPM it stays 1.5, and past the
        # doubling it halves so that SV * BPM -- the thing actually seen -- is
        # unchanged. The old direction doubled it to 3.0 instead.
        for sv in before:
            self.assertAlmostEqual(sv, 1.5, places=6)
        for sv in after:
            self.assertAlmostEqual(sv, 0.75, places=6)

    # -- write-to-disk round trip -----------------------------------------------

    def test_generated_sv_survives_a_write_round_trip(self):
        params = {
            "initial_rate": 1.0, "final_rate": 2.5, "placement": "snaps", "snap_divisor": 4, "position_offset": 0,
            "omit_barline": False, "relative_to_final_bpm": False, "function": "sin",
        }
        self.window._generate_sv(self.path, 10000.0, 12000.0, params)
        self.window.save_all_states()

        reparsed = parse_osu(self.path)
        inherited = [p for p in reparsed.timing_points if not p.uninherited and 10000 <= p.time <= 12000]
        self.assertGreater(len(inherited), 1)

    # -- the real drag -> dialog -> Generate pathway, not just _generate_sv --

    def test_real_drag_opens_the_dialog_and_generate_applies_it(self):
        """The SV view auto-opened by _load_map_path already has its
        function_range_requested connected to the real
        MainWindow._open_sv_function_dialog -- a real QDialog.exec() blocks
        forever offscreen, so the fake subclass must be swapped in *before*
        the drag, not after.
        """
        view = self._sv_view()
        view.resize(800, 200)
        view.window_ms = 4000.0
        # A range that actually contains notes: the dialog's default
        # placement is "Each note", so a note-free range would (correctly)
        # generate nothing and say nothing about the drag -> dialog pathway.
        view.current_time = 2000.0
        view.tool = "function"

        created_dialogs = []

        class FakeSVFunctionDialog(gui.SVFunctionDialog):
            def exec(self):
                created_dialogs.append(self)
                self.initial_rate_spin.setValue(1.0)
                self.final_rate_spin.setValue(2.0)
                return gui.QDialog.DialogCode.Accepted

        original_dialog_class = gui.SVFunctionDialog
        gui.SVFunctionDialog = FakeSVFunctionDialog
        try:
            before = len(self.state.document.timing_points)
            start_x = view.x_for_time(1000.0)
            end_x = view.x_for_time(3000.0)
            _press(view, start_x, y=60.0)
            move = QMouseEvent(
                QMouseEvent.Type.MouseMove, QPointF(end_x, 60.0), Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            view.mouseMoveEvent(move)
            release = QMouseEvent(
                QMouseEvent.Type.MouseButtonRelease, QPointF(end_x, 60.0), Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            view.mouseReleaseEvent(release)
        finally:
            gui.SVFunctionDialog = original_dialog_class

        self.assertEqual(len(created_dialogs), 1)
        self.assertGreater(len(self.state.document.timing_points), before)

    def test_dialog_prefill_keeps_existing_sv_precision(self):
        """-100/beat_length routinely lands on a repeating decimal (1.4286x
        here), so the rate spins need more than 2 decimals or the prefilled
        "SV already in force at this end of the selection" is a rounded
        approximation instead of the line that is actually there.
        """
        point = gui.TimingPoint.inherited_at(1000.0, 1.4286)
        self.state.history.push(gui.InsertTimingPoints([point]), self.state)

        created_dialogs = []

        class FakeSVFunctionDialog(gui.SVFunctionDialog):
            def exec(self):
                created_dialogs.append(self)
                return gui.QDialog.DialogCode.Rejected

        original_dialog_class = gui.SVFunctionDialog
        gui.SVFunctionDialog = FakeSVFunctionDialog
        try:
            self.window._open_sv_function_dialog(self.path, 1000.0, 3000.0)
        finally:
            gui.SVFunctionDialog = original_dialog_class

        self.assertEqual(len(created_dialogs), 1)
        self.assertEqual(created_dialogs[0].initial_rate_spin.decimals(), dialogs.SV_DECIMALS)
        self.assertAlmostEqual(created_dialogs[0].initial_rate_spin.value(), 1.4286, places=3)


class SVPrecisionTests(unittest.TestCase):
    """Under a 60000 BPM red line the useful SV steps live past the sixth
    decimal, so the boxes have to reach them -- the graph still reads at 2dp."""

    def test_the_graph_still_labels_at_two_decimals(self):
        """The graph is read at a glance; 8 decimals on every point is a smear."""
        view = gui.SVEditorView()
        view.resize(600, 220)
        view.window_ms = 4000.0
        view.current_time = 2000.0
        view.set_timing_points([
            TimingPoint(time=1000.0, beat_length=-100.0 / 1.00000001, uninherited_flag=0),
        ])

        drawn: list[str] = []

        def spy(painter, *args):
            if len(args) == 2 and isinstance(args[1], str):
                drawn.append(args[1])
            return None

        pixmap = QPixmap(view.size())
        painter = QPainter(pixmap)
        with patch.object(QPainter, "drawText", spy):
            view._draw_sv_curve(painter, 20.0, 200.0, 0.0, 20000.0)
        painter.end()

        self.assertIn("1.00x", drawn)

    def test_sv_boxes_carry_the_full_precision(self):
        point = TimingPoint(
            time=1000.0, beat_length=-100.0 / 1.00000001, uninherited_flag=0
        )
        dialog = gui.TimingLineDialog(point)
        try:
            self.assertEqual(dialog.value_spin.decimals(), dialogs.SV_DECIMALS)
            self.assertEqual(f"{dialog.value_spin.value():.8f}", "1.00000001")
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
