"""Layer 2 (fake sliders) fixes: the two rows are separate objects, its red
lines are configurable, and its two tools have a big variant.

Its own module rather than more of `test_gimmick_editor.py`, which builds a
window per test and takes minutes to run whole -- the same reason
`test_gimmick_round6.py` exists.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui import gui
from osu_io.parser import OsuDocument
from tests.test_gimmick_editor import _GimmickFixture, _StubTimingLineDialog

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _Layer2(_GimmickFixture):
    """A gimmick session with a fake slider and a shiny on the same snap.

    One snap, two structures a millisecond apart -- the layer's two rows exist
    precisely for this, and it is where every one of these bugs showed up.
    """

    SNAP = 10000

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document
        self.config = self.window._gimmick_config("fake_slider")
        self.layer = self.window._gimmick_views[1].chart_view
        self.layer.resize(800, self.window.GIMMICK_LAYER_HEIGHT)

    def _place_pair(self) -> None:
        self.window._place_gimmick("fake_slider", "regular", self.SNAP)
        self.window._place_gimmick("fake_slider", "shiny", self.SNAP)
        self.layer.current_time = float(self.SNAP)

    @property
    def slider_at(self) -> int:
        return self.SNAP + self.config.fake_slider_offset_ms

    @property
    def shiny_at(self) -> int:
        return self.SNAP + self.config.shiny_offset_ms

    def _rows(self):
        """(upper row y, lower row y) -- fake sliders above, shinies below."""
        baseline = self.layer._baseline_y()
        return self.layer._row_y(baseline, False), self.layer._row_y(baseline, True)

    def _press(self, x: float, y: float) -> QMouseEvent:
        return QMouseEvent(
            QEvent.MouseButtonPress, QPointF(x, y), QPointF(x, y),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )

    def _sliders_at(self, time_ms):
        return [
            note for note in self.document.hit_objects
            if gui.MainWindow.is_fake_slider(note) and round(note.time) == time_ms
        ]


class RowAwareGrabTests(_Layer2, unittest.TestCase):
    """A grab must resolve to the one object under the cursor.

    The two structures are a millisecond apart, which at this layer's zoom is
    well inside one 20px hit radius, so before the click's y was consulted the
    scan returned whichever came first in the document.
    """

    def test_the_two_structures_are_the_same_pixel_column(self) -> None:
        """The premise: if they were not, none of this would have mattered."""
        self._place_pair()
        distance = abs(
            self.layer.x_for_time(self.slider_at) - self.layer.x_for_time(self.shiny_at)
        )
        self.assertLess(distance, 20.0)

    def test_the_row_outranks_the_nearer_millisecond(self) -> None:
        """Aimed at the shiny's own column, but on the fake slider's row.

        Nearest-x alone answers "the shiny" here, which is the bug: at this
        zoom the two columns are a third of a pixel apart and the row is the
        only thing the cursor can actually aim at.
        """
        self._place_pair()
        upper, lower = self._rows()
        x = self.layer.x_for_time(self.shiny_at)
        self.assertEqual(round(self.layer._note_near_x(x).time), self.shiny_at)
        self.assertEqual(round(self.layer._note_near_x(x, y=upper).time), self.slider_at)
        self.assertEqual(round(self.layer._note_near_x(x, y=lower).time), self.shiny_at)

    def test_a_drag_on_the_fake_slider_leaves_the_shiny_alone(self) -> None:
        self._place_pair()
        upper, _lower = self._rows()
        self.layer.set_tool("select")
        # The shiny's column, the fake slider's row -- see above.
        self.layer.mousePressEvent(self._press(self.layer.x_for_time(self.shiny_at), upper))
        grabbed = set(self.layer._move_note_uids)
        self.layer.releaseMouse()
        shiny_uids = {note.uid for note in self._sliders_at(self.shiny_at)}
        slider_uids = {note.uid for note in self._sliders_at(self.slider_at)}
        self.assertEqual(grabbed, slider_uids)
        self.assertFalse(grabbed & shiny_uids, "the shiny came along for the ride")

    def test_a_drag_on_the_shiny_leaves_the_fake_slider_alone(self) -> None:
        self._place_pair()
        _upper, lower = self._rows()
        self.layer.set_tool("select")
        self.layer.mousePressEvent(self._press(self.layer.x_for_time(self.slider_at), lower))
        grabbed = set(self.layer._move_note_uids)
        self.layer.releaseMouse()
        slider_uids = {note.uid for note in self._sliders_at(self.slider_at)}
        self.assertTrue(grabbed)
        self.assertFalse(grabbed & slider_uids, "the fake slider came along for the ride")

    def test_a_right_click_deletes_the_row_it_landed_on(self) -> None:
        """Right click resolves the same way -- deleting the wrong one of a
        pair is the same bug with a worse ending."""
        self._place_pair()
        upper, _lower = self._rows()
        x = self.layer.x_for_time(self.shiny_at)
        self.layer.mousePressEvent(QMouseEvent(
            QEvent.MouseButtonPress, QPointF(x, upper), QPointF(x, upper),
            Qt.RightButton, Qt.RightButton, Qt.NoModifier,
        ))
        self.assertEqual(self._sliders_at(self.slider_at), [])
        self.assertEqual(len(self._sliders_at(self.shiny_at)), self.config.shiny_count)

    def test_a_view_without_rows_still_ignores_y(self) -> None:
        """Every other view draws one row, so a y anywhere in it must hit."""
        chart = self.window._gimmick_views[0].chart_view
        chart.resize(800, self.window.GIMMICK_LAYER_HEIGHT)
        note = chart.notes[0]
        chart.current_time = float(note.time)
        self.assertIs(chart._note_near_x(chart.x_for_time(note.time), y=1.0), note)


class LineOnlyDeleteTests(_Layer2, unittest.TestCase):
    """Right click on a fake slider's red line removes that line alone; right
    click on the slider still removes both."""

    def _place_one(self) -> int:
        self.window._place_gimmick("fake_slider", "regular", self.SNAP)
        self.layer.current_time = float(self.SNAP)
        # A plain fake slider's drawn line is its own millisecond. The fixture
        # map has fake sliders of its own, so pick ours rather than "the" one.
        heads = self.window._fake_slider_lines(self.document)[0]
        self.assertIn(self.slider_at, heads)
        return self.slider_at

    def _right_click(self, x: float, y: float) -> None:
        self.layer.mousePressEvent(QMouseEvent(
            QEvent.MouseButtonPress, QPointF(x, y), QPointF(x, y),
            Qt.RightButton, Qt.RightButton, Qt.NoModifier,
        ))

    def _reds_at(self, time_ms):
        return [
            point for point in self.document.timing_points
            if point.uninherited and round(point.time) == time_ms
        ]

    def test_a_right_click_on_the_line_keeps_the_slider(self) -> None:
        head = self._place_one()
        sliders = len(self._sliders_at(self.slider_at))
        self.assertTrue(sliders)
        self.assertTrue(self._reds_at(head))
        _upper, lower = self._rows()
        # The shiny row is empty here, so this is the line and not a circle.
        self._right_click(self.layer.x_for_time(head), lower)
        self.assertEqual(self._reds_at(head), [])
        self.assertEqual(len(self._sliders_at(self.slider_at)), sliders)
        self.state.history.undo(self.state)
        self.assertTrue(self._reds_at(head), "one undo should bring the line back")

    def test_a_squash_line_goes_alone_too(self) -> None:
        """A Don's drawn line is the gimmick-BPM squash on the note it hides.
        That is the line `_expand_move` grows into the slider one offset
        later, so routed through the structure delete the slider went too."""
        note = next(
            n for n in self.document.hit_objects
            if n.is_circle and not gui.MainWindow.is_fake_slider(n)
        )
        before = set(self.window._fake_slider_lines(self.document)[0])
        self.window._place_gimmick("fake_slider", "don", float(note.time))
        new_heads = set(self.window._fake_slider_lines(self.document)[0]) - before
        self.assertEqual(len(new_heads), 1)
        head = next(iter(new_heads))
        self.assertTrue(any(
            p.bpm == self.config.gimmick_bpm for p in self._reds_at(head)
        ), "expected the Don to be squashed by a gimmick-BPM line")
        slider_at = head + self.config.fake_slider_offset_ms
        sliders = len(self._sliders_at(slider_at))
        self.assertTrue(sliders)
        self.layer.current_time = float(head)
        _upper, lower = self._rows()
        self._right_click(self.layer.x_for_time(head), lower)
        self.assertEqual(self._reds_at(head), [])
        self.assertEqual(len(self._sliders_at(slider_at)), sliders)

    def test_a_right_click_on_the_slider_still_takes_both(self) -> None:
        head = self._place_one()
        upper, _lower = self._rows()
        self._right_click(self.layer.x_for_time(self.slider_at), upper)
        self.assertEqual(self._sliders_at(self.slider_at), [])
        self.assertEqual(self._reds_at(head), [])


class RedLineDialogTests(_Layer2, unittest.TestCase):
    """Double click in layer 2: the object's length first, its red line under
    it second -- the same order `mousePressEvent` resolves a grab in.

    A plain fake slider and the line squashing it share a millisecond, so on
    that column the object wins and the BPM dialog is not offered. The line's
    BPM is 60000 by construction; the length is the number that is actually
    tuned.
    """

    def test_the_line_opens_and_can_be_dragged_here(self) -> None:
        """Editing was off so that a line could not be pulled out from under
        its slider -- but a drag goes through `_expand_move`, which takes the
        slider with it. Off, a red line that landed on a fake slider was
        adopted by this layer and then could not be moved at all."""
        self.assertTrue(self.layer.timing_dialog_enabled)
        self.assertTrue(self.layer.timing_edit_enabled)

    def test_dragging_the_line_takes_its_slider_with_it(self) -> None:
        self._place_pair()
        reds = [
            point for point in self.document.timing_points
            if point.uninherited and round(point.time) in (self.slider_at, self.SNAP)
        ]
        line = reds
        self.assertTrue(line, "the structure wrote no red line to drag")
        notes, points = self.window._expand_move(self.state, set(), {line[0].uid})
        self.assertTrue(
            any(gui.MainWindow.is_fake_slider(note) for note in notes),
            "a drag on the line must carry the object it belongs to",
        )

    def _double_click(self, at_ms) -> list:
        seen = []
        self.layer.timing_line_edit_requested.connect(seen.append)
        x = self.layer.x_for_time(at_ms)
        # The window is already connected to both signals and opens the real,
        # modal dialog on either -- see _StubTimingLineDialog. QInputDialog is
        # the length editor's, and unpatched it hangs the run.
        _StubTimingLineDialog.accepted = False
        with patch.object(gui, "TimingLineDialog", _StubTimingLineDialog),                 patch.object(gui.QInputDialog, "getDouble", return_value=(0.0, False)):
            self.layer.mouseDoubleClickEvent(QMouseEvent(
                QEvent.MouseButtonDblClick, QPointF(x, 20.0), QPointF(x, 20.0),
                Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
            ))
        return seen

    def test_the_object_outranks_the_line_sharing_its_millisecond(self) -> None:
        self.window._place_gimmick("fake_slider", "regular", self.SNAP)
        self.layer.current_time = float(self.SNAP)
        lengths = []
        self.layer.note_length_edit_requested.connect(lengths.append)
        slider = self._sliders_at(self.slider_at)[0]

        self.assertEqual(self._double_click(self.slider_at), [], "the line won")
        self.assertEqual(lengths, [slider.uid])

    def test_a_line_this_layer_does_not_draw_is_not_offered(self) -> None:
        """The fixture's own timing at 0 belongs to no fake slider, so it is
        not drawn here -- and a dialog for an invisible line is worse than no
        dialog at all."""
        self.layer.current_time = 0.0
        self.assertEqual(self._double_click(0), [])

    def test_the_edit_is_one_undo_step(self) -> None:
        self.window._place_gimmick("fake_slider", "regular", self.SNAP)
        point = next(
            p for p in self.document.timing_points
            if p.uninherited and round(p.time) == self.slider_at
        )
        _StubTimingLineDialog.accepted = True
        _StubTimingLineDialog.values = {"beat_length": (point.beat_length, 300.0)}
        before = len(self.state.history.undo_stack)
        with patch.object(gui, "TimingLineDialog", _StubTimingLineDialog):
            self.window._edit_timing_line(self.state.source_path, point.uid)
        self.assertEqual(len(self.state.history.undo_stack), before + 1)
        self.assertAlmostEqual(point.bpm, 200.0)


class LengthDialogTests(_Layer2, unittest.TestCase):
    """Double click a fake slider to type its `length`.

    The one number that decides how far it draws and the one no drag can
    reach: its end precedes its start, so it has no right edge to pull.
    """

    def _slider(self):
        self.window._place_gimmick("fake_slider", "regular", self.SNAP)
        return self._sliders_at(self.slider_at)[0]

    def _type(self, value, accepted=True):
        with patch.object(gui.QInputDialog, "getDouble",
                          return_value=(value, accepted)) as dialog:
            self.window._type_note_length(self.state.source_path, self._uid)
        return dialog

    def test_typing_a_length_rewrites_the_object_in_one_undo_step(self) -> None:
        slider = self._slider()
        self._uid = slider.uid
        before = len(self.state.history.undo_stack)
        self._type(-500.0)
        self.assertEqual(slider.length, -500.0)
        self.assertEqual(len(self.state.history.undo_stack), before + 1)

    def test_the_curve_and_slide_count_survive(self) -> None:
        """Only the length changes: the rest of the object is not this
        dialog's to rewrite."""
        slider = self._slider()
        self._uid = slider.uid
        curve, slides = slider.extras[0], slider.extras[1]
        self._type(-500.0)
        self.assertEqual((slider.extras[0], slider.extras[1]), (curve, slides))

    def test_cancelling_changes_nothing(self) -> None:
        slider = self._slider()
        self._uid = slider.uid
        was = slider.extras
        before = len(self.state.history.undo_stack)
        self._type(-500.0, accepted=False)
        self.assertEqual(slider.extras, was)
        self.assertEqual(len(self.state.history.undo_stack), before)

    def test_the_dialog_cannot_ask_for_a_real_drumroll(self) -> None:
        """Past FAKE_SLIDER_MAX_LENGTH the object stops being a fake slider,
        which is a different object rather than an edit to this one."""
        slider = self._slider()
        self._uid = slider.uid
        dialog = self._type(-500.0)
        _self, _title, _label, _value, low, high, _decimals = dialog.call_args.args
        self.assertEqual(high, gui.FAKE_SLIDER_MAX_LENGTH)
        self.assertLess(low, high)

    def test_a_real_drumroll_is_not_offered_the_dialog(self) -> None:
        note = next(n for n in self.document.hit_objects
                    if not self.window.is_fake_slider(n))
        self._uid = note.uid
        before = len(self.state.history.undo_stack)
        with patch.object(gui.QInputDialog, "getDouble") as dialog:
            self.window._type_note_length(self.state.source_path, note.uid)
        dialog.assert_not_called()
        self.assertEqual(len(self.state.history.undo_stack), before)


