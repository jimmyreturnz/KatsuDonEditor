"""songs_from_cache: which cached entries belong to the chosen Songs folder."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from library import song_library as sl


def _entry(mode: int) -> list:
    # [mtime, size, mode] + every field TaikoDifficulty takes, as the cache stores them.
    return [0, 0, mode] + [""] * (len(sl._FIELD_ORDER) - 1) + [""] * len(sl._DETAIL_ORDER)


class SongsFromCacheTests(unittest.TestCase):
    def test_only_entries_under_the_root_and_only_taiko(self) -> None:
        root = Path("D:/osu!/Songs")
        cache = {
            str(root / "1 a" / "a [Oni].osu"): _entry(sl.TAIKO_MODE),
            str(root / "2 b" / "b [Hard].osu"): _entry(0),
            # A sibling whose name starts with the root's: not under it.
            str(Path("D:/osu!/Songs2/3 c/c [Oni].osu")): _entry(sl.TAIKO_MODE),
        }
        found = [d.path for d in sl.songs_from_cache(cache, root)]
        self.assertEqual(found, [root / "1 a" / "a [Oni].osu"])

    @unittest.skipUnless(os.name == "nt", "Windows paths compare without case")
    def test_the_root_matches_whatever_its_case(self) -> None:
        cache = {str(Path("D:/osu!/Songs/1 a/a [Oni].osu")): _entry(sl.TAIKO_MODE)}
        self.assertEqual(len(list(sl.songs_from_cache(cache, Path("d:/OSU!/songs")))), 1)


if __name__ == "__main__":
    unittest.main()
