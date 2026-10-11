"""Sixth owner feedback round: the convert tools, the two effects bits, the
standalone red line BPM, and oscillating SV.

Kept out of `test_gimmick_editor.py` on purpose -- that file builds a window
per test and takes minutes to run whole, so a round's worth of new behaviour
gets its own module that can be run on its own.
"""
from __future__ import annotations

import dataclasses
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from chart import gimmick_session as gs
from ui import gui
from ui import chart_views
from ui import dialogs
from tests.test_gimmick_editor import _GimmickFixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _Session(_GimmickFixture):
    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document

    def _red(self, time_ms):
        return [p for p in self.document.timing_points if p.time == time_ms and p.uninherited]


class ConvertToolTests(_Session, unittest.TestCase):
    """Turning a range of the chart's own notes into gimmick structures.

    Convert Notes asks for its numbers now (`ConvertNotesDialog`), so every
    test here answers it with the layer's own defaults -- which is what these
    were written against. `ConvertNotesDialogTests` is where the answering
    itself is the subject.
    """

    def setUp(self) -> None:
        super().setUp()
        original = gui.ConvertNotesDialog

        class Accepted(original):
            def exec(self):
                return gui.QDialog.DialogCode.Accepted

        gui.ConvertNotesDialog = Accepted
        self.addCleanup(setattr, gui, "ConvertNotesDialog", original)

    def _range_of_notes(self, count: int) -> tuple[float, float, list]:
        notes = sorted(
            (n for n in self.document.hit_objects if n.is_circle), key=lambda n: n.time
        )[:count]
        return notes[0].time, notes[-1].time, notes

    def test_a_fake_slider_conversion_decorates_every_note_in_the_range(self):
        start, end, notes = self._range_of_notes(3)
        before = len(self.document.hit_objects)
        # The fixture already carries a drumroll of its own, so this counts
        # what the conversion added rather than what is in the map.
        existing = {n.uid for n in self.document.hit_objects}

        self.window._convert_notes_to_gimmick(
            self.window._gimmick_pairing.target, "fake_slider", start, end
        )

        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        sliders = [
            n for n in self.document.hit_objects
            if n.uid not in existing and gui.MainWindow.is_fake_slider(n)
        ]
        self.assertEqual(len(sliders), len(notes))
        self.assertEqual(
            sorted(round(n.time) for n in sliders),
            sorted(round(n.time) + offset for n in notes),
        )
        # The chart's own notes are kept, not replaced: only sliders were added.
        self.assertEqual(len(self.document.hit_objects) - before, len(notes))

    def test_a_kat_converts_to_the_big_fake_slider(self):
        kat = next(n for n in self.document.hit_objects if n.is_circle and n.is_kat)
        self.window._convert_notes_to_gimmick(
            self.window._gimmick_pairing.target, "fake_slider", kat.time, kat.time
        )
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        slider = next(
            n for n in self.document.hit_objects
            if gui.MainWindow.is_fake_slider(n) and round(n.time) == round(kat.time) + offset
        )
        self.assertTrue(slider.is_finisher)

    def test_a_barline_conversion_writes_lines_and_no_extra_notes(self):
        start, end, notes = self._range_of_notes(3)
        before = len(self.document.hit_objects)
        reds = len([p for p in self.document.timing_points if p.uninherited])

        self.window._convert_notes_to_gimmick(
            self.window._gimmick_pairing.target, "barline", start, end
        )

        self.assertEqual(len(self.document.hit_objects), before, "no note is added")
        self.assertGreater(
            len([p for p in self.document.timing_points if p.uninherited]), reds
        )
        for note in notes:
            self.assertAlmostEqual(self._red(note.time)[0].bpm, 60000.0)

    def test_the_whole_conversion_is_one_undo_step(self):
        start, end, _ = self._range_of_notes(3)
        points_before = list(self.document.timing_points)
        notes_before = list(self.document.hit_objects)

        self.window._convert_notes_to_gimmick(
            self.window._gimmick_pairing.target, "fake_slider", start, end
        )
        self.state.history.undo(self.state)

        self.assertEqual(self.document.timing_points, points_before)
        self.assertEqual(self.document.hit_objects, notes_before)

    def test_converted_notes_each_get_their_own_selection_key(self):
        """`_next_original_index` reads the document, which a batch has not
        written to yet -- asking it per note gave every one the same key."""
        start, end, notes = self._range_of_notes(3)
        self.window._convert_notes_to_gimmick(
            self.window._gimmick_pairing.target, "fake_slider", start, end
        )
        indices = [n.original_index for n in self.document.hit_objects]
        self.assertEqual(len(indices), len(set(indices)))

    def test_both_layers_offer_the_tool(self):
        for layer in ("fake_slider", "barline"):
            self.assertIn("convert", dict(gui.MainWindow.GIMMICK_TOOLSETS[layer]))


class AntiBarlineToolTests(_Session, unittest.TestCase):
    """The barline layer's anti-barline converter, wired end to end."""

    def _range_of_notes(self, count: int) -> tuple[float, float, list]:
        notes = sorted(
            (n for n in self.document.hit_objects if n.is_circle and not n.is_finisher),
            key=lambda n: n.time,
        )[:count]
        # Note-to-note, no padding: the wall reaching past both is the point.
        return notes[0].time, notes[-1].time, notes

    def _convert(self, start, end):
        self.window._convert_to_anti_barline(
            self.window._gimmick_pairing.target, start, end
        )

    def test_it_is_reached_through_convert_notes_and_not_its_own_button(self):
        """It had a fourth button in the barline row until Convert Notes could
        draw all three structures; a second way in that could only use the
        layer's saved numbers was the worse one."""
        self.assertNotIn("anti", dict(gui.MainWindow.GIMMICK_TOOLSETS["barline"]))
        self.assertNotIn("anti", self.window.gimmick_tool_buttons["barline"])
        self.assertNotIn("anti", gui.TimelineGameplay.RANGE_TOOLS)
        self.assertEqual(gui.ConvertNotesDialog.ANTI, "anti")

    def test_it_writes_a_wall_and_leaves_the_notes_alone(self):
        start, end, notes = self._range_of_notes(3)
        before = list(self.document.hit_objects)
        reds = len([p for p in self.document.timing_points if p.uninherited])

        self._convert(start, end)

        self.assertEqual(self.document.hit_objects, before, "no note is added or moved")
        self.assertGreater(
            len([p for p in self.document.timing_points if p.uninherited]), reds + 10,
            "a wall is many lines, not a handful",
        )
        for note in notes:
            squash = [
                p for p in self.document.timing_points
                if p.uninherited and round(p.time) == round(note.time)
            ]
            self.assertTrue(squash and squash[0].omit_first_barline)

    def test_the_wall_shows_up_in_the_sv_barlines_layer(self):
        """Layer 6 owns every red line's millisecond already, but it draws
        *green* lines -- so without a handle on each one the wall was a
        thousand barlines the layer listed and could not show."""
        start, end, _ = self._range_of_notes(3)
        before = {round(p.time) for p in self.document.timing_points if p.uninherited}

        self._convert(start, end)

        wall = {round(p.time) for p in self.document.timing_points if p.uninherited} - before
        greens = {round(p.time) for p in self.document.timing_points if p.inherited}
        owned = self.window._sv_layer_times("sv_barline", self.document)
        # The note squash/restore pair is in `wall` too and is deliberately
        # bare -- those two carry omit-first-barline and draw no bar at all.
        notes = {round(n.time) for n in self.document.hit_objects}
        bars = {at for at in wall if at not in notes and at - 1 not in notes}
        self.assertTrue(bars)
        self.assertEqual(bars - greens, set(), "a wall line with no green handle")
        self.assertEqual(bars - owned, set(), "a wall line the layer does not own")

    def test_the_first_and_last_note_are_slits_too(self):
        """Dragged exactly note-to-note -- which is what a drag over a run of
        notes is -- both edge notes still end up as holes in the sheet rather
        than at the edge of it."""
        start, end, notes = self._range_of_notes(3)
        before = {round(p.time) for p in self.document.timing_points if p.uninherited}

        self._convert(start, end)

        note_times = {round(n.time) for n in notes}
        wall = sorted(
            round(p.time) for p in self.document.timing_points
            if p.uninherited and p.meter == 4 and round(p.time) not in before
            and round(p.time) not in note_times
        )
        for note in (notes[0], notes[-1]):
            with self.subTest(note=note.time):
                self.assertTrue([t for t in wall if t < note.time - 1])
                self.assertTrue([t for t in wall if t > note.time + 1])

    def test_the_whole_wall_is_one_undo_step(self):
        start, end, _ = self._range_of_notes(3)
        points_before = list(self.document.timing_points)

        self._convert(start, end)
        self.state.history.undo(self.state)

        self.assertEqual(self.document.timing_points, points_before)

    def test_a_range_with_nothing_convertible_in_it_writes_nothing(self):
        points_before = list(self.document.timing_points)
        toasts = []
        self.window.show_toast = lambda text: toasts.append(text)

        self._convert(1.0, 2.0)

        self.assertEqual(self.document.timing_points, points_before)
        self.assertTrue(toasts, "the user is told why nothing happened")

    def test_the_wall_never_stacks_on_the_maps_own_red_lines(self):
        """Only the first uninherited point on a millisecond is meaningful;
        a second is a line the layer draws and hit-tests forever for nothing."""
        start, end, _ = self._range_of_notes(4)
        self._convert(start, end)
        reds = [round(p.time) for p in self.document.timing_points if p.uninherited]
        self.assertEqual(len(reds), len(set(reds)))


