"""The Editor/Gimmick pages' map backdrop, and right click on the empty space
under the views opening Add view."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from ui import gui

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    from app import settings as settings_module

    settings_module.APPLICATION_NAME = "TaikoFancyArrangerTests"
    _APP = QApplication.instance() or QApplication([])


class BackdropTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.picture = Path(self._temp.name) / "bg.png"
        image = QImage(40, 20, QImage.Format_RGB32)
        image.fill(QColor("#ffffff"))
        image.save(str(self.picture))
        self.backdrop = gui.MapBackdrop()
        self.backdrop.resize(100, 100)

    def tearDown(self):
        self._temp.cleanup()

    def pixel(self) -> QColor:
        return self.backdrop.grab().toImage().pixelColor(50, 50)

    def test_no_map_is_plain_navy(self):
        self.assertEqual(self.pixel(), gui.MapBackdrop.ground())

    def test_the_picture_is_faded_by_the_opacity(self):
        self.backdrop.set_background(self.picture)
        self.backdrop.set_opacity(0)
        self.assertEqual(self.pixel(), gui.MapBackdrop.ground())
        self.backdrop.set_opacity(50)
        faded = self.pixel()
        navy = gui.MapBackdrop.ground()
        self.assertAlmostEqual(faded.red(), (navy.red() + 255) / 2, delta=2)

    def test_a_paint_is_one_blit_until_something_changes(self):
        bakes = []
        original = self.backdrop._bake
        self.backdrop._bake = lambda: bakes.append(1) or original()
        self.backdrop.set_background(self.picture)
        for _ in range(3):
            self.backdrop.grab()
        self.assertEqual(len(bakes), 1)
        self.backdrop.set_opacity(25)  # unchanged: the default
        self.backdrop.grab()
        self.assertEqual(len(bakes), 1)
        self.backdrop.set_opacity(60)
        self.backdrop.grab()
        self.backdrop.resize(120, 90)
        self.backdrop.grab()
        self.assertEqual(len(bakes), 3)


class ViewOpacityTests(unittest.TestCase):
    """Settings > View opacity: how much of a view covers the backdrop."""

    def tearDown(self):
        gui.set_view_opacity(100, [])

    def _rendered_over_white(self, percent):
        holder = self.holder = QWidget()  # kept: it owns the lane
        holder.setAutoFillBackground(True)
        palette = holder.palette()
        palette.setColor(holder.backgroundRole(), QColor("#ffffff"))
        holder.setPalette(palette)
        holder.resize(300, 120)
        frame = gui.EditorViewFrame("sv", "Oni")
        lane = gui.SVEditorView()
        frame.set_content(lane)
        frame.setParent(holder)
        frame.resize(300, 120)
        gui.set_view_opacity(percent, [frame, lane])
        image = holder.grab().toImage()
        return lane, image.pixelColor(lane.mapTo(holder, QPoint(150, 5)))

    def test_full_opacity_is_the_opaque_fast_path(self):
        lane, colour = self._rendered_over_white(100)
        self.assertTrue(lane.testAttribute(gui.Qt.WA_OpaquePaintEvent))
        self.assertLess(colour.lightness(), 80, "a solid lane hides the white")

    def test_zero_lets_the_backdrop_through(self):
        lane, colour = self._rendered_over_white(0)
        self.assertFalse(lane.testAttribute(gui.Qt.WA_OpaquePaintEvent))
        self.assertGreater(colour.lightness(), 200, "the white shows through")

    def test_views_sit_edge_to_edge(self):
        frame = gui.EditorViewFrame("chart", "Oni")
        self.assertIn("border-radius: 0", frame.styleSheet())
        self.assertEqual(frame.layout().contentsMargins().top(), 0)


class AddViewRightClickTests(unittest.TestCase):
    def test_only_the_empty_space_opens_add_view(self):
        container = QWidget()
        container.resize(200, 200)
        view = QPushButton("a view", container)
        view.setGeometry(0, 0, 200, 50)
        opened = []
        gui.add_view_on_empty_right_click(container, lambda: opened.append(1))
        container.customContextMenuRequested.emit(QPoint(10, 20))
        self.assertEqual(opened, [], "a right click on a view")
        container.customContextMenuRequested.emit(QPoint(10, 150))
        self.assertEqual(opened, [1])

    def test_a_gap_between_views_is_not_empty_space(self):
        """Owner's report: a right click meant for a view opened Add view.
        Between two views there is no child under the point, and that was
        the whole test."""
        container = QWidget()
        container.resize(200, 200)
        for top in (0, 60):
            view = QPushButton("a view", container)
            view.setGeometry(0, top, 200, 50)
            view.show()
        opened = []
        gui.add_view_on_empty_right_click(container, lambda: opened.append(1))
        container.customContextMenuRequested.emit(QPoint(10, 55))
        self.assertEqual(opened, [])
        container.customContextMenuRequested.emit(QPoint(10, 150))
        self.assertEqual(opened, [1])

    def test_a_right_click_mid_drag_is_not_a_request(self):
        """Owner, 2026-10-08: a selection box dragged below the last view
        put the pointer on the empty space with the left button down, and a
        right click there opened Add view in the middle of the drag."""
        from unittest.mock import patch
        from PySide6.QtCore import Qt

        container = QWidget()
        container.resize(200, 200)
        opened = []
        gui.add_view_on_empty_right_click(container, lambda: opened.append(1))
        with patch.object(gui.QApplication, "mouseButtons", lambda: Qt.LeftButton):
            container.customContextMenuRequested.emit(QPoint(10, 150))
        self.assertEqual(opened, [])
        container.customContextMenuRequested.emit(QPoint(10, 150))
        self.assertEqual(opened, [1])


class WindowTests(unittest.TestCase):
    def test_both_pages_have_a_backdrop_and_see_through_chrome(self):
        window = gui.MainWindow()
        try:
            self.assertEqual(len(window._backdrops), 2)
            for scroll in (window.gimmick_scroll,):
                self.assertTrue(scroll.viewport().property("seeThrough"))
            self.assertTrue(window.editor_views_layout.parentWidget().property("seeThrough"))
            # A difficulty's group sits between its views and the backdrop:
            # painted navy, View opacity uncovered it rather than the map.
            from pathlib import Path
            group = window._difficulty_group_layout(Path("x.osu"), "x").parentWidget()
            self.assertTrue(group.property("seeThrough"))
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
