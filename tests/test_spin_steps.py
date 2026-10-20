"""An arrow or a wheel notch moves a fraction by 0.01 and a BPM by 1.

QDoubleSpinBox's own step is 1.0, so any box that never set one jumped a
green line from 0.1x to 1.1x in one notch (owner, 2026-10-18).
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDoubleSpinBox

from ui import dialogs
from chart.gimmick_session import GimmickConfig
from osu_io.timing import TimingPoint


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class SpinStepTests(unittest.TestCase):
    def _assert_steps(self, dialog) -> None:
        spins = dialog.findChildren(QDoubleSpinBox)
        self.assertTrue(spins)
        for spin in spins:
            name = spin.objectName() or spin.suffix() or repr(spin)
            if "BPM" in spin.suffix():
                self.assertEqual(spin.singleStep(), 1.0, name)
            elif spin.decimals() > 0:
                self.assertLessEqual(spin.singleStep(), 0.01, name)
        dialog.deleteLater()

    def test_green_line_steps_by_a_hundredth(self):
        dialog = dialogs.TimingLineDialog(TimingPoint(1000, -1000.0, uninherited_flag=0))
        self.assertEqual(dialog.value_spin.singleStep(), 0.01)
        self._assert_steps(dialog)

    def test_red_line_steps_by_one_bpm(self):
        dialog = dialogs.TimingLineDialog(TimingPoint(1000, 500.0))
        self.assertEqual(dialog.value_spin.singleStep(), 1.0)
        dialog.deleteLater()

    def test_gimmick_dialogs(self):
        for layer in ("barline", "fake_slider"):
            self._assert_steps(dialogs.GimmickConfigDialog(GimmickConfig(), layer, GimmickConfig()))
            self._assert_steps(dialogs.ConvertNotesDialog(GimmickConfig(), layer))


if __name__ == "__main__":
    unittest.main()
