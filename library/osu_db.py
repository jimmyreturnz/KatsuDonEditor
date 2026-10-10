"""Star ratings from osu!stable's own `osu!.db`.

osu! computes a star rating for every beatmap in the Songs folder, uploaded or
not, and stores it here -- so reading it gives the song list osu!'s own number
with no calculator of our own and nothing online. osu!stable only: osu!lazer
keeps its library in a Realm database this does not read.

Format: https://github.com/ppy/osu/wiki/Legacy-database-file-structure. Only
what the song list shows is kept; every other field is skipped by its size.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

# The star-rating pairs changed from (int, double) to (int, float) here.
FLOAT_STARS_VERSION = 20250107
# Before this, every beatmap entry carried its own byte size first.
NO_ENTRY_SIZE_VERSION = 20191106
# Difficulty values became singles (they were bytes) and star ratings appeared.
SINGLE_DIFFICULTY_VERSION = 20140609
TAIKO = 1


@dataclass(frozen=True)
class DbBeatmap:
    md5: str
    taiko_stars: float | None


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.at = 0

    def skip(self, count: int) -> None:
        self.at += count

    def byte(self) -> int:
        value = self.data[self.at]
        self.at += 1
        return value

    def short(self) -> int:
        value = struct.unpack_from("<h", self.data, self.at)[0]
        self.at += 2
        return value

    def int(self) -> int:
        value = struct.unpack_from("<i", self.data, self.at)[0]
        self.at += 4
        return value

    def uleb(self) -> int:
        result = shift = 0
        while True:
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    def string(self) -> str:
        if self.byte() != 0x0B:
            return ""
        length = self.uleb()
        value = self.data[self.at:self.at + length].decode("utf-8", errors="replace")
        self.at += length
        return value

    def skip_string(self) -> None:
        if self.byte() == 0x0B:
            # Not `self.at += self.uleb()`: that reads self.at *before* uleb()
            # advances it past the length bytes, and lands short every time.
            length = self.uleb()
            self.at += length


def read(path: Path) -> dict[tuple[str, str], DbBeatmap]:
    """{(folder name, .osu file name): DbBeatmap}, lower-cased keys.

    Folder + file name rather than full path: osu!.db stores both relative to
    the Songs folder, and the library may name that folder differently.
    Raises OSError / struct.error on an unreadable or truncated file; the
    caller treats that as "no ratings", never as a crash.
    """
    reader = _Reader(Path(path).read_bytes())
    version = reader.int()
    reader.int()          # folder count
    reader.skip(1 + 8)    # account unlocked, unlock date
    reader.skip_string()  # player name
    count = reader.int()
    pair_size = 10 if version >= FLOAT_STARS_VERSION else 14
    beatmaps: dict[tuple[str, str], DbBeatmap] = {}
    for _ in range(count):
        if version < NO_ENTRY_SIZE_VERSION:
            reader.skip(4)
        for _field in range(7):  # artist, artist unicode, title, title unicode, creator, version, audio
            reader.skip_string()
        md5 = reader.string()
        osu_file = reader.string()
        reader.skip(1 + 2 + 2 + 2 + 8)  # ranked status, circles, sliders, spinners, modified
        reader.skip(16 if version >= SINGLE_DIFFICULTY_VERSION else 4)  # AR CS HP OD
        reader.skip(8)  # slider velocity
        taiko_stars = None
        if version >= SINGLE_DIFFICULTY_VERSION:
            for mode in range(4):
                pairs = reader.int()
                if mode == TAIKO:
                    for _pair in range(pairs):
                        # 0x08, int mods, 0x0c/0x0d, float/double stars
                        mods = struct.unpack_from("<i", reader.data, reader.at + 1)[0]
                        if mods == 0:
                            if pair_size == 10:
                                taiko_stars = struct.unpack_from("<f", reader.data, reader.at + 6)[0]
                            else:
                                taiko_stars = struct.unpack_from("<d", reader.data, reader.at + 6)[0]
                        reader.skip(pair_size)
                else:
                    reader.skip(pairs * pair_size)
        reader.skip(4 + 4 + 4)  # drain time, total time, preview time
        reader.skip(reader.int() * 17)  # timing points: double, double, bool
        reader.skip(4 + 4 + 4 + 4 + 2 + 4 + 1)  # ids, grades, local offset, stack leniency, mode
        reader.skip_string()  # source
        reader.skip_string()  # tags
        reader.skip(2)        # online offset
        reader.skip_string()  # title font
        reader.skip(1 + 8 + 1)  # unplayed, last played, osz2
        folder = reader.string()
        reader.skip(8 + 5)    # last checked, five option flags
        if version < SINGLE_DIFFICULTY_VERSION:
            reader.skip(2)
        reader.skip(4 + 1)    # last modification, mania scroll speed
        beatmaps[(folder.lower(), osu_file.lower())] = DbBeatmap(md5, taiko_stars)
    return beatmaps


def file_md5(path: Path) -> str | None:
    """The hash osu!.db keys a difficulty's rating to; None if unreadable."""
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
