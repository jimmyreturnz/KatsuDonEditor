"""A mappack: difficulties from different beatmapsets gathered into one set
that can be submitted as the user's own (owner, 2026-10-21).

What a difficulty becomes in the pack:

* **BeatmapID:0, BeatmapSetID:-1.** Left at the source's ids, the submission
  system recognises the difficulty and answers "The beatmap you're trying to
  submit isn't yours!".
* Title and Creator are the user's, the same on every difficulty; Artist is
  Various Artists; Tags and Source stay the source's own, and every
  difficulty's tags gain each original mapper in the pack (once, and not
  where already tagged), so the set is found by searching any of them.
* Version defaults to "Artist - Title (Creator) [Version]" from the source's
  romanised fields, so a pack's difficulty list still says what each one was.
* One background for all by default. Every file is placed by content
  (`place_file`): two difficulties sharing an image or a song share one file.
* Video and storyboard lines go, since their files are not brought along.

**The size limit is per set, and set by its longest difficulty**: 5 MB plus
10 MB a minute, capped at 100 MB, and at most 128 difficulties. A pack of
short songs therefore gets a small budget, which is what the Delete difficulty
is for: two objects 15 minutes apart, silent audio, and the set's length is
15 minutes. The submission takes its length from the difficulty it is
submitted from, so it has to be submitted from the longest one.

**A pack is updated by rebuilding it** (`build_pack(..., replace=)`): its own
difficulties are loaded back as entries, new ones are added, and the whole set
is written beside the old one before the old one goes to the Recycle Bin. A
half-written pack therefore never replaces a whole one.
"""
from __future__ import annotations

import csv
import filecmp
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QFile

from osu_io import metadata
from osu_io.parser import parse_osu

VARIOUS_ARTISTS = "Various Artists"
DIFFICULTY_LIMIT = 128
DELETE_LENGTH_MS = 15 * 60 * 1000
DELETE_VERSION = "Delete"
DELETE_AUDIO = "delete.mp3"
MB = 1024 * 1024

# osu!'s own hitsound file names; a set's custom samples are named this way.
SAMPLE_NAME = re.compile(
    r"^(taiko-)?(normal|soft|drum)-(hit(normal|clap|whistle|finish)|slider(slide|tick|whistle))\d*"
    r"\.(wav|ogg|mp3)$", re.IGNORECASE)


def size_limit_bytes(length_ms: float) -> int:
    """What osu! lets a set weigh, from its longest difficulty's length."""
    return int(min(100.0, 5.0 + 10.0 * max(0.0, length_ms) / 60000.0) * MB)


def default_version(values: dict[str, str]) -> str:
    return (f"{values.get('Artist', '')} - {values.get('Title', '')} "
            f"({values.get('Creator', '')}) [{values.get('Version', '')}]")


def safe_name(text: str) -> str:
    # Windows refuses these in a name, and a trailing dot or space.
    return "".join(c for c in text if c not in '\\/:*?"<>|' and c >= " ").strip().rstrip(".")


def resolve_ci(folder: Path, name: str) -> Path | None:
    """`folder / name`, matched without regard to case as osu! on Windows
    does -- many maps name their audio in a case the file does not have.

    None for a name that would leave `folder`: the name comes from a
    downloaded .osu, and "../../anything" would otherwise copy a file from
    outside the set into a pack.
    """
    relative = Path(name.strip().strip('"'))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return None
    root = Path(folder).resolve()
    direct = (root / relative).resolve()
    if not direct.is_relative_to(root):
        return None
    folder, name = direct.parent, direct.name
    if direct.is_file():
        return direct
    lowered = name.lower()
    try:
        with os.scandir(folder) as entries:
            return next((Path(e.path) for e in entries if e.is_file() and e.name.lower() == lowered), None)
    except OSError:
        return None


