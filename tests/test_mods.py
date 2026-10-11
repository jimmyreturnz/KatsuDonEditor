"""The mods: HD, NC/DT/DC/HT, HR/EZ, FL -- one strip on the Editor and Gimmick
pages, the rate in the audio engine, everything visual in the gameplay preview.

The numbers are ppy/osu's own (TaikoModHidden, TaikoModFlashlight,
TaikoModHardRock, TaikoModEasy); see the constants beside MOD_ORDER in gui.py.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from ui import gui
from ui import gameplay_view
from ui import common
from osu_io.parser import parse_osu
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class ModStringTests(unittest.TestCase):
    def test_osu_order_whatever_the_order_they_were_picked_in(self):
        self.assertEqual(gui.mod_string({"FL", "HR", "DT", "HD"}), "HDDTHRFL")
        self.assertEqual(gui.mod_string({"EZ", "NC"}), "NCEZ")
        self.assertEqual(gui.mod_string(set()), "")


class _Window(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")
        # Test settings are seeded from the real ones, and a real session
        # leaves its mods there -- start from none, whatever was last played.
        from app import settings
        settings.SettingsManager().set_value("playback/mods", "")
        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.pitch_calls: list[bool] = []
        real = self.window.player.set_pitch_shift
        self.window.player.set_pitch_shift = lambda pitch: (self.pitch_calls.append(pitch), real(pitch))

    def tearDown(self) -> None:
        self.window._set_mods(set())
        self.window.close()
        self._temp.cleanup()


class ModToggleTests(_Window):
    def test_rate_mods_are_one_choice(self):
        self.window._toggle_mod("DT")
        self.window._toggle_mod("NC")
        self.assertEqual(self.window._mods, {"NC"})

    def test_hr_and_ez_are_one_choice(self):
        self.window._toggle_mod("HR")
        self.window._toggle_mod("EZ")
        self.assertEqual(self.window._mods, {"EZ"})

    def test_the_rest_combine(self):
        for mod in ("HD", "DT", "HR", "FL"):
            self.window._toggle_mod(mod)
        self.assertEqual(gui.mod_string(self.window._mods), "HDDTHRFL")

    def test_a_second_click_takes_it_off(self):
        self.window._toggle_mod("HD")
        self.window._toggle_mod("HD")
        self.assertEqual(self.window._mods, frozenset())

    def test_nightcore_resamples_at_one_and_a_half(self):
        self.window._toggle_mod("NC")
        self.assertEqual(self.window.player.playbackRate(), 1.5)
        self.assertEqual(self.pitch_calls[-1], True)

    def test_double_time_keeps_the_pitch(self):
        self.window._toggle_mod("DT")
        self.assertEqual(self.window.player.playbackRate(), 1.5)
        self.assertEqual(self.pitch_calls[-1], False)

    def test_dropping_the_rate_mod_goes_back_to_full_speed(self):
        self.window._toggle_mod("HT")
        self.window._toggle_mod("HT")
        self.assertEqual(self.window.player.playbackRate(), 1.0)

    def test_the_song_select_preview_ignores_the_rate_mod(self):
        library = self.window._library
        library.preview_player = gui.QMediaPlayer(self.window)
        preview = library.preview_player
        library.preview_fade = gui.QVariantAnimation(self.window)
        preview.setPlaybackRate(1.5)  # left over from some earlier state
        for mod in ("NC", "DC", "DT", "HT"):
            self.window._toggle_mod(mod)
            library._resume_ms = 0  # as continue_song hands the editor's song back
            library._play_preview()
            self.assertEqual(preview.playbackRate(), 1.0, mod)
        library.preview_fade.stop()
        library.preview_player = None  # no fade was made; closeEvent would reach for it

    def test_a_speed_button_replaces_the_rate_mod(self):
        self.window._toggle_mod("HD")
        self.window._toggle_mod("DT")
        self.window._choose_speed(0.5)
        self.assertEqual(self.window._mods, {"HD"})
        self.assertEqual(self.window.player.playbackRate(), 0.5)
        self.assertEqual(self.pitch_calls[-1], False)

    def test_speed_buttons_show_nothing_pressed_under_double_time(self):
        """There is no 150% button, so DT and NC light none of them."""
        self.window._toggle_mod("DT")
        pressed = [b for b in self.window.editor_playback_speed_buttons if b.isChecked()]
        self.assertEqual(pressed, [])

    def test_half_time_lights_seventy_five_percent_with_it(self):
        """Owner's call: the speed buttons show the speed the song is at,
        so HT and DC light 75% alongside themselves."""
        for mod in ("HT", "DC"):
            self.window._toggle_mod(mod)
            for buttons in (self.window.editor_playback_speed_buttons,
                            self.window.gimmick_playback_speed_buttons):
                pressed = [b.text() for b in buttons if b.isChecked()]
                self.assertEqual(pressed, ["75%"], mod)
            self.assertTrue(self.window._mod_buttons[0][mod].isChecked())

    def test_the_pills_are_grouped_by_what_they_do(self):
        """Layout only: the row reads [HR EZ] [HD FL] [DT NC HT DC] while every
        mod *string* keeps osu!'s order."""
        for buttons in self.window._mod_buttons:
            holders = {b.parentWidget() for b in buttons.values()}
            strip = next(iter(holders)).parentWidget().layout()
            groups = sorted(holders, key=strip.indexOf)
            self.assertEqual(
                [tuple(b.text() for b in group.findChildren(gui.QPushButton)) for group in groups],
                [("HR", "EZ"), ("HD", "FL"), ("DT", "NC", "HT", "DC")])
        self.assertEqual(gui.mod_string({"FL", "DT", "HR", "HD"}), "HDDTHRFL")

    def test_each_pill_wears_its_own_colour(self):
        """Hard-coded, so no theme moves them: none is a pink-palette key."""
        button = self.window._mod_buttons[0]["HD"]
        self.assertIn("#ffc801", button.styleSheet())
        flashlight = self.window._mod_buttons[0]["FL"].styleSheet()
        self.assertIn("#85858f", flashlight)
        self.assertIn("QPushButton:hover { background: #2c2c30", flashlight)
        # A lit EZ fills with a green white text can be read on.
        self.assertIn("QPushButton:checked { background: #4a8413",
                      self.window._mod_buttons[0]["EZ"].styleSheet())

    def test_every_strip_and_the_chip_agree(self):
        self.window._toggle_mod("HR")
        self.window._toggle_mod("HD")
        self.assertEqual(len(self.window._mod_buttons), 2, "Editor and Gimmick pages")
        for buttons in self.window._mod_buttons:
            self.assertEqual({m for m, b in buttons.items() if b.isChecked()}, {"HD", "HR"})
        self.assertEqual(self.window.mods_chip.text(), "HDHR")
        self.assertFalse(self.window.mods_chip.isHidden())

    def test_mods_come_back_next_time(self):
        self.window._toggle_mod("HD")
        self.window._toggle_mod("DT")
        second = gui.MainWindow()
        try:
            self.assertEqual(second._mods, {"HD", "DT"})
        finally:
            second._set_mods(set())
            second.close()


class PreviewModTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.document = parse_osu(write_fixture(Path(self._temp.name), "full_v14"))  # SM 1.4
        self.view = gui.GameplayViewerView()
        self.view.resize(1882, 170)
        self.view.load_document(self.document)
        self.view.current_time = 1000.0

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_hard_rock_scrolls_at_its_multiplier(self):
        before = self.view.velocity_at(1500.0)
        self.view.set_mods({"HR"})
        self.assertAlmostEqual(self.view.velocity_at(1500.0) / before, 1.4 * 4 / 3, places=6)

    def test_easy_scrolls_at_its_multiplier(self):
        before = self.view.velocity_at(1500.0)
        self.view.set_mods({"EZ"})
        self.assertAlmostEqual(self.view.velocity_at(1500.0) / before, 0.8, places=6)

    def test_hard_rock_shortens_a_drumroll_as_in_game(self):
        roll = next(n for n in self.view.notes if n.is_slider and self.view._end_times.get(n.uid))
        before = self.view._end_times[roll.uid] - roll.time
        self.view.set_mods({"HR"})
        after = self.view._end_times[roll.uid] - roll.time
        self.assertAlmostEqual(before / after, 1.4 * 4 / 3, places=3)

    def test_hidden_fades_across_three_eighths_of_the_scroll(self):
        self.view.set_mods({"HD"})
        hit, length = self.view._hit_x(), self.view._scroll_length_px()
        self.assertEqual(self.view.hidden_alpha(hit + length), 1.0)
        self.assertEqual(self.view.hidden_alpha(hit + length * 0.625), 0.0)
        self.assertAlmostEqual(self.view.hidden_alpha(hit + length * 0.8125), 0.5, places=6)

    def test_without_hidden_everything_is_visible(self):
        self.assertEqual(self.view.hidden_alpha(self.view._hit_x() + 1), 1.0)

    def test_flashlight_darkens_away_from_the_hit_target(self):
        self.view.set_mods({"FL"})
        image = self.view.grab().toImage()
        far = QColor(image.pixel(1100, 10))
        self.assertLess(max(far.red(), far.green(), far.blue()), 8)
        unit = self.view.height() * common.STABLE_UNIT_PER_PLAYFIELD
        near = QColor(image.pixel(round(gameplay_view.FL_CENTRE_STABLE * unit), 85))
        self.assertGreater(max(near.red(), near.green(), near.blue()), 8)

    def test_flashlight_closes_in_with_combo(self):
        self.assertEqual(gameplay_view.flashlight_combo_scale(99), 1.0)
        self.assertEqual(gameplay_view.flashlight_combo_scale(100), 0.8125)
        self.assertEqual(gameplay_view.flashlight_combo_scale(200), 0.625)


if __name__ == "__main__":
    unittest.main()
