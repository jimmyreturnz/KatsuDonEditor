"""read_version names a difficulty exactly as parse_osu does.

The difficulty dropdown reads only this line from each file in the folder;
a label that decoded or picked a line differently would list a difficulty
under a name the editor does not give it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from osu_io.parser import parse_osu, read_version
from tests.osu_fixtures import ALL_FIXTURES, KNOWN_BAD, write_fixture


class ReadVersionTest(unittest.TestCase):
    def test_every_fixture_matches_the_full_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in (*ALL_FIXTURES, *KNOWN_BAD):
                with self.subTest(name):
                    path = write_fixture(Path(directory), name)
                    self.assertEqual(read_version(path), parse_osu(path).version)

    def test_cp1252_name_decodes_like_the_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.osu"
            path.write_bytes(b"osu file format v14\r\n\r\n[Metadata]\r\nVersion:Caf\xe9\r\n\r\n[HitObjects]\r\n")
            self.assertEqual(read_version(path), "Café")


if __name__ == "__main__":
    unittest.main()
