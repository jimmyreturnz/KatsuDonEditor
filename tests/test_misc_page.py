"""The Miscellaneous page (owner, 2026-10-18): metadata, offset shift,
scale every green line."""
from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from osu_io import metadata
from osu_io.parser import parse_osu
from ui import gui, misc_page
from tests.test_editing_ux_round3 import WindowTestCase


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class PureTests(unittest.TestCase):
    def test_bookmarks_and_preview_move_and_no_preview_stays(self):
        lines = ["[General]\n", "PreviewTime: 1000\n", "\n", "[Editor]\n", "Bookmarks: 10,20\n", "\n"]
        out = misc_page.shifted_lines(lines, 5, "\n")
        self.assertEqual(metadata.read_keys(out, "Editor")["Bookmarks"], "15,25")
        self.assertEqual(metadata.read_keys(out, "General")["PreviewTime"], "1005")
        none = misc_page.shifted_lines(["[General]\n", "PreviewTime: -1\n"], 5, "\n")
        self.assertEqual(metadata.read_keys(none, "General")["PreviewTime"], "-1")


class MetadataValuesTests(unittest.TestCase):
    """The owner's osu-metadata rules."""

    def test_a_blank_box_is_left_alone(self):
        self.assertEqual(misc_page.metadata_values({"Source": "  ", "Tags": "a"}), {"Tags": "a"})

    def test_tags_are_one_space_separated_run(self):
        self.assertEqual(misc_page.metadata_values({"Tags": " a\n b   c "}), {"Tags": "a b c"})


class MiscPageTests(WindowTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.window._switch_page(gui.PAGE_MISC)
        self.page = self.window.misc_page

    def test_the_rail_switches_pages(self):
        """Owner, 2026-10-18: every section fit on screen, so a rail click
        scrolled nowhere and looked dead."""
        sheet = self.page.sheet
        sheet._rail_buttons["offset"].click()
        self.assertEqual([key for key, s in sheet.sections.items() if not s.isHidden()], ["offset"])
        self.assertTrue(sheet._rail_buttons["offset"].isChecked())
        self.assertNotIn("hitsounds", sheet.sections)

    def test_tags_over_the_limit_cannot_be_applied(self):
        self.page.fields["Tags"].setText("x" * 1000)
        self.assertIn("1000 / 1000", self.page.tags_count.text())
        self.assertTrue(self.page.apply_metadata_button.isEnabled())
        self.page.fields["Tags"].setText("x" * 1001)
        self.assertFalse(self.page.apply_metadata_button.isEnabled())

    def test_a_blank_source_does_not_blank_the_file(self):
        before = metadata.read_keys(self.state.document.lines, "Metadata").get("Creator")
        self.page.fields["Creator"].setText("")
        self.page.fields["Title"].setText("Only Title")
        self.page.apply_metadata()
        after = metadata.read_keys(self.state.document.lines, "Metadata")
        self.assertEqual(after.get("Creator"), before)
        self.assertEqual(after["Title"], "Only Title")

    def test_metadata_is_saved_and_undoable(self):
        self.page.fields["Title"].setText("New Title")
        self.page.fields["Tags"].setText("a b c")
        self.page.apply_metadata()
        values = metadata.read_keys(self.state.document.lines, "Metadata")
        self.assertEqual((values["Title"], values["Tags"]), ("New Title", "a b c"))
        self.assertTrue(self.state.history.dirty)
        self.window._save_states([self.state])
        self.assertEqual(metadata.read_keys(parse_osu(self.path).lines, "Metadata")["Title"], "New Title")
        self.state.history.undo(self.state)
        self.assertNotEqual(metadata.read_keys(self.state.document.lines, "Metadata").get("Title"), "New Title")

    def test_shift_moves_everything_and_undoes_exactly(self):
        notes = [n.time for n in self.state.document.hit_objects]
        points = [p.time for p in self.state.document.timing_points]
        self.page.offset_spin.setValue(25)
        self.page.shift_offset()
        self.assertEqual([n.time for n in self.state.document.hit_objects], [t + 25 for t in notes])
        self.assertEqual([p.time for p in self.state.document.timing_points], [t + 25 for t in points])
        self.state.history.undo(self.state)
        self.assertEqual([n.time for n in self.state.document.hit_objects], notes)
        self.assertEqual([p.time for p in self.state.document.timing_points], points)



if __name__ == "__main__":
    unittest.main()
