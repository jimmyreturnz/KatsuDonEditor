"""A taiko difficulty as an osu!mania chart, 4K or 1K, played the way the
autoplay plays it (owner, 2026-10-21).

**Columns are the autoplay's.** 4K asks `autoplay.note_halves` -- the same
call the preview's input drum makes -- so every playstyle and binding carries
over, and a column is the drum half lit (1 left kat .. 4 right kat). 1K puts
everything in one column, which is the whole of "no colouring".

**A drumroll and a spinner are one hold**, entered into the hand sequence as a
small don: it takes a turn like any note, and lands in that hand's don column.
A fake slider has no playable length and is dropped.

**Mania scrolls the whole chart, so the timing is rewritten.** In taiko an SV
moves the notes at its own millisecond; in mania it moves the playfield, and a
BPM change moves it too. Every green line goes -- except one that changes kiai
or volume, which keeps its millisecond -- and every red line gains a green line
on its own millisecond with SV = its beat length / the chart's: 400 BPM in a
200 BPM chart is 0.5x, so the scroll is the chart BPM's at 1.0x throughout.
"The chart's" is `most_common_beat_length`, song select's "(250)".

**Hitsounds ride on the notes as filenames** (`0:0:0:0:taiko-normal-hitclap
.wav`, the owner's reference), since mania has no don/kat to derive them from.
A finisher on two columns puts its small sound on one and its big sound on the
other, so the pair sounds like the taiko note; alone it keeps the small one,
since a finish sample by itself is a cymbal with no drum under it.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from chart import autoplay
from chart.gimmick_session import FAKE_SLIDER_MAX_LENGTH
from library.mappack import place_file
from library.song_library import most_common_beat_length
from model.hit_object import TYPE_CIRCLE, TYPE_HOLD, HitObject
from osu_io import metadata
from osu_io.timing import (
    EFFECT_KIAI, active_uninherited_at, duration_for_slider_length, sorted_by_time, sv_at,
)
from osu_io.writer import write_osu

STYLE_LABELS = {
    autoplay.FULL_ALT: "full alt", autoplay.SEMI_ALT: "semi alt",
    autoplay.SINGLE_TAP: "single tap", autoplay.ROLL: "roll", autoplay.DDKK: "ddkk",
}


def _hold_end(note, reds, points, slider_multiplier) -> int | None:
    """Where a drumroll or spinner ends, or None when it has no length to hold."""
    if note.is_spinner:
        end = note.end_time
    elif note.is_slider and note.length is not None and note.length > FAKE_SLIDER_MAX_LENGTH:
        duration = duration_for_slider_length(
            note.length * (note.slides or 1), active_uninherited_at(reds, note.time).beat_length,
            sv_at(points, note.time), slider_multiplier)
        end = int(note.time + duration)
    else:
        return None
    return end if end is not None and end > note.time else None


def _sample_volume(note) -> int:
    parts = note.hit_sample.split(":")
    try:
        return int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        return 0


def mania_objects(document, keys: int, style: str, binding: str, left_handed: bool,
                  beat_ms: float, finisher_single: bool, samples: dict[str, str]) -> list[HitObject]:
    points = sorted_by_time(document.timing_points)
    reds = [p for p in points if p.uninherited and p.beat_length > 0]
    entries = []  # (note, hold end or None)
    for note in sorted(document.hit_objects, key=lambda n: n.time):
        if note.is_circle:
            entries.append((note, None))
        elif reds:
            end = _hold_end(note, reds, points, document.slider_multiplier)
            if end is not None:
                entries.append((note, end))
    halves = autoplay.note_halves(
        [(n.time, end is None and n.is_kat, end is None and n.is_finisher and not finisher_single)
         for n, end in entries],
        beat_ms, style, binding, left_handed)
    objects: list[HitObject] = []
    busy = {}  # column -> last millisecond it is occupied through
    for (note, end), lit in zip(entries, halves):
        columns = [0] if keys == 1 else [half - 1 for half in lit]
        sounds = autoplay.hitsound_keys(note) or ("normal",)
        volume = _sample_volume(note)
        for index, column in enumerate(columns):
            # ponytail: a note landing on a column still held (or a second
            # object on the same millisecond, a shiny stack) is dropped rather
            # than re-routed to a free column.
            if note.time <= busy.get(column, -1):
                continue
            busy[column] = end if end is not None else note.time
            sample = samples[sounds[min(index, len(sounds) - 1)]]
            # No file: the note keeps its whistle/clap/finish for the
            # player's own skin to voice.
            bits = 0 if sample else note.hit_sound
            x = int((column + 0.5) * 512 / keys)
            if end is None:
                objects.append(HitObject(x, 192, note.time, TYPE_CIRCLE, bits, (), f"0:0:0:{volume}:{sample}"))
            else:
                objects.append(HitObject(x, 192, note.time, TYPE_HOLD, bits, (), f"{end}:0:0:0:{volume}:{sample}"))
    return objects


def mania_timing(points, chart_beat: float) -> list:
    """Every red line kept, each followed by a green at the SV that scrolls it
    at `chart_beat`; green lines only where kiai or volume changes."""
    out = []
    red = None
    state = None  # (kiai, volume) in force
    for point in sorted_by_time(points):
        if point.uninherited and point.beat_length > 0:
            red = point
            out.append(point)
        elif red is None or state == (point.kiai, point.volume):
            continue
        green = point.copy()
        green.uninherited_flag = 0
        green.effects = point.effects & EFFECT_KIAI
        green.set_sv(red.beat_length / chart_beat)
        if not out[-1].uninherited and out[-1].time == green.time:
            out.pop()  # a red's own green, overridden by the map's on that millisecond
        out.append(green)
        state = (point.kiai, point.volume)
    return out


def place_samples(folder: Path, sources: dict[str, Path], from_skin: bool = True) -> dict[str, str]:
    """The file each hitsound key names in `folder`; "" for none.

    `from_skin`: the app's own sound -- the current skin's, else the built-in
    -- is placed in the folder and used, by `place_file`, so a set's own
    different file by that name stays for its taiko difficulties and the
    skin's arrives as `_2`. Off, a sample the set already ships (any of
    osu!'s three extensions) is used, and nothing is copied.
    """
    try:
        with os.scandir(folder) as entries:
            present = {entry.name.lower(): entry.name for entry in entries if entry.is_file()}
    except OSError:
        present = {}
    names = {}
    for key, default in autoplay.HITSOUND_SAMPLES.items():
        stem = default.rsplit(".", 1)[0]
        found = next((present[stem + ext] for ext in (".wav", ".ogg", ".mp3") if stem + ext in present), None)
        source = sources.get(key)
        if from_skin and source is not None and Path(source).is_file():
            found = place_file(Path(source), Path(folder)).name
        names[key] = found or ""
    return names


def chart_beat_length(document) -> float:
    """The chart BPM's beat, exactly. `most_common_beat_length` compares to a
    thousandth and returns that rounded key, which would put every section
    at the chart's own tempo at 0.999998x rather than 1.0x."""
    last = max((n.time for n in document.hit_objects), default=0)
    lines = [(p.time, p.beat_length) for p in sorted_by_time(document.timing_points)
             if p.uninherited and p.beat_length > 0]
    common = most_common_beat_length(lines, last)
    if common is None:
        return 500.0
    return next((beat for _time, beat in lines if round(beat, 3) == common), common)