class ConvertNotesDialogTests(_Session, unittest.TestCase):
    """Convert Notes in the barline layer asks which structure, and its numbers.

    Three converters behind one drag, and the numbers are per call -- the layer
    Config is only what the boxes open on.
    """

    def _dialog(self, layer_id="barline"):
        return gui.ConvertNotesDialog(self.window._gimmick_config(layer_id), layer_id)

    def test_a_tile_picks_the_structure_and_shows_only_its_numbers(self):
        dialog = self._dialog()
        tiles = [b for b in dialog.findChildren(gui.QToolButton) if b.objectName() == "tile"]
        self.assertEqual(len(tiles), 3)
        tiles[2].click()
        self.assertEqual(dialog.mode(), gui.ConvertNotesDialog.HIDDEN)
        sections = dialog.sheet.sections
        self.assertFalse(sections[gui.ConvertNotesDialog.HIDDEN].isHidden())
        self.assertTrue(sections[gui.ConvertNotesDialog.STRUCTURE].isHidden())
        # The combo stays the model: setting it moves the tiles too.
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(gui.ConvertNotesDialog.ANTI))
        self.assertTrue(tiles[1].isChecked())
        dialog.deleteLater()

    def _range_of_notes(self, count: int):
        notes = sorted(
            (n for n in self.document.hit_objects if n.is_circle and not n.is_finisher),
            key=lambda n: n.time,
        )[:count]
        return notes[0].time, notes[-1].time, notes

    def _convert_with(self, mode, start, end, layer_id="barline", checks=(), **edits):
        """Run Convert Notes with the dialog answered for us."""
        window = self.window

        class Answered(gui.ConvertNotesDialog):
            def exec(self):
                index = self.mode_combo.findData(mode)
                if index >= 0:
                    self.mode_combo.setCurrentIndex(index)
                for name, value in edits.items():
                    getattr(self, name).setValue(value)
                for name, value in checks:
                    getattr(self, name).setChecked(value)
                return gui.QDialog.DialogCode.Accepted

        original = gui.ConvertNotesDialog
        gui.ConvertNotesDialog = Answered
        try:
            window._convert_notes_to_gimmick(
                window._gimmick_pairing.target, layer_id, start, end
            )
        finally:
            gui.ConvertNotesDialog = original

    def test_the_barline_layer_offers_the_three_converters(self):
        dialog = self._dialog()
        try:
            modes = [dialog.mode_combo.itemData(i) for i in range(dialog.mode_combo.count())]
            self.assertEqual(modes, ["structure", "anti", "hidden"])
        finally:
            dialog.deleteLater()

    def test_the_fake_slider_layer_asks_only_for_its_numbers(self):
        """One structure to draw, so nothing to choose between -- but the same
        per-call numbers, and the same option about the note."""
        dialog = self._dialog("fake_slider")
        try:
            self.assertEqual(dialog.mode(), gui.ConvertNotesDialog.STRUCTURE)
            self.assertEqual(dialog.mode_combo.count(), 1)
            self.assertEqual(
                dialog.fake_offset_spin.value(),
                self.window._gimmick_config("fake_slider").fake_slider_offset_ms,
            )
            self.assertTrue(dialog.hide_note_check.isChecked())
        finally:
            dialog.deleteLater()

    def test_showing_the_note_writes_the_charts_own_bpm_with_no_bar(self):
        """The squash is what makes the note invisible, so the option is which
        BPM that line carries -- and detached, or it stamps a bar over the note
        it was just told to show."""
        start, end, notes = self._range_of_notes(3)
        self._convert_with(
            "structure", start, end, checks=(("hide_note_check", False),),
        )
        at = round(notes[0].time)
        own = [
            point for point in self.document.timing_points
            if point.uninherited and round(point.time) == at
        ]
        self.assertTrue(own)
        self.assertNotAlmostEqual(own[-1].bpm, gs.DEFAULT_GIMMICK_BPM, places=3)
        self.assertTrue(own[-1].omit_first_barline)

    def test_hiding_the_note_is_the_default_and_still_squashes(self):
        start, end, notes = self._range_of_notes(3)
        self._convert_with("structure", start, end)
        at = round(notes[0].time)
        own = [
            point for point in self.document.timing_points
            if point.uninherited and round(point.time) == at
        ]
        self.assertTrue(any(
            abs(point.bpm - gs.DEFAULT_GIMMICK_BPM) < 1e-3 for point in own
        ))

    def test_the_boxes_open_on_the_layer_config(self):
        dialog = self._dialog()
        try:
            config = self.window._gimmick_config("barline")
            self.assertEqual(dialog.spacing_spin.value(), config.spacing_ms)
            self.assertEqual(dialog.anti_don_spin.value(), config.anti_don_ticks)
            self.assertAlmostEqual(
                dialog.hidden_kat_sv_spin.value(), config.hidden_kat_sv, places=8,
            )
        finally:
            dialog.deleteLater()

    def test_an_edit_reaches_the_config_without_touching_the_others(self):
        """Only the active page's fields are applied, so picking a converter
        cannot quietly rewrite the numbers another one uses."""
        dialog = self._dialog()
        try:
            base = self.window._gimmick_config("barline")
            dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("hidden"))
            dialog.hidden_wall_bpm_spin.setValue(999.0)
            dialog.anti_don_spin.setValue(base.anti_don_ticks + 5)
            edited = dialog.config()
            self.assertEqual(edited.hidden_wall_bpm, 999.0)
            self.assertEqual(edited.anti_don_ticks, base.anti_don_ticks)
        finally:
            dialog.deleteLater()

    def test_the_fake_slider_layer_converts_through_the_same_dialog(self):
        start, end, _ = self._range_of_notes(3)
        before = len(self.document.timing_points)
        self._convert_with("structure", start, end, layer_id="fake_slider")
        self.assertGreater(len(self.document.timing_points), before)

    def test_hidden_mode_writes_the_hidden_structure(self):
        start, end, notes = self._range_of_notes(3)
        before = list(self.document.hit_objects)

        self._convert_with("hidden", start, end)

        self.assertEqual(self.document.hit_objects, before, "no note is added or moved")
        hide = [
            p for p in self.document.timing_points
            if p.uninherited and round(p.time) == round(notes[0].time)
        ]
        self.assertTrue(hide)
        self.assertAlmostEqual(hide[-1].bpm, gs.DEFAULT_HIDDEN_HIDE_BPM, places=3)
        self.assertTrue(hide[-1].omit_first_barline)

    def test_the_dialogs_numbers_do_not_reach_the_saved_config(self):
        """Per call: a section whose slits want to be wider than the last one's
        is the normal case, not a reason to re-save the layer."""
        start, end, _ = self._range_of_notes(3)
        before = self.window._gimmick_config("barline")

        self._convert_with("hidden", start, end, hidden_wall_bpm_spin=999.0)

        self.assertEqual(self.window._gimmick_config("barline"), before)

    def test_cancelling_writes_nothing(self):
        start, end, _ = self._range_of_notes(3)
        points_before = list(self.document.timing_points)

        class Cancelled(gui.ConvertNotesDialog):
            def exec(self):
                return gui.QDialog.DialogCode.Rejected

        original = gui.ConvertNotesDialog
        gui.ConvertNotesDialog = Cancelled
        try:
            self.window._convert_notes_to_gimmick(
                self.window._gimmick_pairing.target, "barline", start, end
            )
        finally:
            gui.ConvertNotesDialog = original
        self.assertEqual(self.document.timing_points, points_before)

    def test_cancelling_writes_nothing_in_the_fake_slider_layer_either(self):
        start, end, _ = self._range_of_notes(3)
        points_before = list(self.document.timing_points)

        class Cancelled(gui.ConvertNotesDialog):
            def exec(self):
                return gui.QDialog.DialogCode.Rejected

        original = gui.ConvertNotesDialog
        gui.ConvertNotesDialog = Cancelled
        try:
            self.window._convert_notes_to_gimmick(
                self.window._gimmick_pairing.target, "fake_slider", start, end
            )
        finally:
            gui.ConvertNotesDialog = original
        self.assertEqual(self.document.timing_points, points_before)


class DeleteKeepsTheChartTests(_Session, unittest.TestCase):
    """Deleting a gimmick takes the gimmick, not the music under it.

    A structure is drawn *around* a note that was already there
    (`_without_redundant_note`), and `_expand_move` pulls that note in so a
    drag moves the two together. On a delete that same expansion was removing
    a beat of the map nobody asked to lose.
    """

    def _plain(self, index=0):
        return sorted(
            (n for n in self.document.hit_objects if n.is_circle and not n.is_finisher),
            key=lambda n: n.time,
        )[index]

    def _has(self, note):
        return any(n.uid == note.uid for n in self.document.hit_objects)

    def _red_at(self, time_ms, bpm=60000.0):
        return next(
            p for p in self.document.timing_points
            if p.uninherited and round(p.time) == round(time_ms) and p.bpm == bpm
        )

    def test_removing_a_barline_don_keeps_the_don(self):
        note = self._plain()
        at = round(note.time)
        self.window._place_gimmick("barline", "don", at)

        self.window._delete_gimmick_objects(
            self.window._gimmick_pairing.target, (), [self._red_at(at).uid]
        )

        self.assertTrue(self._has(note), "the chart's own note went with the bars")
        self.assertEqual(
            [p for p in self.document.timing_points
             if p.uninherited and p.bpm == 60000.0 and round(p.time) == at],
            [], "the bars stayed behind",
        )

    def test_removing_a_fake_slider_don_keeps_the_note_and_takes_the_slider(self):
        note = self._plain()
        at = round(note.time)
        self.window._place_gimmick("fake_slider", "don", at)
        slider = next(
            n for n in self.document.hit_objects
            if gui.MainWindow.is_fake_slider(n) and abs(n.time - at) < 10
        )

        self.window._delete_gimmick_objects(
            self.window._gimmick_pairing.target, (), [self._red_at(at).uid]
        )

        self.assertTrue(self._has(note))
        self.assertFalse(self._has(slider), "the drawn object is the gimmick's own")

    def test_removing_an_anti_barline_line_keeps_the_note_it_hid(self):
        note, following = self._plain(0), self._plain(1)
        at = round(note.time)
        self.window._convert_to_anti_barline(
            self.window._gimmick_pairing.target, note.time, following.time
        )
        squash = self._red_at(at)
        wall = [
            p for p in self.document.timing_points
            if p.uninherited and p.meter == 4 and p.time > at + 5
        ][0]

        self.window._delete_gimmick_objects(
            self.window._gimmick_pairing.target, (), [squash.uid, wall.uid]
        )

        self.assertTrue(self._has(note))
        for point in (squash, wall):
            self.assertNotIn(point.uid, [p.uid for p in self.document.timing_points])

    def test_a_note_that_was_actually_selected_still_goes(self):
        """The rule is "not pulled in by expansion", not "never" -- layer 1 is
        where a note is deleted, and it has to keep working."""
        note = self._plain()
        self.window._delete_gimmick_objects(
            self.window._gimmick_pairing.target, [note.uid]
        )
        self.assertFalse(self._has(note))


class EffectsBitTests(_Session, unittest.TestCase):
    def test_a_fake_slider_omits_its_barline_by_default(self):
        self.window._place_gimmick("fake_slider", "don", 10000)
        added = [p for p in self.document.timing_points if p.uninherited and p.bpm == 60000.0]
        self.assertTrue(added)
        self.assertTrue(all(p.omit_first_barline for p in added))

    def test_the_config_can_switch_it_off(self):
        self.window.gimmick_configs["fake_slider"] = gui.GimmickConfig(omit_barline=False)
        self.window._place_gimmick("fake_slider", "don", 10000)
        added = [p for p in self.document.timing_points if p.uninherited and p.bpm == 60000.0]
        self.assertTrue(added)
        self.assertFalse(any(p.omit_first_barline for p in added))

    def test_a_barline_note_still_draws_its_bars(self):
        """The barline layer ignores the flag -- bars are its whole output."""
        self.window._place_gimmick("barline", "kat", 10000)
        added = [p for p in self.document.timing_points if 9995 <= p.time <= 10005]
        self.assertFalse(any(p.omit_first_barline for p in added))

    def test_a_barline_don_is_one_bar_after_and_a_kat_is_three_either_side(self):
        self.window._place_gimmick("barline", "don", 10000)
        self.window._place_gimmick("barline", "kat", 20000)
        self.assertEqual(
            sorted(round(p.time) - 10000 for p in self.document.timing_points
                   if 9990 <= p.time <= 10010 and p.uninherited),
            [0, 1],
        )
        self.assertEqual(
            sorted(round(p.time) - 20000 for p in self.document.timing_points
                   if 19990 <= p.time <= 20010 and p.uninherited),
            [-5, -3, -1, 0, 1, 3, 5],
        )

    def test_a_placement_inside_kiai_keeps_kiai_on(self):
        self.document.timing_points.append(
            gui.TimingPoint.inherited_at(9000, 1.0, kiai=True)
        )
        self.document.timing_points.sort(key=lambda p: p.time)
        self.window._place_gimmick("fake_slider", "kat", 10000)
        added = [
            p for p in self.document.timing_points
            if 10000 <= p.time <= 10005 and p.time != 9000
        ]
        self.assertTrue(added)
        self.assertTrue(all(p.kiai for p in added), "a gimmick must not end the chorus")

    def test_a_custom_redline_bpm_is_what_the_red_line_tool_writes(self):
        self.window.gimmick_configs["barline"] = gui.GimmickConfig(red_line_bpm=45.0)
        self.window._place_gimmick("barline", "red_line", 10000)
        self.assertAlmostEqual(self._red(10000)[0].bpm, 45.0)


