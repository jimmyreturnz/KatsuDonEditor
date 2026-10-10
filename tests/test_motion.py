"""motion.py: the living rim's clock, the tab sheen and the Left/Right morph."""
from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QRect
from PySide6.QtGui import QEnterEvent, QImage, QPainter
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from ui import motion

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class RimClockTests(unittest.TestCase):
    def test_it_ticks_only_while_something_is_shown(self):
        clock = motion.RimClock.shared()
        widget = QWidget()
        calls = []
        with mock.patch.object(motion, "reduced_motion", return_value=False):
            motion.tick_while_shown(widget, lambda: calls.append(1))
            self.assertFalse(clock._timer.isActive(), "not shown yet")
            widget.show()
            self.assertTrue(clock._timer.isActive())
            widget.hide()
            self.assertFalse(clock._timer.isActive())

    def test_a_still_rim_is_drawn_in_one_place(self):
        """Under reduced motion the lights stay put rather than vanishing."""
        with mock.patch.object(motion, "reduced_motion", return_value=True):
            self.assertEqual(motion.RimClock.shared().phase(motion.RIM_LAP_MS), 0.0)

    def test_the_rim_paints_both_ways(self):
        image = QImage(120, 40, QImage.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        motion.paint_living_rim(painter, QRectF(0, 0, 120, 40), 4, 1.5, True)
        motion.paint_living_rim(painter, QRectF(0, 0, 120, 40), 4, 1.5, False)
        painter.end()
        self.assertGreater(image.pixelColor(60, 0).alpha(), 0, "the rim is on the edge")
        self.assertEqual(image.pixelColor(60, 20).alpha(), 0, "and nothing inside")


class HoverSheenTests(unittest.TestCase):
    def test_the_glow_follows_the_pointer(self):
        button = QPushButton("Gimmick")
        button.resize(100, 30)
        sheen = motion.HoverSheen(button)
        with mock.patch.object(motion, "reduced_motion", return_value=True):
            QApplication.sendEvent(button, QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5)))
            self.assertEqual(sheen.glow, 1.0)
            QApplication.sendEvent(button, QEvent(QEvent.Leave))
            self.assertEqual(sheen.glow, 0.0)
        self.assertEqual(sheen.overlay.geometry(), button.rect())


class MorphRectTests(unittest.TestCase):
    def test_it_flies_between_the_two_rects_and_hides(self):
        parent = QWidget()
        parent.resize(400, 300)
        morph = motion.MorphRect(parent)
        with mock.patch.object(motion, "reduced_motion", return_value=False):
            morph.fly(QRect(0, 0, 100, 40), QRect(200, 100, 160, 40))
        self.assertTrue(morph.isVisibleTo(parent))
        morph._step(0.0)
        self.assertEqual(morph.current_rect(), QRectF(0, 0, 100, 40))
        morph._step(1.0)
        self.assertAlmostEqual(morph.current_rect().left(), 200, places=3)
        morph._animation.stop()

    def test_reduced_motion_does_not_fly(self):
        parent = QWidget()
        morph = motion.MorphRect(parent)
        with mock.patch.object(motion, "reduced_motion", return_value=True):
            morph.fly(QRect(0, 0, 100, 40), QRect(200, 100, 160, 40))
        self.assertFalse(morph.isVisibleTo(parent))


class RowEntranceTests(unittest.TestCase):
    def test_rows_land_in_order_and_settle(self):
        from PySide6.QtWidgets import QListWidget

        rows = QListWidget()
        rows.addItems([str(n) for n in range(30)])
        rows.resize(200, 300)
        rows.show()
        entrance = motion.RowEntrance(rows, row_ms=200, stagger_ms=22)
        with mock.patch.object(motion, "reduced_motion", return_value=False):
            entrance.start()
        self.assertTrue(entrance.running)
        self.assertLessEqual(entrance.progress(5), entrance.progress(0), "a later row rises later")
        entrance._timer.stop()
        self.assertEqual(entrance.progress(5), 1.0, "and a finished run draws every row settled")

    def test_reduced_motion_never_starts(self):
        from PySide6.QtWidgets import QListWidget

        rows = QListWidget()
        rows.show()
        entrance = motion.RowEntrance(rows)
        with mock.patch.object(motion, "reduced_motion", return_value=True):
            entrance.start()
        self.assertFalse(entrance.running)


class SegmentGlideTests(unittest.TestCase):
    def test_the_pill_lands_on_the_checked_button(self):
        from PySide6.QtWidgets import QButtonGroup, QHBoxLayout

        frame = QWidget()
        layout = QHBoxLayout(frame)
        buttons = [QPushButton(text) for text in ("A", "B", "C")]
        group = QButtonGroup(frame)
        for button in buttons:
            button.setCheckable(True)
            group.addButton(button)
            layout.addWidget(button)
        buttons[0].setChecked(True)
        glide = motion.SegmentGlide(frame, buttons)
        frame.show()
        QApplication.processEvents()
        with mock.patch.object(motion, "reduced_motion", return_value=True):
            buttons[2].setChecked(True)
        self.assertEqual(glide.current_rect(), QRectF(buttons[2].geometry()))


class PlayPauseGlyphTests(unittest.TestCase):
    def test_it_morphs_to_pause_and_back(self):
        button = QPushButton()
        glyph = motion.PlayPauseGlyph(button, "#aeb8c5", "#e8edf3")
        with mock.patch.object(motion, "reduced_motion", return_value=True):
            glyph.set_playing(True)
            self.assertEqual(glyph.progress, 1.0)
            glyph.set_playing(False)
            self.assertEqual(glyph.progress, 0.0)
        # Every corner has a partner: the two shapes morph point for point.
        self.assertEqual([len(quad) for quad in motion.PlayPauseGlyph.PLAY],
                         [len(quad) for quad in motion.PlayPauseGlyph.PAUSE])


if __name__ == "__main__":
    unittest.main()