def mania_version(version: str, keys: int, style: str) -> str:
    return f"{version} ({keys}K {STYLE_LABELS[style]})" if keys > 1 else f"{version} (1K)"


def convert(document, keys: int = 4, style: str = autoplay.FULL_ALT, binding: str = "1234",
            left_handed: bool = False, single_tap_bpm: float | None = None,
            finisher_single: bool = False, samples: dict[str, str] | None = None) -> Path:
    """Write the mania chart beside `document`'s file and return its path.
    `single_tap_bpm` is the beat the autoplay groups against; None is the
    chart's own BPM. `samples` names each hitsound key's file in the folder
    (`place_samples`)."""
    chart_beat = chart_beat_length(document)
    beat_ms = 60000.0 / single_tap_bpm if single_tap_bpm else chart_beat
    lines = metadata.with_keys(document.lines, "General", {"Mode": "3"}, document.line_ending)
    lines = metadata.with_keys(lines, "Metadata", {"BeatmapID": "0"}, document.line_ending)
    values = metadata.read_keys(lines, "Metadata")
    version = mania_version(values.get("Version", document.version), keys, style)
    try:
        approach = float(metadata.read_keys(lines, "Difficulty").get("ApproachRate", 5))
    except ValueError:
        approach = 5.0
    converted = replace(
        document, lines=lines,
        hit_objects=mania_objects(document, keys, style, binding, left_handed, beat_ms, finisher_single,
                                  samples or autoplay.HITSOUND_SAMPLES),
        timing_points=mania_timing(document.timing_points, chart_beat))
    name = metadata.osu_filename(values.get("Artist", ""), values.get("Title", ""),
                                 values.get("Creator", ""), version)
    return write_osu(converted, document.source_path.parent / name, version,
                     force_ar=approach, force_cs=keys)
