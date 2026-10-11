"""The "Kiai and Sound Volume" layer: every timing point on one axis, plus its
Kiai and Volume range tools.

Three properties, one per thing the layer actually promises:

* a millisecond carrying both an uninherited and an inherited point draws
  **one yellow rule**, not a red and a green fighting over the same pixel,
* the Volume sweep writes whole percent inside 0..100 -- an oscillation fans
  past both ends by design, so the clamp is load-bearing rather than defensive,
* neither range tool ever *writes* a timing point: this layer has nothing to
  do with scroll speed, and inventing a green line to carry a kiai flag is a
  scroll-speed edit made on the user's behalf.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui import gui
from osu_io.timing import TimingPoint
from tests.osu_fixtures import write_fixture
from tests.test_gimmick_editor import _GimmickFixture, _StubDialog

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _EmptyDocument:
    """Enough document for the layer's millisecond-ownership question."""

    hit_objects: list = []
    timing_points: list = []


class LayerRegistrationTests(unittest.TestCase):
    """The toolbox side: Kiai moved here, it was not copied here."""

    def test_kiai_left_the_fake_slider_layer(self):
        tools = dict(gui.MainWindow.GIMMICK_TOOLSETS["fake_slider"])
        self.assertNotIn("kiai", tools)

    def test_layer_carries_select_kiai_and_volume(self):
        tools = [tool for tool, _label in gui.MainWindow.GIMMICK_TOOLSETS["kiai_sound"]]
        self.assertEqual(tools, ["select", "kiai", "volume"])

    def test_layer_is_an_sv_view_in_the_gimmick_stack(self):
        layers = {layer: view for layer, view, _label in gui.MainWindow.GIMMICK_LAYERS}
        self.assertEqual(layers.get("kiai_sound"), "sv")


class LineColourTests(unittest.TestCase):
    """One entry per *timestamp*: the rule the timing overview bar already uses."""

    def test_red_and_green_on_one_millisecond_draw_one_yellow_rule(self):
        view = gui.SVEditorView()
        view.set_timing_points([
            TimingPoint.uninherited_at(1000, 120),
            TimingPoint.inherited_at(1000, 1.5),
            TimingPoint.inherited_at(2000, 0.5),
            TimingPoint.uninherited_at(3000, 180),
        ])

        self.assertEqual(
            view.line_kinds(), {1000.0: "yellow", 2000.0: "green", 3000.0: "red"}
        )