def place_file(source: Path, folder: Path) -> Path:
    """Put `source` in `folder` once: a byte-identical file already there
    (by its own name first, then any other) is reused, and a different file
    by the same name pushes the copy to `stem_2`, `stem_3`, ..."""
    source = Path(source)
    destination = folder / source.name
    if destination.exists() and filecmp.cmp(source, destination, shallow=False):
        return destination
    size = source.stat().st_size
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if (entry.is_file() and entry.stat().st_size == size
                        and filecmp.cmp(source, entry.path, shallow=False)):
                    return Path(entry.path)
    except OSError:
        pass
    number = 2
    while destination.exists():
        destination = folder / f"{source.stem}_{number}{source.suffix}"
        number += 1
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination


def _event_fields(content: str) -> list[str]:
    try:
        return next(csv.reader([content], skipinitialspace=True))
    except (csv.Error, StopIteration):
        return []


def background_name(lines: list[str]) -> str | None:
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        elif section == "Events" and stripped and not stripped.startswith("//"):
            fields = _event_fields(stripped)
            if len(fields) >= 3 and fields[0] == "0":
                return fields[2].strip('"')
    return None


def with_events(lines: list[str], background: str | None, ending: str) -> list[str]:
    """[Events] with the background set and videos and storyboard objects
    gone; breaks, comments and background colours are kept."""
    out: list[str] = []
    section = ""
    placed = False
    in_object = False  # inside a storyboard object's indented commands
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "Events" and not placed and background:
                out.append(f'0,0,"{background}",0,0{ending}')
                placed = True
            section = stripped[1:-1]
            out.append(line)
            continue
        if section != "Events" or not stripped or stripped.startswith("//"):
            out.append(line)
            continue
        if line[:1] in (" ", "_"):
            if not in_object:
                out.append(line)
            continue
        kind = _event_fields(stripped)[:1]
        kind = kind[0] if kind else ""
        in_object = kind in ("Sprite", "Animation", "4", "6")
        if kind == "0":
            if background and not placed:
                out.append(f'0,0,"{background}",0,0{ending}')
                placed = True
            elif not background:
                out.append(line)
        elif kind in ("1", "Video", "Sample", "5") or in_object:
            continue
        else:
            out.append(line)
    if not placed and background:
        out += [f"[Events]{ending}", f'0,0,"{background}",0,0{ending}', ending]
    return out


