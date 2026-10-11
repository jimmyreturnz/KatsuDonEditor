"""Regression coverage for a round of owner feedback on the Editor page:

- clicking inside an Editor-page (symmetric) chart view must not seek the
  shared playhead, while the Fancy Arranger timeline's click-to-seek stays
- the M3 note tool row is global (applies to whichever chart view has
  focus), not duplicated per view
- slider/spinner get distinct rendering (yellow brush / skin image)
- Fancy Arranger's transform-controls panel is wide enough for its button
  text, and it has its own timing bar + density chart again (a second
  instance of each widget type, since a QWidget can only live in one
  layout at a time -- see self._timing_bars / self._density_views)
- dropping a background on the Fancy Arranger canvas marks the difficulty
  dirty, so Ctrl+S actually writes the new reference instead of silently
  skipping it ("Nothing to save.")
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui import gui
from ui import common
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _click(view: gui.TimelineGameplay, x: float, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
    press = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(x, 100), button, button, Qt.KeyboardModifier.NoModifier,
    )
    view.mousePressEvent(press)
    release = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(x, 100), button, button, Qt.KeyboardModifier.NoModifier,
    )
    view.mouseReleaseEvent(release)


class ClickToSeekTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")

        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def test_editor_chart_view_click_does_not_seek(self):
        frame = next(f for f in self.window._editor_views if f.view_type == "chart")
        view = frame.chart_view
        view.resize(800, 200)
        view.window_ms = 2000.0
        view.current_time = 5000.0
        self.assertTrue(view.editor_view)

        seeks: list[int] = []
        view.seek_requested.connect(seeks.append)

        _click(view, view.x_for_time(6000.0))

        self.assertEqual(seeks, [], "Editor-page click must not request a seek")

    def test_fancy_arranger_timeline_click_still_seeks(self):
        timeline = self.window.timeline
        timeline.resize(800, 200)
        timeline.window_ms = 2000.0
        timeline.current_time = 5000.0
        # Drawn centred like the Editor's since 2026-10-08, but still the
        # arranger's surface, not an Editor view.
        self.assertFalse(timeline.editor_view)

        seeks: list[int] = []
        timeline.seek_requested.connect(seeks.append)

        _click(timeline, timeline.x_for_time(6000.0))

        self.assertEqual(len(seeks), 1, "Fancy Arranger click-to-seek must be unchanged")

    def test_the_overview_bars_emit_one_seek_per_click(self):
        """The click-stutter report. Both bars used to emit on press *and* on
        release at the same pixel; the audio engine coalesces a burst, so the
        duplicate was not dropped but parked, landing 50ms later as a second
        sink teardown and rebuild -- one hiccup just after every click.
        """
        for bar in (gui.TimingOverviewBar(), gui.DensityOverview()):
            with self.subTest(bar=type(bar).__name__):
                bar.resize(400, bar.height())
                bar.duration_ms = 60000
                seeks: list[int] = []
                bar.seek_requested.connect(seeks.append)

                _click(bar, 200.0)
                self.assertEqual(len(seeks), 1, "press and release are one click")

                # A second click on the same pixel is a real request: playback
                # has moved on since, so it must not be swallowed as a repeat.
                _click(bar, 200.0)
                self.assertEqual(len(seeks), 2)

                # A drag that actually moves still seeks per new millisecond.
                bar.mousePressEvent(QMouseEvent(
                    QMouseEvent.Type.MouseButtonPress, QPointF(100.0, 10.0),
                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier))
                before = len(seeks)
                for x in (120.0, 140.0, 160.0):
                    bar.mouseMoveEvent(QMouseEvent(
                        QMouseEvent.Type.MouseMove, QPointF(x, 10.0),
                        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier))
                self.assertEqual(len(seeks) - before, 3, "a real drag still seeks")


class SliderSpinnerRenderingTests(unittest.TestCase):
    def test_slider_brush_is_distinct_from_don_and_kat(self):
        view = gui.TimelineGameplay()
        self.assertNotEqual(view.slider_brush, view.don_brush)
        self.assertNotEqual(view.slider_brush, view.kat_brush)

    def test_spinner_pixmap_loads(self):
        pixmap = gui.spinner_pixmap()
        self.assertFalse(pixmap.isNull(), "assets/skins/spinner.png must load")

    def test_paint_with_slider_and_spinner_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "audio.mp3").write_bytes(b"\x00")
            path = write_fixture(directory, "full_v14")  # includes a fake slider

            from osu_io.parser import parse_osu
            document = parse_osu(path)

        view = gui.TimelineGameplay()
        view.set_symmetric(True)
        view.resize(400, 200)
        view.load_document(document)
        view.grab()  # forces a paintEvent; must not raise


class FancyArrangerPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")

        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def test_transform_controls_panel_is_wide_enough_for_its_button_text(self):
        """340px as the mockup draws it -- every button in it still has to
        fit its own label at that width."""
        panel = self.window.transform_controls_panel
        self.window.page_stack.setCurrentWidget(self.window.fancy_arranger_page)
        self.window.mode_buttons["split"].click()
        panel.resize(panel.minimumWidth(), panel.height())
        gui.QApplication.processEvents()
        for button in panel.findChildren(gui.QPushButton):
            if button.isVisibleTo(panel) and button.text():
                with self.subTest(text=button.text()):
                    self.assertGreaterEqual(button.width(), gui.button_text_width(button) - 1)

    def test_fancy_arranger_has_its_own_timing_bar_and_density(self):
        self.assertIsInstance(self.window.fancy_timing_bar, gui.TimingOverviewBar)
        self.assertIsInstance(self.window.fancy_density, gui.DensityOverview)
        self.assertIsNot(self.window.fancy_timing_bar, self.window.timing_bar)
        self.assertIn(self.window.fancy_timing_bar, self.window._timing_bars)
        self.assertIn(self.window.fancy_density, self.window._density_views)

    def test_activating_a_difficulty_loads_both_timing_bars_and_fancy_density(self):
        self.assertEqual(self.window.fancy_timing_bar.duration_ms, self.window.timing_bar.duration_ms)
        self.assertGreater(self.window.fancy_density.duration_ms, 1)

    def test_add_view_defaults_to_the_difficulty_being_edited(self):
        entries = [("Muzukashii", Path("m.osu")), ("Oni", Path("o.osu")), ("Inner", Path("i.osu"))]
        dialog = gui.AddViewDialog(entries, self.window, Path("o.osu"))
        self.assertEqual(dialog.selected_difficulty_path(), Path("o.osu"))
        dialog.deleteLater()

    def test_add_view_falls_back_to_the_first_difficulty(self):
        entries = [("Muzukashii", Path("m.osu")), ("Oni", Path("o.osu"))]
        dialog = gui.AddViewDialog(entries, self.window)
        self.assertEqual(dialog.selected_difficulty_path(), Path("m.osu"))
        dialog.deleteLater()

    def test_view_chrome_buttons_are_wide_enough_for_their_glyph(self):
        """The window sheet's 8px 16px button padding is wider than the 28px
        these used to be fixed at, so ✕ and 🔒 were clipped away entirely."""
        frames = self.window.findChildren(gui.EditorViewFrame)
        self.assertTrue(frames)
        for frame in frames:
            for button in (frame.close_button, frame.lock_button):
                self.assertGreaterEqual(button.width(), button.sizeHint().width())

    def test_every_tool_button_is_the_same_width(self):
        """Both rows, not each: they swap into the same spot, so a per-label
        width made the whole row shift on every switch."""
        buttons = [
            *self.window.tool_buttons.values(),
            self.window.new_combo_button,
            *self.window.sv_tool_buttons.values(),
        ]
        widths = {button.width() for button in buttons}
        self.assertEqual(len(widths), 1, f"tool buttons differ in width: {widths}")
        # Wide enough for the longest label, not clipped to the shortest.
        self.assertGreaterEqual(
            widths.pop(), max(button.sizeHint().width() for button in buttons)
        )

    def test_no_button_is_narrower_than_the_label_it_draws(self):
        """The pink-square bug, in one assertion.

        A QPushButton fixed narrower than the window stylesheet's 32px of
        horizontal padding has negative room for its text, so Qt paints the
        body over the whole label and the button reads as blank -- which is
        how the Fancy Arranger's +/- buttons went "missing" at a typed 30px
        and 28px. Compared against `button_text_width`, which is glyphs *plus*
        that padding: comparing against the glyphs alone would have let both
        of those through.
        """
        from PySide6.QtWidgets import QPushButton

        clipped = [
            (button.text(), button.width(), gui.button_text_width(button))
            for button in self.window.findChildren(QPushButton)
            if button.text() and button.width() < gui.button_text_width(button)
        ]
        self.assertEqual(clipped, [], f"buttons narrower than their label: {clipped}")

    def test_a_checkable_button_is_measured_in_its_checked_chrome(self):
        """`QPushButton:checked` is `border: 1px solid` against the base rule's
        `border: 0`, so selecting a button costs it two pixels that were never
        in the width it was sized to -- and every tool row's selected button
        clipped the last letter of its own label. Font-independent, unlike the
        weight half of the same rule, so this holds on the offscreen platform
        where every glyph measures the same."""
        from PySide6.QtWidgets import QPushButton

        checkable = QPushButton("Select", self.window)
        checkable.setCheckable(True)
        plain = QPushButton("Select", self.window)
        try:
            self.assertGreater(
                common.button_chrome_width(checkable), common.button_chrome_width(plain),
            )
        finally:
            checkable.deleteLater()
            plain.deleteLater()

    def test_a_ragged_row_still_fits_every_label(self):
        """`widths=False` used to leave the width to sizeHint, which Qt takes in
        the font the button *reports* -- 600 even while a checked one is painted
        at 700."""
        from PySide6.QtWidgets import QPushButton

        buttons = [QPushButton(text, self.window) for text in ("Select", "Multiple Fake Slider")]
        for button in buttons:
            button.setCheckable(True)
        try:
            gui.equalize_button_widths(buttons, heights=True, widths=False)
            self.assertNotEqual(
                buttons[0].minimumWidth(), buttons[1].minimumWidth(),
                "widths=False must stay ragged",
            )
            for button in buttons:
                with self.subTest(text=button.text()):
                    self.assertGreaterEqual(
                        button.minimumWidth(), gui.button_text_width(button),
                    )
        finally:
            for button in buttons:
                button.deleteLater()

    def test_all_three_playback_rows_are_sized(self):
        """The gimmick page's row was left out of showEvent and kept its
        build-time hint -- unpadded, and five pixels short of "100%"."""
        rows = (
            [self.window.editor_play_button, *self.window.editor_playback_speed_buttons],
            [self.window.timeline_play_button, *self.window.playback_speed_buttons],
            [self.window.gimmick_play_button, *self.window.gimmick_playback_speed_buttons],
        )
        for row in rows:
            with self.subTest(first=row[1].text()):
                self.assertEqual(len({button.width() for button in row}), 1)
                for button in row:
                    self.assertGreaterEqual(
                        button.width(), gui.button_text_width(button),
                    )

    def test_the_fancy_arranger_numbers_carry_their_own_arrows(self):
        """The pink +/- pair beside each number is gone (Fancy Arranger
        mockup): the box steps itself, like every other number in the app."""
        from PySide6.QtWidgets import QPushButton

        controls = [
            gui.ParameterControl(
                {"key": "x", "label": "X", "type": "int", "min": 0, "max": 10, "default": 1}, "X"
            ),
            gui.ParameterControl(
                {"key": "seed", "label": "Seed", "type": "int", "min": 0, "max": 99, "default": 0}
            ),
            gui.DifficultyValueControl("HP", 5.0, "tip"),
        ]
        for control in controls:
            control.setParent(self.window)
            control.show()
            self.assertFalse(
                [b for b in control.findChildren(QPushButton) if b.text() in {"+", "-"}]
            )
        spins = [controls[0].spin, controls[1].spin, controls[2].value_box]
        for spin in spins:
            self.assertNotEqual(spin.buttonSymbols(), gui.QAbstractSpinBox.NoButtons)
            # The value fits: the box's text area is not eaten by its arrows.
            self.assertGreaterEqual(
                spin.lineEdit().width(), spin.fontMetrics().horizontalAdvance(spin.text()),
            )
        for control in controls:
            control.deleteLater()


class BackgroundDropSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")
        self.image = directory / "external" / "bg.png"
        self.image.parent.mkdir()
        self.image.write_bytes(b"\x89PNG\r\n")

        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def test_drop_marks_the_difficulty_dirty(self):
        self.assertFalse(self.window.state.history.dirty)
        self.window._background_dropped(str(self.image))
        self.assertTrue(self.window.state.history.dirty)

    def test_ctrl_s_writes_the_new_background_reference(self):
        from osu_io.parser import parse_osu

        self.window._background_dropped(str(self.image))
        self.window.save_all_states()

        reparsed = parse_osu(self.path)
        self.assertEqual(gui.extract_background_filename(reparsed), "bg.png")


class _Mapset(unittest.TestCase):
    """One folder, several difficulties on one audio file -- plus an
    osu!standard one, which is what the mode filter exists for."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")

        source = self.path.read_bytes()
        self.second = directory / "second.osu"
        self.second.write_bytes(source.replace(b"Version:Oni", b"Version:Muzukashii"))
        self.third = directory / "third.osu"
        self.third.write_bytes(source.replace(b"Version:Oni", b"Version:Futsuu"))
        # Same folder, same audio, not taiko: the file "all difficulties" must
        # not offer, because its hit objects are not taiko notes.
        self.standard = directory / "standard.osu"
        self.standard.write_bytes(
            source.replace(b"Mode: 1", b"Mode: 0").replace(b"Version:Oni", b"Version:Insane"))

        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.state = self.window.state

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _charts(self):
        return [f for f in self.window._editor_views if f.view_type == "chart"]

    def _open_with(self, view_type, paths):
        """Drive _open_add_view_dialog with a given set of ticks."""
        def run(dialog_self):
            dialog_self.type_combo.setCurrentIndex(
                dialog_self.type_combo.findData(view_type))
            for item in dialog_self._items():
                item.setCheckState(
                    Qt.CheckState.Checked
                    if Path(item.data(Qt.ItemDataRole.UserRole)) in paths
                    else Qt.CheckState.Unchecked
                )
            return gui.QDialog.Accepted

        with patch.object(gui.AddViewDialog, "exec", run):
            self.window._open_add_view_dialog()