class RangeToolTests(unittest.TestCase):
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

    def _point_at(self, time_ms: int, uninherited: bool = False) -> TimingPoint:
        return next(
            point for point in self.state.document.timing_points
            if round(point.time) == time_ms and point.uninherited == uninherited
        )

    def test_the_add_view_dialog_builds_a_real_view_for_it(self):
        dialog = gui.AddViewDialog([("Oni", self.path)], self.window, self.path)
        offered = [dialog.type_combo.itemData(i) for i in range(dialog.type_combo.count())]
        dialog.deleteLater()
        self.assertIn("kiai_sound", offered)

        self.window._add_editor_view("kiai_sound", self.path)
        frame = self.window._editor_views[-1]
        self.assertIsInstance(frame.sv_view, gui.SVEditorView)
        # Unfiltered: the layer's whole subject is every layer's points at once.
        self.assertIsNone(frame.sv_view.point_times)

    def test_the_layer_shows_every_point_rather_than_one_structure_s(self):
        # None means "owns every millisecond" -- the three structure SV layers
        # each answer with a set instead, which is what filters their view.
        self.assertIsNone(self.window._sv_layer_times("kiai_sound", _EmptyDocument()))

    # -- Kiai ---------------------------------------------------------------

    def test_a_kiai_edge_with_no_point_is_left_alone(self):
        """Nothing here may invent a timing point.

        The fixture has an inherited point at 4000 and nothing on 4500. The
        old behaviour wrote a green line at each edge restating the SV already
        in force -- which is a scroll-speed edit made on the user's behalf, in
        the one layer that must have nothing to do with scroll speed. The
        section now simply starts at the nearest existing point.
        """
        before = len(self.state.document.timing_points)

        self.window._set_kiai_range(self.path, 4500.0, 5500.0)

        self.assertEqual(len(self.state.document.timing_points), before)
        self.assertFalse(any(
            round(point.time) == 4500 for point in self.state.document.timing_points
        ))

    def test_kiai_range_is_one_undo_step(self):
        """One undo step for the whole range, however many points it flags."""
        # 3500-4500 holds the fixture's own points, so there is something to
        # flag without anything being created. The fixture already has kiai on
        # elsewhere, so this compares against the range's own before/after
        # rather than against an empty list.
        def flagged():
            return {round(p.time) for p in self.state.document.timing_points if p.kiai}

        before = flagged()
        self.window._set_kiai_range(self.path, 3500.0, 4500.0)
        self.assertNotEqual(flagged(), before, "the range really did flag something")

        self.window.undo()

        self.assertEqual(flagged(), before)

    def test_the_view_snaps_the_drag_so_the_handler_does_not(self):
        # Deliberately off-grid: the range arrives already snapped (the view
        # owns the grid *and* the Ctrl override that trades it for whole
        # milliseconds), so it must be used as given rather than being pulled
        # back onto a beat here. An existing point one millisecond inside the
        # range is flagged; one millisecond outside it is not.
        self.window._set_kiai_range(self.path, 3999.0, 4500.0)
        self.assertTrue(self._point_at(4000).kiai)

        self.window.undo()
        self.window._set_kiai_range(self.path, 4001.0, 4500.0)
        self.assertFalse(self._point_at(4000).kiai)

    def test_the_kiai_tool_never_inserts_a_green_line(self):
        """_sv_range_action is what the kiai_sound layer's drag actually calls
        (not _set_kiai_range directly); neither path may write a point."""
        before = len(self.state.document.timing_points)
        self.window._sv_range_action(self.path, "kiai", 4000.0, 4500.0)
        self.assertEqual(len(self.state.document.timing_points), before)

    def test_kiai_with_no_timing_point_in_range_toasts_and_changes_nothing(self):
        before = len(self.state.document.timing_points)
        self.window._sv_range_action(self.path, "kiai", 4500.0, 5500.0)
        self.assertEqual(len(self.state.document.timing_points), before)

    # -- Volume -------------------------------------------------------------

    def _volume_params(self, **overrides) -> dict:
        params = {
            "initial_rate": 20.0, "final_rate": 100.0, "placement": "notes",
            "snap_divisor": 4, "position_offset": 0, "omit_barline": False,
            "relative_to_final_bpm": False, "function": "linear", "oscillate": "point",
        }
        params.update(overrides)
        return params

    def _spread_inherited_points(self) -> None:
        """Existing (inherited) timing points at 1000, 1500, 2500, 3000, 3500,
        alongside the fixture's own one at 2000 -- six points, the same
        cadence the old note-placed version of this test used, so the "20 +/-
        80 fans to -44 and to 100" math below still holds. The Volume tool
        edits timing points already in the range; it does not create them, so
        the range needs some to act on.
        """
        for time_ms in (1000, 1500, 2500, 3000, 3500):
            self.state.document.timing_points.append(TimingPoint.inherited_at(time_ms, 1.0))

    def test_volume_never_inserts_a_point(self):
        self._spread_inherited_points()
        before_ids = {point.uid for point in self.state.document.timing_points}

        self.window._generate_sv(
            self.path, 1000.0, 3500.0, self._volume_params(), volume=True,
        )

        after_ids = {point.uid for point in self.state.document.timing_points}
        self.assertEqual(after_ids, before_ids)

    def test_oscillated_volume_is_clamped_into_0_100(self):
        # 20 +/- 80 fans to -44 and to 100 across the six points in range, so
        # both ends of the clamp are exercised rather than merely available.
        self._spread_inherited_points()

        self.window._generate_sv(
            self.path, 1000.0, 3500.0, self._volume_params(), volume=True,
        )

        volumes = [
            point.volume for point in self.state.document.timing_points
            if 1000 <= round(point.time) <= 3500
        ]
        self.assertTrue(all(isinstance(value, int) for value in volumes), volumes)
        self.assertTrue(all(0 <= value <= 100 for value in volumes), volumes)
        self.assertIn(100, volumes, "the upper clamp never fired")
        self.assertIn(0, volumes, "the lower clamp never fired")

    def test_a_volume_sweep_leaves_scroll_speed_and_kiai_alone(self):
        # 2000 is the fixture's kiai green line at 0.75x. The Volume tool
        # only ever touches the .volume field, so both properties survive
        # untouched rather than needing to be restated.
        self.window._generate_sv(
            self.path, 1000.0, 3500.0, self._volume_params(), volume=True,
        )
        point = self._point_at(2000)
        self.assertAlmostEqual(point.sv_multiplier, 0.75)
        self.assertTrue(point.kiai)

    def test_a_volume_sweep_with_nothing_to_change_pushes_no_undo_step(self):
        """Only 2000 (the fixture's one inherited point) falls in range; if
        its volume is already the value the curve would write, there is
        nothing to record."""
        point = self._point_at(2000)
        self.window._generate_sv(
            self.path, 1000.0, 3500.0,
            self._volume_params(initial_rate=point.volume, final_rate=point.volume),
            volume=True,
        )
        self.assertEqual(len(self.state.history.undo_stack), 0)

    def test_a_volume_sweep_is_one_undo_step(self):
        self._spread_inherited_points()
        before = [(point.uid, point.volume) for point in self.state.document.timing_points]
        self.window._generate_sv(
            self.path, 1000.0, 3500.0, self._volume_params(), volume=True,
        )
        self.assertEqual(len(self.state.history.undo_stack), 1)

        self.window.undo()
        self.assertEqual(
            [(point.uid, point.volume) for point in self.state.document.timing_points],
            before,
        )

    def test_volume_with_no_timing_point_in_range_toasts_and_changes_nothing(self):
        before = [(point.uid, point.volume) for point in self.state.document.timing_points]
        # Nothing sits between the fixture's 2000 and 4000 lines.
        self.window._generate_sv(
            self.path, 2500.0, 3500.0, self._volume_params(), volume=True,
        )
        self.assertEqual(
            [(point.uid, point.volume) for point in self.state.document.timing_points],
            before,
        )
        self.assertEqual(len(self.state.history.undo_stack), 0)


