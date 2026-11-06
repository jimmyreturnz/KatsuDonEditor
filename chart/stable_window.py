"""Which circles osu! stable can never hit: its object window, derailed.

Read from osu!.exe (2026-11-05). Each frame stable gathers the objects that
can be hit, missed or note-locked -- its object window -- starting at
`List.BinarySearch` for the first object whose **EndTime** reaches
`now - PreEmpt`. The list is sorted by StartTime and the search compares
EndTime (`HitObject.CompareTo` against a probe), so it is only right while
end times rise with start times.

A NaN-length slider breaks that badly. Its EndTime is `Start + (int)NaN`,
and a C# cast of NaN on 32-bit .NET Framework is `int.MinValue`: it "ends"
2.1 billion ms before it starts. Whenever the search lands on one, that end
is below any probe, the search jumps right of it, and everything before it
leaves the window for that frame -- not hit, not missed, not there. Which
frames land on it depends on the shape of the search over the whole list,
so this is not "the note before a NaN slider": on Ruby My Dear - Spectacle
[FeelsTaikoMan] it is a run of 21 circles, 6 of them before the first NaN
slider of the section. The NaN slider itself is finished the moment it is in
the window (EndTime <= now), so it never note-locks anything.

Fake sliders end before they start too, and go through the same search.
"""
from __future__ import annotations

from bisect import bisect_right

INT_MIN = -2 ** 31


def net_int(value: float) -> int:
    """C#'s `(int)value` as osu! stable runs it: toward zero, and NaN or
    anything out of range comes out as int.MinValue (x86 cvttsd2si)."""
    if value != value or not -2.0 ** 31 <= value < 2.0 ** 31:
        return INT_MIN
    return int(value)


def _search(ends: list[int], probe: int) -> int:
    """.NET's `List.BinarySearch` with stable's comparison, index or ~index
    folded to the insertion point as stable does."""
    lo, hi = 0, len(ends) - 1
    while lo <= hi:
        mid = lo + ((hi - lo) >> 1)
        if ends[mid] == probe:
            return mid
        if ends[mid] < probe:
            lo = mid + 1
        else:
            hi = mid - 1
    return lo


def unhittable_circles(objects, preempt: int) -> set[int]:
    """`objects` are (start, stable EndTime, is_circle) in file order; the
    file-order indexes of the circles outside stable's object window at
    their own time, which is when autoplay presses them."""
    order = sorted(range(len(objects)), key=lambda k: (objects[k][0], k))
    starts = [objects[k][0] for k in order]
    ends = [objects[k][1] for k in order]
    last = len(order) - 1
    lost = set()
    for position, k in enumerate(order):
        start, _end, is_circle = objects[k]
        if not is_circle:
            continue
        first = _search(ends, start - preempt)
        # Stable walks on while Start <= now + PreEmpt, stopping on the last.
        window_end = max(first, min(bisect_right(starts, start + preempt), last))
        if not first <= position <= window_end:
            lost.add(k)
    return lost
