"""Slider length <-> duration, against the map's real SliderMultiplier.

The app used to hardcode SliderMultiplier 1.4 and never parse [Difficulty], so
every drumroll on a map authored at any other value had its end drawn at
`1.4 / real` of its true length. The editor timeline compounded that by asking
for the SV out of its uninherited-only point list, where every point reads as
1.0x -- so an SV'd slider's end was wrong there even on a 1.4 map.

The case these tests are built on is the mapper's own::

    256,192,84853,2,0,L|320:192,14,25.75      -> ends at ~85338 ms

which is 14 slides of 25.75 osu!px, i.e. 360.5 px of path in ~485 ms. The hit
object alone does not pin the three numbers down -- osu! charges

    duration = length * slides / (SliderMultiplier * 100 * SV) * beatLength

so any map satisfying `BPM * SliderMultiplier * SV = 445.98` lands on 85338.
223 BPM at SliderMultiplier 2.0 is one such map, and the one used below.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui import gui
from osu_io.parser import parse_osu

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


# 223 BPM. Spelled out rather than computed so the fixture is what osu! would
# have written, and the test shares no arithmetic with the code under test.
BEAT_LENGTH_223 = 269.058296

SLIDER_LINE = "256,192,84853,2,0,L|320:192,14,25.75"
SLIDER_END_MS = 85338.0


def _map(slider_multiplier, timing_lines) -> str:
    difficulty = "" if slider_multiplier is None else (
        "[Difficulty]\r\n"
        "HPDrainRate:5\r\n"
        f"SliderMultiplier:{slider_multiplier}\r\n"
        "SliderTickRate:1\r\n"
        "\r\n"
    )
    points = "".join(f"{line}\r\n" for line in timing_lines)
    return (
        "osu file format v14\r\n"
        "\r\n"
        "[General]\r\n"
        "AudioFilename: audio.mp3\r\n"
        "Mode: 1\r\n"
        "\r\n"
        "[Metadata]\r\n"
        "Version:Oni\r\n"
        "\r\n"
        + difficulty
        + "[TimingPoints]\r\n"
        + points
        + "\r\n"
        "[HitObjects]\r\n"
        f"{SLIDER_LINE}\r\n"
    )


def _parse(directory, slider_multiplier, timing_lines):
    path = Path(directory) / "test.osu"
    path.write_bytes(_map(slider_multiplier, timing_lines).encode("utf-8"))
    return parse_osu(path)


class SliderMultiplierParsingTests(unittest.TestCase):
    """[Difficulty] was skipped entirely by the parser."""

    def _multiplier(self, slider_multiplier):
        with tempfile.TemporaryDirectory() as directory:
            document = _parse(
                directory, slider_multiplier, (f"0,{BEAT_LENGTH_223},4,1,0,60,1,0",)
            )
            return document.slider_multiplier

    def test_the_maps_own_value_is_read(self) -> None:
        self.assertAlmostEqual(self._multiplier("2.0"), 2.0)

    def test_an_integer_value_is_read(self) -> None:
        self.assertAlmostEqual(self._multiplier("2"), 2.0)

    def test_a_map_without_the_section_falls_back_to_the_osu_default(self) -> None:
        self.assertAlmostEqual(self._multiplier(None), gui.SLIDER_MULTIPLIER_ASSUMED)

    def test_an_unparseable_value_falls_back_rather_than_raising(self) -> None:
        self.assertAlmostEqual(self._multiplier("wat"), gui.SLIDER_MULTIPLIER_ASSUMED)


class SliderDurationArithmeticTests(unittest.TestCase):
    """The conversion itself, with no view or document involved."""

    def test_the_mappers_drumroll_ends_where_osu_puts_it(self) -> None:
        duration = gui.duration_for_slider_length(25.75 * 14, BEAT_LENGTH_223, 1.0, 2.0)
        self.assertAlmostEqual(84853 + duration, SLIDER_END_MS, delta=0.05)

    def test_sv_and_the_multiplier_are_interchangeable_in_the_product(self) -> None:
        """`SliderMultiplier * SV` is what the formula divides by, so the same
        object on a 1.0x map with a 2.0x green line on it plays identically."""
        duration = gui.duration_for_slider_length(25.75 * 14, BEAT_LENGTH_223, 2.0, 1.0)
        self.assertAlmostEqual(84853 + duration, SLIDER_END_MS, delta=0.05)

    def test_the_old_hardcoded_multiplier_was_late_by_a_fifth_of_a_second(self) -> None:
        """What the bug looked like: 1.4/2.0 of the real speed, so 208 ms long."""
        assumed = gui.duration_for_slider_length(25.75 * 14, BEAT_LENGTH_223, 1.0)
        real = gui.duration_for_slider_length(25.75 * 14, BEAT_LENGTH_223, 1.0, 2.0)
        self.assertAlmostEqual(assumed - real, 207.85, delta=0.5)

    def test_the_round_trip_through_length_is_exact(self) -> None:
        length = gui.slider_length_for_duration(485.0, BEAT_LENGTH_223, 1.5, 2.0)
        self.assertAlmostEqual(
            gui.duration_for_slider_length(length, BEAT_LENGTH_223, 1.5, 2.0),
            485.0,
            places=6,
        )


class TimelineSliderEndTests(unittest.TestCase):
    """The editor timeline's own end time, which is what draws the tail.

    Two separate defects meet here: the multiplier it converted with, and the
    point list it read the SV out of.
    """

    def _view(self, slider_multiplier, timing_lines):
        with tempfile.TemporaryDirectory() as directory:
            document = _parse(directory, slider_multiplier, timing_lines)
        view = gui.TimelineGameplay()
        view.load_document(document)
        return view, view.notes[0]

    def test_the_end_uses_the_maps_multiplier(self) -> None:
        view, slider = self._view("2.0", (f"0,{BEAT_LENGTH_223},4,1,0,60,1,0",))
        self.assertAlmostEqual(view._note_end_time(slider), SLIDER_END_MS, delta=0.05)

    def test_the_end_uses_the_sv_in_force_at_the_slider(self) -> None:
        """The green line is inherited, and `timing_points` on this view holds
        only the uninherited ones -- where every point reads as 1.0x."""
        view, slider = self._view(
            "1.0",
            (f"0,{BEAT_LENGTH_223},4,1,0,60,1,0", "0,-50,4,1,0,60,0,0"),
        )
        self.assertAlmostEqual(view._note_end_time(slider), SLIDER_END_MS, delta=0.05)

    def test_a_map_with_no_difficulty_section_still_draws_something(self) -> None:
        view, slider = self._view(None, (f"0,{BEAT_LENGTH_223},4,1,0,60,1,0",))
        # 1.4 assumed: not the mapper's 485 ms, but a positive, drawable end.
        self.assertGreater(view._note_end_time(slider), 84853)


if __name__ == "__main__":
    unittest.main()