class AddViewDialogMultiSelectTests(_Mapset):
    def _dialog(self):
        return gui.AddViewDialog(
            self.window._taiko_difficulty_entries(), self.window, self.path)

    def test_the_open_difficulty_starts_ticked(self):
        dialog = self._dialog()
        self.assertEqual(dialog.selected_difficulty_paths(), [self.path])
        self.assertEqual(dialog.selected_difficulty_path(), self.path)
        dialog.deleteLater()

    def test_ticking_several_returns_several(self):
        dialog = self._dialog()
        for item in dialog._items():
            if Path(item.data(Qt.ItemDataRole.UserRole)) == self.second:
                item.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(
            set(dialog.selected_difficulty_paths()), {self.path, self.second})
        dialog.deleteLater()

    def test_the_all_checkbox_ticks_every_row(self):
        dialog = self._dialog()
        dialog.all_difficulties_check.setChecked(True)
        self.assertEqual(
            set(dialog.selected_difficulty_paths()),
            {self.path, self.second, self.third},
        )
        dialog.all_difficulties_check.setChecked(False)
        self.assertEqual(dialog.selected_difficulty_paths(), [])
        dialog.deleteLater()

    def test_unticking_one_row_clears_the_all_box(self):
        dialog = self._dialog()
        dialog.all_difficulties_check.setChecked(True)
        dialog._items()[0].setCheckState(Qt.CheckState.Unchecked)
        self.assertFalse(dialog.all_difficulties_check.isChecked())
        dialog.deleteLater()

    def test_a_non_taiko_difficulty_is_not_offered(self):
        offered = {path for _label, path in self.window._taiko_difficulty_entries()}
        self.assertEqual(offered, {self.path, self.second, self.third})
        self.assertNotIn(self.standard, offered)