class KatSpacingConfigTests(_Session, unittest.TestCase):
    """Kat's three mirrored pairs are independently configurable, in place of
    the old single `spacing_ms` a formula (n, n+2, n+4) used to derive all
    three from. Don keeps `spacing_ms` to itself."""

    def setUp(self) -> None:
        super().setUp()
        # Distinct, none matching the old derived 1/3/5 -- so a test that
        # passed by accident against the old progression would fail here.
        self.window.gimmick_configs["barline"] = gui.GimmickConfig(
            kat_spacing1_ms=2, kat_spacing2_ms=6, kat_spacing3_ms=9,
        )

    def _offsets_near(self, centre_ms):
        return sorted(
            round(p.time) - centre_ms for p in self.document.timing_points
            if centre_ms - 15 <= p.time <= centre_ms + 15 and p.uninherited
        )

    def test_a_custom_kat_writes_lines_at_its_own_three_spacings(self):
        self.window._place_gimmick("barline", "kat", 10000)
        self.assertEqual(self._offsets_near(10000), [-9, -6, -2, 0, 2, 6, 9])

    def test_dons_own_spacing_is_untouched_by_the_kat_config(self):
        self.window._place_gimmick("barline", "don", 10000)
        self.assertEqual(self._offsets_near(10000), [0, 1])

    def test_expand_move_gathers_every_custom_spaced_line(self):
        """`_expand_move` used to grow a drag through +/-(n, n+2, n+4) built
        from one shared spacing -- with Kat's three pairs independent, that
        formula no longer names the lines this structure actually wrote, and
        a drag would have left whichever of them the formula missed behind."""
        self.window._place_gimmick("barline", "kat", 10000)
        note = next(n for n in self.document.hit_objects if n.time == 10000)

        notes, points = self.window._expand_move(self.state, {note.uid}, set())

        self.assertEqual({n.uid for n in notes}, {note.uid})
        self.assertEqual(
            sorted(round(p.time) - 10000 for p in points if p.uninherited),
            [-9, -6, -2, 0, 2, 6, 9],
        )

    def test_a_drag_moves_every_bar_and_leaves_none_behind(self):
        self.window._place_gimmick("barline", "kat", 10000)
        note = next(n for n in self.document.hit_objects if n.time == 10000)

        self.window._move_objects(
            self.window._gimmick_pairing.target, [note.uid], [], 200,
        )

        self.assertEqual(self._offsets_near(10200), [-9, -6, -2, 0, 2, 6, 9])
        self.assertEqual(
            [p for p in self.document.timing_points
             if 9985 <= p.time <= 10015 and p.uninherited],
            [], "nothing left at the structure's old position",
        )


class KatSpacingDialogTests(_Session, unittest.TestCase):
    """`GimmickConfigDialog` must round-trip the three Kat fields even from a
    layer that never shows their rows, and must refuse to close with two of
    them equal rather than handing back a config `GimmickConfig` itself would
    reject."""

    def test_the_dialog_round_trips_the_kat_fields_even_when_hidden(self):
        config = gui.GimmickConfig(kat_spacing1_ms=2, kat_spacing2_ms=6, kat_spacing3_ms=9)
        # Opened on a layer that does not show these rows (fake_slider): the
        # values must still come back unchanged, the same pattern as
        # `fake_slider_bpm_multiplier`'s round trip in
        # FakeSliderBpmMultiplierConfigTests above.
        dialog = gui.GimmickConfigDialog(config, "fake_slider", gui.GimmickConfig(), self.window)
        result = dialog.config()
        self.assertEqual(
            (result.kat_spacing1_ms, result.kat_spacing2_ms, result.kat_spacing3_ms),
            (2, 6, 9),
        )
        dialog.deleteLater()

    def test_the_barline_dialog_shows_the_defaults_and_edits_feed_back(self):
        dialog = gui.GimmickConfigDialog(
            gui.GimmickConfig(), "barline", gui.GimmickConfig(), self.window,
        )
        self.assertEqual(dialog.kat_spacing1_spin.value(), 1)
        self.assertEqual(dialog.kat_spacing2_spin.value(), 3)
        self.assertEqual(dialog.kat_spacing3_spin.value(), 5)
        dialog.kat_spacing1_spin.setValue(4)
        dialog.kat_spacing2_spin.setValue(8)
        dialog.kat_spacing3_spin.setValue(12)
        result = dialog.config()
        self.assertEqual(
            (result.kat_spacing1_ms, result.kat_spacing2_ms, result.kat_spacing3_ms),
            (4, 8, 12),
        )
        dialog.deleteLater()

    def test_accepting_with_two_equal_kat_spacings_is_refused(self):
        """Nothing in the spin boxes' own min/max stops two of them landing on
        the same value the way it stops every other field here -- so unlike
        those, this one combination has to be caught by hand before the
        dialog is allowed to close."""
        dialog = gui.GimmickConfigDialog(
            gui.GimmickConfig(), "barline", gui.GimmickConfig(), self.window,
        )
        dialog.kat_spacing1_spin.setValue(3)
        dialog.kat_spacing2_spin.setValue(3)
        with patch.object(gui.QMessageBox, "warning", lambda *a, **k: None):
            dialog.accept()
        self.assertNotEqual(dialog.result(), int(gui.QDialog.Accepted))
        dialog.deleteLater()


class TimingLineDialogEffectsTests(unittest.TestCase):
    """Double click a line: its two effects bits are editable there now."""

    def test_the_boxes_start_from_the_point(self):
        point = gui.TimingPoint.uninherited_at(1000, 180.0, kiai=True)
        dialog = gui.TimingLineDialog(point)
        self.assertTrue(dialog.kiai_check.isChecked())
        self.assertFalse(dialog.omit_barline_check.isChecked())
        dialog.deleteLater()

    def test_toggling_one_bit_leaves_the_other_alone(self):
        point = gui.TimingPoint.uninherited_at(1000, 180.0, kiai=True)
        dialog = gui.TimingLineDialog(point)
        dialog.omit_barline_check.setChecked(True)
        self.assertEqual(dialog.changes(point)["effects"], (1, 9))
        dialog.deleteLater()

    def test_an_unknown_bit_is_not_dropped(self):
        point = gui.TimingPoint.uninherited_at(1000, 180.0)
        point.effects = 4  # nothing this dialog offers
        dialog = gui.TimingLineDialog(point)
        dialog.kiai_check.setChecked(True)
        self.assertEqual(dialog.effects(point), 5)
        dialog.deleteLater()

    def test_no_change_means_no_command(self):
        point = gui.TimingPoint.inherited_at(1000, 1.5, kiai=True)
        dialog = gui.TimingLineDialog(point)
        self.assertEqual(dialog.changes(point), {})
        dialog.deleteLater()

    def test_red_line_meter_is_editable(self):
        point = gui.TimingPoint.uninherited_at(1000, 180.0)
        point.meter = 4
        dialog = gui.TimingLineDialog(point)
        self.assertEqual(dialog.meter_spin.value(), 4)
        dialog.meter_spin.setValue(7)
        self.assertEqual(dialog.changes(point), {"meter": (4, 7)})
        dialog.deleteLater()

    def test_anti_barline_meter_survives_opening(self):
        # 999 is anti-barline's "never stamp another bar"; OK without touching
        # the box must not clamp it into an edit.
        point = gui.TimingPoint.uninherited_at(1000, 180.0)
        point.meter = 999
        dialog = gui.TimingLineDialog(point)
        self.assertEqual(dialog.changes(point), {})
        dialog.deleteLater()

    def test_green_line_has_no_meter(self):
        dialog = gui.TimingLineDialog(gui.TimingPoint.inherited_at(1000, 1.5))
        self.assertIsNone(dialog.meter_spin)
        dialog.deleteLater()


class OscillatingSVTests(_Session, unittest.TestCase):
    """Oscillation reads the growth function as an amplitude, not a position."""

    def _generate(self, mode: str) -> list[float]:
        target = self.window._gimmick_pairing.target
        notes = sorted(
            (n for n in self.document.hit_objects if n.is_circle), key=lambda n: n.time
        )[:7]
        self.window._generate_sv(target, notes[0].time, notes[-1].time, {
            "initial_rate": 1.0, "final_rate": 1.06, "placement": "notes",
            "snap_divisor": 4, "position_offset": 0, "omit_barline": False,
            "relative_to_final_bpm": False, "function": "linear", "oscillate": mode,
        })
        return [
            p.sv_multiplier for p in sorted(
                (p for p in self.document.timing_points
                 if p.inherited and notes[0].time <= p.time <= notes[-1].time),
                key=lambda p: p.time,
            )
        ]

    def test_per_pair_alternates_around_the_base(self):
        values = self._generate("pair")
        self.assertGreater(len(values), 2)
        self.assertAlmostEqual(values[0], 1.0, places=3)
        for index, value in enumerate(values[1:], start=1):
            if index % 2:
                self.assertGreater(value, 1.0)
            else:
                self.assertLess(value, 1.0)

    def test_a_sweep_is_still_monotonic(self):
        values = self._generate("")
        self.assertEqual(values, sorted(values))

    def test_the_dialog_offers_the_modes_and_reports_the_choice(self):
        dialog = gui.SVFunctionDialog(0.0, 1000.0)
        self.assertEqual(dialog.parameters()["oscillate"], "")
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("pair"))
        self.assertEqual(dialog.parameters()["oscillate"], "pair")
        dialog.deleteLater()

    def test_the_preview_plots_the_oscillation_it_would_write(self):
        preview = dialogs.SVFunctionPreview()
        preview.set_range(1.0, 0.06 + 1.0)
        preview.set_oscillate("pair")
        values = [round(value, 4) for value in preview.rates(7)]
        self.assertEqual(values, [1.0, 1.02, 0.98, 1.04, 0.96, 1.06, 0.94])
        preview.deleteLater()


