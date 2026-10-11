"""Round-trip guarantees for the .osu I/O layer.

This is the regression net for the writer rework. The property being protected
is narrow and load-bearing:

    write_osu may change the Version line, the ApproachRate and CircleSize
    lines, and hit-object coordinates. It must not change anything else.

Storyboard events, break periods, [Colours], [Editor] bookmarks and assorted
[General] keys are modelled nowhere in this codebase, so passthrough is the only
thing keeping them intact. These tests fail loudly if a future change to section
handling starts disturbing them.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from osu_io.parser import parse_osu
from osu_io.writer import write_osu
from tests.osu_fixtures import ALL_FIXTURES, write_fixture

# Lines write_osu is permitted to rewrite.
MUTABLE_PREFIXES = ("Version:", "ApproachRate:", "CircleSize:")


def classify(line: str, section: str) -> str:
    stripped = line.rstrip("\r\n").strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1]
    return section


def is_mutable(line: str, section: str) -> bool:
    raw = line.rstrip("\r\n")
    if section == "Metadata" and raw.startswith("Version:"):
        return True
    if section == "Difficulty" and raw.startswith(MUTABLE_PREFIXES[1:]):
        return True
    return section == "HitObjects" and bool(raw.strip())


class RoundTripTests(unittest.TestCase):
    def _roundtrip(self, name: str, directory: Path):
        source = write_fixture(directory, name)
        document = parse_osu(source)
        destination = directory / f"{name}-out.osu"
        write_osu(document, destination, document.version)
        return source, destination, document

    def test_only_permitted_lines_change(self):
        """Every line outside Version, AR/CS and [HitObjects] survives byte-exact."""
        for name in ALL_FIXTURES:
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as directory:
                source, destination, document = self._roundtrip(name, Path(directory))

                before = source.read_bytes().decode(document.encoding).splitlines(keepends=True)
                after = destination.read_bytes().decode(document.encoding).splitlines(keepends=True)
                self.assertEqual(len(before), len(after), f"{name}: line count changed")

                section = ""
                for index, (old, new) in enumerate(zip(before, after)):
                    section = classify(old, section)
                    if is_mutable(old, section):
                        continue
                    self.assertEqual(
                        old, new,
                        f"{name}: line {index + 1} in [{section}] changed but should be passthrough",
                    )

    def test_hit_objects_survive_semantically(self):
        for name in ALL_FIXTURES:
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as directory:
                _source, destination, document = self._roundtrip(name, Path(directory))
                reparsed = parse_osu(destination)

                self.assertEqual(len(reparsed.hit_objects), len(document.hit_objects))
                for original, result in zip(document.hit_objects, reparsed.hit_objects):
                    self.assertEqual(
                        (result.x, result.y, result.time, result.type, result.hit_sound),
                        (original.x, original.y, original.time, original.type, original.hit_sound),
                        f"{name}: hit object at {original.time} ms changed",
                    )

    def test_encoding_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            _source, destination, document = self._roundtrip("cp1252", Path(directory))
            self.assertEqual(document.encoding, "cp1252")

            raw = destination.read_bytes()
            with self.assertRaises(UnicodeDecodeError):
                raw.decode("utf-8")
            text = raw.decode("cp1252")
            self.assertIn("Caf\xe9", text)
            self.assertIn("Bj\xf6rk", text)

    def test_approach_rate_and_circle_size_are_forced(self):
        with tempfile.TemporaryDirectory() as directory:
            source = write_fixture(Path(directory), "full_v14")
            document = parse_osu(source)
            destination = Path(directory) / "forced.osu"
            write_osu(document, destination, document.version, force_ar=3.5, force_cs=2.25)

            text = destination.read_text(encoding=document.encoding)
            self.assertIn("ApproachRate:3.5", text)
            self.assertIn("CircleSize:2.25", text)

    def test_version_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            source = write_fixture(Path(directory), "full_v14")
            document = parse_osu(source)
            destination = Path(directory) / "renamed.osu"
            write_osu(document, destination, "Oni (patterned)")

            reparsed = parse_osu(destination)
            self.assertEqual(reparsed.version, "Oni (patterned)")

    def test_moved_notes_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            source = write_fixture(Path(directory), "full_v14")
            document = parse_osu(source)
            for note in document.hit_objects:
                note.x, note.y = note.x + 7, note.y - 5

            destination = Path(directory) / "moved.osu"
            write_osu(document, destination, document.version)
            reparsed = parse_osu(destination)

            for original, result in zip(document.hit_objects, reparsed.hit_objects):
                self.assertEqual((result.x, result.y), (original.x, original.y))


class ReversedSectionOrderTests(unittest.TestCase):
    """R4: [Difficulty] after [HitObjects], with AR and CS both missing.

    The old writer applied a flat `shift` to every hit object's
    source_line_index after inserting the missing keys, which assumed all
    insertions preceded all hit objects. Here they do not, so it indexed past
    the note block and raised IndexError before writing anything.

    Section spans are now recomputed from the patched line list, so no offset
    arithmetic survives and this round-trips.
    """

    def test_reversed_sections_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            source = write_fixture(Path(directory), "reversed_sections")
            document = parse_osu(source)
            destination = Path(directory) / "fixed.osu"
            write_osu(document, destination, document.version)

            reparsed = parse_osu(destination)
            self.assertEqual(
                [(n.x, n.y, n.time) for n in reparsed.hit_objects],
                [(n.x, n.y, n.time) for n in document.hit_objects],
            )
            text = destination.read_text(encoding=document.encoding)
            self.assertIn("ApproachRate:", text)
            self.assertIn("CircleSize:", text)


class FixtureShapeTests(unittest.TestCase):
    """Guards that the fixtures still contain the hazards they were built for."""

    def test_fixtures_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ALL_FIXTURES:
                with self.subTest(fixture=name):
                    document = parse_osu(write_fixture(Path(directory), name))
                    self.assertTrue(document.version, f"{name}: no version parsed")
                    if name == "empty_chart":
                        # Its whole hazard is having none.
                        self.assertEqual(document.hit_objects, [])
                        continue
                    self.assertTrue(document.hit_objects, f"{name}: no hit objects parsed")

    def test_full_fixture_contains_every_hazard(self):
        with tempfile.TemporaryDirectory() as directory:
            source = write_fixture(Path(directory), "full_v14")
            text = source.read_bytes().decode("utf-8")
            self.assertIn("256,192,54692,2,12,L|624:192,643,-0.0001", text)  # fake slider
            self.assertIn(",12,0,7000,", text)                               # spinner with end time
            self.assertIn("2000,-133.33333333333334,", text)                 # inherited SV point
            self.assertIn("6000,400,4,1,0,70,1,8", text)                     # omit-first-barline
            self.assertIn("2,10000,12000", text)                             # break period
            self.assertIn("Combo1 : 255,192,0", text)                        # colours


if __name__ == "__main__":
    unittest.main()


class TimelineMetadataTests(unittest.TestCase):
    """PreviewTime lives in [General], not [Editor]."""

    def _metadata(self, name):
        from ui import gui
        with tempfile.TemporaryDirectory() as directory:
            return gui.editor_timeline_metadata(parse_osu(write_fixture(Path(directory), name)))

    def test_preview_time_is_read_from_general(self):
        bookmarks, preview_time = self._metadata("full_v14")
        self.assertEqual(preview_time, 3000, "PreviewTime is a [General] key")
        self.assertEqual(bookmarks, [2000, 4000], "Bookmarks stay a [Editor] key")

    def test_absent_preview_time_is_none(self):
        _bookmarks, preview_time = self._metadata("legacy_v4")
        self.assertIsNone(preview_time)