class BigVariantTests(_Layer2, unittest.TestCase):
    """Shift places the big variant, the same bit a Shift-clicked Don gets."""

    def test_a_plain_fake_slider_is_small_without_shift(self) -> None:
        self.window._place_gimmick("fake_slider", "regular", self.SNAP)
        self.assertFalse(self._sliders_at(self.slider_at)[0].is_finisher)

    def test_shift_makes_the_fake_slider_big(self) -> None:
        self.window._place_gimmick("fake_slider", "regular", self.SNAP, True)
        self.assertTrue(self._sliders_at(self.slider_at)[0].is_finisher)

    def test_shift_makes_every_slider_in_a_shiny_big(self) -> None:
        self.window._place_gimmick("fake_slider", "shiny", self.SNAP, True)
        stack = self._sliders_at(self.shiny_at)
        self.assertEqual(len(stack), self.config.shiny_count)
        self.assertTrue(all(note.is_finisher for note in stack))

    def test_shift_does_not_resize_a_don(self) -> None:
        """Don and Kat already spend the finisher bit saying which they are."""
        self.window._place_gimmick("fake_slider", "don", self.SNAP, True)
        self.assertFalse(self._sliders_at(self.slider_at)[0].is_finisher)

    def test_the_views_shift_flag_reaches_the_placement(self) -> None:
        """The layer's signal used to drop `big` on the floor."""
        self.layer.note_place_requested.emit("regular", float(self.SNAP), False, True)
        self.assertTrue(self._sliders_at(self.slider_at)[0].is_finisher)

    def test_the_ghost_previews_the_big_size(self) -> None:
        self.layer.set_tool("regular")
        self.layer.current_time = float(self.SNAP)
        self.layer._hover_time = float(self.SNAP)
        with patch.object(
            gui, "shift_is_held", staticmethod(lambda: True)
        ):
            big = self._ghost_radius()
        with patch.object(
            gui, "shift_is_held", staticmethod(lambda: False)
        ):
            small = self._ghost_radius()
        self.assertGreater(big, small)

    def _ghost_radius(self) -> float:
        """The radius `_draw_placement_ghost` hands the layer ghost."""
        seen = []
        with patch.object(
            gui.TimelineGameplay, "_draw_layer_ghost",
            lambda self, painter, x, baseline_y, radius: seen.append(radius),
        ):
            self.layer._draw_placement_ghost(None, 60, 10.0, 20.0)
        return seen[0]

    def _multi_ghosts(self) -> list:
        """Every (x, radius) the Multiple tool's ghost would draw."""
        seen = []
        with patch.object(
            gui.TimelineGameplay, "_draw_layer_ghost",
            lambda self, painter, x, baseline_y, radius: seen.append((x, radius)),
        ):
            self.layer._draw_placement_ghost(None, 60, 10.0, 20.0)
        return seen

    def test_the_multiple_tool_previews_the_whole_run(self) -> None:
        """It draws N ghosts, one per structure the click would place -- a
        single dot under the cursor said nothing about a run of nine."""
        self.layer.set_tool("multi")
        self.layer.multi_run = (0, 16, 2, 2)
        self.layer.current_time = float(self.SNAP)
        self.layer._hover_time = float(self.SNAP)
        with patch.object(
            gui, "shift_is_held", staticmethod(lambda: False)
        ):
            ghosts = self._multi_ghosts()
        self.assertEqual(len(ghosts), 9, "0..16 step 2 is nine structures")
        # Strictly increasing x: one per position, in run order.
        self.assertEqual([x for x, _ in ghosts], sorted(x for x, _ in ghosts))

    def test_the_multiple_tool_ghost_previews_the_big_size(self) -> None:
        """Shift sizes every ghost in the run, the same way it sizes the single
        Fake Slider tool's -- "multi" was missing from `_draw_placement_ghost`'s
        tool list entirely, so it drew nothing and Shift showed nothing."""
        self.layer.set_tool("multi")
        self.layer.multi_run = (0, 16, 2, 2)
        self.layer.current_time = float(self.SNAP)
        self.layer._hover_time = float(self.SNAP)
        with patch.object(
            gui, "shift_is_held", staticmethod(lambda: True)
        ):
            big = self._multi_ghosts()
        with patch.object(
            gui, "shift_is_held", staticmethod(lambda: False)
        ):
            small = self._multi_ghosts()
        self.assertEqual(len(big), len(small))
        self.assertTrue(big and small)
        for (_, big_r), (_, small_r) in zip(big, small):
            self.assertGreater(big_r, small_r)

    def test_the_multiple_tool_draws_nothing_without_a_run(self) -> None:
        """Selecting the tool and cancelling its dialog leaves no run shape."""
        self.layer.set_tool("multi")
        self.layer.multi_run = None
        self.layer._hover_time = float(self.SNAP)
        self.assertEqual(self._multi_ghosts(), [])


