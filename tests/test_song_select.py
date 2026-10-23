"""Song select: osu!'s star colours, osu!.db's ratings, the scan's new fields,
and the page's Continue row and keyboard."""
from __future__ import annotations

import json
import os
import struct
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from library import osu_db
from ui import common, song_select
from library.song_library import read_header
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    from app import settings as settings_module

    settings_module.APPLICATION_NAME = "TaikoFancyArrangerTests"
    _APP = QApplication.instance() or QApplication([])


def _string(value: str) -> bytes:
    data = value.encode("utf-8")
    assert len(data) < 128  # one ULEB128 byte
    return b"\x0b" + bytes([len(data)]) + data


def _osu_db(version: int, beatmaps: list[tuple[str, str, str, float]]) -> bytes:
    """A minimal osu!.db: (folder, file, md5, taiko nomod stars) per map."""
    single = version >= osu_db.FLOAT_STARS_VERSION
    out = struct.pack("<ii", version, 1) + b"\x01" + bytes(8) + _string("player")
    out += struct.pack("<i", len(beatmaps))
    for folder, osu_file, md5, stars in beatmaps:
        out += b"".join(_string(s) for s in ("A", "", "T", "", "me", "Oni", "a.mp3"))
        out += _string(md5) + _string(osu_file)
        out += b"\x04" + struct.pack("<hhhq", 1, 0, 0, 0) + bytes(16) + bytes(8)
        for mode in range(4):
            if mode == osu_db.TAIKO:
                pairs = [(64, stars + 1), (0, stars)]  # a DT rating first, to be skipped
                out += struct.pack("<i", len(pairs))
                for mods, value in pairs:
                    out += b"\x08" + struct.pack("<i", mods)
                    out += b"\x0c" + struct.pack("<f", value) if single else b"\x0d" + struct.pack("<d", value)
            else:
                out += struct.pack("<i", 0)
        out += bytes(12) + struct.pack("<i", 1) + bytes(17)
        out += bytes(23) + _string("") + _string("") + bytes(2) + _string("") + bytes(10)
        out += _string(folder) + bytes(13) + bytes(5)
    return out


class OsuDbTests(unittest.TestCase):
    def test_both_star_formats(self):
        """Doubles before 20250107, floats from it."""
        for version in (20240101, osu_db.FLOAT_STARS_VERSION):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "osu!.db"
                path.write_bytes(_osu_db(version, [("Song A", "A [Oni].osu", "aa", 5.25),
                                                   ("Song B", "B [Hard].osu", "bb", 2.5)]))
                ratings = osu_db.read(path)
                self.assertEqual(ratings[("song a", "a [oni].osu")].md5, "aa")
                self.assertAlmostEqual(ratings[("song a", "a [oni].osu")].taiko_stars, 5.25, places=5)
                self.assertAlmostEqual(ratings[("song b", "b [hard].osu")].taiko_stars, 2.5, places=5)


class StarColourTests(unittest.TestCase):
    def test_osu_spectrum_stops(self):
        from ui import gui

        self.assertEqual(song_select.star_colour(1.25).name(), "#4fc0ff")
        self.assertEqual(song_select.star_colour(5.8).name(), "#c645b8")
        self.assertEqual(song_select.star_colour(0.05).name(), "#aaaaaa")
        self.assertEqual(song_select.star_colour(None).name(), "#aaaaaa")
        self.assertEqual(song_select.star_colour(12).name(), "#000000")

    def test_rounds_away_from_zero_before_sampling(self):
        from ui import gui

        self.assertEqual(song_select.star_colour(1.245).name(), song_select.star_colour(1.25).name())

    def test_text_is_dark_then_gold_then_its_own_spectrum(self):
        from ui import gui

        self.assertEqual(song_select.star_text_colour(6.4).alpha(), 191)
        self.assertEqual(song_select.star_text_colour(7.0).name(), "#ffd966")
        self.assertEqual(song_select.star_text_colour(9.9).name(), "#ff8068")