class VerticalOnlyTests(unittest.TestCase):
    """The layer's lines belong to the milliseconds they sit on -- a barline, a
    note, a kiai edge -- so there is no horizontal drag in it at all. Dragging
    one sideways would have moved a section boundary out from under whatever
    put it there, and the vertical axis is volume, so up and down is the only
    gesture that means anything here.
    """

    def _view(self, volume_mode: bool) -> gui.SVEditorView:
        view = gui.SVEditorView()
        view.volume_mode = volume_mode
        view.resize(800, 200)
        view.set_timing_points([
            TimingPoint.uninherited_at(0, 120),
            TimingPoint.inherited_at(1000, 1.5),
        ])
        return view

    def test_every_drag_in_the_volume_layer_is_a_value_drag(self):
        view = self._view(True)
        for point in view.timing_points:
            for y in (5.0, 100.0, 195.0):
                axis = view._drag_axis_for_click(point, QPointF(400.0, y))
                self.assertEqual(axis, "value", (point.uninherited, y))

    def test_an_sv_layer_still_retimes_away_from_the_dot(self):
        """The change is scoped to volume_mode: everywhere else, a drag along
        the line is still how a green line is moved."""
        view = self._view(False)
        green = next(p for p in view.timing_points if not p.uninherited)
        far = QPointF(view.x_for_time(green.time) + 200.0, 5.0)
        self.assertEqual(view._drag_axis_for_click(green, far), "time")

    def test_a_vertical_drag_writes_a_volume_and_never_an_sv(self):
        view = self._view(True)
        green = next(p for p in view.timing_points if not p.uninherited)
        volumes, svs = [], []
        view.point_volume_edit_requested.connect(
            lambda uid, volume: volumes.append((uid, volume)))
        view.point_sv_edit_requested.connect(lambda uid, sv: svs.append((uid, sv)))
        view._begin_point_drag(green, "value")
        try:
            view.mouseMoveEvent(QMouseEvent(
                QEvent.MouseMove, QPointF(view.x_for_time(green.time), view._graph_top()),
                Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
            ))
        finally:
            view.releaseMouse()
        self.assertEqual(svs, [])
        self.assertEqual(volumes, [(green.uid, 100)])

    def test_the_axis_top_is_100_and_the_bottom_is_0(self):
        view = self._view(True)
        self.assertEqual(round(view._y_to_sv(
            view._graph_top(), view._graph_top(), view._graph_bottom())), 100)
        self.assertEqual(round(view._y_to_sv(
            view._graph_bottom(), view._graph_top(), view._graph_bottom())), 0)

    def test_a_double_click_asks_for_a_volume_not_the_line_dialog(self):
        """The generic line dialog carries a Time field, which is the one thing
        this layer must not offer."""
        view = self._view(True)
        typed, generic = [], []
        view.point_volume_dialog_requested.connect(typed.append)
        view.timing_line_edit_requested.connect(generic.append)
        view.mouseDoubleClickEvent(QMouseEvent(
            QEvent.MouseButtonDblClick, QPointF(view.x_for_time(1000.0), 100.0),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        ))
        self.assertEqual(generic, [])
        self.assertEqual(len(typed), 1)


