"""Scale SV: the selected green lines times a factor, plus an amount
(owner, 2026-10-18) -- a pasted effect wanted at 0.9x of itself."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from ui import dialogs, gui
from tests.test_editing_ux_round3 import WindowTestCase


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ScaleSvTests(WindowTestCase):
    def _scale(self, factor, add=0.0):
        class Answered(dialogs.ScaleSvDialog):
            def exec(self):
                self.factor_spin.setValue(factor)
                self.add_spin.setValue(add)
                return 1

        with patch.object(gui, "ScaleSvDialog", Answered):
            self.window.scale_selected_sv()

    def test_selected_greens_are_scaled_as_one_undo_step(self):
        view = self._sv_view()
        self.window._editor_view_focus_changed(None, view)
        greens = [p for p in self.state.document.timing_points if not p.uninherited][:2]
        self.assertEqual(len(greens), 2)
        before = [p.sv_multiplier for p in greens]
        view.selected_uids = {p.uid for p in greens}
        steps = len(self.state.history.undo_stack)
        self._scale(0.9, 0.05)
        for point, old in zip(greens, before):
            self.assertAlmostEqual(point.sv_multiplier, old * 0.9 + 0.05)
        self.assertEqual(len(self.state.history.undo_stack), steps + 1)
        self.window.undo()
        self.assertEqual([p.sv_multiplier for p in greens], before)

    def test_nothing_selected_is_a_toast_not_a_dialog(self):
        view = self._sv_view()
        self.window._editor_view_focus_changed(None, view)
        view.selected_uids = set()
        with patch.object(gui, "ScaleSvDialog", side_effect=AssertionError("opened")):
            self.window.scale_selected_sv()

    def test_the_scale_tool_scales_what_its_drag_selects(self):
        """Owner, 2026-10-19: Scale SV is a fourth tool, a selection of its own."""
        view = self._sv_view()
        self.window._editor_view_focus_changed(None, view)
        self.assertIn("scale", self.window.SV_TOOLS_DEFAULT)
        self.window.sv_tool_buttons["scale"].setChecked(True)
        self.assertEqual(view.tool, "scale")
        greens = [p for p in self.state.document.timing_points if not p.uninherited][:2]
        before = [p.sv_multiplier for p in greens]
        view.selected_uids = {p.uid for p in greens}
        view.select_anchor_time = greens[0].time
        view.select_mouse_x = view.x_for_time(greens[-1].time)

        class Answered(dialogs.ScaleSvDialog):
            def exec(self):
                self.factor_spin.setValue(2.0)
                return 1

        release = QMouseEvent(QEvent.MouseButtonRelease, QPointF(view.select_mouse_x, 10), QPointF(),
                              Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        with patch.object(gui, "ScaleSvDialog", Answered):
            view.mouseReleaseEvent(release)
        for point, old in zip(greens, before):
            self.assertAlmostEqual(point.sv_multiplier, min(old * 2.0, 100.0))

    def test_the_result_is_clamped(self):
        dialog = dialogs.ScaleSvDialog(1)
        dialog.factor_spin.setValue(0.01)
        dialog.add_spin.setValue(-5)
        self.assertEqual(dialog.scaled(1.0), 0.01)
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