class ShinyMarkTests(unittest.TestCase):
    """A shiny is marked white in the editor, for separation.

    The colour here is a *label*, not a simulation: white is the one thing that
    cannot be mistaken for the drumroll yellow beside it, so a stack reads as
    different from a lone fake slider at a glance.

    Pinned because it has already been undone once. It was changed to a light
    yellow on the grounds that a stack does not wash out to white in play --
    which is true of the *game*, and irrelevant here, because this view is for
    editing and needs the two to be tellable apart.
    """

    def test_a_shiny_is_white_and_a_fake_slider_is_not(self):
        view = gui.TimelineGameplay()
        shiny, plain = view.shiny_brush, view.slider_brush
        self.assertGreater(min(shiny.red(), shiny.green(), shiny.blue()), 230)
        self.assertLess(plain.blue(), 60, "the drumroll stays yellow")

    def test_the_ghost_follows_the_same_split(self):
        """Placement previews have to read the same way the placed thing does,
        or the preview says one object and the click makes another."""
        view = gui.TimelineGameplay()
        for solid, ghost in (
            (view.shiny_brush, view.ghost_shiny_brush),
            (view.slider_brush, view.ghost_slider_brush),
        ):
            with self.subTest(colour=solid.name()):
                self.assertEqual(
                    (solid.red(), solid.green(), solid.blue()),
                    (ghost.red(), ghost.green(), ghost.blue()),
                )
                self.assertLess(ghost.alpha(), solid.alpha())

    def test_the_opacity_setting_keeps_them_apart(self):
        """`set_note_opacity` rescales every fill from its own base, so the two
        cannot converge at some setting."""
        view = gui.TimelineGameplay()
        for percent in (gui.NOTE_OPACITY_MIN_PERCENT, 60, 100):
            with self.subTest(percent=percent):
                view.set_note_opacity(percent)
                self.assertGreater(
                    view.shiny_brush.blue(), view.slider_brush.blue() + 100)


