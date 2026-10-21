"""The Mappack Creator: what each difficulty becomes, and files stored once."""
import tempfile
import unittest
from pathlib import Path

from library import mappack
from osu_io import metadata
from osu_io.parser import parse_osu

MAP = """osu file format v14

[General]
AudioFilename: Audio.mp3
Mode: 1

[Metadata]
Title:{title}
TitleUnicode:{title}
Artist:{artist}
ArtistUnicode:{artist}
Creator:mapper
Version:{version}
Source:src
Tags:a b
BeatmapID:111
BeatmapSetID:222

[Difficulty]
OverallDifficulty:5

[Events]
//Background and Video events
0,0,"bg.jpg",0,0
Video,0,"video.mp4"
Sprite,Foreground,Centre,"sb.png",320,240
 F,0,0,1000,1,0
//Break Periods
2,5000,6000

[TimingPoints]
0,500,4,1,0,100,1,0

[HitObjects]
256,192,0,1,0,0:0:0:0:
256,192,{last},1,0,0:0:0:0:
"""


def make_set(root: Path, name: str, audio: bytes, image: bytes, last: int = 1000, **fields) -> Path:
    folder = root / name
    folder.mkdir()
    # "audio.mp3" on disk, "Audio.mp3" in the file, as plenty of maps have it.
    (folder / "audio.mp3").write_bytes(audio)
    (folder / "bg.jpg").write_bytes(image)
    path = folder / f"{name}.osu"
    path.write_text(MAP.format(last=last, **fields), encoding="utf-8")
    return path


class PlaceFileTests(unittest.TestCase):
    def test_identical_bytes_are_one_file_and_different_ones_are_renamed(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "a").mkdir(); (root / "b").mkdir(); (root / "c").mkdir(); (root / "out").mkdir()
            (root / "a" / "bg.jpg").write_bytes(b"one")
            (root / "b" / "other.jpg").write_bytes(b"one")
            (root / "c" / "bg.jpg").write_bytes(b"two")
            first = mappack.place_file(root / "a" / "bg.jpg", root / "out")
            same = mappack.place_file(root / "b" / "other.jpg", root / "out")
            different = mappack.place_file(root / "c" / "bg.jpg", root / "out")
            self.assertEqual(first, same)
            self.assertEqual(different.name, "bg_2.jpg")
            self.assertEqual(len(list((root / "out").iterdir())), 2)


class ResolveTests(unittest.TestCase):
    def test_a_name_in_the_wrong_case_still_finds_its_file(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "audio.mp3").write_bytes(b"x")
            found = mappack.resolve_ci(Path(root), "AUDIO.MP3")
            self.assertIsNotNone(found)
            self.assertTrue(found.is_file())
            self.assertIsNone(mappack.resolve_ci(Path(root), "other.mp3"))

    def test_a_name_that_leaves_the_set_finds_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "secret.txt").write_bytes(b"x")
            (Path(root) / "set").mkdir()
            self.assertIsNone(mappack.resolve_ci(Path(root) / "set", "../secret.txt"))
            self.assertIsNone(mappack.resolve_ci(Path(root) / "set", str(Path(root) / "secret.txt")))


class PackTests(unittest.TestCase):
    def build(self, include_delete=False):
        root = Path(self.tmp.name)
        a = make_set(root, "set a", b"song", b"image", title="Song A", artist="Art A", version="Oni")
        b = make_set(root, "set b", b"song", b"image", last=200000, title="Song B", artist="Art B",
                     version="Inner")
        entries = [mappack.Entry(p, mappack.default_version(metadata.read_keys(parse_osu(p).lines, "Metadata")))
                   for p in (a, b)]
        songs = root / "Songs"
        songs.mkdir()
        return mappack.build_pack(entries, "My Pack", "me", songs, include_delete=include_delete)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_difficulty_is_rewritten_for_submission(self):
        report = self.build()
        self.assertEqual(report.folder.name, "Various Artists - My Pack")
        values = metadata.read_keys(parse_osu(report.files[0]).lines, "Metadata")
        self.assertEqual(values["BeatmapID"], "0")
        self.assertEqual(values["BeatmapSetID"], "-1")
        self.assertEqual((values["Title"], values["TitleUnicode"], values["Creator"]), ("My Pack", "My Pack", "me"))
        self.assertEqual((values["Artist"], values["ArtistUnicode"]), ("Various Artists",) * 2)
        self.assertEqual((values["Tags"], values["Source"]), ("a b", "src"))
        self.assertEqual(values["Version"], "Art A - Song A (mapper) [Oni]")

    def test_shared_files_are_stored_once_and_storyboards_go(self):
        report = self.build()
        names = sorted(p.name.lower() for p in report.folder.iterdir() if p.suffix != ".osu")
        self.assertEqual(names, ["audio.mp3", "bg.jpg"])
        text = report.files[0].read_text(encoding="utf-8")
        self.assertIn('0,0,"bg.jpg",0,0', text)
        self.assertIn("2,5000,6000", text)
        self.assertNotIn("video.mp4", text)
        self.assertNotIn("sb.png", text)
        self.assertNotIn(" F,0,0,1000", text)

    def test_the_longest_difficulty_sets_the_limit(self):
        report = self.build()
        self.assertEqual(report.longest, ("Art B - Song B (mapper) [Inner]", 200000))
        self.assertEqual(report.limit_bytes, mappack.size_limit_bytes(200000))

    def test_delete_difficulty_is_two_objects_fifteen_minutes_apart(self):
        report = self.build(include_delete=True)
        delete = parse_osu(report.folder / "Various Artists - My Pack (me) [Delete].osu")
        self.assertEqual([n.time for n in delete.hit_objects], [0, 900000])
        self.assertEqual(report.longest, ("Delete", 900000))
        self.assertTrue((report.folder / "delete.mp3").is_file())
        self.assertEqual(report.limit_bytes, 100 * mappack.MB)

    def test_an_existing_folder_is_never_written_into(self):
        self.build()
        with self.assertRaises(FileExistsError):
            self.build()


class LimitTests(unittest.TestCase):
    def test_five_plus_ten_a_minute_capped_at_a_hundred(self):
        self.assertEqual(mappack.size_limit_bytes(0), 5 * mappack.MB)
        self.assertEqual(mappack.size_limit_bytes(60000), 15 * mappack.MB)
        self.assertEqual(mappack.size_limit_bytes(30 * 60000), 100 * mappack.MB)

    def test_silent_audio_is_the_length_asked_for(self):
        data = mappack.silent_mp3(900000)
        self.assertGreaterEqual(len(data) // 72 * 72, 900000)  # 72-byte, 72ms frames
        self.assertEqual(data[:4], bytes((0xFF, 0xE3, 0x18, 0xC0)))


if __name__ == "__main__":
    unittest.main()