def silent_mp3(length_ms: int) -> bytes:
    """`length_ms` of silence as MPEG-2.5 Layer III, 8 kHz mono 8 kbps: 72-byte
    frames of 72ms each, every one a header and zeroed side info, which every
    decoder reads as silence. 15 minutes is 0.9 MB."""
    frame = bytes((0xFF, 0xE3, 0x18, 0xC0)) + bytes(68)
    return frame * (length_ms // 72 + 2)


@dataclass
class Entry:
    path: Path
    version: str
    background: Path | None = None  # None: the pack's, else the difficulty's own


@dataclass
class Report:
    folder: Path
    files: list[Path] = field(default_factory=list)
    clashes: list[str] = field(default_factory=list)
    longest: tuple[str, int] = ("", 0)
    total_bytes: int = 0

    @property
    def limit_bytes(self) -> int:
        return size_limit_bytes(self.longest[1])


def length_ms(document) -> int:
    """The last object's millisecond (a spinner's or hold's end, if later)."""
    return max((max(n.time, n.end_time or 0) for n in document.hit_objects), default=0)


def _samples_of(document) -> set[str]:
    names = set()
    try:
        with os.scandir(document.source_path.parent) as entries:
            names = {e.name for e in entries if e.is_file() and SAMPLE_NAME.match(e.name)}
    except OSError:
        pass
    for note in document.hit_objects:
        parts = note.hit_sample.split(":")
        if len(parts) >= 5 and parts[-1]:
            names.add(parts[-1])
    return names


def _write(path: Path, lines: list[str], encoding: str) -> None:
    payload = "".join(lines)
    try:
        data = payload.encode(encoding)
    except UnicodeEncodeError:
        data = payload.encode("utf-8")
    path.write_bytes(data)


def delete_difficulty_lines(title: str, creator: str, background: str | None, mode: str,
                            tags: str = "") -> list[str]:
    n = "\r\n"
    lines = [
        f"osu file format v14{n}", n, f"[General]{n}", f"AudioFilename: {DELETE_AUDIO}{n}",
        f"AudioLeadIn: 0{n}", f"PreviewTime: -1{n}", f"Mode: {mode}{n}", n,
        f"[Metadata]{n}", f"Title:{title}{n}", f"TitleUnicode:{title}{n}",
        f"Artist:{VARIOUS_ARTISTS}{n}", f"ArtistUnicode:{VARIOUS_ARTISTS}{n}", f"Creator:{creator}{n}",
        f"Version:{DELETE_VERSION}{n}", f"Source:{n}", f"Tags:{tags}{n}", f"BeatmapID:0{n}", f"BeatmapSetID:-1{n}", n,
        f"[Difficulty]{n}", f"HPDrainRate:5{n}", f"CircleSize:5{n}", f"OverallDifficulty:5{n}",
        f"ApproachRate:5{n}", f"SliderMultiplier:1.4{n}", f"SliderTickRate:1{n}", n,
        f"[Events]{n}",
    ]
    if background:
        lines.append(f'0,0,"{background}",0,0{n}')
    lines += [n, f"[TimingPoints]{n}", f"0,500,4,1,0,100,1,0{n}", n,
              f"[HitObjects]{n}", f"256,192,0,1,0,0:0:0:0:{n}", f"256,192,{DELETE_LENGTH_MS},1,0,0:0:0:0:{n}"]
    return lines


def merge_tags(tags: str, extra: str) -> str:
    """`tags` with each word of `extra` it lacks (ignoring case) appended."""
    words = tags.split()
    have = {word.lower() for word in words}
    for word in extra.split():
        if word.lower() not in have:
            have.add(word.lower())
            words.append(word)
    return " ".join(words)


def load_pack(folder: Path) -> tuple[dict[str, str], list[Path], Path | None]:
    """An existing pack's (Metadata of one difficulty, its difficulties, its
    Delete difficulty if any), for editing it."""
    paths = sorted(Path(folder).glob("*.osu"))
    if not paths:
        raise ValueError(f"{Path(folder).name} has no difficulties.")
    delete = None
    rest = []
    for path in paths:
        values = metadata.read_keys(parse_osu(path).lines, "Metadata")
        if values.get("Version") == DELETE_VERSION:
            delete = path
        else:
            rest.append(path)
    return metadata.read_keys(parse_osu((rest or paths)[0]).lines, "Metadata"), rest, delete


def build_pack(entries: list[Entry], title: str, creator: str, songs_folder: Path,
               background: Path | None = None, include_delete: bool = False,
               tag_creators: bool = True, delete_tags: str | None = None,
               replace: Path | None = None) -> Report:
    """Write the pack to `songs_folder / "Various Artists - <title>"`.
    `delete_tags` are the Delete difficulty's (the page's collected, edited
    tags of the whole pack); None gives it just the mappers. `replace` is a
    pack being updated: entries may be read from it, and it goes to the
    Recycle Bin once the new one is whole."""
    if not entries:
        raise ValueError("The pack has no difficulties.")
    if len(entries) + include_delete > DIFFICULTY_LIMIT:
        raise ValueError(f"A set can hold at most {DIFFICULTY_LIMIT} difficulties.")
    final = Path(songs_folder) / safe_name(f"{VARIOUS_ARTISTS} - {title}")
    replace = Path(replace).resolve() if replace else None
    if final.exists() and final.resolve() != replace:
        raise FileExistsError(f"{final.name} already exists in the Songs folder.")
    folder = final.with_name(final.name + " (updating)") if replace else final
    if folder.exists():
        shutil.rmtree(folder)  # our own leftover from an update that failed
    folder.mkdir(parents=True)
    try:
        report = _fill_pack(folder, entries, title, creator, background, include_delete,
                            tag_creators, delete_tags)
    except BaseException:
        if replace:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    if replace:
        # osu! holds the selected set's audio open, and then neither move can happen.
        if replace.exists() and not QFile.moveToTrash(str(replace)):
            shutil.rmtree(folder, ignore_errors=True)
            raise OSError(f"{replace.name} is in use: select another set in osu! and try again.")
        folder.rename(final)
        report.folder = final
        report.files = [final / p.name for p in report.files]
    return report


def _fill_pack(folder: Path, entries: list[Entry], title: str, creator: str,
               background: Path | None, include_delete: bool, tag_creators: bool,
               delete_tags: str | None) -> Report:
    report = Report(folder)
    shared_background = place_file(background, folder).name if background else None
    sample_sources: dict[str, Path] = {}
    documents = [parse_osu(entry.path) for entry in entries]
    seen: dict[str, str] = {}
    for document in documents if tag_creators else ():
        name = metadata.read_keys(document.lines, "Metadata").get("Creator", "").strip()
        # A difficulty loaded back from the pack already carries the pack's
        # creator, and its original mapper is already in its tags.
        if name and name.lower() != creator.lower():
            seen.setdefault(name.lower(), name)
    creators = list(seen.values())
    for entry, document in zip(entries, documents):
        source_folder = document.source_path.parent
        audio = resolve_ci(source_folder, document.audio_filename)
        if audio is None:
            raise FileNotFoundError(f"{entry.path.name}: its audio {document.audio_filename} is missing.")
        audio_name = place_file(audio, folder).name
        if entry.background is not None:
            image = place_file(entry.background, folder).name
        elif shared_background:
            image = shared_background
        else:
            own = background_name(document.lines)
            own_path = resolve_ci(source_folder, own) if own else None
            image = place_file(own_path, folder).name if own_path else None
        for name in _samples_of(document):
            source = resolve_ci(source_folder, name)
            if source is None:
                continue
            if name.lower() in sample_sources:
                if not filecmp.cmp(source, sample_sources[name.lower()], shallow=False):
                    report.clashes.append(f"{name} ({entry.path.name})")
                continue
            sample_sources[name.lower()] = source
            shutil.copy2(source, folder / source.name)
        ending = document.line_ending
        lines = metadata.with_keys(document.lines, "General", {"AudioFilename": f" {audio_name}"}, ending)
        tags = merge_tags(metadata.read_keys(lines, "Metadata").get("Tags", ""), " ".join(creators))
        lines = metadata.with_keys(lines, "Metadata", {
            "Tags": tags, "Title": title, "TitleUnicode": title, "Artist": VARIOUS_ARTISTS,
            "ArtistUnicode": VARIOUS_ARTISTS, "Creator": creator, "Version": entry.version,
            "BeatmapID": "0", "BeatmapSetID": "-1"}, ending)
        lines = with_events(lines, image, ending)
        destination = folder / metadata.osu_filename(VARIOUS_ARTISTS, title, creator, entry.version)
        if destination.exists():
            raise FileExistsError(f"Two difficulties are both named {entry.version}.")
        _write(destination, lines, document.encoding)
        written = parse_osu(destination)
        if len(written.hit_objects) != len(document.hit_objects):
            raise ValueError(f"{destination.name} did not write back whole.")
        report.files.append(destination)
        length = max((max(n.time, n.end_time or 0) for n in written.hit_objects), default=0)
        if length > report.longest[1]:
            report.longest = (entry.version, length)
    if include_delete:
        (folder / DELETE_AUDIO).write_bytes(silent_mp3(DELETE_LENGTH_MS))
        mode = metadata.read_keys(documents[0].lines, "General").get("Mode", "1")
        destination = folder / metadata.osu_filename(VARIOUS_ARTISTS, title, creator, DELETE_VERSION)
        tags = " ".join(creators) if delete_tags is None else merge_tags("", delete_tags)
        _write(destination, delete_difficulty_lines(title, creator, shared_background, mode, tags), "utf-8")
        report.files.append(destination)
        report.longest = (DELETE_VERSION, DELETE_LENGTH_MS)
    report.total_bytes = sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
    return report