class ShinyNoteTests(_Session, unittest.TestCase):
    """A stack of fake sliders on one millisecond, and the layer that shows it."""

    def _layer(self):
        return self.window._gimmick_views[1].chart_view

    def _sliders_at(self, time_ms):
        return [
            n for n in self.document.hit_objects
            if gui.MainWindow.is_fake_slider(n) and round(n.time) == time_ms
        ]

    def test_the_shiny_tool_writes_a_stack_at_the_shiny_offset(self):
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        self.assertEqual(
            len(self._sliders_at(10000 + config.shiny_offset_ms)), config.shiny_count
        )

    def test_a_plain_fake_slider_is_not_a_shiny(self):
        self.window._place_gimmick("fake_slider", "regular", 10000)
        self.assertEqual(self.window._shiny_times(self.document), set())

    def test_a_stacked_fake_slider_is_still_a_fake_slider(self):
        """The offset says which structure it is, not the stack size: a plain
        fake slider gets stacked too, to brighten it."""
        config = self.window._gimmick_config("fake_slider")
        pairing = self.window._gimmick_pairing
        commands = self.window._gimmick_commands(
            self.state, pairing, "fake_slider", "regular", 10000,
            iter(range(1000, 1100)), copies=4,
        )
        self.state.history.push(gui.CompositeCommand(commands, "test"), self.state)
        at = 10000 + config.fake_slider_offset_ms
        self.assertEqual(len(self._sliders_at(at)), 4)
        self.assertNotIn(at, self.window._shiny_times(self.document))

    def test_the_layer_marks_the_shiny_and_nothing_else(self):
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        self.window._place_gimmick("fake_slider", "regular", 20000)
        layer = self._layer()
        self.assertIn(10000 + config.shiny_offset_ms, layer.shiny_times)
        self.assertNotIn(20000 + config.fake_slider_offset_ms, layer.shiny_times)
        self.assertTrue(layer.split_rows, "layer 2 draws two rows")

    def test_a_band_selects_in_its_own_row_and_shift_takes_both(self):
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        self.window._place_gimmick("fake_slider", "regular", 10100)
        layer = self._layer()
        shiny_at = 10000 + config.shiny_offset_ms
        slider_at = 10100 + config.fake_slider_offset_ms
        middle = layer._baseline_y()

        def band(y, shift=False):
            layer.drag_anchor_time = 9990.0
            layer.drag_mouse_x = layer.x_for_time(10150.0)
            layer._drag_y = y
            # `shift_is_held` asks the platform for the live key state: Qt's
            # cached modifiers go stale and a tap used to stick. See gui.py.
            with patch.object(chart_views, "shift_is_held", return_value=shift):
                layer._update_drag_selection()
                span = layer._row_span(y)
            return {round(n.time) for n in layer.selected_notes()}, span

        self.assertEqual(band(middle - 10), ({slider_at}, (0.0, float(middle))))
        self.assertEqual(
            band(middle + 10), ({shiny_at}, (float(middle), float(layer.height()))))
        self.assertEqual(
            band(middle + 10, shift=True),
            ({shiny_at, slider_at}, (0.0, float(layer.height()))),
        )

    def test_the_two_rows_are_apart_and_the_grid_is_between_them(self):
        layer = self._layer()
        baseline = layer.height() / 2
        self.assertLess(layer._row_y(baseline, False), baseline)
        self.assertGreater(layer._row_y(baseline, True), baseline)

    def test_layer_five_leaves_shiny_notes_alone(self):
        """Its Generate walks fake sliders; a shiny's speed comes from layer 4."""
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        self.window._place_gimmick("fake_slider", "regular", 20000)
        times = self.window._sv_layer_object_times("sv_fake_slider", self.document)
        self.assertIn(20000 + config.fake_slider_offset_ms, times)
        self.assertNotIn(10000 + config.shiny_offset_ms, times)
        self.assertNotIn(10000, times)

    def test_layer_four_still_reaches_the_shiny(self):
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        times = self.window._sv_layer_object_times("sv_chart", self.document)
        self.assertIn(10000, times, "the shiny's gimmick line is the chart layer's")

    def test_a_fake_slider_and_a_shiny_do_not_collide_on_one_snap(self):
        """The two rows exist so they can be read a millisecond apart, so the
        one-structure-per-millisecond rule cannot apply to this layer.

        A plain fake slider's own line no longer carries the gimmick BPM --
        there is nothing to squash, so it just holds the chart's own BPM (see
        the table in `gimmick_session.fake_slider`'s docstring) -- so the two
        structures placed on one snap no longer share a single gimmick line
        the way they did under the old squash-based scheme. Each still gets
        its own, undisturbed by the other."""
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "regular", 10000)
        self.window._place_gimmick("fake_slider", "shiny", 10000)

        self.assertEqual(len(self._sliders_at(10000 + config.fake_slider_offset_ms)), 1)
        self.assertEqual(
            len(self._sliders_at(10000 + config.shiny_offset_ms)), config.shiny_count
        )
        # The shiny's own red line, on the snap itself...
        self.assertEqual(
            len([p for p in self.document.timing_points if p.time == 10000 and p.uninherited]),
            1,
        )
        # ...and the fake slider's own, on its own offset -- two lines, not one.
        self.assertEqual(
            len([
                p for p in self.document.timing_points
                if p.time == 10000 + config.fake_slider_offset_ms and p.uninherited
            ]),
            1,
        )

    def test_the_same_structure_twice_on_one_snap_is_refused(self):
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        self.assertEqual(
            len(self._sliders_at(10000 + config.shiny_offset_ms)), config.shiny_count
        )

    def test_deleting_one_slider_takes_the_whole_stack(self):
        """The stack is one object to the mapper -- leaving two thirds of a
        shiny behind is the wreckage of one, not a smaller one."""
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        at = 10000 + config.shiny_offset_ms
        victim = self._sliders_at(at)[0]

        self.window._delete_gimmick_objects(
            self.window._gimmick_pairing.target, [victim.uid]
        )
        self.assertEqual(self._sliders_at(at), [])
        self.assertEqual(
            [p for p in self.document.timing_points if p.time == 10000 and p.uninherited], []
        )

    def test_the_function_tool_fills_a_range_with_shiny_notes(self):
        target = self.window._gimmick_pairing.target

        class _Stub:
            def __init__(self, *a, **k):
                pass

            def exec(self):
                return 1

            def times(self):
                return [10000, 11000]

            def object_kind(self):
                return "shiny"

            def shiny_count(self):
                return 4

            def bpm_multiplier(self):
                return 0.5

            def deleteLater(self):
                pass

        with patch.object(gui, "FakeSliderFunctionDialog", _Stub):
            self.window._generate_fake_sliders(target, 10000, 11000)

        config = self.window._gimmick_config("fake_slider")
        self.assertEqual(config.shiny_count, 4, "the chosen stack size is kept")
        self.assertEqual(config.shiny_bpm_multiplier, 0.5)
        for at in (10000, 11000):
            self.assertEqual(len(self._sliders_at(at + config.shiny_offset_ms)), 4)
        self.assertIn(10000 + config.shiny_offset_ms, self.window._shiny_times(self.document))

    def test_the_whole_generated_run_is_one_undo_step(self):
        target = self.window._gimmick_pairing.target
        notes_before = list(self.document.hit_objects)

        class _Stub:
            def __init__(self, *a, **k):
                pass

            def exec(self):
                return 1

            def times(self):
                return [10000, 11000]

            def object_kind(self):
                return "regular"

            def shiny_count(self):
                return 3

            def bpm_multiplier(self):
                return 1.0

            def deleteLater(self):
                pass

        with patch.object(gui, "FakeSliderFunctionDialog", _Stub):
            self.window._generate_fake_sliders(target, 10000, 11000)
        self.state.history.undo(self.state)
        self.assertEqual(self.document.hit_objects, notes_before)

    def test_the_bpm_multiplier_pays_itself_back_in_sv(self):
        """100 BPM at 2.4x is the same picture as 200 BPM at 1.2x."""
        config = self.window._gimmick_config("fake_slider")
        base_bpm = gui.base_bpm_at(self.window._gimmick_pairing.base_timing, 10000)
        config.shiny_bpm_multiplier = 0.5
        self.window._place_gimmick("fake_slider", "shiny", 10000)

        # The red line sits on the snap itself, not on the stack -- see the
        # table in gimmick_session.fake_slider's docstring. It is now the whole
        # structure: the green line that paid the retiming back in SV was
        # computed from the chart's own speed, which a shiny no longer
        # inherits, so the retiming is not paid back at all.
        red = next(p for p in self.document.timing_points if p.time == 10000 and p.uninherited)
        self.assertAlmostEqual(red.bpm, base_bpm * 0.5)
        self.assertEqual(
            [p for p in self.document.timing_points if p.time == 10000 and p.inherited], [],
        )

    def test_shiny_red_line_off_writes_the_stack_and_no_line(self):
        config = self.window._gimmick_config("fake_slider")
        config.shiny_red_line = False
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        self.assertEqual(
            len(self._sliders_at(10000 + config.shiny_offset_ms)), config.shiny_count
        )
        self.assertEqual(self._red(10000), [])

    def test_shiny_red_line_off_loses_detection_with_no_note_to_fall_back_on(self):
        """The documented cost: a shiny placed on empty space has only its
        line to tell it apart from a plain fake slider (see `shiny_red_line`
        in `GimmickConfig`), so turning the line off there makes it read as a
        plain fake slider instead. A shiny decorating a note already in the
        chart keeps its identity regardless -- `_compute_shiny_times` checks
        for the note first."""
        config = self.window._gimmick_config("fake_slider")
        config.shiny_red_line = False
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        self.assertNotIn(10000 + config.shiny_offset_ms, self.window._shiny_times(self.document))


class HandAuthoredShinyTests(_Session, unittest.TestCase):
    """The owner's own file, which is what the classifier has to survive.

        256,192,65393,1,0,0:0:0:0:            <- a normal note
        256,192,65394,2,8,...,-0.0001  x3     <- shiny:        note + 1
        256,192,65395,2,12,...,-0.0001 x3     <- fake sliders: note + 2

    with one red+green pair governing each of the two positions, and no 60000
    BPM squash line anywhere. So the structure cannot be found by looking for a
    gimmick BPM -- it is the distance back to the *note* that says which is
    which.
    """

    def setUp(self) -> None:
        super().setUp()
        document = self.document
        document.hit_objects.append(gui.HitObject(
            x=256, y=192, time=65393, type=1, hit_sound=0, hit_sample="0:0:0:0:",
        ))
        for at, hit_sound in ((65394, 8), (65395, 12)):
            for _ in range(3):
                document.hit_objects.append(gui.HitObject(
                    x=256, y=192, time=at, type=2, hit_sound=hit_sound,
                    extras=("L|624:192", "643", "-0.0001"), hit_sample="",
                ))
        document.hit_objects.sort(key=lambda note: note.time)
        document.timing_points.extend([
            gui.TimingPoint(65393, 648.648648648649, 4, 1, 0, 100, 1, 9),
            gui.TimingPoint(65393, -43.4782608695652, 4, 1, 0, 100, 0, 1),
            gui.TimingPoint(65395, 487.80487804878, 4, 1, 0, 100, 1, 9),
            gui.TimingPoint(65395, -1.001001001001, 4, 1, 0, 100, 0, 1),
        ])
        document.timing_points.sort(key=lambda point: point.time)

    def test_the_stack_one_ms_out_is_the_shiny(self):
        self.assertIn(65394, self.window._shiny_times(self.document))

    def test_the_stack_two_ms_out_is_a_fake_slider(self):
        self.assertNotIn(65395, self.window._shiny_times(self.document))

    def test_layer_five_reaches_the_fake_sliders_and_not_the_shiny(self):
        times = self.window._sv_layer_object_times("sv_fake_slider", self.document)
        self.assertIn(65395, times)
        self.assertNotIn(65394, times)
        self.assertNotIn(65393, times)

    def test_the_chart_sv_layer_reaches_the_note_and_its_shiny_together(self):
        times = self.window._sv_layer_object_times("sv_chart", self.document)
        self.assertIn(65393, times, "which is where the shiny's own green line is")

    def test_kiai_has_no_say_in_what_the_stack_is(self):
        """Position alone decides. Kiai used to be a fourth condition, which
        made the same bytes mean two different objects depending on a flag
        written somewhere else -- maddening in a hand-edited file."""
        self.assertIn(65394, self.window._shiny_times(self.document))
        for point in self.document.timing_points:
            if point.time == 65393:
                point.set_kiai(False)
        self.assertIn(
            65394, self.window._shiny_times(self.document),
            "+1 is a shiny whether or not a chorus happens to cover it",
        )

    def test_the_layer_draws_them_on_opposite_rows(self):
        layer = self.window._gimmick_views[1].chart_view
        layer.refresh_notes(self.document)
        self.assertIn(65394, layer.shiny_times)
        self.assertNotIn(65395, layer.shiny_times)
        baseline = layer.height() / 2
        self.assertGreater(layer._row_y(baseline, True), layer._row_y(baseline, False))


