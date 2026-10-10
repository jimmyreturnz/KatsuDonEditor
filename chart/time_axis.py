"""The time axis every scrolling view shares.

`TimelineGameplay`, `SVEditorView` and (partly) `GameplayViewerView` each grew
their own copy of the same five behaviours: the render throttle, the pixel<->time
mapping, Alt-wheel snap changes, Ctrl-wheel zoom plus wheel seeking, and the
auto-scroll that lets a rubber-band drag run past the view's edge. The copies had
already begun to drift -- `SVEditorView.wheelEvent` carried the comment "mirrors
TimelineGameplay" while clamping zoom to a different maximum -- and M5 adds six
more views on top of them.

Only the genuinely identical parts live here. `GameplayViewerView` keeps its own
`wheelEvent`, because Ctrl there means scroll speed rather than zoom and it has
no `window_ms` to share, and it keeps its own geometry, because its x axis is
pixels-per-beat rather than a centred time window.

A mixin rather than a base class: these are QWidget subclasses with their own
`__init__`, signals and paint code, and Qt's metaclass is happier composing
plain-Python behaviour in than being subclassed twice deep.

The host class must provide, as attributes or signals:

    current_time, last_rendered_time, window_ms, wheel_accumulator,
    snap_divisor, timing_points
    seek_requested(float), zoom_changed(float), snap_changed_by_wheel(int)
"""
from __future__ import annotations

import math
from bisect import bisect_right

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QApplication

from osu_io.timing import (
    TimingPoint,
    active_uninherited_at,
    kiai_spans,
    uninherited_points,
)


def osu_round(value: float) -> int:
    """Nearest whole millisecond, halves **up** -- never to even.

    For a value that is not a beat position: the millisecond under the cursor
    (Ctrl placement), where nearest is what the pointer means. Python's
    built-in `round` is banker's rounding -- `round(0.5)` is 0 and `round(1.5)`
    is 2 -- so "sometimes down, sometimes up" depending on whether the
    neighbouring integer happened to be even, which is the hardest possible
    version of that to reason about while placing objects a millisecond apart.

    **Not** how a snapped position becomes an integer -- see `osu_snap_ms`.
    """
    return math.floor(value + 0.5)


def osu_snap_ms(value: float) -> int:
    """The whole millisecond osu!'s editor commits a beat position to: **down**.

    Measured, because this was rounding and the report was that notes written
    here sat one millisecond above the ones osu! writes on the same snap.
    Over 25 installed maps, every circle on a clean single-BPM map lands in
    `(-1, 0]` of its exact fractional beat position and **never above it** --
    a flat spread with nothing on the positive side, which is truncation and
    could not be rounding. (`Hiyashi 2014`, a 315.789ms beat: 114 notes a full
    0.9ms below their gridline, none above.) osu!stable casts the position to
    an int; C# truncates toward zero, and an editor time is never negative.

    Every millisecond this editor commits to a *beat* -- a drawn gridline, a
    wheel seek, a placement -- goes through here, so a note placed on a
    gridline here is the millisecond osu! would have written for the same snap.
    """
    return math.floor(value)

# Kiai, behind everything else in a scrolling view. The same orange the
# overview bar marks its sections with, at an alpha low enough to read the grid,
# the notes and the SV curve straight through -- it says "this is a chorus" and
# takes nothing away.
KIAI_BAND_COLOR = QColor(255, 170, 0, 34)

# Sentinel end for a kiai section the map never closes. `kiai_spans` needs a
# duration to close the last one against and a view has none; anything past the
# end of any real song does, since the span is clipped to the window anyway.
KIAI_OPEN_END_MS = 1_000_000_000

# Translucent grey for the Ctrl-precision line (see draw_ctrl_precision_line):
# distinct from the opaque white playhead cursor and from the selection drag's
# own tint, so a glance tells the three apart.
CTRL_PRECISION_LINE_COLOR = QColor(190, 200, 215, 130)
# Bright enough to read against the view's dark ground without competing
# with the objects, which is what you are actually looking at.
CURSOR_READOUT_COLOR = QColor(205, 214, 228, 190)