class OpenChartsForEveryDifficultyTests(_Mapset):
    def test_one_chart_each_and_no_sv_views(self):
        before_sv = len([f for f in self.window._editor_views if f.view_type == "sv"])
        self._open_with("chart", {self.path, self.second, self.third})

        charted = {f.difficulty_path for f in self._charts()}
        self.assertEqual(charted, {self.path, self.second, self.third})
        self.assertEqual(
            len([f for f in self.window._editor_views if f.view_type == "sv"]),
            before_sv,
            "opening charts must not open timing points as well",
        )

    def test_running_it_twice_adds_nothing(self):
        self._open_with("chart", {self.path, self.second, self.third})
        count = len(self._charts())
        self._open_with("chart", {self.path, self.second, self.third})
        self.assertEqual(len(self._charts()), count)

    def test_a_different_view_type_is_still_added(self):
        """Idempotence is per (difficulty, type), not per difficulty."""
        self._open_with("chart", {self.second})
        self._open_with("sv", {self.second})
        types = {
            f.view_type for f in self.window._editor_views
            if f.difficulty_path == self.second
        }
        self.assertEqual(types, {"chart", "sv"})

    def test_every_chart_opens_at_the_shared_zoom_and_playhead(self):
        self.window._zoom_changed(self.path, 5500.0, None)
        self.window.seek_audio(7000.0)
        self._open_with("chart", {self.second, self.third})
        for frame in self._charts():
            self.assertEqual(frame.chart_view.window_ms, 5500.0)
            self.assertAlmostEqual(
                frame.chart_view.current_time,
                self.window.timeline.current_time, places=3)


