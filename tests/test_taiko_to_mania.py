"""TaikoToMania: the autoplay's hands as columns, the scroll at the chart BPM."""
import tempfile
import unittest
from pathlib import Path

from chart import autoplay
from chart.taiko_to_mania import convert
from osu_io import metadata
from osu_io.parser import parse_osu

# 200 BPM for 3s, a 400 BPM second, then 200 again: 200 is the chart BPM.
HEAD = """osu file format v14

[General]
AudioFilename: audio.mp3
Mode: 1

[Metadata]
Title:t
Artist:a
Creator:c
Version:Oni
BeatmapID:123

[Difficulty]
HPDrainRate:5
CircleSize:2
OverallDifficulty:6
ApproachRate:7
SliderMultiplier:1.4
SliderTickRate:1

[TimingPoints]
0,300,4,1,0,100,1,0
1000,-50,4,1,0,100,0,0
2000,-100,4,1,0,100,0,1
3000,150,4,1,0,100,1,0
3500,-100,4,1,0,60,0,0
4000,300,4,1,0,100,1,0

[HitObjects]
"""


def write_map(folder: Path, objects: str) -> Path:
    path = folder / "a - t (c) [Oni].osu"
    path.write_text(HEAD + objects, encoding="utf-8")
    return path


def converted(objects: str, **options):
    with tempfile.TemporaryDirectory() as folder:
        out = convert(parse_osu(write_map(Path(folder), objects)), **options)
        return parse_osu(out), out.read_text(encoding="utf-8")


# ddkk at 1/4 of 300ms, then a kat finisher.
KDDK = "".join(f"256,192,{t},1,{hs},0:0:0:0:\n" for t, hs in
               ((0, 0), (75, 0), (150, 8), (225, 8), (1200, 12)))


class ColumnTests(unittest.TestCase):
    def test_columns_are_the_autoplays_halves(self):
        document, _ = converted(KDDK)
        notes = [(n.time, n.x * 4 // 512 + 1) for n in document.hit_objects]
        source = [(0, False, False), (75, False, False), (150, True, False),
                  (225, True, False), (1200, True, True)]
        expected = [h for halves in autoplay.note_halves(source, 300.0, autoplay.FULL_ALT) for h in halves]
        self.assertEqual([c for _t, c in notes], expected)
        self.assertEqual(len(document.hit_objects), 6)  # the finisher is two

    def test_finisher_single_is_one_object(self):
        document, _ = converted(KDDK, finisher_single=True)
        self.assertEqual(len(document.hit_objects), 5)

    def test_one_key_is_one_column(self):
        document, text = converted(KDDK, keys=1)
        self.assertEqual({n.x for n in document.hit_objects}, {256})
        self.assertEqual(len(document.hit_objects), 5)
        self.assertIn("CircleSize:1", text)

    def test_hitsounds_are_filenames_on_the_notes(self):
        document, _ = converted(KDDK)
        samples = [n.hit_sample for n in document.hit_objects]
        self.assertEqual(samples[0], "0:0:0:0:taiko-normal-hitnormal.wav")
        self.assertEqual(samples[2], "0:0:0:0:taiko-normal-hitclap.wav")
        self.assertEqual(sorted(samples[4:]), ["0:0:0:0:taiko-normal-hitclap.wav",
                                               "0:0:0:0:taiko-normal-hitwhistle.wav"])
        self.assertEqual({n.hit_sound for n in document.hit_objects}, {0})

    def test_drumroll_is_a_hold_and_a_fake_slider_is_gone(self):
        document, _ = converted(
            "256,192,0,2,0,L|330:192,1,70\n"          # half a beat at 1.0x: 150ms
            "256,192,500,2,0,L|330:192,1,-0.0001\n")
        self.assertEqual(len(document.hit_objects), 1)
        hold = document.hit_objects[0]
        self.assertTrue(hold.is_hold)
        self.assertEqual(hold.end_time, 150)
        self.assertIn(hold.x * 4 // 512, (1, 2))  # a don column


class TimingTests(unittest.TestCase):
    def test_scroll_is_normalised_to_the_chart_bpm(self):
        document, text = converted(KDDK)
        greens = {p.time: p.sv_multiplier for p in document.timing_points if not p.uninherited}
        # Red lines at 0 / 3000 / 4000 get 1.0 / 0.5 / 1.0; the taiko SV at
        # 1000 is gone; kiai at 2000 and volume at 3500 stay, at their
        # section's SV.
        self.assertEqual(greens, {0: 1.0, 2000: 1.0, 3000: 0.5, 3500: 0.5, 4000: 1.0})
        kiai = [p.time for p in document.timing_points if p.kiai]
        self.assertEqual(kiai, [2000])
        self.assertIn("Mode:3", text)
        self.assertEqual(metadata.read_keys(document.lines, "Metadata")["BeatmapID"], "0")
        self.assertEqual(document.version, "Oni (4K full alt)")

    def test_the_green_follows_its_red_in_file_order(self):
        document, _ = converted(KDDK)
        at_3000 = [p.uninherited for p in document.timing_points if p.time == 3000]
        self.assertEqual(at_3000, [True, False])


if __name__ == "__main__":
    unittest.main()