# Every beat subdivision the editor offers; Alt+wheel steps along this list.
SNAP_DIVISORS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 27, 32, 36, 48, 64)


def snap_time(timing_points: list[TimingPoint], time_ms: float, divisor: int) -> float:
    timing = active_uninherited_at(timing_points, time_ms)
    snap_length = timing.beat_length / divisor
    return timing.time + round((time_ms - timing.time) / snap_length) * snap_length


# Where an object's *own* grid is looked for: coarsest first, so a note on a
# beat reads as 1/1 rather than 1/16.
OWN_DIVISORS = (1, 2, 3, 4, 6, 8, 12, 16)

# How far a gridline may be from an object that is "on" it. osu! commits a
# snapped position as a whole millisecond (`osu_snap_ms`), so an object written
# on a gridline is always less than one millisecond from it.
ON_GRID_TOLERANCE_MS = 1.0

# Resnap's reach: one more millisecond than being on the grid at all, which is
# the "a millisecond off" another editor's rounding leaves (osu! truncates, a
# rounding editor puts half its notes one above). Anything farther away was put
# there on purpose and is left alone.
RESNAP_TOLERANCE_MS = 2.0 * ON_GRID_TOLERANCE_MS


def own_divisor(
    timing_points: list[TimingPoint],
    time_ms: float,
    tolerance_ms: float = ON_GRID_TOLERANCE_MS,
    divisors: tuple[int, ...] = OWN_DIVISORS,
) -> int | None:
    """The coarsest divisor with a gridline within `tolerance_ms`, or None.

    A grid whose lines are closer together than twice the tolerance matches
    every time there is, so it says nothing about the object -- the search stops
    before those instead of reporting them. That bound is what keeps a gimmick
    section's 60000 BPM line (a 1ms beat) from calling every object on it
    "snapped".
    """
    beat_length = active_uninherited_at(timing_points, time_ms).beat_length
    for divisor in divisors:
        if beat_length / divisor <= 2.0 * tolerance_ms:
            break
        if abs(time_ms - snap_time(timing_points, time_ms, divisor)) < tolerance_ms:
            return divisor
    return None


def resnap_time(timing_points: list[TimingPoint], time_ms: float) -> float | None:
    """Where Resnap puts an object: the millisecond osu! writes for the nearest
    line of the object's own grid, or None when it is on no grid within reach."""
    divisor = own_divisor(timing_points, time_ms, RESNAP_TOLERANCE_MS)
    if divisor is None:
        return None
    return float(osu_snap_ms(snap_time(timing_points, time_ms, divisor)))


def wheel_seek_time(
    timing_points: list[TimingPoint],
    current_time: float,
    direction: int,
    divisor: int,
) -> float:
    """One snap division either side of `current_time` (one beat at divisor 1).

    Shared by every scrolling view so a wheel notch means the same thing in all
    of them. Each widget keeps its own accumulator: that part is about the
    hardware's delta, not about what one step means.

    The beat arithmetic itself is done in exact fractional milliseconds --
    rounding it first and feeding the rounded value into the next wheel step
    would accumulate drift across a section, the same way repeatedly rounding
    a running total drifts from rounding the total once. Only the value handed
    back is rounded, so a single notch lands the playhead on the same whole
    millisecond a placed object would use (see `TimeAxisMixin.snap_ms`).
    """
    timing = active_uninherited_at(timing_points, current_time)
    step = timing.beat_length / divisor
    snapped = snap_time(timing_points, current_time, divisor)
    if direction > 0:
        target = snapped + step
        # A red line is a gridline of its own, and usually an off-grid one --
        # a step that crossed it skipped the downbeat it was put there to mark.
        index = bisect_right(timing_points, current_time, key=lambda point: point.time)
        following = next(
            (point.time for point in timing_points[index:]
             if point.uninherited and point.beat_length > 0),
            None,
        )
        if following is not None and following < target:
            target = following
    elif snapped <= timing.time:
        # On this section's red line: the step back belongs to the section
        # before it, whose last gridline is not one of this section's.
        previous = active_uninherited_at(timing_points, timing.time - 1e-6)
        if previous is timing:
            target = snapped - step
        else:
            previous_step = previous.beat_length / divisor
            target = previous.time + (math.ceil(
                (timing.time - previous.time) / previous_step - 1e-6
            ) - 1) * previous_step
    else:
        target = snapped - step
    return float(osu_snap_ms(max(0.0, target)))


