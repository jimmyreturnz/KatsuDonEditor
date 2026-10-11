"""The Fancy Arranger page: osu!'s proportions on the canvas, and the page's
boxes as docks that can be moved, floated, closed and brought back.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from ui import gui
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class CanvasProportionTests(unittest.TestCase):
    """`OsuPlayfieldAdjustmentContainer`: 512x384 is 80% of a 640x480 frame,
    centred, 8 osu!pixels low -- osu!stable's (64, 56). The canvas used to
    stretch the playfield to the full height, 25% larger than osu! draws it."""

    def setUp(self) -> None:
        self.canvas = gui.TransformCanvas()
        self.canvas.resize(1624, 924)  # a 1600x900 render area inside the 12px margin
        self.canvas._update_view_geometry()
        self.render = self.canvas.render_rect
        self.playfield = self.canvas.playfield_rect
        self.scale = self.render.height() / 480.0

    def test_the_render_area_is_16_9(self):
        self.assertAlmostEqual(self.render.width() / self.render.height(), 16 / 9, places=6)

    def test_the_playfield_is_80_percent_of_the_height(self):
        self.assertAlmostEqual(self.playfield.height() / self.render.height(), 0.8, places=6)
        self.assertAlmostEqual(self.playfield.width() / self.playfield.height(), 4 / 3, places=6)

    def test_it_sits_at_osu_stables_64_56(self):
        frame_left = self.render.center().x() - self.render.height() * 4 / 3 / 2
        self.assertAlmostEqual(self.playfield.left() - frame_left, 64 * self.scale, places=4)
        self.assertAlmostEqual(self.playfield.top() - self.render.top(), 56 * self.scale, places=4)

    def test_notes_map_through_the_same_scale(self):
        self.assertAlmostEqual(self.canvas.view_scale, self.scale, places=6)

    def test_circle_size_is_osus(self):
        self.assertAlmostEqual(gui.osu_circle_radius(5.0), 32.0, places=6)
        self.assertAlmostEqual(gui.osu_circle_radius(7.0), 23.04, places=6)


class _Window(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")
        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.window._show_page(gui.PAGE_FANCY)

    def tearDown(self) -> None:
        self.window._reset_fancy_layout()
        self.window.close()
        self._temp.cleanup()


class DockTests(_Window):
    def test_the_canvas_is_the_centre_and_the_rest_are_docks(self):
        docks = self.window.fancy_docks
        self.assertIs(docks.centralWidget(), self.window.canvas)
        self.assertEqual(docks.dockWidgetArea(self.window.fancy_transform_dock), Qt.RightDockWidgetArea)
        self.assertEqual(docks.dockWidgetArea(self.window.fancy_timeline_dock), Qt.BottomDockWidgetArea)

    def test_the_timeline_dock_holds_the_existing_timeline(self):
        dock = self.window.fancy_timeline_dock
        for widget in (self.window.timeline, self.window.fancy_timing_bar, self.window.fancy_density,
                       self.window.snap_combo):
            self.assertTrue(dock.isAncestorOf(widget))

    def test_the_layout_comes_back_next_time(self):
        docks = self.window.fancy_docks
        docks.addDockWidget(Qt.TopDockWidgetArea, self.window.fancy_timeline_dock)
        self.window.settings.set_value("fancy/dock_state", docks.saveState())
        second = gui.MainWindow()
        try:
            self.assertEqual(
                second.fancy_docks.dockWidgetArea(second.fancy_timeline_dock), Qt.TopDockWidgetArea)
        finally:
            second._reset_fancy_layout()
            second.close()

    def test_reset_puts_everything_back(self):
        docks = self.window.fancy_docks
        docks.addDockWidget(Qt.LeftDockWidgetArea, self.window.fancy_transform_dock)
        self.window.fancy_timeline_dock.hide()
        self.window._reset_fancy_layout()
        self.assertEqual(docks.dockWidgetArea(self.window.fancy_transform_dock), Qt.RightDockWidgetArea)
        self.assertFalse(self.window.fancy_timeline_dock.isHidden())

    def test_a_floating_dock_still_gets_the_windows_wheel_snap(self):
        """The app-wide filter drops events from other top-level windows, and a
        floating dock is one. Its parent chain still reaches this window."""
        self.window.fancy_timeline_dock.setFloating(True)
        timeline = self.window.timeline
        before = timeline.snap_divisor
        centre = QPointF(timeline.width() / 2, timeline.height() / 2)
        QApplication.sendEvent(timeline, QWheelEvent(
            centre, QPointF(timeline.mapToGlobal(centre.toPoint())), QPoint(0, 0), QPoint(0, 120),
            Qt.NoButton, Qt.AltModifier, Qt.NoScrollPhase, False))
        self.assertNotEqual(timeline.snap_divisor, before)

    def test_the_layout_menu_lists_both_docks_and_a_reset(self):
        menu = self.window.fancy_layout_button.menu()
        self.window._fill_fancy_layout_menu(menu)
        texts = [action.text() for action in menu.actions() if not action.isSeparator()]
        self.assertEqual(len(texts), 3)


class GlideTests(unittest.TestCase):
    def test_a_note_is_drawn_between_its_old_place_and_its_target(self):
        """The target is what callers and hit tests read; the drawing eases
        to it. Tested on the arithmetic because the offscreen platform runs
        with animation off."""
        from types import SimpleNamespace
        canvas = gui.TransformCanvas()
        note = SimpleNamespace(original_index=7, x=0, y=0)
        canvas.positions = {7: (100.0, 40.0)}
        canvas._glide_from = {7: (0.0, 0.0)}
        canvas._glide_t = 0.25
        self.assertEqual(canvas._drawn_position(note), (25.0, 10.0))
        canvas._glide_t = 1.0
        self.assertEqual(canvas._drawn_position(note), (100.0, 40.0))
        canvas.deleteLater()


class FancyButtonTests(_Window):
    def test_the_duplicate_save_button_is_gone(self):
        self.assertFalse(hasattr(self.window, "apply_original_button"))

    def test_export_says_what_it_does(self):
        self.assertEqual(self.window.export_button.text(), "Export as new difficulty…")

    def test_the_transform_button_counts_its_notes(self):
        self.window._selection_changed({0, 1, 2})
        self.assertEqual(self.window.apply_button.text(), "Transform 3 notes")
        self.window._selection_changed(set())
        self.assertEqual(self.window.apply_button.text(), "Transform Selected Notes")

    def test_the_mode_pills_drive_the_mode(self):
        self.window.mode_buttons["split"].click()
        self.assertTrue(self.window._is_split_mode())
        self.assertTrue(self.window.mode_buttons["split"].isChecked())
        self.assertFalse(self.window.mode_buttons["all"].isChecked())
        self.window.mode_buttons["all"].click()
        self.assertFalse(self.window._is_split_mode())


class SelectionSurvivesChromeTests(_Window):
    """The click-to-clear check was a whitelist of three widgets, so a press on
    the dock or the sub-toolbar (dragging Background Opacity, typing CS) threw
    the selection away before the control acted on it."""

    def _press(self, widget, point=None):
        from PySide6.QtTest import QTest
        QTest.mousePress(widget, Qt.LeftButton, Qt.NoModifier, point or widget.rect().center())
        QTest.mouseRelease(widget, Qt.LeftButton, Qt.NoModifier, point or widget.rect().center())

    def setUp(self) -> None:
        super().setUp()
        QApplication.processEvents()
        self.window._selection_changed({0, 1})
        self.assertEqual(self.window.selected, {0, 1})

    def test_a_press_on_the_sub_toolbar_keeps_it(self):
        self._press(self.window.circle_size_control.value_box)
        self.assertEqual(self.window.selected, {0, 1})

    def test_a_press_on_the_transform_dock_keeps_it(self):
        self._press(self.window.fancy_transform_dock)
        self.assertEqual(self.window.selected, {0, 1})

    def test_a_press_on_empty_canvas_clears_it(self):
        self._press(self.window.canvas, QPoint(3, 3))
        self.assertEqual(self.window.selected, set())


class CircleSizeTests(_Window):
    def test_radius_follows_cs_from_0_to_10(self):
        self.assertAlmostEqual(gui.osu_circle_radius(0.0), 54.4, places=6)
        self.assertAlmostEqual(gui.osu_circle_radius(10.0), 9.6, places=6)

    def test_loading_a_map_sets_the_cs_box_from_it(self):
        # The fixture says CircleSize:5; the box used to stay at its 7.0.
        self.assertAlmostEqual(self.window.circle_size_control.value(), 5.0)
        self.assertAlmostEqual(self.window.canvas.circle_size, 5.0)

    def test_the_cs_slider_and_box_are_one_value(self):
        control = self.window.circle_size_control
        seen = []
        control.changed.connect(seen.append)
        control.slider.setValue(420)
        self.assertAlmostEqual(control.value(), 4.2)
        self.assertAlmostEqual(self.window.canvas.circle_size, 4.2)
        # One emit per step: the slider writes the box, and only the box emits.
        self.assertEqual(seen, [4.2])
        control.value_box.setValue(6.5)
        self.assertEqual(control.slider.value(), 650)

    def test_hit_radius_is_the_drawn_radius(self):
        canvas = self.window.canvas
        canvas.circle_size = 10.0
        canvas._update_view_geometry()
        note = canvas.notes[0]
        canvas.selected = {note.original_index}
        x, y = canvas.positions.get(note.original_index, (note.x, note.y))
        centre = QPointF(canvas.view_offset_x + x * canvas.view_scale, canvas.view_offset_y + y * canvas.view_scale)
        radius = canvas._drawn_radius()
        self.assertIsNotNone(canvas._note_at_canvas_position(centre + QPointF(radius * 0.9, 0)))
        self.assertIsNone(canvas._note_at_canvas_position(centre + QPointF(radius * 1.5 + 30, 0)))

    def test_drawn_radius_stays_visible_on_the_smallest_canvas(self):
        canvas = gui.TransformCanvas()
        canvas.resize(300, 160)
        canvas._update_view_geometry()
        canvas.circle_size = 10.0
        self.assertGreaterEqual(canvas._drawn_radius(), 2 * 1.5)
        canvas.deleteLater()


class TransformAnimationSettingTests(unittest.TestCase):
    def test_off_means_no_glide(self):
        from types import SimpleNamespace
        canvas = gui.TransformCanvas()
        canvas.show()
        note = SimpleNamespace(original_index=1, x=0, y=0, is_kat=False, is_finisher=False)
        canvas.animate_transforms = False
        canvas.set_state([note], {1: (50, 50)}, set())
        self.assertEqual(canvas._glide_t, 1.0)
        canvas.close()


if __name__ == "__main__":
    unittest.main()
