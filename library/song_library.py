"""Index every .osu under an osu! Songs folder, keeping only taiko charts.

No Qt import on purpose (same rule as `osu_io` / `model`): the scan is a plain
generator so the caller decides how much of it to run per UI frame, and the
cache is plain JSON so it can be inspected and deleted by hand.

`parse_osu` is deliberately *not* reused here. It builds every hit object of
every file; a real Songs folder is tens of thousands of files. Non-taiko files
are read only as far as their header. A taiko file is read to the end, but only
counted: the song list shows its background, BPM, note count and length.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# 3: taiko entries carry background, BPM range, note count and length.
# 4: and the audio file and PreviewTime, for the song select's preview. An
# older cache is dropped, so the first scan after upgrading reads every chart
# again.
CACHE_VERSION = 4
TAIKO_MODE = "1"

# Header keys we index. Everything else in [General]/[Metadata] is ignored.
# The Unicode pair is what osu! stores the song's own-language metadata in;
# the plain pair is the romanized one.
_WANTED = {
    "Mode": "mode",
    "Artist": "artist",
    "ArtistUnicode": "artist_unicode",
    "Title": "title",
    "TitleUnicode": "title_unicode",
    "Version": "version",
    "Creator": "creator",
    "Tags": "tags",
    "AudioFilename": "audio",
    "PreviewTime": "preview",
}
# Cache entry layout after [mtime, size]: keeps the JSON compact and maps
# straight onto TaikoDifficulty's fields (mode excepted, which only decides
# whether an entry becomes one).
_FIELD_ORDER = ("mode", "artist", "artist_unicode", "title", "title_unicode", "version", "creator", "tags")
# What the song list's detail pane shows, read from the rest of a taiko file.
_DETAIL_ORDER = ("background", "bpm_min", "bpm_max", "notes", "length_ms", "audio", "preview")
# First section that can never hold one of the above; the file is huge past it.
_STOP_SECTIONS = {"[Events]", "[TimingPoints]", "[HitObjects]", "[Colours]", "[Difficulty]"}


@dataclass(frozen=True, slots=True)
class TaikoDifficulty:
    path: Path
    artist: str
    artist_unicode: str
    title: str
    title_unicode: str
    version: str
    creator: str
    tags: str
    # Defaults, so an entry from a header-only source (and every existing
    # caller) still builds; the song list shows a dash for what is unknown.
    background: str = ""
    bpm_min: float = 0.0
    bpm_max: float = 0.0
    notes: int = 0
    length_ms: int = 0
    # The song select's preview: the audio file and osu!'s PreviewTime in ms,
    # as written ("-1", or empty from an older entry, means none was set).
    audio: str = ""
    preview: str = ""

    @property
    def folder(self) -> Path:
        return self.path.parent

    def display_artist(self, original: bool = False) -> str:
        """Romanized by default; the song's own script when asked for it.

        Either field can be empty in a real map, so each falls back to the
        other rather than showing a blank where a name belongs.
        """
        pair = (self.artist_unicode, self.artist) if original else (self.artist, self.artist_unicode)
        return next((value for value in pair if value), "")

    def display_title(self, original: bool = False) -> str:
        pair = (self.title_unicode, self.title) if original else (self.title, self.title_unicode)
        return next((value for value in pair if value), self.folder.name)

    def song_label(self, original: bool = False) -> str:
        artist = self.display_artist(original)
        title = self.display_title(original)
        return f"{artist} - {title}" if artist else title

    def search_text(self) -> str:
        """Everything the library's search box matches against, lowercased."""
        return " ".join((
            self.artist, self.artist_unicode, self.title, self.title_unicode,
            self.version, self.creator, self.tags,
        )).lower()


def matches_search(difficulties, query: str) -> bool:
    """Whether one of `difficulties` carries every keyword in `query`.

    Each whitespace-separated word is its own keyword and they are matched
    against the pooled `search_text`, so "jimmyre dea vio" finds a mapper, a
    title and a difficulty name at once without the user having to know which
    field each word came from -- and without typing any of them in full. Taken
    as one literal needle it matched nothing: those three words never appear
    in that order in any field.

    All of them against the *same* difficulty, not the mapset as a whole. The
    version is the one field that differs between a song's difficulties, so
    spreading the words across them would make "oni futsuu" match any mapset
    that merely has both.
    """
    words = query.lower().split()
    return not words or any(
        all(word in text for word in words)
        for text in (difficulty.search_text() for difficulty in difficulties)
    )


