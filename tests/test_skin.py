"""Skin loading, against folders built for the test rather than a real install.

The interesting parts are all decisions about *other people's* files -- which
names count, which of them get the note's colour multiplied in, and what
happens when a skin ships half of what is asked for -- so the fixtures here are
deliberately awkward in the ways real skins are.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QApplication

from library import skin

_APP = None


def setUpModule() -> None:
    global _APP
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _APP = QApplication.instance() or QApplication([])


def _write_png(path: Path, colour: QColor, size: int = 16) -> None:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(colour)
    image.save(str(path))


class ElementLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.folder = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_the_2x_variant_wins_over_the_plain_one(self):
        """Same artwork at twice the resolution, and everything is scaled to
        the lane height anyway, so the bigger source is free quality."""
        _write_png(self.folder / "taikohitcircle.png", QColor("white"))
        _write_png(self.folder / "taikohitcircle@2x.png", QColor("white"))
        found = skin.element_paths(self.folder)
        self.assertEqual(found["taikohitcircle"].name, "taikohitcircle@2x.png")

    def test_names_are_matched_case_insensitively(self):
        """osu! does, and skins are inconsistent about it -- a real one ships
        `taiko-Slider@2x` beside `taiko-slider`."""
        _write_png(self.folder / "TaikoHitCircle.PNG", QColor("white"))
        self.assertIn("taikohitcircle", skin.element_paths(self.folder))

    def test_an_animated_element_falls_back_to_its_first_frame(self):
        """A skin that animates an element ships `name-0.png` and no
        `name.png`; one frame is a fair still of it."""
        _write_png(self.folder / "taiko-bar-right-0.png", QColor("white"))
        found = skin.element_paths(self.folder)
        self.assertEqual(found["taiko-bar-right"].name, "taiko-bar-right-0.png")

    def test_the_scrolling_background_is_not_loaded(self):
        """Owner's call: the bar is the lane, with nothing scrolling under it."""
        _write_png(self.folder / "taiko-slider.png", QColor("white"))
        self.assertNotIn("taiko-slider", skin.element_paths(self.folder))

    def test_an_unreadable_folder_does_not_take_the_menu_down(self):
        missing = self.folder / "not-here"
        self.assertEqual(skin.element_paths(missing), {})
        self.assertEqual(skin.sound_paths(missing), {})

    def test_sounds_are_found_by_the_key_the_hitsound_player_uses(self):
        for stem in skin.SKIN_SOUNDS.values():
            (self.folder / f"{stem}.wav").write_bytes(b"RIFF")
        found = skin.sound_paths(self.folder)
        self.assertEqual(set(found), {"normal", "clap", "finish", "whistle"})


class AvailableSkinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _skin(self, name: str) -> Path:
        folder = self.root / name
        folder.mkdir()
        return folder

    def test_a_folder_with_nothing_taiko_in_it_is_not_offered(self):
        """An osu! install collects dozens of skins for other modes. Listing
        one that would change nothing here is a menu of disappointments."""
        mania = self._skin("some mania skin")
        _write_png(mania / "mania-note1.png", QColor("white"))
        self.assertEqual(skin.available_skins(self.root), [])

    def test_a_sounds_only_skin_is_still_offered(self):
        """Plenty of people pick a skin for how it sounds."""
        folder = self._skin("sounds only")
        (folder / "taiko-normal-hitnormal.wav").write_bytes(b"RIFF")
        self.assertEqual(skin.available_skins(self.root), ["sounds only"])

    def test_the_list_is_sorted_case_insensitively(self):
        for name in ("zebra", "Apple", "mango"):
            _write_png(self._skin(name) / "taikohitcircle.png", QColor("white"))
        self.assertEqual(skin.available_skins(self.root), ["Apple", "mango", "zebra"])

    def test_no_skins_folder_is_an_empty_list_not_an_error(self):
        self.assertEqual(skin.available_skins(None), [])
        self.assertIsNone(skin.skins_root(""))


