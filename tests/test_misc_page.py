"""The Miscellaneous page (owner, 2026-10-18): metadata, offset shift,
scale every green line, Taiko to Mania, the Mappack Creator."""
from __future__ import annotations

import unittest
from pathlib import Path

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton

from library import mappack
from model.hit_object import HITSOUND_CLAP, HITSOUND_FINISH, HITSOUND_WHISTLE
from osu_io import metadata
from osu_io.parser import parse_osu
from chart import rate_change
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
        sheet._rail_buttons["taiko"].click()
        self.assertEqual([key for key, s in sheet.sections.items() if not s.isHidden()], ["taiko"])
        self.assertTrue(sheet._rail_buttons["taiko"].isChecked())
        self.assertNotIn("hitsounds", sheet.sections)

    def test_every_kat_takes_the_chosen_sound_and_keeps_its_size(self):
        notes = self.window.state.document.hit_objects
        circles = [note for note in notes if note.is_circle]
        circles[0].hit_sound = HITSOUND_CLAP | HITSOUND_FINISH
        circles[1].hit_sound = HITSOUND_CLAP
        circles[2].hit_sound = 0
        self.page.kat_sound_combo.setCurrentIndex(self.page.kat_sound_combo.findData(HITSOUND_WHISTLE))
        self.page.set_kat_sound()
        self.assertEqual(circles[0].hit_sound, HITSOUND_WHISTLE | HITSOUND_FINISH)
        self.assertEqual(circles[1].hit_sound, HITSOUND_WHISTLE)
        self.assertEqual(circles[2].hit_sound, 0, "a don stays a don")
        self.window.undo()
        self.assertEqual(circles[1].hit_sound, HITSOUND_CLAP)

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

    def test_the_header_names_the_applied_artist_and_title(self):
        """Owner, 2026-10-24: Artist and Title looked unchanged by Apply --
        the header read them off disk, so it named the old ones until the
        map was reopened, and undo left the new ones up."""
        self.window._show_chart_identity(self.state)
        old = self.window.chart_title_label.text()
        for key, text in (("Artist", "New Artist"), ("Title", "New Title"),
                          ("ArtistUnicode", "新アーティスト"), ("TitleUnicode", "新タイトル")):
            self.page.fields[key].setText(text)
        self.page.apply_metadata()
        self.assertIn("New Artist - New Title", self.window.chart_title_label.text())
        values = metadata.read_keys(self.state.document.lines, "Metadata")
        self.assertEqual((values["ArtistUnicode"], values["TitleUnicode"]), ("新アーティスト", "新タイトル"))
        self.window.undo()
        self.assertEqual(self.window.chart_title_label.text(), old)

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

    def _written(self):
        return sorted((p for p in self.path.parent.glob("*.osu") if p != self.path), key=lambda p: p.name)

    def test_rates_whose_audio_exists_are_written_without_rendering(self):
        """A second difficulty converted at the same rate reuses the song the
        first one rendered; nothing is decoded, no worker is started."""
        toasts = []
        self.window.show_toast = toasts.append
        for rate in (1.2, 1.5):
            (self.path.parent / f"audio {rate:g}x.mp3").write_bytes(b"0")
            self.page.rate_buttons[rate].click()
        self.assertIn("1.2x, 1.5x", self.page.rate_note.text())
        self.page.rate_button.click()
        self.assertIsNone(self.page._rate_pool)
        self.assertEqual([parse_osu(p).audio_filename for p in self._written()],
                         ["audio 1.2x.mp3", "audio 1.5x.mp3"])
        self.assertEqual(toasts, [])
        self.page.convert_rates()  # the same again is refused before any work
        self.assertIn("already exists", toasts[-1])

    def test_the_chosen_format_is_the_one_reused_and_is_remembered(self):
        page = self.page
        (self.path.parent / "audio 1.2x.mp3").write_bytes(b"0")
        page.rate_buttons[1.2].click()
        self.assertIn("1.2x", page.rate_note.text())
        page.rate_format.setCurrentIndex(page.rate_format.findData(".ogg"))
        self.assertNotIn("reused", page.rate_note.text())  # an MP3 is not the OGG asked for
        self.assertEqual(self.window.settings.string_value("rates/audio_format"), ".ogg")

    def test_a_pill_is_a_row_and_rows_start_at_the_chart(self):
        page = self.page
        self.assertFalse(page.rate_button.isEnabled())
        page.rate_buttons[1.3].click()
        page.rate_buttons[1.1].click()
        lines = self.state.document.lines
        od, hp = (rate_change.difficulty_setting(lines, key) for key in ("OverallDifficulty", "HPDrainRate"))
        self.assertEqual([(r.rate, r.od, r.hp) for r in page.rate_rows], [(1.1, od, hp), (1.3, od, hp)])
        self.assertEqual(page.rate_table.rowCount(), 2)
        self.assertTrue(page.rate_button.isEnabled())
        page.rate_buttons[1.1].click()
        self.assertEqual([r.rate for r in page.rate_rows], [1.3])

    def test_a_typed_rate_becomes_a_pill_and_a_preset_one_presses_its_own(self):
        page = self.page
        page.rate_custom.setValue(1.7)
        page.rate_add.click()
        self.assertTrue(page.rate_buttons[1.7].isChecked())
        self.assertFalse(page.rate_customs.isHidden())
        page.rate_custom.setValue(1.2)
        page.rate_add.click()
        self.assertTrue(page.rate_buttons[1.2].isChecked())
        self.assertEqual([r.rate for r in page.rate_rows], [1.2, 1.7])

    def test_pitch_sets_every_row_and_a_row_can_differ(self):
        page = self.page
        for rate in (1.1, 1.2):
            page.rate_buttons[rate].click()
        page.rate_pitch.setCurrentIndex(1)
        self.assertEqual([r.pitch for r in page.rate_rows], [True, True])
        box = page.rate_table.cellWidget(0, 3).findChild(QCheckBox)
        box.click()
        self.assertEqual([r.pitch for r in page.rate_rows], [False, True])
        name = page.rate_table.cellWidget(1, misc_page.COL_NAME)
        self.assertTrue(name.placeholderText().startswith(self.state.version + " 1.2x ("))

    def test_scale_fills_od_and_typing_one_goes_back_to_manual(self):
        page = self.page
        page.rate_buttons[1.5].click()
        od = rate_change.difficulty_setting(self.state.document.lines, "OverallDifficulty")
        page.rate_od_mode.setCurrentIndex(1)
        self.assertEqual(page.rate_rows[0].od, rate_change.dt_od(od, 1.5))
        page.rate_od_mode.setCurrentIndex(0)
        self.assertEqual(page.rate_rows[0].od, od)  # Manual is the chart's own again
        page.rate_od_mode.setCurrentIndex(1)
        page.rate_table.cellWidget(0, 4).setValue(4.0)
        self.assertEqual((page.rate_rows[0].od, page.rate_od_mode.currentIndex()), (4.0, 0))
        # The pills follow, so Scale then Manual by clicking still reverts.
        pills = {b.text(): b for b in page.rate_od_mode.parentWidget().findChildren(QPushButton)}
        self.assertTrue(pills["Manual"].isChecked())
        pills["Scale"].click()
        pills["Manual"].click()
        self.assertEqual(page.rate_rows[0].od, od)

    def test_each_row_writes_its_own_od_and_hp(self):
        self.window.show_toast = lambda _: None
        for rate in (1.3, 1.4):
            (self.path.parent / f"audio {rate:g}x.mp3").write_bytes(b"0")
            self.page.rate_buttons[rate].click()
        self.page.rate_table.cellWidget(0, 4).setValue(7.5)
        self.page.rate_table.cellWidget(1, 5).setValue(2)
        self.page.convert_rates()
        values = [metadata.read_keys(parse_osu(p).lines, "Difficulty") for p in self._written()]
        self.assertEqual(values[0]["OverallDifficulty"], "7.5")
        self.assertEqual(values[1]["HPDrainRate"], "2")

    def test_osus_selection_becomes_the_source_and_falls_back_when_it_goes(self):
        page = self.page
        selected = parse_osu(self.path)  # a separate document: the one osu! has
        page.rate_buttons[1.2].click()
        image = QImage(32, 18, QImage.Format_RGB32)
        image.fill(0xff3366)
        page._osu_read((("folder", self.path.name), selected, image))
        self.assertIs(page._rate_document, selected)
        self.assertIsNotNone(page.rate_banner._art)  # osu!'s map's background
        self.assertIn("selected in osu!", page.rate_original.text())
        page._osu_read((("folder", self.path.name), None, None))  # unchanged: nothing moves
        self.assertIs(page._rate_document, selected)
        page._osu_read(None)  # osu! closed
        self.assertIs(page._rate_document, self.state.document)
        self.assertIn("being edited", page.rate_original.text())

    def test_a_bpm_becomes_the_rate_that_reaches_it(self):
        page = self.page
        bpm = page._rate_chart[0]
        page.rate_bpm.setValue(round(bpm * 1.19, 1))
        page.rate_add_bpm.click()
        (row,) = page.rate_rows
        self.assertAlmostEqual(bpm * row.rate, round(bpm * 1.19, 1), delta=0.01)
        self.assertEqual(row.rate, round(row.rate, rate_change.RATE_DECIMALS))
        self.assertAlmostEqual(page.rate_table.cellWidget(0, misc_page.COL_BPM).value(), bpm * row.rate, places=1)

    def test_editing_a_row_moves_its_pill_and_remove_unchecks_it(self):
        page = self.page
        page.rate_buttons[1.2].click()
        page.rate_table.cellWidget(0, misc_page.COL_RATE).setValue(1.3)
        self.assertEqual([r.rate for r in page.rate_rows], [1.3])
        self.assertEqual((page.rate_buttons[1.2].isChecked(), page.rate_buttons[1.3].isChecked()), (False, True))
        page.rate_table.cellWidget(0, misc_page.COL_REMOVE).click()
        self.assertEqual(page.rate_rows, [])
        self.assertFalse(page.rate_buttons[1.3].isChecked())

    def test_the_name_template_and_a_typed_name(self):
        page = self.page
        page.rate_buttons[1.2].click()
        # The suite shares one settings file, and every later test would
        # read this template.
        self.addCleanup(self.window.settings.set_value, "rates/name_template", rate_change.DEFAULT_NAME_TEMPLATE)
        page.rate_name_template.setText("{name} at {bpm}BPM ({rate}x)")
        bpm = round(page._rate_chart[0] * 1.2)
        name = page.rate_table.cellWidget(0, misc_page.COL_NAME)
        self.assertEqual(name.placeholderText(), f"{self.state.version} at {bpm}BPM (1.2x)")
        self.assertEqual(self.window.settings.string_value("rates/name_template"), "{name} at {bpm}BPM ({rate}x)")
        page._row_name_typed(page.rate_rows[0], "Mine")
        self.assertEqual(page._row_version(page._rate_document, page.rate_rows[0]), "Mine")

    def test_one_name_twice_is_refused_before_any_work(self):
        toasts = []
        self.window.show_toast = toasts.append
        page = self.page
        # The suite shares one settings file, and every later test would
        # read this template.
        self.addCleanup(self.window.settings.set_value, "rates/name_template", rate_change.DEFAULT_NAME_TEMPLATE)
        page.rate_name_template.setText("{name} fast")
        for rate in (1.2, 1.3):
            page.rate_buttons[rate].click()
        page.convert_rates()
        self.assertIn("more than one difficulty", toasts[-1])
        self.assertIsNone(page._rate_run)
        self.assertEqual(self._written(), [])

    def test_reading_osu_parses_only_a_new_selection(self):
        from unittest import mock
        from library import osu_memory
        songs = self.path.parent.parent
        key = (self.path.parent.name, self.path.name)
        with mock.patch.object(osu_memory, "current_difficulty", return_value=key):
            found = misc_page.read_osu_selection(songs, None)
            self.assertEqual((found[0], found[1].source_path.name), (key, self.path.name))
            self.assertEqual(misc_page.read_osu_selection(songs, key), (key, None, None))
        with mock.patch.object(osu_memory, "current_difficulty", side_effect=osu_memory.OsuNotFound("x")):
            self.assertIsNone(misc_page.read_osu_selection(songs, key))

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
