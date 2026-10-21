"""The Miscellaneous page (owner, 2026-10-18): metadata, offset shift,
scale every green line, Taiko to Mania, the Mappack Creator."""
from __future__ import annotations

import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from library import mappack
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

    def test_a_bookmark_that_is_not_a_number_stays_and_does_not_raise(self):
        out = misc_page.shifted_lines(["[General]\n", "PreviewTime: ²\n", "\n",
                                       "[Editor]\n", "Bookmarks: 10,x,1.5,--5,²,-20\n"], 5, "\n")
        self.assertEqual(metadata.read_keys(out, "Editor")["Bookmarks"], "15,x,1.5,--5,²,-15")
        self.assertEqual(metadata.read_keys(out, "General")["PreviewTime"], "²")


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

    def test_converting_to_mania_writes_a_new_difficulty_beside_the_open_one(self):
        self.page.mania_keys.setCurrentIndex(self.page.mania_keys.findData(1))
        self.page.convert_to_mania()
        written = [p for p in self.path.parent.glob("*.osu") if p != self.path]
        self.assertEqual(len(written), 1)
        self.assertEqual(metadata.read_keys(parse_osu(written[0]).lines, "General")["Mode"], "3")
        self.assertTrue((self.path.parent / "taiko-normal-hitnormal.wav").is_file())

    def test_a_difficulty_is_added_to_the_pack_once(self):
        toasts = []
        self.window.show_toast = toasts.append
        self.page.add_difficulty(self.path)
        self.page.add_difficulty(self.path)
        self.assertEqual(self.page.pack_table.rowCount(), 1)
        self.assertEqual(len(toasts), 2)
        self.assertFalse(self.page.pack_create.isEnabled())  # no title, no creator yet
        self.page.pack_title.setText("Pack")
        self.page.pack_creator.setText("me")
        self.assertTrue(self.page.pack_create.isEnabled())
        self.assertTrue(self.page.pack_delete.isChecked())  # on by default
        self.assertEqual(self.page.pack_count.format(), f"2 / {mappack.DIFFICULTY_LIMIT}")

    def test_tags_are_collected_for_the_delete_difficulty_and_counted(self):
        self.window.show_toast = lambda _: None
        values = metadata.read_keys(self.state.document.lines, "Metadata")
        self.page.add_difficulty(self.path)
        expected = mappack.merge_tags(values.get("Tags", ""), values.get("Creator", ""))
        self.assertEqual(self.page.pack_tags.text(), expected)
        self.page.add_difficulty(self.path)  # refused: nothing added twice
        self.assertEqual(self.page.pack_tags.text(), expected)
        self.page.pack_title.setText("Pack")
        self.page.pack_creator.setText("me")
        self.assertTrue(self.page.pack_create.isEnabled())
        self.page.pack_tags.setText("x" * 1001)
        self.assertIn("1001 / 1000", self.page.pack_tags_count.text())
        self.assertFalse(self.page.pack_create.isEnabled())
        self.page.pack_delete.setChecked(False)  # no Delete, so no limit to keep
        self.assertTrue(self.page.pack_create.isEnabled())

    def test_a_loaded_pack_takes_new_difficulties_and_updates(self):
        from unittest import mock
        self.window.show_toast = lambda _: None
        import tempfile
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        songs = Path(temporary.name)
        self.window.settings.set_value("library/songs_folder", str(songs))
        self.page.add_difficulty(self.path)
        self.page.pack_title.setText("Pack")
        self.page.pack_creator.setText("me")
        self.assertFalse(self.page.pack_update.isEnabled())  # nothing loaded
        self.page.pack_tags.setText("hand edited")
        self.page.create_pack()
        folder = songs / "Various Artists - Pack"
        self.page.pack_tags.setText("")
        self.page.load_pack(folder)
        self.assertEqual(self.page.pack_title.text(), "Pack")
        self.assertEqual(self.page.pack_table.rowCount(), 1)
        self.assertIn("hand", self.page.pack_tags.text().split())
        self.page.pack_title.setText("Pack Two")
        with mock.patch.object(mappack.QFile, "moveToTrash",
                               side_effect=lambda p: __import__("shutil").rmtree(p) is None):
            self.page.create_pack(update=True)
        self.assertFalse(folder.exists())
        self.assertEqual(self.page.pack_source, (songs / "Various Artists - Pack Two").resolve())
        self.assertEqual(self.page.pack_table.rowCount(), 1)

    def test_a_tags_only_edit_updates_the_delete_difficulty_and_stays_removed(self):
        from unittest import mock
        import shutil, tempfile
        self.window.show_toast = lambda _: None
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        songs = Path(temporary.name)
        self.window.settings.set_value("library/songs_folder", str(songs))
        self.page.add_difficulty(self.path)
        self.page.pack_title.setText("Pack")
        self.page.pack_creator.setText("me")
        self.page.create_pack()
        folder = songs / "Various Artists - Pack"
        self.page.load_pack(folder)
        dropped = self.page.pack_tags.text().split()[0]  # one of the difficulty's own
        self.page.pack_tags.setText(" ".join(self.page.pack_tags.text().split()[1:] + ["new"]))
        with mock.patch.object(mappack.QFile, "moveToTrash", side_effect=lambda p: shutil.rmtree(p) is None):
            self.page.create_pack(update=True)
        delete = folder / "Various Artists - Pack (me) [Delete].osu"
        written = metadata.read_keys(parse_osu(delete).lines, "Metadata")["Tags"].split()
        self.assertIn("new", written)
        self.assertNotIn(dropped, written)
        self.assertNotIn(dropped, self.page.pack_tags.text().split())  # reloaded as written

    def test_only_the_packs_own_box_asks_for_a_drop(self):
        self.assertIn("Select a difficulty", self.page.pack_original.text())
        self.assertIn("Drop", self.page.pack_well.text())

    def test_remove_duplicates_ignores_capitals_and_keeps_the_first(self):
        self.page.pack_tags.setText("Taiko taiko\nOni TAIKO oni x")
        self.page.pack_dedup.click()
        self.assertEqual(self.page.pack_tags.text(), "Taiko Oni x")

    def test_the_selected_rows_own_background_shows_beside_the_packs(self):
        self.window.show_toast = lambda _: None
        self.page.add_difficulty(self.path)
        self.page.pack_table.setCurrentCell(0, 0)
        own = next(iter(self.page._pack_info[self.page.pack_entries[0].path][2]), None)
        if own is None:
            self.assertIn("no background", self.page.pack_original.text())
        else:
            self.assertFalse(self.page.pack_original.pixmap().isNull())
        self.page._set_pack_background(self.path)  # not an image: the well stays empty
        self.assertIn("on the right", self.page.pack_original.text())

    def test_tags_boxes_are_tall_enough_for_the_whole_allowance(self):
        for box in (self.page.pack_tags, self.page.fields["Tags"]):
            box.resize(400, 40)
            box.fit_height()  # hidden, its resizeEvent waits for show()
            per_line = box.viewport().width() // box.fontMetrics().averageCharWidth()
            self.assertGreaterEqual(box.minimumHeight(), 1000 // per_line * box.fontMetrics().lineSpacing())

    def test_osus_current_difficulty_converts_without_opening_it(self):
        from unittest import mock
        self.page.mania_keys.setCurrentIndex(self.page.mania_keys.findData(1))
        with mock.patch.object(self.page, "_osu_current_path", return_value=self.path):
            self.page.convert_osu_to_mania()
        written = [p for p in self.path.parent.glob("*.osu") if p != self.path]
        self.assertEqual(len(written), 1)

    def test_typing_the_title_reads_no_difficulty_again(self):
        """Owner, 2026-10-21: typing lagged, because every key re-parsed
        every difficulty in the pack (28ms each)."""
        self.window.show_toast = lambda _: None
        self.page.add_difficulty(self.path)
        from unittest import mock
        with mock.patch.object(misc_page, "parse_osu", side_effect=AssertionError("parsed")):
            self.page.pack_title.setText("Pack")
            self.page.pack_delete.setChecked(False)

    def test_song_selects_shortcut_adds_the_selected_difficulty(self):
        added = []
        self.page.add_difficulty = added.append
        library = self.window._library
        library.difficulty_list.addItem("x")
        item = library.difficulty_list.item(library.difficulty_list.count() - 1)
        item.setData(gui.Qt.UserRole, str(self.path))
        library.difficulty_list.setCurrentItem(item)
        library.add_selected_to_mappack()
        self.assertEqual([str(p) for p in added], [str(self.path)])



if __name__ == "__main__":
    unittest.main()