class BarlineGenerateSvTests(_Session, unittest.TestCase):
    """Generating red lines writes a green line with each of them.

    Every uninherited point resets SV to 1.0x, so a run of red lines used to
    flatten the chart's scroll speed for its whole length. "Current speed" --
    the default -- restates what was already in force, so the run changes
    where the bars fall and nothing else.
    """

    def setUp(self) -> None:
        super().setUp()
        self.document.timing_points.append(gui.TimingPoint.inherited_at(9000, 1.7))
        self.document.timing_points.sort(key=lambda p: p.time)
        self.target = self.window._gimmick_pairing.target

    def _generate(self, sv):
        class _Stub:
            def __init__(self, *a, **k):
                pass

            def exec(self):
                return 1

            def times(self):
                return [10000, 10001, 10002]

            def bpms(self, times):
                return [180.0] * len(times)

            def sv_multiplier(self):
                return sv

            class meter_spin:
                @staticmethod
                def value():
                    return 4

            def deleteLater(self):
                pass

        with patch.object(gui, "BarlineFunctionDialog", _Stub):
            self.window._generate_barlines(self.target, 10000, 10002)
        return [p for p in self.document.timing_points if 10000 <= p.time <= 10002]

    def test_current_speed_keeps_the_chart_moving_at_the_speed_it_was(self):
        greens = [p for p in self._generate(None) if p.inherited]
        self.assertEqual(len(greens), 3)
        for point in greens:
            self.assertAlmostEqual(point.sv_multiplier, 1.7)

    def test_a_custom_multiplier_drives_the_whole_run(self):
        greens = [p for p in self._generate(0.25) if p.inherited]
        self.assertEqual(len(greens), 3)
        for point in greens:
            self.assertAlmostEqual(point.sv_multiplier, 0.25)

    def test_each_green_sorts_after_the_red_it_belongs_to(self):
        """osu! resolves a shared timestamp by file order, and the red line has
        just reset SV to 1.0x -- the other way round the reset wins."""
        written = self._generate(None)
        at_first = [p.uninherited for p in written if round(p.time) == 10000]
        self.assertEqual(at_first, [True, False])

    def test_the_generated_sv_belongs_to_the_barline_sv_layer(self):
        self._generate(None)
        owned = self.window._sv_layer_object_times("sv_barline", self.document)
        for at in (10000, 10001, 10002):
            self.assertIn(at, owned)

    def test_the_dialog_defaults_to_the_speed_already_in_force(self):
        dialog = gui.BarlineFunctionDialog(
            10000.0, 10002.0, base_timing=self.window._gimmick_pairing.base_timing,
            current_sv=1.7,
        )
        self.assertIsNone(dialog.sv_multiplier(), "current speed, not a number")
        self.assertAlmostEqual(dialog.sv_spin.value(), 1.7)
        dialog.sv_mode_combo.setCurrentIndex(dialog.sv_mode_combo.findData("custom"))
        self.assertAlmostEqual(dialog.sv_multiplier(), 1.7)
        dialog.deleteLater()


class BarlineBetweenNoteAndSliderTests(_Session, unittest.TestCase):
    """The owner's second real file, which the first rule got wrong.

        87124  note        + a 66666 BPM squash
        87125  red line    <- the don's barline, one forward
        87126  fake slider + its own SV

    The object at 87126 is two milliseconds after its *note*, so it is a fake
    slider -- but only one after the barline that happens to sit between them.
    Reading the nearest anchor of any kind called it a shiny; a note has to
    outrank a line, at both distances, before lines are considered at all.
    """

    def setUp(self) -> None:
        super().setUp()
        self.document.hit_objects.append(gui.HitObject(
            x=256, y=192, time=87124, type=1, hit_sound=0, hit_sample="0:0:0:0:",
        ))
        self.document.hit_objects.append(gui.HitObject(
            x=256, y=192, time=87126, type=2, hit_sound=12,
            extras=("L|624:192", "643", "-0.0001"), hit_sample="",
        ))
        self.document.hit_objects.sort(key=lambda note: note.time)
        self.document.timing_points.extend([
            gui.TimingPoint(87124, 0.90000900009, 666, 1, 0, 100, 1, 8),
            gui.TimingPoint(87125, 324.324324324324, 555, 1, 0, 100, 1, 0),
            gui.TimingPoint(87125, -61.5384615384615, 4, 1, 0, 100, 0, 0),
            gui.TimingPoint(87126, -15.3846153846154, 4, 1, 0, 100, 0, 0),
        ])
        self.document.timing_points.sort(key=lambda point: point.time)

    def test_the_slider_two_ms_after_its_note_is_not_a_shiny(self):
        self.assertNotIn(87126, self.window._shiny_times(self.document))

    def test_an_intervening_barline_does_not_make_it_one(self):
        """87125 carries a red line, so `at - 1` finds an anchor -- the note
        two milliseconds back has to win that argument."""
        reds = {round(p.time) for p in self.document.timing_points if p.uninherited}
        self.assertIn(87125, reds, "the barline this test is about")
        self.assertNotIn(87126, self.window._shiny_times(self.document))

    def test_it_belongs_to_the_fake_slider_sv_layer(self):
        self.assertIn(
            87126, self.window._sv_layer_object_times("sv_fake_slider", self.document)
        )

    def test_the_other_file_still_reads_the_way_it_did(self):
        """The +1 stack off a note is still a shiny -- this fix must not undo
        the case it was built for."""
        self.document.hit_objects.append(gui.HitObject(
            x=256, y=192, time=65393, type=1, hit_sound=0, hit_sample="0:0:0:0:",
        ))
        for _ in range(3):
            self.document.hit_objects.append(gui.HitObject(
                x=256, y=192, time=65394, type=2, hit_sound=8,
                extras=("L|624:192", "643", "-0.0001"), hit_sample="",
            ))
        self.document.hit_objects.sort(key=lambda note: note.time)
        shiny = self.window._shiny_times(self.document)
        self.assertIn(65394, shiny)
        self.assertNotIn(87126, shiny)


class KiaiRangeTests(_Session, unittest.TestCase):
    """The Kiai tool: one drag, one kiai section.

    It lives in the Kiai and Sound Volume layer now -- a kiai section is read
    against every layer at once, so the fake slider layer was never its home.
    """

    def _kiai_at(self, time_ms):
        return gui.in_kiai(gui.sorted_by_time(self.document.timing_points), time_ms)

    def _lines_at(self, *times):
        """Put a green line on each of `times`, at the SV already in force.

        The Kiai tool flags the lines already inside the range and writes
        none of its own -- the layer it lives in must never touch scroll
        speed, and inventing an inherited point to carry the flag is a
        scroll-speed edit. So a section's edges have to be real lines before
        the drag, which is what these tests set up here rather than relying on
        the tool to conjure them.
        """
        target = self.window._gimmick_pairing.target
        for time_ms in times:
            sv = gui.sv_at(gui.sorted_by_time(self.document.timing_points), time_ms)
            self.window._add_sv_point(target, time_ms, sv)

    def test_every_point_in_the_range_carries_kiai(self):
        # Kiai first: a shiny is refused outside one (see KiaiRefusalTests),
        # so placing it after the section exists is what a mapper actually
        # does, and lets this test its real subject -- that every line a
        # placement writes inside an existing section still carries kiai.
        self._lines_at(9000, 12000)
        self.window._set_kiai_range(self.window._gimmick_pairing.target, 9000, 12000)
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        self.window._place_gimmick("barline", "kat", 11000)

        inside = [
            p for p in self.document.timing_points
            if 9000 <= round(p.time) < 12000
        ]
        self.assertTrue(inside)
        self.assertTrue(all(p.kiai for p in inside), "one line without it ends the section")

    def test_the_section_starts_and_ends_where_the_drag_did(self):
        target = self.window._gimmick_pairing.target
        self._lines_at(9000, 12000)
        self.window._set_kiai_range(target, 9000, 12000)
        self.assertFalse(self._kiai_at(8999))
        self.assertTrue(self._kiai_at(9000))
        self.assertTrue(self._kiai_at(11999))
        self.assertFalse(self._kiai_at(12000))

    def test_the_edges_change_nothing_but_the_flag(self):
        target = self.window._gimmick_pairing.target
        self._lines_at(9000, 12000)
        before = gui.sv_at(gui.sorted_by_time(self.document.timing_points), 9000)
        self.window._set_kiai_range(target, 9000, 12000)
        self.assertAlmostEqual(
            gui.sv_at(gui.sorted_by_time(self.document.timing_points), 9000), before
        )

    def test_a_backwards_or_empty_drag_does_nothing(self):
        target = self.window._gimmick_pairing.target
        before = list(self.document.timing_points)
        self.window._set_kiai_range(target, 12000, 12000)
        self.assertEqual(self.document.timing_points, before)

    def test_the_whole_range_is_one_undo_step(self):
        target = self.window._gimmick_pairing.target
        self._lines_at(9000, 12000)
        self.window._place_gimmick("barline", "kat", 11000)
        before = [(p.uid, p.effects) for p in self.document.timing_points]
        self.window._set_kiai_range(target, 9000, 12000)
        self.state.history.undo(self.state)
        self.assertEqual(
            [(p.uid, p.effects) for p in self.document.timing_points], before
        )

    def test_the_tool_is_in_the_kiai_and_sound_effect_toolbox(self):
        self.assertIn("kiai", dict(gui.MainWindow.GIMMICK_TOOLSETS["kiai_sound"]))
        self.assertNotIn("kiai", dict(gui.MainWindow.GIMMICK_TOOLSETS["fake_slider"]))

    def test_every_layer_draws_the_section(self):
        """The band is background information, so it belongs in every layer
        rather than only in the one whose tool wrote it."""
        target = self.window._gimmick_pairing.target
        self._lines_at(9000, 12000)
        self.window._set_kiai_range(target, 9000, 12000)
        for frame in self.window._gimmick_views:
            view = getattr(frame, "chart_view", None) or frame.sv_view
            self.assertIn(
                (9000, 12000), view.kiai_bands, f"{frame.gimmick_layer} has no band"
            )

    def test_the_band_is_clipped_to_the_window(self):
        view = self.window._gimmick_views[0].chart_view
        view.kiai_bands = [(9000, 12000)]
        view.current_time = 10000.0
        view.window_ms = 200.0
        image = view.grab().toImage()
        self.assertGreater(image.width(), 0, "the layer painted without raising")


class SpinButtonTests(unittest.TestCase):
    def test_spin_boxes_keep_their_own_arrows_and_the_art_exists(self):
        """The step arrows live inside the box now (config_sheet's stylesheet),
        so a spin box keeps Qt's own buttons -- and every image the rule names
        has to be on disk, or Qt silently draws nothing there."""
        import re
        from ui.config_sheet import control_stylesheet
        dialog = gui.TimingLineDialog(gui.TimingPoint.uninherited_at(0, 180.0))
        self.assertNotEqual(dialog.time_spin.buttonSymbols(), gui.QAbstractSpinBox.NoButtons)
        before = dialog.time_spin.value()
        dialog.time_spin.stepUp()
        self.assertEqual(dialog.time_spin.value(), before + 1)
        dialog.deleteLater()
        urls = re.findall(r"url\(([^)]+)\)", control_stylesheet())
        self.assertTrue(urls)
        for url in urls:
            self.assertTrue(Path(url).is_file(), url)


class BpmLabelTests(_Session, unittest.TestCase):
    def test_layer_three_writes_the_bpm_and_layer_six_no_longer_does(self):
        layers = {
            frame.gimmick_layer: getattr(frame, "chart_view", None) or frame.sv_view
            for frame in self.window._gimmick_views
        }
        self.assertTrue(layers["barline"].show_bpm_labels)
        self.assertFalse(layers["sv_barline"].show_bpm_labels)


class LayerRemovalTests(_Session, unittest.TestCase):
    def test_closing_a_layer_removes_it_from_the_stack(self):
        frame = self.window._gimmick_views[1]
        view = frame.chart_view
        frame.close_button.click()

        self.assertNotIn(frame, self.window._gimmick_views)
        self.assertEqual(
            len(self.window._gimmick_views), len(gui.MainWindow.GIMMICK_LAYERS) - 1
        )
        # ...and it stops being fed the playhead, which would call set_time on
        # a deleted C++ object.
        self.assertNotIn(view, self.window._chart_views)

    def test_re_entering_the_page_brings_every_layer_back(self):
        self.window._gimmick_views[1].close_button.click()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.assertEqual(
            len(self.window._gimmick_views), len(gui.MainWindow.GIMMICK_LAYERS)
        )

    def test_a_layer_is_still_focused_afterwards(self):
        self.window._gimmick_views[0].close_button.click()
        self.assertIsNotNone(self.window._active_chart_view)


