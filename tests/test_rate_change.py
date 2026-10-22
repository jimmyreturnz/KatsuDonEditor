"""The Rate Changer's chart half (`chart.rate_change`)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chart import rate_change
from osu_io import metadata
from osu_io.parser import parse_osu

OSU = """osu file format v14

[General]
AudioFilename: audio.mp3
PreviewTime: 12000
Mode: 1

[Editor]
Bookmarks: 1200,2400

[Metadata]
Title:t
Artist:a
Creator:c
Version:Oni
BeatmapID:123

[Difficulty]
HPDrainRate:5
CircleSize:5
OverallDifficulty:5
ApproachRate:5
SliderMultiplier:1.4
SliderTickRate:1

[Events]
0,0,"bg.jpg",0,0
1,600,"video.mp4"
2,6000,9000

[TimingPoints]
0,500,4,1,0,100,1,0
1200,-50,4,1,0,100,0,0
3000,1,999,1,0,60,1,8
3001,500,1,1,0,60,1,0

[HitObjects]
256,192,1200,1,0,0:0:0:0:
256,192,3000,1,2,0:0:0:0:
256,192,3001,1,0,0:0:0:0:
256,192,4000,12,0,6000,0:0:0:0:
"""


def load(text: str = OSU):
    folder = Path(tempfile.mkdtemp())
    path = folder / "map.osu"
    path.write_text(text, encoding="utf-8")
    return parse_osu(path)


class TimeMapTests(unittest.TestCase):
    def test_a_1ms_pair_stays_apart_at_every_rate(self):
        for rate in (1.1, 1.2, 1.25, 1.3, 1.4, 1.5, 1.75, 2.0):
            times = [t for start in range(1000, 3000, 37) for t in (start, start + 1)]
            mapped = rate_change.time_map(times, rate)
            new = [mapped[t] for t in sorted(times)]
            self.assertEqual(new, sorted(set(new)), rate)

    def test_a_run_on_every_millisecond_shares_rather_than_drifts(self):
        # Nbt-Hwt: never closing a gap pushed such a run 109ms late at 1.5x.
        times = list(range(1000, 1600))
        for rate in (1.2, 1.5, 2.0):
            mapped = rate_change.time_map(times, rate)
            self.assertLessEqual(max(abs(mapped[t] - t / rate) for t in times), 1.5, rate)
            self.assertEqual([mapped[t] for t in times], sorted(mapped[t] for t in times))

    def test_nearest_not_truncated(self):
        # 1001 / 1.5 = 667.33 and 1000 / 1.5 = 666.67: osu-trainer writes 667 and 666.
        self.assertEqual(rate_change.time_map([1000], 1.5)[1000], 667)

    def test_equal_stays_equal_and_slow_rates_spread(self):
        mapped = rate_change.time_map([100, 100, 101], 0.5)
        self.assertEqual(mapped, {100: 200, 101: 202})


class ScaledDocumentTests(unittest.TestCase):
    def test_reds_scale_greens_do_not(self):
        doc = rate_change.scaled_document(load(), 1.25)
        reds = [p for p in doc.timing_points if p.uninherited]
        greens = [p for p in doc.timing_points if not p.uninherited]
        self.assertEqual([p.beat_length for p in reds], [400.0, 0.8, 400.0])
        self.assertEqual([p.beat_length for p in greens], [-50.0])
        self.assertEqual([p.time for p in doc.timing_points], [0, 960, 2400, 2401])

    def test_hide_and_restore_keep_their_note_and_their_gap(self):
        doc = rate_change.scaled_document(load(), 1.2)
        # 3000 / 1.2 = 2500 and 3001 / 1.2 = 2500.83: rounded alone, both 2500.
        self.assertEqual([n.time for n in doc.hit_objects], [1000, 2500, 2501, 3333])
        self.assertEqual([p.time for p in doc.timing_points][2:], [2500, 2501])

    def test_spinner_end_and_passthrough_times(self):
        doc = rate_change.scaled_document(load(), 1.5)
        self.assertEqual(doc.hit_objects[-1].end_time, 4000)
        self.assertEqual(metadata.read_keys(doc.lines, "Editor")["Bookmarks"], "800,1600")
        self.assertEqual(metadata.read_keys(doc.lines, "General")["PreviewTime"], "8000")
        events = [line.strip() for line in doc.lines if line.startswith(("1,", "2,", "0,0"))][:3]
        self.assertEqual(events, ['0,0,"bg.jpg",0,0', '1,400,"video.mp4"', "2,4000,6000"])

    def test_no_preview_stays_none(self):
        doc = rate_change.scaled_document(load(OSU.replace("PreviewTime: 12000", "PreviewTime: -1")), 1.2)
        self.assertEqual(metadata.read_keys(doc.lines, "General")["PreviewTime"], "-1")

    def test_od_like_dt(self):
        self.assertEqual(rate_change.dt_od(5, 1.5), 8.9)  # 35ms great / 1.5 = 23.3ms = OD 8.9
        self.assertEqual(rate_change.dt_od(9, 1.5), 10.0)

    def test_typed_od_and_hp_replace_the_charts(self):
        doc = rate_change.scaled_document(load(), 1.5, od=7, hp=3.5)
        values = metadata.read_keys(doc.lines, "Difficulty")
        self.assertEqual((values["OverallDifficulty"], values["HPDrainRate"]), ("7", "3.5"))
        untouched = metadata.read_keys(rate_change.scaled_document(load(), 1.5).lines, "Difficulty")
        self.assertEqual(untouched["OverallDifficulty"], "5")

    def test_a_file_without_ar_keeps_its_own_od_as_ar(self):
        source = load(OSU.replace("ApproachRate:5\n", "").replace("OverallDifficulty:5", "OverallDifficulty:6"))
        written = parse_osu(rate_change.write(source, 1.2, False, "a.ogg", od=9))
        values = metadata.read_keys(written.lines, "Difficulty")
        self.assertEqual((values["OverallDifficulty"], float(values["ApproachRate"])), ("9", 6.0))

    def test_write_round_trips_and_names_the_rate(self):
        source = load()
        path = rate_change.write(source, 1.2, False, "audio 1.2x.ogg")
        written = parse_osu(path)
        self.assertEqual(written.version, "Oni 1.2x (144bpm)")
        self.assertEqual(written.audio_filename, "audio 1.2x.ogg")
        self.assertEqual(metadata.read_keys(written.lines, "Metadata")["BeatmapID"], "0")
        self.assertEqual(len(written.hit_objects), 4)
        with self.assertRaises(FileExistsError):
            rate_change.write(source, 1.2, False, "audio 1.2x.ogg")


if __name__ == "__main__":
    unittest.main()
