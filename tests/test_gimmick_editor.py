"""M5 gimmick editor: the entry question and what it writes.

The dialog is stubbed rather than shown -- what matters here is the decision it
returns and the file and index state that follow from it.
"""
from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QEventLoop, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import QApplication

from ui import gui
from ui import common
from chart import gimmick_session
from osu_io.parser import parse_osu
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _delete_window(window) -> None:
    """Delete a test's window once the test is over. Closed only, every window
    lived to the end of the run: 6.3GB at peak over 241 tests, and 47s after
    OK for Qt to destroy them all at exit (measured 2026-10-11).

    Registered with addCleanup *before* anything else in setUp, so it runs
    last: a test's own cleanups (`dialog.deleteLater` on a dialog parented to
    the window) must reach their objects before the window takes its
    children with it, or they post a delete to freed memory and the next
    event loop pass crashes. `processEvents` never runs deleteLater, so a
    short real loop does; `sendPostedEvents(None, DeferredDelete)` outside
    one deleted the window too, and the next test's first paint crashed."""
    window.deleteLater()
    loop = QEventLoop()
    QTimer.singleShot(0, loop.quit)
    loop.exec()


class _StubDialog:
    """Stands in for GimmickEntryDialog, answering with `action`.

    It carries the real class's constants because gui.py reads them off whatever
    `GimmickEntryDialog` currently names -- which, under patch, is this.
    """

    CREATE = gui.GimmickEntryDialog.CREATE
    USE_CURRENT = gui.GimmickEntryDialog.USE_CURRENT
    CANCEL = gui.GimmickEntryDialog.CANCEL

    action = CANCEL
    constructed = 0
    # Which timing reference the dialog "chose"; None means the open document,
    # which is what every test that does not care about the reference wants.
    reference = None
    offered: list = []

    def __init__(self, version, parent=None, references=None):
        type(self).constructed += 1
        type(self).offered = list(references or [])
        self.version = version

    def exec(self):
        return 1

    def selected_action(self):
        return type(self).action

    def selected_reference(self):
        return type(self).reference

    def deleteLater(self):
        pass