class SVLayerOwnershipTests(_Session, unittest.TestCase):
    """Task 2's rewritten `_sv_layer_object_times`: one owner per millisecond."""

    def setUp(self) -> None:
        super().setUp()
        self.target = self.window._gimmick_pairing.target
        self.window._set_kiai_range(self.target, 0, 30000)

    def test_layer_four_no_longer_sees_a_plain_fake_sliders_line(self):
        """The bug this round fixed: a plain fake slider's own line used to
        leak into the normal-chart SV layer even though no note is there."""
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "regular", 10000)
        times = self.window._sv_layer_object_times("sv_chart", self.document)
        self.assertNotIn(10000 + config.fake_slider_offset_ms, times)

    def test_layer_five_sees_the_fake_slider_and_not_the_shiny(self):
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "regular", 10000)
        self.window._place_gimmick("fake_slider", "shiny", 20000)
        times = self.window._sv_layer_object_times("sv_fake_slider", self.document)
        self.assertIn(10000 + config.fake_slider_offset_ms, times)
        self.assertNotIn(20000 + config.shiny_offset_ms, times)

    def test_layer_five_sees_a_shiny_millisecond_once_it_carries_a_green_line(self):
        """The exception: a shiny's own millisecond stays invisible to layer 5
        normally (its speed is layer 4's business), but once something --
        hand-written or generated -- puts a green line exactly there, it has
        to be editable rather than lost.

        Shown via `_sv_layer_times` (what the view displays), not
        `_sv_layer_object_times` (what Generate sweeps and a paste maps its
        clipboard onto by index) -- see `test_the_millisecond_is_not_a_generate_or_paste_object`
        for why the two must disagree here."""
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        at = 10000 + config.shiny_offset_ms
        self.document.timing_points.append(gui.TimingPoint.inherited_at(at, 1.2))
        self.document.timing_points.sort(key=lambda p: p.time)

        times = self.window._sv_layer_times("sv_fake_slider", self.document)
        self.assertIn(at, times)

    def test_the_millisecond_is_not_a_generate_or_paste_object(self):
        """A shiny sitting between plain fake sliders, once it carries its own
        green line, used to insert an extra slot into layer 5's object set --
        `_sv_layer_object_times` folded `_shiny_green_line_times` in directly.
        That extra slot ate a clipboard entry on an index-mapped paste and
        shifted every fake slider after the shiny by one, and it also gave
        Generate an uninvited target the layer's own docstring says it never
        reaches without `include_shiny`."""
        config = self.window._gimmick_config("fake_slider")
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        at = 10000 + config.shiny_offset_ms
        self.document.timing_points.append(gui.TimingPoint.inherited_at(at, 1.2))
        self.document.timing_points.sort(key=lambda p: p.time)

        objects = self.window._sv_layer_object_times("sv_fake_slider", self.document)
        self.assertNotIn(at, objects)

    def test_an_orphan_green_line_lands_in_the_barline_layer(self):
        """A green line on nothing at all -- no note, no fake slider, no red
        line -- used to be owned by no SV layer and so showed in none of them,
        while the Kiai and Sound Volume layer listed it happily."""
        at = 12345
        self.document.timing_points.append(gui.TimingPoint.inherited_at(at, 1.7))
        self.document.timing_points.sort(key=lambda p: p.time)

        self.assertIn(at, self.window._sv_layer_times("sv_barline", self.document))
        self.assertNotIn(at, self.window._sv_layer_times("sv_chart", self.document))
        self.assertNotIn(
            at, self.window._sv_layer_times("sv_fake_slider", self.document))
        # A line, not an object: a Generate sweep across the layer still
        # targets its red lines and not this.
        self.assertNotIn(
            at, self.window._sv_layer_object_times("sv_barline", self.document))

    def test_a_green_line_that_already_has_an_owner_is_not_also_the_barlines(self):
        """Only the unowned ones move: a line on a note stays layer 4's, or
        the same millisecond would be drawn in two layers at once."""
        note = self.document.hit_objects[0]
        at = round(note.time)
        self.document.timing_points.append(gui.TimingPoint.inherited_at(at, 1.7))
        self.document.timing_points.sort(key=lambda p: p.time)

        self.assertIn(at, self.window._sv_layer_times("sv_chart", self.document))
        self.assertNotIn(at, self.window._sv_layer_times("sv_barline", self.document))

    def test_a_shiny_placement_writes_no_inherited_point_at_the_snap(self):
        """Task 3 was that the restore-point append must not stack a second
        green line where the builder already wrote one. Neither is written now
        -- the builder's carried the chart's SV and the append handed it back
        -- so the count this guards has gone from one to zero rather than the
        two it was protecting against."""
        self.window._place_gimmick("fake_slider", "shiny", 10000)
        greens = [p for p in self.document.timing_points if p.time == 10000 and p.inherited]
        self.assertEqual(len(greens), 0)


class GimmickLayerWiringTests(_Session, unittest.TestCase):
    """Task 5: the six layers share the editor's zoom and its scrub bar."""

    def test_every_layer_opens_at_the_default_editor_window(self):
        for frame in self.window._gimmick_views:
            view = getattr(frame, "chart_view", None) or frame.sv_view
            self.assertEqual(view.window_ms, self.window.DEFAULT_EDITOR_WINDOW_MS)

    def test_every_layer_shares_the_gimmick_pages_timing_bar(self):
        for frame in self.window._gimmick_views:
            view = getattr(frame, "chart_view", None) or frame.sv_view
            self.assertIs(view.timing_bar, self.window.gimmick_timing_bar)


class MultiFakeSliderToolTests(_Session, unittest.TestCase):
    """Task 1: one click, N independent plain fake sliders."""

    def setUp(self) -> None:
        super().setUp()
        self.target = self.window._gimmick_pairing.target
        # Kiai across the whole area these tests place in, to prove the tool
        # cannot accidentally produce a shiny even where one would be legal.
        self.window._set_kiai_range(self.target, 0, 30000)
        self.config = self.window._gimmick_config("fake_slider")

    def _sliders_at(self, time_ms):
        return [
            n for n in self.document.hit_objects
            if gui.MainWindow.is_fake_slider(n) and round(n.time) == time_ms
        ]

    def test_one_structure_per_position_with_the_right_spacing(self):
        self.window._multi_fake_slider_params = (0, 16, 2)
        before = {n.uid for n in self.document.hit_objects}

        self.window._place_gimmick("fake_slider", "multi", 10000)

        positions = list(range(10000, 10016 + 1, 2))
        for position in positions:
            self.assertEqual(
                len(self._sliders_at(position + self.config.fake_slider_offset_ms)), 1,
                f"no slider at {position}",
            )
        added = [n for n in self.document.hit_objects if n.uid not in before]
        self.assertEqual(len(added), len(positions))

    def _place_run_with_red_lines(self, enabled: bool) -> list:
        """One run of the Multiple tool at `fake_slider_red_line=enabled`."""
        self.window.gimmick_configs["fake_slider"] = gui.GimmickConfig(
            fake_slider_red_line=enabled,
        )
        self.window._multi_fake_slider_params = (0, 16, 2)
        before = {p.uid for p in self.document.timing_points}
        self.window._place_gimmick("fake_slider", "multi", 10000)
        return [p for p in self.document.timing_points if p.uid not in before]

    def test_the_run_writes_a_line_per_slider_only_when_the_dial_is_on(self):
        """`fake_slider_red_line` is what the run's dead lines turn on and off.

        A plain fake slider has no note to hide, so its red line only ever
        applied `fake_slider_bpm_multiplier` -- and BPM persists until the next
        uninherited point, so every line after the first re-stated a speed
        already in force. On, that is nine lines for nine sliders; off, the run
        is its sliders and nothing else, having given up the retiming.
        """
        added = self._place_run_with_red_lines(True)
        reds = [p for p in added if p.uninherited]
        self.assertEqual(len(reds), 9, "0..16 step 2, one line each")
        self.assertEqual(
            len(self._sliders_at(10000 + self.config.fake_slider_offset_ms)), 1,
        )

    def test_the_run_writes_no_timing_points_with_the_dial_off(self):
        added = self._place_run_with_red_lines(False)
        self.assertEqual(added, [], "the run writes sliders only")
        # The sliders themselves are still there -- this is not a no-op tool.
        self.assertEqual(
            len(self._sliders_at(10000 + self.config.fake_slider_offset_ms)), 1,
        )

    def test_the_dial_reaches_a_single_plain_fake_slider_too(self):
        """It belongs to the structure, not to the Multiple tool -- one click
        of the plain Fake Slider tool obeys the same flag."""
        for enabled, expected in ((True, 1), (False, 0)):
            with self.subTest(fake_slider_red_line=enabled):
                self.window.gimmick_configs["fake_slider"] = gui.GimmickConfig(
                    fake_slider_red_line=enabled,
                )
                at = 10000 if enabled else 14000
                before = {p.uid for p in self.document.timing_points}
                self.window._place_gimmick("fake_slider", "regular", at)
                added = [p for p in self.document.timing_points if p.uid not in before]
                self.assertEqual(len([p for p in added if p.uninherited]), expected)
                self.assertEqual(
                    len(self._sliders_at(at + self.config.fake_slider_offset_ms)), 1,
                    "the object is written either way",
                )

    def test_the_dial_defaults_on(self):
        """What every structure written before the dial existed did."""
        self.assertTrue(gui.GimmickConfig().fake_slider_red_line)

    def test_a_fake_slider_don_keeps_its_red_lines(self):
        """The named exception. Its squash line hides the note and its restore
        brings the chart's BPM back -- drop those and there is no gimmick."""
        before = {p.uid for p in self.document.timing_points}
        self.window._place_gimmick("fake_slider", "don", 10000)
        added = [p for p in self.document.timing_points if p.uid not in before]
        reds = [p for p in added if p.uninherited]
        self.assertEqual(len(reds), 2, "the squash and the restore")
        self.assertEqual(
            sorted(round(p.time) for p in reds),
            [10000, 10000 + self.config.fake_slider_offset_ms],
        )

    def test_none_of_the_structures_classify_as_shiny_even_inside_kiai(self):
        self.window._multi_fake_slider_params = (0, 16, 2)
        self.window._place_gimmick("fake_slider", "multi", 10000)
        self.assertEqual(self.window._shiny_times(self.document), set())

    def test_a_negative_start_offset_places_structures_before_the_click(self):
        self.window._multi_fake_slider_params = (-8, 0, 2)

        self.window._place_gimmick("fake_slider", "multi", 10000)

        for position in range(9992, 10001, 2):
            self.assertEqual(
                len(self._sliders_at(position + self.config.fake_slider_offset_ms)), 1,
                f"no slider at {position}",
            )
        # And nothing was written past the click -- Start and End were both
        # negative, so the run should not have overshot into positive space.
        self.assertEqual(self._sliders_at(10000 + self.config.fake_slider_offset_ms + 2), [])

    def test_the_whole_run_is_one_undo_step(self):
        self.window._multi_fake_slider_params = (0, 16, 2)
        notes_before = list(self.document.hit_objects)
        points_before = list(self.document.timing_points)

        self.window._place_gimmick("fake_slider", "multi", 10000)
        self.state.history.undo(self.state)

        self.assertEqual(self.document.hit_objects, notes_before)
        self.assertEqual(self.document.timing_points, points_before)

    def test_every_placed_note_gets_its_own_original_index(self):
        self.window._multi_fake_slider_params = (0, 16, 2)
        self.window._place_gimmick("fake_slider", "multi", 10000)
        indices = [n.original_index for n in self.document.hit_objects]
        self.assertEqual(len(indices), len(set(indices)))

    def test_the_tool_sits_between_shiny_and_function(self):
        tools = [tool_id for tool_id, _label in gui.MainWindow.GIMMICK_TOOLSETS["fake_slider"]]
        self.assertEqual(tools[tools.index("shiny") + 1], "multi")
        self.assertEqual(tools[tools.index("multi") + 1], "function")