class PlaybackDifficultyTests(_Mapset):
    """The sounded difficulty is the one whose view was last clicked into.

    There was a "Hitsounds from" combo that pinned one; the owner dropped it
    on 2026-10-07."""

    def _times(self):
        return list(self.window.hitsounds.schedule[0])

    def _focus_chart_of(self, path):
        """Open a chart view of `path` and focus it the way Qt would."""
        self.window._add_editor_view("chart", path)
        view = next(f.chart_view for f in reversed(self.window._editor_views)
                    if f.difficulty_path == path and f.view_type == "chart")
        self.window._editor_view_focus_changed(None, view)
        return view

    def test_there_is_no_choice_to_pin(self):
        self.assertFalse(hasattr(self.window, "hitsound_source_combo"))

    def test_an_edit_in_the_focused_difficulty_is_rescheduled(self):
        self._focus_chart_of(self.second)
        self.window._place_note(self.second, "don", 9876.0, False)
        self.assertIn(9876, self._times())

    def test_an_edit_elsewhere_is_not(self):
        self._focus_chart_of(self.second)
        self.window._place_note(self.third, "don", 9876.0, False)
        self.assertNotIn(9876, self._times())

    def test_the_timing_bars_stay_on_the_active_difficulty(self):
        """Only the hit objects follow focus -- an overlay describing a
        difficulty other than the one being edited disagrees with the chart
        under it."""
        self._focus_chart_of(self.second)
        before = list(self.window.timing_bar.timing_markers)
        other = self.window._ensure_state(self.second)
        other.document.timing_points[0].time = 12345.0
        self.window._refresh_difficulty_sv_views(self.second)
        self.assertEqual(list(self.window.timing_bar.timing_markers), before)

    def test_active_follows_the_focused_views_difficulty(self):
        other = self.window._ensure_state(self.second)
        other.document.hit_objects[0].time = 4321
        self._focus_chart_of(self.second)
        self.assertIs(self.window._hitsound_state(), other)
        self.assertIn(4321, self._times())

        self._focus_chart_of(self.path)
        self.assertIs(self.window._hitsound_state(), self.state)
        self.assertNotIn(4321, self._times())

    def test_focus_does_not_reach_the_gimmick_page(self):
        self._focus_chart_of(self.second)
        self.window._show_page(gui.PAGE_GIMMICK)
        self.assertIs(self.window._hitsound_state(), self.state)