class BpmTextTests(unittest.TestCase):
    def test_a_subnormal_beat_length_is_infinite_bpm_not_a_crash(self):
        # EGTS 2022 writes `1.14514535393084E-319`: 60000 over it is inf, and
        # round(inf) used to stop the difficulty list at its first chart.
        from ui import gui

        bpm = 60000.0 / 1.14514535393084e-319
        self.assertEqual(song_select.format_bpm(180.0, bpm), "180–∞")
        self.assertEqual(song_select.format_bpm(bpm, bpm), "∞")
        self.assertEqual(song_select.format_bpm(150.2, 149.8), "150")

    def test_a_range_says_which_bpm_it_mostly_is(self):
        # Owner, 2026-10-09: aleph-0 as "125–400 (250)", the density view's rule.
        from ui import gui
        from library.song_library import most_common_beat_length

        self.assertEqual(song_select.format_bpm(125, 400, 250), "125–400 (250)")
        self.assertEqual(song_select.format_bpm(250, 250, 250), "250", "one tempo needs no brackets")
        # 250 for 60s, 400 for 20s, 125 for 10s up to the last object at 90s.
        lines = [(0.0, 240.0), (60000.0, 150.0), (80000.0, 480.0)]
        self.assertEqual(most_common_beat_length(lines, 90000.0), 240.0)


class SongDotTests(unittest.TestCase):
    def test_a_pack_shows_a_count_of_the_rest(self):
        from ui import gui

        self.assertEqual(song_select.song_dots_shown(6, 1000), (6, 0))
        # 1000px allows 20 dots at most, so 40 charts are 18 dots and "+22".
        self.assertEqual(song_select.song_dots_shown(40, 1000), (18, 22))
        self.assertEqual(song_select.song_dots_shown(20, 1000), (20, 0))
        # A narrow list gives the dots 40% of it: 300px is 8 slots.
        shown, more = song_select.song_dots_shown(12, 300)
        self.assertEqual(shown + more, 12)
        self.assertLessEqual(shown, 8)


class ScanFieldTests(unittest.TestCase):
    def test_a_taiko_file_gives_background_bpm_notes_and_length(self):
        with tempfile.TemporaryDirectory() as temp:
            path = write_fixture(Path(temp), "full_v14")
            text = path.read_text(encoding="utf-8")
            fields = read_header(path)
            objects = [line for line in text.split("[HitObjects]")[1].splitlines() if line.strip()]
            self.assertEqual(fields["notes"], len(objects))
            times = [int(line.split(",")[2]) for line in objects]
            self.assertEqual(fields["length_ms"], times[-1] - times[0])
            self.assertGreater(fields["bpm_max"], 0)


