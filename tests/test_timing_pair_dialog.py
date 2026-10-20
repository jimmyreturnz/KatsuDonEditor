"""A red line and the green line on its millisecond open as one dialog
(owner, 2026-10-18): double clicking the shared millisecond only ever reached
the green line, and omitting the red line's barline meant dragging one aside.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from osu_io.timing import TimingPoint
from ui import dialogs, gui


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _red(time):
    return TimingPoint(time, 500.0)


def _green(time, sv):
    return TimingPoint.inherited_at(time, sv)


class _History:
    def push(self, command, state):
        command.apply(state)


class _Dialog(dialogs.TimingLineDialog):
    """The real dialog, its fields set by the test instead of a person."""
    setup = None

    def __init__(self, point, parent=None, partner=None):
        super().__init__(point, None, partner=partner)

    def exec(self):
        type(self).setup(self)
        return 1


class TimingPairTests(unittest.TestCase):
    def _edit(self, points, uid, setup):
        document = SimpleNamespace(timing_points=points)
        state = SimpleNamespace(document=document, history=_History())
        path = Path("x.osu")
        window = SimpleNamespace(
            _states={path: state}, _timing_pair=gui.MainWindow._timing_pair,
            _refresh_cycle=contextlib.nullcontext,
            _refresh_difficulty_views=lambda path: None, _refresh_difficulty_sv_views=lambda path: None)
        _Dialog.setup = setup
        with patch.object(gui, "TimingLineDialog", _Dialog):
            gui.MainWindow._edit_timing_line(window, path, uid)
        return document.timing_points

    def test_the_green_line_opens_its_red_line(self):
        red, green = _red(1000), _green(1000, 1.5)
        self.assertEqual(gui.MainWindow._timing_pair(SimpleNamespace(timing_points=[red, green]), green),
                         (red, green))

    def test_a_green_line_before_the_red_partners_nothing(self):
        green, red = _green(1000, 1.5), _red(1000)
        self.assertEqual(gui.MainWindow._timing_pair(SimpleNamespace(timing_points=[green, red]), red),
                         (red, None))

    def test_omit_barline_and_sv_from_the_green_line(self):
        red, green = _red(1000), _green(1000, 1.5)

        def setup(dialog):
            self.assertTrue(dialog.uninherited)
            self.assertEqual(dialog.sv_spin.value(), 1.5)
            dialog.omit_barline_check.setChecked(True)
            dialog.sv_spin.setValue(2.0)

        points = self._edit([red, green], green.uid, setup)
        self.assertTrue(red.omit_first_barline)
        self.assertAlmostEqual(green.sv_multiplier, 2.0)
        self.assertEqual(points, [red, green])

    def test_sv_on_a_bare_red_line_adds_a_green_after_it(self):
        red = _red(1000)
        points = self._edit([red], red.uid, lambda dialog: dialog.sv_spin.setValue(0.8))
        self.assertEqual(len(points), 2)
        self.assertIs(points[0], red)
        self.assertFalse(points[1].uninherited)
        self.assertEqual(points[1].time, 1000)
        self.assertAlmostEqual(points[1].sv_multiplier, 0.8)

    def test_one_x_on_a_bare_red_line_adds_nothing(self):
        red = _red(1000)
        self.assertEqual(self._edit([red], red.uid, lambda dialog: None), [red])

    def test_a_retime_keeps_the_green_after_the_red(self):
        for target in (3000, 200):
            red, green = _red(1000), _green(1000, 1.5)
            other = _green(2000, 1.0)
            points = self._edit([red, green, other], red.uid,
                                lambda dialog: dialog.time_spin.setValue(target))
            moved = [p for p in points if p.time == target]
            self.assertEqual(moved, [red, green], target)


if __name__ == "__main__":
    unittest.main()
