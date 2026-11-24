"""Combo numbers under the chart view's notes: osu!'s editor numbering, thinned
to whatever the zoom has room for, and never on a gimmick layer."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from ui import chart_views
from ui.common import combo_numbers
from osu_io.parser import parse_osu
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ComboStepTests(unittest.TestCase):
    def test_every_note_when_there_is_room(self):
        self.assertEqual(chart_views.combo_step(40.0, 20.0), 1)

    def test_the_smallest_step_whose_labels_fit(self):
        self.assertEqual(chart_views.combo_step(3.0, 20.0), 10)  # 5 x 3 = 15 < 20
        self.assertEqual(chart_views.combo_step(4.0, 20.0), 5)


class ChartViewComboTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.document = parse_osu(write_fixture(Path(self._temp.name), "full_v14"))

    def tearDown(self) -> None:
        chart_views.set_show_combo(True)
        self._temp.cleanup()

    def _view(self, object_filter=None):
        view = chart_views.TimelineGameplay()
        view.object_filter = object_filter
        view.resize(1600, 200)
        view.load_document(self.document)
        return view

    def test_the_chart_view_numbers_as_a_timestamp_does(self):
        view = self._view()
        self.assertEqual(view.note_combo,
                         [n for n, _ in combo_numbers(self.document.hit_objects)])

    def test_a_gimmick_layer_has_none(self):
        self.assertEqual(self._view(lambda note: True).note_combo, [])

    def _labels_drawn(self, view) -> int:
        drawn = []
        real = chart_views.QPainter.drawText
        image = QImage(view.size(), QImage.Format_ARGB32_Premultiplied)
        chart_views.QPainter.drawText = lambda painter, *args: drawn.append(args[-1])
        try:
            view.render(image)
        finally:
            chart_views.QPainter.drawText = real
        return len([text for text in drawn if str(text).isdigit()])

    def test_the_setting_turns_them_off(self):
        view = self._view()
        view.current_time = self.document.hit_objects[0].time
        self.assertGreater(self._labels_drawn(view), 0)
        chart_views.set_show_combo(False)
        self.assertEqual(self._labels_drawn(view), 0)


if __name__ == "__main__":
    unittest.main()
