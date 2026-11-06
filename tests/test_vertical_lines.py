"""`draw_vertical_lines` draws what drawLines would, as one stretched row."""
from __future__ import annotations

import unittest

from PySide6.QtCore import QLine, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication

from ui.common import draw_vertical_lines


def setUpModule() -> None:
    QApplication.instance() or QApplication([])


def _paint(body, antialiased: bool) -> QImage:
    image = QImage(200, 60, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#151b24"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, antialiased)
    body(painter)
    painter.end()
    return image


class VerticalLinesTests(unittest.TestCase):
    def test_every_grid_pen_matches_drawlines(self):
        """The snap grid's pens -- 1px and 2px, opaque and translucent, with
        ticks from either edge -- on columns that touch and repeat."""
        xs = [3, 4, 10, 10, 57, 58, 120, 199, 0]
        for pen in (QPen(QColor("#f2f2f2"), 2), QPen(QColor("#4b9cff"), 1),
                    QPen(QColor(255, 255, 255, 70), 1), QPen(QColor(225, 230, 240, 90), 1)):
            for y0, y1 in ((0, 32), (60, 28), (5, 40), (0, 60)):
                for antialiased in (False, True):
                    with self.subTest(pen=pen.color().name(QColor.HexArgb), width=pen.width(),
                                      span=(y0, y1), antialiased=antialiased):
                        def lines(painter):
                            painter.setPen(pen)
                            painter.drawLines([QLine(x, y0, x, y1) for x in xs])
                        expected = _paint(lines, antialiased)
                        got = _paint(lambda painter: draw_vertical_lines(painter, pen, xs, y0, y1), antialiased)
                        # Equal, but for 1/255 where an antialiased 2px line
                        # at x=0 is clipped by the device edge: the two clip
                        # paths round that column differently.
                        a, b = bytes(got.constBits()), bytes(expected.constBits())
                        self.assertLessEqual(max(abs(x - y) for x, y in zip(a, b)), 1)
                        if not antialiased:
                            self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
