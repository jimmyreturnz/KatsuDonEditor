"""Hit-object model.

The parser used to keep only `fields[5]` and discard everything after it, which
silently dropped slider curve data, slide counts, lengths, edge sounds and
spinner end times. A taiko fake slider such as

    256,192,54692,2,12,L|624:192,643,-0.0001

parsed to hit_sample="L|624:192" with "643,-0.0001" simply gone. That was
invisible only because the writer never regenerated the line. Regenerating
[HitObjects] is required for note editing and for placing fake sliders, so the
middle fields are now preserved verbatim.

They are kept as raw strings rather than a typed slider model on purpose. Raw
strings round-trip "-0.0001", "L|624:192" and "0:0|0:0" character for character,
where a typed model risks reformatting 643 into 643.0 or -0.0001 into -1e-04
across every existing map. Typed accessors are layered on top instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import count

HITSOUND_WHISTLE = 2
HITSOUND_FINISH = 4
HITSOUND_CLAP = 8
KAT_HITSOUNDS = HITSOUND_WHISTLE | HITSOUND_CLAP

TYPE_CIRCLE = 1
TYPE_SLIDER = 2
TYPE_NEW_COMBO = 4
TYPE_SPINNER = 8
TYPE_HOLD = 128

# A trailing hit sample is "normalSet:additionSet:index:volume:filename".
# Testing for that shape is what lets sliders with 6, 8 or 9 fields be split
# correctly, where counting positionally cannot.
_HIT_SAMPLE = re.compile(r"^\d+:\d+:\d+:\d+:")

_uid_counter = count(1)


def _next_uid() -> int:
    return next(_uid_counter)


def split_object_fields(fields: list[str]) -> tuple[tuple[str, ...], str]:
    """Split the fields after hitSound into (extras, hit_sample)."""
    rest = fields[5:]
    if rest and _HIT_SAMPLE.match(rest[-1]):
        return tuple(rest[:-1]), rest[-1]
    return tuple(rest), ""


@dataclass(slots=True)
class HitObject:
    x: int
    y: int
    time: int
    type: int
    hit_sound: int
    extras: tuple[str, ...] = ()
    hit_sample: str = ""
    original_index: int = 0
    source_line_index: int = -1
    uid: int = field(default_factory=_next_uid)

    # -- type predicates ---------------------------------------------------

    @property
    def is_circle(self) -> bool:
        return not (self.type & (TYPE_SLIDER | TYPE_SPINNER | TYPE_HOLD))

    @property
    def is_slider(self) -> bool:
        return bool(self.type & TYPE_SLIDER)

    @property
    def is_spinner(self) -> bool:
        return bool(self.type & TYPE_SPINNER)

    @property
    def is_hold(self) -> bool:
        return bool(self.type & TYPE_HOLD)

    @property
    def is_new_combo(self) -> bool:
        return bool(self.type & TYPE_NEW_COMBO)

    # -- taiko semantics ---------------------------------------------------

    @property
    def is_kat(self) -> bool:
        """Whether the kat hitsound bits are set.

        Deliberately NOT gated on is_circle. gui.py uses this both for rendering
        and for splitting a selection into the Don and Kat transformation
        groups, so gating it here would silently move every drumroll into the
        Don group and change existing transformation output. Use note_kind when
        the actual object type matters.
        """
        return bool(self.hit_sound & KAT_HITSOUNDS)

    @property
    def is_finisher(self) -> bool:
        """Whether the finish hitsound bit is set. See is_kat on gating."""
        return bool(self.hit_sound & HITSOUND_FINISH)

    @property
    def note_kind(self) -> str:
        """don, kat, big_don, big_kat, drumroll, denden or hold.

        Resolved by object type first, because a taiko drumroll is a slider and
        a denden is a spinner. On those, the hitsound bits mean something other
        than kat, so classifying by hitsound alone reports a drumroll carrying
        hitsound 12 as a big kat.
        """
        if self.is_slider:
            return "drumroll"
        if self.is_spinner:
            return "denden"
        if self.is_hold:
            return "hold"
        if self.is_kat:
            return "big_kat" if self.is_finisher else "kat"
        return "big_don" if self.is_finisher else "don"

    # -- slider and spinner accessors, parsed lazily from extras -----------

    @property
    def curve(self) -> str | None:
        """Raw "L|624:192" style curve description, sliders only."""
        return self.extras[0] if self.is_slider and self.extras else None

    @property
    def slides(self) -> int | None:
        if not self.is_slider or len(self.extras) < 2:
            return None
        try:
            return int(float(self.extras[1]))
        except ValueError:
            return None

    @property
    def is_nan_slider(self) -> bool:
        """A slider of length NaN: osu! draws nothing for it, and the don/kat
        right before it can't be hit (owner, 2026-10-28; taiko-rs plays that).
        The chart still shows it, marked NaN, so it can be found."""
        length = self.length
        return length is not None and length != length

    @property
    def length(self) -> float | None:
        if not self.is_slider or len(self.extras) < 3:
            return None
        try:
            return float(self.extras[2])
        except ValueError:
            return None

    @property
    def end_time(self) -> int | None:
        """End time of a spinner or hold.

        A spinner keeps it in its own field. A mania hold merges end time and
        hit sample into a single "8000:0:0:0:0:" field, which is
        indistinguishable from a plain hit sample and therefore lands in
        hit_sample rather than extras. Round-tripping is unaffected either way,
        since to_line reassembles the same text.
        """
        if not (self.is_spinner or self.is_hold):
            return None
        source = self.extras[0] if self.extras else self.hit_sample
        if not source:
            return None
        try:
            return int(float(source.split(":", 1)[0]))
        except ValueError:
            return None

    # -- serialization -----------------------------------------------------

    def to_line(self, ending: str = "\r\n") -> str:
        parts = [
            str(self.x), str(self.y), str(self.time),
            str(self.type), str(self.hit_sound),
        ]
        parts.extend(self.extras)
        if self.hit_sample:
            parts.append(self.hit_sample)
        return ",".join(parts) + ending

    @classmethod
    def from_line(cls, text: str, original_index: int = 0, source_line_index: int = -1) -> "HitObject | None":
        content = text.rstrip("\r\n")
        fields = content.split(",")
        if len(fields) < 5:
            return None
        try:
            x, y, time, type_, hit_sound = (int(fields[i]) for i in range(5))
        except ValueError:
            return None
        extras, hit_sample = split_object_fields(fields)
        return cls(
            x=x, y=y, time=time, type=type_, hit_sound=hit_sound,
            extras=extras, hit_sample=hit_sample,
            original_index=original_index, source_line_index=source_line_index,
        )