class MultiFakeSliderDialogWiringTests(_Session, unittest.TestCase):
    """Selecting the tool opens its config every time; cancelling it falls
    back to Select rather than leaving the tool armed with nothing chosen."""

    def test_cancelling_the_config_falls_back_to_select(self):
        class _Stub:
            def __init__(self, *a, **k):
                pass

            def exec(self):
                return int(gui.QDialog.Rejected)

        with patch.object(gui, "MultiFakeSliderDialog", _Stub):
            self.window.gimmick_tool_buttons["fake_slider"]["multi"].setChecked(True)

        self.assertTrue(self.window.gimmick_tool_buttons["fake_slider"]["select"].isChecked())
        self.assertFalse(self.window.gimmick_tool_buttons["fake_slider"]["multi"].isChecked())

    def test_accepting_arms_the_tool_with_the_chosen_shape(self):
        class _Stub:
            def __init__(self, *a, **k):
                pass

            def exec(self):
                return int(gui.QDialog.Accepted)

            def parameters(self):
                return (5, 25, 4)

        with patch.object(gui, "MultiFakeSliderDialog", _Stub):
            self.window.gimmick_tool_buttons["fake_slider"]["multi"].setChecked(True)

        self.assertEqual(self.window._multi_fake_slider_params, (5, 25, 4))
        self.assertTrue(self.window.gimmick_tool_buttons["fake_slider"]["multi"].isChecked())


class KiaiSoundPasteTests(_Session, unittest.TestCase):
    """The one SV layer that owns every millisecond, and so pastes by delta."""

    def _view(self):
        for frame in self.window._gimmick_views:
            if getattr(frame, "gimmick_layer", None) == "kiai_sound":
                return frame.sv_view
        self.fail("no kiai_sound layer")

    def test_paste_lands_instead_of_toasting_about_owned_milliseconds(self):
        """`_sv_layer_times` returns None here to mean "owns them all", which is
        the *delta* case and not the empty one.

        Collapsed with `or ()` it came out as owning none, so `targets` was
        always empty: every paste on this layer toasted "no milliseconds after
        the cursor" and wrote nothing, on a layer where every millisecond is
        available. The other three layers own a scattered set and are unchanged
        -- see `SVPasteByIndexTests` for the index-mapped path.
        """
        self.assertIsNone(
            self.window._sv_layer_times("kiai_sound", self.document),
            "this layer owns every millisecond",
        )
        target = self.window._gimmick_pairing.target
        self.document.timing_points.extend((
            gui.TimingPoint.inherited_at(8000, 1.4),
            gui.TimingPoint.inherited_at(8500, 1.9),
        ))
        self.document.timing_points.sort(key=lambda p: p.time)
        self.window._refresh_difficulty_sv_views(target)

        view = self._view()
        self.window._active_sv_view = view
        sources = [
            p for p in view.timing_points
            if p.inherited and round(p.time) in (8000, 8500)
        ]
        self.assertEqual(len(sources), 2)
        view.selected_uids = {p.uid for p in sources}
        self.window._copy_sv_points()
        self.assertEqual(len(self.window._sv_clipboard), 2)

        before = {p.uid for p in self.document.timing_points}
        view.current_time = 20000.0
        self.window._paste_sv_points()

        added = [p for p in self.document.timing_points if p.uid not in before]
        self.assertEqual(len(added), 2, "both clipboard entries landed")
        # Delta-mapped: the 500ms gap between the two is preserved.
        times = sorted(round(p.time) for p in added)
        self.assertEqual(times[1] - times[0], 500)


class SVPasteByIndexTests(_Session, unittest.TestCase):
    """Task 2: a gimmick SV layer's paste maps onto its own object index."""

    def setUp(self) -> None:
        super().setUp()
        self.target = self.window._gimmick_pairing.target
        # The fixture carries its own fake slider (FULL_V14's fixed
        # HitObjects, at 54692) -- layer 5's too, and left in place it is a
        # fifth "remaining" millisecond every one of these tests would have
        # to account for. Stripped so `self.owned` below is exactly the four
        # this class places.
        self.document.hit_objects = [
            n for n in self.document.hit_objects if not gui.MainWindow.is_fake_slider(n)
        ]
        self.window._set_kiai_range(self.target, 0, 30000)
        for t in (10000, 10100, 10200, 10300):
            self.window._place_gimmick("fake_slider", "regular", t)
        self.view = self.window._gimmick_views[4].sv_view
        self.window._active_sv_view = self.view
        self.owned = sorted(self.window._sv_layer_times("sv_fake_slider", self.document))
        self.assertEqual(len(self.owned), 4)
        # Placement no longer leaves a green line behind: the fake slider layer
        # carries no SV from the chart, so `_gimmick_commands` skips the
        # restore point and the builder writes none either. The layer still
        # *owns* these four milliseconds (asserted above) -- it just arrives
        # with nothing on them, and a line has to be put there by hand before
        # there is anything to paste onto. That is what layer 5's own green
        # line tool leaves, and what this class is really exercising, so the
        # lines are placed explicitly rather than relied on as a side effect.
        self.document.timing_points.extend(
            gui.TimingPoint.inherited_at(at, 1.0) for at in self.owned
        )
        # Sorted and pushed into the views: appending behind their backs leaves
        # each view's snapshot without the new uids, and a selection made of
        # uids the view does not hold copies nothing at all.
        self.document.timing_points.sort(key=lambda p: p.time)
        self.window._refresh_difficulty_sv_views(self.target)

    def _point_at(self, time_ms):
        return next(
            p for p in self.document.timing_points
            if p.inherited and round(p.time) == time_ms
        )

    def test_paste_lands_on_owned_milliseconds_not_a_beat_grid_one(self):
        self.window._edit_sv_point(self.target, self._point_at(self.owned[0]).uid, 2.5)
        self.window._edit_sv_point(self.target, self._point_at(self.owned[1]).uid, 3.5)
        self.view.selected_uids = {
            self._point_at(self.owned[0]).uid, self._point_at(self.owned[1]).uid,
        }
        self.window._copy_sv_points()
        self.assertEqual(len(self.window._sv_clipboard), 2)

        # A cursor between the second and third owned milliseconds, so the
        # third and fourth are the two that remain: the old millisecond-delta
        # path would snap this to the beat grid and offset from there,
        # landing nowhere near either target.
        cursor = (self.owned[1] + self.owned[2]) / 2
        self.view.current_time = cursor
        self.window._paste_sv_points()

        self.assertAlmostEqual(self._point_at(self.owned[2]).sv_multiplier, 2.5)
        self.assertAlmostEqual(self._point_at(self.owned[3]).sv_multiplier, 3.5)
        grid_base = round(max(0.0, gui.snap_time(
            gui.extract_timing_points(self.document), cursor, self.view.snap_divisor,
        )))
        self.assertNotIn(grid_base, (self.owned[2], self.owned[3]))

    def test_a_short_clipboard_pastes_what_fits_against_few_remaining(self):
        # Distinct values, or a match against any restore point would pass.
        for index, t in enumerate(self.owned):
            self.window._edit_sv_point(self.target, self._point_at(t).uid, 1.1 + index / 10)
        self.view.selected_uids = {self._point_at(t).uid for t in self.owned}
        self.window._copy_sv_points()
        self.assertEqual(len(self.window._sv_clipboard), 4)

        # Only the last two owned milliseconds remain past this cursor.
        self.view.current_time = self.owned[2] - 1
        self.window._paste_sv_points()

        self.assertIn("2", self.window.status.text())
        self.assertIn("4", self.window.status.text())
        # The first two clipboard entries (owned[0]'s and owned[1]'s values,
        # in copy order) landed on the two positions that did remain.
        self.assertAlmostEqual(self._point_at(self.owned[2]).sv_multiplier, 1.1)
        self.assertAlmostEqual(self._point_at(self.owned[3]).sv_multiplier, 1.2)

    def test_paste_lands_on_the_layer_offset_millisecond_not_the_objects_own(self):
        """With a non-zero `sv_offset_ms` the layer owns two milliseconds per
        object -- the object's own (a map arrives with SV on it) and the shifted
        one Generate writes to -- so a paste had twice the targets it has
        objects and filled both, half of them where the layer never generates.
        Layer 4's default offset is -5ms, so this was the default case."""
        self.window.gimmick_configs["sv_fake_slider"] = dataclasses.replace(
            self.window._gimmick_config("sv_fake_slider"), sv_offset_ms=5,
        )
        for index, at in enumerate(self.owned):
            self.window._edit_sv_point(self.target, self._point_at(at).uid, 1.1 + index / 10)
        self.view.selected_uids = {
            self._point_at(self.owned[0]).uid, self._point_at(self.owned[1]).uid,
        }
        self.window._copy_sv_points()

        before = {p.uid for p in self.document.timing_points}
        self.view.current_time = float(self.owned[2]) - 1
        self.window._paste_sv_points()

        landed = sorted(
            round(p.time) for p in self.document.timing_points if p.uid not in before
        )
        self.assertEqual(landed, [self.owned[2] + 5, self.owned[3] + 5])

    def test_no_owned_milliseconds_after_the_cursor_toasts_and_does_nothing(self):
        self.view.selected_uids = {self._point_at(self.owned[0]).uid}
        self.window._copy_sv_points()
        points_before = list(self.document.timing_points)

        self.view.current_time = self.owned[-1] + 1
        self.window._paste_sv_points()

        self.assertEqual(self.document.timing_points, points_before)
        self.assertTrue(self.window._toast.isVisible())

    def test_a_shiny_with_its_own_green_line_is_not_a_paste_slot(self):
        """The bug report: copying SV off a run of plain fake sliders into a
        destination range that has a shiny sitting between two of them, where
        that shiny already carries a green line, used to eat a clipboard slot
        on the shiny and shift every value after it by one -- the fourth
        fake slider's copied value landed on the shiny instead, and the
        fourth fake slider itself got nothing."""
        for index, t in enumerate(self.owned):
            self.window._edit_sv_point(self.target, self._point_at(t).uid, 1.1 + index / 10)
        self.view.selected_uids = {self._point_at(t).uid for t in self.owned}
        self.window._copy_sv_points()
        self.assertEqual(len(self.window._sv_clipboard), 4)

        # A shiny between the third and fourth owned milliseconds, carrying
        # its own green line -- the exact scenario that used to insert an
        # extra slot into the index-mapped walk.
        self.window._place_gimmick("fake_slider", "shiny", self.owned[2] + 20)
        config = self.window._gimmick_config("fake_slider")
        shiny_at = self.owned[2] + 20 + config.shiny_offset_ms
        self.document.timing_points.append(gui.TimingPoint.inherited_at(shiny_at, 1.0))
        self.document.timing_points.sort(key=lambda p: p.time)
        self.window._refresh_difficulty_sv_views(self.target)

        before = {p.uid for p in self.document.timing_points}
        self.view.current_time = float(self.owned[0]) - 1
        self.window._paste_sv_points()

        added = [p for p in self.document.timing_points if p.uid not in before]
        landed = sorted(round(p.time) for p in added)
        self.assertEqual(landed, list(self.owned), "landed on the four fake sliders, not the shiny")
        for index, at in enumerate(self.owned):
            self.assertAlmostEqual(self._point_at(at).sv_multiplier, 1.1 + index / 10)

    def test_the_editor_pages_own_sv_view_keeps_the_millisecond_delta_path(self):
        """No `gimmick_layer` attribute at all -- it owns every millisecond,
        so the old offset-from-a-snapped-base behaviour is still correct
        there and must not be disturbed."""
        self.window._add_editor_view("sv", self.target)
        sv_view = [f for f in self.window._editor_views if f.view_type == "sv"][-1].sv_view
        self.assertFalse(hasattr(sv_view, "gimmick_layer"))

        source_point = self._point_at(self.owned[0])
        self.window._gimmick_views[4].sv_view.selected_uids = {source_point.uid}
        self.window._active_sv_view = self.window._gimmick_views[4].sv_view
        self.window._copy_sv_points()

        self.window._active_sv_view = sv_view
        sv_view.current_time = 5000.0
        self.window._paste_sv_points()

        expected_base = gui.osu_snap_ms(max(0.0, gui.snap_time(
            gui.extract_timing_points(self.document), 5000.0, sv_view.snap_divisor,
        )))
        pasted = self._point_at(expected_base)
        self.assertAlmostEqual(pasted.beat_length, source_point.beat_length)