class PageTests(unittest.TestCase):
    def setUp(self):
        from ui import gui

        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        for name in ("Tester - Test Song", "Other - Second Song"):
            (self.root / name).mkdir()
            write_fixture(self.root / name, "full_v14")
            (self.root / name / "audio.mp3").write_bytes(b"")
        self.stored = {"library/songs_folder": str(self.root)}
        self.window = gui.MainWindow()
        self.window.settings.string_value = lambda key, default="": self.stored.get(key, default)
        self.window.settings.set_value = lambda key, value: self.stored.__setitem__(key, value)
        self.window.settings.sync = lambda: None
        self.window._library_cache_path = lambda: self.root / "index.json"
        self.window._library.start_scan()
        while self.window._scan_iterator is not None:
            self.window._library.scan_step()

    def tearDown(self):
        self.window.scan_timer.stop()
        self.window.close()
        self.window.deleteLater()
        self._temp.cleanup()

    def key(self, widget, key, text=""):
        # sendEvent, not widget.event(): only the former passes the filters.
        QApplication.sendEvent(widget, QKeyEvent(QKeyEvent.KeyPress, key, Qt.NoModifier, text))

    def test_the_band_holds_still_unless_the_song_is_heard(self):
        # Owner, 2026-10-09: paused (or loading another pick), the playhead
        # ran on from the preview point in silence.
        from PySide6.QtMultimedia import QMediaPlayer

        lib = self.window._library
        state = [QMediaPlayer.StoppedState]

        class Player:
            def playbackRate(self): return 1.0
            def playbackState(self): return state[0]
            def position(self): return 42000

        lib.preview_player = Player()
        lib._preview_audio = lib._beat_audio = Path("audio.mp3")
        lib._beat_anchor_ms = 30000.0
        lib._preview_loading = True
        self.assertEqual(lib._banner_clock(), 30000.0, "loading: held at where it will start")
        self.assertFalse(lib._banner_playing())
        lib._preview_loading = False
        state[0] = QMediaPlayer.PausedState
        self.assertEqual(lib._banner_clock(), 42000.0, "paused: held where it stopped")
        self.assertFalse(lib._banner_playing())
        state[0] = QMediaPlayer.PlayingState
        self.assertTrue(lib._banner_playing())
        lib.preview_player = None  # tearDown's close() would stop the fake

    def test_a_long_pause_does_not_count_as_song_time_on_resume(self):
        # Owner, 2026-10-09: paused for a long while, the playhead blinked to
        # where the song would have been had it kept playing.
        from PySide6.QtMultimedia import QMediaPlayer

        lib = self.window._library
        wall = [0.0]
        state = [QMediaPlayer.PlayingState]

        class Elapsed:
            def __init__(self): self.base = 0.0
            def elapsed(self): return wall[0] - self.base
            def start(self): self.base = wall[0]
            restart = start

        class Player:
            def playbackRate(self): return 1.0
            def playbackState(self): return state[0]
            def position(self): return 20000

        lib.preview_player = Player()
        lib._preview_audio = lib._beat_audio = Path("audio.mp3")
        lib._beat_elapsed = Elapsed()
        lib._beat_anchor_ms = 20000.0
        state[0] = QMediaPlayer.PausedState
        lib._preview_state_changed(state[0])
        wall[0] = 600000.0  # ten minutes paused
        state[0] = QMediaPlayer.PlayingState
        lib._preview_state_changed(state[0])
        wall[0] += 8.0  # the first frame after resuming, before any new report
        reading = lib._beat_clock()
        lib.preview_player = None  # tearDown's close() would stop the fake
        # Within the clock's own easing of a frame; ten minutes off before.
        self.assertLess(abs(reading - 20008.0), 5.0)

    def test_the_preview_clock_never_steps_back_between_coarse_positions(self):
        # The player reports its position in ~52ms steps. Checked every frame
        # against a 40ms bound, the stale value snapped the clock back 40ms
        # five times a second: the chart preview's "a bit laggy".
        from PySide6.QtMultimedia import QMediaPlayer

        lib = self.window._library
        wall = [0.0]

        class Elapsed:
            def __init__(self): self.base = 0.0
            def elapsed(self): return wall[0] - self.base
            def start(self): self.base = wall[0]
            restart = start

        class Player:
            def playbackRate(self): return 1.0
            def playbackState(self): return QMediaPlayer.PlayingState
            def position(self): return 10000 + int(wall[0] // 52) * 52

        lib.preview_player = Player()
        lib._preview_audio = lib._beat_audio = Path("audio.mp3")
        lib._beat_elapsed = Elapsed()
        lib._beat_anchor_ms = 10000.0
        readings = []
        for frame in range(500):  # four seconds at 8ms
            wall[0] = frame * 8.0
            readings.append(lib._beat_clock())
        lib.preview_player = None  # tearDown's close() would stop the fake
        steps = [b - a for a, b in zip(readings, readings[1:])]
        self.assertGreaterEqual(min(steps), 0.0)
        self.assertLess(abs(readings[-1] - (10000 + wall[0])), 15.0)

    def test_typing_on_the_list_goes_to_the_search(self):
        self.key(self.window.song_list, Qt.Key_Z, "z")
        self.assertEqual(self.window.library_search.text(), "z")

    def test_arrows_in_the_search_move_through_songs(self):
        self.window.song_list.setCurrentRow(0)
        self.key(self.window.library_search, Qt.Key_Down)
        self.assertEqual(self.window.song_list.currentRow(), 1)

    def test_arrows_on_the_list_step_one_song(self):
        self.window.song_list.setCurrentRow(0)
        self.key(self.window.song_list, Qt.Key_Down)
        self.assertEqual(self.window.song_list.currentRow(), 1)
        self.key(self.window.song_list, Qt.Key_Up)
        self.assertEqual(self.window.song_list.currentRow(), 0)

    def test_a_missing_chart_leaves_its_card_to_the_next_one(self):
        # Owner, 2026-10-09: one of the last four had been deleted, and with
        # only four remembered the row showed three.
        from ui import gui

        library = self.window._library
        charts = []
        for index in range(song_select.RECENT_CHARTS_SHOWN + 1):
            folder = self.root / f"Extra {index}"
            folder.mkdir()
            charts.append(write_fixture(folder, "full_v14"))
        for chart in charts:
            library.remember_recent(chart)
        charts[-1].unlink()  # the newest is gone
        library.refresh_continue_row()
        shown = [card.path for card in library.continue_cards if not card.isHidden()]
        self.assertEqual(len(shown), song_select.RECENT_CHARTS_SHOWN)
        self.assertNotIn(str(charts[-1]), shown)

    def test_opening_a_chart_puts_it_in_continue(self):
        library = self.window._library
        library.refresh_continue_row()  # built from the real settings, before the patch
        self.assertTrue(library.continue_row.isHidden())
        self.window.song_list.setCurrentRow(0)
        self.window._library.open_selected_difficulty()
        recent = json.loads(self.stored["library/recent"])
        self.assertEqual(Path(recent[0][0]), self.window.state.source_path)
        self.assertFalse(library.continue_row.isHidden())
        self.assertEqual(library.continue_cards[0].path, recent[0][0])

    def test_song_select_opens_a_gameplay_preview_under_the_pair(self):
        # Owner, 2026-10-09: from song select, chart + SV + gameplay; another
        # difficulty opened from inside the editor gets the pair alone.
        self.window.song_list.setCurrentRow(0)
        self.window._library.open_selected_difficulty()
        first = self.window.state.source_path
        kinds = [frame.view_type for frame in self.window._editor_views
                 if getattr(frame, "difficulty_path", None) == first]
        self.assertEqual(kinds, ["chart", "sv", "gameplay"])
        other = Path(self.window.song_list.item(1).data(Qt.UserRole))
        other = next(other.glob("*.osu"))
        self.window._load_map_path(other)
        kinds = [frame.view_type for frame in self.window._editor_views
                 if getattr(frame, "difficulty_path", None) == self.window.state.source_path]
        self.assertEqual(kinds, ["chart", "sv"])

    def test_a_continue_card_shows_its_charts_art_and_no_age(self):
        """Owner's call: no "opened N min ago", the chart's background
        very dim behind the title instead."""
        from PySide6.QtGui import QImage
        song = next(Path(self.window.song_list.item(0).data(Qt.UserRole)).glob("*.osu"))
        image = QImage(64, 36, QImage.Format_RGB32)
        image.fill(Qt.red)
        image.save(str(song.parent / "bg.jpg"))  # the fixture's own background
        self.stored["library/recent"] = json.dumps([[str(song), 0.0]])
        self.window._library.refresh_continue_row()
        card = self.window._library.continue_cards[0]
        self.assertIsNotNone(card.art)
        self.assertFalse(hasattr(card, "when"))
        card.resize(240, 60)
        card.grab()  # paints without raising

    def test_a_path_saved_before_open_times_still_lists(self):
        song = next(Path(self.window.song_list.item(0).data(Qt.UserRole)).glob("*.osu"))
        self.stored["library/recent"] = json.dumps([str(song)])
        self.window._library.refresh_continue_row()
        self.assertEqual(self.window._library.continue_cards[0].path, str(song))

    def _last_song_file(self):
        row = self.window.song_list.count() - 1
        return row, next(Path(self.window.song_list.item(row).data(Qt.UserRole)).glob("*.osu"))

    def test_a_continue_card_selects_its_chart_and_does_not_open_it(self):
        row, song = self._last_song_file()
        self.window.song_list.setCurrentRow(0)
        self.window._library.show_recent(str(song))
        from ui.gui import PAGE_LIBRARY
        self.assertEqual(self.window.page_stack.currentIndex(), PAGE_LIBRARY)
        self.assertIsNone(self.window.state)
        self.assertEqual(self.window.song_list.currentRow(), row)
        self.assertEqual(Path(self.window.difficulty_list.currentItem().data(Qt.UserRole)), song)

    def test_a_continue_card_clears_a_search_that_hides_its_song(self):
        row, song = self._last_song_file()
        self.window.library_search.setText("no song is called this")
        self.window._library.show_recent(str(song))
        self.assertEqual(self.window.library_search.text(), "")
        self.assertEqual(Path(self.window.difficulty_list.currentItem().data(Qt.UserRole)), song)

    def test_no_osu_db_says_so_and_rates_nothing(self):
        library = self.window._library
        self.assertIsNone(library.star_ratings)
        self.assertFalse(library.library_rating_note.isHidden())
        self.window.song_list.setCurrentRow(0)
        row = self.window.difficulty_list.item(0).data(Qt.UserRole + 1)
        self.assertIsNone(row["stars"])

    def test_a_rating_goes_stale_when_the_file_changes(self):
        folder = Path(self.window.song_list.item(0).data(Qt.UserRole))
        song = next(folder.glob("*.osu"))
        db = self.root / "osu!.db"
        for md5, stale in ((osu_db.file_md5(song), False), ("0" * 32, True)):
            with self.subTest(stale=stale):
                self.window._library.star_ratings = osu_db.read(self._write_db(db, song, md5))
                self.window._library.song_selected(0)
                # Hashed off the GUI thread, and marked when it is done.
                self.window._library.jobs.drain()
                row = self.window.difficulty_list.item(0).data(Qt.UserRole + 1)
                self.assertAlmostEqual(row["stars"], 4.5, places=5)
                self.assertEqual(row["stale"], stale)

    def test_background_work_is_delivered_on_the_gui_thread(self):
        import threading

        seen = []
        jobs = self.window._library.jobs
        jobs.submit(threading.get_ident, lambda worker: seen.append((worker, threading.get_ident())))
        jobs.drain()
        (worker, deliverer), = seen
        self.assertNotEqual(worker, threading.main_thread().ident, "the work left the GUI thread")
        self.assertEqual(deliverer, threading.main_thread().ident, "and came back to it")

    def test_the_banner_art_is_decoded_off_the_gui_thread_and_kept_only_if_still_wanted(self):
        library = self.window._library
        self.window.song_list.setCurrentRow(0)
        key = library._banner_art_key
        library._banner_art_key = ("somewhere", "else")  # the user moved on
        library.jobs.drain()
        library._banner_art_decoded(key, None)
        self.assertIsNone(library.song_banner._art)

    def test_the_chart_fades_out_on_a_new_song_and_in_once_loaded(self):
        library = self.window._library
        self.window.song_list.setCurrentRow(0)
        library._load_chart_preview()
        library.jobs.drain()  # parsed on a worker
        self.assertEqual(library.chart_preview.chart_opacity, 1.0)
        self.window.song_list.setCurrentRow(1)
        self.assertEqual(library.chart_preview.chart_opacity, 0.0, "the old chart goes")
        library._load_chart_preview()
        library.jobs.drain()  # parsed on a worker
        self.assertEqual(library.chart_preview.chart_opacity, 1.0, "the new one comes")

    def test_the_preview_follows_the_difficultys_own_audio(self):
        """One folder, two songs: each difficulty previews its own file."""
        folder = Path(self.window.song_list.item(0).data(Qt.UserRole))
        first = next(folder.glob("*.osu"))
        (folder / "other.mp3").write_bytes(b"")
        (folder / "other.osu").write_bytes(first.read_bytes()
                                            .replace(b"AudioFilename: audio.mp3", b"AudioFilename: other.mp3")
                                            .replace(b"Version:Oni", b"Version:Zzz"))
        self.window._library.start_scan()
        while self.window._scan_iterator is not None:
            self.window._library.scan_step()
        row = next(i for i in range(self.window.song_list.count())
                   if self.window.song_list.item(i).data(Qt.UserRole) == str(folder))
        self.window._library.song_selected(row)
        queued = {}
        for index in range(self.window.difficulty_list.count()):
            self.window.difficulty_list.setCurrentRow(index)
            queued[self.window.difficulty_list.item(index).text()] = self.window._library._preview_pending[0].name
        self.assertEqual(queued, {"Oni": "audio.mp3", "Zzz": "other.mp3"})

    def test_the_banner_follows_the_difficultys_own_background(self):
        """Same folder, a background per difficulty: the art is the selected
        one's, and moving between two that share one reloads nothing."""
        folder = Path(self.window.song_list.item(0).data(Qt.UserRole))
        first = next(folder.glob("*.osu"))
        (folder / "other.osu").write_bytes(first.read_bytes()
                                            .replace(b'0,0,"bg.jpg"', b'0,0,"other.jpg"')
                                            .replace(b"Version:Oni", b"Version:Zzz"))
        self.window._library.start_scan()
        while self.window._scan_iterator is not None:
            self.window._library.scan_step()
        row = next(i for i in range(self.window.song_list.count())
                   if self.window.song_list.item(i).data(Qt.UserRole) == str(folder))
        library = self.window._library
        library.song_selected(row)
        shown = {}
        for index in range(self.window.difficulty_list.count()):
            self.window.difficulty_list.setCurrentRow(index)
            shown[self.window.difficulty_list.item(index).text()] = library._banner_art_key[1]
        self.assertEqual(shown, {"Oni": "bg.jpg", "Zzz": "other.jpg"})

    def test_the_beat_line_keeps_each_difficultys_own_tempo(self):
        """Same folder, different audio and timing: the beat is the selected
        difficulty's red lines, not the first difficulty's BPM."""
        folder = Path(self.window.song_list.item(0).data(Qt.UserRole))
        first = next(folder.glob("*.osu"))
        (folder / "other.mp3").write_bytes(b"")
        (folder / "other.osu").write_bytes(first.read_bytes()
                                            .replace(b"AudioFilename: audio.mp3", b"AudioFilename: other.mp3")
                                            .replace(b"0,500,4,1,0,60,1,0", b"0,300,4,1,0,60,1,0")
                                            .replace(b"6000,400,4,1,0,70,1,8", b"6000,250,4,1,0,70,1,8")
                                            .replace(b"Version:Oni", b"Version:Zzz"))
        self.window._library.start_scan()
        while self.window._scan_iterator is not None:
            self.window._library.scan_step()
        row = next(i for i in range(self.window.song_list.count())
                   if self.window.song_list.item(i).data(Qt.UserRole) == str(folder))
        self.window._library.song_selected(row)
        banner = self.window._library.song_banner
        beats = {}
        for index in range(self.window.difficulty_list.count()):
            self.window.difficulty_list.setCurrentRow(index)
            beats[self.window.difficulty_list.item(index).text()] = [beat for _t, beat in banner._tempo]
        self.assertEqual(beats, {"Oni": [500.0, 400.0], "Zzz": [300.0, 250.0]})

    def test_the_beat_is_phased_from_the_red_line_in_force(self):
        banner = self.window._library.song_banner
        banner.set_tempo([(0.0, 500.0), (1000.0, 400.0)], lambda: 0.0)
        self.assertAlmostEqual(banner.beat_phase_at(250.0), 0.5)
        self.assertAlmostEqual(banner.beat_phase_at(1200.0), 0.5)   # 200 into a 400ms beat
        # Past 300 BPM the flash halves until it is a pulse, not a flicker.
        banner.set_tempo([(0.0, 100.0)], lambda: 0.0)
        self.assertAlmostEqual(banner.beat_phase_at(100.0), 0.5)

    def _record_ui_sounds(self):
        played = []
        self.window._library.play_ui_sound = played.append
        return played

    def test_changing_page_clicks_at_full_level(self):
        from ui import gui

        played = []
        self.window._library.play_ui_sound = lambda key, volume=None: played.append((key, volume))
        self.window._switch_page(gui.PAGE_EDITOR)
        self.window._switch_page(gui.PAGE_EDITOR)  # already there: no page changed
        self.window._show_page(gui.PAGE_LIBRARY)   # a map loading, not a click
        self.assertEqual(played, [("select_difficulty", 1.0)])
        # Off in Settings, a page change is silent.
        self.window.settings.bool_value = lambda key, default=False: key != "audio/page_click" and default
        self.window._switch_page(gui.PAGE_EDITOR)
        self.assertEqual(len(played), 1)

    def test_a_new_song_expands_and_a_new_difficulty_clicks(self):
        played = self._record_ui_sounds()
        self.window.song_list.setCurrentRow(0)
        self.key(self.window.song_list, Qt.Key_Down)
        # The first difficulty coming up with the song is not a second pick.
        self.assertEqual(played, ["select_expand", "select_expand"])
        folder = Path(self.window.song_list.currentItem().data(Qt.UserRole))
        first = next(folder.glob("*.osu"))
        (folder / "other.osu").write_bytes(first.read_bytes().replace(b"Version:Oni", b"Version:Zzz"))
        self.window._library.start_scan()
        while self.window._scan_iterator is not None:
            self.window._library.scan_step()
        played.clear()
        self.window.difficulty_list.setCurrentRow(1)
        self.assertEqual(played, ["select_difficulty"])

    def test_a_search_that_keeps_the_song_is_silent(self):
        self.window.song_list.setCurrentRow(0)
        played = self._record_ui_sounds()
        self.window._library.rebuild_song_list()
        self.assertEqual(played, [])

    def _current_folder(self):
        return self.window.song_list.currentItem().data(Qt.UserRole)

    def test_the_player_order_is_fixed_for_the_session(self):
        library = self.window._library
        first = list(library.song_order())
        self.assertEqual(sorted(first), sorted(library.library_songs))
        self.assertEqual(library.song_order(), first)
        library.library_songs[self.root / "Late - Addition"] = [object()]
        self.assertEqual(library.song_order()[:-1], first, "a late song joins the end; nothing moves")
        self.assertEqual(library.song_order()[-1], self.root / "Late - Addition")

    def test_next_selects_the_song_and_previous_comes_back(self):
        library = self.window._library
        library.song_order()
        self.window.song_list.setCurrentRow(0)
        start = self._current_folder()
        library.step_song(1)
        moved = self._current_folder()
        self.assertNotEqual(moved, start)
        self.assertEqual(Path(moved), library.song_order()[(library.song_order().index(Path(start)) + 1) % 2])
        library.step_song(-1)
        self.assertEqual(self._current_folder(), start)

    def test_the_next_button_steps(self):
        library = self.window._library
        self.window.song_list.setCurrentRow(0)
        start = self._current_folder()
        library.player_buttons["next"].click()
        self.assertNotEqual(self._current_folder(), start)

    def test_space_toggles_the_preview_but_types_inside_a_query(self):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        library = self.window._library
        toggles = []
        library.toggle_preview = lambda: toggles.append(True)

        def space(widget):
            return library.keys.eventFilter(widget, QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier, " "))

        self.assertTrue(space(self.window.song_list))
        self.assertTrue(space(library.library_search))
        library.library_search.setText("yotsuya")
        self.assertFalse(space(library.library_search))
        self.assertEqual(len(toggles), 2)

    def test_space_on_the_song_list_after_editing_is_the_song_lists(self):
        """The Space shortcut is application-wide, so on the song list it fires
        before `LibraryKeys` sees the key. With a difficulty opened earlier it
        used to restart the editor's song from the editor's playhead."""
        from ui import gui

        library = self.window._library
        toggles, editor = [], []
        library.toggle_preview = lambda: toggles.append(True)
        self.window.toggle_playback = lambda: editor.append(True)
        self.window._show_page(gui.PAGE_LIBRARY)
        # A difficulty was opened earlier: `document` reads it from the state.
        self.window.state = types.SimpleNamespace(document=object())
        self.window.play_shortcut.activated.emit()
        self.assertEqual((len(toggles), len(editor)), (1, 0))

    def test_the_seek_band_is_the_whole_song(self):
        """The track between the two times is the whole song (2026-10-09:
        the hidden stripe became a band with a playhead on it)."""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QMouseEvent

        banner = self.window._library.song_banner
        banner.resize(400, 200)
        seen = []
        banner.seek_fraction.connect(seen.append)

        def press(x, y):
            event = QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y), QPointF(x, y),
                                Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
            banner.mousePressEvent(event)
            release = QMouseEvent(QEvent.MouseButtonRelease, QPointF(x, y), QPointF(x, y),
                                  Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
            banner.mouseReleaseEvent(release)

        def quarter():
            track = banner.track_rect()
            return track.left() + track.width() / 4

        press(quarter(), 50)
        self.assertEqual(seen, [], "the art above the band is not a seek bar")
        press(quarter(), 195)
        self.assertAlmostEqual(seen[-1], 0.25)
        banner.resize(800, 200)  # the splitter widened the pane
        press(quarter(), 195)
        self.assertAlmostEqual(seen[-1], 0.25)

    def test_a_new_song_rewinds_the_playhead_then_glides_out(self):
        # Owner, 2026-10-09: back to the start, then out to the preview point.
        # The banner reads reduced_motion through its own module.
        from ui import song_select

        banner = self.window._library.song_banner
        banner.set_tempo([(0.0, 500.0)], lambda: 30000.0)
        banner.set_duration(100000)
        banner._tick_beat()                     # drawn at 0.30
        with unittest.mock.patch.object(song_select, "reduced_motion", return_value=False):
            banner.glide(rewind=True)
        banner._now = 60000.0                   # the new song's preview point
        path = []
        for t in (0.0, banner.REWIND_SHARE, 0.7, 1.0 - 1e-9):
            banner._glide_t = t
            path.append(round(banner._shown_fraction(), 3))
        banner.glide_animation.stop()
        self.assertEqual(path[0], 0.3, "it leaves from where it was drawn")
        self.assertEqual(path[1], 0.0, "it reaches the start")
        self.assertTrue(0.0 < path[2] < 0.6, "then heads out")
        self.assertAlmostEqual(path[3], 0.6, places=2)

    def test_the_playhead_is_the_clock_over_the_songs_length(self):
        banner = self.window._library.song_banner
        banner.set_tempo([(0.0, 500.0)], lambda: 30000.0)
        banner.set_duration(120000)
        banner._tick_beat()
        self.assertAlmostEqual(banner._shown_fraction(), 0.25)

    def test_a_fraction_seeks_the_song_lists_player_only(self):
        from ui import gui

        library = self.window._library
        seeks = []

        class Player:
            def duration(self):
                return 200000
            def setPosition(self, ms):
                seeks.append(ms)
            def playbackState(self):
                return gui.QMediaPlayer.PausedState
            def position(self):
                return seeks[-1] if seeks else 0

        library.preview_player = Player()
        library._seek_preview_fraction(0.5)
        self.assertEqual(seeks, [100000])
        self.assertEqual(library.chart_preview.current_time, 100000)
        library.preview_player = None

    def test_the_chart_preview_shows_the_selected_difficulty(self):
        from ui import gui

        library = self.window._library
        self.window.song_list.setCurrentRow(0)
        library.difficulty_changed()
        library._load_chart_preview()
        library.jobs.drain()  # parsed on a worker
        self.assertTrue(library.chart_preview.notes)
        library.chart_preview.resize(600, 300)
        natural = common.osu_screen_height_for(600)
        self.assertEqual(library.chart_preview.height(), round(natural * 0.67))
        self.assertFalse(library.chart_preview.follows_view_opacity)

    def test_the_chart_preview_comes_and_goes_with_the_song(self):
        # Like the difficulties (owner, 2026-10-08): an empty black band
        # before anything was picked read as broken.
        library = self.window._library
        self.window.show()
        self.window.song_list.setCurrentRow(-1)
        self.assertFalse(library.chart_preview.isVisible())
        self.window.song_list.setCurrentRow(0)
        library.difficulty_changed()
        library._load_chart_preview()
        library.jobs.drain()  # parsed on a worker
        self.assertTrue(library.chart_preview.isVisible())
        self.window.song_list.setCurrentRow(-1)
        self.assertFalse(library.chart_preview.isVisible())

    def test_the_song_select_stage_is_bare(self):
        # Black, no skin bar, no cover past osu!'s edge (the gray strip on
        # the right), and the hit target nearer the left than in game.
        from ui import gui

        preview = self.window._library.chart_preview
        preview.resize(900, 114)
        self.assertTrue(preview.plain_stage)
        self.assertEqual(preview.osu_edge_x(), 900.0)
        editor_view = gui.GameplayViewerView()
        editor_view.resize(900, 114)
        self.assertLess(preview._hit_x(), editor_view._hit_x())
        image = preview.grab().toImage()
        self.assertEqual(image.pixelColor(899, 2).name(), "#000000")

    def test_stop_with_nothing_playing_is_harmless(self):
        self.window._library.rewind_preview()

    def _write_db(self, db: Path, song: Path, md5: str) -> Path:
        db.write_bytes(_osu_db(osu_db.FLOAT_STARS_VERSION, [(song.parent.name, song.name, md5, 4.5)]))
        return db


if __name__ == "__main__":
    unittest.main()