class TintTests(unittest.TestCase):
    def test_a_white_source_becomes_the_colour_exactly(self):
        source = QImage(4, 4, QImage.Format_ARGB32)
        source.fill(QColor(255, 255, 255))
        result = skin.tinted(QPixmap.fromImage(source), QColor(240, 60, 45)).toImage()
        self.assertEqual(result.pixelColor(2, 2), QColor(240, 60, 45))

    def test_black_stays_black_because_the_tint_multiplies(self):
        """The bug this replaces: `taiko-roll-middle` is black along its edges
        around a translucent white core, and replacing its colour flattened it
        into one slab of the drumroll colour."""
        source = QImage(4, 4, QImage.Format_ARGB32)
        source.fill(QColor(0, 0, 0, 179))
        result = skin.tinted(QPixmap.fromImage(source), QColor(255, 200, 60)).toImage()
        pixel = result.pixelColor(2, 2)
        self.assertEqual((pixel.red(), pixel.green(), pixel.blue()), (0, 0, 0))

    def test_alpha_is_the_shape_and_is_left_alone(self):
        source = QImage(4, 4, QImage.Format_ARGB32)
        source.fill(QColor(255, 255, 255, 90))
        result = skin.tinted(QPixmap.fromImage(source), QColor(255, 200, 60)).toImage()
        self.assertEqual(result.pixelColor(2, 2).alpha(), 90)


class TaikoSkinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.folder = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_no_folder_is_a_falsy_skin_that_answers_nothing(self):
        """What the built-in default is: every lookup misses, so every caller
        falls through to its own drawing."""
        empty = skin.TaikoSkin()
        self.assertFalse(empty)
        self.assertIsNone(empty.scaled("taikohitcircle", 64, QColor("red")))

    def test_a_partial_skin_supplies_what_it_has(self):
        """Per element, not per skin: a skin with notes but no drumroll art
        keeps its notes and gets the built-in drawing for the rest."""
        _write_png(self.folder / "taikohitcircle.png", QColor("white"))
        loaded = skin.TaikoSkin(self.folder)
        self.assertTrue(loaded)
        self.assertTrue(loaded.has("taikohitcircle"))
        self.assertFalse(loaded.has("taiko-roll-middle"))
        self.assertIsNone(loaded.scaled("taiko-roll-middle", 64))

    def test_scaling_is_to_the_requested_height(self):
        _write_png(self.folder / "taikohitcircle.png", QColor("white"), size=32)
        loaded = skin.TaikoSkin(self.folder)
        self.assertEqual(loaded.scaled("taikohitcircle", 80, QColor("red")).height(), 80)

    def test_a_one_pixel_track_survives_scaling_down_to_a_note(self):
        """`taiko-roll-middle` is a 1px column (2px at @2x). Scaled to a 44px
        note by aspect it was 0.34px wide, which Qt returns as a null pixmap,
        and every real drumroll in the editor lost its whole track."""
        track = QImage(2, 256, QImage.Format_ARGB32)
        track.fill(QColor("white"))
        track.save(str(self.folder / "taiko-roll-middle.png"))
        loaded = skin.TaikoSkin(self.folder)
        for diameter in (30, 44, 62):
            scaled = loaded.scaled("taiko-roll-middle", diameter, QColor("yellow"))
            self.assertFalse(scaled.isNull(), diameter)
            self.assertEqual((scaled.width(), scaled.height()), (1, diameter))

    def test_the_same_request_twice_is_the_same_cached_pixmap(self):
        """Rescaling a 256x256 source for every note on every frame is the kind
        of per-frame work that redoes static work."""
        _write_png(self.folder / "taikohitcircle.png", QColor("white"))
        loaded = skin.TaikoSkin(self.folder)
        first = loaded.scaled("taikohitcircle", 64, QColor("red"))
        self.assertIs(loaded.scaled("taikohitcircle", 64, QColor("red")), first)
        loaded.clear_cache()
        self.assertIsNot(loaded.scaled("taikohitcircle", 64, QColor("red")), first)