class SessionViewsTests(_Mapset):
    """Views survive a trip to the song list (owner's report, 2026-10-07:
    "editor view gets wiped once I selected and ordered difficulties and
    come back to song selection")."""

    def _round_trip(self, path):
        self.window._back_to_library()
        self.assertEqual(self.window._editor_views, [])
        self.window._load_map_path(path, refresh_difficulties=False)

    def test_the_same_views_come_back_in_the_same_order(self):
        self.window._add_editor_view("chart", self.second)
        self.window._add_editor_view("gameplay", self.path)
        self.window._move_view(self.window._editor_views[-1], -1)
        self.window._editor_views[0].lock_button.setChecked(True)
        before = self.window._editor_view_layout()
        self.assertEqual(len(before), 4)

        self._round_trip(self.path)
        self.assertEqual(self.window._editor_view_layout(), before)

    def test_a_difficulty_opened_for_the_first_time_gets_the_default_pair(self):
        self.window._add_editor_view("gameplay", self.path)
        self._round_trip(self.third)
        self.assertEqual(
            self.window._editor_view_layout(),
            [("chart", self.third, False), ("sv", self.third, False)])


class PageWheelTests(_Mapset):
    """The wheel scrolls the song anywhere on the page, not only over a lane
    (owner's call, 2026-10-07)."""

    def _wheel(self, widget):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent
        centre = QPointF(widget.rect().center())
        event = QWheelEvent(
            centre, QPointF(widget.mapToGlobal(centre.toPoint())), QPoint(0, 0),
            QPoint(0, -120), gui.Qt.NoButton, gui.Qt.NoModifier,
            gui.Qt.NoScrollPhase, False)
        gui.QApplication.sendEvent(widget, event)

    def test_the_wheel_over_the_space_around_the_views_moves_the_playhead(self):
        """A wheel the views ignore (a header, a label, the space below them)
        is passed up to the scroll viewport by Qt, which is where it is
        caught -- sent there directly, since a synthetic event is not
        propagated the way a real one is."""
        self.window.seek_audio(1000.0)
        viewport = self.window.editor_views_layout.parentWidget().parentWidget()
        self._wheel(viewport)
        self.assertGreater(self.window.timeline.current_time, 1000.0)

    def test_the_strip_above_the_views_moves_it_too(self):
        self.window.seek_audio(1000.0)
        page = self.window.page_stack.widget(gui.PAGE_EDITOR)
        self._wheel(page)
        self.assertGreater(self.window.timeline.current_time, 1000.0)

    def test_the_page_does_not_scroll_vertically_for_it(self):
        scroll = self.window.editor_views_layout.parentWidget().parentWidget().parentWidget()
        bar = scroll.verticalScrollBar()
        bar.setRange(0, 500)
        bar.setValue(0)
        self._wheel(self.window.editor_views_layout.parentWidget())
        self.assertEqual(bar.value(), 0)


