"""Structured model for the [TimingPoints] section.

Before this module the section existed only as raw text, re-scanned by five
independent ad-hoc parsers that each kept a different subset of the fields.
Inherited points, which carry all slider-velocity information, were discarded by
most of them.

Design notes:

* `TimingPoint` is mutable. SV editing adjusts points in place, and freezing the
  dataclass would force a rebuild for every drag of a green line.
* The raw `uninherited_flag` int is stored rather than a derived boolean, so a
  round trip reproduces the authored field exactly and the serialized value can
  never drift from the property used for logic.
* Every field has a default. osu file format v4 and earlier write only
  `offset,ms_per_beat`; requiring seven fields is what made those maps parse to
  zero timing points and silently fall back to a fabricated 120 BPM.

This module deliberately imports nothing from `osu_io.parser`, so `transformer`
can consume timing points without pulling in the parser.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import count
from typing import Iterable, Iterator, Sequence

# Bit 0 of the effects field. Bit 3 (value 8) omits the first barline, which
# barline gimmicks set in bulk, so masking here rather than testing the whole
# field is what keeps kiai detection correct.
EFFECT_KIAI = 1
EFFECT_OMIT_FIRST_BARLINE = 8

DEFAULT_BEAT_LENGTH = 500.0  # 120 BPM, used only when a map has no timing at all

_uid_counter = count(1)


def _next_uid() -> int:
    return next(_uid_counter)


@dataclass(slots=True)
class TimingPoint:
    """One line of [TimingPoints].

    Field order matches the osu! file format:
        time, beatLength, meter, sampleSet, sampleIndex, volume,
        uninherited, effects
    """

    time: float
    beat_length: float
    meter: int = 4
    sample_set: int = 0
    sample_index: int = 0
    volume: int = 100
    uninherited_flag: int = 1
    effects: int = 0
    uid: int = field(default_factory=_next_uid)
    source_line_index: int = -1

    # -- derived -----------------------------------------------------------

    @property
    def uninherited(self) -> bool:
        return self.uninherited_flag == 1

    @property
    def inherited(self) -> bool:
        return not self.uninherited

    @property
    def kiai(self) -> bool:
        return bool(self.effects & EFFECT_KIAI)

    @property
    def omit_first_barline(self) -> bool:
        return bool(self.effects & EFFECT_OMIT_FIRST_BARLINE)

    @property
    def bpm(self) -> float | None:
        """Beats per minute, or None for an inherited point."""
        if not self.uninherited or self.beat_length <= 0:
            return None
        return 60000.0 / self.beat_length

    @property
    def sv_multiplier(self) -> float:
        """Slider velocity multiplier. Uninherited points always read as 1.0x."""
        if self.uninherited:
            return 1.0
        # `not < 0` rather than `>= 0`: a NaN green line is 1.0x in osu!, every
        # comparison against NaN being false (LegacyBeatmapDecoder).
        if not self.beat_length < 0:
            return 1.0
        return -100.0 / self.beat_length

    # -- mutation helpers --------------------------------------------------

    def set_kiai(self, enabled: bool) -> None:
        self.effects = (self.effects | EFFECT_KIAI) if enabled else (self.effects & ~EFFECT_KIAI)

    def set_omit_first_barline(self, enabled: bool) -> None:
        if enabled:
            self.effects |= EFFECT_OMIT_FIRST_BARLINE
        else:
            self.effects &= ~EFFECT_OMIT_FIRST_BARLINE

    def set_sv(self, multiplier: float) -> None:
        """Set the SV multiplier on an inherited point."""
        if self.uninherited:
            raise ValueError("Cannot set SV on an uninherited timing point")
        if not math.isfinite(multiplier) or multiplier <= 0:
            raise ValueError("SV multiplier must be positive and finite")
        self.beat_length = -100.0 / multiplier

    def copy(self) -> "TimingPoint":
        """Duplicate this point with a fresh identity and no source line."""
        return TimingPoint(
            time=self.time,
            beat_length=self.beat_length,
            meter=self.meter,
            sample_set=self.sample_set,
            sample_index=self.sample_index,
            volume=self.volume,
            uninherited_flag=self.uninherited_flag,
            effects=self.effects,
        )

    # -- constructors ------------------------------------------------------

    @classmethod
    def default(cls) -> "TimingPoint":
        """The fallback used when a document carries no timing information."""
        return cls(time=0.0, beat_length=DEFAULT_BEAT_LENGTH)

    @classmethod
    def uninherited_at(
        cls,
        time: float,
        bpm: float,
        *,
        meter: int = 4,
        kiai: bool = False,
        omit_first_barline: bool = False,
        template: "TimingPoint | None" = None,
    ) -> "TimingPoint":
        if not math.isfinite(bpm) or bpm <= 0:
            raise ValueError("BPM must be positive and finite")
        effects = (EFFECT_KIAI if kiai else 0) | (EFFECT_OMIT_FIRST_BARLINE if omit_first_barline else 0)
        return cls(
            time=float(time),
            beat_length=60000.0 / float(bpm),
            meter=meter,
            sample_set=template.sample_set if template else 0,
            sample_index=template.sample_index if template else 0,
            volume=template.volume if template else 100,
            uninherited_flag=1,
            effects=effects,
        )

    @classmethod
    def inherited_at(
        cls,
        time: float,
        sv: float,
        *,
        kiai: bool = False,
        omit_first_barline: bool = False,
        template: "TimingPoint | None" = None,
    ) -> "TimingPoint":
        """Create an SV point.

        Sample set, index and volume are inherited from `template`, normally the
        point active at `time`. Without that, bulk-generated SV would silently
        reset hitsound volume across the whole section it covers.
        """
        if not math.isfinite(sv) or sv <= 0:
            raise ValueError("SV multiplier must be positive and finite")
        effects = (EFFECT_KIAI if kiai else 0) | (EFFECT_OMIT_FIRST_BARLINE if omit_first_barline else 0)
        return cls(
            time=float(time),
            beat_length=-100.0 / float(sv),
            meter=template.meter if template else 4,
            sample_set=template.sample_set if template else 0,
            sample_index=template.sample_index if template else 0,
            volume=template.volume if template else 100,
            uninherited_flag=0,
            effects=effects,
        )


# -- parsing ---------------------------------------------------------------


def _as_int(fields: Sequence[str], index: int, default: int) -> int:
    if index >= len(fields):
        return default
    text = fields[index].strip()
    if not text:
        return default
    # Some editors write "4.0" where an int is expected.
    return int(float(text))


def parse_timing_line(text: str, source_line_index: int = -1) -> TimingPoint | None:
    """Parse one [TimingPoints] line, or return None if it is not one.

    Missing trailing fields fall back to their defaults instead of rejecting the
    line, which is what makes v4-and-earlier maps parse.
    """
    content = text.rstrip("\r\n").strip()
    if not content or content.startswith("//"):
        return None
    fields = content.split(",")
    if len(fields) < 2:
        return None
    try:
        time = float(fields[0])
        beat_length = float(fields[1])
    except ValueError:
        return None
    if not math.isfinite(time) or not math.isfinite(beat_length):
        return None
    try:
        return TimingPoint(
            time=time,
            beat_length=beat_length,
            meter=_as_int(fields, 2, 4),
            sample_set=_as_int(fields, 3, 0),
            sample_index=_as_int(fields, 4, 0),
            volume=_as_int(fields, 5, 100),
            # A legacy two-field line is always uninherited: inherited points
            # did not exist before the flag did.
            uninherited_flag=_as_int(fields, 6, 1),
            effects=_as_int(fields, 7, 0),
            source_line_index=source_line_index,
        )
    except ValueError:
        return None


def parse_timing_points(lines: Iterable[str]) -> list[TimingPoint]:
    """Parse the [TimingPoints] section out of a full list of file lines."""
    points: list[TimingPoint] = []
    section = ""
    for index, line in enumerate(lines):
        stripped = line.rstrip("\r\n").strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section != "TimingPoints":
            continue
        point = parse_timing_line(line, index)
        if point is not None:
            points.append(point)
    return points


def _format_number(value: float) -> str:
    """Render a float the way osu! does, without gaining a trailing .0."""
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(float(value))


def serialize_timing_point(point: TimingPoint, ending: str = "\r\n") -> str:
    return ",".join((
        _format_number(point.time),
        _format_number(point.beat_length),
        str(int(point.meter)),
        str(int(point.sample_set)),
        str(int(point.sample_index)),
        str(int(point.volume)),
        str(int(point.uninherited_flag)),
        str(int(point.effects)),
    )) + ending


# -- queries ---------------------------------------------------------------


def sorted_by_time(points: Sequence[TimingPoint]) -> list[TimingPoint]:
    """Stable sort by time only.

    Never sort by (time, uninherited). osu! resolves points sharing a timestamp
    by file order, so a compound key would silently reorder kiai and SV
    boundaries in existing maps.
    """
    return sorted(points, key=lambda point: point.time)


def uninherited_points(points: Iterable[TimingPoint]) -> list[TimingPoint]:
    return [point for point in points if point.uninherited and point.beat_length > 0]


def _index_at_or_before(points: Sequence[TimingPoint], time_ms: float) -> int:
    """Index of the last point at or before `time_ms`, or -1.

    Binary search rather than a scan: these lookups run per note per frame in
    the editor views, and a gimmick map carries tens of thousands of points --
    a linear walk there is the difference between a repaint and a freeze.
    """
    low, high = 0, len(points)
    while low < high:
        middle = (low + high) // 2
        if points[middle].time <= time_ms:
            low = middle + 1
        else:
            high = middle
    return low - 1


def active_point_at(points: Sequence[TimingPoint], time_ms: float) -> TimingPoint | None:
    """Last point at or before `time_ms`, assuming `points` is time-sorted."""
    index = _index_at_or_before(points, time_ms)
    return points[index] if index >= 0 else None


def active_uninherited_at(points: Sequence[TimingPoint], time_ms: float) -> TimingPoint:
    """Timing section covering `time_ms`, falling back to 120 BPM.

    `points` must be time-sorted; it may hold inherited points too, which are
    stepped over rather than filtered out into a fresh list -- this is called
    once per note per frame, and the filtering allocation was showing up as a
    stutter on maps with many SV points.
    """
    index = _index_at_or_before(points, time_ms)
    while index >= 0:
        point = points[index]
        if point.uninherited and point.beat_length > 0:
            return point
        index -= 1
    first = next((point for point in points if point.uninherited and point.beat_length > 0), None)
    return first or TimingPoint.default()


def sv_at(points: Sequence[TimingPoint], time_ms: float) -> float:
    """Effective SV multiplier at `time_ms`, assuming `points` is time-sorted.

    An uninherited point resets SV to 1.0x, which is why this reads the last
    point of either kind rather than only the inherited ones.
    """
    point = active_point_at(points, time_ms)
    return point.sv_multiplier if point is not None else 1.0


def kiai_spans(points: Sequence[TimingPoint], duration_ms: int) -> list[tuple[int, int]]:
    """Merged (start, end) kiai ranges.

    Kiai is a property of the active point, so this tracks state transitions in
    file order rather than pairing open/close events. Pairing breaks when an
    uninherited and an inherited point share a timestamp, because sorting
    tuples puts False before True and emits a zero-length span.
    """
    spans: list[tuple[int, int]] = []
    active = False
    start = 0
    for point in sorted_by_time(points):
        if point.kiai and not active:
            active, start = True, int(point.time)
        elif not point.kiai and active:
            active = False
            end = int(point.time)
            if end > start:
                spans.append((start, end))
    if active and duration_ms > start:
        spans.append((start, duration_ms))
    return spans


def iter_barline_times(
    points: Sequence[TimingPoint],
    duration_ms: int,
    *,
    limit: int = 100000,
) -> Iterator[float]:
    """Barline positions, one per measure within each uninherited section.

    Sections honour their own meter, and a point with the omit-first-barline
    effect skips the downbeat at its own start.
    """
    sections = uninherited_points(sorted_by_time(points))
    if not sections:
        return
    emitted = 0
    for index, section in enumerate(sections):
        end = sections[index + 1].time if index + 1 < len(sections) else float(duration_ms)
        measure = section.beat_length * max(1, section.meter)
        if measure <= 0:
            continue
        time = section.time
        if section.omit_first_barline:
            time += measure
        while time < end and emitted < limit:
            yield time
            time += measure
            emitted += 1


# -- validation ------------------------------------------------------------


class TimingValidationError(ValueError):
    """Raised when a timing point cannot be safely serialized."""


def validate_timing_points(points: Sequence[TimingPoint]) -> None:
    """Reject anything that would produce an unloadable [TimingPoints] section.

    Duplicate timestamps are explicitly allowed: barline gimmicks create dense
    clusters at note time +/- 2, 4 and 6 ms by design.
    """
    if not points:
        return
    for point in points:
        if not math.isfinite(point.time):
            raise TimingValidationError(f"Timing point has a non-finite time: {point.time!r}")
        if not math.isfinite(point.beat_length):
            raise TimingValidationError(f"Timing point at {point.time} has a non-finite beat length")
        if point.beat_length == 0:
            raise TimingValidationError(f"Timing point at {point.time} has a zero beat length")
        if point.uninherited and point.beat_length < 0:
            raise TimingValidationError(
                f"Uninherited timing point at {point.time} has a negative beat length"
            )
        if point.inherited and point.beat_length > 0:
            raise TimingValidationError(
                f"Inherited timing point at {point.time} has a positive beat length"
            )
        if point.meter < 1:
            raise TimingValidationError(f"Timing point at {point.time} has meter {point.meter}")
        if not 0 <= point.volume <= 100:
            raise TimingValidationError(f"Timing point at {point.time} has volume {point.volume}")
        if point.effects < 0:
            raise TimingValidationError(f"Timing point at {point.time} has negative effects")
    if not any(point.uninherited for point in points):
        raise TimingValidationError("A map with timing points needs at least one uninherited point")


# osu!'s own [Difficulty] SliderMultiplier default, and nothing more: the
# parser now reads the map's real value (`OsuDocument.slider_multiplier`), so
# this is only the fallback for a file with no SliderMultiplier line at all.
# It is not a stand-in for the real number -- the whole length<->duration
# conversion scales linearly with it, so a map authored at 2.0 had every
# drumroll end drawn at 1.4/2.0 of its true length while this was assumed.
SLIDER_MULTIPLIER_ASSUMED = 1.4


def duration_for_slider_length(
    length: float, beat_length: float, sv: float,
    slider_multiplier: float = SLIDER_MULTIPLIER_ASSUMED,
) -> float:
    """Inverse of slider_length_for_duration, for hit-testing an existing slider's end.

    `length` is the whole path a drumroll travels -- one slide's length times
    its slide count -- because osu! charges the per-slide duration once per
    slide and the two multiplications are the same one. Here rather than in
    `ui.common` so the converters in `chart/` can use it without Qt.
    """
    if beat_length <= 0 or sv <= 0 or slider_multiplier <= 0:
        return 0.0
    return length / (slider_multiplier * 100.0 * sv) * beat_length