class EditorRollCapTests(unittest.TestCase):
    """The editor draws a fake slider as a head and nothing else.

    `_draw_skinned_roll_body` stamps `taiko-roll-end` at the far end of the
    track whether or not a track was wide enough to draw, so a fake slider --
    whose end precedes its start, or trails it by a rounding artefact -- wore a
    cap butted straight against its own head. The scrolling body and its cap
    belong to the gameplay preview, which draws the extent to scale.
    """

    def _view(self, length):
        view = gui.TimelineGameplay()
        view.resize(800, 180)
        document = OsuDocument(
            source_path=None, lines=[], encoding="utf-8", version="v14",
            audio_filename="a.mp3",
            hit_objects=[
                gui.HitObject(
                    x=256, y=192, time=1000, type=2, hit_sound=0,
                    extras=("L|624:192", "1", repr(length)),
                )
            ],
            timing_points=[
                gui.TimingPoint(time=0, beat_length=500.0, meter=4, uninherited_flag=1)
            ],
        )
        view.load_document(document)
        view.current_time = 1000.0
        return view

    def _bodies_drawn(self, length):
        view = self._view(length)
        calls = []
        original = gui.TimelineGameplay._draw_skinned_roll_body
        # `1`, not the arguments: they include the live QPainter, and keeping a
        # reference to it past the paint event aborts the process.
        gui.TimelineGameplay._draw_skinned_roll_body = (
            lambda self, *a: calls.append(1) or False
        )
        try:
            view.grab()
        finally:
            gui.TimelineGameplay._draw_skinned_roll_body = original
        return calls

    def test_a_fake_slider_gets_no_body_or_cap(self):
        for length in (-0.0001, 0.001, -800.0):
            with self.subTest(length=length):
                self.assertEqual(self._bodies_drawn(length), [])

    def test_a_real_drumroll_still_does(self):
        self.assertEqual(len(self._bodies_drawn(800.0)), 1)


