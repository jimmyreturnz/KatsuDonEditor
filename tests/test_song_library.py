"""Song-library scan, cache and the browse -> edit -> Esc-back flow."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QApplication, QMessageBox

from tests.osu_fixtures import write_fixture

from library import song_library
from library.song_library import (
    group_by_song, load_cache, matches_search, read_header, save_cache, scan,
)

_APP: QApplication | None = None
_REAL_APPLICATION_NAME = ""


def setUpModule() -> None:
    """Point QSettings at a test-only application name.

    These tests build real MainWindows, which read preferences (and, through
    the library page, could write them) from QSettings. Without this they
    would edit the running user's own settings -- which is exactly how a
    stray `library/original_metadata` once ended up switched on for real.
    """
    global _APP, _REAL_APPLICATION_NAME
    from app import settings as settings_module

    _REAL_APPLICATION_NAME = settings_module.APPLICATION_NAME
    settings_module.APPLICATION_NAME = "TaikoFancyArrangerTests"
    _APP = QApplication.instance() or QApplication([])


def tearDownModule() -> None:
    from app import settings as settings_module

    from PySide6.QtCore import QSettings

    QSettings(settings_module.ORGANIZATION_NAME, settings_module.APPLICATION_NAME).clear()
    settings_module.APPLICATION_NAME = _REAL_APPLICATION_NAME


def make_song(root: Path, folder_name: str, fixture: str = "full_v14") -> Path:
    folder = root / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    return write_fixture(folder, fixture)


class SearchKeywordTests(unittest.TestCase):
    """The box holds keywords, not one phrase. Every word has to land, and on
    the same difficulty -- the fields are pooled, so a mapper, a title and a
    difficulty name can be typed together in any order and none of them in
    full."""

    def _difficulty(self, version, creator="jimmyreturnz", title="Deathstream",
                    tags="taiko stream"):
        return song_library.TaikoDifficulty(
            path=Path("songs") / "x" / f"{version}.osu",
            artist="Tester", artist_unicode="", title=title, title_unicode="",
            version=version, creator=creator, tags=tags,
        )

    def setUp(self):
        self.mapset = [self._difficulty("Violation"), self._difficulty("Futsuu")]

    def test_three_partial_words_from_three_fields_match_at_once(self):
        self.assertTrue(matches_search(self.mapset, "jimmyre dea vio"))

    def test_order_does_not_matter(self):
        self.assertTrue(matches_search(self.mapset, "vio jimmyre dea"))

    def test_the_same_words_as_one_phrase_would_not_have(self):
        """What this replaces: no field contains the three run together."""
        self.assertFalse(
            any("jimmyre dea vio" in d.search_text() for d in self.mapset)
        )

    def test_every_word_has_to_land(self):
        self.assertFalse(matches_search(self.mapset, "jimmyre dea nonesuch"))

    def test_the_words_have_to_land_on_one_difficulty(self):
        """"violation futsuu" is two difficulties of the same mapset, and
        matching it would mean the box answers about the folder rather than
        about anything the user can open."""
        self.assertFalse(matches_search(self.mapset, "violation futsuu"))

    def test_an_empty_or_blank_query_keeps_everything(self):
        for query in ("", "   ", "	"):
            with self.subTest(query=repr(query)):
                self.assertTrue(matches_search(self.mapset, query))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(matches_search(self.mapset, "JimmyRe DEA ViO"))

    def test_a_tag_counts_as_a_field(self):
        self.assertTrue(matches_search(self.mapset, "stream vio"))


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def test_header_reads_metadata_without_the_hit_objects(self):
        path = make_song(self.root, "song")
        header = read_header(path)
        self.assertEqual(header["mode"], "1")
        self.assertEqual(header["title"], "Test Song")
        self.assertEqual(header["artist"], "Tester")
        self.assertEqual(header["version"], "Oni")

    def test_only_taiko_files_are_yielded(self):
        taiko = make_song(self.root, "taiko song")
        standard = make_song(self.root, "std song")
        standard.write_bytes(standard.read_bytes().replace(b"Mode: 1", b"Mode: 0"))

        cache: dict[str, list] = {}
        found = [item for item in scan(self.root, cache) if item is not None]
        self.assertEqual([item.path for item in found], [taiko])
        # Both are cached, so the standard map is not re-read on every rescan.
        self.assertEqual(len(cache), 2)

    def test_unchanged_files_are_not_re_read(self):
        make_song(self.root, "song")
        cache: dict[str, list] = {}
        list(scan(self.root, cache))

        reads = []
        original = song_library.read_header
        song_library.read_header = lambda path: reads.append(path) or original(path)
        try:
            found = [item for item in scan(self.root, cache) if item is not None]
        finally:
            song_library.read_header = original
        self.assertEqual(reads, [])
        self.assertEqual(len(found), 1)

    def test_edited_file_is_re_read(self):
        path = make_song(self.root, "song")
        cache: dict[str, list] = {}
        list(scan(self.root, cache))
        path.write_bytes(path.read_bytes().replace(b"Version:Oni", b"Version:Inner Oni"))
        found = [item for item in scan(self.root, cache) if item is not None]
        self.assertEqual(found[0].version, "Inner Oni")

    def test_deleted_files_leave_the_cache(self):
        path = make_song(self.root, "song")
        cache: dict[str, list] = {}
        list(scan(self.root, cache))
        path.unlink()
        list(scan(self.root, cache))
        self.assertEqual(cache, {})

    def test_cache_round_trip_and_version_guard(self):
        cache_file = self.root / "index.json"
        save_cache(cache_file, {"a": [1, 2, "1", "artist", "title", "Oni"]})
        self.assertEqual(load_cache(cache_file)["a"][5], "Oni")

        cache_file.write_text('{"version": 999, "files": {"a": []}}', encoding="utf-8")
        self.assertEqual(load_cache(cache_file), {})
        self.assertEqual(load_cache(self.root / "missing.json"), {})

    def test_header_reads_mapper_tags_and_original_language(self):
        path = make_song(self.root, "song")
        path.write_bytes(path.read_bytes().replace(
            b"Title:Test Song\r\n",
            b"Title:Test Song\r\nTitleUnicode:\xe3\x83\x86\xe3\x82\xb9\xe3\x83\x88\r\n",
        ))
        header = read_header(path)
        self.assertEqual(header["creator"], "jimmyreturnz")
        self.assertEqual(header["tags"], "taiko")
        self.assertEqual(header["title_unicode"], "テスト")

        difficulty = next(item for item in scan(self.root, {}) if item is not None)
        self.assertEqual(difficulty.display_title(original=True), "テスト")
        self.assertEqual(difficulty.display_title(original=False), "Test Song")
        # An empty ArtistUnicode falls back rather than showing a blank name.
        self.assertEqual(difficulty.display_artist(original=True), "Tester")
        self.assertIn("jimmyreturnz", difficulty.search_text())

    def test_cache_rebuilds_songs_without_reading_the_files(self):
        make_song(self.root, "song")
        cache: dict[str, list] = {}
        list(scan(self.root, cache))

        # A different songs folder's leftovers: the cache outlives a folder
        # change, so entries outside the current root must not show up.
        elsewhere = self.root.parent / "not-my-songs" / "other.osu"
        cache[str(elsewhere)] = [0, 0, "1", "A", "", "T", "", "Oni", "someone", ""]

        from library.song_library import songs_from_cache

        found = list(songs_from_cache(cache, self.root))
        self.assertEqual([item.version for item in found], ["Oni"])
        self.assertNotIn(elsewhere, [item.path for item in found])

    def test_songs_group_by_folder(self):
        make_song(self.root, "song one")
        make_song(self.root, "song two")
        songs = group_by_song(item for item in scan(self.root, {}) if item is not None)
        self.assertEqual(len(songs), 2)


class HeardTempoTests(unittest.TestCase):
    def _chart(self, *red_and_green: str) -> Path:
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "t.osu"
        lines = ["osu file format v14", "", "[TimingPoints]", *red_and_green, "", "[HitObjects]"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_red_lines_in_order_green_lines_ignored(self):
        path = self._chart("4000,400,4,1,0,70,1,0", "0,500,4,1,0,60,1,0", "2000,-50,4,1,0,60,0,0")
        self.assertEqual(song_library.heard_tempo(path), [(0.0, 500.0), (4000.0, 400.0)])

    def test_a_red_line_that_never_finishes_a_beat_is_not_a_tempo(self):
        # Anti-barline wall ticks: 2250ms beats, 10ms apart.
        path = self._chart("0,750,4,1,0,60,1,0", "1000,2250,4,1,0,60,1,0",
                           "1010,2250,4,1,0,60,1,0", "1020,750,4,1,0,60,1,0")
        # The wall ends where the chart's own tempo comes back, 10ms later.
        self.assertEqual(song_library.heard_tempo(path), [(0.0, 750.0), (1020.0, 750.0)])


class LibraryPageTests(unittest.TestCase):
    def setUp(self):
        from ui import gui

        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.song = make_song(self.root, "Tester - Test Song")
        (self.root / "Tester - Test Song" / "audio.mp3").write_bytes(b"")

        self.window = gui.MainWindow()
        # Folder and cache both live in the temp dir: the test must never
        # write the user's real settings or their real song index.
        folder = str(self.root)
        self.window.settings.string_value = (
            lambda key, default="": folder if key == "library/songs_folder" else default
        )
        self.window.settings.set_value = lambda key, value: None
        self.window.settings.sync = lambda: None
        self.window._library_cache_path = lambda: self.root / "index.json"

    def tearDown(self):
        self.window.scan_timer.stop()
        self.window.close()
        self.window.deleteLater()
        self._temp.cleanup()

    def run_scan(self) -> None:
        self.window._start_scan()
        while self.window._scan_iterator is not None:
            self.window._scan_step()

    def test_a_volume_change_reaches_a_preview_already_playing(self):
        """The preview read the music volume only as it started, so the
        Settings slider was not heard until the song was selected again."""
        from PySide6.QtCore import QVariantAnimation
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        library = self.window._library
        library.preview_player = QMediaPlayer(self.window)
        library.preview_player.setAudioOutput(QAudioOutput(library.preview_player))
        library.preview_fade = QVariantAnimation(library.preview_player)
        library._preview_loading = False
        self.window._set_volumes(40, 70)
        self.assertAlmostEqual(library.preview_player.audioOutput().volume(), 0.4, places=3)
        # Mid-fade, the fade is retargeted rather than overwritten next step.
        library.preview_fade.setDuration(10_000)
        library.preview_fade.setStartValue(0.0)
        library.preview_fade.setEndValue(0.4)
        library.preview_fade.start()
        self.window._set_volumes(90, 70)
        self.assertAlmostEqual(library.preview_fade.endValue(), 0.9)
        library.preview_fade.stop()

    def test_scan_fills_the_song_list_and_the_difficulty_list(self):
        self.run_scan()
        self.assertEqual(self.window.song_list.count(), 1)
        self.assertIn("Tester - Test Song", self.window.song_list.item(0).text())
        self.window.song_list.setCurrentRow(0)
        self.assertEqual(self.window.difficulty_list.item(0).text(), "Oni")

    def test_search_filters_by_artist_title_difficulty_mapper_and_tags(self):
        self.run_scan()
        for needle in ("tester", "test song", "oni", "jimmyreturnz", "taiko"):
            with self.subTest(needle=needle):
                self.window.library_search.setText(needle)
                self.assertEqual(self.window.song_list.count(), 1)
        self.window.library_search.setText("nothing here")
        self.assertEqual(self.window.song_list.count(), 0)

    def test_reload_this_song_rereads_only_the_selected_folder(self):
        """An edit that keeps size and mtime is invisible to Quick scan;
        reloading the song reads it anyway, and leaves the others' index alone."""
        other = make_song(self.root, "Other - Second Song")
        self.run_scan()
        row = next(i for i in range(self.window.song_list.count())
                   if "Test Song" in (self.window.song_list.item(i).data(Qt.UserRole) or ""))
        self.window.song_list.setCurrentRow(row)
        stat = self.song.stat()
        self.song.write_bytes(self.song.read_bytes().replace(b"Version:Oni", b"Version:Ura"))
        os.utime(self.song, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        self.window._library.reload_current_song()

        self.assertEqual(self.window.difficulty_list.item(0).text(), "Ura")
        self.assertIn(str(other), self.window._library_cache)
        self.assertIn(str(self.song), load_cache(self.root / "index.json"))

    def test_song_row_shows_the_mapper(self):
        self.run_scan()
        self.assertIn("jimmyreturnz", self.window.song_list.item(0).text())

    def test_grouping_and_sort_order(self):
        make_song(self.root, "Other - Second Song")
        (self.root / "Other - Second Song" / "audio.mp3").write_bytes(b"")
        second = self.root / "Other - Second Song" / "full_v14.osu"
        second.write_bytes(second.read_bytes()
                           .replace(b"Artist:Tester", b"Artist:Another")
                           .replace(b"Creator:jimmyreturnz", b"Creator:someone else"))
        self.run_scan()

        rows = lambda: [self.window.song_list.item(i) for i in range(self.window.song_list.count())]
        songs = lambda: [i.text() for i in rows() if i.data(Qt.UserRole) is not None]

        self.window.library_group_combo.setCurrentIndex(
            self.window.library_group_combo.findData("none")
        )
        self.assertEqual(len(rows()), 2)
        ascending = songs()
        self.assertTrue(ascending[0].startswith("Another"))

        self.window.library_sort_combo.setCurrentIndex(
            self.window.library_sort_combo.findData("za")
        )
        self.assertEqual(songs(), list(reversed(ascending)))

        self.window.library_sort_combo.setCurrentIndex(
            self.window.library_sort_combo.findData("az")
        )
        self.window.library_group_combo.setCurrentIndex(
            self.window.library_group_combo.findData("mapper")
        )
        headers = [i.text() for i in rows() if i.data(Qt.UserRole) is None]
        self.assertEqual(headers, ["jimmyreturnz", "someone else"])

    def test_original_language_metadata_switches_the_labels(self):
        self.song.write_bytes(self.song.read_bytes().replace(
            b"Title:Test Song\r\n",
            b"Title:Test Song\r\nTitleUnicode:\xe3\x83\x86\xe3\x82\xb9\xe3\x83\x88\r\n",
        ))
        # Test settings are seeded from the real ones, where the owner has
        # original-language titles on; the first half needs them off.
        self.window.original_metadata_check.setChecked(False)
        self.run_scan()
        self.assertIn("Test Song", self.window.song_list.item(0).text())
        self.window.original_metadata_check.setChecked(True)
        self.assertIn("テスト", self.window.song_list.item(0).text())

    def test_opening_a_difficulty_switches_to_the_editor(self):
        from ui import gui

        self.run_scan()
        self.window.song_list.setCurrentRow(0)
        self.window._open_selected_difficulty()
        self.assertEqual(self.window.state.source_path, self.song.resolve())
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_EDITOR)
        self.assertTrue(self.window.editor_page_button.isChecked())

    def test_escape_returns_to_the_song_list_when_nothing_is_dirty(self):
        from ui import gui

        self.run_scan()
        self.window.song_list.setCurrentRow(0)
        self.window._open_selected_difficulty()
        self.window.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_LIBRARY)
        self.assertTrue(self.window.library_page_button.isChecked())

    def test_escape_in_a_view_with_a_selection_clears_it_instead_of_leaving(self):
        from ui import gui

        self.run_scan()
        self.window.song_list.setCurrentRow(0)
        self.window._open_selected_difficulty()
        view = self.window._chart_views[-1]
        view.selected = {0}
        view.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        self.assertEqual(view.selected, set())
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_EDITOR)

        # Second Esc has nothing to clear, so the view leaves it unaccepted
        # and Qt hands it up to the window.
        event = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
        view.keyPressEvent(event)
        self.assertFalse(event.isAccepted())
        self.window.keyPressEvent(event)
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_LIBRARY)

    def test_first_start_explains_the_folder_picker_then_scans(self):
        from ui import gui
        from ui import song_select  # the folder picker lives there now

        shown = []
        chosen = str(self.root)

        class ExplainBox:
            information = staticmethod(lambda parent, title, text: shown.append(text))

        # Settings that start empty and remember the pick, like the real ones:
        # start_library must take the "nothing stored" branch, and the scan
        # that follows must read back what _choose_songs_folder just wrote.
        stored = {"library/songs_folder": ""}
        self.window.settings.string_value = lambda key, default="": stored.get(key, default)
        self.window.settings.bool_value = lambda key, default=False: bool(stored.get(key, default))
        self.window.settings.set_value = lambda key, value: stored.__setitem__(key, value)

        original_box, original_dialog = song_select.QMessageBox, song_select.QFileDialog
        song_select.QMessageBox = ExplainBox
        song_select.QFileDialog = type("FakeDialog", (), {
            "getExistingDirectory": staticmethod(lambda *args, **kwargs: chosen)
        })
        try:
            self.window.start_library()
        finally:
            song_select.QMessageBox, song_select.QFileDialog = original_box, original_dialog
        self.assertEqual(stored["library/songs_folder"], chosen)
        self.assertTrue(shown, "no explanation before the folder picker")
        self.assertIsNotNone(self.window._scan_iterator)
        # Setup counts as done only once a folder was really chosen.
        self.assertIs(stored["setup/completed"], True)
        self.window.scan_timer.stop()

    def test_setup_runs_again_when_a_folder_was_stored_but_setup_never_finished(self):
        """An install that predates the library still gets the first-run flow."""
        from ui import gui
        from ui import song_select  # the folder picker lives there now

        stored = {"library/songs_folder": str(self.root)}  # no setup/completed
        self.window.settings.string_value = lambda key, default="": stored.get(key, default)
        self.window.settings.bool_value = lambda key, default=False: bool(stored.get(key, default))
        self.window.settings.set_value = lambda key, value: stored.__setitem__(key, value)

        shown = []
        original_box, original_dialog = song_select.QMessageBox, song_select.QFileDialog
        song_select.QMessageBox = type("ExplainBox", (), {
            "information": staticmethod(lambda parent, title, text: shown.append(text))
        })
        song_select.QFileDialog = type("FakeDialog", (), {
            "getExistingDirectory": staticmethod(lambda *args, **kwargs: "")  # cancelled
        })
        try:
            self.window.start_library()
        finally:
            song_select.QMessageBox, song_select.QFileDialog = original_box, original_dialog
        self.assertTrue(shown)
        self.assertNotIn("setup/completed", stored)

    def test_second_start_lists_the_songs_before_the_walk_begins(self):
        self.run_scan()  # writes the index
        self.window._start_scan()
        try:
            # Not one file reopened yet, and the list is already complete.
            self.assertEqual(self.window._scan_files, 0)
            self.assertEqual(self.window.song_list.count(), 1)
        finally:
            self.window.scan_timer.stop()

    def test_a_song_deleted_since_the_last_scan_leaves_the_list(self):
        self.run_scan()
        for path in self.root.rglob("*.osu"):
            path.unlink()
        self.run_scan()
        self.assertEqual(self.window.song_list.count(), 0)

    def test_group_headers_are_bold_accent_and_unselectable(self):
        from ui import gui

        self.run_scan()
        self.window.library_group_combo.setCurrentIndex(
            self.window.library_group_combo.findData("mapper")
        )
        header = self.window.song_list.item(0)
        self.assertIsNone(header.data(Qt.UserRole))
        self.assertTrue(header.font().bold())
        self.assertEqual(header.foreground().color().name(), gui.ACCENT_PINK)
        self.assertFalse(bool(header.flags() & Qt.ItemIsSelectable))

    def test_going_back_closes_the_views_and_reopening_starts_fresh(self):
        self.run_scan()
        self.window.song_list.setCurrentRow(0)
        self.window._open_selected_difficulty()
        opened = [frame.view_type for frame in self.window._editor_views]
        # Song select opens a gameplay preview with the pair (owner, 2026-10-09).
        self.assertEqual(sorted(opened), ["chart", "gameplay", "sv"])

        self.window._back_to_library()
        self.assertEqual(self.window._editor_views, [])
        self.assertEqual(self.window._sv_views, [])
        self.assertEqual(self.window._active_chart_view, None)

        self.window._open_selected_difficulty()
        self.assertEqual(sorted(frame.view_type for frame in self.window._editor_views), ["chart", "gameplay", "sv"])

    def test_going_back_keeps_the_song_playing_from_the_playhead(self):
        self.run_scan()
        self.window.song_list.setCurrentRow(0)
        self.window._open_selected_difficulty()
        state = self.window.state
        playhead = round(self.window.timeline.current_time)
        handed = []
        self.window._library.continue_song = lambda *args: handed.append(args)
        self.window._back_to_library()
        self.assertEqual(handed, [(state.audio_path, playhead, 3000)])

    def test_a_continued_song_starts_where_it_was_then_loops_to_the_preview(self):
        library = self.window._library
        positions = []

        class Player:
            def setPosition(self, ms): positions.append(ms)
            def play(self): pass
            def duration(self): return 10000
            def setPitchCompensation(self, on): pass
            def setPlaybackRate(self, rate): pass

        class Fade:
            def __getattr__(self, name): return lambda *args: None

        real = library.preview_player
        library.preview_player, library.preview_fade = Player(), Fade()
        library._preview_ms, library._resume_ms = 2000, 7345
        try:
            library._play_preview()
            library._play_preview()  # EndOfMedia: round again
        finally:
            library.preview_player = real
        self.assertEqual(positions, [7345, 2000])

    def test_every_configurable_shortcut_is_wired(self):
        from app.settings import all_shortcut_definitions

        wired = {
            "play_pause": self.window.play_shortcut,
            "undo": self.window.undo_shortcut,
            "redo": self.window.redo_shortcut,
            "save_all": self.window.save_shortcut,
            "copy": self.window.copy_shortcut,
            "paste": self.window.paste_shortcut,
            "resnap": self.window.resnap_shortcut,
            "hitsound_offset_earlier": self.window.hitsound_earlier_shortcut,
            "hitsound_offset_later": self.window.hitsound_later_shortcut,
            **self.window.tool_shortcuts,
        }
        # all_shortcut_definitions, not SHORTCUT_DEFINITIONS: every gimmick
        # layer registers its own toolbox keys at gui import time, and they
        # have to be wired too.
        # back_to_songs is compared inside keyPressEvent instead of being
        # registered, so Escape still reaches the views first.
        expected = {d.action_id for d in all_shortcut_definitions()} - {"back_to_songs"}
        self.assertEqual(set(wired), expected)
        for action_id, shortcut in wired.items():
            with self.subTest(action_id=action_id):
                self.assertEqual(
                    shortcut.key(), QKeySequence(self.window.shortcuts.sequence(action_id))
                )

    def test_tool_digits_do_not_eat_typing_in_the_search_box(self):
        self.run_scan()
        self.window.song_list.setCurrentRow(0)
        self.window._open_selected_difficulty()
        view = self.window._chart_views[-1]

        self.window._editor_view_focus_changed(view, self.window.library_search)
        self.assertFalse(any(s.isEnabled() for s in self.window.tool_shortcuts.values()))

        self.window._editor_view_focus_changed(self.window.library_search, view)
        # Only the Editor page's own toolbox comes back: every gimmick layer
        # numbers its tools from 1 as well, and two enabled shortcuts on one
        # key would make Qt fire neither (see _refresh_tool_shortcut_scope).
        editor_tools = [f"tool_{digit}" for digit in "123456"]
        self.assertTrue(all(self.window.tool_shortcuts[a].isEnabled() for a in editor_tools))
        self.assertFalse(
            any(
                shortcut.isEnabled()
                for action_id, shortcut in self.window.tool_shortcuts.items()
                if action_id not in editor_tools
            )
        )

    def test_rebound_shortcuts_take_effect(self):
        from ui import gui

        sequences = {"tool_2": "F2", "back_to_songs": "Ctrl+B"}
        self.window.shortcuts.sequence = lambda action_id: sequences.get(
            action_id, {"undo": "Ctrl+Z", "redo": "Ctrl+Y", "play_pause": "Space",
                        "copy": "Ctrl+C", "paste": "Ctrl+V", "save_all": "Ctrl+S"}.get(action_id, "")
        )
        self.window._reload_shortcuts()
        self.assertEqual(self.window.tool_shortcuts["tool_2"].key(), QKeySequence("F2"))

        self.run_scan()
        self.window.song_list.setCurrentRow(0)
        self.window._open_selected_difficulty()
        self.window.keyPressEvent(
            QKeyEvent(QKeyEvent.KeyPress, Qt.Key_B, Qt.ControlModifier)
        )
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_LIBRARY)

    def _open_and_dirty(self):
        self.run_scan()
        self.window.song_list.setCurrentRow(0)
        self.window._open_selected_difficulty()
        self.window._place_note(self.window.state.source_path, "don", 3000, False)
        self.assertTrue(self.window.state.history.dirty)

    def test_leaving_with_unsaved_edits_asks_and_can_be_cancelled(self):
        from ui import gui

        self._open_and_dirty()
        answers = {"role": QMessageBox.RejectRole}
        self._fake_message_box(answers)
        self.window._back_to_library()
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_EDITOR)
        self.assertTrue(self.window.state.history.dirty)

    def test_leaving_with_unsaved_edits_can_save(self):
        from ui import gui

        self._open_and_dirty()
        self._fake_message_box({"role": QMessageBox.AcceptRole})
        self.window._back_to_library()
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_LIBRARY)
        self.assertFalse(self.window.state.history.dirty)

    def test_leaving_with_unsaved_edits_can_discard(self):
        from ui import gui

        self._open_and_dirty()
        self._fake_message_box({"role": QMessageBox.DestructiveRole})
        self.window._back_to_library()
        self.assertEqual(self.window.page_stack.currentIndex(), gui.PAGE_LIBRARY)
        # Nothing was written; the edit is still there in this session.
        self.assertTrue(self.window.state.history.dirty)

    def test_continue_without_saving_does_not_reprompt_until_edited_again(self):
        """Answering "Continue Without Saving" must stick: the very next
        page/view change (or Esc, or quit) must not immediately re-ask about
        the exact same still-unsaved edit."""
        from ui import gui

        self._open_and_dirty()
        self._fake_message_box({"role": QMessageBox.DestructiveRole})
        self.assertTrue(self.window._confirm_leaving_editor())
        self.assertTrue(self.window.state.history.dirty, "discarding must not write anything")

        # No box this time: the acknowledged revision matches, so the state
        # is filtered out of "dirty" before a prompt is ever built.
        original_box = gui.QMessageBox

        def _no_box(*args, **kwargs):
            raise AssertionError("must not prompt again for the same unsaved edit")

        gui.QMessageBox = _no_box
        try:
            self.assertTrue(self.window._confirm_leaving_editor())
        finally:
            gui.QMessageBox = original_box

        # A fresh edit bumps history.revision past the acknowledged one, so
        # the prompt is legitimate again.
        self.window._place_note(self.window.state.source_path, "don", 3200, False)
        self._fake_message_box({"role": QMessageBox.RejectRole})
        self.assertFalse(self.window._confirm_leaving_editor())

    def test_escape_at_the_library_asks_before_exiting(self):
        """The front door: Esc with nothing open still asks first, since a
        stray keypress there would otherwise close the window outright."""
        self.run_scan()
        self._fake_message_box({"role": QMessageBox.RejectRole})
        closed = []
        self.window.close = lambda: closed.append(1)

        self.window.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))

        self.assertEqual(closed, [])

    def test_escape_at_the_library_exits_when_confirmed_and_nothing_is_dirty(self):
        self.run_scan()
        self._fake_message_box({"role": QMessageBox.AcceptRole})
        closed = []
        self.window.close = lambda: closed.append(1)

        self.window.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))

        self.assertEqual(closed, [1])

    def test_escape_at_the_library_still_checks_unsaved_work_when_confirmed(self):
        """Confirming "Exit" does not skip the separate unsaved-changes gate:
        answering that one "Cancel" must still keep the window open.

        There is no ordinary route to the library page with a dirty,
        unacknowledged state already sitting on it -- both `_switch_page` and
        `_back_to_library` run this same unsaved-work check on the way there,
        so by the time you have arrived it is already resolved. `_show_page`
        alone (bypassing that) is what puts the window in the state this
        test needs to reach `_confirm_exit_application`'s own belt-and-
        suspenders call to it in isolation.
        """
        from ui import gui

        self._open_and_dirty()
        self.window._show_page(gui.PAGE_LIBRARY)
        self._fake_message_box_sequence([QMessageBox.AcceptRole, QMessageBox.RejectRole])
        closed = []
        self.window.close = lambda: closed.append(1)

        self.window.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))

        self.assertEqual(closed, [])
        self.assertTrue(self.window.state.history.dirty)

    def test_escape_at_the_library_exits_after_discarding_unsaved_work(self):
        from ui import gui

        self._open_and_dirty()
        self.window._show_page(gui.PAGE_LIBRARY)
        self._fake_message_box_sequence(
            [QMessageBox.AcceptRole, QMessageBox.DestructiveRole]
        )
        closed = []
        self.window.close = lambda: closed.append(1)

        self.window.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))

        self.assertEqual(closed, [1])

    def _fake_message_box_sequence(self, roles: list) -> None:
        """Like `_fake_message_box`, but a different answer for each
        QMessageBox constructed in turn -- for a flow that shows more than
        one, such as Esc's exit confirmation followed by the unsaved-work
        prompt.
        """
        from ui import gui

        pending = list(roles)

        class FakeBox(QMessageBox):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._roles = []

            def addButton(self, text, role):
                button = super().addButton(text, role)
                self._roles.append((button, role))
                return button

            def exec(self):
                wanted = pending.pop(0)
                self._clicked = next(b for b, role in self._roles if role == wanted)
                return 0

            def clickedButton(self):
                return self._clicked

        original = gui.QMessageBox
        gui.QMessageBox = FakeBox
        self.addCleanup(lambda: setattr(gui, "QMessageBox", original))

    def _fake_message_box(self, answers: dict) -> None:
        """Answer the unsaved-changes prompt by role instead of by clicking.

        A subclass, so every QMessageBox constant the prompt uses still
        resolves; only exec() and clickedButton() are replaced.
        """
        from ui import gui

        class FakeBox(QMessageBox):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._roles = []

            def addButton(self, text, role):
                button = super().addButton(text, role)
                self._roles.append((button, role))
                return button

            def exec(self):
                self._clicked = next(b for b, role in self._roles if role == answers["role"])
                return 0

            def clickedButton(self):
                return self._clicked

        original = gui.QMessageBox
        gui.QMessageBox = FakeBox
        self.addCleanup(lambda: setattr(gui, "QMessageBox", original))


if __name__ == "__main__":
    unittest.main()
