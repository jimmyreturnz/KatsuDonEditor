"""Reading a chart's own SV back as the curves it was drawn with.

The experimental converters (owner, 2026-10-09) turn notes into barline and
anti-barline structures without losing the chart's scroll speed. Every
structure is red lines, and a red line resets SV to 1.0x -- so the speed has to
be written back after each one, and an anti-barline wall puts a line every
twentieth of a beat *between* notes, where the chart never said what the speed
was. Read as osu! reads it, SV is a step at each green line, and a wall drawn
on the steps is a staircase where the mapper drew a ramp.

So the green lines are read as what they were made with: a sweep of
`sv_ease` curves (Generate SV's, ported from TaikoEditor) between the notes
they govern, which can then be evaluated anywhere in between. Three steps:

* `anchors` -- which note each green line is *for*. Mappers place a note's
  line a few ms before it so the note reads it; it is never placed back past
  the previous note, which is what makes "the next note" the answer.
* `detect_runs` -- the curves: the longest stretches of anchors that one
  curve passes through, and which curve.
* `SvModel.value_at` -- the speed at any millisecond: the curve inside a run,
  the chart's own step everywhere else. It never invents a value where the
  chart has no green line to read.

No Qt here: `sv_ease` moved in from gui.py so the detector and Generate SV
share the very function that wrote the numbers being read back.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from osu_io.timing import TimingPoint, sorted_by_time, sv_at


def _true_exp_ease(t: float, initial: float | None, final: float | None) -> float:
    """The one curve whose shape is not fixed.

    Interpolating `initial + (final - initial) * this` reduces exactly to
    `initial * (final / initial) ** t` -- a geometric sweep, multiplying by a
    constant factor per step, which is what reads as an even acceleration. So
    the bend has to come from the range itself: 1.0 -> 1.1 is nearly a straight
    line where 1.0 -> 10.0 is a hard curve.

    A fixed shape cannot do that. This was `(exp(3t) - 1) / (exp(3) - 1)`,
    which gave every range the same bend and put every intermediate point in
    the wrong place -- 0.52x out at the midpoint of a 1 -> 10 sweep -- while
    still hitting both endpoints exactly, which is why it looked right.

    Falls back to linear on any range with no geometric reading: equal
    endpoints (the 0/0), a zero start, or a sign change, whose fractional power
    is complex rather than an error in Python. TaikoEditor guards the same
    cases at `SvFunctionLayer` lines 275-287.
    """
    if initial is None or final is None or initial == final or initial == 0:
        return t
    ratio = final / initial
    if ratio <= 0:
        return t
    try:
        eased = (initial - initial * ratio ** t) / (initial - final)
    except (OverflowError, ValueError):
        return t
    return eased if math.isfinite(eased) else t


def sv_ease(
    function_id: str, t: float,
    initial: float | None = None, final: float | None = None,
) -> float:
    """0..1 progress -> 0..1 eased position. Shared by the preview square and
    the actual generated points, so the preview is an honest picture of what
    Generate produces, not just a decoration.

    Ported from TaikoEditor's `SvFunctionLayer` (its lines 118-128), so the
    same-named curve in either editor writes the same numbers. That includes
    its naming of the sine pair, which is the *inverse* of easings.net's: here
    "Sin In" is fast at the start and slow at the end.

    `initial`/`final` are the endpoints the caller goes on to interpolate
    between. Only "true_exp" reads them; every other curve is a fixed arc.
    """
    t = max(0.0, min(1.0, t))
    if function_id == "sin_in":
        return math.sin(t * math.pi / 2)
    if function_id == "sin_out":
        return 1 - math.cos(t * math.pi / 2)
    if function_id.startswith("exp") and function_id != "true_exp":
        # The exponent is carried *in the id* ("exp1.3", "exp2.5"), so a typed
        # one needs no extra argument threaded through every caller -- the
        # preview, the generator and a stored choice all take the same string
        # they always did. Floored at 1: below it the curve bends the other
        # way, which is what the two fixed tiles above 1 were there to avoid.
        try:
            exponent = float(function_id[3:])
        except ValueError:
            return t
        return t ** max(1.0, exponent)
    if function_id == "true_exp":
        return _true_exp_ease(t, initial, final)
    if function_id == "sin":
        return -(math.cos(math.pi * t) - 1) / 2
    return t  # "linear" and any unrecognized id


# A run whose two ends are its only anchors. Barline mode keeps the chart's own
# step there; anti-barline mode ramps it (owner, 2026-10-09: the wall fills the
# gap between notes, so a jump in it is a tear in the sheet).
STEP = "step"
LINEAR = "linear"
# The fixed curves Generate SV offers, in the order a tie is settled: the
# simplest explanation first.
FIXED_CURVES = ("linear", "exp1.3", "exp1.6", "sin_in", "sin_out", "sin", "true_exp")
# Generate SV's "Exp x" takes any typed exponent; the detector searches for it
# between these, and only over runs long enough that a fitted exponent is a
# finding rather than a way to bend a curve through one stray point.
EXPONENT_RANGE = (1.0, 10.0)
FITTED_EXPONENT_MIN_ANCHORS = 4
BARLINE, ANTI = "barline", "anti"


def tolerance(value: float) -> float:
    """How far a green line may sit off a curve and still be on it. Values
    come back through a text file as beat lengths, and other editors write
    them typed to two decimals; 0.2% is far looser than either round trip and
    far tighter than the gap between two Generate SV curves."""
    return max(5e-4, 0.002 * abs(value))


@dataclass(frozen=True)
class Anchor:
    """One green line, at the millisecond it governs and with its value.
    `group` changes at every original red line: nothing is interpolated
    across an SV reset."""

    time: float
    value: float
    source_time: float
    group: int


@dataclass
class Run:
    """A stretch of anchors one curve passes through. `function` is editable
    (the conversion window lets the mapper correct a guess). `points` are the
    anchors themselves, kept so the run can pass through each one exactly."""

    start: Anchor
    end: Anchor
    function: str
    count: int
    points: tuple = ()

    def curve_at(self, time_ms: float) -> float:
        span = self.end.time - self.start.time
        t = 0.0 if span <= 0 else (time_ms - self.start.time) / span
        a, b = self.start.value, self.end.value
        return a + (b - a) * sv_ease(self.function, t, a, b)


def anchors(points, notes, start_ms: float, end_ms: float) -> list[Anchor]:
    """The green lines governing `start_ms`..`end_ms`, each at the note it is for.

    The last green line before a note, with no note between them, is that
    note's: it was offset back so the note would read it. Any other green line
    (two before one note, one with no note after it) stands at its own time.
    A line offset back from the range's first note sits before `start_ms`, so
    the window opens at the note before the range instead.
    """
    ordered = sorted_by_time(points)
    note_times = sorted({float(note.time) for note in notes})
    first_note = bisect_left(note_times, start_ms)
    window_start = note_times[first_note - 1] if first_note > 0 else float("-inf")
    red_times = [point.time for point in ordered if point.uninherited]
    greens = [point for point in ordered
              if not point.uninherited and window_start < point.time <= end_ms]
    found: list[Anchor] = []
    for index, green in enumerate(greens):
        group = bisect_right(red_times, green.time)
        at = green.time
        next_note = bisect_left(note_times, green.time)
        if next_note < len(note_times):
            note_time = note_times[next_note]
            later_green = index + 1 < len(greens) and greens[index + 1].time <= note_time
            red_between = bisect_right(red_times, note_time) != group
            if not later_green and not red_between:
                at = note_time
        found.append(Anchor(at, green.sv_multiplier, green.time, group))
    return found


def _residual(anchors_: list[Anchor], function: str, stop_at_miss: bool = False) -> float:
    """The worst miss past tolerance (<= 0 is a fit). `stop_at_miss` returns
    at the first miss: most candidates fail on an early point, and scoring
    them to the end was most of a whole-map conversion. A curve that fits is
    still scored in full, so ties are broken as before."""
    run = Run(anchors_[0], anchors_[-1], function, len(anchors_))
    worst = -1.0
    for anchor in anchors_[1:-1]:
        excess = abs(anchor.value - run.curve_at(anchor.time)) - tolerance(anchor.value)
        if excess > worst:
            worst = excess
            if stop_at_miss and excess > 0:
                return excess
    return worst


def _fitted_exponent(anchors_: list[Anchor]) -> str | None:
    """Generate SV's typed "Exp x": the exponent that explains the run, kept
    only if it fits.

    Solved, not searched. A point on `a + (b - a) * t**p` has
    `p = ln(u) / ln(t)` with `u` its share of the way from `a` to `b`, so each
    interior anchor names the exponent directly; the median of the
    well-conditioned ones is checked once. A 60-step golden-section search per
    candidate run end was 117s of a 131s whole-map conversion (Darling Game
    Over Love, Ura Oni, measured 2026-10-09).
    """
    first, last = anchors_[0], anchors_[-1]
    span, rise = last.time - first.time, last.value - first.value
    if span <= 0 or rise == 0:
        return None
    named = []
    for anchor in anchors_[1:-1]:
        t = (anchor.time - first.time) / span
        u = (anchor.value - first.value) / rise
        # Near either end ln() of t or u is near 0 and a rounding error in
        # the value names any exponent at all.
        if 0.05 < t < 0.95 and 0.0 < u < 1.0:
            named.append(math.log(u) / math.log(t))
    if not named:
        return None
    named.sort()
    exponent = named[len(named) // 2]
    if not EXPONENT_RANGE[0] <= exponent <= EXPONENT_RANGE[1]:
        return None
    function = f"exp{round(exponent, 2):g}"
    return function if _residual(anchors_, function) <= 0 else None


def fit(anchors_: list[Anchor], mode: str = BARLINE) -> str | None:
    """The curve these anchors lie on, or None. Two anchors are always a
    run: a step in barline mode, a ramp in anti-barline mode."""
    if len(anchors_) < 2:
        return None
    if len(anchors_) == 2:
        return STEP if mode == BARLINE else LINEAR
    if anchors_[-1].time <= anchors_[0].time:
        return None
    scored = [(_residual(anchors_, function, stop_at_miss=True), order, function)
              for order, function in enumerate(FIXED_CURVES)]
    fitting = [entry for entry in scored if entry[0] <= 0]
    if fitting:
        return min(fitting)[2]
    if len(anchors_) >= FITTED_EXPONENT_MIN_ANCHORS:
        return _fitted_exponent(anchors_)
    return None


def detect_runs(found: list[Anchor], mode: str = BARLINE) -> list[Run]:
    """The curves through `found`, each as long as one curve will reach.

    From the left, per group, and the next run starts on the anchor this one
    ended on -- consecutive sweeps share their turning point, as Generate SV
    writes them.

    Not grown one anchor at a time: the first few points of a Sin sweep are
    not a Sin sweep between *their own* ends, so a run grown that way stopped
    after three points and the sweep came back as seven pieces. Instead,
    every offered curve is monotonic, so a run can reach no further than the
    values keep going one way; the ends are tried from there inward and the
    first that fits is taken.
    """
    runs: list[Run] = []
    groups: dict[int, list[Anchor]] = {}
    for anchor in found:
        groups.setdefault(anchor.group, []).append(anchor)
    for group in groups.values():
        start = 0
        while start < len(group) - 1:
            reach = _monotone_reach(group, start)
            end, function = start + 1, fit(group[start:start + 2], mode)
            for candidate in range(reach, start + 1, -1):
                longer = fit(group[start:candidate + 1], mode)
                if longer is not None:
                    end, function = candidate, longer
                    break
            runs.append(Run(group[start], group[end], function, end - start + 1,
                            tuple(group[start:end + 1])))
            start = end
    return runs


def _monotone_reach(group: list[Anchor], start: int) -> int:
    """The last index the values from `start` keep going one way to --
    flat counts as either way, within tolerance."""
    direction = 0
    end = start
    for index in range(start + 1, len(group)):
        step = group[index].value - group[index - 1].value
        if abs(step) <= tolerance(group[index].value):
            step = 0.0
        if step:
            sign = 1 if step > 0 else -1
            if direction and sign != direction:
                break
            direction = sign
        end = index
    return end


class SvModel:
    """The chart's SV as a function of time.

    **At every note, exactly the value osu! reads there** -- whatever the
    curve says. A green line usually governs several notes, and every one of
    them scrolls at its step value; interpolating anchor to anchor sped up
    every note between two lines (measured on installed maps: up to 7.7x off).

    **Between two neighbouring notes, a ramp only if a green line sits in
    that gap** -- the gap where the chart itself changed speed -- following
    the run's curve from one note's value to the next. Every other gap holds
    the step: no green line, no new speed (owner, 2026-10-09). So does a gap
    crossed by a red line, outside every run, and inside a `step` run.
    """

    def __init__(self, runs: list[Run], original_points, notes=()) -> None:
        self.runs = sorted(runs, key=lambda run: run.start.time)
        self._starts = [run.start.time for run in self.runs]
        self._original = sorted_by_time(original_points)
        self._green_times = [point.time for point in self._original if not point.uninherited]
        self._red_times = [point.time for point in self._original if point.uninherited]
        self._note_times = sorted({float(note.time) for note in notes})

    def run_at(self, time_ms: float) -> Run | None:
        index = bisect_right(self._starts, time_ms) - 1
        # A shared boundary belongs to the run it starts, so a curve's last
        # anchor and the next one's first read the same value either way.
        for run in self.runs[max(0, index - 1):index + 1][::-1]:
            if run.start.time <= time_ms <= run.end.time:
                return run
        return None

    def value_at(self, time_ms: float) -> float:
        step = sv_at(self._original, time_ms)
        run = self.run_at(time_ms)
        if run is None or run.function == STEP:
            return step
        index = bisect_right(self._note_times, time_ms) - 1
        if index < 0 or index + 1 >= len(self._note_times) or self._note_times[index] == time_ms:
            return step
        left, right = self._note_times[index], self._note_times[index + 1]
        changes = bisect_right(self._green_times, right) - bisect_right(self._green_times, left)
        resets = bisect_right(self._red_times, right) - bisect_right(self._red_times, left)
        if not changes or resets:
            return step
        start, end = sv_at(self._original, left), sv_at(self._original, right)
        curve_left, curve_right = run.curve_at(left), run.curve_at(right)
        if abs(curve_right - curve_left) > 1e-12:
            share = (run.curve_at(time_ms) - curve_left) / (curve_right - curve_left)
        else:
            share = (time_ms - left) / (right - left)
        return max(0.01, start + (end - start) * min(1.0, max(0.0, share)))


def build_model(points, notes, start_ms: float, end_ms: float, mode: str = BARLINE) -> SvModel:
    return SvModel(detect_runs(anchors(points, notes, start_ms, end_ms), mode), points, notes)