def read_header(path: Path) -> dict[str, str] | None:
    """Return the indexed header fields, or None when the file cannot be read.

    Decoding is lenient (`errors="replace"`): these strings are only ever
    displayed in the browser, and the real load path re-reads the file through
    `parse_osu`, which does proper cp1252 fallback.

    A taiko file is read on past the header for `_DETAIL_ORDER`; any other
    mode stops at the first section that cannot hold a header field.
    """
    fields: dict = dict.fromkeys(_FIELD_ORDER, "")
    fields["mode"] = "0"
    fields.update(background="", bpm_min=0.0, bpm_max=0.0, notes=0, length_ms=0, audio="", preview="")
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            section = ""
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    if stripped in _STOP_SECTIONS and fields["mode"] != TAIKO_MODE:
                        break
                    if stripped == "[HitObjects]":
                        # The last section, and nearly all of the file: taken
                        # in one read and only its ends parsed. Line by line it
                        # was 14.5M calls and most of a 16s warm scan.
                        _read_hit_objects(fields, handle.read())
                        break
                    section = stripped
                    continue
                if section in ("", "[General]", "[Metadata]"):
                    key, separator, value = stripped.partition(":")
                    if separator and key in _WANTED:
                        fields[_WANTED[key]] = value.strip()
                elif section == "[Events]":
                    _read_event(fields, stripped)
                elif section == "[TimingPoints]":
                    _read_timing_point(fields, stripped)
    except OSError:
        return None
    first = fields.pop("_first_ms", None)
    last = fields.pop("_last_ms", None)
    if first is not None and last is not None:
        fields["length_ms"] = max(0, last - first)
    return fields


def _read_event(fields: dict, line: str) -> None:
    """The background: `0,0,"file.jpg",x,y` -- the first image event."""
    if fields["background"] or not line.startswith("0,"):
        return
    parts = line.split(",")
    if len(parts) >= 3 and parts[1].strip() == "0":
        fields["background"] = parts[2].strip().strip('"')


def _read_timing_point(fields: dict, line: str) -> None:
    """BPM from red lines only: `time,beatLength,...,uninherited` with a
    positive beat length. Absurd gimmick BPMs are real, and shown as such."""
    parts = line.split(",")
    if len(parts) < 2:
        return
    try:
        beat_length = float(parts[1])
    except ValueError:
        return
    uninherited = len(parts) < 7 or parts[6].strip() != "0"
    if not uninherited or beat_length <= 0:
        return
    bpm = 60000.0 / beat_length
    fields["bpm_min"] = bpm if not fields["bpm_min"] else min(fields["bpm_min"], bpm)
    fields["bpm_max"] = max(fields["bpm_max"], bpm)


