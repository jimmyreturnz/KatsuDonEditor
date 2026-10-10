import tempfile
import unittest
from pathlib import Path
from ui.gui import resolve_song_asset
from chart.transformer import _compile_safe_expression
from osu_io.parser import parse_osu
from osu_io.writer import write_osu

class PathSecurityTests(unittest.TestCase):
    def test_song_asset_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):resolve_song_asset(Path(d), "../outside.mp3", {".mp3"})
    def test_song_asset_rejects_wrong_extension(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):resolve_song_asset(Path(d), "payload.exe", {".mp3"})

class EquationSecurityTests(unittest.TestCase):
    def test_safe_equation_still_works(self):
        self.assertAlmostEqual(_compile_safe_expression("sin(x)+2", {"x"})(x=0),2.0)
    def test_attribute_access_is_rejected(self):
        with self.assertRaises(ValueError):_compile_safe_expression("x.__class__", {"x"})
    def test_large_exponent_is_rejected(self):
        f=_compile_safe_expression("2^1000", set())
        with self.assertRaises(ValueError):f()

class WriterSecurityTests(unittest.TestCase):
    def test_non_osu_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/"map.osu"
            source.write_text("osu file format v14\n\n[General]\nAudioFilename:a.mp3\n\n[Metadata]\nVersion:Test\n\n[Difficulty]\nCircleSize:5\nApproachRate:8\n\n[HitObjects]\n256,192,1000,1,0,0:0:0:0:\n",encoding="utf-8")
            with self.assertRaises(ValueError):write_osu(parse_osu(source),Path(d)/"out.exe","Test")
if __name__=="__main__":unittest.main()