class FakeSliderLengthTests(unittest.TestCase):
    """What counts as a fake slider. The canonical form is negative, but the
    positive side of zero is the same trick: `mekurume` writes 616 of its 640
    sliders as `0.001`, whose derived duration is 0.0045ms -- no tick, nothing
    hittable. Read as `< 0` they were ordinary drumrolls and never reached
    layer 2."""

    def _slider(self, length):
        return gui.HitObject(
            x=256, y=192, time=1000, type=2, hit_sound=0,
            extras=("L|624:192", "1", repr(length)),
        )

    def test_a_near_zero_positive_length_is_a_fake_slider(self):
        for length in (0.001, 0.0, -0.0001, -800.0):
            with self.subTest(length=length):
                self.assertTrue(gui.MainWindow.is_fake_slider(self._slider(length)))

    def test_a_real_drumroll_is_not(self):
        for length in (0.002, 1.0, 800.0):
            with self.subTest(length=length):
                self.assertFalse(gui.MainWindow.is_fake_slider(self._slider(length)))

    def test_a_circle_is_not(self):
        circle = gui.HitObject(x=256, y=192, time=1000, type=1, hit_sound=0)
        self.assertFalse(gui.MainWindow.is_fake_slider(circle))


if __name__ == "__main__":
    unittest.main()