def heard_tempo(path: Path) -> list[tuple[float, float]]:
    """The red lines a listener hears as tempo: (time, beat length), in order.

    For the song select's beat line, read from the one difficulty selected --
    difficulties can carry different audio and different timing. A red line
    that does not last one whole beat of its own before the next is left out:
    that is a gimmick (a 60000 BPM line hiding a note, an anti-barline wall
    tick), not a tempo, and pulsing to it would be a strobe. [] when the file
    cannot be read or has no red line.
    """
    lines: list[tuple[float, float]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            section = ""
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    if section == "[TimingPoints]":
                        break
                    section = stripped
                    continue
                if section != "[TimingPoints]":
                    continue
                parts = stripped.split(",")
                if len(parts) < 2:
                    continue
                try:
                    time, beat_length = float(parts[0]), float(parts[1])
                except ValueError:
                    continue
                uninherited = len(parts) < 7 or parts[6].strip() != "0"
                if uninherited and beat_length > 0:
                    lines.append((time, beat_length))
    except OSError:
        return []
    lines.sort()
    return [
        (time, beat) for index, (time, beat) in enumerate(lines)
        if index == len(lines) - 1 or lines[index + 1][0] - time >= beat
    ]


def most_common_beat_length(lines, last_time: float) -> float | None:
    """The beat length of the red line in force for the most time up to
    `last_time` (the last object): osu!lazer's
    `BeatmapExtensions.GetMostCommonBeatLength`, which its song select shows as
    "BPM a-b (mostly c)". aleph-0 switches between 250, 400 and 140, and is
    "mostly 250". `lines` are (time, beat length) of red lines, in order.

    As lazer counts it: the first line from 0, each until the next, the last
    until `last_time`, and a line after the last object counts for nothing.
    Beat lengths are compared to a thousandth so float noise does not split
    one tempo in two.
    """
    lines = [(time, beat) for time, beat in lines if beat > 0]
    if not lines:
        return None
    spans: dict[float, float] = {}
    for index, (time, beat) in enumerate(lines):
        if time > last_time:
            continue
        start = 0.0 if index == 0 else time
        end = last_time if index == len(lines) - 1 else min(lines[index + 1][0], last_time)
        key = round(beat, 3)
        spans[key] = spans.get(key, 0.0) + max(0.0, end - start)
    if not spans:
        return lines[0][1]
    return max(spans.items(), key=lambda item: item[1])[0]


def most_common_bpm(path: Path) -> float | None:
    """`most_common_beat_length` of a file, as a BPM: the "(250)" of a
    difficulty row's "125–400 (250)". Every red line counts here, gimmick or
    not, as it does for lazer; None when the file cannot be read."""
    lines: list[tuple[float, float]] = []
    last_time = 0.0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            section = ""
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    section = stripped
                    if section == "[HitObjects]":
                        # Only its last object matters: one read, not a loop.
                        objects = [row for row in handle.read().splitlines() if row.strip()]
                        objects = objects[:next((i for i, row in enumerate(objects) if row.startswith("[")),
                                                len(objects))]
                        if objects:
                            try:
                                last_time = float(objects[-1].split(",", 3)[2])
                            except (IndexError, ValueError):
                                pass
                        break
                    continue
                if section != "[TimingPoints]":
                    continue
                parts = stripped.split(",")
                if len(parts) < 2:
                    continue
                try:
                    time, beat_length = float(parts[0]), float(parts[1])
                except ValueError:
                    continue
                if len(parts) < 7 or parts[6].strip() != "0":
                    lines.append((time, beat_length))
    except OSError:
        return None
    lines.sort()
    beat = most_common_beat_length(lines, last_time)
    return 60000.0 / beat if beat else None


def _read_hit_objects(fields: dict, text: str) -> None:
    lines = [line for line in text.splitlines() if line.strip()]
    lines = lines[:next((i for i, line in enumerate(lines) if line.startswith("[")), len(lines))]
    times = []
    for line in (lines[:1] + lines[-1:]) if lines else ():
        parts = line.split(",", 3)
        try:
            times.append(int(float(parts[2])))
        except (IndexError, ValueError):
            pass
    fields["notes"] = len(lines)
    if len(times) == 2:
        fields["_first_ms"], fields["_last_ms"] = times


def scan(root: Path, cache: dict[str, list]) -> Iterator[TaikoDifficulty | None]:
    """Walk `root`, yielding one item per .osu file: the taiko ones, else None.

    A generator, so the caller can run it a few milliseconds at a time and keep
    the window responsive. `cache` is mutated in place (mtime + size keyed) and
    belongs to the caller to persist; entries for vanished files are dropped
    once the walk finishes.
    """
    root = Path(root)
    seen: set[str] = set()
    for path in root.rglob("*.osu"):
        key = str(path)
        seen.add(key)
        try:
            stat = path.stat()
        except OSError:
            continue
        entry = cache.get(key)
        if not (entry and entry[0] == stat.st_mtime_ns and entry[1] == stat.st_size):
            header = read_header(path)
            if header is None:
                continue
            entry = ([stat.st_mtime_ns, stat.st_size]
                     + [header[name] for name in _FIELD_ORDER]
                     + [header[name] for name in _DETAIL_ORDER])
            cache[key] = entry
        # entry[2] is the mode; everything after it lines up with
        # TaikoDifficulty's fields: _FIELD_ORDER, then _DETAIL_ORDER.
        yield TaikoDifficulty(path, *entry[3:]) if entry[2] == TAIKO_MODE else None
    for stale in set(cache) - seen:
        del cache[stale]


def songs_from_cache(cache: dict[str, list], root: Path) -> Iterator[TaikoDifficulty]:
    """Rebuild a previous scan's taiko entries without touching the disk.

    What makes the second start instant: the list is on screen before the
    verification walk begins. Entries are filtered by `root` because the cache
    outlives a change of songs folder.
    """
    root = Path(root)
    for key, entry in cache.items():
        if entry[2] != TAIKO_MODE:
            continue
        path = Path(key)
        if root in path.parents:
            yield TaikoDifficulty(path, *entry[3:])


def load_cache(path: Path) -> dict[str, list]:
    """Read the on-disk index, or start empty when it is missing or unusable."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if payload.get("version") != CACHE_VERSION:
        return {}
    return payload.get("files", {})


def save_cache(path: Path, cache: dict[str, list]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": CACHE_VERSION, "files": cache}),
        encoding="utf-8",
    )


def group_by_song(difficulties) -> dict[Path, list[TaikoDifficulty]]:
    """Group difficulties by their folder -- one folder is one song."""
    songs: dict[Path, list[TaikoDifficulty]] = {}
    for difficulty in difficulties:
        songs.setdefault(difficulty.folder, []).append(difficulty)
    return songs
