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


def _string_end(data: bytes, at: int) -> tuple[int, int]:
    """(start, end) of the string at `at`; start == end for an absent one.

    0x0B, a ULEB128 length, then that many bytes; 0x00 alone is "no string".
    The one-byte length is the common case and takes no loop.
    """
    if data[at] != 0x0B:
        return at + 1, at + 1
    at += 1
    length = data[at]
    at += 1
    if length & 0x80:
        length &= 0x7F
        shift = 7
        while True:
            byte = data[at]
            at += 1
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
    return at, at + length


def read(path: Path) -> dict[tuple[str, str], DbBeatmap]:
    """{(folder name, .osu file name): DbBeatmap}, lower-cased keys.

    Folder + file name rather than full path: osu!.db stores both relative to
    the Songs folder, and the library may name that folder differently.
    Raises OSError / struct.error / IndexError on an unreadable or truncated
    file; the caller treats that as "no ratings", never as a crash.

    One loop over locals rather than a reader object with a method per field:
    this runs on the GUI thread at every launch, and a 29MB osu!.db (24,348
    beatmaps) was 230ms through method calls, about a million of them.
    """
    data = Path(path).read_bytes()
    unpack = struct.unpack_from
    version = unpack("<i", data, 0)[0]
    at = 4 + 4 + 1 + 8          # version, folder count, account unlocked, unlock date
    at = _string_end(data, at)[1]  # player name
    count = unpack("<i", data, at)[0]
    at += 4
    pair_size = 10 if version >= FLOAT_STARS_VERSION else 14
    has_entry_size = version < NO_ENTRY_SIZE_VERSION
    single = version >= SINGLE_DIFFICULTY_VERSION
    difficulty_size = 16 if single else 4
    beatmaps: dict[tuple[str, str], DbBeatmap] = {}
    for _ in range(count):
        if has_entry_size:
            at += 4
        for _field in range(7):  # artist, artist unicode, title, title unicode, creator, version, audio
            at = _string_end(data, at)[1]
        start, at = _string_end(data, at)
        md5 = data[start:at].decode("utf-8", errors="replace")
        start, at = _string_end(data, at)
        osu_file = data[start:at].decode("utf-8", errors="replace")
        at += 1 + 2 + 2 + 2 + 8  # ranked status, circles, sliders, spinners, modified
        at += difficulty_size + 8  # AR CS HP OD, slider velocity
        taiko_stars = None
        if single:
            for mode in range(4):
                pairs = unpack("<i", data, at)[0]
                at += 4
                if mode == TAIKO:
                    for _pair in range(pairs):
                        # 0x08, int mods, 0x0c/0x0d, float/double stars
                        if unpack("<i", data, at + 1)[0] == 0:
                            taiko_stars = unpack("<f" if pair_size == 10 else "<d", data, at + 6)[0]
                        at += pair_size
                else:
                    at += pairs * pair_size
        at += 4 + 4 + 4  # drain time, total time, preview time
        at += 4 + unpack("<i", data, at)[0] * 17  # timing points: double, double, bool
        at += 4 + 4 + 4 + 4 + 2 + 4 + 1  # ids, grades, local offset, stack leniency, mode
        at = _string_end(data, at)[1]  # source
        at = _string_end(data, at)[1]  # tags
        at += 2  # online offset
        at = _string_end(data, at)[1]  # title font
        at += 1 + 8 + 1  # unplayed, last played, osz2
        start, at = _string_end(data, at)
        folder = data[start:at].decode("utf-8", errors="replace")
        at += 8 + 5  # last checked, five option flags
        if not single:
            at += 2
        at += 4 + 1  # last modification, mania scroll speed
        beatmaps[(folder.lower(), osu_file.lower())] = DbBeatmap(md5, taiko_stars)
    return beatmaps


def file_md5(path: Path) -> str | None:
    """The hash osu!.db keys a difficulty's rating to; None if unreadable."""
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
