"""A difficulty at another rate, as its own .osu (owner, 2026-10-23): osu-trainer's
rate changer, without the two things it gets wrong for this editor's maps.

osu-trainer's `SetRate` is `int(t / rate)` on every time independently. That
truncates (-0.5ms on average against where DT would put the object), and it
lands `t` and `t + 1` on the same millisecond for about `1 - 1/rate` of all
pairs -- 17% at 1.2x. Every structure here that works by a 1ms hole (barline
notes, hidden anti-barline, fake sliders) is such a pair, and collapsed it is a
second uninherited point on one millisecond, where only the first ever counts.

So the times go through one map (`time_map`): nearest, not truncated, and a
millisecond later than nearest where that keeps two distinct times apart -- a
1ms gap stays 1ms rather than becoming 0.83. Never more than that one: a run of
points on every millisecond (`Nbt-Hwt`'s walls) cannot fit into fewer
milliseconds without some sharing, and "never close a gap" pushed such a run
109ms late at 1.5x, measured. Times that were equal stay equal, so a note and
its own red line still share a millisecond.

Green lines are untouched. Taiko scrolls at BPM x SV, every red line's BPM is
multiplied by the rate, and that is exactly what DT does to it, so every SV,
gimmick speed and drumroll length comes out as DT would show it.
"""
from __future__ import annotations

from copy import copy
from dataclasses import replace
from pathlib import Path

from chart.taiko_to_mania import chart_beat_length
from chart.time_axis import osu_round
from osu_io import metadata
from osu_io.writer import write_osu

# Any rate with a BPM above zero, up to 1000 BPM on the chart's main line
# (owner, 2026-10-23): a 727 BPM song mapped in 1/2 plays at ~360, so the
# ceiling is a BPM, not a rate.
MIN_RATE = 0.01
MAX_BPM = 1000.0
# Every rate is rounded to this when it is chosen, so it can key its audio
# file exactly: 320/269 has no end, and 4 places is 0.03 BPM at 269.
RATE_DECIMALS = 4
# Today's name, the default of the user's own template (owner, 2026-10-23).
DEFAULT_NAME_TEMPLATE = "{name} {rate}x ({bpm}bpm)"


def rate_text(rate: float) -> str:
    """A rate as a name shows it: 320/269 reads 1.19, not 1.1896. The
    audio file's name keeps every place (`audio_name`), because two rates a
    name cannot tell apart must not share a song."""
    return f"{round(rate, 3):g}"


def time_map(times, rate: float) -> dict[float, int]:
    """{source time: new millisecond} for every time in `times`: nearest, or
    one later to stay off the previous time's millisecond, never further. See
    the module docstring."""
    mapped: dict[float, int] = {}
    previous = None
    for time in sorted(set(times)):
        new = osu_round(time / rate)
        if previous is not None and new <= previous:
            # Off nearest by at most 1, and never before the previous time.
            new = previous + 1 if new == previous else previous
        mapped[time] = previous = new
    return mapped


def _scaled_field(value: str, scale) -> str:
    # Parsed, not pre-checked: isdigit() passes "²" and "--5", which int()
    # rejects. A value that is not a whole number (a hand-edited file) stays.
    try:
        return str(scale(int(value)))
    except ValueError:
        return value


def mapped_lines(lines: list[str], scale, ending: str) -> list[str]:
    """`lines` with every passthrough time put through `scale`: [Editor]
    Bookmarks, [General] PreviewTime (-1 is "none" and stays), and [Events]
    breaks and the video's start. Storyboard commands are left alone.
    ponytail: add them when a rate-changed map with a storyboard needs it."""
    bookmarks = metadata.read_keys(lines, "Editor").get("Bookmarks", "")
    if bookmarks:
        values = [value.strip() for value in bookmarks.split(",") if value.strip()]
        lines = metadata.with_keys(lines, "Editor", {"Bookmarks": ",".join(
            _scaled_field(value, scale) for value in values)}, ending)
    preview = metadata.read_keys(lines, "General").get("PreviewTime", "")
    try:
        if int(preview) >= 0:
            lines = metadata.with_keys(lines, "General", {"PreviewTime": str(scale(int(preview)))}, ending)
    except ValueError:
        pass
    out, section = [], None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        elif section == "Events" and not line.startswith((" ", "_", "//")):
            fields = stripped.split(",")
            if fields[0] in ("2", "Break") and len(fields) >= 3:
                fields[1:3] = (_scaled_field(fields[1], scale), _scaled_field(fields[2], scale))
            elif fields[0] in ("1", "Video") and len(fields) >= 2:
                fields[1] = _scaled_field(fields[1], scale)
            else:
                out.append(line)
                continue
            line = ",".join(fields) + line[len(line.rstrip("\r\n")):]
        out.append(line)
    return out