class VolumeReachesTheHitsoundsTests(RangeToolTests):
    """What the layer is for: the number it writes has to be what the don and
    kat actually play at. A graph of a value nothing reads is decoration."""

    def test_the_layer_writes_the_volume_the_samples_play_at(self):
        point = self._point_at(0, uninherited=True)
        note = min(self.state.document.hit_objects, key=lambda n: n.time)
        self.window._edit_volume_point(self.state.source_path, point.uid, 30)
        self.assertEqual(point.volume, 30)
        # Through the forwarder's own schedule rather than a fresh call to
        # `hitsound_schedule`: what has to hold is that the *window* re-sent
        # after the edit, not merely that the pure function can resolve a
        # volume. The sound itself is mixed into the music stream from these
        # three lists (`audio_engine.HitsoundMixer`).
        times, _keys, volumes = self.window.hitsounds.schedule
        index = times.index(float(note.time))
        self.assertAlmostEqual(volumes[index], 0.3)

    def test_zero_is_obeyed_rather_than_treated_as_a_floor(self):
        """Mappers silence a section deliberately -- a swell under a fade, a
        section the audio already covers."""
        point = self._point_at(0, uninherited=True)
        self.window._edit_volume_point(self.state.source_path, point.uid, 0)
        self.assertEqual(self.window.hitsounds.schedule[2][0], 0.0)

    def test_out_of_range_is_clamped_rather_than_written(self):
        point = self._point_at(0, uninherited=True)
        self.window._edit_volume_point(self.state.source_path, point.uid, 400)
        self.assertEqual(point.volume, 100)
        self.window._edit_volume_point(self.state.source_path, point.uid, -50)
        self.assertEqual(point.volume, 0)

    def test_an_unchanged_volume_pushes_no_undo_step(self):
        """A drag emits on every mouse move, so most of them land on the value
        already in force."""
        point = self._point_at(0, uninherited=True)
        before = len(self.state.history.undo_stack)
        self.window._edit_volume_point(self.state.source_path, point.uid, point.volume)
        self.assertEqual(len(self.state.history.undo_stack), before)


class VolumeDialogTests(unittest.TestCase):
    """Same generator, same options -- only the quantity changes."""

    def test_volume_mode_offers_whole_percent_in_0_100(self):
        dialog = gui.SVFunctionDialog(0.0, 1000.0, volume=True, initial_rate=60, final_rate=90)
        self.assertEqual(dialog.initial_rate_spin.decimals(), 0)
        self.assertEqual(dialog.initial_rate_spin.minimum(), 0.0)
        self.assertEqual(dialog.initial_rate_spin.maximum(), 100.0)
        self.assertEqual(dialog.final_rate_spin.value(), 90.0)
        dialog.deleteLater()

    def test_volume_mode_keeps_every_function_and_both_oscillations(self):
        volume = gui.SVFunctionDialog(0.0, 1000.0, volume=True)
        sv = gui.SVFunctionDialog(0.0, 1000.0)
        self.assertEqual(set(volume.function_buttons), set(sv.function_buttons))
        self.assertEqual(
            [volume.mode_combo.itemData(i) for i in range(volume.mode_combo.count())],
            [sv.mode_combo.itemData(i) for i in range(sv.mode_combo.count())],
        )
        volume.deleteLater()
        sv.deleteLater()


if __name__ == "__main__":
    unittest.main()