class MoveAcrossDifficultiesTests(_Mapset):
    """Up/down used to stop dead at a difficulty group's edge.

    Each difficulty keeps its views in a group, and a move never files a view
    under another difficulty. So a difficulty with a single view -- one chart
    each is exactly what ticking every difficulty produces -- had two buttons
    that did nothing. At the edge the group now moves instead.
    """

    def setUp(self) -> None:
        super().setUp()
        self.window.resize(1400, 1000)
        self._open_with("chart", {self.second})
        _APP.processEvents()

    def _screen_order(self):
        _APP.processEvents()
        frames = [f for f in self.window._editor_views if not f.compact]
        return [
            (f.view_type, f.difficulty_path) for f in sorted(
                frames, key=lambda f: f.mapTo(self.window, f.rect().topLeft()).y())
        ]

    def _frame(self, view_type, path):
        return next(
            f for f in self.window._editor_views
            if f.view_type == view_type and f.difficulty_path == path)

    def test_a_lone_view_moves_up_past_the_difficulty_above(self):
        lone = self._frame("chart", self.second)
        self.assertEqual(self._screen_order()[-1], ("chart", self.second))
        lone.move_requested.emit(lone, -1)
        self.assertEqual(self._screen_order()[0], ("chart", self.second))

    def test_the_last_view_of_a_group_moves_its_group_down(self):
        last_oni = [f for f in self.window._editor_views
                    if f.difficulty_path == self.path][-1]
        last_oni.move_requested.emit(last_oni, 1)
        order = self._screen_order()
        self.assertEqual(order[0], ("chart", self.second))

    def test_a_view_is_never_filed_under_another_difficulty(self):
        """Every group still holds one difficulty's views only."""
        lone = self._frame("chart", self.second)
        lone.move_requested.emit(lone, -1)
        for path, group_layout in self.window._editor_view_groups.items():
            for index in range(group_layout.count()):
                widget = group_layout.itemAt(index).widget()
                if isinstance(widget, gui.EditorViewFrame):
                    self.assertEqual(widget.difficulty_path, path)

    def test_inside_a_group_it_still_swaps_one_view(self):
        chart = self._frame("chart", self.path)
        before = self._screen_order()
        chart.move_requested.emit(chart, 1)
        after = self._screen_order()
        self.assertEqual(after[:2], [before[1], before[0]])

    def test_the_ends_of_the_page_are_no_ops(self):
        order = self._screen_order()
        top = self._frame(*order[0])
        top.move_requested.emit(top, -1)
        self.assertEqual(self._screen_order(), order)
        bottom = self._frame(*order[-1])
        bottom.move_requested.emit(bottom, 1)
        self.assertEqual(self._screen_order(), order)