def _end_field(field: str, new_end: int) -> str:
    return ":".join((str(new_end), *field.split(":")[1:]))


def difficulty_setting(lines: list[str], key: str, default: float = 5.0) -> float:
    try:
        return float(metadata.read_keys(lines, "Difficulty").get(key, default))
    except ValueError:
        return default


def scaled_document(document, rate: float, od: float | None = None, hp: float | None = None):
    """A copy of `document` at `rate`, ready for `write`. `od` and `hp`
    replace the chart's own."""
    times = [note.time for note in document.hit_objects]
    times += [end for note in document.hit_objects if (end := note.end_time) is not None]
    times += [point.time for point in document.timing_points]
    mapped = time_map(times, rate)

    def scale(time: float) -> int:
        return mapped[time] if time in mapped else osu_round(time / rate)

    notes = []
    for note in document.hit_objects:
        end = note.end_time
        note = copy(note)
        note.time = mapped[note.time]
        if end is not None:
            if note.extras:
                note.extras = (_end_field(note.extras[0], mapped[end]), *note.extras[1:])
            else:  # a mania hold keeps its end in the hit sample
                note.hit_sample = _end_field(note.hit_sample, mapped[end])
        notes.append(note)
    points = []
    for point in document.timing_points:
        point = copy(point)
        if point.uninherited:
            point.beat_length /= rate
        point.time = mapped[point.time]
        points.append(point)
    lines = mapped_lines(document.lines, scale, document.line_ending)
    values = {}
    if od is not None:
        values["OverallDifficulty"] = f"{od:g}"
    if hp is not None:
        values["HPDrainRate"] = f"{hp:g}"
    if values:
        lines = metadata.with_keys(lines, "Difficulty", values, document.line_ending)
    return replace(document, lines=lines, hit_objects=notes, timing_points=points)


def dt_od(od: float, rate: float) -> float:
    """The OD whose great window, at 1x, is what `od`'s is under a `rate`
    clock: taiko's great is 50 - 3 x OD ms, and a rate mod divides it by the
    rate in real time. Clamped to [0, 10], so a high rate falls short."""
    return round(min(10.0, max(0.0, (50 - (50 - 3 * od) / rate) / 3)), 1)


def audio_name(audio_filename: str, rate: float, pitch: bool, suffix: str) -> str:
    stem = Path(audio_filename).stem
    return f"{stem} {round(rate, RATE_DECIMALS):g}x{' pitched' if pitch else ''}{suffix}"


def version_name(document, rate: float, template: str = DEFAULT_NAME_TEMPLATE) -> str:
    """`template` with {name}, {rate} and {bpm} filled in. Replaced rather
    than str.format, so a stray brace typed into the box is just a brace."""
    version = metadata.read_keys(document.lines, "Metadata").get("Version", document.version)
    bpm = 60000.0 / chart_beat_length(document) * rate
    # Pitch is not in the name (owner, 2026-10-23); the audio file keeps it,
    # so both versions of one rate can sit in the folder and be reused.
    return (template.replace("{name}", version).replace("{rate}", rate_text(rate))
            .replace("{bpm}", f"{bpm:.0f}").strip())


def destination(document, rate: float, version: str | None = None) -> Path:
    """Where `write` puts this rate: checked before the audio is rendered, so
    a conversion that would be refused is refused before a minute of work."""
    values = metadata.read_keys(document.lines, "Metadata")
    return document.source_path.parent / metadata.osu_filename(
        values.get("Artist", ""), values.get("Title", ""), values.get("Creator", ""),
        version or version_name(document, rate))


def write(document, rate: float, pitch: bool, audio_file: str,
          od: float | None = None, hp: float | None = None, version: str | None = None) -> Path:
    """Write `document` at `rate` beside it, playing `audio_file` (a name in
    the same folder), and return the new .osu's path."""
    scaled = scaled_document(document, rate, od, hp)
    ending = document.line_ending
    lines = metadata.with_keys(scaled.lines, "General", {"AudioFilename": f" {audio_file}"}, ending)
    lines = metadata.with_keys(lines, "Metadata", {"BeatmapID": "0"}, ending)
    # From the source, not the scaled lines: a file with no ApproachRate
    # takes its OD as AR, and that is the OD it had, not the one typed here.
    original = document.lines
    version = version or version_name(document, rate)
    return write_osu(replace(scaled, lines=lines), destination(document, rate, version), version,
                     force_ar=difficulty_setting(original, "ApproachRate",
                                                 difficulty_setting(original, "OverallDifficulty")),
                     force_cs=difficulty_setting(original, "CircleSize"))