class IncludeShinyGenerationTests(_Session, unittest.TestCase):
    """Task 3: a per-sweep opt-in that does not change layer 5's ownership."""

    def setUp(self) -> None:
        super().setUp()
        self.target = self.window._gimmick_pairing.target
        self.window._set_kiai_range(self.target, 0, 30000)
        self.window._place_gimmick("fake_slider", "regular", 10000)
        self.window._place_gimmick("fake_slider", "shiny", 20000)
        self.config = self.window._gimmick_config("fake_slider")
        self.shiny_at = 20000 + self.config.shiny_offset_ms

    def test_off_by_default_excludes_the_shiny_millisecond(self):
        times = self.window._sv_generation_times(
            self.state, 0, 30000, {"placement": "notes"}, layer_id="sv_fake_slider",
        )
        self.assertNotIn(self.shiny_at, times)

    def test_on_adds_the_shiny_millisecond_for_this_sweep_only(self):
        times = self.window._sv_generation_times(
            self.state, 0, 30000, {"placement": "notes", "include_shiny": True},
            layer_id="sv_fake_slider",
        )
        self.assertIn(self.shiny_at, times)
        # Ownership itself is untouched -- only this one sweep's targets grew.
        self.assertNotIn(
            self.shiny_at, self.window._sv_layer_object_times("sv_fake_slider", self.document)
        )

    def test_the_checkbox_only_appears_on_the_fake_slider_sv_layer(self):
        for layer_id, expected in (("sv_fake_slider", True), ("sv_chart", False), ("sv_barline", False)):
            dialog = gui.SVFunctionDialog(0.0, 1000.0, gimmick_layer=layer_id)
            self.assertEqual(
                gui.is_row_visible(dialog._form_layout, dialog.include_shiny_check), expected,
            )
            self.assertFalse(dialog.parameters()["include_shiny"], "off by default")
            dialog.deleteLater()


class FakeSliderBpmMultiplierConfigTests(_Session, unittest.TestCase):
    """Task 4: the plain fake slider's own BPM control."""

    def test_the_dialog_round_trips_the_field_even_when_hidden(self):
        config = gui.GimmickConfig(fake_slider_bpm_multiplier=2.5)
        # Opened on a layer that does not show this row (barline): the value
        # must still come back unchanged.
        dialog = gui.GimmickConfigDialog(config, "barline", gui.GimmickConfig(), self.window)
        self.assertEqual(dialog.config().fake_slider_bpm_multiplier, 2.5)
        dialog.deleteLater()

        dialog = gui.GimmickConfigDialog(config, "fake_slider", gui.GimmickConfig(), self.window)
        self.assertEqual(dialog.fake_slider_bpm_spin.value(), 2.5)
        dialog.fake_slider_bpm_spin.setValue(4.0)
        self.assertEqual(dialog.config().fake_slider_bpm_multiplier, 4.0)
        dialog.deleteLater()

    def test_the_function_dialog_writes_back_to_the_right_field_by_kind(self):
        target = self.window._gimmick_pairing.target

        class _Stub:
            def __init__(self, *a, **k):
                pass

            def exec(self):
                return 1

            def times(self):
                return [10000]

            def object_kind(self):
                return "regular"

            def shiny_count(self):
                return 1

            def bpm_multiplier(self):
                return 3.0

            def deleteLater(self):
                pass

        with patch.object(gui, "FakeSliderFunctionDialog", _Stub):
            self.window._generate_fake_sliders(target, 10000, 10000)

        config = self.window._gimmick_config("fake_slider")
        self.assertEqual(config.fake_slider_bpm_multiplier, 3.0)
        self.assertEqual(config.shiny_bpm_multiplier, 1.0, "the shiny field is untouched")


class KeepSvConversionTests(_Session, unittest.TestCase):
    """Experimental Conversion and Convert Notes' Keep SV (owner, 2026-10-09):
    notes become barline structures or an anti-barline wall and the chart's
    own SV survives -- exactly at every note, along its curve in between --
    with kiai and volume where they were."""

    NOTES = [1000 + 250 * index for index in range(17)]  # 1000 .. 5000

    def setUp(self) -> None:
        super().setUp()
        from osu_io.timing import TimingPoint

        # A linear 1.0x -> 2.0x sweep, one line per note, each offset 10ms
        # back as mappers place them; kiai opening on a green line mid-sweep;
        # the volume dropping on a flat green line after it.
        sweep = self.NOTES[:9]  # 1000 .. 3000
        greens = []
        for index, time_ms in enumerate(sweep):
            point = TimingPoint.inherited_at(time_ms - 10, 1.0 + index / (len(sweep) - 1))
            point.volume = 60
            greens.append(point)
        greens[6].set_kiai(True)                      # 2490: kiai opens
        greens[7].set_kiai(True)
        greens[8].set_kiai(True)
        flat = TimingPoint.inherited_at(3990, 2.0, kiai=True)
        flat.volume = 40                               # 3990: volume drops
        red = TimingPoint.uninherited_at(0, 120)
        red.volume = 60
        closing = TimingPoint.uninherited_at(6000, 120)
        self.document.timing_points[:] = [red, *greens, flat, closing]
        self.document.hit_objects[:] = [
            gui.HitObject(x=256, y=192, time=time_ms, type=gui.TYPE_CIRCLE, hit_sound=0)
            for time_ms in self.NOTES
        ]
        self.before = [(p.time, p.beat_length, p.uninherited, bool(p.kiai), p.volume)
                       for p in self.document.timing_points]
        # Copies: the converter edits a kept kiai/volume edge in place.
        self.ordered_before = gui.sorted_by_time([dataclasses.replace(p) for p in self.document.timing_points])

    def _convert(self, mode, keep_sv=True, whole_map=True):
        original = gui.ConvertNotesDialog

        class Answered(original):
            def exec(self):
                self.mode_combo.setCurrentIndex(self.mode_combo.findData(mode))
                if not self.experimental:
                    self.keep_sv_check.setChecked(keep_sv)
                return gui.QDialog.DialogCode.Accepted

        with patch.object(gui, "ConvertNotesDialog", Answered):
            if whole_map:
                self.window._experimental_conversion(self.window._gimmick_pairing.target)
            else:
                self.window._convert_notes_to_gimmick(
                    self.window._gimmick_pairing.target, "barline", 900, 5100)
        return gui.sorted_by_time(list(self.document.timing_points))

    def test_whole_map_anti_barline_keeps_sv_kiai_and_volume_and_is_one_undo(self):
        from osu_io.timing import active_point_at, kiai_spans, sv_at

        after = self._convert(gui.ConvertNotesDialog.ANTI)
        self.assertGreater(len(after), len(self.before) * 10, "a wall was written")
        for time_ms in self.NOTES:
            self.assertAlmostEqual(sv_at(after, time_ms), sv_at(self.ordered_before, time_ms), places=9,
                                   msg=f"the note at {time_ms} scrolls as it did")
        self.assertEqual(kiai_spans(after, 7000), kiai_spans(self.ordered_before, 7000))
        for time_ms in range(0, 6500, 5):
            self.assertEqual(active_point_at(after, time_ms).volume,
                             active_point_at(self.ordered_before, time_ms).volume, msg=f"volume at {time_ms}")
        self.window._states[self.window._gimmick_pairing.target].history.undo(self.state)
        self.assertEqual(sorted((p.time, p.beat_length, p.uninherited, bool(p.kiai), p.volume)
                                for p in self.document.timing_points), sorted(self.before))

    def test_the_wall_follows_the_sweep_instead_of_its_steps(self):
        after = self._convert(gui.ConvertNotesDialog.ANTI)
        wall = [p.sv_multiplier for p in after if not p.uninherited and 1000 < p.time < 3000]
        self.assertEqual(wall, sorted(wall), "the sweep only rises")
        self.assertGreater(len(set(round(v, 6) for v in wall)), 2 * 9, "a ramp, not nine steps")

    def test_converting_keeps_whistle_kats_and_their_positions(self):
        # Owner, 2026-10-09: a whistle kat read as a don on the clap bit alone,
        # so each was replaced by a fresh clap kat at the centre -- 713 notes of
        # a real map lost their hitsound and their Fancy Arranger position.
        for note in self.document.hit_objects[::2]:
            note.hit_sound = 2  # whistle
            note.x, note.y = 400, 300
        before = [(n.time, n.hit_sound, n.x, n.y) for n in self.document.hit_objects]
        self._convert(gui.ConvertNotesDialog.STRUCTURE, whole_map=True)
        after = sorted((n.time, n.hit_sound, n.x, n.y) for n in self.document.hit_objects)
        self.assertEqual(after, sorted(before))

    def test_a_kats_leading_bars_take_the_notes_speed_after_a_jump(self):
        # Owner, 2026-10-09 (Darling Game Over Love, 50841ms): a Kat's mirrored
        # bars sit before the note, and right after a speed change they still
        # carried the old speed, so the bars came apart.
        from osu_io.timing import TimingPoint

        jump = TimingPoint.inherited_at(4000, 0.5)  # on the note's own millisecond
        self.document.timing_points.append(jump)
        self.document.hit_objects[12].hit_sound = gui.HITSOUND_CLAP  # 4000: a kat
        after = self._convert(gui.ConvertNotesDialog.STRUCTURE, whole_map=True)
        # The Kat's own bars: 3990 is the fixture's own flat line.
        cluster = [p for p in after if not p.uninherited and 3992 <= p.time <= 4008]
        self.assertTrue(any(p.time < 4000 for p in cluster), "the mirrored bars before the note")
        self.assertTrue(all(abs(p.sv_multiplier - 0.5) < 1e-9 for p in cluster),
                        [(p.time, round(p.sv_multiplier, 4)) for p in cluster])

    def test_keep_sv_barline_notes_restore_the_speed_after_every_red_line(self):
        from osu_io.timing import sv_at

        after = self._convert(gui.ConvertNotesDialog.STRUCTURE, whole_map=False)
        reds = [p for p in after if p.uninherited and 900 <= p.time <= 5100]
        self.assertTrue(reds)
        for red in reds:
            index = after.index(red)
            following = after[index + 1] if index + 1 < len(after) else None
            self.assertTrue(following is not None and not following.uninherited and following.time == red.time,
                            f"a green line straight after the red line at {red.time}")
        for time_ms in self.NOTES:
            self.assertAlmostEqual(sv_at(after, time_ms), sv_at(self.ordered_before, time_ms), places=9)


class GimmickViewSizeTests(_Session, unittest.TestCase):
    """The gimmick page's View size (owner, 2026-10-09): every layer band at
    once, as the Editor page's does for its views."""

    def test_view_size_resizes_every_layer_and_leaves_the_editor_alone(self):
        stored = {}
        self.window.settings.set_value = stored.__setitem__
        editor_heights = [view.height() for view in self.window._all_editor_views()]
        self.window.gimmick_view_height_spin.setValue(150)
        layers = [frame.content for frame in self.window._gimmick_views
                  if isinstance(frame.content, (gui.TimelineGameplay, gui.SVEditorView))]
        self.assertTrue(layers)
        self.assertEqual({view.minimumHeight() for view in layers}, {150})
        self.assertEqual(stored.get("gimmick/view_height"), 150)
        self.assertEqual([view.height() for view in self.window._all_editor_views()], editor_heights)


if __name__ == "__main__":
    unittest.main()
