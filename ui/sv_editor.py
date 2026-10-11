"""The SV editor: green lines as a speed graph, with the bounds and steps its drag and labels use."""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import QWidget
from ui import theme
from osu_io.timing import TimingPoint, sorted_by_time, uninherited_points
from chart.time_axis import TimeAxisMixin
from ui.common import view_ground, view_ground_is_opaque
from ui.chart_views import (
    SV_LABEL_MIN_SPACING_PX,
    TimelineGameplay,
    active_timing,
    value_label_font,
)


# Range for mapping SV multiplier to vertical position in the SV graph;
# log-scaled since SV is inherently multiplicative (0.5x and 2x are equally
# "far" from 1x). Doesn't affect stored values, only where the line is drawn.
# The floor is fixed: anything slower than SV_VISUAL_MIN is simply drawn along
# the bottom of the graph rather than dragging the whole axis down with it.
# Only the ceiling autoscales, and only upward past SV_VISUAL_MAX -- see
# SVEditorView.update_scale.
SV_VISUAL_MIN = 0.1


SV_VISUAL_MAX = 2.5


# Absolute limits on the axis, whatever the map contains.
SV_SCALE_FLOOR = 0.01


SV_SCALE_CEILING = 100.0


# The axis only ever sits on "round" values -- 0.5x, 1.5x, 2.0x -- rather than
# whatever min/max the visible points happen to have. An axis labelled 0.43x to
# 3.17x is arithmetically correct and useless to read against.
SV_BOUND_STEPS = ((0.5, 0.5), (0.1, 0.1), (0.0, 0.01))


# The smallest step a vertical drag commits to. Typing still reaches SV_DECIMALS
# -- this is only what a mouse-move quantizes *to* before it is even sent
# onward. At full float precision a drag emits a new value on essentially
# every pixel, so _edit_sv_point's full refresh (history push, hitsound
# schedule rebuild, every open view) ran once per pixel too -- measured at
# ~17ms a call on a 3000-point gimmick chart (tools/measure_sv_drag.py),
# against a mouse that can deliver a move every few ms. Rounding to this step
# and skipping a call whose value did not actually change (see
# SVEditorView.mouseMoveEvent) is what lets most of those moves cost nothing.
SV_DRAG_STEP = 0.01


# A click on a green line within this many pixels of its actual value dot
# adjusts the SV (vertical); farther away on the same line -- the vertical
# guide line is drawn full-height, so a click anywhere along it lands "on" the
# line -- retimes it (horizontal) instead. Smaller than the line's own hit
# radius, so the dot is a precise target inside a more forgiving line.
SV_DOT_HIT_RADIUS_PX = 10.0


def _sv_bound_step(value: float) -> float:
    """Ladder spacing at `value`: coarse for normal SV, finer near zero."""
    for threshold, step in SV_BOUND_STEPS:
        if value >= threshold:
            return step
    return SV_BOUND_STEPS[-1][1]


def sv_bound_below(value: float) -> float:
    """Largest ladder value strictly below `value`.

    Strictly below, so the line is never drawn along the very edge of the
    graph. Falls to a finer step when the coarse one would land on (or under)
    zero, which a logarithmic axis cannot represent.
    """
    for _threshold, step in SV_BOUND_STEPS:
        candidate = (math.ceil(value / step - 1e-9) - 1) * step
        if candidate >= SV_SCALE_FLOOR:
            return min(candidate, SV_SCALE_CEILING)
    return SV_SCALE_FLOOR


def sv_bound_above(value: float) -> float:
    """Smallest ladder value strictly above `value`."""
    step = _sv_bound_step(value)
    candidate = (math.floor(value / step + 1e-9) + 1) * step
    return max(SV_SCALE_FLOOR * 2, min(candidate, SV_SCALE_CEILING))


