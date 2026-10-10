"""The settings dialog's sidebar search and its switch rows."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import gui
from settings_dialog import SettingsDialog

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class SettingsDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = gui.SettingsManager()
        self.dialog = SettingsDialog(settings, gui.ShortcutRegistry(settings))
        self.addCleanup(self.dialog.deleteLater)

    def _shown_pages(self) -> list[str]:
        nav = self.dialog.nav
        return [self.dialog._page_ids[row] for row in range(nav.count()) if not nav.item(row).isHidden()]

    def test_search_keeps_only_the_pages_that_mention_it(self) -> None:
        self.dialog.page_search.setText("opacity")
        self.assertEqual(self._shown_pages(), ["skin"])
        # The page showing was filtered out, so the match is shown instead.
        self.assertEqual(self.dialog._page_ids[self.dialog.pages.currentIndex()], "skin")
        self.dialog.page_search.setText("")
        self.assertEqual(len(self._shown_pages()), 6)

    def test_search_reads_switch_names_and_menu_choices(self) -> None:
        # A switch's label is drawn beside it, not on it.
        self.dialog.page_search.setText("check for updates on startup")
        self.assertEqual(self._shown_pages(), ["general"])
        self.dialog.page_search.setText("monokai")
        self.assertEqual(self._shown_pages(), ["skin"])

    def test_clicking_a_switch_row_flips_its_switch(self) -> None:
        check = self.dialog.confirm_overwrite
        before = check.isChecked()
        row = check.parentWidget()
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        QTest.mouseClick(row, Qt.LeftButton, pos=QPoint(10, 10))
        self.assertNotEqual(check.isChecked(), before)

    def test_a_clash_marks_both_chips_and_names_itself(self) -> None:
        from PySide6.QtGui import QKeySequence
        from PySide6.QtWidgets import QLineEdit

        editors = self.dialog._shortcut_editors
        undo, redo = editors["undo"], editors["redo"]
        redo.setKeySequence(undo.keySequence())
        self.assertTrue(undo.findChild(QLineEdit).property("warn"))
        self.assertTrue(redo.findChild(QLineEdit).property("warn"))
        self.assertFalse(self.dialog.validation_label.isHidden())
        redo.setKeySequence(QKeySequence("Ctrl+Shift+F12"))
        self.assertFalse(undo.findChild(QLineEdit).property("warn"))
        self.assertTrue(self.dialog.validation_label.isHidden())

    def test_reset_shows_only_once_a_key_differs_from_its_default(self) -> None:
        from PySide6.QtGui import QKeySequence
        from PySide6.QtWidgets import QToolButton

        editor = self.dialog._shortcut_editors["undo"]
        default = self.dialog.shortcuts.default_sequence("undo")
        editor.setKeySequence(QKeySequence(default))
        reset = editor.parentWidget().findChild(QToolButton, "shortcutReset")
        self.assertTrue(reset.isHidden())
        editor.setKeySequence(QKeySequence("Ctrl+Shift+F12"))
        self.assertFalse(reset.isHidden())
        reset.click()
        self.assertEqual(editor.keySequence(), QKeySequence(default))
        self.assertTrue(reset.isHidden())

    def test_hitsound_rows_follow_the_hitsounds_switch(self) -> None:
        self.dialog.hitsounds_enabled.setChecked(False)
        self.assertFalse(any(row.isEnabled() for row in self.dialog._hitsound_rows))
        self.dialog.hitsounds_enabled.setChecked(True)
        self.assertTrue(all(row.isEnabled() for row in self.dialog._hitsound_rows))


if __name__ == "__main__":
    unittest.main()