class AddViewDialogPerPageTests(_Mapset):
    def test_the_gimmick_page_keeps_its_single_difficulty_combo(self):
        """It edits exactly one difficulty; a list of checkboxes there would
        offer a choice the page does not have."""
        dialog = gui.AddViewDialog(
            [("Gimmick: oni", self.path)], self.window, self.path, gimmick=True)
        self.assertIsNotNone(dialog.difficulty_combo)
        self.assertIsNone(dialog.difficulty_list)
        self.assertIsNone(dialog.all_difficulties_check)
        self.assertEqual(dialog.selected_difficulty_paths(), [self.path])
        dialog.deleteLater()

    def test_the_editor_list_shows_every_difficulty_without_scrolling(self):
        """Sized from its rows, not a typed 140px that showed four."""
        entries = [(f"Diff {i}", Path(f"d{i}.osu")) for i in range(12)]
        dialog = gui.AddViewDialog(entries, self.window, entries[0][1])
        rows = dialog.difficulty_list.count()
        row = dialog.difficulty_list.sizeHintForRow(0)
        self.assertGreaterEqual(dialog.difficulty_list.minimumHeight(), rows * row)
        dialog.deleteLater()

    def test_a_huge_set_is_still_bounded_by_the_screen(self):
        entries = [(f"Diff {i}", Path(f"d{i}.osu")) for i in range(400)]
        dialog = gui.AddViewDialog(entries, self.window, entries[0][1])
        screen = dialog.screen() or _APP.primaryScreen()
        self.assertLessEqual(
            dialog.difficulty_list.minimumHeight(),
            screen.availableGeometry().height())
        dialog.deleteLater()


class DensityTests(unittest.TestCase):
    """The density histogram reads against the chart's most common BPM, one
    bar per 1% of the song (owner, 2026-10-08)."""

    def test_the_most_common_bpm_is_the_one_in_force_longest(self):
        # aleph-0's shape: 250 most of the way, bursts of 400, a dip to 140.
        from osu_io.timing import TimingPoint
        points = [
            TimingPoint(0, 240.0), TimingPoint(30000, 150.0), TimingPoint(40000, 240.0),
            TimingPoint(90000, 60000 / 140), TimingPoint(100000, 240.0),
        ]
        self.assertEqual(gui.most_common_beat_length(points, 150000), 240.0)
        # A line after the last object counts for nothing.
        points.append(TimingPoint(200000, 150.0))
        self.assertEqual(gui.most_common_beat_length(points, 150000), 240.0)

    def test_sixteen_notes_in_four_beats_is_a_full_bar(self):
        beat = 250.0  # 240 BPM; 4 beats are 1000ms
        duration = 100000.0  # so each 1% bar is exactly 1000ms
        stream = [1000 * 10 + i * 62.5 for i in range(16)]  # 16 in bar 10
        half = [1000 * 20 + i * 125.0 for i in range(8)]     # 8 in bar 20
        bars = gui.density_bars(sorted(stream + half), duration, beat)
        self.assertEqual(len(bars), gui.DENSITY_BARS)
        self.assertEqual(bars[10][2], 1.0)
        self.assertAlmostEqual(bars[20][2], 0.5)
        self.assertEqual(bars[30][2], 0.0)
        # More than full is still full.
        burst = [1000 * 40 + i * 30.0 for i in range(30)]
        self.assertEqual(gui.density_bars(burst, duration, beat)[40][2], 1.0)


if __name__ == "__main__":
    unittest.main()