class SVEditorView(TimeAxisMixin, QWidget):
    """M4: view and edit [TimingPoints]' inherited (SV) points on a time axis.

    Vertical-line colour follows the same rule as the timing overview bar,
    resolved per timestamp rather than per point: **red** where only an
    uninherited (BPM) point sits, **green** where only inherited (SV) points
    sit, **yellow** where both share the millisecond. Resolving per point
    instead drew the two lines on top of each other and whichever came last
    in file order won, which is why a BPM point with SV stacked on it used to
    render as a plain green line.

    A line connects the effective SV over time; dragging a point in select
    *or* green-line mode moves it, either vertically (SV) or horizontally
    (retime), decided by how close the click landed to the point's own value
    dot -- see _drag_axis_for_click. Green-line mode also shows a snapped
    ghost line under the cursor before placing, mirroring the chart view's
    placement ghost. Holds no MainWindow reference -- like TimelineGameplay,
    it emits requests and the owner applies them.
    """

    seek_requested = Signal(float)
    snap_changed_by_wheel = Signal(int)
    # (start_ms, end_ms) -- drag-select in function mode.
    function_range_requested = Signal(float, float)
    # (snapped_time_ms, sv) -- click in green-line mode.
    point_add_requested = Signal(float, float)
    # (uid, new_time_ms) -- horizontal drag of a line (green or red) in select
    # mode, snapped to the grid. Retiming only, no SV change.
    point_time_edit_requested = Signal(int, float)
    # (uid, new_sv) -- vertical drag of a green line's own value dot. An
    # uninherited point has no SV, so this never fires for one. Kept as its
    # own signal (rather than folded into point_time_edit_requested) because
    # _drag_axis_for_click makes a drag either-or, never both, so each drag
    # only ever emits one of the two -- letting a continuous drag merge into a
    # single undo step instead of interleaving two command shapes that can't
    # merge with each other.
    point_sv_edit_requested = Signal(int, float)
    # (uid, volume 0-100). The Kiai and Sound Volume layer's own edit: its
    # vertical axis is hitsound volume, not SV, so a drag there writes this.
    point_volume_edit_requested = Signal(int, int)
    # Double click in that layer: type the volume, with no time field.
    point_volume_dialog_requested = Signal(int)
    # uid -- right click near an inherited point.
    point_delete_requested = Signal(int)
    # uid -- double click on a line of either kind. Same signal and same dialog
    # as TimelineGameplay's: a green line's SV and a red line's BPM are the same
    # edit seen from two views, and both can be retimed there.
    timing_line_edit_requested = Signal(int)
    # list[uid] -- Delete/Backspace with green lines selected. One signal for
    # the whole selection so the owner can push a single undo step.
    points_delete_requested = Signal(object)
    # New window_ms after a Ctrl+wheel zoom -- see TimelineGameplay.zoom_changed.
    zoom_changed = Signal(float)

    # Twice the chart views' limit: an SV sweep is read across a whole section,
    # a note pattern is not. This was already true before the shared axis was
    # extracted -- it is a deliberate difference, not drift.
    MAX_WINDOW_MS = 120000.0

    # See TimelineGameplay.DESIGN_HEIGHT: the height the graph's margins were
    # measured at. MIN_HEIGHT is this view's floor outside the gimmick page,
    # and is below it, so a short view scales its margins down rather than
    # keeping 56 designed pixels of them in a 120px band.
    DESIGN_HEIGHT = TimelineGameplay.DESIGN_HEIGHT
    MIN_HEIGHT = TimelineGameplay.MIN_HEIGHT

    def __init__(self) -> None:
        super().__init__()

        self.timing_points: list[TimingPoint] = []
        self.current_time = 0.0
        self.window_ms = 4000.0
        self.snap_divisor = 4
        # "select" mirrors TimelineGameplay's default; changed only by this
        # view's own tool row entry (global, per the Editor page's tool
        # rows), never anything else.
        self.tool = "select"
        # Set true only for the Kiai and Sound Volume layer (MainWindow.
        # _add_editor_view). Switches the vertical axis, the connected curve
        # and vertical drags from SV to a plain 0-100 volume percentage; the
        # red/green/yellow line placement itself is unchanged.
        self.volume_mode = False

        self.drag_start_x: float | None = None
        self.drag_anchor_time: float | None = None
        self.drag_mouse_x = 0.0
        self.wheel_accumulator = 0.0
        self.last_rendered_time = -1.0
        self._drag_point: TimingPoint | None = None
        self._drag_axis: str = "time"  # "time" or "value", set by _begin_point_drag
        self._last_drag_value: float | None = None
        # Paint caches, rebuilt per edit rather than per frame -- see
        # _rebuild_caches for why that distinction is load-bearing.
        # Milliseconds this view shows SV for, or None to show every point.
        # The gimmick page's three SV layers each own one structure's SV --
        # see MainWindow._sv_layer_times -- and set this to the timestamps of
        # the objects they belong to. Only inherited points survive the filter,
        # which is also why those layers draw green where the plain SV editor
        # draws yellow: the red line sharing the millisecond is not in the view.
        #
        # `point_times_for` derives it from the document on every refresh, which
        # is what keeps it honest: an edit anywhere can add the very object a
        # layer is keyed to, and refreshes arrive from several paths.
        self.point_times: set[int] | None = None
        self.point_times_for = None
        self._visible_points: list[TimingPoint] = []
        # Write the BPM beside each red line. Off in the barline SV layer: the
        # number belongs next to the lines themselves, which is layer 3, and a
        # barline gimmick puts thousands of them in one window.
        self.show_bpm_labels = True
        self._series: list[tuple[float, float]] = []
        self._series_times: list[float] = []
        # Effective volume over time, used only when volume_mode is True. Kept
        # separate from _series/_series_times rather than repurposing them:
        # SV resolution resets to 1.0x at an uninherited point, volume does
        # not (every point, red or green, carries its own .volume), so the
        # two curves are built by different rules from the same points.
        self._volume_series: list[tuple[float, float]] = []
        self._volume_series_times: list[float] = []
        self._line_kinds: dict[float, str] = {}
        self._line_kind_times: list[float] = []
        self._bpm_at: dict[float, float] = {}
        self._beat_points: list[TimingPoint] = []
        # What the caches above were last built from -- see set_timing_points.
        self._cache_key = None
        self._cache_len = -1
        # Vertical scale, refitted to the visible SV on every paint. Starts at
        # the historical fixed window so an empty view still looks sensible.
        self.autoscale = True
        self.scale_min = SV_VISUAL_MIN
        self.scale_max = SV_VISUAL_MAX
        # Rubber-band selection over a time range (select tool, empty space).
        # Kept separate from drag_anchor_time, which is function mode's range.
        self.select_anchor_time: float | None = None
        self.select_mouse_x = 0.0
        self.auto_scroll_timer = QTimer(self)
        self.auto_scroll_timer.setInterval(16)
        self.auto_scroll_timer.timeout.connect(self._auto_scroll_selection)
        # uids of inherited points picked in select mode, so Delete/Backspace
        # and Ctrl+C have something to act on.
        self.selected_uids: set[int] = set()

        # Ghost preview of the green line a click would place: needs mouse
        # tracking, since plain mouseMoveEvent only fires with a button held.
        self.setMouseTracking(True)
        self._hover_time: float | None = None
        self._hover_sv: float | None = None

        self.red_pen = QPen(QColor(255, 90, 90, 230), 2)
        self.green_pen = QPen(QColor(90, 225, 130, 230), 2)
        # Both kinds share this millisecond -- same convention the timing
        # overview bar already uses for its markers.
        self.yellow_pen = QPen(QColor(255, 212, 0, 235), 2)
        self.selected_pen = QPen(QColor(255, 255, 255, 240), 3)
        self.ghost_pen = QPen(QColor(140, 255, 185, 130), 2, Qt.DashLine)
        self.graph_pen = QPen(QColor(140, 255, 185, 210), 2)
        self.range_brush = QColor(190, 195, 205, 40)
        self.label_pen = QPen(QColor(225, 230, 240, 235), 1)
        # The numbers on the graph, as the approved mockup draws them: the app
        # font, bold, and the SV or volume values in the theme's accent so they
        # read apart from the line labels. Kept off the painter's own font,
        # which the shared cursor readout also uses.
        self.label_font = value_label_font(self.font())
        self.value_pen = QPen(theme.color("#ff9dcc"), 1)
        self.cursor_pen = QPen(QColor("#ffffff"), 2)

        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setFocusPolicy(Qt.StrongFocus)
        # paintEvent opens with an opaque fill of the whole rect, so nothing
        # behind this widget is ever seen. Without the attribute Qt did not
        # know that, and on every frame repainted what sits underneath --
        # the EditorViewFrame's stylesheet (a rounded, bordered rect drawn
        # through QStyleSheetStyle) and the scroll viewport -- only to cover
        # it again. Measured: over half of a six-chart Editor frame was Qt's
        # own work in processEvents, not these paintEvents. If a paintEvent
        # here ever stops filling its whole rect, this has to go with it, or
        # stale pixels show through -- which is why it follows View opacity
        # (`set_view_opacity`): below 100% the fill lets the backdrop through.
        self.setAttribute(Qt.WA_OpaquePaintEvent, view_ground_is_opaque())

    # -- data ----------------------------------------------------------------

    def load_document(self, document) -> None:
        self.refresh_points(document)
        self.current_time = float(self.timing_points[0].time if self.timing_points else 0)
        self.update()

    def refresh_points(self, document) -> None:
        """Re-read timing points after an edit, without resetting the cursor."""
        if self.point_times_for is not None:
            self.point_times = self.point_times_for(document)
        self.set_timing_points(document.timing_points)
        # Points deleted elsewhere must not stay "selected" forever.
        live = {point.uid for point in self.timing_points}
        self.selected_uids &= live
        self.update()

    def set_timing_points(self, points) -> None:
        """Adopt a point list (any order) and rebuild the paint caches."""
        self.timing_points = (
            self.sorted_points_for(points) if self.sorted_points_for is not None
            else sorted_by_time(points)
        )
        # Every edit refreshes every gimmick layer, and placing a note changes no
        # timing point at all -- yet each SV layer rebuilt its caches from the
        # whole list: 71ms of a 192ms placement on a 21k-point gimmick map.
        # Skip the rebuild when nothing it reads has changed. The key is the
        # points' own fields rather than an edit counter, because TimingPoint
        # is mutable and edited in place (set_kiai, set_sv, drags), and a
        # counter some path forgot to bump would leave a layer silently stale.
        # ~2.5ms to build at that size, against ~18ms per layer skipped.
        #
        # A changed point count cannot match, so it rebuilds without paying for
        # the key: building it anyway made every fake slider placement -- which
        # always inserts points -- 23ms slower than no skip at all. The key is
        # then built on the next same-length refresh, costing one rebuild there.
        if len(self.timing_points) != self._cache_len:
            self._cache_len = len(self.timing_points)
            self._cache_key = None
        else:
            key = (
                [
                    (p.uid, p.time, p.beat_length, p.uninherited_flag, p.volume, p.effects, p.meter)
                    for p in self.timing_points
                ],
                None if self.point_times is None else frozenset(self.point_times),
                self.volume_mode, self.show_bpm_labels, self.autoscale,
            )
            if key == self._cache_key:
                return
            self._cache_key = key
        if self.kiai_bands_for is not None:
            self.kiai_bands = self.kiai_bands_for(self.timing_points)
        else:
            self.set_kiai_from(self.timing_points)
        self._rebuild_caches()

    def _rebuild_caches(self) -> None:
        """Per-timestamp series and line colours, computed once per edit.

        These used to be rebuilt from every point on every paint -- three full
        passes per frame, per open view, at the playback frame rate. On an
        ordinary map that is invisible; on a gimmick map carrying tens of
        thousands of timing points it is the difference between scrolling and
        a frozen window.
        """
        self._visible_points = (
            self.timing_points if self.point_times is None
            else [
                point for point in self.timing_points
                if not point.uninherited and round(point.time) in self.point_times
            ]
        )
        # Only one of these two series is ever read -- `_draw_sv_curve` picks
        # `_volume_series` in volume_mode and `_series` otherwise, and
        # `volume_mode` is set once at construction and never toggled, so the
        # other one is dead weight on every refresh. Same reasoning as
        # `_bpm_at` below: built only where it is read. `sv_series()` (test
        # only) returns `[]` on a volume_mode view as a result -- it was never
        # meaningful there anyway.
        kinds: dict[float, set[bool]] = {}
        if self.volume_mode:
            self._series, self._series_times = [], []
            # Effective volume: unlike SV, every point (red or green) carries
            # its own value and none of them reset it, so this is a plain
            # last-in-file-order-wins fold over the same points, iterated in
            # `self.timing_points`' sorted (stable) order.
            by_time_volume: dict[float, float] = {}
            for point in self._visible_points:
                kinds.setdefault(point.time, set()).add(point.uninherited)
                by_time_volume[point.time] = float(point.volume)
            self._volume_series = sorted(by_time_volume.items())
            self._volume_series_times = [time_ms for time_ms, _volume in self._volume_series]
        else:
            self._volume_series, self._volume_series_times = [], []
            by_time: dict[float, float] = {}
            for point in self._visible_points:
                kinds.setdefault(point.time, set()).add(point.uninherited)
                if point.uninherited and point.time in by_time:
                    # An inherited point already claimed this millisecond; a red
                    # line stacked with a green one does not reset it back to 1.0x.
                    continue
                by_time[point.time] = point.sv_multiplier
            self._series = sorted(by_time.items())
            self._series_times = [time_ms for time_ms, _sv in self._series]
        # Keyed by the point's exact time, not by a rounded millisecond: this
        # is where the line is *drawn*, and a point at 4845.4 rounded to 4845
        # drew half a millisecond away from the note it belongs to -- invisible
        # at chart zoom, a third of the view at the 20ms floor, which is where
        # gimmick work happens. Rounding also merged two points half a
        # millisecond apart into one line.
        self._line_kinds = {
            time_ms: "yellow" if len(flags) > 1 else "red" if True in flags else "green"
            for time_ms, flags in kinds.items()
        }
        self._line_kind_times = sorted(self._line_kinds)
        # Only built where it is read, and reading `.bpm` once per point
        # rather than twice: it is a division behind a property, this runs for
        # every point in every SV view on every refresh, and a gimmick map
        # carries tens of thousands of them. The label pass is the only
        # consumer, so a view that does not draw labels does not pay for it.
        self._bpm_at = {}
        if self.show_bpm_labels:
            for point in self.timing_points:
                if not point.uninherited:
                    continue
                bpm = point.bpm
                if bpm:
                    self._bpm_at[point.time] = bpm
        # The red lines on their own. active_uninherited_at steps back over
        # inherited points one at a time, and this view's whole subject matter
        # is maps with thousands of them between two red lines -- asking it
        # against the full list, once per curve segment per frame, was the SV
        # editor's entire paint cost.
        self._beat_points = (
            self.uninherited_points_for(self.timing_points) if self.uninherited_points_for is not None
            else uninherited_points(self.timing_points)
        )
        # Their times alone, so _draw_sv_curve can tell when its walk leaves
        # the current section with one C-level bisect instead of asking
        # active_timing again for every segment.
        self._beat_times = [point.time for point in self._beat_points]
        if self.volume_mode:
            # Volume is a plain 0-100% scale, never the log SV ladder -- set
            # here too (not only in update_scale) so a mouse handler that
            # reads scale_min/max between paints never sees a stale SV range.
            self.scale_min, self.scale_max = 0.0, 100.0
        elif self.autoscale:
            # The ceiling follows the whole document, not the visible window: a
            # bound refitted per frame moved every time a fast point crossed
            # the view edge, which read as the graph flickering while scrolling.
            highest = max((sv for _time_ms, sv in self._series), default=1.0)
            self.scale_max = max(SV_VISUAL_MAX, sv_bound_above(highest))

    def set_snap_divisor(self, divisor: int) -> None:
        self.snap_divisor = divisor
        self.update()

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self.update()  # the green line ghost; see TimelineGameplay.set_tool

    def line_kinds(self) -> dict[float, str]:
        """{millisecond: "red" | "green" | "yellow"} for the vertical lines, cached per edit.

        One entry per *timestamp*, not per point. Painting one line per point
        meant an uninherited and an inherited point sharing a millisecond drew
        over each other and file order picked the winner, so a BPM line with
        SV stacked on it rendered green -- indistinguishable from a plain SV
        point. Same convention as TimingOverviewBar's markers: yellow means
        both kinds are here.
        """
        return self._line_kinds

    def selected_points(self) -> list[TimingPoint]:
        """Selected inherited points in time order, for copy and for Delete."""
        return sorted(
            (point for point in self.timing_points if point.uid in self.selected_uids),
            key=lambda point: (point.time, point.uid),
        )

    # -- SV <-> vertical position ----------------------------------------------

    def _margin_scale(self) -> float:
        """How much of the designed margins this view's height can afford.

        The margins hold the axis labels and the BPM text, and at DESIGN_HEIGHT
        they cost 56 of 180 pixels. Kept at that on a gimmick band -- a third of
        its height -- they left the curve confined to the middle of the view.
        """
        return min(1.0, self.height() / self.DESIGN_HEIGHT)

    def _graph_top(self) -> float:
        return 20.0 * self._margin_scale()

    def _graph_bottom(self) -> float:
        return self.height() - 36.0 * self._margin_scale()

    def _sv_to_y(self, sv: float, top: float, bottom: float) -> float:
        low, high = self.scale_min, self.scale_max
        clamped = max(low, min(high, sv))
        if self.volume_mode:
            # Linear 0-100%, not log SV -- math.log(0) would crash here, and a
            # log scale has no meaning for a volume percentage anyway.
            ratio = (clamped - low) / (high - low)
        else:
            ratio = (math.log(clamped) - math.log(low)) / (math.log(high) - math.log(low))
        return bottom - ratio * (bottom - top)

    def _y_to_sv(self, y: float, top: float, bottom: float) -> float:
        """The SV under `y`, which is **not** bounded by the drawn axis.

        SV_VISUAL_MIN/MAX are a *drawing* bound -- the floor is fixed so one
        0.05x stop does not squash a whole map into the top of the graph. The
        values a green line may hold are SV_SCALE_FLOOR..SV_SCALE_CEILING.
        Clamping to the graph made the bottom pixel 0.1x, so everything down
        to 0.01x was unreachable by dragging, and mirrored it at the top: the
        ceiling autoscales to the map's own maximum, so no drag could ever
        make a point faster than the fastest one already there. Drag past
        either edge and the log scale carries on to the real limit.

        Volume is the exception: 0-100% is the whole range, not a window on
        one, so there is nothing outside it to reach.
        """
        low, high = self.scale_min, self.scale_max
        ratio = (bottom - y) / max(1.0, bottom - top)
        if self.volume_mode:
            return low + max(0.0, min(1.0, ratio)) * (high - low)
        # Clamped in log space: a cursor thrown far off the widget would
        # otherwise overflow math.exp before anything got to bound it.
        return math.exp(min(math.log(SV_SCALE_CEILING), max(
            math.log(SV_SCALE_FLOOR),
            math.log(low) + ratio * (math.log(high) - math.log(low)))))

    def update_scale(self) -> tuple[float, float]:
        """The current axis. Both bounds are decided per *document*, not per frame.

        * The floor is fixed at SV_VISUAL_MIN. Slow sections are read against
          the same baseline everywhere, and one 0.05x stop no longer squashes a
          whole map's worth of ordinary SV into the top of the graph -- it just
          sits on the ground (_sv_to_y clamps).
        * The ceiling is SV_VISUAL_MAX unless the map exceeds it, in which case
          it rises to the enclosing round value (`sv_bound_above`), set in
          `_rebuild_caches`. Fitting it to the *visible window* instead meant it
          moved every time a fast point crossed the view edge -- the graph
          visibly flickering while scrolling, and the sticky-bound machinery
          that used to damp that was only ever treating the symptom.

        Kept as a method (rather than reading the attribute) because
        `autoscale = False` still has to freeze whatever a caller set by hand.

        In volume_mode the whole SV_BOUND ladder is bypassed: the axis is
        always a fixed 0%-100%, because that is what a volume percentage is.
        """
        if self.volume_mode:
            self.scale_min, self.scale_max = 0.0, 100.0
            return self.scale_min, self.scale_max
        return self.scale_min, self.scale_max

    def sv_series(self) -> list[tuple[float, float]]:
        """[(time, effective SV)], one entry per timestamp, cached per edit.

        A red (uninherited) line resets SV to 1.0x, but only when nothing else
        sits on that millisecond: mappers routinely stack a green line on a red
        one precisely to set the speed the new BPM section starts at, and osu!
        gives the later line in file order the final say. Emitting one entry
        per *point* meant the red line contributed a 1.0x sample the green line
        immediately overwrote, which showed up as a spike down to 1.0x at every
        BPM change that had SV on it.
        """
        return self._series

    def _nearest_inherited(self, x: float, radius_px: float = 20.0) -> TimingPoint | None:
        # Hit-testing follows what is drawn, or a filtered layer would grab and
        # edit a point it never showed.
        #
        # `<=` on the tie, not `<`: two green lines can share one exact
        # millisecond -- a converter's own SV stacked on the chart's, kept
        # rather than deduped so the layer has no gaps -- and `_visible_points`
        # is stable-sorted from insertion order, so the last of an exact tie is
        # the one osu! actually reads. Picking the first instead grabbed and
        # showed the superseded line while the effective one sat under it.
        best = None
        best_distance = None
        for point in self._visible_points:
            if point.uninherited:
                continue
            distance = abs(self.x_for_time(point.time) - x)
            if distance <= radius_px and (best_distance is None or distance <= best_distance):
                best = point
                best_distance = distance
        return best

    def _nearest_point(self, x: float, radius_px: float = 20.0) -> TimingPoint | None:
        """Nearest point of either kind, for select-mode drag-to-retime.

        `<=` on the tie: see `_nearest_inherited` on why the last of an
        exact-millisecond tie is the point actually in force.
        """
        best = None
        best_distance = None
        for point in self._visible_points:
            distance = abs(self.x_for_time(point.time) - x)
            if distance <= radius_px and (best_distance is None or distance <= best_distance):
                best = point
                best_distance = distance
        return best

    def _placement_time(self, x: float) -> float:
        """Where a green line placed at `x` would go.

        In a layer that owns a fixed set of milliseconds (`point_times`), the
        nearest of those rather than the nearest snap division. A green line
        anywhere else in such a layer is invisible in it -- `_rebuild_caches`
        filters on exactly this set -- and gets reset to 1.0x by the next
        uninherited line anyway, which is why placement used to look like it
        did nothing at all.
        """
        if self.point_times:
            return float(min(self.point_times, key=lambda t: abs(self.x_for_time(t) - x)))
        return self.snap_ms(self.time_for_x(x))

    def _dot_position(self, point: TimingPoint) -> QPointF:
        """Screen position of a green point's value dot, for the click-radius
        test that decides whether a drag adjusts SV or retimes the line."""
        return QPointF(
            self.x_for_time(point.time),
            self._sv_to_y(point.sv_multiplier, self._graph_top(), self._graph_bottom()),
        )

    def _drag_axis_for_click(self, point: TimingPoint, pos: QPointF) -> str:
        """"value" (adjust SV) if the click landed on the point's dot, else
        "time" (retime). An uninherited point has no SV, so it is always
        "time" -- see SV_DOT_HIT_RADIUS_PX for the threshold.

        **Always "value" in volume_mode**, dot or no dot. That layer's vertical
        axis is hitsound volume, and its lines belong to the milliseconds they
        sit on -- a barline, a note, a kiai edge. Retiming one there does not
        move a value, it moves the section boundary out from under whatever put
        it there, so the layer offers no horizontal drag at all.
        """
        if self.volume_mode:
            return "value"
        if point.uninherited:
            return "time"
        dot = self._dot_position(point)
        distance = math.hypot(pos.x() - dot.x(), pos.y() - dot.y())
        return "value" if distance <= SV_DOT_HIT_RADIUS_PX else "time"

    # -- mouse -----------------------------------------------------------------

    def mouseDoubleClickEvent(self, event) -> None:
        """Double click any line to retype its SV (or BPM) and its time.

        Reaches red lines too: this view draws both kinds, and a BPM line
        double-clicked here is the same edit the chart layers offer.
        """
        if event.button() != Qt.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        point = self._nearest_point(event.position().x())
        if point is None:
            super().mouseDoubleClickEvent(event)
            return
        if self.volume_mode:
            # Same rule as the drag above: this layer types a volume, and the
            # generic line dialog would have offered a time field that must not
            # be there.
            self.point_volume_dialog_requested.emit(point.uid)
        else:
            self.timing_line_edit_requested.emit(point.uid)
        event.accept()

    def _begin_point_drag(self, point: TimingPoint, axis: str, press_y: float = 0.0) -> None:
        self._drag_point = point
        self._drag_axis = axis
        # A value drag is relative to where it was pressed -- see
        # SV_DRAG_STEP. Held here because the point is replaced on every edit.
        self._drag_press_y = press_y
        self._drag_start_sv = point.sv_multiplier
        # See mouseMoveEvent: what the last emitted value-drag step was, so a
        # fresh drag does not inherit the previous one's and skip its own
        # first, genuinely-new step. The SV it starts at counts as sent, so a
        # press that never moves emits nothing.
        self._last_drag_value = None if self.volume_mode else self._drag_start_sv
        self.selected_uids = {point.uid}
        self.grabMouse()
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            # A drag-selection (function-range or select-mode rubber-band) is
            # live and this press landed on the page's timing bar rather than
            # in the view: scrub the playhead there instead of deleting a
            # point, the same reasoning as TimelineGameplay.mousePressEvent.
            if (self.drag_anchor_time is not None or self.select_anchor_time is not None) and (
                self._press_is_on_timing_bar(event.globalPosition().toPoint())
            ):
                self._bar_scrubbing = True
                self._scrub_bar_to(event.globalPosition().toPoint())
                event.accept()
                return
            point = self._nearest_inherited(event.position().x())
            if point is not None:
                self.point_delete_requested.emit(point.uid)
            event.accept()
            return
        if event.button() != Qt.LeftButton:
            return

        if self.tool in ("function", "kiai", "volume"):
            # Same gesture for all three: drag a range, then the owner decides
            # what to do with it. Kiai and Volume are the Kiai and Sound Volume
            # layer's tools -- see MainWindow._sv_range_action.
            self.drag_start_x = event.position().x()
            self.drag_mouse_x = self.drag_start_x
            self.drag_anchor_time = self.time_for_x(self.drag_start_x)
            self.grabMouse()
            self.update()
            event.accept()
            return

        if self.tool == "green_line":
            # Landing on an existing green line adjusts it (drag its SV) the
            # same way select mode does, instead of stacking a second point
            # on top of it -- one inherited point per millisecond is the rule
            # the owner enforces anyway, so placing here could only ever have
            # replaced what was already there.
            existing = self._nearest_inherited(event.position().x())
            if existing is not None:
                self._begin_point_drag(existing, self._drag_axis_for_click(existing, event.position()),
                                       event.position().y())
                event.accept()
                return
            time_ms = self._placement_time(event.position().x())
            # Quantized like a drag (SV_DRAG_STEP): a placed line otherwise
            # landed at whatever float the pixel mapped to -- 1.2371... -- and
            # the ghost promised a rounder value than the click delivered.
            sv = round(self._y_to_sv(event.position().y(), self._graph_top(), self._graph_bottom())
                       / SV_DRAG_STEP) * SV_DRAG_STEP
            self.point_add_requested.emit(time_ms, sv)
            event.accept()
            return

        # select: click a line (green or red) to select it and drag it. A
        # click on a green line's own value dot adjusts its SV (vertical);
        # anywhere else along the line retimes it (horizontal) instead -- see
        # _drag_axis_for_click. On empty space, start a rubber-band selection
        # over a time range instead -- and clear what was selected, so a
        # plain click deselects.
        point = self._nearest_point(event.position().x())
        if point is not None:
            self._begin_point_drag(point, self._drag_axis_for_click(point, event.position()),
                                   event.position().y())
            event.accept()
            return
        self.selected_uids.clear()
        self.select_anchor_time = self.time_for_x(event.position().x())
        self.select_mouse_x = event.position().x()
        self.grabMouse()
        self.auto_scroll_timer.start()
        self.update()
        event.accept()

    def _update_range_selection(self) -> None:
        if self.select_anchor_time is None:
            return
        a, b = sorted((self.select_anchor_time, self.time_for_x(self.select_mouse_x)))
        self.selected_uids = {
            point.uid
            for point in self._visible_points
            if not point.uninherited and a <= point.time <= b
        }
        self.update()

    def _scrub_bar_to(self, global_pos) -> None:
        """Move the playhead to the timing bar under `global_pos`, and grow
        whichever range drag is currently live to follow it.

        The anchor stays pinned in time -- only `current_time` changes, and
        the view scrolls under it -- so the far edge (`self.time_for_x` of the
        moving side) always reads as the scrub target once the view re-centres
        on it. Only one of `drag_anchor_time` / `select_anchor_time` is ever
        set at a time (function-range mode vs. select mode), so exactly one
        branch here has anything to update.
        """
        self.current_time = self._time_on_timing_bar(global_pos)
        self.seek_requested.emit(self.current_time)
        if self.drag_anchor_time is not None:
            self.drag_mouse_x = self.x_for_time(self.current_time)
            self.update()
        elif self.select_anchor_time is not None:
            self.select_mouse_x = self.x_for_time(self.current_time)
            self._update_range_selection()

    def _auto_scroll_selection(self) -> None:
        """Scroll the view when a rubber-band drag runs past its edges.

        Same acceleration curve as TimelineGameplay's: without it, a selection
        can never be larger than one screenful, since the view stands still
        however far the cursor goes.
        """
        if self.select_anchor_time is None:
            self.auto_scroll_timer.stop()
            return
        if self.auto_scroll_step(self.select_mouse_x):
            self._update_range_selection()

    def mouseMoveEvent(self, event) -> None:
        if self._bar_scrubbing:
            self._scrub_bar_to(event.globalPosition().toPoint())
            return
        if self.drag_anchor_time is not None:
            self.drag_mouse_x = event.position().x()
            self.update()
            return
        if self.select_anchor_time is not None:
            self.select_mouse_x = event.position().x()
            self._update_range_selection()
            return
        if self._drag_point is not None:
            if self._drag_axis == "value":
                if self.volume_mode:
                    # _y_to_sv is linear 0-100 here (see update_scale), so the
                    # number already is a volume percentage -- already coarse
                    # enough (100 steps) that the SV_DRAG_STEP problem below
                    # barely reaches it, but the same skip is free to apply.
                    value = round(self._y_to_sv(
                        event.position().y(), self._graph_top(), self._graph_bottom()))
                else:
                    # One SV_DRAG_STEP per pixel from the press, not the log
                    # axis under the cursor: a pixel of that axis is 0.03x at
                    # 1.0x and more above it, so an absolute drag skipped most
                    # of the steps it was rounded to.
                    moved = round(self._drag_press_y - event.position().y())
                    # Unmoved, the typed 8-decimal value stays as it was
                    # rather than being snapped to 0.01 by a jittery click.
                    value = self._drag_start_sv if moved == 0 else round(max(
                        SV_SCALE_FLOOR, min(SV_SCALE_CEILING, (round(
                            self._drag_start_sv / SV_DRAG_STEP) + moved) * SV_DRAG_STEP)), 2)
                # A mouse delivers far more move events than this many steps
                # actually exist across a typical drag range -- most of them
                # would otherwise ask for the exact value already in force.
                # See SV_DRAG_STEP for what skipping that costs elsewhere.
                if value != self._last_drag_value:
                    self._last_drag_value = value
                    if self.volume_mode:
                        self.point_volume_edit_requested.emit(self._drag_point.uid, int(value))
                    else:
                        self.point_sv_edit_requested.emit(self._drag_point.uid, value)
            else:
                # Same rule as placement: in a layer keyed to its own objects,
                # a line dragged between them would vanish from the layer that
                # was dragging it.
                self.point_time_edit_requested.emit(
                    self._drag_point.uid, self._placement_time(event.position().x())
                )
            self.update()
            return
        # Ghost preview: only green-line mode has something to preview, but
        # the cursor millisecond is drawn in every mode -- see
        # TimelineGameplay.mouseMoveEvent for why the repaint is unconditional.
        self._hover_time = self.time_for_x(event.position().x())
        self._hover_sv = round(self._y_to_sv(event.position().y(), self._graph_top(), self._graph_bottom())
                               / SV_DRAG_STEP) * SV_DRAG_STEP
        self.update()

    def leaveEvent(self, event) -> None:
        """See TimelineGameplay.leaveEvent."""
        self._hover_time = None
        self._hover_sv = None
        super().leaveEvent(event)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            # A right release only ever matters here as the end of a
            # bar-scrub (see mousePressEvent above) -- it must never fall
            # through to the generic release logic below, which would end
            # whatever drag a left button is still holding open.
            self._bar_scrubbing = False
            event.accept()
            return
        if self.drag_anchor_time is not None:
            self.releaseMouse()
            a, b = sorted((self.drag_anchor_time, self.time_for_x(self.drag_mouse_x)))
            if self.tool == "kiai":
                # A kiai section starts and ends on the grid the mapper is
                # looking at, not on whatever fraction of a millisecond the
                # cursor happened to be over -- and `snap_ms` is also where
                # Ctrl means "whole milliseconds instead", which is the
                # override this tool needs for sections that dodge a line.
                a, b = self.snap_ms(a), self.snap_ms(b)
            self.drag_start_x = None
            self.drag_anchor_time = None
            self.update()
            if b - a >= 1.0:
                self.function_range_requested.emit(max(0.0, a), max(0.0, b))
            return
        if self.select_anchor_time is not None:
            self.auto_scroll_timer.stop()
            self.releaseMouse()
            self.select_anchor_time = None
            self.update()
            return
        if self._drag_point is not None:
            self.releaseMouse()
            self._drag_point = None

    # -- keyboard --------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            # See TimelineGameplay.keyPressEvent: an Esc with nothing selected
            # is the window's, for leaving the editor.
            if self.selected_uids:
                self.selected_uids.clear()
                self.update()
                event.accept()
                return
            super().keyPressEvent(event)
            return

        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            if self.selected_uids:
                self.points_delete_requested.emit([point.uid for point in self.selected_points()])
            event.accept()
            return

        if event.matches(QKeySequence.StandardKey.SelectAll):
            start_time = self.current_time - self.window_ms / 2
            end_time = self.current_time + self.window_ms / 2
            self.selected_uids = {
                point.uid
                for point in self._visible_points
                if not point.uninherited and start_time <= point.time <= end_time
            }
            self.update()
            event.accept()
            return

        super().keyPressEvent(event)

    # -- paint -------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), view_ground("#151b24"))
        self.draw_kiai_bands(painter)
        painter.setRenderHint(QPainter.Antialiasing, True)

        top = self._graph_top()
        bottom = self._graph_bottom()
        start_time = self.current_time - self.window_ms / 2
        end_time = self.current_time + self.window_ms / 2
        self.update_scale()

        for anchor, mouse_x in (
            (self.drag_anchor_time, self.drag_mouse_x),
            (self.select_anchor_time, self.select_mouse_x),
        ):
            if anchor is None:
                continue
            anchor_x = self.x_for_time(anchor)
            current_x = max(0.0, min(float(self.width()), mouse_x))
            left, width = min(anchor_x, current_x), abs(current_x - anchor_x)
            painter.fillRect(QRectF(left, 0, width, self.height()), self.range_brush)

        painter.setPen(QPen(theme.color("#3a4554"), 1))
        painter.drawLine(0, int(bottom), self.width(), int(bottom))
        self._draw_scale_guides(painter, top, bottom)

        pens = {"red": self.red_pen, "green": self.green_pen, "yellow": self.yellow_pen}
        # Only scanned when something is actually selected: during playback --
        # the case that has to stay cheap on a map with thousands of points --
        # nothing is.
        selected_times = (
            {point.time for point in self.timing_points if point.uid in self.selected_uids}
            if self.selected_uids
            else frozenset()
        )

        # Slice to the window rather than testing every timestamp for
        # visibility: a gimmick map has tens of thousands, and this runs at the
        # playback frame rate in every open SV view.
        first = bisect_left(self._line_kind_times, start_time - self.window_ms * 0.1)
        after_last = bisect_right(self._line_kind_times, end_time + self.window_ms * 0.1)
        # A barline gimmick stacks thousands of lines within one window, most of
        # them on the same pixel column: skip a repeat of the same kind at the
        # same x, and space the BPM labels like the SV ones already are.
        last_drawn: tuple[int, str] | None = None
        previous_x = -1e9
        for time_ms in self._line_kind_times[first:after_last]:
            kind = self._line_kinds[time_ms]
            x = self.x_for_time(time_ms)
            if x < -80 or x > self.width() + 80:
                continue
            selected = time_ms in selected_times
            clear_of_previous = x - previous_x >= SV_LABEL_MIN_SPACING_PX
            previous_x = x
            if not selected and last_drawn == (round(x), kind):
                continue
            last_drawn = (round(x), kind)
            if selected:
                painter.setPen(self.selected_pen)
                painter.drawLine(round(x), 0, round(x), self.height())
            painter.setPen(pens[kind])
            painter.drawLine(round(x), 0, round(x), self.height())
            bpm = self._bpm_at.get(time_ms) if self.show_bpm_labels else None
            if bpm and clear_of_previous:
                painter.save()
                painter.setFont(self.label_font)
                painter.setPen(self.label_pen)
                painter.drawText(QPointF(x + 4, self.height() - 6), f"{bpm:.0f} BPM")
                painter.restore()

        self._draw_sv_curve(painter, top, bottom, start_time, end_time)
        self._draw_placement_ghost(painter, top, bottom)
        self.draw_ctrl_precision_line(painter)
        self.draw_cursor_position(painter)

        cursor_x = self.width() // 2
        painter.setPen(self.cursor_pen)
        painter.drawLine(cursor_x, 0, cursor_x, self.height())

    def _draw_scale_guides(self, painter: QPainter, top: float, bottom: float) -> None:
        """Label the autoscaled range, so "high" on the graph means something.

        Without these the curve is unreadable after autoscaling: the same
        shape can be a 0.9x-1.1x wobble or a 1x-8x sweep.
        """
        painter.setPen(QPen(QColor(120, 132, 150, 90), 1, Qt.DotLine))
        painter.drawLine(0, int(top), self.width(), int(top))
        painter.save()
        painter.setFont(self.label_font)
        painter.setPen(self.label_pen)
        if self.volume_mode:
            painter.drawText(QPointF(4, top + 12), f"{self.scale_max:.0f}%")
            painter.drawText(QPointF(4, bottom - 4), f"{self.scale_min:.0f}%")
        else:
            painter.drawText(QPointF(4, top + 12), f"{self.scale_max:.2f}x")
            painter.drawText(QPointF(4, bottom - 4), f"{self.scale_min:.2f}x")
        painter.restore()

    def _draw_sv_curve(
        self, painter: QPainter, top: float, bottom: float, start_time: float, end_time: float,
    ) -> None:
        """The SV line: straight segments from each point to the next.

        SV is physically a step function -- it holds a value until the next
        point -- and drawing that literally, as horizontal runs joined by
        vertical risers, made every generated sweep look like a staircase
        whatever easing produced it. Connecting the points instead lets the
        generating function show in its own output: linear reads straight, sin
        reads as an S, exponential reads as a hockey stick.

        Straight segments, not a smoothed curve. A spline through the points
        invents curvature between them that no stored value justifies, and near
        a sharp SV change it overshoots past values the document does not
        contain. Dots mark the real points, so the line is never mistaken for a
        claim that SV ramps continuously between them.
        """
        # In volume_mode this plots effective volume (carried forward from
        # each point's own .volume, never reset by an uninherited point) --
        # see _rebuild_caches -- rather than effective SV. Same step/diagonal
        # drawing rules either way: a volume sweep generated by the Volume
        # tool (2d) is eased between existing points exactly like an SV sweep,
        # so the same visual makes the easing shape readable.
        series = self._volume_series if self.volume_mode else self._series
        series_times = self._volume_series_times if self.volume_mode else self._series_times
        if not series:
            return

        # Sliced by bisect, not by filtering the whole series three times: this
        # runs per frame per view, and a gimmick map carries tens of thousands
        # of points.
        margin = self.window_ms * 0.1
        first = bisect_left(series_times, start_time - margin)
        after_last = bisect_right(series_times, end_time + margin)
        # Anchor to the neighbours just outside the window, or the curve would
        # start and end abruptly at the view edges as it scrolls.
        visible = series[max(0, first - 1):min(len(series), after_last + 1)]
        if not visible:
            return

        points = [QPointF(self.x_for_time(t), self._sv_to_y(sv, top, bottom)) for t, sv in visible]

        painter.setPen(self.graph_pen)
        if len(points) == 1:
            painter.drawLine(QPointF(0.0, points[0].y()), QPointF(float(self.width()), points[0].y()))
        else:
            # Beyond the outermost points there is nothing to interpolate
            # toward, so the line runs flat out to the view edges.
            painter.drawLine(QPointF(0.0, points[0].y()), points[0])
            painter.drawLine(points[-1], QPointF(float(self.width()), points[-1].y()))
            # A gap of at least two whole beats between consecutive points
            # isn't a ramp the document defined -- SV physically holds its
            # value until the next point, so draw that literally (flat, then
            # a riser) instead of a diagonal implying a gradual change.
            # Closer points keep the diagonal, which is what makes a
            # generated sweep's easing shape (linear/sin/exponential/...)
            # readable in the first place.
            # `visible` is time-ordered, so the beat section only moves
            # forward -- but this asked active_timing for every single
            # segment, which profiling caught at 219 lookups per frame across
            # the gimmick page's SV bands, each one a binary search plus a
            # backward walk. Hold the section until the walk leaves it.
            beat_length = 0.0
            section_expires = -math.inf
            for i in range(1, len(points)):
                prev_point, point = points[i - 1], points[i]
                prev_time, time = visible[i - 1][0], visible[i][0]
                if prev_time >= section_expires:
                    beat_length = active_timing(self._beat_points, prev_time).beat_length
                    following = bisect_right(self._beat_times, prev_time)
                    section_expires = (
                        self._beat_times[following]
                        if following < len(self._beat_times)
                        else math.inf
                    )
                if time - prev_time >= 2 * beat_length:
                    painter.drawLine(prev_point, QPointF(point.x(), prev_point.y()))
                    painter.drawLine(QPointF(point.x(), prev_point.y()), point)
                else:
                    painter.drawLine(prev_point, point)

        painter.setBrush(QColor(140, 255, 185, 200))
        painter.setPen(Qt.NoPen)
        for point in points:
            if -20 <= point.x() <= self.width() + 20:
                painter.drawEllipse(point, 2.5, 2.5)

        # One label per point is unreadable once a sweep is dense -- the text
        # overlaps into a smear and costs a drawText per point per frame. Label
        # only a point that stands clear of its *predecessor*, which is a
        # property of the pair: chaining from the last label instead made which
        # points got labelled depend on where the window happened to start, so
        # they blinked on and off as the view scrolled.
        painter.save()
        painter.setFont(self.label_font)
        painter.setPen(self.value_pen)
        ascent = painter.fontMetrics().ascent()
        for i, (point, (_time_ms, sv)) in enumerate(zip(points, visible)):
            if not 0 <= point.x() <= self.width():
                continue
            if i and point.x() - points[i - 1].x() < SV_LABEL_MIN_SPACING_PX:
                continue
            label = f"{sv:.0f}%" if self.volume_mode else f"{sv:.2f}x"
            painter.drawText(QPointF(point.x() + 3, self._label_baseline(point.y(), ascent)), label)
        painter.restore()

    def _draw_placement_ghost(self, painter: QPainter, top: float, bottom: float) -> None:
        """Dashed preview of the green line a click would place, at the snapped
        time and the SV the cursor's height maps to -- the SV editor's
        equivalent of the chart view's translucent note ghost.
        """
        if self.tool != "green_line" or self._hover_time is None:
            return
        snapped = self._placement_time(self.x_for_time(self._hover_time))
        x = self.x_for_time(snapped)
        if x < -40 or x > self.width() + 40:
            return
        painter.setPen(self.ghost_pen)
        painter.drawLine(round(x), 0, round(x), self.height())
        if self._hover_sv is not None:
            y = self._sv_to_y(self._hover_sv, top, bottom)
            painter.setBrush(QColor(140, 255, 185, 130))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(x, y), 4, 4)
            painter.save()
            painter.setFont(self.label_font)
            painter.setPen(self.value_pen)
            painter.drawText(
                QPointF(x + 6, self._label_baseline(y, painter.fontMetrics().ascent(), 6)),
                f"{self._hover_sv:.2f}x")
            painter.restore()

    @staticmethod
    def _label_baseline(y: float, ascent: int, gap: float = 4) -> float:
        """Above the point, unless that runs off the top of the view.

        The graph's top margin is 20px scaled by the band's height -- about 7px
        on a gimmick band -- so a point at the top of the range (100% volume,
        the highest SV) put its label's ascent above y=0 and cut it off. Below
        the point it reads just as well.
        """
        if y - gap - ascent >= 0:
            return y - gap
        return y + gap + ascent