class DevicePixelRatioTests(unittest.TestCase):
    """Qt stamps `devicePixelRatio = 2.0` on anything named `@2x`, and every
    derived pixmap inherits it -- so `drawPixmap(point, ...)` drew those at
    half size while an element the same skin ships without `@2x` came out full
    size. Half of one skin's playfield at half scale."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.folder = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_a_2x_source_is_normalised_to_ratio_one(self):
        _write_png(self.folder / "taikohitcircle@2x.png", QColor("white"), size=64)
        loaded = skin.TaikoSkin(self.folder)
        self.assertEqual(loaded._sources["taikohitcircle"].devicePixelRatio(), 1.0)

    def test_a_scaled_element_is_exactly_the_requested_pixels(self):
        _write_png(self.folder / "taiko-bar-right@2x.png", QColor("white"), size=64)
        loaded = skin.TaikoSkin(self.folder)
        stretched = loaded.stretched("taiko-bar-right", 400, 120)
        self.assertEqual((stretched.width(), stretched.height()), (400, 120))
        self.assertEqual(stretched.devicePixelRatio(), 1.0)


class SilhouetteTests(unittest.TestCase):
    """`silhouette` turns artwork into the light it takes.

    The colour is replaced rather than multiplied -- this is light, not a tint,
    so it must not carry the artwork's own hue. But the alpha is scaled by the
    artwork's luminance, which is what stops the dark parts of a note lighting
    up: `tinted` multiplies, so black stays black however it is coloured, and
    light has to agree with that or the two disagree about what the art is.
    """

    @staticmethod
    def _light(rgba, colour):
        source = QImage(4, 4, QImage.Format_ARGB32)
        source.fill(QColor(*rgba))
        return skin.silhouette(QPixmap.fromImage(source), QColor(colour)).toImage()

    def test_white_artwork_takes_the_colour_in_full(self):
        pixel = self._light((255, 255, 255, 255), QColor(251, 183, 6)).pixelColor(2, 2)
        self.assertEqual(
            (pixel.red(), pixel.green(), pixel.blue(), pixel.alpha()),
            (251, 183, 6, 255),
        )

    def test_black_artwork_takes_no_light_at_all(self):
        """The eyes and mouth a skinner draws into `taikohitcircle` -- 4% of
        that element is dark ink in one real skin, and taking the shape alone
        glowed them out."""
        self.assertEqual(
            self._light((0, 0, 0, 255), "white").pixelColor(2, 2).alpha(), 0)

    def test_mid_grey_takes_half(self):
        alpha = self._light((128, 128, 128, 255), "white").pixelColor(2, 2).alpha()
        self.assertAlmostEqual(alpha, 128, delta=4)

    def test_the_shape_still_bounds_it(self):
        """Luminance scales the light; the artwork's own alpha still says where
        there is anything to light at all."""
        self.assertEqual(
            self._light((255, 255, 255, 90), "white").pixelColor(2, 2).alpha(), 90)
        self.assertEqual(
            self._light((255, 255, 255, 0), "white").pixelColor(2, 2).alpha(), 0)

    def test_a_saturated_blue_does_not_glow_like_white(self):
        """Rec.601 luma rather than a plain average: the eye weights green far
        above blue, and a kat is blue."""
        blue = self._light((0, 0, 255, 255), "white").pixelColor(2, 2).alpha()
        white = self._light((255, 255, 255, 255), "white").pixelColor(2, 2).alpha()
        self.assertLess(blue, white // 3)


class PlayfieldMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_a_standard_skin_is_still_not_offered(self):
        """`approachcircle` is a playfield element here and an osu!standard
        file everywhere -- almost every skin of any mode ships one, so listing
        on it would fill the menu with skins that change nothing. Only the
        taiko-prefixed names count as a signature."""
        folder = self.root / "a standard skin"
        folder.mkdir()
        for name in ("approachcircle", "hitcircle"):
            _write_png(folder / f"{name}.png", QColor("white"))
        self.assertEqual(skin.available_skins(self.root), [])

    def test_a_playfield_only_taiko_skin_is_offered(self):
        folder = self.root / "bar only"
        folder.mkdir()
        _write_png(folder / "taiko-bar-right.png", QColor("white"))
        self.assertEqual(skin.available_skins(self.root), ["bar only"])


class UiSoundTests(unittest.TestCase):
    def test_the_select_sounds_are_found_in_any_osu_format_and_case(self):
        import shutil
        import tempfile
        from library.skin import UI_SOUNDS, sound_paths
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "Select-Expand.ogg").write_bytes(b"")
        found = sound_paths(folder, UI_SOUNDS)
        self.assertEqual(set(found), {"select_expand"})
        self.assertEqual(found["select_expand"].name, "Select-Expand.ogg")
        # And it is not mistaken for a hitsound.
        self.assertEqual(sound_paths(folder), {})


if __name__ == "__main__":
    unittest.main()