class TimeAxisMixin:
    """Scroll, zoom, snap and hit-test against a centred time window."""

    # Zoom limits. The SV editor allows a wider window than the chart views --
    # an SV sweep is read across a whole section, a note pattern is not.
    #
    # The floor is 20ms because that is the scale gimmick work actually happens
    # at: a barline note's restore lines sit +/-2ms from its gimmick line and a
    # fake slider's restore is +1ms, so at the old 500ms floor the whole
    # structure was a handful of pixels and could not be aimed at, let alone
    # dragged. 20ms puts one millisecond at 5% of the view width.
    MIN_WINDOW_MS = 20.0
    MAX_WINDOW_MS = 60000.0

    # One frame at 60Hz. Repainting more often than the screen updates is work
    # thrown away, and set_time is driven by the playback clock.
    RENDER_THROTTLE_MS = 16.0

    ZOOM_IN_FACTOR = 0.86
    ZOOM_OUT_FACTOR = 1.16

    # Whether the song is running, kept in step by MainWindow.toggle_playback.
    # A class attribute so every view that mixes this in has one without each
    # constructor having to remember; `TimelineGameplay` sets its own instance
    # copy for the playhead it draws.
    is_playing = False
    # True only while wheelEvent is emitting seek_requested -- see there.
    wheel_seek_in_progress = False

    # Set by the gimmick editor to the base timing snapshot. Empty everywhere
    # else, so the view snaps against the document it is showing.
    base_timing: list[TimingPoint] = []
    _snap_times: list[float] = []

    # (start, end) of every kiai section, refreshed with the document. Drawn by
    # `draw_kiai_bands`; empty until a host fills it in.
    kiai_bands: list[tuple[int, int]] = []

    def set_base_timing(self, points: list[TimingPoint]) -> None:
        """Anchor snaps and scroll to `points` instead of the live document."""
        self.base_timing = list(points)
        self._snap_times = [point.time for point in self.base_timing]
        self.update()

    @property
    def snap_points(self) -> list[TimingPoint]:
        """Timing the snap grid and the wheel step are measured against.

        In the gimmick editor this is the base snapshot rather than the live
        document: a gimmick fills the file with 60000 BPM lines, and snapping to
        those would collapse the grid to a few milliseconds under the cursor.
        """
        return self.base_timing or self.timing_points

    # Cache for `snap_beat_points`, and the exact list it was derived from.
    # The source is held by reference rather than by id() so a freed list's
    # address cannot be reused by a different one and pass as a cache hit;
    # the length is checked too, to catch an in-place append.
    _snap_beats: list[TimingPoint] = []
    _snap_beats_source: list[TimingPoint] | None = None
    _snap_beats_length: int = -1

    @property
    def snap_beat_points(self) -> list[TimingPoint]:
        """`snap_points` with the inherited points already removed.

        Same answers, without the walk: everything that resolves a snap goes
        through `active_uninherited_at`, which steps back over inherited points
        one at a time. On a gimmick difficulty -- thousands of green lines
        between two red ones -- that walk measured 81 steps per lookup and 1.3
        million attribute reads per 200 rendered frames, which is what made the
        grid the most expensive thing in a frame.

        Cached rather than filtered per call, because filtering per call is the
        cost the backward walk was written to avoid in the first place.
        """
        source = self.snap_points
        if source is not self._snap_beats_source or len(source) != self._snap_beats_length:
            self._snap_beats = uninherited_points(source)
            self._snap_beats_source = source
            self._snap_beats_length = len(source)
        return self._snap_beats

    # -- playhead ----------------------------------------------------------

    def set_time(self, time_ms: float, force: bool = False) -> None:
        """Follow the shared playhead, repainting at most once a frame.

        A snap is rarely a whole millisecond -- at 180 BPM a 1/64 division is
        5.208ms -- and the view is centred on `current_time` while the grid is
        drawn at the exact snap, so any millisecond the playhead loses on the
        way round is a visible gap between the two: at the 20ms zoom floor half
        a millisecond is 2.5% of the view's width, and it grew with every step
        further in. The playhead is therefore carried as a float from the view
        that moved it all the way back out to every other view (see
        `MainWindow.seek_audio`), and this adopts whatever it is handed.

        It used to keep its own fraction whenever the incoming position rounded
        to the same millisecond, which fixed the view that emitted the seek and
        left every other view a fraction of a millisecond away from it -- the
        six gimmick layers disagreeing with each other about where "now" is.
        """
        self.current_time = max(0.0, float(time_ms))
        if force or abs(self.current_time - self.last_rendered_time) >= self.RENDER_THROTTLE_MS:
            self.last_rendered_time = self.current_time
            self.update()

    # Supplies the kiai bands instead of each view deriving its own -- see
    # `MainWindow._cached`. Every band is the same list for every view, and
    # `kiai_spans` sorts the whole point list to build it, so six views
    # deriving it separately on every refresh was six sorts of a map that can
    # carry tens of thousands of points. Same shape as `foreign_times_for` and
    # the other `_for` hooks, and None everywhere it is not wired.
    kiai_bands_for = None

    # Same shape, same reason, for the two lists every view rebuilds from the
    # document on every refresh regardless of what it actually shows: the
    # whole point list stable-sorted by time, and that list's uninherited
    # (red-line) points alone. A gimmick placement refreshes every open view,
    # and each one sorting the same tens-of-thousands-of-points list -- twice,
    # since a chart view and an SV view both derive their own copy -- was
    # measurable. `MainWindow._share_kiai_bands` wires both when a view is a
    # gimmick or Editor-page view; None (compute locally) everywhere else,
    # including a view under test with no host to share a cache with.
    sorted_points_for = None
    uninherited_points_for = None

    def set_kiai_from(self, points: list[TimingPoint]) -> None:
        """Recompute the kiai bands from a document's *full* point list.

        The full list on purpose: kiai normally lives on inherited points, and a
        chart view keeps only the uninherited ones (`extract_timing_points`), so
        asking its own `timing_points` would find no kiai at all.
        """
        self.kiai_bands = kiai_spans(points, KIAI_OPEN_END_MS)

    def draw_kiai_bands(self, painter) -> None:
        """Translucent orange behind every kiai section in view.

        Drawn before the grid and the objects rather than over them: it is
        background information -- which sections are choruses -- and anything
        opaque enough to sit on top would be competing with the material.
        """
        if not self.kiai_bands:
            return
        start_time = self.current_time - self.window_ms / 2
        end_time = self.current_time + self.window_ms / 2
        for band_start, band_end in self.kiai_bands:
            if band_end < start_time or band_start > end_time:
                continue
            left = max(0.0, self.x_for_time(band_start))
            right = min(float(self.width()), self.x_for_time(band_end))
            if right > left:
                painter.fillRect(QRectF(left, 0, right - left, self.height()), KIAI_BAND_COLOR)

    def draw_ctrl_precision_line(self, painter) -> None:
        """Full-height line at the exact millisecond Ctrl-precision would use.

        Ctrl makes `snap_ms` skip the grid and just round to a whole
        millisecond -- at the 20ms zoom floor two adjacent milliseconds are
        several pixels apart, and without this the cursor gave no sign of
        which one a click would actually land on. Gated on `underMouse()`
        rather than on `_hover_time` merely being set: the hover time is
        last-known and goes stale the moment the cursor leaves, and a repaint
        triggered by something else entirely (playback, another view's edit)
        must not draw a line where the cursor no longer is.
        """
        if self._hover_time is None or not self.underMouse():
            return
        if not QApplication.keyboardModifiers() & Qt.ControlModifier:
            return
        x = self.x_for_time(round(self._hover_time))
        top, bottom = self.cursor_line_span()
        painter.setPen(QPen(CTRL_PRECISION_LINE_COLOR, 1))
        painter.drawLine(QPointF(x, top), QPointF(x, bottom))

    def cursor_line_span(self) -> tuple[float, float]:
        """(top, bottom) of the Ctrl-precision line: the whole view, except on
        a split layer, which keeps it to the row under the cursor."""
        return 0.0, float(self.height())

    def draw_cursor_position(self, painter) -> None:
        """The millisecond under the cursor, in the corner of the view.

        Millisecond-precision work needs the number, not just a line: at the
        20ms zoom floor you can see that two objects differ, and nothing on
        screen said by how much or where you were about to put the next one.

        Shows the millisecond a **click would use** rather than the raw cursor
        position -- that is the actionable number, and with Ctrl held (which
        skips the grid) the two are the same thing anyway.
        """
        if self._hover_time is None or not self.underMouse():
            return
        painter.setPen(QPen(CURSOR_READOUT_COLOR, 1))
        painter.drawText(
            QRectF(0, 2, self.width() - 6, 16),
            Qt.AlignRight | Qt.AlignTop,
            f"{int(self.snap_ms(self._hover_time))} ms",
        )

    def snap_ms(self, time_ms: float, divisor: int | None = None) -> float:
        """Snap `time_ms` to the grid and round it to a whole millisecond.

        osu! stores hit objects and timing points as integer milliseconds, but
        the snap grid is drawn at exact fractional beat positions -- at 185 BPM
        a 1/4 snap is 81.081081ms, so a note "on" that gridline is written up
        to a whole millisecond away from it. At the 20ms zoom floor that is
        roughly 60 pixels of visible drift between the object and its own
        gridline. Every in-view snapping call should go through this rather
        than `snap_time` directly.

        Down, via `osu_snap_ms`, and not to the nearest: that is what osu!
        itself does, and rounding here put every note whose beat position had a
        fraction of a half or more one millisecond above the one osu! writes
        for the same snap.

        Ctrl bypasses the grid entirely and just rounds: a mapper working on a
        gimmick wants to place objects one millisecond apart regardless of what
        the snap divisor says, and Ctrl is free to use for this because nothing
        else claims it as a mouse gesture -- only as a wheel modifier, in
        `wheelEvent` below.
        """
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            # Nearest, not down: this one is not a beat position, it is the
            # millisecond the pointer is over.
            return float(osu_round(time_ms))
        return max(0.0, float(osu_snap_ms(snap_time(
            self.snap_points, time_ms, divisor or self.snap_divisor))))

    # -- pixels <-> time ---------------------------------------------------

    def time_for_x(self, x: float) -> float:
        start_time = self.current_time - self.window_ms / 2
        return start_time + x / max(1, self.width()) * self.window_ms

    def x_for_time(self, time_ms: float) -> float:
        start_time = self.current_time - self.window_ms / 2
        return (time_ms - start_time) / self.window_ms * self.width()

    # -- wheel -------------------------------------------------------------

    def _change_snap_from_wheel(self, delta: int) -> None:
        if not delta:
            return
        try:
            current = SNAP_DIVISORS.index(self.snap_divisor)
        except ValueError:
            current = SNAP_DIVISORS.index(4)
        # Wheel up moves toward finer snaps; wheel down toward coarser snaps.
        direction = 1 if delta > 0 else -1
        new_divisor = SNAP_DIVISORS[max(0, min(len(SNAP_DIVISORS) - 1, current + direction))]
        if new_divisor != self.snap_divisor:
            self.snap_divisor = new_divisor
            self.snap_changed_by_wheel.emit(new_divisor)
            self.update()

    def wheelEvent(self, event) -> None:
        angle = event.angleDelta()
        pixel = event.pixelDelta()
        # Windows/Qt can turn Alt+vertical-wheel into a horizontal delta.
        delta = angle.y() or angle.x() or pixel.y() or pixel.x()
        if delta == 0:
            event.accept()
            return

        # The event's own modifiers combined with the global state: the global
        # state alone can be stale, and for synthetic events the event's own
        # modifiers may be the only place a key shows up.
        modifiers = event.modifiers() | QApplication.keyboardModifiers()

        if modifiers & Qt.AltModifier:
            self._change_snap_from_wheel(delta)
            event.accept()
            return

        if modifiers & Qt.ControlModifier:
            factor = self.ZOOM_IN_FACTOR if delta > 0 else self.ZOOM_OUT_FACTOR
            self.window_ms = max(
                self.MIN_WINDOW_MS, min(self.MAX_WINDOW_MS, self.window_ms * factor)
            )
            self.zoom_changed.emit(self.window_ms)
            self.update()
            event.accept()
            return

        threshold = 40.0 if angle.isNull() == 0 else 120.0
        self.wheel_accumulator += delta
        steps = int(self.wheel_accumulator / threshold)
        if steps:
            self.wheel_accumulator -= steps * threshold
            # A whole beat per notch while the song is running. Scrolling
            # during playback is looking around, not placing anything, and at a
            # fine snap a notch moves the view a few milliseconds -- so getting
            # anywhere means spinning the wheel while the music keeps going,
            # which is the opposite of following along. The snap divisor is
            # what you edit on, so it governs again the moment playback stops.
            # Shift is the same step, kept because it is the one that works
            # while paused.
            divisor = 1 if (modifiers & Qt.ShiftModifier or self.is_playing) \
                else self.snap_divisor
            # One movement per wheelEvent that crosses the threshold, not one
            # per multiple of it: a notched mouse can report a delta several
            # times a single "line" in one event (Windows' lines-per-notch
            # setting, a high-resolution wheel, ...), and looping abs(steps)
            # times turned one physical click into several snaps.
            self.current_time = wheel_seek_time(
                self.snap_points, self.current_time, -1 if steps > 0 else 1, divisor
            )
            # Direct connections run the slot inside emit, so this tells
            # MainWindow.seek_audio the seek came from the wheel.
            TimeAxisMixin.wheel_seek_in_progress = True
            try:
                self.seek_requested.emit(self.current_time)
            finally:
                TimeAxisMixin.wheel_seek_in_progress = False
            self.update()
        event.accept()

    # -- drag past the edge ------------------------------------------------

    def auto_scroll_step(self, mouse_x: float) -> bool:
        """Advance the view when a drag has run past its left or right edge.

        Returns whether it moved, so the caller can refresh whichever kind of
        selection it is dragging. Without this a selection can never be larger
        than one screenful: the view stands still however far the cursor goes.
        """
        if mouse_x < 0:
            overflow = mouse_x
        elif mouse_x > self.width():
            overflow = mouse_x - self.width()
        else:
            return False
        amount = min(4.0, abs(overflow) / 80.0)
        direction = -1 if overflow < 0 else 1
        self.current_time = max(0.0, self.current_time + direction * (8.0 + 22.0 * amount * amount))
        self.seek_requested.emit(self.current_time)
        return True

    # -- right-click scrub on the page's timing bar -------------------------

    # Set at construction (see MainWindow._add_editor_view and the gimmick/
    # Fancy Arranger view setup) to the page's TimingOverviewBar. None for a
    # bare view built without a page around it -- most tests -- so a press
    # never mistakes "no bar wired up" for "not over the bar".
    timing_bar = None

    # True from a right press caught on the timing bar (see
    # _press_is_on_timing_bar) until the right button releases. Reassigned
    # wholesale rather than mutated, so sharing this default across instances
    # via the class is safe the same way `timing_bar = None` above is.
    _bar_scrubbing = False

    def _press_is_on_timing_bar(self, global_pos) -> bool:
        """Whether a global press position lands on `self.timing_bar`.

        A right press during a drag never reaches the bar directly: the
        anchor view holds `grabMouse()` for the whole drag (see
        `TimelineGameplay.mousePressEvent`), so every event -- wherever the
        cursor actually is on screen -- routes to the view instead. This is
        how the view notices the press was meant for the bar underneath it.
        """
        bar = self.timing_bar
        return bar is not None and bar.rect().contains(bar.mapFromGlobal(global_pos))

    def _time_on_timing_bar(self, global_pos) -> float:
        """Time at a global position's x on `self.timing_bar`.

        Clamped by the bar's width only, not its height: once a scrub has
        started, drifting the cursor above or below the bar (28px tall) must
        not abort it, the same way dragging past a slider's track still
        tracks the slider.
        """
        bar = self.timing_bar
        fraction = max(0.0, min(1.0, bar.mapFromGlobal(global_pos).x() / max(1, bar.width())))
        return fraction * bar.duration_ms