class _GimmickFixture:
    """A window with one map open and its own throwaway pairing index.

    Not a TestCase: inheriting the tests as well as the fixture would re-run
    every entry test against an already-opened session.
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")
        self.index_path = directory / "gimmick_index.json"

        self.window = gui.MainWindow()
        self.addCleanup(_delete_window, self.window)
        self.window._gimmick_index_path = lambda: self.index_path
        self.window._gimmick_pairings = {}
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)

        _StubDialog.constructed = 0
        _StubDialog.action = gui.GimmickEntryDialog.CANCEL
        _StubDialog.reference = None

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _enter(self, action: str) -> bool:
        _StubDialog.action = action
        with patch.object(gui, "GimmickEntryDialog", _StubDialog):
            return self.window._enter_gimmick_page()


class GimmickSharedZoomTests(_GimmickFixture, unittest.TestCase):
    """Every layer on the page zooms together, inside one shared range."""

    def _layers(self):
        return [
            view for view in (
                getattr(frame, "chart_view", None) or getattr(frame, "sv_view", None)
                for frame in self.window._gimmick_views
            )
            if view is not None and not isinstance(view, gui.GameplayViewerView)
        ]

    def _layer(self, layer_id: str):
        for view in self._layers():
            if getattr(view, "gimmick_layer", None) == layer_id:
                return view
        self.fail(f"no {layer_id} layer")

    def setUp(self) -> None:
        super().setUp()
        self.assertTrue(self._enter(gui.GimmickEntryDialog.CREATE))

    def test_the_kiai_and_volume_layer_zooms_with_everything_else(self):
        chart = self._layer("chart")
        chart.window_ms = 8000.0
        chart.zoom_changed.emit(8000.0)
        self.assertEqual({view.window_ms for view in self._layers()}, {8000.0})

    def test_zooming_from_the_kiai_layer_moves_the_rest(self):
        kiai = self._layer("kiai_sound")
        kiai.window_ms = 12000.0
        kiai.zoom_changed.emit(12000.0)
        self.assertEqual({view.window_ms for view in self._layers()}, {12000.0})

    def test_no_layer_is_pushed_past_a_limit_it_enforces_itself(self):
        """The Kiai/Volume layer is an SVEditorView, which allows a 120s window
        while the chart views allow 60s. Broadcasting unclamped set the chart
        views to 120s -- past their own maximum -- and the next notch of the
        wheel on one of them snapped back, so zooming stepped by a factor of
        two one way and not the other.
        """
        kiai = self._layer("kiai_sound")
        self.assertGreater(kiai.MAX_WINDOW_MS, self._layer("chart").MAX_WINDOW_MS)

        kiai.window_ms = kiai.MAX_WINDOW_MS
        kiai.zoom_changed.emit(kiai.MAX_WINDOW_MS)

        for view in self._layers():
            self.assertLessEqual(view.window_ms, view.MAX_WINDOW_MS, view.gimmick_layer)
            self.assertGreaterEqual(view.window_ms, view.MIN_WINDOW_MS, view.gimmick_layer)
        self.assertEqual(len({view.window_ms for view in self._layers()}), 1)

    def test_the_shared_floor_is_respected_too(self):
        chart = self._layer("chart")
        chart.zoom_changed.emit(1.0)
        floor = max(view.MIN_WINDOW_MS for view in self._layers())
        self.assertEqual({view.window_ms for view in self._layers()}, {floor})


class GimmickSkinTests(_GimmickFixture, unittest.TestCase):
    """The gimmick layers are chart views too, so a skin has to reach them.

    It did not: three places built chart views and each appended to
    `_chart_views` itself, so adding the skin to the Editor path reached two of
    the three. The gimmick layers stayed on the built-in art until Settings was
    applied again *after* the page had been opened, which read as the same
    chart looking different on the two pages.
    """

    def test_every_gimmick_layer_gets_the_window_s_skin(self):
        marker = object()
        self.window.skin = marker
        self.assertTrue(self._enter(gui.GimmickEntryDialog.CREATE))

        layers = [frame.chart_view for frame in self.window._gimmick_views
                  if getattr(frame, "chart_view", None) is not None]
        self.assertGreater(len(layers), 0, "the page builds chart layers")
        for view in layers:
            self.assertIs(view.skin, marker)

    def test_the_shared_timeline_and_the_layers_agree(self):
        """The complaint this comes from: the two pages showing the same chart
        with different note art.

        Driven through `_apply_appearance_settings`, the way Settings does it, rather
        than by assigning `window.skin` -- the point is that opening the
        gimmick page afterwards cannot leave a view behind.
        """
        self.window._apply_appearance_settings()
        self.assertTrue(self._enter(gui.GimmickEntryDialog.CREATE))

        self.assertGreater(len(self.window._chart_views), 1)
        for view in self.window._chart_views:
            self.assertIs(view.skin, self.window.skin)

    def test_registration_is_the_only_way_into_the_broadcast(self):
        """Guards the fix rather than its effect: a fourth call site appending
        to `_chart_views` directly would silently reintroduce this."""
        source = Path(gui.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            source.count("_chart_views.append("), 1,
            "chart views are registered through _register_chart_view only",
        )


class NoteOpacityTests(_GimmickFixture, unittest.TestCase):
    """The setting reaches every layer that draws a note, by the same route the
    skin does -- which is the whole reason `_register_chart_view` exists.

    The store is stubbed rather than written: `SettingsManager` wraps the real
    `QSettings`, so a test that sets a value edits the running user's own
    configuration. Every other settings test in this suite stubs it for the
    same reason; these four did not, and left whatever the last one wrote
    behind in the registry.
    """

    def setUp(self) -> None:
        super().setUp()
        self._stored: dict[str, object] = {}
        settings = self.window.settings
        real_int = settings.int_value
        settings.set_value = self._stored.__setitem__
        settings.int_value = (
            lambda key, default=0: int(self._stored.get(key, real_int(key, default)))
        )

    def _set_opacity(self, percent: int) -> None:
        self.window.settings.set_value("appearance/note_opacity", percent)
        self.window._apply_appearance_settings()

    def _opacities(self):
        return [view.don_brush.alpha() for view in self.window._chart_views]

    def test_the_default_reproduces_the_built_in_look(self):
        self._set_opacity(gui.NOTE_OPACITY_DEFAULT_PERCENT)
        self.assertEqual(self.window.timeline.don_brush.alpha(), 180)

    def test_a_higher_setting_makes_every_layer_more_solid(self):
        self._set_opacity(100)
        self.assertTrue(self._enter(gui.GimmickEntryDialog.CREATE))
        self.assertGreater(len(self.window._chart_views), 1)
        for alpha in self._opacities():
            self.assertEqual(alpha, 255)

    def test_a_layer_opened_afterwards_is_not_left_behind(self):
        """The bug the skin had: a view built after Settings applied kept the
        built-in value until Settings was applied again."""
        self._set_opacity(40)
        self.assertTrue(self._enter(gui.GimmickEntryDialog.CREATE))
        for alpha in self._opacities():
            self.assertEqual(alpha, round(180 * 40 / gui.NOTE_OPACITY_DEFAULT_PERCENT))

    def test_repeated_applications_do_not_compound(self):
        """Scaled from the captured base rather than from the current alpha --
        otherwise applying Settings twice halves the notes twice."""
        self._set_opacity(35)
        once = self.window.timeline.don_brush.alpha()
        self.window._apply_appearance_settings()
        self.assertEqual(self.window.timeline.don_brush.alpha(), once)

    def test_the_ghosts_stay_fainter_than_the_notes(self):
        """What a ghost is, is "fainter than a note" -- pinning them while the
        notes went solid would have inverted that."""
        for percent in (20, 70, 100):
            with self.subTest(percent=percent):
                self.window.timeline.set_note_opacity(percent)
                self.assertLess(self.window.timeline.ghost_don_brush.alpha(),
                                self.window.timeline.don_brush.alpha())

    def test_a_chosen_value_is_remembered_and_re_applied(self):
        """What "remember it" means in practice: Settings writes the store,
        and the next read -- a reopened dialog, or the next launch -- gets the
        number back rather than the default."""
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.window.settings, self.window.shortcuts, self.window)
        dialog.note_opacity.setValue(45)
        dialog.apply()
        self.assertEqual(self._stored["appearance/note_opacity"], 45)

        reopened = SettingsDialog(
            self.window.settings, self.window.shortcuts, self.window)
        self.assertEqual(reopened.note_opacity.value(), 45)
        self.window._apply_appearance_settings()
        self.assertEqual(self.window.note_opacity_percent, 45)
        for dead in (dialog, reopened):
            dead.deleteLater()


class ViewReorderTests(_GimmickFixture, unittest.TestCase):
    """The ▲/▼ chrome buttons. Driven through the buttons rather than through
    `_move_view`, because half of what could break is the wiring: the gimmick
    page builds its frames on a different path from the Editor page, and that
    is exactly how the skin came to reach only one of them.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)

    def _order(self) -> list[str]:
        layout = self.window.gimmick_views_layout
        return [
            layout.itemAt(i).widget().gimmick_layer
            for i in range(layout.count())
            if isinstance(layout.itemAt(i).widget(), gui.EditorViewFrame)
        ]

    def _frame(self, layer_id):
        return next(f for f in self.window._gimmick_views
                    if f.gimmick_layer == layer_id)

    def test_down_swaps_a_layer_with_the_one_below_it(self):
        before = self._order()
        self.assertGreater(len(before), 2)
        self._frame(before[0]).move_down_button.click()
        expected = [before[1], before[0], *before[2:]]
        self.assertEqual(self._order(), expected)

    def test_up_puts_it_back(self):
        before = self._order()
        self._frame(before[0]).move_down_button.click()
        self._frame(before[0]).move_up_button.click()
        self.assertEqual(self._order(), before)

    def test_the_list_follows_the_layout(self):
        """`_gimmick_views[0]` decides which layer the tool row and copy/paste
        act on, so a list that disagreed with the screen would aim them at a
        layer the user is not looking at."""
        before = self._order()
        self._frame(before[0]).move_down_button.click()
        self.assertEqual(
            [f.gimmick_layer for f in self.window._gimmick_views], self._order())

    def test_the_ends_do_nothing_rather_than_wrapping(self):
        before = self._order()
        self._frame(before[0]).move_up_button.click()
        self.assertEqual(self._order(), before)
        self._frame(before[-1]).move_down_button.click()
        self.assertEqual(self._order(), before)

    def test_the_trailing_stretch_stays_last(self):
        """The column is held up by a stretch after the frames; stepping onto
        it would file a layer below the thing holding the column open."""
        layout = self.window.gimmick_views_layout
        before = self._order()
        self._frame(before[-1]).move_down_button.click()
        last = layout.itemAt(layout.count() - 1)
        self.assertIsNone(last.widget())

    def _drag(self, frame, rows: float, cancel: bool = False):
        """Press on `frame`'s header and carry it `rows` of its own height,
        through the real header events, then let go and wait for the glide."""
        from PySide6.QtTest import QTest

        header = frame.header
        start = header.mapToGlobal(QPoint(header.width() // 2, 10))

        def send(kind, at, buttons):
            QApplication.sendEvent(header, QMouseEvent(
                kind, QPointF(header.mapFromGlobal(at)), QPointF(at),
                Qt.LeftButton, buttons, Qt.NoModifier))

        send(QEvent.MouseButtonPress, start, Qt.LeftButton)
        send(QEvent.MouseMove, start + QPoint(0, 20), Qt.LeftButton)
        drag = self.window._view_drag
        drag.follow(start.y() + round(frame.height() * rows))
        if cancel:
            QApplication.sendEvent(drag, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        else:
            drag.drop()
        from shiboken6 import isValid

        def landed():
            return not isValid(drag) or drag.isHidden()

        for _ in range(100):
            if landed():
                break
            QTest.qWait(10)
        self.assertTrue(landed(), "the glide never settled")

    def test_dragging_a_header_moves_the_view(self):
        before = self._order()
        self._drag(self._frame(before[0]), 1.6)
        self.assertEqual(self._order(), [before[1], before[0], *before[2:]])
        self.assertEqual([f.gimmick_layer for f in self.window._gimmick_views], self._order())

    def test_focusing_a_view_leaves_its_lane_where_it_was(self):
        """The rim used to inset the lane 2px a side, which narrowed it and
        moved every snap tick off-centre (owner, 2026-10-08). The others fade
        while one is focused, on that page only."""
        frames = self.window._gimmick_views
        before = frames[0].content.geometry()
        self.window._mark_focused_view(frames[0].content)
        QApplication.processEvents()
        self.assertTrue(frames[0].focused)
        self.assertEqual(frames[0].content.geometry(), before)
        self.assertFalse(frames[0].dimmed)
        self.assertTrue(all(frame.dimmed for frame in frames[1:]))
        self.window._mark_focused_view(frames[1].content)
        self.assertTrue(frames[0].dimmed)
        self.assertFalse(frames[1].dimmed)

    def test_the_gameplay_preview_never_fades(self):
        # Owner, 2026-10-09: it shows the result of an edit made elsewhere.
        frame = gui.EditorViewFrame("gameplay", "x")
        frame.set_dimmed(True)
        self.assertFalse(frame.dimmed)
        chart = gui.EditorViewFrame("chart", "x")
        chart.set_dimmed(True)
        self.assertTrue(chart.dimmed)

    def test_escape_or_a_blank_click_lets_go_of_the_focused_view(self):
        """Owner, 2026-10-08: Esc lets go of the focused view first, and the
        next Esc goes back as before; so does a press under the views."""
        self.window._show_page(gui.PAGE_GIMMICK)
        QApplication.processEvents()
        frames = self.window._gimmick_views
        self.window._mark_focused_view(frames[0].content)
        QApplication.sendEvent(self.window, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.assertFalse(any(frame.focused or frame.dimmed for frame in frames))
        self.assertNotEqual(self.window.page_stack.currentIndex(), gui.PAGE_LIBRARY,
                            "the first Esc only let go of the view")

        self.window._mark_focused_view(frames[1].content)
        container = self.window.gimmick_views_layout.parentWidget()
        below = QPointF(10, container.height() - 2)
        QApplication.sendEvent(container, QMouseEvent(
            QEvent.MouseButtonPress, below, container.mapToGlobal(below),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        self.assertFalse(frames[1].focused)

    def test_escape_in_a_text_box_only_leaves_the_box(self):
        from PySide6.QtWidgets import QDialog, QLineEdit, QVBoxLayout
        dialog = QDialog(self.window)
        box = QLineEdit(dialog)
        QVBoxLayout(dialog).addWidget(box)
        dialog.show()
        dialog.activateWindow()
        box.setFocus()
        QApplication.processEvents()
        if QApplication.focusWidget() is not box:
            self.skipTest("the platform gave the box no focus")
        QApplication.sendEvent(box, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.assertTrue(dialog.isVisible(), "the first Esc does not close the dialog")
        self.assertFalse(box.hasFocus())
        dialog.close()

    def test_escape_puts_the_view_back(self):
        before = self._order()
        self._drag(self._frame(before[0]), 2.5, cancel=True)
        self.assertEqual(self._order(), before)


class EditorPageReorderTests(_GimmickFixture, unittest.TestCase):
    """The same buttons on the Editor page, whose frames go into a layout per
    difficulty group rather than one shared column."""

    def _frames(self):
        return [f for f in self.window._editor_views if f.view_type != "density"]

    def _layout_order(self, frame):
        layout = self.window._frame_layout(frame)
        return [
            layout.itemAt(i).widget() for i in range(layout.count())
            if isinstance(layout.itemAt(i).widget(), gui.EditorViewFrame)
        ]

    def test_two_views_in_one_group_swap(self):
        self.window._add_editor_view("chart", self.path)
        self.window._add_editor_view("sv", self.path)
        first, second = self._frames()[-2:]
        before = self._layout_order(first)
        index = before.index(first)
        self.assertIs(before[index + 1], second, "the two land adjacent")
        first.move_down_button.click()
        after = self._layout_order(first)
        self.assertIs(after[index], second)
        self.assertIs(after[index + 1], first)

    def test_a_density_view_can_be_moved_off_the_bottom(self):
        """Density is pinned last when a view is *added*; that is a default for
        where things land, not a rule about where they may sit."""
        self.window._add_editor_view("chart", self.path)
        self.window._add_editor_view("density", self.path)
        density = self.window._editor_views[-1]
        self.assertIs(self._layout_order(density)[-1], density)
        density.move_up_button.click()
        self.assertIsNot(self._layout_order(density)[-1], density)

    def test_a_view_never_leaves_its_own_difficulty_group(self):
        """The two groups are separate layouts, so the walk cannot step out of
        one -- which is what would file a view under the wrong difficulty."""
        self.window._add_editor_view("chart", self.path)
        frame = self.window._editor_views[-1]
        layout = self.window._frame_layout(frame)
        frame.move_up_button.click()
        frame.move_down_button.click()
        self.assertIs(self.window._frame_layout(frame), layout)


class DialogWheelIsolationTests(_GimmickFixture, unittest.TestCase):
    """A dialog's wheel belongs to the dialog.

    `MainWindow` installs its event filter on the *application*, so every event
    in the process runs through it -- including the ones inside a dialog. One
    notch in the Settings dialog seeked the gimmick layer behind it by 125ms.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        # Shown, because `_page_wheel_target` only claims a wheel while the
        # gimmick page is current -- without this the routing below is never
        # exercised and the wheel only worked by reaching the layer directly.
        self.window._show_page(gui.PAGE_GIMMICK)
        self.layer = self.window._gimmick_views[0].chart_view

    def _wheel(self, widget, modifiers=Qt.NoModifier, delta=-120) -> None:
        centre = QPointF(widget.width() / 2, widget.height() / 2)
        QApplication.sendEvent(widget, QWheelEvent(
            centre, QPointF(widget.mapToGlobal(centre.toPoint())),
            QPoint(0, 0), QPoint(0, delta),
            Qt.NoButton, modifiers, Qt.NoScrollPhase, False,
        ))
        QApplication.processEvents()

    def _dialog(self):
        from ui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(
            self.window.settings, self.window.shortcuts, self.window)
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        QApplication.processEvents()
        return dialog

    def test_scrolling_a_dialog_does_not_seek_the_page_behind_it(self):
        dialog = self._dialog()
        before = self.layer.current_time
        self._wheel(dialog.note_opacity)
        self.assertEqual(self.layer.current_time, before)

    def test_alt_wheel_in_a_dialog_does_not_change_the_snap_divisor(self):
        dialog = self._dialog()
        before = self.window.editor_snap_combo.currentData()
        self._wheel(dialog.note_opacity, modifiers=Qt.AltModifier)
        self.assertEqual(self.window.editor_snap_combo.currentData(), before)

    def test_the_page_still_takes_its_own_wheel(self):
        """The guard is scoped to other windows -- what it must not do is stop
        the editor working when no dialog is open.

        Routing first, then a gesture rather than one notch: a notch is one
        snap division, and this fixture's gimmick lines are 60000 BPM, where
        that division is a quarter of a millisecond and rounds to the same
        millisecond. A single notch doing nothing there is the *correct*
        behaviour, not a lost event.
        """
        self.assertIsNotNone(self.window._page_wheel_target(self.layer))
        before = self.layer.current_time
        for _ in range(8):
            self._wheel(self.layer)
        self.assertNotEqual(self.layer.current_time, before)


class ScrollBarWheelTests(_GimmickFixture, unittest.TestCase):
    """The scrollbar is the one control on the page whose whole job is the
    wheel, and it was the one control that did not get it.

    `_page_wheel_target` hands every wheel on the gimmick page to a layer so
    that seeking works wherever the pointer is. Six bands do not fit on one
    screen, so the page has a scrollbar -- and a wheel over it seeked a layer
    and left the scroll position where it was, which made everything below the
    fold reachable only by dragging the bar.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        # The page has to be *shown*: an unshown scroll area is never laid out,
        # so its bar has no range -- and `_page_wheel_target` only claims a
        # wheel while the gimmick page is current, so without this the "a layer
        # still seeks" test below would pass for the wrong reason.
        self.window._show_page(gui.PAGE_GIMMICK)
        # Short enough that the bands cannot all fit: the whole point is the
        # scrollbar, and there is none on a window tall enough to show them.
        self.window.resize(1200, 480)
        for _ in range(8):
            QApplication.processEvents()
        self.scroll = self.window.gimmick_scroll
        self.bar = self.scroll.verticalScrollBar()
        self.layer = self.window._gimmick_views[0].chart_view
        # The window cannot be made shorter than its minimum, and since the
        # bands went edge to edge all seven fit inside it -- so the scroll
        # area is held short instead. The scrollbar is what this class is about.
        self.scroll.setMaximumHeight(300)
        for _ in range(8):
            QApplication.processEvents()

    def _wheel(self, widget, delta=-120) -> None:
        centre = QPointF(widget.width() / 2, widget.height() / 2)
        QApplication.sendEvent(widget, QWheelEvent(
            centre, QPointF(widget.mapToGlobal(centre.toPoint())),
            QPoint(0, 0), QPoint(0, delta),
            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
        ))
        QApplication.processEvents()

    def test_the_bands_overflow_so_there_is_something_to_scroll(self):
        """If this ever stops being true the rest of the class proves nothing."""
        self.assertGreater(self.bar.maximum(), 0)

    def test_a_wheel_over_the_scrollbar_scrolls_the_page(self):
        self.bar.setValue(0)
        self._wheel(self.bar)
        self.assertGreater(self.bar.value(), 0)

    def test_the_gaps_between_bands_seek_rather_than_scroll(self):
        """The bar is the only surface here that does not move. Letting the
        gaps scroll too meant scrolling slid the bands under a stationary
        cursor, so one gesture landed alternately on a gap and on a band and
        did half a scroll and half a seek."""
        self.bar.setValue(0)
        before = self.layer.current_time
        self._wheel(self.scroll.viewport())
        self.assertEqual(self.bar.value(), 0)
        self.assertNotEqual(self.layer.current_time, before)

    def test_one_gesture_across_gaps_and_bands_only_ever_seeks(self):
        """What "scrolling horizontal and vertical simultaneously" was: half
        the notches of a single gesture scrolled and half of them seeked."""
        self.bar.setValue(0)
        times = [self.layer.current_time]
        for index in range(6):
            self._wheel(self.layer if index % 2 else self.scroll.viewport())
            times.append(self.layer.current_time)
        self.assertEqual(self.bar.value(), 0, "the page never slid")
        for earlier, later in zip(times, times[1:]):
            self.assertGreaterEqual(later, earlier, "and none of it went back")
        self.assertGreater(times[-1], times[0])

    def test_the_page_chrome_outside_the_scroll_area_still_seeks(self):
        """The regression this comes from. Scrolling is scoped to the scroll
        area's own space; the timing bar and tool rows above it are not in it,
        and making every off-band wheel scroll left them doing nothing at
        all -- reported as "scrolling no longer moves the view horizontally"."""
        for name in ("gimmick_timing_bar", "gimmick_page"):
            widget = getattr(self.window, name, None)
            if widget is None:
                continue
            with self.subTest(widget=name):
                self.bar.setValue(0)
                before = self.layer.current_time
                self._wheel(widget)
                self.assertNotEqual(self.layer.current_time, before)
                self.assertEqual(self.bar.value(), 0, "and does not scroll")

    def test_only_the_scrollbar_is_exempt(self):
        """Everything else on the page belongs to a band, whatever the
        modifiers -- which is what it has always done."""
        self.assertIsNone(self.window._page_wheel_target(self.bar))
        for widget in (self.scroll.viewport(), self.layer, self.window.gimmick_page):
            with self.subTest(widget=type(widget).__name__):
                self.assertIsNotNone(self.window._page_wheel_target(widget))

    def test_and_does_not_seek_a_layer_at_the_same_time(self):
        self.bar.setValue(0)
        before = self.layer.current_time
        self._wheel(self.bar)
        self.assertEqual(self.layer.current_time, before)

    def test_a_wheel_over_a_layer_still_seeks(self):
        """The rule this narrows, not replaces: seeking has to keep working
        wherever else the pointer is on the page."""
        self.bar.setValue(0)
        before = self.layer.current_time
        self._wheel(self.layer)
        self.assertNotEqual(self.layer.current_time, before)
        self.assertEqual(self.bar.value(), 0)


class GimmickEntryTests(_GimmickFixture, unittest.TestCase):
    def test_no_creates_nothing_and_refuses_the_page(self):
        self.assertFalse(self._enter(gui.GimmickEntryDialog.CANCEL))
        self.assertEqual(self.window._gimmick_pairings, {})
        self.assertFalse(self.index_path.exists())
        self.assertEqual(list(Path(self._temp.name).glob("*[[]Gimmick[]]*.osu")), [])

    def test_yes_copies_the_difficulty_and_appends_the_suffix(self):
        self.assertTrue(self._enter(gui.GimmickEntryDialog.CREATE))

        target = gui.gimmick_path_for(self.path)
        self.assertTrue(target.is_file())
        copy = parse_osu(target)
        self.assertEqual(copy.version, "Oni [Gimmick]")
        # A copy, not a new chart: every hit object comes along.
        original = parse_osu(self.path)
        self.assertEqual(
            [note.to_line("") for note in copy.hit_objects],
            [note.to_line("") for note in original.hit_objects],
        )

    def test_yes_snapshots_the_timing_it_was_copied_from(self):
        self._enter(gui.GimmickEntryDialog.CREATE)
        pairing = self.window._gimmick_pairing
        original = parse_osu(self.path)
        self.assertEqual(
            [point.time for point in pairing.base_timing],
            [point.time for point in original.timing_points],
        )

    def test_use_this_one_edits_in_place_and_writes_no_file(self):
        self.assertTrue(self._enter(gui.GimmickEntryDialog.USE_CURRENT))
        self.assertEqual(self.window._gimmick_pairing.target, self.path)
        self.assertFalse(gui.gimmick_path_for(self.path).exists())

    def test_the_pairing_is_remembered_and_never_asked_again(self):
        self._enter(gui.GimmickEntryDialog.CREATE)
        self.assertEqual(_StubDialog.constructed, 1)
        self.assertTrue(self.index_path.is_file())

        # Second entry: the answer is already on disk.
        self.assertTrue(self._enter(gui.GimmickEntryDialog.CREATE))
        self.assertEqual(_StubDialog.constructed, 1)

        # ...and it survives a restart, since it is read from the index file.
        reloaded = gui.load_index(self.index_path)
        self.assertEqual(
            reloaded[gui.index_key(self.path)].target, gui.gimmick_path_for(self.path)
        )

    def test_a_deleted_gimmick_file_is_forgotten_and_asked_again(self):
        """Deleting the file by hand (testing, tidying up) must not leave the
        page pointing at something that is not there."""
        self._enter(gui.GimmickEntryDialog.CREATE)
        target = self.window._gimmick_pairing.target
        target.unlink()

        _StubDialog.constructed = 0
        with patch.object(gui.QMessageBox, "information", lambda *a, **k: None):
            self.assertTrue(self._enter(gui.GimmickEntryDialog.CREATE))

        self.assertEqual(_StubDialog.constructed, 1, "the question must be asked again")
        self.assertTrue(target.is_file(), "and the file recreated")

    def test_an_existing_gimmick_file_is_adopted_not_duplicated(self):
        self._enter(gui.GimmickEntryDialog.CREATE)
        target = gui.gimmick_path_for(self.path)
        before = target.read_bytes()

        self.window._gimmick_pairings = {}
        with patch.object(gui.QMessageBox, "information", lambda *a, **k: None):
            self.assertTrue(self._enter(gui.GimmickEntryDialog.CREATE))

        self.assertEqual(self.window._gimmick_pairing.target, target)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(len(list(Path(self._temp.name).glob("*[[]Gimmick[]]*.osu"))), 1)

    def test_switching_to_the_page_is_refused_when_the_dialog_is_cancelled(self):
        self.window._show_page(gui.PAGE_EDITOR)
        _StubDialog.action = gui.GimmickEntryDialog.CANCEL
        with patch.object(gui, "GimmickEntryDialog", _StubDialog):
            self.window._switch_page(gui.PAGE_GIMMICK)
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_EDITOR)

    def test_switching_to_the_page_lands_there_once_answered(self):
        self.window._show_page(gui.PAGE_EDITOR)
        _StubDialog.action = gui.GimmickEntryDialog.USE_CURRENT
        with patch.object(gui, "GimmickEntryDialog", _StubDialog):
            self.window._switch_page(gui.PAGE_GIMMICK)
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_GIMMICK)
        self.assertTrue(self.window.gimmick_page_button.isChecked())


class GimmickLayerTests(_GimmickFixture, unittest.TestCase):
    """The gimmick layers, built once a session is open."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)

    def test_the_layers_open_in_order(self):
        self.assertEqual(
            [frame.gimmick_layer for frame in self.window._gimmick_views],
            ["chart", "fake_slider", "barline", "sv_chart", "sv_fake_slider",
             "sv_barline", "kiai_sound"],
        )
        self.assertEqual(
            [frame.view_type for frame in self.window._gimmick_views],
            ["chart", "chart", "chart", "sv", "sv", "sv", "sv"],
        )

    def test_every_layer_snaps_against_the_base_timing_not_the_document(self):
        base = self.window._gimmick_pairing.base_timing
        for frame in self.window._gimmick_views:
            view = getattr(frame, "chart_view", None) or frame.sv_view
            self.assertEqual([p.time for p in view.snap_points], [p.time for p in base])

    def test_a_gimmick_line_does_not_move_the_snap_grid(self):
        """The whole reason the snapshot exists: 60000 BPM lines land in the
        document constantly, and the grid must not follow them."""
        view = self.window._gimmick_views[0].chart_view
        before = view.snap_points[0].beat_length

        document = self.window._states[self.window._gimmick_pairing.target].document
        document.timing_points.append(gui.TimingPoint.uninherited_at(1000, 60000.0))
        view.refresh_notes(document)

        self.assertEqual(view.snap_points[0].beat_length, before)
        self.assertNotIn(60000.0, [p.bpm for p in view.snap_points])

    def test_the_fake_slider_layer_shows_only_fake_sliders(self):
        chart, fake_slider, barline = (
            frame.chart_view for frame in self.window._gimmick_views[:3]
        )
        # The fixture has 8 objects, exactly one of them a fake slider, so this
        # cannot pass by showing nothing.
        self.assertEqual(len(chart.notes), 7, "the normal chart layer shows regular notes")
        self.assertFalse(
            any(gui.MainWindow.is_fake_slider(n) for n in chart.notes),
            "a fake slider is layer 2's, not the normal chart's",
        )
        self.assertEqual(len(fake_slider.notes), 1)
        self.assertTrue(all(gui.MainWindow.is_fake_slider(n) for n in fake_slider.notes))
        self.assertEqual(barline.notes, [], "barlines are red lines, not hit objects")

    def test_a_positive_length_drumroll_is_not_a_fake_slider(self):
        document = self.window._states[self.window._gimmick_pairing.target].document
        real = next(n for n in document.hit_objects if n.is_slider and (n.length or 0) > 0)
        self.assertFalse(gui.MainWindow.is_fake_slider(real))

    def test_a_moved_layer_stays_moved_across_visits(self):
        """The layers are rebuilt on every visit; they used to come back in
        their fixed order, undoing any move."""
        self.window._move_view(self.window._gimmick_views[0], 1)
        self.window._open_gimmick_layers()
        self.assertEqual(
            [frame.gimmick_layer for frame in self.window._gimmick_views][:2],
            ["fake_slider", "chart"])

    def test_an_added_band_survives_the_song_list(self):
        target = self.window._gimmick_pairing.target
        self.window._add_editor_view(
            "gameplay", target, container=self.window.gimmick_views_layout)
        self.window._move_view(self.window._editor_views[-1], -1)
        order = [kind if kind == "view" else value
                 for kind, value in self.window._gimmick_band_order()]
        self.window._close_all_editor_views()
        self.window._open_gimmick_layers()
        self.assertEqual(
            [kind if kind == "view" else value
             for kind, value in self.window._gimmick_band_order()],
            order)

    def test_reopening_does_not_stack_a_second_set_of_layers(self):
        self.window._open_gimmick_layers()
        self.assertEqual(
            len(self.window._gimmick_views), len(gui.MainWindow.GIMMICK_LAYERS)
        )


class GimmickPlacementTests(_GimmickFixture, unittest.TestCase):
    """Placing through MainWindow: the command, the file state, the undo step."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document

    def _times(self):
        return [point.time for point in self.document.timing_points]

    def _at(self, time_ms):
        return [p for p in self.document.timing_points if p.time == time_ms]

    def _red(self, time_ms):
        return [p for p in self._at(time_ms) if p.uninherited]

    def test_a_barline_kat_writes_its_seven_lines(self):
        before = len([p for p in self.document.timing_points if p.uninherited])
        self.window._place_gimmick("barline", "kat", 10000)
        added = len([p for p in self.document.timing_points if p.uninherited]) - before
        self.assertEqual(added, 7)
        for offset in (-5, -3, -1, 0, 1, 3, 5):
            self.assertTrue(self._at(10000 + offset), f"no line at {10000 + offset}")
        self.assertAlmostEqual(self._red(10000)[0].bpm, 60000.0)

    def test_a_barline_don_writes_two(self):
        before = len([p for p in self.document.timing_points if p.uninherited])
        self.window._place_gimmick("barline", "don", 10000)
        self.assertEqual(
            len([p for p in self.document.timing_points if p.uninherited]) - before, 2
        )

    def test_a_fake_slider_kat_writes_two_lines_a_note_and_a_slider(self):
        points_before = len([p for p in self.document.timing_points if p.uninherited])
        notes_before = len(self.document.hit_objects)
        self.window._place_gimmick("fake_slider", "kat", 10000)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms

        self.assertEqual(
            len([p for p in self.document.timing_points if p.uninherited]) - points_before, 2
        )
        self.assertEqual(len(self.document.hit_objects) - notes_before, 2)
        note = next(n for n in self.document.hit_objects if n.time == 10000)
        self.assertTrue(note.is_circle, "the hittable note is on the snap")
        self.assertTrue(note.is_kat, "Kat places an actual kat, not a big don")
        slider = next(n for n in self.document.hit_objects if n.time == 10000 + offset)
        self.assertTrue(gui.MainWindow.is_fake_slider(slider))

    def test_the_whole_placement_is_one_undo_step(self):
        points_before = list(self.document.timing_points)
        notes_before = list(self.document.hit_objects)

        self.window._place_gimmick("fake_slider", "don", 10000)
        self.state.history.undo(self.state)

        self.assertEqual(self.document.timing_points, points_before)
        self.assertEqual(self.document.hit_objects, notes_before)

    def test_the_placement_appears_in_its_layer(self):
        self.window._place_gimmick("fake_slider", "don", 10000)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        fake_slider_layer = self.window._gimmick_views[1].chart_view
        # The layer shows fake sliders, so it is the slider at the offset that
        # appears here -- the hittable note on the snap belongs to layer 1.
        self.assertIn(10000 + offset, [note.time for note in fake_slider_layer.notes])

    def test_the_red_line_tool_takes_the_placement_offset(self):
        self.window.gimmick_configs["barline"] = gui.GimmickConfig(red_line_offset_ms=-1)
        self.window._place_gimmick("barline", "red_line", 10000)
        self.assertTrue(self._at(9999))
        self.assertFalse([p for p in self._at(10000) if p.uninherited and p.bpm != 60000.0])

    def test_placement_snaps_to_the_base_grid(self):
        self.window._place_gimmick("barline", "don", 10037)
        # 180 BPM from 0 at 1/4 -> 83.33ms snaps; the cluster centres on one.
        centre = next(p for p in self.document.timing_points if p.bpm == 60000.0)
        self.assertNotEqual(centre.time, 10037)


class SVRestoreOnPlacementTests(_GimmickFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document

    def test_an_sv_in_force_is_handed_back_after_the_cluster(self):
        # After the fixture's last uninherited point (6000), or that point
        # would cancel this SV before the placement ever sees it.
        self.document.timing_points.append(gui.TimingPoint.inherited_at(8000, 1.6))
        self.document.timing_points.sort(key=lambda p: p.time)

        self.window._place_gimmick("barline", "don", 10000)

        greens = [p for p in self.document.timing_points if p.inherited and p.time == 10001]
        self.assertEqual(len(greens), 1, "SV must be restored at the cluster's end")
        self.assertAlmostEqual(greens[0].sv_multiplier, 1.6)

    def test_the_restored_green_line_sorts_after_the_red_one(self):
        """osu! resolves a shared timestamp by file order, so the green line
        has to come second or the red one wins and SV stays at 1.0x."""
        # After the fixture's last uninherited point (6000), or that point
        # would cancel this SV before the placement ever sees it.
        self.document.timing_points.append(gui.TimingPoint.inherited_at(8000, 1.6))
        self.document.timing_points.sort(key=lambda p: p.time)

        self.window._place_gimmick("barline", "don", 10000)

        at_end = [p for p in self.document.timing_points if p.time == 10001]
        self.assertEqual([p.uninherited for p in at_end], [True, False])

    def test_a_structure_still_gets_its_handle_when_the_chart_has_no_sv(self):
        """A 1.0x line agrees with the timing either way, and it is the only
        thing the structure's SV layer can show, drag or generate from."""
        for point in list(self.document.timing_points):
            if point.inherited:
                self.document.timing_points.remove(point)

        self.window._place_gimmick("barline", "don", 10000)

        greens = [p for p in self.document.timing_points if p.inherited]
        self.assertEqual([p.time for p in greens], [10001.0])
        self.assertAlmostEqual(greens[0].sv_multiplier, 1.0)

    def test_the_plain_red_line_tool_invents_nothing(self):
        """One line is not a structure -- layer 6 already owns its millisecond,
        so a 1.0x green line stacked on it would be clutter, not a handle."""
        for point in list(self.document.timing_points):
            if point.inherited:
                self.document.timing_points.remove(point)

        self.window._place_gimmick("barline", "red_line", 10000)

        self.assertEqual([p for p in self.document.timing_points if p.inherited], [])


class GimmickToolTests(_GimmickFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)

    def test_each_layer_has_its_own_toolbox(self):
        self.assertEqual(
            list(self.window.gimmick_tool_rows),
            ["chart", "fake_slider", "barline", "sv_chart", "sv_fake_slider",
             "sv_barline", "kiai_sound"],
        )
        self.assertEqual(
            # Kiai left for the Kiai and Sound Volume layer, which is where a
            # section that every layer is read against belongs.
            list(self.window.gimmick_tool_buttons["fake_slider"]),
            ["select", "regular", "don", "kat", "shiny", "multi", "function", "convert"],
        )
        self.assertEqual(
            list(self.window.gimmick_tool_buttons["barline"]),
            ["select", "don", "kat", "red_line", "function", "convert"],
        )

    def test_new_combo_lets_go_when_another_tool_is_pressed(self):
        """It shares the row's exclusive group, so the only word that it has
        been released is another tool arriving. Latched, every note placed in
        the layer afterwards carried the flag."""
        chart = self.window._gimmick_views[0].chart_view
        self.window.gimmick_tool_buttons["chart"]["new_combo"].setChecked(True)
        self.assertTrue(chart.new_combo)

        self.window.gimmick_tool_buttons["chart"]["don"].setChecked(True)
        self.assertFalse(chart.new_combo)
        self.assertEqual(chart.tool, "don")

    def test_the_sv_layers_tools_reach_their_view(self):
        """An SV view has no new combo. Calling for one anyway raised before
        set_tool was ever reached, which left Green Line and Function in layers
        4, 5 and 6 doing nothing at all."""
        for layer_id in ("sv_chart", "sv_fake_slider", "sv_barline"):
            view = next(
                f.sv_view for f in self.window._gimmick_views
                if f.gimmick_layer == layer_id
            )
            self.window.gimmick_tool_buttons[layer_id]["function"].setChecked(True)
            self.assertEqual(view.tool, "function", layer_id)
            self.window.gimmick_tool_buttons[layer_id]["green_line"].setChecked(True)
            self.assertEqual(view.tool, "green_line", layer_id)

    def test_a_tool_applies_only_to_the_layer_that_owns_it(self):
        self.window._set_gimmick_tool("barline", "kat")
        by_layer = {
            f.gimmick_layer: f.chart_view.tool
            for f in self.window._gimmick_views if hasattr(f, "chart_view")
        }
        self.assertEqual(by_layer["barline"], "kat")
        self.assertEqual(by_layer["fake_slider"], "select", "other layers are untouched")

    def test_clicking_a_layer_shows_that_layer_s_toolbox(self):
        # A child of a hidden page reports isVisible() False whatever its own
        # flag says, so the page has to be up for this to mean anything.
        self.window._show_page(gui.PAGE_GIMMICK)
        for frame in self.window._gimmick_views:
            view = getattr(frame, "chart_view", None) or frame.sv_view
            self.window._editor_view_focus_changed(None, view)
            visible = [lid for lid, row in self.window.gimmick_tool_rows.items() if row.isVisible()]
            self.assertEqual(visible, [frame.gimmick_layer])

    def test_the_same_tool_means_different_things_per_layer(self):
        """Don in the fake-slider layer is a fake slider; in the barline layer
        it is a run of red lines. Same button, different structure."""
        document = self.window._states[self.window._gimmick_pairing.target].document
        reds = lambda: len([p for p in document.timing_points if p.uninherited])
        points_before = reds()
        notes_before = len(document.hit_objects)

        self.window._place_gimmick("fake_slider", "don", 10000)
        # A note on the snap plus the fake slider one offset later, and the two
        # lines that squash it.
        self.assertEqual(len(document.hit_objects), notes_before + 2)
        self.assertEqual(reds(), points_before + 2)

        self.window._place_gimmick("barline", "don", 10400)
        # One note, but two red lines: the bars are the structure here.
        self.assertEqual(len(document.hit_objects), notes_before + 3)
        self.assertEqual(reds(), points_before + 4)

    def test_a_click_in_a_gimmick_layer_places_through_the_layer(self):
        document = self.window._states[self.window._gimmick_pairing.target].document
        before = len([p for p in document.timing_points if p.uninherited])
        fake_slider_layer = self.window._gimmick_views[1].chart_view
        fake_slider_layer.note_place_requested.emit("kat", 10000, False, False)
        self.assertEqual(
            len([p for p in document.timing_points if p.uninherited]) - before, 2
        )

    def test_the_normal_chart_layer_places_no_gimmick_structure(self):
        """Layer 1 is a normal chart view -- it must not be hijacked into
        placing gimmicks."""
        document = self.window._states[self.window._gimmick_pairing.target].document
        before = list(document.timing_points)
        self.window._gimmick_views[0].chart_view.note_place_requested.emit(
            "don", 10000, False, False
        )
        self.assertEqual(document.timing_points, before)

    def test_config_cannot_ask_for_a_real_drumroll_length(self):
        """The box's ceiling is the config's own, so it cannot produce a value
        GimmickConfig would reject -- and it reaches the near-zero positive
        form as well as the canonical negative one."""
        dialog = gui.GimmickConfigDialog(
            gui.GimmickConfig(), "fake_slider", gui.GimmickConfig(), self.window,
        )
        self.assertEqual(dialog.length_spin.maximum(), gui.FAKE_SLIDER_MAX_LENGTH)
        self.assertLess(dialog.length_spin.minimum(), 0)
        dialog.deleteLater()


    def test_a_clash_marks_both_fields_and_restore_defaults_clears_it(self):
        """The config sheet puts the caution on the fields, not just under the
        form: both offsets turn amber when they meet, and Restore defaults
        brings back GimmickConfig's own values."""
        dialog = gui.GimmickConfigDialog(
            gui.GimmickConfig(), "fake_slider", gui.GimmickConfig(), self.window,
        )
        dialog.shiny_offset_spin.setValue(dialog.fake_offset_spin.value())
        self.assertTrue(dialog.shiny_offset_spin.property("warn"))
        self.assertTrue(dialog.fake_offset_spin.property("warn"))
        self.assertFalse(dialog.shiny_caution.isHidden())
        dialog.restore_button.click()
        self.assertEqual(dialog.shiny_offset_spin.value(), gui.GimmickConfig().shiny_offset_ms)
        self.assertFalse(dialog.shiny_offset_spin.property("warn"))
        self.assertTrue(dialog.shiny_caution.isHidden())
        dialog.deleteLater()

    def test_custom_bpm_is_a_pill_over_the_same_checkbox(self):
        dialog = gui.GimmickConfigDialog(
            gui.GimmickConfig(), "barline", gui.GimmickConfig(), self.window,
        )
        self.assertFalse(dialog.red_bpm_spin.isEnabled())
        custom = [b for b in dialog.red_bpm_widget.findChildren(gui.QPushButton) if b.isCheckable()][1]
        custom.click()
        self.assertTrue(dialog.red_bpm_check.isChecked())
        self.assertTrue(dialog.red_bpm_spin.isEnabled())
        self.assertIsNotNone(dialog.config().red_line_bpm)
        dialog.deleteLater()


class GimmickPageChromeTests(_GimmickFixture, unittest.TestCase):
    """The page's own timeline strip, layer sizing, playhead and keybinds."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)

    def _views(self):
        for frame in self.window._gimmick_views:
            yield getattr(frame, "chart_view", None) or frame.sv_view

    def test_every_layer_fits_its_band(self):
        # The band is the view's height; each frame adds its own chrome row on
        # top, so the screen budget is measured on the frames' size hints.
        heights = [frame.sizeHint().height() for frame in self.window._gimmick_views]
        self.assertEqual(len(heights), len(gui.MainWindow.GIMMICK_LAYERS))
        # The page's View size (2026-10-09), which a saved setting can move off 88.
        self.assertTrue(all(v.height() == self.window.gimmick_view_height_spin.value() for v in self._views()))
        # Per band rather than a total: the stack grows a row whenever a
        # layer is added, and what must not grow is any one band's share of
        # the screen -- 150px keeps the whole stack plus the strip and tool
        # row inside a normal window.
        self.assertLessEqual(sum(heights), 150 * len(heights))

    def test_the_page_has_its_own_timeline_bar(self):
        self.assertIsInstance(self.window.gimmick_timing_bar, gui.TimingOverviewBar)
        self.assertIsNot(self.window.gimmick_timing_bar, self.window.timing_bar)
        self.assertIn(self.window.gimmick_timing_bar, self.window._timing_bars)
        self.assertGreater(self.window.gimmick_timing_bar.duration_ms, 1)

    def test_the_layers_follow_the_playhead(self):
        """Registered for the broadcast, or the layers sit still while the
        song plays."""
        for view in self._views():
            self.assertTrue(
                view in self.window._chart_views or view in self.window._sv_views,
                "layer is not on the playhead broadcast",
            )
        self.window.seek_audio(7000)
        for view in self._views():
            view.set_time(7000, force=True)
            self.assertEqual(view.current_time, 7000)

    def test_rebuilding_layers_unregisters_the_old_ones(self):
        before = len(self.window._chart_views) + len(self.window._sv_views)
        self.window._open_gimmick_layers()
        after = len(self.window._chart_views) + len(self.window._sv_views)
        self.assertEqual(before, after, "stale views left on the broadcast list")

    def test_tool_digits_follow_the_focused_layer(self):
        """The same digit means different things in different layers, exactly
        as the buttons do."""
        self.window._show_page(gui.PAGE_GIMMICK)
        barline = next(f for f in self.window._gimmick_views if f.gimmick_layer == "barline")
        self.window._editor_view_focus_changed(None, barline.chart_view)
        self.window._tool_state.activate_tool_digit("4")
        self.assertEqual(barline.chart_view.tool, "red_line")

        fake = next(f for f in self.window._gimmick_views if f.gimmick_layer == "fake_slider")
        self.window._editor_view_focus_changed(None, fake.chart_view)
        self.window._tool_state.activate_tool_digit("2")
        self.assertEqual(fake.chart_view.tool, "regular")

    def test_tool_digits_still_belong_to_the_editor_page_elsewhere(self):
        self.window._show_page(gui.PAGE_GIMMICK)
        barline = next(f for f in self.window._gimmick_views if f.gimmick_layer == "barline")
        self.window._editor_view_focus_changed(None, barline.chart_view)
        self.window._show_page(gui.PAGE_EDITOR)
        self.window._tool_state.activate_tool_digit("3")
        self.assertEqual(barline.chart_view.tool, "select", "editor page must not drive a layer")

    def test_zooming_one_layer_zooms_them_all(self):
        views = list(self._views())
        views[0].zoom_changed.emit(1234.0)
        self.assertEqual([v.window_ms for v in views], [1234.0] * len(views))

    def test_notes_are_sized_from_the_view_so_they_stay_centred(self):
        """A fixed 42px finisher is taller than a 132px band once the frame
        chrome is taken out, so it spilled past the edges."""
        view = self.window._gimmick_views[0].chart_view
        view.resize(800, 90)
        view.grab()  # paints; must not raise
        self.assertLessEqual(
            min(31.0, view.height() * 0.22) * common.TAIKO_STRONG_SCALE * 2, view.height(),
        )

    def test_the_snap_combo_drives_every_layer(self):
        combo = self.window.gimmick_snap_combo
        combo.setCurrentIndex(combo.findData(8))
        for view in self._views():
            self.assertEqual(view.snap_divisor, 8)


class GimmickPageTests(unittest.TestCase):
    def test_the_page_exists_between_editor_and_fancy_arranger(self):
        self.assertEqual(
            (gui.PAGE_LIBRARY, gui.PAGE_EDITOR, gui.PAGE_GIMMICK, gui.PAGE_FANCY),
            (0, 1, 2, 3),
        )


class GimmickSVLayerTests(_GimmickFixture, unittest.TestCase):
    """Which green lines each of the three SV layers owns.

    The fixture has green lines at 2000 and 4000, hit objects at 1000-5000 plus
    a fake slider at 54692, and red lines at 0 and 6000 -- so only 2000 lands on
    a real object, and neither green line touches a fake slider or a red line.

    4000 is therefore the orphan: SV belonging to nothing at all. It goes to
    the barline layer, which is the one that holds the chart's own timing --
    owned by nobody it was drawn in no SV layer, while the Kiai and Sound
    Volume layer listed it.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document
        self.layers = {
            frame.gimmick_layer: frame.sv_view
            for frame in self.window._gimmick_views if hasattr(frame, "sv_view")
        }

    def _shown(self, layer_id):
        return sorted(round(p.time) for p in self.layers[layer_id]._visible_points)

    def test_the_three_layers_do_not_share_each_other_s_sv(self):
        self.assertEqual(self._shown("sv_chart"), [2000], "SV on a real hit object")
        self.assertEqual(self._shown("sv_fake_slider"), [])
        self.assertEqual(self._shown("sv_barline"), [4000], "the orphan")

    def test_a_fake_sliders_sv_belongs_to_the_fake_slider_layer_alone(self):
        """Layer 4 is the *chart's* SV: real notes, plus a shiny's own line.
        A plain fake slider's line used to land there too, which is how its SV
        leaked into the normal-chart layer on a millisecond carrying no note at
        all -- now it is layer 5's, on the slider's own millisecond."""
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        for at in (54692, 54692 + offset):
            self.document.timing_points.append(gui.TimingPoint.inherited_at(at, 1.8))
        self.window._refresh_gimmick_views()

        self.assertEqual(self._shown("sv_chart"), [2000], "real notes only")
        self.assertIn(54692, self._shown("sv_fake_slider"))
        self.assertNotIn(54692, self._shown("sv_barline"))

    def test_sv_on_a_red_line_lands_in_the_barline_layer_whatever_the_bpm(self):
        for time_ms in (0, 6000):
            self.document.timing_points.append(gui.TimingPoint.inherited_at(time_ms, 0.5))
        self.window._refresh_gimmick_views()

        # 0 is 500ms/beat, 6000 is 400ms/beat -- the BPM is not a criterion.
        # 4000 is the orphan green line, which this layer also holds.
        self.assertEqual(self._shown("sv_barline"), [0, 4000, 6000])

    def test_a_gimmick_layer_draws_a_stacked_line_green_not_yellow(self):
        """Only inherited points reach these layers, so a red line sharing the
        millisecond cannot recolour the green one the way it does in the plain
        SV editor."""
        self.document.timing_points.append(gui.TimingPoint.inherited_at(6000, 0.5))
        self.window._refresh_gimmick_views()

        self.assertEqual(self.layers["sv_barline"].line_kinds()[6000], "green")

        plain = gui.SVEditorView()
        plain.set_timing_points(self.document.timing_points)
        self.assertEqual(plain.line_kinds()[6000], "yellow", "unchanged elsewhere")

    def test_a_hidden_point_cannot_be_grabbed(self):
        layer = self.layers["sv_barline"]
        layer.current_time = 2000.0
        self.assertIsNone(layer._nearest_inherited(layer.width() / 2))


class _StubTimingLineDialog:
    """Stands in for TimingLineDialog, which is modal: the window's own handler
    opens one the moment a double click reaches it."""

    accepted = False
    values: dict = {}

    def __init__(self, point, parent=None, partner=None):
        self.point = point

    def exec(self):
        return 1 if type(self).accepted else 0

    def changes(self, point):
        return dict(type(self).values)

    def partner_changes(self, point):
        return {}

    def new_partner_sv(self):
        return None

    def deleteLater(self):
        pass


class GimmickRedLineTests(_GimmickFixture, unittest.TestCase):
    """Red lines: drawn in the chart layers, owned by the barline layer.

    The fixture's red lines are at 0 and 6000, neither on a hit object, and its
    fake slider is at 54692.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document
        self.chart, self.fake, self.barline = (
            frame.chart_view for frame in self.window._gimmick_views[:3]
        )

    def _lines(self, view, time_ms):
        view.current_time = float(time_ms)
        return {round(p.time): owned for p, _x, owned in view._visible_timing_points()}

    def test_the_barline_layer_owns_a_red_line_that_is_on_no_object(self):
        self.assertEqual(self._lines(self.barline, 0), {0: True})
        self.assertEqual(self._lines(self.barline, 6000), {6000: True})

    def test_the_middle_line_of_a_barline_note_can_be_deleted(self):
        """Its 60000 BPM line lands on the note the same placement writes.
        While ownership meant "no hit object here", that made the one line the
        layer exists to edit the one line it could not touch -- visible, not
        right-clickable, removable only by undo."""
        # Mirrored, so the line under test has a neighbour on either side.
        self.window.gimmick_configs["barline"] = dataclasses.replace(
            self.window._gimmick_config("barline"), mirror_don_lines=True)
        self.window._place_gimmick("barline", "don", 10000)
        self.barline.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        # Zoomed to where the structure is actually worked on: its three lines
        # are one millisecond apart, and _visible_timing_points draws at most
        # one per pixel column, so at chart zoom they are deliberately one line.
        self.barline.window_ms = 40.0
        self.barline.current_time = 10000.0

        self.assertEqual(self._lines(self.barline, 10000), {9999: True, 10000: True, 10001: True})
        hit = self.barline._timing_point_near_x(self.barline.x_for_time(10000))
        self.assertIsNotNone(hit)
        self.assertEqual(round(hit.time), 10000, "the middle line, not a neighbour")

        self.barline.mousePressEvent(QMouseEvent(
            QEvent.MouseButtonPress, QPointF(self.barline.x_for_time(10000), 40.0),
            Qt.RightButton, Qt.RightButton, Qt.NoModifier,
        ))
        self.assertEqual([p for p in self.document.timing_points if p.time == 10000], [])

    def test_a_fake_sliders_own_line_stays_out_of_the_barline_layer(self):
        """One line now, not two: a plain fake slider stopped writing a 60000
        BPM squash (there is no note under it to hide), so all it has is its
        own line on its own millisecond, one offset after the snap."""
        self.window._place_gimmick("fake_slider", "regular", 20000)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        at = 20000 + offset
        owned = self._lines(self.barline, at)
        self.assertIn(at, [round(p.time) for p in self.document.timing_points])
        self.assertNotEqual(owned.get(at), True, at)

    def test_a_red_line_on_an_object_is_shown_but_not_owned(self):
        """The line that squashes a fake slider belongs to the fake slider, so
        the barline layer draws it for context rather than as its own."""
        self.document.timing_points.append(gui.TimingPoint.uninherited_at(54692, 60000.0))
        self.window._refresh_gimmick_views()
        self.assertEqual(self._lines(self.barline, 54692), {54692: False})

    def test_the_object_layers_show_red_lines_and_only_layer_2_edits_them(self):
        """Layer 1 shows every red line for reference and edits none. Layer 2
        edits its own: a drag there goes through `_expand_move`, which takes
        the slider with the line, and without it a red line that landed on a
        fake slider could not be moved again at all."""
        self.assertTrue(self.chart.show_timing_lines)
        self.assertFalse(self.chart.timing_edit_enabled)
        self.assertTrue(self.fake.show_timing_lines)
        self.assertTrue(self.fake.timing_edit_enabled)
        # The normal chart shows every red line; the fake slider layer shows
        # only the ones on its own objects, so the fixture's line at 0 -- which
        # is on no object at all -- reaches one and not the other.
        self.assertEqual(self._lines(self.chart, 0), {0: True})
        self.assertEqual(self._lines(self.fake, 0), {})
        self.assertTrue(self.barline.timing_edit_enabled, "layer 3 is where they are edited")

    def test_double_clicking_a_red_line_asks_for_its_bpm(self):
        seen = []
        _StubTimingLineDialog.accepted = False
        self.enterContext(patch.object(gui, "TimingLineDialog", _StubTimingLineDialog))
        self.barline.timing_line_edit_requested.connect(seen.append)
        self.barline.current_time = 6000.0
        self.barline.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        point = next(p for p in self.document.timing_points if p.time == 6000)

        self.barline.mouseDoubleClickEvent(QMouseEvent(
            QEvent.MouseButtonDblClick,
            QPointF(self.barline.x_for_time(6000), 40.0),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        ))
        self.assertEqual(seen, [point.uid])

    def test_a_double_click_is_ignored_where_editing_is_off(self):
        seen = []
        self.chart.timing_line_edit_requested.connect(seen.append)
        self.chart.current_time = 6000.0
        self.chart.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        self.chart.mouseDoubleClickEvent(QMouseEvent(
            QEvent.MouseButtonDblClick,
            QPointF(self.chart.x_for_time(6000), 40.0),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        ))
        self.assertEqual(seen, [])

    def test_the_bpm_and_time_edit_is_applied_as_one_undo_step(self):
        point = next(p for p in self.document.timing_points if p.time == 6000)
        _StubTimingLineDialog.accepted = True
        _StubTimingLineDialog.values = {
            "beat_length": (point.beat_length, 300.0), "time": (6000.0, 6500.0),
        }
        with patch.object(gui, "TimingLineDialog", _StubTimingLineDialog):
            self.window._edit_timing_line(self.state.source_path, point.uid)
        self.assertAlmostEqual(point.bpm, 200.0)
        self.assertEqual(point.time, 6500.0)

        self.state.history.undo(self.state)
        self.assertAlmostEqual(point.bpm, 150.0, msg="6000 is 400ms/beat in the fixture")
        self.assertEqual(point.time, 6000.0, "value and time undo together")

    def test_a_cancelled_dialog_changes_nothing(self):
        point = next(p for p in self.document.timing_points if p.time == 6000)
        before = point.beat_length
        _StubTimingLineDialog.accepted = False
        with patch.object(gui, "TimingLineDialog", _StubTimingLineDialog):
            self.window._edit_timing_line(self.state.source_path, point.uid)
        self.assertEqual(point.beat_length, before)


class GimmickStructureTests(_GimmickFixture, unittest.TestCase):
    """What each tool writes, per layer, with its own config."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document

    def _at(self, time_ms):
        return [n for n in self.document.hit_objects if n.time == time_ms]

    def test_the_two_layers_keep_separate_configs(self):
        self.window.gimmick_configs["barline"] = gui.GimmickConfig(spacing_ms=7)
        self.assertEqual(self.window._gimmick_config("barline").spacing_ms, 7)
        self.assertEqual(
            self.window._gimmick_config("fake_slider").spacing_ms,
            gui.GimmickConfig().spacing_ms,
            "the fake slider layer must not inherit the barline's spacing",
        )

    def test_a_plain_fake_slider_writes_only_the_fake_slider(self):
        """On its restore line, one offset after the snap -- every object this
        layer writes sits there, which is what tells a fake slider from a
        shiny note."""
        self.window._place_gimmick("fake_slider", "regular", 10000)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        placed = self._at(10000 + offset)
        self.assertEqual(len(placed), 1)
        self.assertTrue(gui.MainWindow.is_fake_slider(placed[0]))

    def test_a_fake_slider_don_also_writes_a_real_note_on_the_snap(self):
        """The note is what the player hits; the fake slider is the decoration
        one offset later, and cannot share the note's millisecond."""
        self.window._place_gimmick("fake_slider", "don", 10000)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms

        note = self._at(10000)
        self.assertEqual(len(note), 1)
        self.assertTrue(note[0].is_circle, "a real, hittable note")
        self.assertFalse(note[0].is_kat, "Don places an actual don")

        slider = self._at(10000 + offset)
        self.assertEqual(len(slider), 1)
        self.assertTrue(gui.MainWindow.is_fake_slider(slider[0]))

    def test_a_fake_slider_kat_places_a_real_kat(self):
        """The tools' small/big distinction is the hover preview's; what lands
        in the file is the don or kat the button names."""
        self.window._place_gimmick("fake_slider", "kat", 10000)
        self.assertEqual(self._at(10000)[0].note_kind, "kat")

    def test_a_barline_kat_places_a_real_kat_too(self):
        self.window._place_gimmick("barline", "kat", 10000)
        self.assertEqual(self._at(10000)[0].note_kind, "kat")

    def test_the_fake_slider_offset_moves_both_the_slider_and_its_restore(self):
        self.window.gimmick_configs["fake_slider"] = gui.GimmickConfig(fake_slider_offset_ms=3)
        self.window._place_gimmick("fake_slider", "don", 10000)

        self.assertTrue(self._at(10003), "the slider follows the offset")
        reds = [p.time for p in self.document.timing_points if p.uninherited]
        self.assertIn(10000, reds, "the gimmick line sits on the note")
        self.assertIn(10003, reds, "and the restore where the slider goes in")

    def test_a_fake_slider_don_carries_its_gimmick_sv_and_nothing_else(self):
        """`fake_slider_sv` on the gimmick line stays -- it is the gimmick's own
        number, and it takes the squashed note the rest of the way off screen.

        The second green line on the restore does not. That one was
        `sv_restore_point` handing back the chart's SV, and this layer no longer
        carries the chart's SV in any form. It was also what layer 5 showed of
        the slider, so this structure now arrives without a handle there -- an
        accepted cost of the change, pinned here so it is not mistaken for a
        regression later.
        """
        self.window.gimmick_configs["fake_slider"] = gui.GimmickConfig(fake_slider_sv=12.0)
        self.window._place_gimmick("fake_slider", "don", 10000)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms

        greens = {
            p.time: p for p in self.document.timing_points
            if p.inherited and 9990 <= p.time <= 10010
        }
        self.assertAlmostEqual(greens[10000].sv_multiplier, 12.0)
        self.assertNotIn(10000 + offset, greens, "no chart SV handed back on the restore")
        self.assertEqual(list(greens), [10000.0], "the gimmick's own SV is the only green line")

    def test_a_fake_slider_don_squashes_its_own_note_too(self):
        """The 60000 BPM line goes on the note, not on the slider: the note the
        player hits is drawn away to nothing and the fake slider is what is
        left to look at."""
        self.window._place_gimmick("fake_slider", "don", 10000)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms

        gimmick = [
            p for p in self.document.timing_points
            if p.bpm == 60000.0 and 9990 <= p.time <= 10010
        ]
        self.assertEqual([p.time for p in gimmick], [10000])
        self.assertTrue(self._at(10000)[0].is_circle, "the note shares that millisecond")
        self.assertTrue(gui.MainWindow.is_fake_slider(self._at(10000 + offset)[0]))

    def test_a_barline_note_places_a_real_note_by_default(self):
        self.window._place_gimmick("barline", "don", 10000)
        self.assertEqual(len(self._at(10000)), 1)

    def test_the_barline_note_toggle_leaves_only_red_lines(self):
        self.window.gimmick_configs["barline"] = gui.GimmickConfig(place_notes=False)
        before = len(self.document.hit_objects)
        self.window._place_gimmick("barline", "kat", 10000)

        self.assertEqual(len(self.document.hit_objects), before, "no hit object")
        self.assertTrue([p for p in self.document.timing_points if p.time == 10000])

    def test_the_red_line_tool_writes_one_line_and_no_note(self):
        before = len(self.document.hit_objects)
        self.window._place_gimmick("barline", "red_line", 10000)
        self.assertEqual(len(self.document.hit_objects), before)
        self.assertEqual(len([p for p in self.document.timing_points if p.time == 10000]), 1)

    def test_the_spacing_caution_fires_only_when_the_two_agree(self):
        self.assertFalse(
            gimmick_session.spacing_collides(gui.GimmickConfig(), gui.GimmickConfig()),
            "the defaults are deliberately different: 1ms spacing, +2ms offset",
        )
        self.assertTrue(
            gimmick_session.spacing_collides(
                gui.GimmickConfig(spacing_ms=1), gui.GimmickConfig(fake_slider_offset_ms=1)
            )
        )


class GimmickInteractionTests(_GimmickFixture, unittest.TestCase):
    """Undo/redo, right-click delete and the per-layer hover ghosts."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.window._show_page(gui.PAGE_GIMMICK)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document

    def test_undo_and_redo_act_on_the_paired_difficulty(self):
        before = len(self.document.timing_points)
        self.window._place_gimmick("barline", "don", 10000)
        self.assertGreater(len(self.document.timing_points), before)

        self.window.undo()
        self.assertEqual(len(self.document.timing_points), before)
        self.window.redo()
        self.assertGreater(len(self.document.timing_points), before)

    def test_undo_reaches_the_layers_not_just_the_document(self):
        self.window._place_gimmick("fake_slider", "don", 10000)
        layer = self.window._gimmick_views[1].chart_view
        placed = [n for n in layer.notes if 10000 <= n.time <= 10010]
        self.assertTrue(placed)

        self.window.undo()
        self.assertFalse(
            [n for n in layer.notes if 10000 <= n.time <= 10010],
            "the layer must repaint what undo reverted",
        )

    def test_right_clicking_a_fake_slider_removes_it(self):
        self.window._place_gimmick("fake_slider", "regular", 10000)
        at = 10000 + self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        layer = self.window._gimmick_views[1].chart_view
        layer.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        layer.current_time = float(at)
        victim = next(n for n in self.document.hit_objects if n.time == at)

        layer.mousePressEvent(QMouseEvent(
            QEvent.MouseButtonPress, QPointF(layer.x_for_time(at), 40.0),
            Qt.RightButton, Qt.RightButton, Qt.NoModifier,
        ))
        self.assertNotIn(victim, self.document.hit_objects)
        self.assertNotIn(at, [n.time for n in layer.notes])

    def _right_click(self, view, time_ms):
        view.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        view.current_time = float(time_ms)
        view.mousePressEvent(QMouseEvent(
            QEvent.MouseButtonPress, QPointF(view.x_for_time(float(time_ms)), 40.0),
            Qt.RightButton, Qt.RightButton, Qt.NoModifier,
        ))

    def _red_times(self, low, high):
        return sorted(
            round(p.time) for p in self.document.timing_points
            if p.uninherited and low <= p.time <= high
        )

    def test_right_clicking_a_fake_slider_don_removes_the_whole_structure(self):
        """The lines and the drawn object go; the hittable note stays.

        A gimmick is drawn *around* a note that was already in the chart, and a
        delete cannot tell that note from one the structure had to write for
        itself -- so it keeps both rather than deleting a beat of the map. This
        used to assert "the note and the slider both go", which is the safe
        half of that trade in the wrong direction.
        """
        self.window._place_gimmick("fake_slider", "don", 10000)
        self._right_click(self.window._gimmick_views[1].chart_view, 10001)

        self.assertEqual(self._red_times(9990, 10010), [])
        self.assertEqual(
            [n for n in self.document.hit_objects
             if 9990 <= n.time <= 10010 and gui.MainWindow.is_fake_slider(n)], [],
            "the drawn object is the gimmick's own",
        )
        self.assertTrue(
            [n for n in self.document.hit_objects if n.time == 10000 and n.is_circle],
            "the hittable note is not the gimmick's to delete",
        )

    def test_a_layer_with_its_own_gimmick_bpm_is_still_one_structure(self):
        """The two object layers hold independent configs. Walking the
        structure by the barline layer's BPM alone left a fake slider's real
        note standing after the rest of it had gone."""
        self.window.gimmick_configs["fake_slider"] = gui.GimmickConfig(gimmick_bpm=30000.0)
        self.window._place_gimmick("fake_slider", "don", 10000)
        self.window._refresh_gimmick_views()

        self._right_click(self.window._gimmick_views[1].chart_view, 10001)

        self.assertEqual(self._red_times(9990, 10010), [])
        self.assertEqual(
            [n for n in self.document.hit_objects
             if 9990 <= n.time <= 10010 and gui.MainWindow.is_fake_slider(n)], [],
            "the whole structure goes whichever BPM its layer holds",
        )

    def test_right_clicking_a_barline_note_removes_the_whole_structure(self):
        """All of it is bars, so all of it goes -- except the note underneath,
        which the bars were drawn around. See the fake slider case above."""
        self.window._place_gimmick("barline", "kat", 10000)
        # On one of its restore bars, not its centre: they are the same pixel.
        self._right_click(self.window._gimmick_views[2].chart_view, 9995)

        self.assertEqual(self._red_times(9990, 10010), [])
        self.assertTrue([n for n in self.document.hit_objects if n.time == 10000])

    def test_the_structures_sv_survives_its_deletion(self):
        """Only the red lines go. A green line describes the chart's scroll
        speed at a millisecond rather than belonging to what was standing on
        it, and it is cheaper to leave than to rebuild a sweep around."""
        self.window._place_gimmick("barline", "don", 10000)
        self.document.timing_points.append(gui.TimingPoint.inherited_at(10002, 1.4))
        self.window._refresh_gimmick_views()

        self._right_click(self.window._gimmick_views[2].chart_view, 10000)

        self.assertEqual(self._red_times(9990, 10010), [])
        self.assertTrue([p for p in self.document.timing_points if p.time == 10002])

    def test_delete_removes_the_structure_a_click_selected(self):
        """Click to select, Delete to remove -- without the click selecting,
        Delete had nothing to act on unless you knew to rubber-band first."""
        self.window._place_gimmick("fake_slider", "don", 10000)
        layer = self.window._gimmick_views[1].chart_view
        layer.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        layer.current_time = 10000.0
        layer.set_tool("select")
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms

        x = layer.x_for_time(10000.0 + offset)
        QApplication.sendEvent(layer, QMouseEvent(
            QEvent.MouseButtonPress, QPointF(x, 40.0), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        QApplication.sendEvent(layer, QMouseEvent(
            QEvent.MouseButtonRelease, QPointF(x, 40.0), Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
        self.assertTrue(layer.selected, "a click selects what it landed on")

        layer.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Backspace, Qt.NoModifier))

        self.assertEqual(self._red_times(9990, 10010), [])
        # The fake slider *was* what the click selected, so it goes; the note
        # beside it was only pulled in by the structure walk, so it stays.
        self.assertEqual(
            [n for n in self.document.hit_objects
             if 9990 <= n.time <= 10010 and gui.MainWindow.is_fake_slider(n)], [],
        )
        self.assertTrue([n for n in self.document.hit_objects if n.time == 10000])

    def test_the_deletion_is_one_undo_step(self):
        self.window._place_gimmick("barline", "kat", 10000)
        before = self._red_times(9990, 10010)
        self._right_click(self.window._gimmick_views[2].chart_view, 10000)

        self.window.undo()
        self.assertEqual(self._red_times(9990, 10010), before)

    def test_the_fake_slider_tool_previews_what_it_would_place(self):
        """The one tool whose whole output is a drawn object was the one tool
        showing nothing under the cursor."""
        view = self.window._gimmick_views[1].chart_view
        view.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        view.set_tool("regular")

        def rendered():
            image = QImage(view.size(), QImage.Format_ARGB32)
            image.fill(0)
            view.render(image)
            return image

        view._hover_time = None
        cold = rendered()
        view._hover_time = view.current_time
        self.assertNotEqual(rendered(), cold, "hovering draws a ghost")

    def test_each_layer_previews_the_shape_it_writes(self):
        chart, fake, barline = (f.chart_view for f in self.window._gimmick_views[:3])
        self.assertEqual(chart.ghost_style, "note")
        self.assertEqual(fake.ghost_style, "fake_slider")
        self.assertEqual(barline.ghost_style, "barline")

    def test_a_layer_ghost_paints_without_raising(self):
        for frame in self.window._gimmick_views[:3]:
            view = frame.chart_view
            view.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
            for tool in ("don", "kat"):
                view.set_tool(tool)
                view._hover_time = view.current_time
                pixmap = QPixmap(view.size())
                view.render(pixmap)
                self.assertFalse(pixmap.isNull(), f"{frame.gimmick_layer}/{tool}")

    def test_the_fake_slider_layer_shows_only_its_own_red_lines(self):
        """A red line belonging to a barline gimmick is not this layer's."""
        self.window._place_gimmick("fake_slider", "regular", 10000)
        self.window._place_gimmick("barline", "red_line", 20000)
        layer = self.window._gimmick_views[1].chart_view

        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        layer.current_time = float(10000 + offset)
        self.assertEqual(
            [round(p.time) for p, _x, _o in layer._visible_timing_points()], [10000 + offset]
        )
        layer.current_time = 20000.0
        self.assertEqual([round(p.time) for p, _x, _o in layer._visible_timing_points()], [])

    def test_the_two_object_layers_name_their_own_tools(self):
        captions = {}
        for layer_id, row in self.window.gimmick_tool_rows.items():
            label = row.layout().itemAt(0).widget()
            captions[layer_id] = label.text()
        self.assertEqual(captions["fake_slider"], "Fake Slider Note Tool:")
        self.assertEqual(captions["barline"], "Barline Note Tool:")


class _StubBarlineFunctionDialog:
    """Stands in for BarlineFunctionDialog, which is modal."""

    accepted = False
    result: list = []
    seen_range: tuple = ()

    def __init__(
        self, start_ms, end_ms, parent=None, base_timing=None, snap_divisor=4, note_times=None,
        current_sv=1.0, meter=4,
    ):
        type(self).seen_range = (start_ms, end_ms)
        type(self).seen_meter = meter

    class meter_spin:
        """The Config's default; a test that cares sets `meter`."""
        meter = 4

        @classmethod
        def value(cls):
            return cls.meter

    def exec(self):
        return 1 if type(self).accepted else 0

    def times(self):
        return list(type(self).result)

    def bpms(self, times):
        """No ramp: the base timing's own BPM, which is the dialog's default."""
        return [180.0] * len(times)

    def sv_multiplier(self):
        """None is the dialog's default: keep whatever speed is already in
        force, restated on a green line beside each generated red one."""
        return None

    def deleteLater(self):
        pass


class GimmickFunctionTests(_GimmickFixture, unittest.TestCase):
    """The function tools: SV per layer, and the barline layer's red line run."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document
        self.barline = self.window._gimmick_views[2].chart_view
        self.sv_layers = {
            f.gimmick_layer: f.sv_view
            for f in self.window._gimmick_views if hasattr(f, "sv_view")
        }

    # -- SV layers ---------------------------------------------------------

    def test_generate_lands_only_on_the_layer_s_own_objects(self):
        """The fixture has regular objects at 1000-5000 and a fake slider at
        54692, so the three layers cannot agree by accident."""
        self.document.timing_points.append(gui.TimingPoint.uninherited_at(2500, 180.0))
        params = {"placement": "notes"}
        for layer_id, expected in (
            ("sv_chart", [1000.0, 1500.0, 2000.0, 2500.0, 3000.0, 3500.0]),
            ("sv_fake_slider", []),
            ("sv_barline", [0.0, 2500.0]),
        ):
            times = self.window._sv_generation_times(self.state, 0, 4000, params, layer_id)
            self.assertEqual(times, expected, layer_id)

    def test_generate_without_a_layer_still_uses_every_object(self):
        times = self.window._sv_generation_times(self.state, 0, 4000, {"placement": "notes"})
        self.assertEqual(times, [1000.0, 1500.0, 2000.0, 2500.0, 3000.0, 3500.0])

    def test_a_generated_sweep_appears_in_the_layer_that_made_it(self):
        self.window._generate_sv(
            self.state.source_path, 1000, 3000,
            {
                "placement": "notes", "function": "linear",
                "initial_rate": 1.0, "final_rate": 2.0, "position_offset": 0,
                "omit_barline": False, "relative_to_final_bpm": False, "snap_divisor": 4,
            },
            layer_id="sv_chart",
        )
        shown = {round(p.time) for p in self.sv_layers["sv_chart"]._visible_points}
        self.assertTrue({1000, 1500, 2000, 2500, 3000} <= shown)
        self.assertEqual(self.sv_layers["sv_fake_slider"]._visible_points, [])

    def test_the_sv_layers_can_be_edited_at_all(self):
        """They were built without a single editing connection, so every tool
        in them reached nothing."""
        layer = self.sv_layers["sv_barline"]
        before = len(self.document.timing_points)
        layer.point_add_requested.emit(6000.0, 1.5)
        self.assertEqual(len(self.document.timing_points), before + 1, "green line placed")

        added = next(p for p in self.document.timing_points if p.inherited and p.time == 6000)
        layer.point_delete_requested.emit(added.uid)
        self.assertEqual(len(self.document.timing_points), before, "and deleted again")

    # -- barline function --------------------------------------------------

    def test_the_barline_layer_has_a_function_tool(self):
        self.assertIn("function", self.window.gimmick_tool_buttons["barline"])

    def test_a_dragged_range_generates_red_lines_every_n_ms(self):
        _StubBarlineFunctionDialog.accepted = True
        _StubBarlineFunctionDialog.result = [10000, 10001, 10002, 10003]
        before = len(self.document.timing_points)
        with patch.object(gui, "BarlineFunctionDialog", _StubBarlineFunctionDialog):
            self.window._generate_barlines(self.state.source_path, 10000, 10003)

        # Two points per millisecond now: the red line, and a green one
        # restating the SV it would otherwise have reset to 1.0x.
        self.assertEqual(len(self.document.timing_points), before + 8)
        for at in (10000, 10001, 10002, 10003):
            written = [p for p in self.document.timing_points if p.time == at]
            self.assertEqual([p.uninherited for p in written], [True, False], at)

    def test_the_generated_run_carries_the_dialogs_meter(self):
        """osu! reads meter from the red line in force, so a run written at a
        fixed 4/4 re-bars the chart after it whatever the mapper typed."""
        _StubBarlineFunctionDialog.accepted = True
        _StubBarlineFunctionDialog.result = [10000, 10001]
        _StubBarlineFunctionDialog.meter_spin.meter = 999
        try:
            with patch.object(gui, "BarlineFunctionDialog", _StubBarlineFunctionDialog):
                self.window._generate_barlines(self.state.source_path, 10000, 10001)
        finally:
            _StubBarlineFunctionDialog.meter_spin.meter = 4
        reds = [
            p for p in self.document.timing_points
            if p.uninherited and p.time in (10000, 10001)
        ]
        self.assertEqual([p.meter for p in reds], [999, 999])

    def test_the_dialog_opens_on_the_red_line_configs_meter(self):
        self.window.gimmick_configs["barline"] = gui.GimmickConfig(red_line_meter=7)
        with patch.object(gui, "BarlineFunctionDialog", _StubBarlineFunctionDialog):
            self.window._generate_barlines(self.state.source_path, 10000, 10001)
        self.assertEqual(_StubBarlineFunctionDialog.seen_meter, 7)

    def test_the_generated_run_is_one_undo_step(self):
        _StubBarlineFunctionDialog.accepted = True
        _StubBarlineFunctionDialog.result = list(range(10000, 10050))
        before = len(self.document.timing_points)
        with patch.object(gui, "BarlineFunctionDialog", _StubBarlineFunctionDialog):
            self.window._generate_barlines(self.state.source_path, 10000, 10049)

        self.state.history.undo(self.state)
        self.assertEqual(len(self.document.timing_points), before)

    def test_the_range_start_is_snapped_before_the_dialog_sees_it(self):
        _StubBarlineFunctionDialog.accepted = False
        with patch.object(gui, "BarlineFunctionDialog", _StubBarlineFunctionDialog):
            self.window._generate_barlines(self.state.source_path, 10037, 11000)
        start, _end = _StubBarlineFunctionDialog.seen_range
        self.assertNotEqual(start, 10037, "the drag start snaps like a placement does")

    def test_generation_skips_a_millisecond_that_already_has_a_red_line(self):
        _StubBarlineFunctionDialog.accepted = True
        _StubBarlineFunctionDialog.result = [0, 1, 2]  # 0 is the fixture's own
        before = len(self.document.timing_points)
        with patch.object(gui, "BarlineFunctionDialog", _StubBarlineFunctionDialog):
            self.window._generate_barlines(self.state.source_path, 0, 2)
        # Two of the three milliseconds are written, and each brings its own
        # green line restating the SV the red one would have reset.
        self.assertEqual(len(self.document.timing_points), before + 4)
        self.assertEqual(
            [round(p.time) for p in self.document.timing_points if p.time in (1, 2)],
            [1, 1, 2, 2],
        )

    def test_the_dialog_walks_the_range_from_its_offset(self):
        dialog = gui.BarlineFunctionDialog(1000, 1010, self.window)
        self.assertEqual(dialog.times(), list(range(1000, 1011)), "n=1, m=0 by default")
        dialog.spacing_spin.setValue(4)
        dialog.start_offset_spin.setValue(2)
        self.assertEqual(dialog.times(), [1002, 1006, 1010])
        dialog.deleteLater()

    def test_snap_offset_shifts_each_line_without_moving_the_anchor(self):
        """Unlike the starting offset, this one must not change which grid
        lines "every n (ms)" would have picked -- here there is only one
        rhythm (spacing 1), so every millisecond in range is already picked;
        the snap offset just slides the whole picked set sideways."""
        dialog = gui.BarlineFunctionDialog(1000, 1010, self.window)
        dialog.snap_offset_spin.setValue(3)
        self.assertEqual(dialog.times(), list(range(1000, 1011)), "ms mode ignores it")
        dialog.deleteLater()

    # -- deleting red lines ------------------------------------------------

    def test_right_clicking_a_barline_deletes_it(self):
        self.window._place_gimmick("barline", "red_line", 10000)
        victim = next(p for p in self.document.timing_points if p.time == 10000)
        self.barline.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        self.barline.current_time = 10000.0

        self.barline.mousePressEvent(QMouseEvent(
            QEvent.MouseButtonPress, QPointF(self.barline.x_for_time(10000), 40.0),
            Qt.RightButton, Qt.RightButton, Qt.NoModifier,
        ))
        self.assertNotIn(victim, self.document.timing_points)

    def test_a_drag_selects_barlines_and_delete_removes_them_together(self):
        for at in (10000, 10100, 10200):
            self.window._place_gimmick("barline", "red_line", at)
        before = len(self.document.timing_points)
        self.barline.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        self.barline.current_time = 10100.0
        self.barline.window_ms = 1000.0
        self.barline.set_tool("select")

        self.barline.drag_anchor_time = 9950.0
        self.barline.drag_mouse_x = self.barline.x_for_time(10250.0)
        self.barline._update_drag_selection()
        self.assertEqual(len(self.barline.selected_timing_uids), 3)

        self.barline.keyPressEvent(QKeyEvent(
            QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier,
        ))
        self.assertEqual(len(self.document.timing_points), before - 3)

    def test_a_selection_never_reaches_a_line_the_layer_does_not_own(self):
        """The fixture's own line at 6000 is a barline; a line on a fake slider
        is not, and must survive a drag over it."""
        self.document.timing_points.append(gui.TimingPoint.uninherited_at(54692, 60000.0))
        self.window._refresh_gimmick_views()
        self.barline.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        self.barline.current_time = 54692.0

        self.barline.drag_anchor_time = 54000.0
        self.barline.drag_mouse_x = self.barline.x_for_time(55000.0)
        self.barline._update_drag_selection()
        self.assertEqual(self.barline.selected_timing_uids, set())

    # -- no duplicate barline notes ----------------------------------------

    def test_a_barline_note_cannot_be_placed_twice_on_one_millisecond(self):
        self.window._place_gimmick("barline", "don", 10000)
        after_first = len(self.document.timing_points)
        notes_after_first = len(self.document.hit_objects)

        self.window._place_gimmick("barline", "don", 10000)
        self.assertEqual(len(self.document.timing_points), after_first)
        self.assertEqual(len(self.document.hit_objects), notes_after_first)

    def test_a_fake_slider_cannot_be_placed_twice_on_one_millisecond_either(self):
        """One accidental double click used to write two sliders and four red
        lines on one snap: a note invisible in the editor and doubled in game."""
        self.window._place_gimmick("fake_slider", "don", 10000)
        points, notes = len(self.document.timing_points), len(self.document.hit_objects)

        self.window._place_gimmick("fake_slider", "don", 10000)
        self.assertEqual(len(self.document.timing_points), points)
        self.assertEqual(len(self.document.hit_objects), notes)

    def test_a_red_line_is_not_placed_on_one_that_is_already_there(self):
        self.window._place_gimmick("barline", "red_line", 10000)
        before = len(self.document.timing_points)
        self.window._place_gimmick("barline", "red_line", 10000)
        self.assertEqual(len(self.document.timing_points), before)

    def test_a_different_millisecond_still_places(self):
        self.window._place_gimmick("barline", "don", 10000)
        before = len(self.document.timing_points)
        self.window._place_gimmick("barline", "don", 10400)
        self.assertGreater(len(self.document.timing_points), before)


class GimmickAddViewTests(_GimmickFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)

    def test_the_dialog_offers_the_three_chart_only_views(self):
        dialog = gui.AddViewDialog([("Oni", self.path)], self.window, self.path, gimmick=True)
        offered = [dialog.type_combo.itemData(i) for i in range(dialog.type_combo.count())]
        self.assertEqual(
            set(gui.MainWindow.CHART_ONLY_VIEWS) - set(offered), set(),
            "every chart-only type has to be reachable from the gimmick dialog",
        )
        dialog.deleteLater()

    def test_the_page_has_an_add_view_button(self):
        self.assertTrue(self.window.gimmick_add_view_button.isEnabled())

    def test_an_added_view_is_a_band_on_the_base_timing(self):
        target = self.window._gimmick_pairing.target
        before = self.window.gimmick_views_layout.count()
        self.window._add_editor_view(
            "chart_barline", target, container=self.window.gimmick_views_layout
        )

        self.assertEqual(self.window.gimmick_views_layout.count(), before + 1)
        frame = self.window._editor_views[-1]
        view = frame.chart_view
        self.assertEqual(view.height(), self.window.gimmick_view_height_spin.value())
        self.assertTrue(frame.compact)
        self.assertTrue(view.show_timing_lines)
        self.assertEqual(
            [p.time for p in view.snap_points],
            [p.time for p in self.window._gimmick_pairing.base_timing],
            "an added band must snap against the snapshot like the six layers",
        )

    def test_each_chart_only_type_shows_only_its_own_objects(self):
        target = self.window._gimmick_pairing.target
        document = self.window._states[target].document
        expected = {
            "chart_regular": len([n for n in document.hit_objects if not gui.MainWindow.is_fake_slider(n)]),
            "chart_fake_slider": len([n for n in document.hit_objects if gui.MainWindow.is_fake_slider(n)]),
            "chart_barline": 0,
        }
        for view_type, count in expected.items():
            self.window._add_editor_view(
                view_type, target, container=self.window.gimmick_views_layout
            )
            self.assertEqual(len(self.window._editor_views[-1].chart_view.notes), count, view_type)

    def test_each_gameplay_only_type_scrolls_only_its_own_objects(self):
        """The same three layers as a scrolling preview, so a fake slider can
        be judged without the real chart's notes over it."""
        target = self.window._gimmick_pairing.target
        document = self.window._states[target].document
        expected = {
            "gameplay_regular": len([n for n in document.hit_objects if not gui.MainWindow.is_fake_slider(n)]),
            "gameplay_fake_slider": len([n for n in document.hit_objects if gui.MainWindow.is_fake_slider(n)]),
            "gameplay_barline": 0,
        }
        for view_type, count in expected.items():
            self.window._add_editor_view(
                view_type, target, container=self.window.gimmick_views_layout
            )
            view = self.window._editor_views[-1].gameplay_view
            self.assertEqual(len(view.notes), count, view_type)
            self.assertTrue(view.timing_points, "timing is never filtered -- it moves the notes")

    def test_a_rebuild_keeps_the_six_layers_above_an_added_band(self):
        """Leaving the tab and coming back rebuilds the layers; an added band
        survives that, so appending the layers left it sitting on top."""
        target = self.window._gimmick_pairing.target
        self.window._add_editor_view(
            "gameplay", target, container=self.window.gimmick_views_layout
        )
        added = self.window._editor_views[-1]
        self.window._open_gimmick_layers()

        layout = self.window.gimmick_views_layout
        frames = [
            layout.itemAt(i).widget() for i in range(layout.count())
            if isinstance(layout.itemAt(i).widget(), gui.EditorViewFrame)
        ]
        self.assertEqual(frames[: len(self.window._gimmick_views)], self.window._gimmick_views)
        self.assertIs(frames[-1], added)

    def test_the_dialog_offers_the_three_gameplay_only_views(self):
        dialog = gui.AddViewDialog([("Oni", self.path)], self.window, self.path, gimmick=True)
        offered = [dialog.type_combo.itemData(i) for i in range(dialog.type_combo.count())]
        self.assertEqual(set(gui.MainWindow.GAMEPLAY_ONLY_VIEWS) - set(offered), set())
        dialog.deleteLater()

    def test_the_two_editors_offer_their_own_lists(self):
        """The layer-restricted types answer a question only the gimmick page
        asks; on the Editor page they were eight entries between the five that
        page is for."""
        layer_only = set(gui.MainWindow.CHART_ONLY_VIEWS) | set(gui.MainWindow.GAMEPLAY_ONLY_VIEWS)
        plain = gui.AddViewDialog([("Oni", self.path)], self.window, self.path)
        gimmick = gui.AddViewDialog([("Oni", self.path)], self.window, self.path, gimmick=True)
        offered = {
            name: [d.type_combo.itemData(i) for i in range(d.type_combo.count())]
            for name, d in (("plain", plain), ("gimmick", gimmick))
        }
        self.assertEqual(set(offered["plain"]) & layer_only, set())
        self.assertLessEqual(layer_only, set(offered["gimmick"]))
        # The plain views of a whole chart stay in both.
        for view_type in ("chart", "sv", "kiai_sound", "gameplay", "density"):
            self.assertIn(view_type, offered["plain"], view_type)
            self.assertIn(view_type, offered["gimmick"], view_type)
        plain.deleteLater()
        gimmick.deleteLater()

    def test_an_added_band_can_be_closed_again(self):
        target = self.window._gimmick_pairing.target
        self.window._add_editor_view(
            "chart_regular", target, container=self.window.gimmick_views_layout
        )
        frame = self.window._editor_views[-1]
        view = frame.chart_view
        frame.closed.emit(frame)
        self.assertNotIn(frame, self.window._editor_views)
        self.assertNotIn(view, self.window._chart_views)


class GimmickLayoutTests(_GimmickFixture, unittest.TestCase):
    """The page's chrome: band-sized views and a full-sized toolbox."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)

    def test_every_layer_view_is_exactly_one_band_tall(self):
        """Two ways this has already gone wrong: a standalone minimum taller
        than the band (the view is clipped, showing its top half) and no
        minimum at all (a bare QWidget has no height of its own, so the layout
        gives it zero and the layer disappears)."""
        for frame in self.window._gimmick_views:
            view = getattr(frame, "chart_view", None) or frame.sv_view
            band = self.window.gimmick_view_height_spin.value()
            self.assertEqual(view.minimumHeight(), band, frame.gimmick_layer)
            self.assertEqual(view.maximumHeight(), band, frame.gimmick_layer)

    def test_a_layer_paints_at_its_band_height(self):
        """paintEvent divides by and clamps against the view's height; at a
        band's height none of that may raise, or the layer renders blank."""
        self.window.show()
        for frame in self.window._gimmick_views:
            view = getattr(frame, "chart_view", None) or frame.sv_view
            view.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
            pixmap = QPixmap(view.size())
            view.render(pixmap)
            self.assertFalse(pixmap.isNull(), frame.gimmick_layer)

    def test_the_snap_ticks_shrink_with_the_view(self):
        view = self.window._gimmick_views[0].chart_view
        beat_tick = view._tick_styles["beat"][1]
        # Two ticks grow inward from the edges, so at a band's height they must
        # still leave the middle -- where the notes are drawn -- clear.
        band = self.window.GIMMICK_LAYER_HEIGHT
        scaled = beat_tick * min(1.0, band / view.DESIGN_HEIGHT)
        self.assertLess(2 * scaled, band)

    def test_the_sv_graph_uses_the_band_it_is_given(self):
        view = self.window._gimmick_views[3].sv_view
        view.resize(600, self.window.GIMMICK_LAYER_HEIGHT)
        self.assertGreater(view._graph_bottom() - view._graph_top(), 0.6 * view.height())

    def test_the_gimmick_toolbox_is_its_own_compact_size(self):
        """Deliberately smaller than the Editor page's row, and no longer one
        shared width. The fake slider layer reached ten buttons, and giving
        every one of them the width of "Multiple Fake Slider" -- across all
        six layers, since they are equalized as one set -- came to 2798px,
        wider than the monitor. Heights still match each other so the rows do
        not jump when you switch layer; widths are each button's own."""
        self.window.show()
        heights = {button.height() for button in self.window._gimmick_row_buttons}
        self.assertEqual(len(heights), 1, "one height, so switching layer is stable")
        self.assertLess(
            heights.pop(), self.window.tool_buttons["select"].height(),
            "the gimmick row is the compact one",
        )
        widths = {button.width() for button in self.window._gimmick_row_buttons}
        self.assertGreater(len(widths), 1, "each button is its own width now")

    def test_a_tool_button_is_tall_enough_for_its_label(self):
        """Typed at 32 the padded label had no room left and descenders were
        clipped; the polished hint is what actually fits. The gimmick rows opt
        out of TOOL_BUTTON_HEIGHT entirely (see above), so the hint -- which
        already accounts for font, padding and border -- is the only bound
        that means anything for them."""
        self.window.show()
        self.assertGreaterEqual(
            self.window.tool_buttons["select"].height(), self.window.TOOL_BUTTON_HEIGHT
        )
        for button in (self.window.tool_buttons["select"], *self.window._gimmick_row_buttons):
            self.assertGreaterEqual(button.height(), button.sizeHint().height(), button.text())

    def test_both_pages_chrome_is_the_trimmed_size(self):
        """The chrome padding was trimmed for the gimmick page, where six
        frames of it stack in one screen. The Editor page is now the same
        shape -- a view height down to 90px and a chart per difficulty -- so
        both use it, and `compact` governs only the margins and spacing."""
        self.window.show()
        self.window._add_editor_view("chart", self.path)
        editor_frame = self.window._editor_views[-1]
        editor_frame.show()
        gimmick_frame = self.window._gimmick_views[0]

        self.assertTrue(gimmick_frame.compact)
        self.assertFalse(editor_frame.compact)
        self.assertEqual(
            gimmick_frame.close_button.sizeHint().height(),
            editor_frame.close_button.sizeHint().height(),
        )
        # Still the thing the trimming was for: a band's chrome must not be
        # taller than the layer it belongs to.
        self.assertLess(
            gimmick_frame.close_button.sizeHint().height(),
            self.window.GIMMICK_LAYER_HEIGHT,
        )


class GimmickSVTargetingTests(_GimmickFixture, unittest.TestCase):
    """A gimmick SV layer's points land on that layer's objects, and nowhere else.

    This is the whole of "the SV editor doesn't apply to its object": a point
    generated off its object is invisible in the layer that made it (the layer
    filters on exact milliseconds) *and* silently reset to 1.0x by the next
    uninherited line, which in a gimmick map is never far away.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document
        self.sv_layers = {
            f.gimmick_layer: f.sv_view
            for f in self.window._gimmick_views if hasattr(f, "sv_view")
        }

    def _params(self, **overrides):
        params = {
            "initial_rate": 1.0, "final_rate": 2.0, "placement": "notes",
            "snap_divisor": 4, "position_offset": 0, "omit_barline": False,
            "relative_to_final_bpm": False, "function": "linear",
        }
        params.update(overrides)
        return params

    def test_every_snap_does_not_escape_the_layers_objects(self):
        """"Every snap" walks the beat grid, which lands between a gimmick's
        objects. In a layer it is overridden rather than obeyed."""
        times = self.window._sv_generation_times(
            self.state, 0, 4000, self._params(placement="snaps"), "sv_chart",
        )
        note_times = {float(n.time) for n in self.document.hit_objects}
        self.assertTrue(times)
        self.assertTrue(set(times) <= note_times, times)

    def test_a_generated_point_is_visible_in_the_layer_that_made_it(self):
        self.window._generate_sv(
            self.window._gimmick_pairing.target, 0, 4000, self._params(), "sv_chart",
        )
        shown = {round(p.time) for p in self.sv_layers["sv_chart"]._visible_points}
        self.assertTrue(shown, "the sweep vanished from its own layer")
        self.assertIn(1000, shown)

    def test_the_dialog_takes_the_where_away_in_a_layer_but_keeps_the_offset(self):
        """A layer has no "where" to offer -- its points go on its own objects.
        How far ahead of them they sit is a real choice, and layer 4 needs it."""
        dialog = gui.SVFunctionDialog(0, 4000, self.window, gimmick_layer=True)
        dialog.position_offset_spin.setValue(-5)
        self.assertEqual(dialog.parameters()["position_offset"], -5)
        self.assertFalse(dialog.parameters()["relative_to_final_bpm"])
        self.assertFalse(gui.is_row_visible(dialog._form_layout, dialog.placement_combo))
        self.assertTrue(gui.is_row_visible(dialog._form_layout, dialog.position_offset_spin))
        dialog.deleteLater()

    def test_the_rate_ceiling_reaches_a_barline_gimmicks_speeds(self):
        dialog = gui.SVFunctionDialog(0, 4000, self.window, gimmick_layer=True)
        dialog.final_rate_spin.setValue(100.0)
        self.assertEqual(dialog.parameters()["final_rate"], 100.0)
        dialog.deleteLater()

    def test_the_sweep_starts_and_ends_on_the_rates_that_were_typed(self):
        """With BPM compensation on, a gimmick's 60000 BPM multiplied every
        rate by hundreds and neither end landed on its number."""
        target = self.window._gimmick_pairing.target
        self.window._place_gimmick("barline", "don", 2000)
        self.window._generate_sv(target, 0, 4000, self._params(), "sv_chart")

        generated = sorted(
            (p for p in self.document.timing_points if not p.uninherited and 0 <= p.time <= 4000),
            key=lambda p: p.time,
        )
        self.assertAlmostEqual(generated[0].sv_multiplier, 1.0, places=2)
        self.assertAlmostEqual(generated[-1].sv_multiplier, 2.0, places=2)

    def test_a_placed_green_line_snaps_to_the_layers_own_object(self):
        layer = self.sv_layers["sv_chart"]
        layer.resize(800, 120)
        layer.current_time = 1000.0
        layer.window_ms = 2000.0
        # 40px right of the object at 1000 is nowhere near a 1/4 division of a
        # 180 BPM grid, but it is nearest to that object.
        self.assertEqual(layer._placement_time(layer.x_for_time(1000.0) + 40), 1000.0)

    def test_the_plain_sv_editor_still_snaps_to_the_grid(self):
        plain = gui.SVEditorView()
        plain.resize(800, 200)
        plain.set_timing_points(self.document.timing_points)
        plain.current_time = 1000.0
        self.assertIsNone(plain.point_times)
        placed = plain._placement_time(plain.x_for_time(1040.0))
        self.assertNotEqual(placed, 1040.0, "still snapped, not taken literally")


class GimmickNoteReuseTests(_GimmickFixture, unittest.TestCase):
    """Don/Kat in layers 2 and 3 leave an existing note of the same kind alone."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.state = self.window._states[self.window._gimmick_pairing.target]
        self.document = self.state.document
        # A real don on a base-grid snap, with a hitsound worth not losing.
        self.window._place_note(self.window._gimmick_pairing.target, "don", 10000, False)
        self.note = next(n for n in self.document.hit_objects if n.time == 10000)
        self.note.hit_sample = "1:2:3:40:"

    def test_a_fake_slider_don_reuses_the_note_that_is_already_there(self):
        before = len(self.document.hit_objects)
        self.window._place_gimmick("fake_slider", "don", 10000)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms

        self.assertEqual(len(self.document.hit_objects) - before, 1, "only the slider")
        kept = next(n for n in self.document.hit_objects if n.time == 10000)
        self.assertEqual(kept.hit_sample, "1:2:3:40:", "the mapper's own note survived")
        self.assertTrue(any(
            gui.MainWindow.is_fake_slider(n) for n in self.document.hit_objects
            if n.time == 10000 + offset
        ))

    def test_a_barline_don_writes_its_lines_and_no_second_note(self):
        before = len(self.document.hit_objects)
        points_before = list(self.document.timing_points)
        self.window._place_gimmick("barline", "don", 10000)

        self.assertEqual(len(self.document.hit_objects), before)
        self.assertEqual(
            len([p for p in self.document.timing_points if p.uninherited])
            - len([p for p in points_before if p.uninherited]),
            2,
        )

    def test_a_note_a_millisecond_off_the_snap_still_centres_the_structure(self):
        """A snap is rarely a whole millisecond and the map's own rounding of it
        need not match ours. Built on the bare snap, a barline Don next to a
        note one millisecond away wrote a *second* hit object beside it."""
        self.note.time = 10001
        before = len(self.document.hit_objects)

        self.window._place_gimmick("barline", "don", 10000)

        self.assertEqual(len(self.document.hit_objects), before, "no second note")
        centre = next(
            p for p in self.document.timing_points
            if p.bpm == 60000.0 and 9990 <= p.time <= 10010
        )
        self.assertEqual(centre.time, 10001, "the structure moved onto the note")

    def test_the_other_kind_still_replaces(self):
        before = len(self.document.hit_objects)
        self.window._place_gimmick("barline", "kat", 10000)
        self.assertEqual(len(self.document.hit_objects), before, "replaced, not added")
        self.assertTrue(next(n for n in self.document.hit_objects if n.time == 10000).is_kat)

    def test_a_don_on_a_drumroll_still_writes_its_own_note(self):
        """A drumroll's hit sound is 0, which reads as a don -- so the "the
        chart already has this note" test used to drop the gimmick's own note
        and leave the bars with nothing to hit."""
        roll = next(n for n in self.document.hit_objects if n.is_slider)
        at = round(roll.time)

        self.window._place_gimmick("barline", "don", at)

        self.assertTrue(
            any(n.is_circle and round(n.time) == at for n in self.document.hit_objects),
            "the bars were written; the note has to be there to hit",
        )

    def test_a_fake_slider_never_displaces_the_note_it_decorates(self):
        """A fake slider is drawn beside a note, not a note competing with it
        for the millisecond, so the one-per-millisecond rule leaves it alone."""
        before = len(self.document.hit_objects)
        self.window._place_gimmick("fake_slider", "regular", 10000)

        self.assertEqual(len(self.document.hit_objects) - before, 1)
        self.assertTrue(any(
            n.time == 10000 and not gui.MainWindow.is_fake_slider(n)
            for n in self.document.hit_objects
        ), "the real note is still there")


class GimmickMoveTests(_GimmickFixture, unittest.TestCase):
    """Dragging a gimmick object carries the structure around it."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.target = self.window._gimmick_pairing.target
        self.state = self.window._states[self.target]
        self.document = self.state.document

    def _red_times(self):
        return sorted(round(p.time) for p in self.document.timing_points if p.uninherited)

    def test_the_red_line_tool_refuses_a_fake_slider_or_shiny_line(self):
        """No second red line on one millisecond, ever -- a fake slider's or
        shiny's own line included -- and the refusal says why by name."""
        # Both lines sit on the snap, where a Red Line click lands: a Don fake
        # slider's squash line is on its note, and a shiny's head line is on
        # the clicked millisecond (the object is shiny_offset_ms after it). A
        # plain fake slider's own line is fake_slider_offset_ms off the grid,
        # so a click there snaps back before it reaches the line.
        self.window._place_gimmick("fake_slider", "don", 10000)
        self.window._place_gimmick("fake_slider", "shiny", 20000)
        for label, line_at in (("fake slider", 10000), ("shiny", 20000)):
            with self.subTest(label):
                self.assertIn(line_at, self._red_times())
                before = len(self.document.timing_points)
                self.window._place_gimmick("barline", "red_line", line_at)
                self.assertEqual(len(self.document.timing_points), before)
                self.assertEqual(self.window._toast.text(), "There is already a red line here.")

    def test_a_fake_slider_carries_its_own_red_line(self):
        """One line, not two. A plain fake slider stopped writing a 60000 BPM
        squash -- there is no note under it to hide -- so all it has is the
        line on its own millisecond, and dragging the slider must take that
        line with it or the object stops being fake."""
        self.window._place_gimmick("fake_slider", "regular", 10000)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        slider = next(
            n for n in self.document.hit_objects
            if gui.MainWindow.is_fake_slider(n) and n.time == 10000 + offset
        )
        before = set(self._red_times())

        self.window._move_objects(self.target, [slider.uid], [], 500)

        self.assertEqual(slider.time, 10500 + offset)
        moved = set(self._red_times())
        self.assertNotIn(10000 + offset, moved)
        self.assertIn(10500 + offset, moved)
        self.assertEqual(len(moved), len(before), "nothing was created or lost")

    def test_a_barline_gimmick_line_carries_its_restores_and_its_note(self):
        self.window._place_gimmick("barline", "kat", 10000)
        gimmick_line = next(
            p for p in self.document.timing_points
            if p.uninherited and round(p.time) == 10000 and p.bpm == 60000.0
        )

        self.window._move_objects(self.target, [], [gimmick_line.uid], -300)

        moved = self._red_times()
        for offset in (-5, -3, -1, 0, 1, 3, 5):
            self.assertIn(9700 + offset, moved, f"restore line at {offset} stayed behind")
        self.assertTrue(any(n.time == 9700 for n in self.document.hit_objects))

    def test_the_normal_chart_layers_note_carries_the_bars_it_stands_on(self):
        """Layer 1 is where a barline note's note is shown. Nudging it there
        used to leave the bars behind at the old time."""
        self.window._place_gimmick("barline", "don", 10000)
        note = next(n for n in self.document.hit_objects if n.time == 10000)

        self.window._move_objects(self.target, [note.uid], [], 200)

        moved = self._red_times()
        for offset in (0, 1):
            self.assertIn(10200 + offset, moved)
            self.assertNotIn(10000 + offset, moved)

    def test_the_move_is_one_undo_step(self):
        self.window._place_gimmick("barline", "don", 10000)
        before = self._red_times()
        line = next(p for p in self.document.timing_points if p.bpm == 60000.0)

        self.window._move_objects(self.target, [], [line.uid], 250)
        self.state.history.undo(self.state)

        self.assertEqual(self._red_times(), before)

    def test_a_line_dragged_onto_its_own_kind_replaces_it(self):
        """One red and one green per millisecond. A red and a green may share
        one; a second of either kind is replaced, as a dragged note is."""
        points = self.document.timing_points
        red = gui.TimingPoint(time=20000.0, beat_length=500.0, uninherited_flag=1)
        other_red = gui.TimingPoint(time=20300.0, beat_length=400.0, uninherited_flag=1)
        green = gui.TimingPoint(time=20300.0, beat_length=-50.0, uninherited_flag=0)
        points.extend([red, other_red, green])
        points.sort(key=lambda p: p.time)

        self.window._move_objects(self.target, [], [red.uid], 300)

        at = [p for p in points if round(p.time) == 20300]
        self.assertIn(red, at)
        self.assertNotIn(other_red, at, "the red line landed on was replaced")
        self.assertIn(green, at, "a green line is not the same kind")
        self.state.history.undo(self.state)
        self.assertIn(other_red, points)

    def test_a_drag_of_nothing_writes_nothing(self):
        self.window._move_objects(self.target, [], [], 0)
        self.assertFalse(self.state.history.can_undo())

    def test_the_editor_pages_own_chart_view_also_moves_on_drag(self):
        """A plain click (no drag) still selects/retypes -- TimelineGameplay's
        own _finish_move falls back to that when nothing moved -- but a press
        that turns into a drag must relocate the note, the same as a gimmick
        layer, or a select-tool drag can only ever box a note and never nudge
        it."""
        self.window._add_editor_view("chart", self.path)
        self.assertTrue(self.window._editor_views[-1].chart_view.move_enabled)
        self.assertTrue(self.window._gimmick_views[0].chart_view.move_enabled)


class GimmickClipboardTests(_GimmickFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.target = self.window._gimmick_pairing.target
        self.state = self.window._states[self.target]
        self.document = self.state.document
        self.barline = self.window._gimmick_views[2].chart_view

    def test_the_barline_layer_copies_and_pastes_its_red_lines(self):
        self.window._place_gimmick("barline", "don", 10000)
        self.barline.refresh_notes(self.document)
        self.barline.selected_timing_uids = {
            p.uid for p in self.document.timing_points
            if p.uninherited and 9990 <= p.time <= 10010
        }
        self.assertEqual(len(self.barline.selected_timing_uids), 2)

        self.window._active_chart_view = self.barline
        self.window._copy_notes()
        self.barline.current_time = 20000.0
        self.window._paste_notes()

        pasted = sorted(
            round(p.time) for p in self.document.timing_points
            if p.uninherited and 19000 <= p.time <= 21000
        )
        self.assertEqual(len(pasted), 2, pasted)
        self.assertEqual(pasted[1] - pasted[0], 1, "the structure kept its shape")

    def test_a_copied_structure_keeps_its_green_line_and_its_file_order(self):
        """A Don fake slider's `fake_slider_sv` is what takes the squashed note
        the rest of the way off screen, and copy dropped every inherited point
        -- so the pasted structure was a squashed but still-drawn note. It has
        to land *after* the red line sharing its millisecond too: osu! resolves
        a shared timestamp by file order and an uninherited point resets SV to
        1.0x, so the other way round it is silently cancelled."""
        self.window._place_gimmick("fake_slider", "don", 10000)
        fake = self.window._gimmick_views[1].chart_view
        fake.refresh_notes(self.document)
        source = next(
            p for p in self.document.timing_points
            if p.inherited and round(p.time) == 10000
        )
        # The layer draws fake sliders, not notes -- the structure is grabbed by
        # its slider and `_expand_move` grows it into the rest.
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        fake.selected = {
            n.original_index for n in self.document.hit_objects
            if n.time == 10000 + offset
        }

        self.window._active_chart_view = fake
        self.window._copy_notes()
        fake.current_time = 20000.0
        self.window._paste_notes()

        at_20000 = [p for p in self.document.timing_points if round(p.time) == 20000]
        self.assertEqual(
            [p.uninherited for p in at_20000], [True, False], "red line first",
        )
        self.assertAlmostEqual(at_20000[1].sv_multiplier, source.sv_multiplier)

    def test_a_paste_does_not_stack_a_second_green_line_on_one_millisecond(self):
        """Same rule the red lines follow: the millisecond already has SV, and
        a second point there only overrides what is in it."""
        self.window._place_gimmick("fake_slider", "don", 10000)
        self.window._place_gimmick("fake_slider", "don", 20000)
        fake = self.window._gimmick_views[1].chart_view
        fake.refresh_notes(self.document)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        fake.selected = {
            n.original_index for n in self.document.hit_objects
            if n.time == 10000 + offset
        }
        self.window._active_chart_view = fake
        self.window._copy_notes()
        before = len([p for p in self.document.timing_points if p.inherited])

        fake.current_time = 20000.0
        self.window._paste_notes()

        self.assertEqual(
            len([p for p in self.document.timing_points if p.inherited]), before,
        )

    def test_a_pasted_fake_slider_keeps_its_offset_from_the_snap(self):
        """Paste lands where placing at the playhead would, not on it.

        A plain fake slider's own line is on its own millisecond, so the copy
        had nothing on the snap to measure from and pasted the slider onto the
        playhead itself -- its offset lost on every Ctrl+V."""
        config = self.window._gimmick_config("fake_slider")
        fake = self.window._gimmick_views[1].chart_view
        self.window._active_chart_view = fake
        for kind, source, target, offset in (
            ("regular", 10000, 20000, config.fake_slider_offset_ms),
            ("shiny", 40000, 50000, config.shiny_offset_ms),
        ):
            with self.subTest(kind):
                self.window._place_gimmick("fake_slider", kind, source)
                fake.refresh_notes(self.document)
                fake.selected = {
                    n.original_index for n in self.document.hit_objects
                    if gui.MainWindow.is_fake_slider(n) and n.time == source + offset
                }
                self.assertTrue(fake.selected)
                self.window._copy_notes()
                fake.current_time = float(target)
                before = {n.uid for n in self.document.hit_objects}
                self.window._paste_notes()
                landed = sorted(
                    n.time for n in self.document.hit_objects
                    if n.uid not in before and gui.MainWindow.is_fake_slider(n)
                )
                self.assertTrue(landed)
                self.assertEqual(landed[0], target + offset)

    def test_the_fake_slider_layer_copies_the_line_that_makes_it_fake(self):
        """Layer 2 owns no red lines, so its selection never contains one --
        a notes-only copy pasted a bare drumroll."""
        self.window._place_gimmick("fake_slider", "regular", 10000)
        fake = self.window._gimmick_views[1].chart_view
        fake.refresh_notes(self.document)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        fake.selected = {
            n.original_index for n in self.document.hit_objects
            if gui.MainWindow.is_fake_slider(n) and n.time == 10000 + offset
        }

        self.window._active_chart_view = fake
        self.window._copy_notes()
        fake.current_time = 20000.0
        self.window._paste_notes()

        pasted = [
            round(p.time) for p in self.document.timing_points
            if p.uninherited and 19000 <= p.time <= 21000
        ]
        self.assertTrue(pasted, "the gimmick line came with the slider")

    def test_the_gimmick_page_routes_copy_by_the_focused_layer(self):
        self.window._show_page(gui.PAGE_GIMMICK)
        self.window._active_gimmick_layer = "sv_barline"
        self.assertEqual(self.window._clipboard_target(), "sv")
        self.window._active_gimmick_layer = "barline"
        self.assertEqual(self.window._clipboard_target(), "chart")

    def test_a_gimmick_layer_resolves_to_its_difficulty(self):
        self.assertEqual(self.window._difficulty_for_view(self.barline), self.target)


class GimmickViewChromeTests(_GimmickFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)

    def test_alt_wheel_moves_the_whole_pages_snap(self):
        view = self.window._gimmick_views[0].chart_view
        self.window.gimmick_snap_combo.setCurrentIndex(
            self.window.gimmick_snap_combo.findData(4)
        )
        view._change_snap_from_wheel(120)

        self.assertEqual(self.window._gimmick_snap_divisor(), 5)
        for frame in self.window._gimmick_views:
            layer = getattr(frame, "chart_view", None) or getattr(frame, "sv_view", None)
            self.assertEqual(layer.snap_divisor, 5, frame.gimmick_layer)

    def test_moving_to_another_layer_arrives_on_select(self):
        first = self.window._gimmick_views[1].chart_view
        second = self.window._gimmick_views[2].chart_view
        self.window._editor_view_focus_changed(None, first)
        self.window.gimmick_tool_buttons["fake_slider"]["don"].setChecked(True)
        self.assertEqual(first.tool, "don")

        self.window._editor_view_focus_changed(first, second)
        self.window._editor_view_focus_changed(second, first)

        self.assertEqual(first.tool, "select")
        self.assertTrue(self.window.gimmick_tool_buttons["fake_slider"]["select"].isChecked())

    def test_returning_to_the_same_view_keeps_its_tool(self):
        """A modal dialog takes focus and gives it back; that is not a move."""
        view = self.window._gimmick_views[2].chart_view
        self.window._editor_view_focus_changed(None, view)
        self.window.gimmick_tool_buttons["barline"]["don"].setChecked(True)
        self.window._editor_view_focus_changed(None, view)
        self.assertEqual(view.tool, "don")

    def test_the_zoom_floor_reaches_a_gimmicks_own_scale(self):
        view = self.window._gimmick_views[0].chart_view
        self.assertLessEqual(view.MIN_WINDOW_MS, 20.0)
        view.window_ms = view.MIN_WINDOW_MS
        view.resize(800, 88)
        # One millisecond has to be worth aiming at: a barline note's restore
        # lines are 2ms from its centre.
        self.assertGreater(view.x_for_time(1.0) - view.x_for_time(0.0), 10.0)


class BarlineSnapGenerationTests(_GimmickFixture, unittest.TestCase):
    """The barline function tool's second rhythm: every n beat divisions."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.base = self.window._gimmick_pairing.base_timing

    def _dialog(self, start, end, **fields):
        dialog = gui.BarlineFunctionDialog(
            start, end, self.window, base_timing=self.base, snap_divisor=4,
        )
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("snaps"))
        for name, value in fields.items():
            getattr(dialog, name).setValue(value)
        return dialog

    def test_every_snap_walks_the_base_grid(self):
        dialog = self._dialog(0, 1000)
        times = dialog.times()
        dialog.deleteLater()
        # The fixture is 500ms a beat from 0, so 125ms at 1/4.
        self.assertEqual(times[:4], [0, 125, 250, 375])

    def test_every_second_snap_takes_every_other_one(self):
        dialog = self._dialog(0, 1000, snap_count_spin=2)
        times = dialog.times()
        dialog.deleteLater()
        self.assertEqual(times[:3], [0, 250, 500])

    def test_the_snap_offset_still_applies(self):
        dialog = self._dialog(0, 1000, snap_offset_spin=5)
        times = dialog.times()
        dialog.deleteLater()
        self.assertEqual(times[0], 5)

    def test_the_starting_offset_phase_shifts_which_snaps_are_picked(self):
        """A snap offset alone would slide every picked line by 5ms, not
        change which ones "every 2nd snap" picks. The starting offset moves
        the anchor `_snap_times` counts from instead, so it can pick the
        *other* half of the grid -- the 125/375/625... beats rather than
        0/250/500 -- something no per-line shift can do since it always
        keeps the same beat-grid lines and merely offsets their timestamps."""
        dialog = self._dialog(0, 1000, snap_count_spin=2, start_offset_spin=125)
        times = dialog.times()
        dialog.deleteLater()
        self.assertEqual(times[:3], [125, 375, 625])

    def test_grid_lines_commit_by_truncation_not_nearest(self):
        """Reported as the snap offset landing 1ms off unpredictably: a red
        line 2ms before a grid line sometimes came out 3ms before instead.

        The grid itself was the bug, not the offset -- round()'s banker's
        rounding does not match `time_axis.osu_snap_ms`'s truncation, which is
        what every other beat-committing site in the app uses. A 333ms beat at
        1/4 steps 83.25ms at a time, so the exact fractional part cycles
        .0/.25/.5/.75 across the grid: round() and floor() agree at .0 and
        .25, but round() rounds .75 up and .5 either way depending on parity
        -- both wrong by the project's own "always down" convention, and
        inconsistently so across one continuous grid.
        """
        base = [gui.TimingPoint.uninherited_at(0, 60000.0 / 333.0)]
        dialog = gui.BarlineFunctionDialog(0, 700, self.window, base_timing=base, snap_divisor=4)
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("snaps"))
        times = dialog.times()
        dialog.deleteLater()
        self.assertEqual(times, [0, 83, 166, 249, 333, 416, 499, 582, 666])

    def test_without_a_growth_function_the_lines_keep_the_base_bpm(self):
        dialog = self._dialog(0, 1000)
        bpms = dialog.bpms(dialog.times())
        dialog.deleteLater()
        # The fixture is 500ms a beat: 120 BPM, all the way through.
        self.assertEqual(set(bpms), {120.0})

    def test_a_linear_ramp_reaches_both_ends_exactly(self):
        dialog = self._dialog(0, 1000)
        dialog.bpm_curve_combo.setCurrentIndex(dialog.bpm_curve_combo.findData("linear"))
        dialog.start_bpm_spin.setValue(120.0)
        dialog.end_bpm_spin.setValue(240.0)
        times = dialog.times()
        bpms = dialog.bpms(times)
        dialog.deleteLater()

        self.assertAlmostEqual(bpms[0], 120.0)
        self.assertAlmostEqual(bpms[-1], 240.0)
        # Progress is measured in time, so the middle of the range is the middle
        # of the ramp whatever the spacing.
        middle = min(range(len(times)), key=lambda i: abs(times[i] - (times[0] + times[-1]) / 2))
        self.assertAlmostEqual(bpms[middle], 180.0, places=1)

    def test_an_exponential_ramp_stays_below_the_linear_one(self):
        """That is what "grows slowly then rushes" means -- and it is the same
        `sv_ease` the SV generator plots, so the two agree on the shape."""
        dialog = self._dialog(0, 1000)
        dialog.start_bpm_spin.setValue(120.0)
        dialog.end_bpm_spin.setValue(240.0)
        times = dialog.times()
        curves = {}
        for function_id in ("linear", "exp1.3", "exp1.6", "true_exp"):
            dialog.bpm_curve_combo.setCurrentIndex(
                dialog.bpm_curve_combo.findData(function_id)
            )
            curves[function_id] = dialog.bpms(times)
        dialog.deleteLater()

        middle = len(times) // 2
        for function_id in ("exp1.3", "exp1.6", "true_exp"):
            self.assertLess(curves[function_id][middle], curves["linear"][middle], function_id)
            self.assertAlmostEqual(curves[function_id][-1], 240.0, places=6, msg=function_id)

    def test_the_millisecond_mode_is_unchanged(self):
        dialog = gui.BarlineFunctionDialog(
            0, 10, self.window, base_timing=self.base, snap_divisor=4,
        )
        times = dialog.times()
        dialog.deleteLater()
        self.assertEqual(times, list(range(0, 11)))


class GimmickTimingReferenceTests(_GimmickFixture, unittest.TestCase):
    """The base grid comes from a difficulty chosen on entry, and is remembered."""

    def setUp(self) -> None:
        super().setUp()
        # A second difficulty in the same folder, at a different BPM, so which
        # file the grid came from is visible in the numbers.
        self.other = Path(self._temp.name) / "reference.osu"
        self.other.write_bytes(
            self.path.read_bytes().replace(b"333.333333333333", b"500.0")
        )
        self.window._load_map_path(self.path, refresh_difficulties=True)

    def test_the_dialog_is_offered_every_difficulty_its_own_first(self):
        """Including the one being entered from, and listed first so it is what
        the combo shows before the list is ever opened.

        It used to be excluded, on the grounds that a difficulty is a poor
        reference for its own gimmicks -- true once the file fills with 60000
        BPM lines, but the snapshot is taken before any of that, so its timing
        is still the song's at the moment it is read. Excluding it left the
        common case ("this map is already timed, just use it") with no answer.
        """
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        offered = [path.name for _label, path in _StubDialog.offered]
        self.assertIn("reference.osu", offered)
        self.assertEqual(offered[0], self.path.name, "its own, and the default")

    def test_the_chosen_difficultys_timing_becomes_the_grid(self):
        _StubDialog.reference = self.other
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        pairing = self.window._gimmick_pairing

        self.assertEqual(pairing.reference, self.other)
        self.assertEqual(pairing.base_timing[0].beat_length, 500.0)
        for frame in self.window._gimmick_views:
            view = getattr(frame, "chart_view", None) or getattr(frame, "sv_view", None)
            self.assertEqual(view.snap_points[0].beat_length, 500.0)

    def test_the_reference_is_remembered_with_the_pairing(self):
        _StubDialog.reference = self.other
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)

        reloaded = gui.load_index(self.index_path)
        pairing = reloaded[gui.index_key(self.path)]
        self.assertEqual(pairing.reference, self.other)
        self.assertEqual(pairing.base_timing[0].beat_length, 500.0)

    def test_barlines_placed_here_never_move_the_grid(self):
        _StubDialog.reference = self.other
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        view = self.window._gimmick_views[0].chart_view
        before = [point.beat_length for point in view.snap_points]

        self.window._place_gimmick("barline", "don", 10000)

        self.assertEqual([p.beat_length for p in view.snap_points], before)


def _wheel(view, dy, modifiers=Qt.NoModifier):
    return QWheelEvent(
        QPointF(50, 20), view.mapToGlobal(QPoint(50, 20)), QPoint(0, dy), QPoint(0, dy),
        Qt.NoButton, modifiers, Qt.NoScrollPhase, False,
    )


class GimmickWheelTests(_GimmickFixture, unittest.TestCase):
    """The wheel means the same thing anywhere on the page.

    The application-wide event filter used to swallow every Alt+wheel in the
    program and hand it to the Fancy Arranger timeline, so no gimmick layer ever
    saw one -- and a plain wheel outside a band scrolled the scroll area instead
    of seeking.
    """

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.window._show_page(gui.PAGE_GIMMICK)
        self.page = self.window.gimmick_page
        self.layers = [
            getattr(f, "chart_view", None) or getattr(f, "sv_view", None)
            for f in self.window._gimmick_views
        ]

    def test_alt_wheel_over_the_page_background_changes_the_snap(self):
        combo = self.window.gimmick_snap_combo
        combo.setCurrentIndex(combo.findData(4))

        QApplication.sendEvent(self.page, _wheel(self.page, 120, Qt.AltModifier))

        self.assertEqual(self.window._gimmick_snap_divisor(), 5)
        for layer in self.layers:
            self.assertEqual(layer.snap_divisor, 5)

    def test_alt_wheel_leaves_the_fancy_arranger_timeline_alone(self):
        before = self.window.timeline.snap_divisor
        QApplication.sendEvent(self.page, _wheel(self.page, 120, Qt.AltModifier))
        self.assertEqual(self.window.timeline.snap_divisor, before)

    def test_a_plain_wheel_over_the_page_background_seeks(self):
        """Seeking is what the wheel means on this page wherever the pointer
        is, so the page's own chrome -- its timing bar, its tool rows -- keeps
        it. Only the scroll area's own space (the gaps between bands, and the
        scrollbar) scrolls instead; see ScrollBarWheelTests. Making *everything*
        off a band scroll turned this chrome into a dead zone, which is what
        "scrolling no longer moves the view horizontally" was."""
        before = self.layers[0].current_time
        QApplication.sendEvent(self.page, _wheel(self.page, -120))
        self.assertNotEqual(self.layers[0].current_time, before)

    def test_ctrl_wheel_over_the_page_background_zooms_every_band(self):
        before = self.layers[0].window_ms
        QApplication.sendEvent(self.page, _wheel(self.page, 120, Qt.ControlModifier))
        windows = {layer.window_ms for layer in self.layers}
        self.assertEqual(len(windows), 1, "the bands stayed on one scale")
        self.assertLess(windows.pop(), before)

    def test_a_combo_box_keeps_its_own_wheel(self):
        self.assertIsNone(self.window._page_wheel_target(self.window.gimmick_snap_combo))

    def test_the_editor_page_seeks_its_own_views_not_these(self):
        self.window._show_page(gui.PAGE_EDITOR)
        target = self.window._page_wheel_target(self.window.editor_page)
        self.assertNotIn(target, self.layers)


class GimmickPlacementToolMoveTests(_GimmickFixture, unittest.TestCase):
    """Dragging works with a placement tool in hand; clicking still places."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.target = self.window._gimmick_pairing.target
        self.document = self.window._states[self.target].document
        self.view = self.window._gimmick_views[0].chart_view
        self.view.resize(800, 88)
        self.view.window_ms = 2000.0
        self.view.current_time = 1000.0

    def _press(self, x):
        QApplication.sendEvent(self.view, QMouseEvent(
            QEvent.MouseButtonPress, QPointF(x, 44), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))

    def _move(self, x):
        QApplication.sendEvent(self.view, QMouseEvent(
            QEvent.MouseMove, QPointF(x, 44), Qt.NoButton, Qt.LeftButton, Qt.NoModifier))

    def _release(self, x):
        QApplication.sendEvent(self.view, QMouseEvent(
            QEvent.MouseButtonRelease, QPointF(x, 44), Qt.LeftButton, Qt.NoButton, Qt.NoModifier))

    def _drag(self, from_ms, to_ms):
        self._press(self.view.x_for_time(from_ms))
        self._move(self.view.x_for_time(to_ms))
        self._release(self.view.x_for_time(to_ms))

    def test_a_drag_with_a_placement_tool_held_moves_the_object(self):
        self.view.set_tool("don")
        self._drag(1000.0, 1250.0)
        times = sorted(n.time for n in self.document.hit_objects)
        self.assertNotIn(1000, times)
        self.assertIn(1250, times)

    def test_a_click_with_a_placement_tool_held_still_places(self):
        self.view.set_tool("kat")
        self._press(self.view.x_for_time(1000.0))
        self._release(self.view.x_for_time(1000.0))

        at = [n for n in self.document.hit_objects if n.time == 1000]
        self.assertEqual(len(at), 1, "retyped, not stacked")
        self.assertTrue(at[0].is_kat)

    def test_the_normal_chart_layers_tools_reach_the_document(self):
        """Layer 1's toolbox was wired to nothing at all, so Don, Kat, Slider,
        Spinner and New Combo were inert in the one layer that is a plain
        chart."""
        before = len(self.document.hit_objects)
        self.view.set_tool("don")
        self._press(self.view.x_for_time(1750.0))
        self._release(self.view.x_for_time(1750.0))
        self.assertEqual(len(self.document.hit_objects) - before, 1)

    def test_a_move_onto_an_occupied_millisecond_replaces_rather_than_stacks(self):
        self.view.set_tool("select")
        self._drag(1000.0, 1500.0)
        self.assertEqual(len([n for n in self.document.hit_objects if n.time == 1500]), 1)

    def _barline_drag(self, from_ms, to_ms):
        barline = self.window._gimmick_views[2].chart_view
        barline.resize(800, 88)
        barline.window_ms = 1000.0
        barline.current_time = float(from_ms)
        barline.set_tool("select")
        for kind, button, buttons in (
            (QEvent.MouseButtonPress, Qt.LeftButton, Qt.LeftButton),
            (QEvent.MouseMove, Qt.NoButton, Qt.LeftButton),
            (QEvent.MouseButtonRelease, Qt.LeftButton, Qt.NoButton),
        ):
            at = from_ms if kind == QEvent.MouseButtonPress else to_ms
            QApplication.sendEvent(barline, QMouseEvent(
                kind, QPointF(barline.x_for_time(float(at)), 44), button, buttons,
                Qt.NoModifier))
        return barline

    def test_a_plain_red_line_drag_moves_by_milliseconds_not_by_snaps(self):
        """The fixture's 1/4 division is 125ms, so a line nudged 50ms would
        otherwise round straight back to where it started."""
        self.window._place_gimmick("barline", "red_line", 10000)
        self._barline_drag(10000, 10050)

        moved = sorted(
            round(p.time) for p in self.document.timing_points
            if p.uninherited and 9900 < p.time < 10200
        )
        self.assertEqual(moved, [10050])

    def test_a_barline_note_drag_lands_on_the_grid(self):
        """A barline note is a note drawn out of red lines, so it moves like
        one -- its restore lines keep their millisecond offsets around it."""
        # 150 BPM from 6000 at 1/4: the divisions here are 100ms.
        self.window._place_gimmick("barline", "kat", 10000)
        self._barline_drag(10000, 10160)

        moved = sorted(
            round(p.time) for p in self.document.timing_points
            if p.uninherited and 10100 < p.time < 10300
        )
        self.assertEqual(moved, [10195, 10197, 10199, 10200, 10201, 10203, 10205])
        self.assertTrue(any(n.time == 10200 for n in self.document.hit_objects))

    def test_a_configured_gimmick_bpm_is_still_a_structure(self):
        """The centre is found by the layer's own configured BPM, not by "high
        enough": at 800 a bar dragged alone left the note and six bars behind."""
        self.window.gimmick_configs["barline"] = gui.GimmickConfig(gimmick_bpm=800.0)
        self.window._place_gimmick("barline", "kat", 10000)
        self.window._refresh_gimmick_views()
        self._barline_drag(9995, 10160)

        moved = sorted(
            round(p.time) for p in self.document.timing_points
            if p.uninherited and 10100 < p.time < 10300
        )
        self.assertEqual(moved, [10195, 10197, 10199, 10200, 10201, 10203, 10205])

    def test_any_bar_of_a_barline_note_drags_the_whole_note(self):
        """Its seven lines are one pixel column apart; the centre is the one a
        drag means whichever of them the cursor was nearest."""
        self.window._place_gimmick("barline", "kat", 10000)
        barline = self._barline_drag(9995, 10105)

        self.assertTrue(any(n.time == 10100 for n in self.document.hit_objects))
        self.assertIsNone(barline._move_origin)

    def test_a_dragged_note_is_previewed_ahead_of_its_own_ghost(self):
        """Mid-drag the object has to say two things: where it is going, and
        where it came from. The document is untouched until release, so the
        ghost is the only thing marking the position it would go back to."""
        self.view.set_tool("select")
        self._press(self.view.x_for_time(1000.0))
        self._move(self.view.x_for_time(1250.0))

        note = next(n for n in self.document.hit_objects if n.time == 1000)
        self.assertEqual(self.view._move_delta, 250.0)
        ghost_x = self.view.x_for_time(note.time)
        preview_x = self.view.x_for_time(note.time + self.view._move_delta)
        self.assertNotEqual(round(ghost_x), round(preview_x))

        # Both are drawn in the one pass, and the pass survives it.
        pixmap = QPixmap(self.view.size())
        self.view.render(pixmap)
        self.assertFalse(pixmap.isNull())

        self._release(self.view.x_for_time(1250.0))
        self.assertIsNone(self.view._move_origin, "the ghost clears on release")

    def test_a_dragged_red_line_is_previewed_ahead_of_its_own_ghost(self):
        self.window._place_gimmick("barline", "don", 10000)
        barline = self.window._gimmick_views[2].chart_view
        barline.resize(800, 88)
        barline.window_ms = 400.0
        barline.current_time = 10000.0
        barline.set_tool("select")

        QApplication.sendEvent(barline, QMouseEvent(
            QEvent.MouseButtonPress, QPointF(barline.x_for_time(10000.0), 44),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        QApplication.sendEvent(barline, QMouseEvent(
            QEvent.MouseMove, QPointF(barline.x_for_time(10125.0), 44),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier))

        self.assertEqual(barline._move_delta, 100.0)
        self.assertTrue(barline._move_point_uids)
        pixmap = QPixmap(barline.size())
        barline.render(pixmap)
        self.assertFalse(pixmap.isNull())

        QApplication.sendEvent(barline, QMouseEvent(
            QEvent.MouseButtonRelease, QPointF(barline.x_for_time(10125.0), 44),
            Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
        self.assertIsNone(barline._move_origin)

    def test_a_note_drag_still_snaps_to_the_grid(self):
        self.view.set_tool("select")
        self._press(self.view.x_for_time(1000.0))
        self._move(self.view.x_for_time(1030.0))
        self._release(self.view.x_for_time(1030.0))
        # 125ms divisions: 30ms of drag rounds back rather than landing off-grid.
        self.assertTrue(any(n.time == 1000 for n in self.document.hit_objects))


class GimmickSVOffsetTests(_GimmickFixture, unittest.TestCase):
    """The normal chart SV layer's lead-in, and who agrees about it."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.target = self.window._gimmick_pairing.target
        self.document = self.window._states[self.target].document
        self.layers = {
            f.gimmick_layer: f.sv_view
            for f in self.window._gimmick_views if hasattr(f, "sv_view")
        }

    def _params(self, offset):
        return {
            "initial_rate": 1.0, "final_rate": 2.0, "placement": "notes", "snap_divisor": 4,
            "position_offset": offset, "omit_barline": False,
            "relative_to_final_bpm": False, "function": "linear",
        }

    def _shown(self, layer_id):
        return sorted(round(p.time) for p in self.layers[layer_id]._visible_points)

    def test_every_gimmick_sv_layer_starts_on_its_objects(self):
        """The -5ms lead-in is the Editor page's; in the gimmick editor even the
        normal chart's SV starts on the note, and a lead-in is asked for."""
        for layer_id in ("sv_chart", "sv_barline", "sv_fake_slider"):
            with self.subTest(layer_id):
                self.assertEqual(self.window._gimmick_config(layer_id).sv_offset_ms, 0)
        self.assertEqual(gui.SVFunctionDialog.DEFAULT_POSITION_OFFSET_MS, -5)

    def test_an_offset_sweep_still_belongs_to_the_layer_that_made_it(self):
        self.window._gimmick_config("sv_chart").sv_offset_ms = -5
        self.window._generate_sv(self.target, 0, 4000, self._params(-5), "sv_chart")
        shown = self._shown("sv_chart")
        self.assertIn(995, shown, "generated at note - 5 and still owned")
        self.assertNotIn(1000, shown)

    def test_changing_the_offset_moves_what_the_layer_looks_for(self):
        self.window._gimmick_config("sv_chart").sv_offset_ms = -20
        self.window._generate_sv(self.target, 0, 4000, self._params(-20), "sv_chart")
        self.assertIn(980, self._shown("sv_chart"))

    def test_the_dialog_keeps_the_offset_row_in_a_gimmick_layer(self):
        dialog = gui.SVFunctionDialog(0, 4000, self.window, gimmick_layer=True, position_offset=-7)
        self.assertEqual(dialog.parameters()["position_offset"], -7)
        dialog.deleteLater()

    def test_both_the_note_and_its_lead_in_are_reachable_by_hand(self):
        """The layer owns its objects' own milliseconds as well as the offset
        ones, so a click can land on either -- and, more to the point, an SV
        line a mapper already put on a note is still visible here."""
        # A lead-in is opt-in in the gimmick editor now (see
        # test_every_gimmick_sv_layer_starts_on_its_objects).
        self.window._gimmick_config("sv_chart").sv_offset_ms = -5
        self.window._refresh_gimmick_views()
        layer = self.layers["sv_chart"]
        layer.resize(800, 120)
        layer.current_time = 1000.0
        layer.window_ms = 200.0
        self.assertEqual(layer._placement_time(layer.x_for_time(1000.0)), 1000.0)
        self.assertEqual(layer._placement_time(layer.x_for_time(996.0)), 995.0)

    def test_sv_already_sitting_on_a_note_stays_visible(self):
        self.document.timing_points.append(gui.TimingPoint.inherited_at(1000, 1.4))
        self.window._refresh_gimmick_views()
        self.assertIn(1000, self._shown("sv_chart"))


class BarlineNoteGuardTests(_GimmickFixture, unittest.TestCase):
    """Red line generation stays off the notes' own milliseconds by default."""

    def setUp(self) -> None:
        super().setUp()
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.base = self.window._gimmick_pairing.base_timing

    def _dialog(self, notes):
        return gui.BarlineFunctionDialog(
            1000, 1010, self.window, base_timing=self.base, snap_divisor=4, note_times=notes,
        )

    def test_a_millisecond_with_a_note_is_skipped_by_default(self):
        dialog = self._dialog({1002, 1005})
        times = dialog.times()
        dialog.deleteLater()
        self.assertNotIn(1002, times)
        self.assertNotIn(1005, times)
        self.assertEqual(len(times), 9)

    def test_the_option_lets_them_through(self):
        dialog = self._dialog({1002, 1005})
        dialog.allow_on_notes_check.setChecked(True)
        times = dialog.times()
        dialog.deleteLater()
        self.assertIn(1002, times)
        self.assertEqual(len(times), 11)

    def test_the_count_label_follows_the_option(self):
        dialog = self._dialog({1002, 1005})
        blocked = dialog.count_label.text()
        dialog.allow_on_notes_check.setChecked(True)
        self.assertNotEqual(dialog.count_label.text(), blocked)
        dialog.deleteLater()

    def test_the_generator_asks_about_real_notes_only(self):
        """A fake slider's millisecond is not a note's -- the red line there is
        the structure that makes it fake."""
        self.window._place_gimmick("fake_slider", "regular", 10000)
        offset = self.window._gimmick_config("fake_slider").fake_slider_offset_ms
        document = self.window._states[self.window._gimmick_pairing.target].document
        seen = {}

        class _Stub:
            def __init__(self, start, end, parent=None, base_timing=None, snap_divisor=4,
                         note_times=None, current_sv=1.0, meter=4):
                seen["note_times"] = note_times
            def exec(self): return 0
            def times(self): return []
            def sv_multiplier(self): return None
            def deleteLater(self): pass

        with patch.object(gui, "BarlineFunctionDialog", _Stub):
            self.window._generate_barlines(self.window._gimmick_pairing.target, 0, 100)

        self.assertNotIn(10000 + offset, seen["note_times"])
        self.assertIn(1000, seen["note_times"])
        self.assertTrue(any(n.time == 10000 + offset for n in document.hit_objects))


class GimmickReferenceReselectTests(_GimmickFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.other = Path(self._temp.name) / "reference.osu"
        self.other.write_bytes(self.path.read_bytes().replace(b"333.333333333333", b"500.0"))
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self._enter(gui.GimmickEntryDialog.USE_CURRENT)
        self.pairing = self.window._gimmick_pairing

    def _reselect(self, name):
        captured = {}

        def fake_get_item(parent, title, label, items, index, editable):
            captured["items"] = list(items)
            captured["index"] = index
            return name, True

        with patch.object(gui.QInputDialog, "getItem", staticmethod(fake_get_item)):
            self.window._change_gimmick_reference()
        return captured

    def test_the_list_includes_the_difficulty_being_edited(self):
        captured = self._reselect(self.pairing.target.name)
        self.assertIn(self.pairing.target.name, captured["items"])
        self.assertIn("reference.osu", captured["items"])
        self.assertEqual(self.pairing.reference, self.pairing.target)

    def test_choosing_another_difficulty_re_anchors_every_layer(self):
        self._reselect("reference.osu")

        self.assertEqual(self.pairing.reference, self.other)
        self.assertEqual(self.pairing.base_timing[0].beat_length, 500.0)
        for frame in self.window._gimmick_views:
            view = getattr(frame, "chart_view", None) or getattr(frame, "sv_view", None)
            self.assertEqual(view.snap_points[0].beat_length, 500.0)

    def test_the_new_answer_is_remembered(self):
        self._reselect("reference.osu")
        reloaded = gui.load_index(self.index_path)
        self.assertEqual(reloaded[gui.index_key(self.path)].reference, self.other)

    def test_the_status_says_which_file_the_grid_comes_from(self):
        self._reselect("reference.osu")
        self.assertIn("reference.osu", self.window.gimmick_status.text())


class LibraryRescanTests(unittest.TestCase):
    """Rescan throws the index away instead of trusting it."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.window = gui.MainWindow()
        self.addCleanup(_delete_window, self.window)
        self.cache = Path(self._temp.name) / "song_index.json"
        self.window._library_cache_path = lambda: self.cache

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def test_the_cached_index_file_is_deleted(self):
        self.cache.write_text("{}", encoding="utf-8")
        self.window._library_cache = {"stale": []}
        self.window._library_songs = {Path("x"): []}
        self.window._listed_paths = {Path("x/y.osu")}

        self.window._library.rescan()

        self.assertFalse(self.cache.exists())
        self.assertEqual(self.window._library_cache, {})
        self.assertEqual(self.window._listed_paths, set())

    def test_a_missing_cache_file_is_not_an_error(self):
        self.window._library.rescan()
        self.assertEqual(self.window._library_cache, {})


if __name__ == "__main__":
    unittest.main()