class VolumeDialogRowTests(unittest.TestCase):
    """What the Volume dialog offers, and what it must not.

    Everything describing where an *inserted* point sits is meaningless here:
    the Volume tool edits the timing points already in the range. The position
    offset in particular kept showing despite being hidden, because
    pink_spin_buttons re-wrapped the row into a fresh visible container after
    the hide -- so this asserts the rendered state, not the hide call.
    """

    def _dialog(self, volume: bool) -> gui.SVFunctionDialog:
        dialog = gui.SVFunctionDialog(0.0, 1000.0, volume=volume)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_volume_mode_offers_no_position_offset(self):
        dialog = self._dialog(volume=True)
        self.assertFalse(dialog.position_offset_spin.isVisibleTo(dialog))
        self.assertEqual(dialog.parameters()["position_offset"], 0)

    def test_volume_mode_offers_no_placement_or_omit_barline(self):
        dialog = self._dialog(volume=True)
        # The combo is the model behind pills; the field is what is shown.
        self.assertFalse(gui.is_row_visible(None, dialog.placement_combo))
        self.assertFalse(dialog.omit_barline_check.isVisibleTo(dialog))
        self.assertFalse(dialog.relative_to_final_bpm_check.isVisibleTo(dialog))

    def test_the_sv_dialog_still_offers_all_of_them(self):
        dialog = self._dialog(volume=False)
        self.assertTrue(dialog.position_offset_spin.isVisibleTo(dialog))
        self.assertTrue(gui.is_row_visible(None, dialog.placement_combo))
        self.assertTrue(dialog.omit_barline_check.isVisibleTo(dialog))
        self.assertEqual(
            dialog.parameters()["position_offset"],
            gui.SVFunctionDialog.DEFAULT_POSITION_OFFSET_MS,
        )


class GimmickBandTests(_GimmickFixture, unittest.TestCase):
    """The layer's real home is the gimmick page's seventh band.

    That page builds its own SVEditorView rather than going through
    _add_editor_view, which is how the volume axis came to be set in only one
    of the two places a view of this layer is made -- and not the one anybody
    actually looks at.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)

    def _band(self, layer_id: str):
        frame = next(
            f for f in self.window._gimmick_views if f.gimmick_layer == layer_id
        )
        return frame.sv_view

    def test_the_band_graphs_volume_not_scroll_speed(self):
        view = self._band("kiai_sound")
        self.assertTrue(view.volume_mode)
        self.assertEqual(view.update_scale(), (0.0, 100.0))

    def test_the_other_sv_bands_still_graph_scroll_speed(self):
        for layer_id in ("sv_chart", "sv_fake_slider", "sv_barline"):
            self.assertFalse(self._band(layer_id).volume_mode, layer_id)


class CursorReadoutTests(_GimmickFixture, unittest.TestCase):
    """TimeAxisMixin.draw_cursor_position -- the small millisecond written in
    the corner of every layer -- has to follow the mouse.

    It reads self._hover_time, which was always current; what was missing was
    the repaint. Both views gated update() on having a placement ghost to
    draw, so in select mode the number only changed when something else
    repainted the view -- in practice, when you clicked.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.view = self.window._gimmick_views[0].chart_view
        self.view.resize(800, self.window.GIMMICK_LAYER_HEIGHT)

    def _move_to(self, x: float) -> None:
        self.view.mouseMoveEvent(QMouseEvent(
            QEvent.MouseMove, QPointF(x, 10.0), Qt.NoButton, Qt.NoButton, Qt.NoModifier,
        ))

    def test_leaving_the_view_clears_and_repaints(self):
        self._move_to(600.0)
        self.assertIsNotNone(self.view._hover_time)
        repaints = []
        self.view.update = lambda *a: repaints.append(1)

        self.view.leaveEvent(QEvent(QEvent.Leave))

        self.assertIsNone(self.view._hover_time)
        self.assertTrue(repaints, "the stale number has to be painted away")

    def test_select_mode_repaints_so_the_cursor_millisecond_follows(self):
        """TimeAxisMixin.draw_cursor_position writes the millisecond under the
        cursor in *every* tool, but the repaint used to be gated on there
        being a placement ghost to draw -- so in select mode the number only
        refreshed when something else repainted the view, which meant
        clicking.
        """
        self.view.tool = "select"
        repaints = []
        self.view.update = lambda *a: repaints.append(1)

        self._move_to(600.0)

        self.assertTrue(repaints, "a move in select mode has to repaint")
        self.assertEqual(self.view._hover_time, self.view.time_for_x(600.0))

    def test_an_sv_view_in_select_mode_repaints_too(self):
        sv_view = next(
            f.sv_view for f in self.window._gimmick_views
            if f.gimmick_layer == "kiai_sound"
        )
        sv_view.resize(800, self.window.GIMMICK_LAYER_HEIGHT)
        sv_view.tool = "select"
        repaints = []
        sv_view.update = lambda *a: repaints.append(1)

        sv_view.mouseMoveEvent(QMouseEvent(
            QEvent.MouseMove, QPointF(400.0, 10.0), Qt.NoButton, Qt.NoButton, Qt.NoModifier,
        ))

        self.assertTrue(repaints)
        self.assertEqual(sv_view._hover_time, sv_view.time_for_x(400.0))

