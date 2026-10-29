"""The editor's time-axis views: the chart lane, the timing and density overview bars, and the frame each view sits in."""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from functools import lru_cache
from PySide6.QtCore import QEvent, QLine, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QRegion,
    QLinearGradient,
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
from ui.motion import paint_living_rim, tick_while_shown
from app.i18n import tr
from app.settings import NOTE_OPACITY_DEFAULT_PERCENT, NOTE_OPACITY_MIN_PERCENT
from ui import theme
from osu_io.timing import (
    TimingPoint,
    active_uninherited_at,
    kiai_spans,
    sorted_by_time,
    sv_at,
    uninherited_points,
)
from chart.time_axis import TimeAxisMixin, osu_snap_ms
from chart.gimmick_session import FAKE_SLIDER_MAX_LENGTH
from ui.common import (
    ACCENT_PINK,
    DON_COLOR,
    DRUMROLL_COLOR,
    KAT_COLOR,
    SLIDER_MULTIPLIER_ASSUMED,
    TAIKO_STRONG_SCALE,
    draw_note_sprite,
    duration_for_slider_length,
    equalize_button_widths,
    most_common_beat_length,
    spinner_pixmap,
    view_ground,
    view_ground_is_opaque,
)


@lru_cache(maxsize=None)
def value_label_font(base: QFont) -> QFont:
    """The numbers drawn on a layer -- SV, volume, BPM -- in the approved
    mockup's 11px bold app font, rather than the painter's default."""
    font = QFont(base)
    font.setPixelSize(11)
    font.setBold(True)
    return font


def tick_kind(position_in_beat: int, divisor: int) -> str:
    """Which tick style a grid line at `position_in_beat` of `divisor` gets."""
    if position_in_beat == 0:
        return "beat"
    if divisor % 2 == 0 and position_in_beat == divisor // 2:
        return "half"
    if divisor % 4 == 0 and position_in_beat % (divisor // 4) == 0:
        return "quarter"
    if divisor % 8 == 0 and position_in_beat % (divisor // 8) == 0:
        return "eighth"
    for prime, kind in ((3, "third"), (5, "fifth"), (7, "seventh"), (11, "eleventh"), (13, "thirteenth")):
        if divisor % prime == 0:
            return kind
    return "other"


def divisors_descending(divisor: int) -> tuple[int, ...]:
    """`divisor`'s own divisors, coarsest last -- the grids that are subsets of it.

    `_draw_snap_grid` walks this to find the finest division that is still
    wide enough to see. Halving instead would take 1/12 to 1/6 to 1/3 and then
    to 1.5, which is not a division of the beat at all.
    """
    return tuple(d for d in range(divisor, 0, -1) if divisor % d == 0)


def shift_is_held() -> bool:
    """Is Shift physically down **now**, outside of an event?

    `QApplication.keyboardModifiers()` is not that: it is the modifier state as
    of the last event Qt processed, and it goes stale. A Shift release that
    lands while this window is not focused, or that a shortcut consumes, leaves
    it reporting Shift held until some later key event happens to correct it --
    so one tap of Shift turned every note placed afterwards into a finisher,
    which is what "it should be hold" was. `queryKeyboardModifiers` asks the
    platform for the real state instead.

    **Inside a mouse handler, use the event's own modifiers.** Those are the
    truth at the instant of the click and cannot go stale; OR-ing the cached
    global into them is what let the stale bit through in the first place.
    """
    return bool(QApplication.queryKeyboardModifiers() & Qt.ShiftModifier)


def extract_timing_points(document) -> list[TimingPoint]:
    """Uninherited (BPM) points only, sorted by time.

    The snap grid and every beat calculation work in whole beats, so inherited
    SV points are deliberately excluded here. Use document.timing_points when
    SV matters.
    """
    return uninherited_points(sorted_by_time(document.timing_points))


def active_timing(
    timing_points: list[TimingPoint],
    time_ms: float,
) -> TimingPoint:
    return active_uninherited_at(timing_points, time_ms)


def editor_timeline_metadata(document) -> tuple[list[int], int | None]:
    """Bookmarks and PreviewTime for the timing overview bar.

    Bookmarks live in [Editor], but PreviewTime lives in [General]. Reading both
    from [Editor] meant preview_time was always None, so the yellow PreviewTime
    marker never appeared for any real beatmap. [Editor] is still accepted for
    PreviewTime in case some editor writes it there.
    """
    section=""; bookmarks=[]; preview_time=None
    for line in document.lines:
        raw=line.rstrip("\r\n"); stripped=raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section=stripped[1:-1]; continue
        if section=="Editor" and raw.startswith("Bookmarks:"):
            for value in raw.split(":",1)[1].split(","):
                try: bookmarks.append(max(0,round(float(value.strip()))))
                except ValueError: pass
        elif section in ("General","Editor") and raw.startswith("PreviewTime:"):
            try:
                value=round(float(raw.split(":",1)[1].strip()))
                preview_time=value if value>=0 else None
            except ValueError: pass
    return sorted(set(bookmarks)),preview_time


def kiai_ranges(document, duration_ms: int) -> list[tuple[int, int]]:
    return kiai_spans(document.timing_points, duration_ms)


def is_fake_slider(note) -> bool:
    """A drumroll with no playable duration: drawn, never hittable.

    The threshold is `gimmick_session.FAKE_SLIDER_MAX_LENGTH`, shared with the
    config that writes them, so what the toolbox can produce is exactly what
    the layer will admit. Exposed as `MainWindow.is_fake_slider` too, which is
    the name it has been reached by; the definition lives here because the
    editor timeline needs it and is built long before that class.
    """
    return (
        note.is_slider and note.length is not None
        and note.length <= FAKE_SLIDER_MAX_LENGTH
    )


class TimelineGameplay(TimeAxisMixin, QWidget):
    # Tools whose gesture is "drag a range", not "click a spot". Listed once
    # because mousePressEvent has to start the drag and mouseReleaseEvent has
    # to emit the range, and a tool added to one tuple and not the other is a
    # press that grabs the mouse and a release that never lets go.
    RANGE_TOOLS = ("function", "convert", "kiai")

    selection_changed = Signal(object)
    selection_finalized = Signal(object)
    seek_requested = Signal(float)
    snap_changed_by_wheel = Signal(int)
    # (note_kind, snapped_time_ms, new_combo, big) -- emitted on left click
    # when self.tool is "don"/"kat"; Shift-click places a big (finisher) note,
    # the same modifier that already marks a slider/spinner big on release.
    # The widget holds no MainWindow reference, so it emits the request rather
    # than mutating the document itself; the owner (MainWindow, per
    # difficulty) builds the HitObject.
    note_place_requested = Signal(str, float, bool, bool)
    # (note_kind, start_ms, end_ms, new_combo, big) -- slider/spinner
    # placement is a click-drag (start position to stop position), not a
    # single click; a plain click without drag arrives here too, with
    # start_ms == end_ms, and the owner falls back to a small default length.
    note_place_with_duration_requested = Signal(str, float, float, bool, bool)
    # (uid, new_end_ms) -- dragging an existing slider/spinner's right edge.
    note_duration_edit_requested = Signal(int, float)
    # (note uid) -- a fake slider's `length`, which is the one number that
    # decides how far it draws and is not reachable by dragging: its tail is
    # behind its head, so `_extendable_note_near_edge` never sees an edge.
    note_length_edit_requested = Signal(int)
    # uid of the note nearest a right click, within a small pixel radius.
    note_delete_requested = Signal(int)
    # list[uid] -- Delete/Backspace with a selection, Editor-page views only.
    # One signal for the whole selection so the owner can push a single undo
    # step instead of one per note.
    notes_delete_requested = Signal(object)
    # New window_ms after a Ctrl+wheel zoom. Zoom is a per-difficulty
    # property: every view of the same difficulty (chart and SV alike) has to
    # keep showing the same time span or they stop scrolling together.
    zoom_changed = Signal(float)
    # uid -- double click on a red line, in a view that allows it. Only the
    # barline gimmick layer does; everywhere else the red lines are drawn for
    # reference and do not respond. The owner reads the point's own values.
    timing_line_edit_requested = Signal(int)
    # list[uid] -- right click on a red line, or Delete with red lines selected.
    # One signal for the whole set so a bulk delete is a single undo step.
    timing_lines_delete_requested = Signal(object)
    # (start_ms, end_ms) -- drag-select with the function tool. The chart view's
    # equivalent of SVEditorView.function_range_requested, and the barline
    # layer's route to generating a run of red lines over a range.
    timing_function_range_requested = Signal(float, float)
    # (note uids, timing point uids, delta_ms) -- a select-tool drag that
    # started on an object or an owned red line rather than on empty space. The
    # view reports only what was actually grabbed; growing that into the whole
    # gimmick structure around it is the owner's job, because which lines belong
    # to which structure is its knowledge and not the view's.
    objects_move_requested = Signal(object, object, float)

    # The height every fixed pixel size in paintEvent was measured at. The
    # gimmick page's layers are drawn scaled against it, and so is a standalone
    # view now that MIN_HEIGHT is below it.
    DESIGN_HEIGHT = 180
    # The smallest a standalone view is allowed to get. Its own number rather
    # than DESIGN_HEIGHT because the two are different jobs: one is what the
    # artwork was measured at, the other is how much room a view demands on a
    # page that stacks several of them. Tied together, asking for a shorter
    # view also rescaled every tick and label in it.
    MIN_HEIGHT = 80
    # What the height control opens on. Not MIN_HEIGHT: the floor is how short
    # a view may be pushed, and lowering it should not shorten everybody's
    # views by itself.
    DEFAULT_VIEW_HEIGHT = 120

    # Note radius as a fraction of the view height, (full row, split row), and
    # its ceiling. A chart view is read as a row of timestamps rather than as
    # gameplay, so these are well under the sizes the view was first drawn at
    # -- tuned by eye against a stack of views rather than derived.
    NOTE_HEIGHT_FRACTIONS = (0.204, 0.141)
    NOTE_RADIUS_MAX = 25.0
    # The gimmick page keeps the pre-halving numbers: a band is 88px, so its
    # notes are already scaled down by the same height that scales everything
    # else there, and halving them again leaves less circle than the lines
    # drawn beside it.
    GIMMICK_NOTE_HEIGHT_FRACTIONS = (0.22, 0.15)
    GIMMICK_NOTE_RADIUS_MAX = 31.0
    # A finisher a third bigger again than its scale below makes it, on both
    # pages (owner, 2026-10-08): at the old size a big note in a dense row
    # did not stand out from the small ones around it. The hit test reads
    # the same radius, so the grab grows with it.
    FINISHER_ENLARGE = 1.33

    def __init__(self) -> None:
        super().__init__()

        self.notes = []
        self.note_times: list[int] = []
        self._max_extend_ms = 0.0
        self.timing_points: list[TimingPoint] = []
        self._timing_times: list[float] = []
        # Every point, inherited ones included. `timing_points` above is the
        # uninherited-only list the beat grid needs; a slider's length depends
        # on the SV in force at it, and that only exists on inherited points.
        self._sv_points: list[TimingPoint] = []
        # The map's [Difficulty] SliderMultiplier, the other half of that same
        # conversion. Falls back to osu!'s default until a document is loaded.
        self.slider_multiplier = SLIDER_MULTIPLIER_ASSUMED
        self.selected: set[int] = set()
        # Red lines picked by a drag, in a view that owns them. Kept apart from
        # `selected` (which is note original_index) because the barline layer
        # shows no notes at all and these are what its Delete acts on.
        self.selected_timing_uids: set[int] = set()
        # Predicate deciding which hit objects this view shows; see refresh_notes.
        self.object_filter = None
        # Draw the document's uninherited (BPM) points as vertical red lines.
        # Off for the shared player timeline and the Editor page's chart views,
        # where the snap grid already says where the beats are; on for the
        # gimmick layers, whose whole subject matter is red lines.
        self.show_timing_lines = False
        # Hand this view's red lines to its tools -- right click deletes one,
        # select drags one, a rubber band picks them up. The barline layer
        # only: a red line is that layer's material, and it is view-only
        # elsewhere.
        self.timing_edit_enabled = False
        # ...and let a double click on one open its BPM. Both structure layers,
        # because a fake slider's red line is the half of it that makes the
        # object fake and retiming that line is how a shiny is tuned. Separate
        # from `timing_edit_enabled` on purpose: the fake slider layer must not
        # let the line be dragged on its own, since a line pulled out from
        # under its sliders dismantles the structure rather than moving it
        # (only `_expand_move` knows how to move the whole thing, and it
        # recognises a structure by the object, not the line).
        self.timing_dialog_enabled = False
        # Right click on a drawn red line removes that line alone, with no
        # dragging or selecting. The fake slider layer: a mapper removing the
        # line under a slider wants the slider kept, and a right click on the
        # slider itself -- tried first -- still takes both.
        self.timing_delete_enabled = False
        # Restrict the drawn red lines to these milliseconds; None draws all of
        # them. The fake slider layer sets it to the fake sliders' own times, so
        # the line it shows is the one squashing the object beside it rather
        # than every red line in the map.
        self.timing_line_times: set[int] | None = None
        # Derives the above from the document on every refresh -- see
        # SVEditorView.point_times_for, which solves the same staleness.
        self.timing_line_times_for = None
        # How the hover preview draws a Don/Kat: as the note itself, as the
        # yellow fake slider it will become, or as the bar a barline note draws.
        # The shape a tool writes differs per layer, so the ghost has to as well.
        self.ghost_style = "note"
        # (start, end, distance, slider_offset) while the Multiple Fake Slider
        # tool is held, None otherwise -- the run its ghost previews. The offset
        # rides along because the view has no gimmick config of its own to read
        # it from. See `_set_gimmick_tool`.
        self.multi_run: tuple[int, int, int, int] | None = None
        # Write the BPM beside each red line this view owns. On in the barline
        # layer, whose whole material is red lines and where the number used to
        # be readable only by dropping down to the SV layer under it.
        self.show_bpm_labels = False
        # Milliseconds whose red line belongs to a *different* layer, drawn
        # dimmed and untouchable here. Supplied by the owner, because which
        # structure owns what is its knowledge, not the view's.
        self.foreign_times: set[int] = set()
        # ...derived from the document on every refresh, like the other two
        # `_for` hooks: the objects it depends on move as the map is edited.
        self.foreign_times_for = None
        # Milliseconds carrying a gimmick line -- the centre of a structure, as
        # opposed to one of the restore lines around it. A drag lands one of
        # these on the snap grid and a right click resolves to it, so which
        # lines they are has to be exact: the BPM that makes a line a gimmick
        # line is configurable per layer, and guessing "anything above 1000" got
        # a 900 BPM gimmick wrong in one direction and a real 1200 BPM section
        # wrong in the other. Supplied by the owner for the same reason
        # `foreign_times` is -- it holds the configs.
        self.gimmick_times: set[int] = set()
        self.gimmick_times_for = None
        # Milliseconds carrying a shiny note -- a stack of fake sliders on one
        # timestamp, which is what reads as white. Derived from the document per
        # refresh like the three sets above, because "is this a shiny" is a
        # question about how many objects share a millisecond and that changes
        # with every placement.
        self.shiny_times: set[int] = set()
        self.shiny_times_for = None
        # Two rows and a centred grid, for the fake slider layer alone: it shows
        # two kinds of drawn object one millisecond apart, and stacked on one
        # baseline at gimmick zoom they are the same blob. Fake sliders take the
        # upper row, shiny notes the lower, and the snap ticks move to the
        # middle so neither row is drawn over them.
        self.split_rows = False

        self.current_time = 0.0
        self.window_ms = 2000.0
        self.snap_divisor = 4
        # Bottom-anchored (today's shared player-deck timeline) by default.
        # Editor-page chart/gimmick views turn this on: snap ticks radiate
        # both above and below the baseline and notes sit centered on it.
        self.symmetric = False
        # What the view is *for*, apart from how it is drawn: an Editor-page
        # view (no click-to-seek, Delete deletes, a tool target) rather than
        # the Fancy Arranger's transform-selection surface. It used to be read
        # off `symmetric`, which stopped working once the Fancy timeline was
        # drawn centred too (2026-10-08). set_symmetric sets both.
        self.editor_view = False
        # "select" preserves today's drag-select-by-time-range behavior
        # (used by the shared Fancy Arranger timeline). Editor-page chart
        # views switch this via their own tool row; nothing else ever
        # changes it, so the shared timeline's behavior is untouched.
        self.tool = "select"
        self.new_combo = False
        # The Finisher toggle (tool 4, E): Shift held down for good. Read
        # wherever Shift is, so the two always mean the same note.
        self.finisher = False

        self.drag_start_x: float | None = None
        self.drag_anchor_time: float | None = None
        self.drag_mouse_x = 0.0
        self.wheel_accumulator = 0.0
        self.alt_wheel_accumulator = 0.0
        self.last_rendered_time = -1.0
        self.auto_scroll_timer = QTimer(self)
        self.auto_scroll_timer.setInterval(16)
        self.auto_scroll_timer.timeout.connect(self._auto_scroll_selection)
        self.is_playing = False

        # Hover ghost: the note that would be placed if you clicked right
        # now, shown translucent. Needs mouse tracking since plain
        # mouseMoveEvent only fires with a button held otherwise.
        self.setMouseTracking(True)
        self._hover_time: float | None = None
        self._hover_y: float | None = None
        # The y a rubber band started at, which on a split layer is the row it
        # selects in. None for every drag that is not a selection.
        self._drag_y: float | None = None
        # What a Ctrl rubber band adds to: (note indexes, line uids) selected
        # when it started. None for a plain band, which replaces.
        self._band_base: tuple[set, set] | None = None
        self._move_clicked: tuple = (None, None)

        # Slider/spinner placement is click-drag (start -> stop), not a
        # single click with a fixed length.
        self._placing_tool: str | None = None
        self._placing_start_time: float | None = None
        self._placing_end_time: float | None = None
        # Dragging an existing slider/spinner's right edge to extend it.
        self._resizing_note = None
        self._resize_new_end_time: float | None = None
        # Whether a select-tool press on an object drags it to a new time
        # instead of starting a rubber-band selection.
        self.move_enabled = False
        # Whether double-clicking an object here types its length. Layer 2's,
        # because a fake slider is the only object whose length is a gimmick
        # parameter rather than a duration you drag.
        self.note_length_dialog_enabled = False
        # On a normal chart every click lands near a note, so a bare press
        # only starts a move when the note under it is already selected --
        # rubber-band selecting is still what an unselected note's click (or
        # drag) does, and a plain click with no drag still fully deselects
        # (see _finish_move) rather than reselecting what it landed on. A
        # gimmick layer has neither rule: the material is sparse, a structure
        # is placed and then nudged before it is ever selected, and a click
        # there always means "select this".
        self.move_requires_selection = False
        # Select-tool drag of something rather than of empty space -- see
        # objects_move_requested and _begin_move.
        self._move_note_uids: list[int] = []
        self._move_point_uids: list[int] = []
        self._move_origin: float | None = None
        self._move_anchor_time = 0.0
        self._move_delta = 0.0
        self._move_snaps = True
        # What to place instead, if a grab under a placement tool turns out to
        # be a plain click rather than a drag.
        self._move_fallback: tuple | None = None

        self.don_brush = QColor(*DON_COLOR, 180)
        self.kat_brush = QColor(*KAT_COLOR, 180)
        self.slider_brush = QColor(*DRUMROLL_COLOR, 180)
        # An osu! skin's note art, or None for the built-in circles.
        self.skin = None
        self.ghost_don_brush = QColor(*DON_COLOR, 90)
        self.ghost_kat_brush = QColor(*KAT_COLOR, 90)
        self.ghost_slider_brush = QColor(*DRUMROLL_COLOR, 90)
        # A shiny note is several drumrolls on one millisecond, drawn as one
        # mark rather than as three overlapping yellows (which at this size is
        # just a blob).
        #
        # **White, for separation.** This layer is an editing view and the
        # colour is a label, not a simulation: white is the one thing that
        # cannot be mistaken for the drumroll yellow beside it, so a stack is
        # tellable from a lone fake slider at a glance.
        #
        # It is deliberately *not* what the game does, and the comment that
        # used to be here claimed otherwise -- that a stack "washes out to
        # white" in play. It does not: a pile of opaque heads reads as one
        # head, which is why the gameplay preview draws it flat #fcb806.
        self.shiny_brush = QColor(255, 254, 245, 235)
        self.ghost_shiny_brush = QColor(255, 254, 245, 120)
        # Spinner extent: grey so it reads as "this span is occupied" rather
        # than competing with don/kat/slider colour coding.
        self.spinner_band_brush = QColor(190, 195, 205, 70)
        self.spinner_edge_pen = QPen(QColor(220, 226, 236, 170), 2)
        # Captured here, after every fill above is built, so set_note_opacity
        # scales from the built-in look instead of compounding each time it is
        # called -- and so a fill added later is picked up by naming it once.
        self._base_note_alphas = {
            name: getattr(self, name).alpha() for name in (
                "don_brush", "kat_brush", "slider_brush",
                "ghost_don_brush", "ghost_kat_brush", "ghost_slider_brush",
                "shiny_brush", "ghost_shiny_brush", "spinner_band_brush",
            )
        }
        self.restyle_pens()

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

    def restyle_pens(self) -> None:
        """The view's pens, built once rather than per tick per frame. The
        chart's whites go through `theme.screen`: on NSO's pink lanes a white
        barline or outline is violet. Rebuilt on a theme switch (retheme).
        """
        self.ghost_pen = QPen(theme.screen_rgba(255, 255, 255, 110), 2)
        self.normal_note_pen = QPen(theme.screen_rgba(255, 255, 255, 220), 2)
        self.selected_note_pen = QPen(theme.screen_rgba(255, 220, 110, 235), 3)
        self.baseline_pen = QPen(theme.screen("#7a8492"), 2)
        self.bpm_label_pen = QPen(theme.screen_rgba(225, 230, 240, 200), 1)
        self.label_font = value_label_font(self.font())
        self.cursor_pen = QPen(theme.screen("#ffffff"), 3)
        # Two weights of red line: the ones this view owns, and the ones drawn
        # so the owned ones can be read in context.
        self.timing_line_pen = QPen(QColor(255, 90, 90, 235), 2)
        self.timing_line_ref_pen = QPen(QColor(255, 90, 90, 90), 1)
        # Where a line being dragged started. Dashed as well as faded, so it
        # cannot be misread as one of the reference lines above.
        self.timing_line_ghost_pen = QPen(QColor(255, 90, 90, 110), 2, Qt.DashLine)
        # Built once instead of constructing a QPen per tick per frame --
        # at fine snap divisors with many chart views open simultaneously,
        # that allocation churn was a real contributor to paint stutter.
        # (kind -> (QPen, tick_height))
        # Two ticks are two ticks only with a background pixel between them,
        # and the widest tick pen is 2px -- so a grid finer than this is a
        # filled rectangle drawn one line at a time. Read off the pens rather
        # than typed, so restyling a tick cannot silently invalidate it.
        self._tick_styles = {
            "beat": (QPen(theme.screen("#f2f2f2"), 2), 32),
            "half": (QPen(QColor("#ff5151"), 2), 24),
            "quarter": (QPen(QColor("#4b9cff"), 1), 18),
            "eighth": (QPen(theme.screen("#f2d34f"), 1), 12),
            "third": (QPen(QColor("#b578ff"), 1), 16),
            "fifth": (QPen(theme.screen("#f2d34f"), 1), 14),
            "seventh": (QPen(theme.screen("#55d6be"), 1), 12),
            "eleventh": (QPen(QColor("#ff9f40"), 1), 12),
            "thirteenth": (QPen(theme.screen("#8bd1ff"), 1), 12),
            "other": (QPen(QColor("#738098"), 1), 10),
        }
        self.beat_line_pen = QPen(theme.screen_rgba(255, 255, 255, 70), 1)
        self._min_tick_spacing_px = max(
            pen.width() for pen, _height in self._tick_styles.values()
        ) + 1

    def _draw_skinned_roll_body(self, painter, x, end_x, center_y, radius) -> bool:
        """A drumroll's stretched body and its tail cap, from the skin.

        The head is not drawn here: every note in this view goes through one
        `draw_note_sprite` call below, and a drumroll's head is that call with
        the drumroll colour. Returns whether the skin supplied the art, so the
        caller can fall back to its own bar.

        The alpha the view draws notes at is carried as painter opacity, the
        same way `draw_note_sprite` carries it -- these layers are deliberately
        translucent so the snap grid reads through them, and skin art is opaque.
        """
        skin = self.skin
        if skin is None:
            return False
        diameter = round(radius * 2)
        fill = self.slider_brush
        middle = skin.scaled("taiko-roll-middle", diameter,
                             QColor(fill.red(), fill.green(), fill.blue()))
        if middle is None:
            return False
        previous = painter.opacity()
        painter.setOpacity(previous * fill.alphaF())
        body_end = max(end_x, x)
        if body_end > x:
            painter.drawPixmap(
                QRectF(x, center_y - radius, body_end - x, radius * 2),
                middle, QRectF(middle.rect()),
            )
        # Origin TopLeft, per the wiki: the cap butts onto the end of the track
        # and reaches past it, the way the head reaches back past the start. A
        # negative length collapses the track, not the cap -- see
        # GameplayViewerView._draw_skinned_roll.
        tail = skin.scaled("taiko-roll-end", diameter,
                           QColor(fill.red(), fill.green(), fill.blue()))
        if tail is not None:
            painter.drawPixmap(
                QPointF(body_end, center_y - tail.height() / 2.0), tail)
        painter.setOpacity(previous)
        return True

    def set_note_opacity(self, percent: float) -> None:
        """Scale every note fill's alpha; NOTE_OPACITY_DEFAULT_PERCENT is the
        built-in look, and 100 draws them solid.

        The ghosts and the spinner band scale with the notes rather than being
        pinned, because what they are is "fainter than a note" -- holding them
        still while the notes went solid would have inverted that.
        """
        scale = max(NOTE_OPACITY_MIN_PERCENT, float(percent)) / NOTE_OPACITY_DEFAULT_PERCENT
        for name, alpha in self._base_note_alphas.items():
            getattr(self, name).setAlpha(max(1, min(255, round(alpha * scale))))
        self.update()

    def load_document(self, document) -> None:
        self.refresh_notes(document)
        self.current_time = float(
            self.notes[0].time if self.notes else 0
        )
        self.selected.clear()
        self.update()

    def refresh_notes(self, document) -> None:
        """Re-read notes/timing after an edit, without resetting cursor or selection.

        Placing or deleting a note must not jump the view back to the first
        note or clear the current selection the way a fresh load_document
        would.
        """
        # The gimmick editor's layers each show one kind of object, so that a
        # fake slider can be selected and dragged without the real chart's notes
        # sitting under the cursor. Everywhere else this is None and every
        # object is shown.
        if self.object_filter is None:
            self.notes = document.hit_objects
        else:
            self.notes = [note for note in document.hit_objects if self.object_filter(note)]
        if self.timing_line_times_for is not None:
            self.timing_line_times = self.timing_line_times_for(document)
        self.note_times = [note.time for note in self.notes]
        if self.foreign_times_for is not None:
            self.foreign_times = self.foreign_times_for(document)
        if self.gimmick_times_for is not None:
            self.gimmick_times = self.gimmick_times_for(document)
        if self.shiny_times_for is not None:
            self.shiny_times = self.shiny_times_for(document)
        if self.kiai_bands_for is not None:
            self.kiai_bands = self.kiai_bands_for(document)
        else:
            self.set_kiai_from(document.timing_points)
        # NOT shared with MainWindow._sorted_points/_uninherited_points_cached
        # (unlike SVEditorView's own timing_points/_beat_points) -- reverted
        # after a report that the regular Editor page's snap grid started
        # shifting position right after this sharing landed. Independent
        # investigation and repeated stress tests of the shared path found no
        # provable defect, but the report pointed at this exact view and this
        # exact change, and a wrong snap grid is worse than the sort it saves.
        # `extract_timing_points` is the same composition
        # (uninherited_points(sorted_by_time(...))), done locally again.
        self.timing_points = extract_timing_points(document)
        self._timing_times = [point.time for point in self.timing_points]
        self._sv_points = sorted_by_time(document.timing_points)
        self.slider_multiplier = document.slider_multiplier
        # Lines deleted elsewhere must not stay "selected" forever -- the same
        # rule SVEditorView.refresh_points applies to its own selection.
        self.selected_timing_uids &= {point.uid for point in self.timing_points}
        # Longest slider/spinner duration, so paintEvent can widen its visible-
        # notes lookup: a long spinner's band must stay drawn until its *end*
        # scrolls out of view, not just its start.
        self._max_extend_ms = 0.0
        for note in self.notes:
            if note.is_slider or note.is_spinner:
                end = self._note_end_time(note)
                if end is not None:
                    self._max_extend_ms = max(self._max_extend_ms, end - note.time)
        self.update()

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        # The placement ghost is drawn from the tool, and a view repaints on
        # mouse move: picking Kat over a still cursor kept showing a Don until
        # the mouse twitched (owner, 2026-10-08). Same reason as Shift.
        self.update()

    def set_new_combo(self, value: bool) -> None:
        self.new_combo = bool(value)
        self.update()

    def set_finisher(self, value: bool) -> None:
        self.finisher = bool(value)
        self.update()

    def set_snap_divisor(self, divisor: int) -> None:
        """Change the grid, not the playhead.

        Alt+wheel routes here through `snap_changed_by_wheel` on every open
        view, and this used to re-snap `current_time` onto the new grid and
        broadcast a seek -- so switching 1/4 to 1/3 physically moved every
        view's playhead. Looking at a different grid is not a request to
        move: the playhead self-corrects on the very next wheel notch anyway,
        since `wheel_seek_time` snaps from wherever `current_time` currently
        is before stepping, so sitting off-grid between notches costs
        nothing. Matches `SVEditorView.set_snap_divisor` and
        `GameplayViewerView.set_snap_divisor`, which never snapped here either.
        """
        self.snap_divisor = divisor
        self.update()

    def set_symmetric(self, symmetric: bool, editor_view: bool | None = None) -> None:
        self.symmetric = symmetric
        self.editor_view = symmetric if editor_view is None else editor_view
        self.update()

    def note_radii(self) -> tuple[float, float]:
        """(normal, finisher) drawn note radius, derived from the view height.

        One place, because the painter and the hit test both need it and a
        note you can see but not grab is the bug that comes of them drifting.

        Sized from the view, not typed: the gimmick page shows six layers as
        short bands, and a fixed 42px finisher there is taller than the band it
        sits in -- it spills past the edges and stops reading as centred on the
        baseline. Full-height views are unchanged, since they clamp at the
        sizes these used to be fixed at. A split layer halves it again: it
        draws two rows of objects instead of one, and at the full radius the
        two overlap and the pair reads as a single smear.

        The finisher ratio here is **not** the ruleset's. `TAIKO_STRONG_SCALE`
        (1.538x) is what a player sees and is what `GameplayViewerView` draws;
        on a timestamp row that far apart reads as two different kinds of
        object rather than one note and its louder twin, and the normal note
        has to shrink to absurdity for the big one to fit. `EDITOR_STRONG_SCALE`
        puts the normal note at 75% of the big one, which is the proportion the
        editor is read at. The gimmick page's bands keep
        the ruleset ratio along with their own sizes. No second cap on the
        finisher: `normal` is already capped.
        """
        fractions, ceiling = (
            (self.GIMMICK_NOTE_HEIGHT_FRACTIONS, self.GIMMICK_NOTE_RADIUS_MAX)
            if getattr(self, "gimmick_layer", None) is not None
            else (self.NOTE_HEIGHT_FRACTIONS, self.NOTE_RADIUS_MAX)
        )
        normal = min(ceiling, self.height() * fractions[bool(self.split_rows)])
        strong = (
            TAIKO_STRONG_SCALE
            if getattr(self, "gimmick_layer", None) is not None
            else EDITOR_STRONG_SCALE
        )
        return normal, normal * strong * self.FINISHER_ENLARGE

    def _note_near_x(self, x: float, radius_px: float | None = None, y: float | None = None):
        """Nearest note whose drawn circle covers `x`, for grabbing and deleting.

        The reach is the note's **own body**, not a fixed number: a normal note
        is drawn at up to 31px and a finisher at up to 42, so a flat 20 meant
        the outer two thirds of every circle looked grabbable and was not --
        which is what "you have to click exactly on the millisecond" was. A
        finisher is bigger on screen and is now bigger to grab, which is the
        point of drawing it bigger.

        `radius_px` overrides that for a caller that wants a fixed reach.

        `y` matters on a split layer and nowhere else. There the two rows hold
        two different structures a millisecond apart -- the same pixel column
        at any zoom the layer is readable at -- so the row the click landed in
        is the only thing that says which of them was meant. Without it the
        scan returned whichever came first in the document, and grabbing the
        fake slider drawn on the upper row dragged the shiny note under it
        (and the other way round).
        """
        wants_lower = None if y is None or not self.split_rows else y > self._baseline_y()
        normal_radius, finisher_radius = self.note_radii()
        best = None; best_distance = None
        for note in self.notes:
            if wants_lower is not None and (round(note.time) in self.shiny_times) != wants_lower:
                continue
            reach = radius_px if radius_px is not None else (
                finisher_radius if (note.is_finisher or note.is_spinner) else normal_radius
            )
            distance = abs(self.x_for_time(note.time) - x)
            if note.is_spinner and self._on_spinner_band(note, x):
                # A spinner is its whole duration: anywhere on the band is on it.
                distance = 0.0
            if distance <= reach and (best_distance is None or distance < best_distance):
                best = note; best_distance = distance
        return best

    def _on_spinner_band(self, note, x: float) -> bool:
        """Whether x falls between a spinner's start and end, where its band is drawn."""
        end = self._note_end_time(note)
        return end is not None and self.x_for_time(note.time) <= x <= self.x_for_time(end)

    def _note_end_time(self, note) -> float | None:
        """End time of a slider or spinner, for edge hit-testing and resize."""
        if note.is_spinner:
            return float(note.end_time) if note.end_time is not None else None
        if note.is_slider and note.length is not None and note.slides is not None:
            timing = active_timing(self.timing_points, note.time)
            # `self.timing_points` is the uninherited-only list (see
            # extract_timing_points), where every point reads as 1.0x -- so the
            # SV has to come off the full list, which is what osu! resolves the
            # slider's speed from at the slider's own start time.
            sv = sv_at(self._sv_points, note.time)
            duration = duration_for_slider_length(
                note.length * note.slides, timing.beat_length, sv, self.slider_multiplier,
            )
            return note.time + duration if duration > 0 else None
        return None

    def _extendable_note_near_edge(self, x: float, radius_px: float = 16.0):
        """A slider/spinner whose right (end-time) edge is near x, for resize-by-drag."""
        best = None; best_distance = None
        for note in self.notes:
            if not (note.is_slider or note.is_spinner):
                continue
            end = self._note_end_time(note)
            if end is None:
                continue
            distance = abs(self.x_for_time(end) - x)
            if distance <= radius_px and (best_distance is None or distance < best_distance):
                best = note; best_distance = distance
        return best

    def mousePressEvent(self, event) -> None:
        if event.button()==Qt.RightButton:
            # A drag-selection is live and this press landed on the page's
            # timing bar rather than in the view: scrub the playhead there
            # instead of deleting whatever sits under the cursor, so a range
            # longer than one screenful can be built without dragging all the
            # way through it. Only reachable at all because grabMouse() during
            # the drag routes every event here regardless of where the cursor
            # actually is -- see TimeAxisMixin._press_is_on_timing_bar.
            if self.drag_anchor_time is not None and self._press_is_on_timing_bar(
                event.globalPosition().toPoint()
            ):
                self._bar_scrubbing = True
                self.current_time = self._time_on_timing_bar(event.globalPosition().toPoint())
                self.seek_requested.emit(self.current_time)
                self.drag_mouse_x = self.x_for_time(self.current_time)
                self._update_drag_selection()
                event.accept()
                return
            note = self._note_near_x(event.position().x(), y=event.position().y())
            # On something selected, right click is Delete: the whole
            # selection goes, not just the one under the cursor.
            if note is not None and note.original_index in self.selected and self._delete_selection():
                event.accept()
                return
            if note is not None:
                self.note_delete_requested.emit(note.uid)
                event.accept()
                return
            # No note under the cursor: in a layer that owns its red lines, the
            # red line is what right click is for. Tried second so a layer
            # showing both never loses the note to the line beneath it.
            if self.timing_edit_enabled:
                point = (
                    self._gimmick_centre_near_x(event.position().x())
                    or self._timing_point_near_x(event.position().x())
                )
                if point is not None and not (
                    point.uid in self.selected_timing_uids and self._delete_selection()
                ):
                    self.timing_lines_delete_requested.emit([point.uid])
            elif self.timing_delete_enabled:
                # Only the lines this view draws: no structure centre lookup,
                # because the point is to take the line and nothing around it.
                point = self._timing_point_near_x(event.position().x())
                if point is not None:
                    self.timing_lines_delete_requested.emit([point.uid])
            event.accept()
            return
        if event.button()!=Qt.LeftButton:
            return

        if self.tool in self.RANGE_TOOLS:
            # Same gesture as the SV editor's function tool: drag a range, then
            # answer a dialog about what to fill it with. The others share it --
            # they also act on a range, they just need nothing else asked.
            self.drag_start_x = event.position().x()
            self.drag_mouse_x = self.drag_start_x
            self.drag_anchor_time = self.time_for_x(self.drag_start_x)
            self._drag_y = None
            self.grabMouse()
            self.update()
            event.accept()
            return

        if self.tool in ("slider", "spinner"):
            # Extend an already-placed slider/spinner if the click landed
            # near its right edge; otherwise start placing a new one.
            existing = self._extendable_note_near_edge(event.position().x())
            if existing is not None:
                self._resizing_note = existing
                self._resize_new_end_time = self._note_end_time(existing)
                self.grabMouse()
                event.accept()
                return
            snapped = self.snap_ms(self.time_for_x(event.position().x()))
            self._placing_tool = self.tool
            self._placing_start_time = snapped
            self._placing_end_time = snapped
            self.grabMouse()
            event.accept()
            return

        if self.tool != "select":
            time_ms = self.snap_ms(self.time_for_x(event.position().x()))
            # The event's own modifiers, never the cached global -- see
            # `shift_is_held`. A tap of Shift used to stick.
            big = bool(event.modifiers() & Qt.ShiftModifier) or self.finisher
            # Pressing on something that is already there starts a move, even
            # with a placement tool in hand. Dragging is how a gimmick object
            # gets nudged, and having to switch back to Select first is the
            # reason it read as "objects can't be moved" -- after placing a run
            # of them, the placement tool is what you are still holding.
            #
            # A press that does not turn into a drag still places, so clicking
            # an existing note to retype it (Don over a Kat) is unchanged: the
            # placement is only handed over once the object has actually moved.
            grabbed = (
                self._note_near_x(event.position().x(), y=event.position().y())
                if self.move_enabled else None
            )
            if grabbed is not None:
                self._begin_move(grabbed, None, event.position().x())
                self._move_fallback = (self.tool, time_ms, self.new_combo, big)
                event.accept()
                return
            self.note_place_requested.emit(self.tool, time_ms, self.new_combo, big)
            event.accept()
            return

        # A tail grab takes priority over everything else select does, but
        # only within its own small radius (_extendable_note_near_edge's 16px,
        # tighter than the note body _note_near_x reaches with) -- so a click on
        # a slider's head or body still falls through to the move/rubber-band
        # handling below.
        # Independent of move_enabled: a drumroll's tail is draggable on any
        # layer, not only the gimmick ones where whole-object moving is
        # enabled. This is what lets a slider be lengthened without switching
        # back to the tool that created it (see the identical branch above
        # for "slider"/"spinner").
        existing = self._extendable_note_near_edge(event.position().x())
        if existing is not None:
            self._resizing_note = existing
            self._resize_new_end_time = self._note_end_time(existing)
            self.grabMouse()
            event.accept()
            return

        # Select mode, on top of something: drag it to a new time. A note is
        # tried before a red line so a layer showing both never loses the
        # object to the line under it.
        if self.move_enabled:
            grabbed_note = self._note_near_x(event.position().x(), y=event.position().y())
            grabbed_point = (
                self._timing_point_near_x(event.position().x())
                if grabbed_note is None and self.timing_edit_enabled else None
            )
            # A press that is literally on the note's circle moves it, selected
            # or not: that is what grabbing a thing means, and it costs the
            # rubber band only the object's own area -- everything above and
            # below the row still selects. `move_requires_selection` remains the
            # rule off the body, so a drag started in the space beside a note
            # still boxes rather than nudging whatever column it began in.
            on_body = grabbed_note is not None and self._press_is_on_note_body(
                grabbed_note, event.position().x(), event.position().y())
            if self.move_requires_selection and not on_body and not (
                (grabbed_note is not None and grabbed_note.original_index in self.selected)
                or (grabbed_point is not None and grabbed_point.uid in self.selected_timing_uids)
            ):
                grabbed_note = grabbed_point = None
            if grabbed_note is not None or grabbed_point is not None:
                self._begin_move(grabbed_note, grabbed_point, event.position().x())
                event.accept()
                return

        # A left click clears the selection before the drag rebuilds it, so a
        # plain click with no drag deselects rather than silently keeping a
        # selection the user has visibly moved on from. Ctrl adds the band to
        # it instead, osu!'s rule -- and with the select tool Ctrl is free here,
        # since its whole-millisecond meaning only matters where something lands.
        if event.modifiers() & Qt.ControlModifier:
            self._band_base = (set(self.selected), set(self.selected_timing_uids))
        else:
            self._band_base = None
            if self.selected:
                self.selected.clear()
                self.selection_changed.emit(set())
        self.drag_start_x=event.position().x(); self.drag_mouse_x=self.drag_start_x
        self._drag_y = event.position().y()
        self.drag_anchor_time=self.time_for_x(self.drag_start_x); self.grabMouse(); self.auto_scroll_timer.start(); self.update()
    def _begin_move(self, note, point, x: float) -> None:
        """Start a select-tool drag of whatever the click landed on.

        Grabbing something that is part of the current selection moves the whole
        selection; grabbing something outside it moves that alone and leaves the
        selection untouched -- the same rule every editor uses, and the one that
        makes "select a run of barlines, then shift all of them" possible.
        """
        # Whichever bar of a barline note was grabbed, the structure's centre is
        # what moves -- see _gimmick_centre_near_x.
        if point is not None:
            point = self._gimmick_centre_near_x(x) or point
        in_selection = (
            note is not None and note.original_index in self.selected
            or point is not None and point.uid in self.selected_timing_uids
        )
        # The object under the cursor alone, for a Ctrl+click that never drags.
        self._move_clicked = (note, point)
        if in_selection:
            self._move_note_uids = [n.uid for n in self.selected_notes()]
            self._move_point_uids = sorted(self.selected_timing_uids)
        else:
            self._move_note_uids = [note.uid] if note is not None else []
            self._move_point_uids = [point.uid] if point is not None else []
        self._move_origin = float(note.time if note is not None else point.time)
        # A note belongs on the beat grid, so its drag snaps to it -- and so
        # does a barline note, which is a note drawn out of red lines and is
        # placed on the grid to begin with.
        #
        # A plain red line is not: a barline gimmick's whole vocabulary is
        # millisecond offsets -- its restore lines sit +/-2ms from its centre --
        # and snapping those to a 125ms division meant every nudge smaller than
        # a beat rounded back to where it started, which reads as the drag doing
        # nothing. Only the structure's centre snaps; nudging any other line off
        # the grid by hand still works.
        self._move_snaps = note is not None or (
            point is not None and round(point.time) in self.gimmick_times
        )
        # Always the current grid. An object on a grid the setting does not
        # contain used to move along its own instead, so a 1/4 note dragged
        # with 1/3 selected stepped in quarters and never reached a triplet --
        # and changing the snap is how a mapper says where it should go
        # (owner, 2026-10-24; 649 of one map's 1207 notes moved that way).
        self._move_anchor_time = self.time_for_x(x)
        self._move_delta = 0.0
        self.grabMouse()
        self.update()

    def _update_move(self, x: float) -> None:
        """Land the *grabbed* object on a position and shift everything by that.

        Positioning the grabbed object rather than the cursor: otherwise the
        offset would depend on where inside the object the drag started, and a
        structure could never be landed exactly on a division.

        The grid for a note, whole milliseconds for a red line -- see
        `_begin_move`.
        """
        if self._move_origin is None:
            return
        wanted = self._move_origin + (self.time_for_x(x) - self._move_anchor_time)
        target = self.snap_ms(wanted) if self._move_snaps else round(wanted)
        # Nothing may be dragged to a negative time; clamping the delta rather
        # than each object keeps the structure's own shape intact.
        self._move_delta = max(-self._move_origin, target - self._move_origin)
        self.update()

    def _finish_move(self, toggle: bool = False) -> None:
        self.releaseMouse()
        delta = self._move_delta
        note_uids, point_uids = self._move_note_uids, self._move_point_uids
        fallback = self._move_fallback
        self._move_note_uids = []
        self._move_point_uids = []
        self._move_origin = None
        self._move_delta = 0.0
        self._move_fallback = None
        if delta and (note_uids or point_uids):
            self.objects_move_requested.emit(note_uids, point_uids, delta)
        elif fallback is not None:
            # Never moved: this was a click with a placement tool held, so it
            # means what it always meant.
            self.note_place_requested.emit(*fallback)
        elif note_uids or point_uids:
            # A press on an object that never became a drag is a click, and a
            # click selects it -- on every page. Without this, clicking a
            # gimmick object did nothing at all: it could only be selected by
            # dragging a band around it, so Delete had nothing to act on
            # unless you knew that.
            #
            # The Editor page used to *clear* here instead, on the rule that a
            # plain click deselects. That rule belongs to a click on empty
            # space (see mouseReleaseEvent's rubber-band path, which still
            # clears), and applying it to a click on the object itself is what
            # made a note in the regular editor impossible to select the way
            # one in a gimmick layer is -- same widget, same yellow
            # `selected_note_pen`, opposite result from the same gesture.
            # Only this branch is shared; `move_requires_selection` still
            # governs what a press *off* the body grabs, so a drag started
            # beside a note on a dense chart still boxes rather than nudging.
            grabbed = set(note_uids)
            indexes = {
                note.original_index for note in self.notes if note.uid in grabbed
            }
            if toggle:
                # Ctrl+click adds or removes the one object, so picking the
                # K's out of `d kdd K d K` is three clicks. Not the grabbed
                # uids: a press on a selected note grabbed the whole selection.
                note, point = self._move_clicked
                self.selected ^= {note.original_index} if note is not None else set()
                self.selected_timing_uids ^= {point.uid} if point is not None else set()
            else:
                self.selected = indexes
                self.selected_timing_uids = set(point_uids)
            self.selection_changed.emit(set(self.selected))
        self.update()

    def _update_drag_selection(self) -> None:
        if self.drag_anchor_time is None: return
        a,b=sorted((self.drag_anchor_time,self.time_for_x(self.drag_mouse_x)))
        first,last=bisect_left(self.note_times,a),bisect_right(self.note_times,b)
        # A split layer's band selects in the row it started in, so a run of
        # fake sliders can be taken without the shinies a millisecond from
        # them. Shift takes both.
        lower = self._selection_row()
        self.selected={
            n.original_index for n in self.notes[first:last]
            if lower is None or (round(n.time) in self.shiny_times) == lower
        }
        # A layer that owns its red lines selects those too, and only the ones
        # it owns: a line sitting on someone else's object is another layer's.
        if self.timing_edit_enabled:
            self.selected_timing_uids = {
                point.uid
                for point in self.timing_points
                if a <= point.time <= b and self._owns_timing_point(point)
            }
        if self._band_base is not None:
            self.selected |= self._band_base[0]
            self.selected_timing_uids |= self._band_base[1]
        self.selection_changed.emit(set(self.selected)); self.update()
    def mouseMoveEvent(self,event) -> None:
        if self._bar_scrubbing:
            self.current_time = self._time_on_timing_bar(event.globalPosition().toPoint())
            self.seek_requested.emit(self.current_time)
            self.drag_mouse_x = self.x_for_time(self.current_time)
            self._update_drag_selection()
            return
        if self._move_origin is not None:
            self._update_move(event.position().x())
            return
        if self._placing_tool is not None:
            snapped = max(
                self._placing_start_time,
                self.snap_ms(self.time_for_x(event.position().x())),
            )
            self._placing_end_time = snapped
            self.update()
            return
        if self._resizing_note is not None:
            minimum = self._resizing_note.time + 20.0
            snapped = max(minimum, self.snap_ms(self.time_for_x(event.position().x())))
            self._resize_new_end_time = snapped
            self.update()
            return
        if self.drag_anchor_time is not None:
            self.drag_mouse_x=event.position().x(); self._update_drag_selection()
            return
        # Repaint on every move, whatever the tool. The hover *ghost* is only
        # worth drawing for a placement tool, but draw_cursor_position writes
        # the millisecond under the cursor in every one of them -- and with
        # the repaint gated on the tool, that number only refreshed when
        # something else happened to repaint the view, which in select mode
        # meant clicking.
        self._hover_time = self.time_for_x(event.position().x())
        self._hover_y = event.position().y()
        self.update()

    def _selection_row(self, y: float | None = None) -> bool | None:
        """The row `y` (default: the band's start) is in on a split layer --
        True for the lower one -- or None where selection spans the whole view:
        off a split layer, with Shift held, or with no y to go by."""
        y = self._drag_y if y is None else y
        if not self.split_rows or y is None or shift_is_held():
            return None
        return y > self._baseline_y()

    def _row_span(self, y: float | None) -> tuple[float, float]:
        """(top, bottom) of what `_selection_row(y)` covers."""
        lower = self._selection_row(y)
        if lower is None:
            return 0.0, float(self.height())
        middle = float(self._baseline_y())
        return (middle, float(self.height())) if lower else (0.0, middle)

    def cursor_line_span(self) -> tuple[float, float]:
        return self._row_span(self._drag_y if self.drag_anchor_time is not None else self._hover_y)

    def leaveEvent(self, event) -> None:
        """Hand the readout back to the playhead when the cursor leaves."""
        self._hover_time = None
        super().leaveEvent(event)
        self.update()

    def _auto_scroll_selection(self) -> None:
        if self.drag_anchor_time is None: self.auto_scroll_timer.stop(); return
        if self.auto_scroll_step(self.drag_mouse_x): self._update_drag_selection()
    def _delete_selection(self) -> bool:
        """Delete and right click on a selected object. Editor-page views
        only: the shared Fancy Arranger timeline (symmetric=False) is a
        transform-selection surface, where this would destroy notes someone
        was only trying to arrange. False when nothing was deleted."""
        if not self.editor_view or not (self.selected or self.selected_timing_uids):
            return False
        if self.selected:
            self.notes_delete_requested.emit([note.uid for note in self.selected_notes()])
        if self.selected_timing_uids:
            self.timing_lines_delete_requested.emit(sorted(self.selected_timing_uids))
        return True

    def mouseReleaseEvent(self,event) -> None:
        if event.button() == Qt.RightButton:
            # A right release only ever matters here as the end of a bar-scrub
            # (see mousePressEvent above) -- it must never fall through to the
            # generic release logic below, which would end whatever drag a
            # left button is still holding open.
            self._bar_scrubbing = False
            event.accept()
            return
        if self._move_origin is not None:
            self._finish_move(toggle=bool(event.modifiers() & Qt.ControlModifier))
            return
        if self._placing_tool is not None:
            self.releaseMouse()
            tool = self._placing_tool
            start = self._placing_start_time
            end = max(start, self._placing_end_time if self._placing_end_time is not None else start)
            # Combine the event's own modifiers with the global state, same
            # as wheelEvent elsewhere in this class: the global state alone
            # can be stale, and (for synthetic/test events especially) the
            # event's own modifiers may be the only place Shift shows up.
            # The event's own modifiers, never the cached global -- see
            # `shift_is_held`. A tap of Shift used to stick.
            big = bool(event.modifiers() & Qt.ShiftModifier) or self.finisher
            self._placing_tool = None
            self._placing_start_time = None
            self._placing_end_time = None
            self.note_place_with_duration_requested.emit(tool, start, end, self.new_combo, big)
            self.update()
            return
        if self._resizing_note is not None:
            self.releaseMouse()
            note = self._resizing_note
            new_end = self._resize_new_end_time
            self._resizing_note = None
            self._resize_new_end_time = None
            if new_end is not None:
                self.note_duration_edit_requested.emit(note.uid, new_end)
            self.update()
            return
        if self.drag_anchor_time is None: return
        if self.tool in self.RANGE_TOOLS:
            self.releaseMouse()
            start, end = sorted((self.drag_anchor_time, self.time_for_x(event.position().x())))
            if self.tool == "kiai":
                # Snapped here rather than by the handler -- see
                # SVEditorView.mouseReleaseEvent, which owns the same gesture
                # in the Kiai and Sound Volume layer and for the same reason:
                # only the view knows Ctrl means whole milliseconds.
                start, end = self.snap_ms(start), self.snap_ms(end)
            self.drag_start_x = None
            self.drag_anchor_time = None
            self.update()
            self.timing_function_range_requested.emit(start, end)
            return
        self.auto_scroll_timer.stop(); self.releaseMouse()
        # Click-to-seek is a Fancy-Arranger-timeline convenience; Editor-page
        # views (symmetric) must not jump the shared playhead just because
        # someone clicked inside them to select/place/inspect a note.
        if not self.editor_view and abs(event.position().x()-(self.drag_start_x or 0.0))<5:
            self.current_time=self.snap_ms(self.time_for_x(event.position().x())); self.seek_requested.emit(self.current_time)
        self.drag_start_x=None; self.drag_anchor_time=None; self._band_base=None; self.update()
        # Previewing on every mouse-move would be far too expensive, so the
        # expensive refresh is deferred until the drag actually ends.
        self.selection_finalized.emit(set(self.selected))

    def selected_notes(self) -> list:
        """Selected notes in time order, for copy and for Delete."""
        return sorted(
            (note for note in self.notes if note.original_index in self.selected),
            key=lambda note: (note.time, note.uid),
        )

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            # Only swallow Esc when it actually cleared something. With no
            # selection it belongs to the window, which uses it to leave the
            # editor for the song list.
            if self.selected or self.selected_timing_uids:
                self.selected.clear()
                self.selected_timing_uids.clear()
                self.selection_changed.emit(set())
                self.update()
                event.accept()
                return
            super().keyPressEvent(event)
            return

        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            # Editor-page views only. The shared Fancy Arranger timeline
            # (symmetric=False) is a transform-selection surface, where
            # Delete would destroy notes someone was only trying to arrange.
            self._delete_selection()
            event.accept()
            return

        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.selected = {
                note.original_index
                for note in self.notes
            }
            self.selection_changed.emit(set(self.selected))
            self.update()
            return

        super().keyPressEvent(event)

    def _next_timing_time_after(self, time_ms: float) -> float | None:
        """Start of the next timing section strictly after `time_ms`."""
        times = self._snap_times or self._timing_times
        index = bisect_right(times, time_ms)
        return times[index] if index < len(times) else None

    def _draw_snap_grid(
        self,
        painter: QPainter,
        baseline_y: int,
    ) -> None:
        start_time = self.current_time - self.window_ms / 2
        end_time = self.current_time + self.window_ms / 2

        if end_time <= start_time:
            return

        # _tick_styles' heights are pixels at DESIGN_HEIGHT. Sized from the view
        # for the same reason the note radii are: on the gimmick page's short
        # bands a full-size tick growing in from each edge meets the one
        # opposite, and the notes end up drawn on top of a solid grid.
        tick_scale = min(1.0, self.height() / self.DESIGN_HEIGHT)
        divisor = self.snap_divisor
        snap_points = self.snap_beat_points
        timing = active_timing(
            snap_points,
            start_time,
        )
        # The walk below only moves forward, and the timing section it is in
        # changes once or twice in a window -- but it used to binary-search the
        # whole point list again for every single tick. Profiling one gimmick
        # page frame caught 152 of those searches, 30 per grid draw. Hold the
        # section until the walk reaches the next timing point instead.
        #
        # Conservative on purpose: an inherited point expires the section
        # without changing which uninherited one is active, so that case costs
        # a search that returns what we already had -- never a stale section.
        section_expires = -math.inf

        # A gimmick map's "invisible note" points carry beat_length = 0.0001,
        # which puts millions of sub-pixel ticks in one window -- a hard freeze,
        # not a slow frame. Nothing this fine is visible anyway (see
        # `_min_tick_spacing_px`), so it is not drawn.
        min_snap_length = (
            self.window_ms / max(1.0, float(self.width())) * self._min_tick_spacing_px
        )

        # Collected per tick kind and stroked in one drawLines each, rather
        # than setPen + drawLine per tick. Worth about half the raster call
        # (1882 lines: 1.67ms one at a time, 0.89ms batched) and no more --
        # the frame was never in the pen changes, it was in the walk below.
        batches: dict[str, list[QLine]] = {}
        tick_heights = {
            name: round(height * tick_scale)
            for name, (_pen, height) in self._tick_styles.items()
        }

        # Floored with the same minimum: a million-BPM point makes snap_length
        # small enough that the division below overflows to infinity, and
        # int(inf) raises. Only the *starting* tick is affected, and the loop
        # skips those sections whole anyway.
        snap_length = max(timing.beat_length / divisor, min_snap_length)
        # The divisor actually drawn, coarsened per section so the grid stays
        # legible instead of turning into fill. Measured on nbt-hwt's hidden
        # anti-barline (a 12345 BPM wall): a 1/4 grid there is 1.15px apart,
        # 1422 ticks a frame, 15.3ms of a 16.3ms chart paint against an 8.33ms
        # budget -- and every one of those ticks lands on a pixel its
        # neighbour already covered. Only ever a divisor *of* the setting, so
        # what is drawn is a subset of the real grid rather than a different
        # one.
        grid_divisor = divisor

        tick_time = (
            timing.time
            + int(
                (start_time - timing.time)
                // snap_length
            )
            * snap_length
        )

        # Everything below is per tick, and a heavy gimmick section still has
        # ~470 of them per band after the coarsening above -- a band paint was
        # 3.1ms, the grid most of it (tools/measure_wheel_seek.py --gimmick
        # --from 117000 --profile, Nbt-Hwt's hidden anti-barline wall). So
        # what only changes per *section* is worked out per section: which
        # kind each position in the beat is, and the lines each kind draws
        # into. Locals rather than attributes for the same reason.
        x_for_time = self.x_for_time
        right_edge = float(self.width() + 1)
        height = self.height()
        split_rows, symmetric = self.split_rows, self.symmetric
        kinds: list[tuple[list[QLine], int]] = []
        # Every whole beat also gets a faint line the full height of the view,
        # under the ticks, as in the approved mockup: the ticks stay where each
        # layer wants them (edges, or the middle on a split layer) and the line
        # is what the eye follows down through the notes. Identity, not a
        # second lookup: `kinds` below hands out this very list for "beat".
        beat_lines = batches.setdefault("beat", [])
        full_beats: list[QLine] = []

        while tick_time <= end_time + snap_length:
            at = tick_time + 0.001
            if at >= section_expires:
                # -inf is the walk's first section, not a red line it crossed.
                crossed = section_expires != -math.inf
                timing = active_timing(snap_points, at)
                following = self._next_timing_time_after(tick_time)
                section_expires = math.inf if following is None else following
                grid_divisor = divisor
                for candidate in divisors_descending(divisor):
                    if timing.beat_length / candidate >= min_snap_length:
                        grid_divisor = candidate
                        break
                kinds = []
                for position in range(grid_divisor):
                    kind = tick_kind(position, grid_divisor)
                    lines = batches.get(kind)
                    if lines is None:
                        lines = batches[kind] = []
                    kinds.append((lines, tick_heights[kind]))
                # Restart the walk on this section's own red line. Carrying the
                # previous section's phase across it drew every tick after an
                # off-grid red line where the *old* timing said, not where a
                # note snaps (`snap_time` anchors on the red line) -- and since
                # the phase came from whichever section the window opened in,
                # the grid after the line shifted as the view scrolled.
                # Counted from the last tick drawn, not this one: this one has
                # already overshot the red line, and starting from it skipped
                # the section's own downbeat -- the white beat tick on the line.
                # Only on a line actually crossed: the walk's first section is
                # already phased on its own red line, and restarting there
                # jumped a view opening before a map's first red line straight
                # to it, so the grid to its left vanished.
                section_snap = timing.beat_length / grid_divisor
                if crossed and section_snap >= min_snap_length:
                    drawn = tick_time - snap_length
                    tick_time = timing.time + max(0, math.floor(
                        (drawn - timing.time) / section_snap + 1e-6
                    ) + 1) * section_snap

            snap_length = timing.beat_length / grid_divisor
            if snap_length < min_snap_length:
                next_section = self._next_timing_time_after(tick_time)
                if next_section is None or next_section > end_time:
                    break
                tick_time = next_section
                continue

            lines, tick_height = kinds[round((tick_time - timing.time) / snap_length) % grid_divisor]
            # Clamped to just off either edge before it reaches Qt: drawLine
            # takes a C int, and one 0.00001 BPM section puts the tick after
            # this one billions of pixels out. Handing that over raises
            # OverflowError *inside* paintEvent, which aborts the whole frame --
            # grid, notes and all -- and looks like the map vanishing.
            #
            # Drawn at `osu_snap_ms(tick_time)`, not at the exact fractional
            # beat: objects snap to a whole millisecond and osu! takes that one
            # *down* (see `osu_snap_ms`), so a grid drawn at the unrounded
            # position sits up to a whole millisecond off the note it is meant
            # to mark. The walk itself still steps by the exact `snap_length` --
            # only the drawn position is snapped, never accumulated back into
            # `tick_time`, which keeps the grid from drifting off its own beats
            # across a long section.
            x = round(max(-1.0, min(right_edge, x_for_time(osu_snap_ms(tick_time)))))
            if lines is beat_lines:
                full_beats.append(QLine(x, 0, x, height))

            if split_rows:
                # Straddling the middle instead of growing in from the edges:
                # this layer's objects are up against the top and bottom, and
                # ticks there were drawn through them. See `_row_y`.
                lines.append(QLine(x, baseline_y - tick_height // 2, x, baseline_y + tick_height // 2))
            elif symmetric:
                # Notes alone stay centered on the baseline; ticks anchor to
                # the view's top and bottom edges and grow inward, framing
                # the notes rather than straddling the middle with them.
                lines.append(QLine(x, 0, x, tick_height))
                lines.append(QLine(x, height, x, height - tick_height))
            else:
                lines.append(QLine(x, baseline_y, x, baseline_y - tick_height))

            tick_time += snap_length

        if full_beats:
            painter.setPen(self.beat_line_pen)
            painter.drawLines(full_beats)
        for kind, lines in batches.items():
            if lines:
                painter.setPen(self._tick_styles[kind][0])
                painter.drawLines(lines)

    def _owns_timing_point(self, point: TimingPoint) -> bool:
        """Whether this view's tools act on `point`, or merely show it.

        Ownership is decided by which *structure* a line belongs to, not by
        whether some hit object shares its millisecond. It used to be the
        latter, which was true only while a barline note wrote no note of its
        own: once it did, its middle line -- the 60000 BPM line that is the
        whole gimmick -- landed on that note and became untouchable in the one
        layer that exists to touch it.
        """
        return round(point.time) not in self.foreign_times

    def _visible_timing_points(self):
        """The document's red lines inside the window, at most one per pixel.

        A barline gimmick puts thousands of uninherited points in one screen,
        most of them on the same pixel column. Drawing every one is a line per
        point per frame for a result indistinguishable from drawing one.
        """
        if not self.show_timing_lines or not self.timing_points:
            return
        start_time = self.current_time - self.window_ms / 2
        end_time = self.current_time + self.window_ms / 2
        first = bisect_left(self._timing_times, start_time)
        after_last = bisect_right(self._timing_times, end_time)
        previous: tuple[int, bool] | None = None
        for point in self.timing_points[first:after_last]:
            if self.timing_line_times is not None and round(point.time) not in self.timing_line_times:
                continue
            owned = self._owns_timing_point(point)
            x = round(self.x_for_time(point.time))
            if previous == (x, owned):
                continue
            previous = (x, owned)
            yield point, x, owned

    def _draw_timing_lines(self, painter: QPainter) -> None:
        # Same two-places-at-once rule the dragged notes follow: a line being
        # moved is drawn at its destination, and faintly where it came from.
        moving = set(self._move_point_uids) if self._move_origin is not None else frozenset()
        # Labels are spaced the same way the SV editor spaces its own: a
        # barline gimmick stacks thousands of lines in one window, and a number
        # beside each of them is a solid grey block.
        last_label_x = -1e9
        for point, x, owned in self._visible_timing_points():
            if point.uid in moving:
                painter.setPen(self.timing_line_ghost_pen)
                painter.drawLine(x, 0, x, self.height())
                x = round(self.x_for_time(point.time + self._move_delta))
            if point.uid in self.selected_timing_uids:
                painter.setPen(self.selected_note_pen)
            else:
                painter.setPen(self.timing_line_pen if owned else self.timing_line_ref_pen)
            painter.drawLine(x, 0, x, self.height())
            if (
                self.show_bpm_labels and owned and point.bpm
                and x - last_label_x >= SV_LABEL_MIN_SPACING_PX
            ):
                last_label_x = x
                painter.save()
                painter.setFont(self.label_font)
                painter.setPen(self.bpm_label_pen)
                painter.drawText(QPointF(x + 4, self.height() - 4), f"{point.bpm:.0f} BPM")
                painter.restore()

    def _gimmick_centre_near_x(self, x: float, radius_px: float = 12.0):
        """The owned gimmick line in the same pixel neighbourhood as `x`, if any.

        A barline note is up to seven red lines inside twelve milliseconds. At
        any zoom you can read a chart at they are the same pixel column, so
        "the nearest line" picks between its centre and its restores by
        rounding error -- and only the centre is the object. Right click and
        drag both ask for it here, so grabbing a barline note means the same
        thing wherever inside it the cursor was.

        `<=`, not `<`, on the tie itself: two points can legitimately share one
        exact millisecond (a converter's green line stacked on the chart's own,
        deliberately never deduped -- see `_convert_to_anti_barline`), and
        `self.timing_points` is stable-sorted from insertion order, so the last
        of an exact tie is the one osu! actually honours. `<` kept whichever
        was inserted first -- the superseded one -- so a click there grabbed
        and showed a dead point while the effective one sat untouched under it.
        """
        best = None
        best_distance = None
        for point in self.timing_points:
            if round(point.time) not in self.gimmick_times or not self._owns_timing_point(point):
                continue
            distance = abs(self.x_for_time(point.time) - x)
            if distance <= radius_px and (best_distance is None or distance <= best_distance):
                best = point
                best_distance = distance
        return best

    def _timing_point_near_x(self, x: float, radius_px: float = 12.0):
        """The owned red line nearest `x`, for the double-click BPM edit.

        Restricted to the lines this view actually draws. A layer that filters
        them (`timing_line_times`, the fake slider layer) would otherwise hand
        back a line that is not on screen at all -- the chart's own timing, or
        a barline gimmick's -- and open a dialog for something invisible.

        `<=` on the tie: see `_gimmick_centre_near_x` on why the last of an
        exact-millisecond tie, not the first, is the point actually in force.
        """
        best = None
        best_distance = None
        for point in self.timing_points:
            if self.timing_line_times is not None and round(point.time) not in self.timing_line_times:
                continue
            if not self._owns_timing_point(point):
                continue
            distance = abs(self.x_for_time(point.time) - x)
            if distance <= radius_px and (best_distance is None or distance <= best_distance):
                best = point
                best_distance = distance
        return best

    def mouseDoubleClickEvent(self, event) -> None:
        """Double click a red line to retype its BPM, where that is allowed.

        Reached before mousePressEvent's tool handling gets a second click, so a
        placement tool being active does not swallow it.
        """
        if event.button() != Qt.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        # The object before the line under it, the same order mousePressEvent
        # resolves a grab in: on layer 2 the fake slider and the red line that
        # squashes it are a millisecond apart, which is one pixel column at any
        # zoom the layer is readable at, and the line is the half you did not
        # click on.
        if self.note_length_dialog_enabled:
            note = self._note_near_x(event.position().x(), y=event.position().y())
            if note is not None and note.length is not None:
                self.note_length_edit_requested.emit(note.uid)
                event.accept()
                return
        if not self.timing_dialog_enabled:
            super().mouseDoubleClickEvent(event)
            return
        point = self._timing_point_near_x(event.position().x())
        if point is None or point.bpm is None:
            super().mouseDoubleClickEvent(event)
            return
        self.timing_line_edit_requested.emit(point.uid)
        event.accept()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), view_ground("#151b24"))
        self.draw_kiai_bands(painter)

        baseline_y = self._baseline_y()
        normal_note_radius, finisher_note_radius = self.note_radii()

        self._draw_snap_grid(painter, baseline_y)
        self._draw_timing_lines(painter)

        painter.setPen(self.baseline_pen)
        painter.drawLine(0, baseline_y, self.width(), baseline_y)

        start_time = self.current_time - self.window_ms / 2
        end_time = self.current_time + self.window_ms / 2

        # Only visit notes inside the visible window instead of scanning the
        # complete beatmap on every audio-position update. Widened on the left
        # by the longest slider/spinner duration so a long note whose start
        # has scrolled off-screen still gets drawn while its end is visible.
        first_visible = bisect_left(self.note_times, start_time - self._max_extend_ms)
        after_last_visible = bisect_right(self.note_times, end_time)

        if self.drag_anchor_time is not None:
            anchor_x=self.x_for_time(self.drag_anchor_time); current_x=max(0.0,min(float(self.width()),self.drag_mouse_x))
            left,width=min(anchor_x,current_x),abs(current_x-anchor_x)
            top, bottom = self._row_span(self._drag_y)
            painter.fillRect(QRectF(left,top,width,bottom-top),QColor(190,195,205,28))
            painter.setPen(QPen(QColor(220,225,235,70),1)); painter.drawRect(QRectF(left,top,width,bottom-top-1))
        painter.setRenderHint(QPainter.Antialiasing, True)

        # Notes being dragged are drawn where they are *going*, with a
        # translucent copy left where they came from -- see _draw_move_ghost.
        # Without the preview, a drag showed the object standing still and only
        # a marker line moving, so there was nothing to line the object up with.
        moving_notes = set(self._move_note_uids) if self._move_origin is not None else frozenset()

        for note in self.notes[first_visible:after_last_visible]:
            x = self.x_for_time(note.time)
            # A dragged note is previewed at its destination, so its body (a
            # slider bar, a spinner band) has to travel with its head.
            shift = 0.0
            if note.uid in moving_notes:
                self._draw_move_ghost(painter, note, x, baseline_y, normal_note_radius, finisher_note_radius)
                shift = self._move_delta
                x = self.x_for_time(note.time + shift)
            # Spinners always render at the finisher/"big note" size,
            # independent of the finisher hitsound bit -- a plain visual
            # convention for this app, not a gameplay difference.
            radius = (
                finisher_note_radius
                if (note.is_finisher or note.is_spinner)
                else normal_note_radius
            )
            shiny = self.split_rows and round(note.time) in self.shiny_times
            note_center_y = self.note_center_y(note, baseline_y, radius)
            note_pen = self.selected_note_pen if note.original_index in self.selected else self.normal_note_pen

            if note.is_spinner:
                # A spinner is a duration, but only its start had any visual
                # weight, so where it *ended* was invisible unless you dragged
                # its edge and watched the ghost. Grey band from start to end,
                # with a cap at each edge.
                spinner_end = self._note_end_time(note)
                if spinner_end is not None and spinner_end > note.time:
                    end_x = self.x_for_time(spinner_end + shift)
                    band = QRectF(x, note_center_y - radius * 0.5, end_x - x, radius)
                    painter.setBrush(self.spinner_band_brush)
                    # Selected, the band is outlined as well as the head: the
                    # whole duration is what a click selects.
                    painter.setPen(note_pen if note.original_index in self.selected else Qt.NoPen)
                    painter.drawRoundedRect(band, radius * 0.25, radius * 0.25)
                    painter.setPen(self.spinner_edge_pen)
                    for edge_x in (x, end_x):
                        painter.drawLine(
                            QPointF(edge_x, note_center_y - radius * 0.7),
                            QPointF(edge_x, note_center_y + radius * 0.7),
                        )
                pixmap = spinner_pixmap()
                if not pixmap.isNull():
                    size = radius * 2
                    target = QRectF(x - radius, note_center_y - radius, size, size)
                    painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
                    if note.original_index in self.selected:
                        painter.setBrush(Qt.NoBrush)
                        painter.setPen(note_pen)
                        painter.drawEllipse(QPointF(x, note_center_y), radius, radius)
                    continue

            # A fake slider is a head and nothing else here. It has no body to
            # cap -- its end precedes its start, or trails it by a rounding
            # artefact -- but `_draw_skinned_roll_body` stamps `taiko-roll-end`
            # at `body_end` whether or not a track got drawn, so every fake
            # slider in the editor wore a cap butted against its own head. The
            # scrolling body belongs to the gameplay preview, which draws the
            # extent to scale; this view has no room to say anything but where
            # the object is.
            if note.is_slider and not is_fake_slider(note):
                end_time = self._note_end_time(note)
                end_x = x
                if end_time is not None and end_time > note.time:
                    end_x = self.x_for_time(end_time + shift)
                # A drumroll is `taikohitcircle` + `taiko-roll-middle` +
                # `taiko-roll-end`, here as well as in the gameplay preview: the
                # skin is meant to reach every view that draws a note, and a
                # timeline that drew its own bar beside a skinned head was the
                # one place the two disagreed. The head itself is drawn by the
                # shared call below, so this is the body and its cap.
                if not self._draw_skinned_roll_body(
                    painter, x, end_x, note_center_y, radius
                ) and end_x > x:
                    bar = QRectF(x, note_center_y - radius * 0.4, end_x - x, radius * 0.8)
                    painter.setBrush(self.slider_brush)
                    painter.setPen(Qt.NoPen)
                    painter.drawRoundedRect(bar, radius * 0.4, radius * 0.4)

            # Semi-transparent fill keeps the snap grid visible through notes;
            # with a skin loaded that alpha becomes painter opacity so the art
            # stays see-through too. See draw_note_sprite.
            draw_note_sprite(
                painter,
                self.shiny_brush if shiny
                else self.slider_brush if note.is_slider
                else (self.kat_brush if note.is_kat else self.don_brush),
                note_pen,
                x,
                note_center_y,
                radius,
                self.skin,
                big=note.is_finisher,
                selected=note.original_index in self.selected,
            )
            if note.is_nan_slider:
                # Invisible in osu! and the gameplay preview; marked here so
                # it can be found and edited.
                painter.save()
                font = QFont(painter.font())
                font.setPixelSize(max(6, round(radius * 0.6)))
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(
                    QRectF(x - radius, note_center_y - radius, radius * 2, radius * 2),
                    Qt.AlignCenter, "NaN")
                painter.restore()

        self._draw_placement_ghost(painter, baseline_y, normal_note_radius, finisher_note_radius)
        self.draw_ctrl_precision_line(painter)
        self.draw_cursor_position(painter)

        cursor_x = self.width() // 2
        painter.setPen(self.cursor_pen)
        if self.symmetric:
            painter.drawLine(cursor_x, 0, cursor_x, self.height())
        else:
            painter.drawLine(
                cursor_x,
                baseline_y - 92,
                cursor_x,
                baseline_y + 7,
            )

    def _baseline_y(self) -> float:
        """The y objects are centred on. Bottom-anchored views draw lower."""
        return self.height() // 2 if self.symmetric else self.height() // 2 + 34

    def note_center_y(self, note, baseline_y: float, radius: float) -> float:
        """The y `note` is drawn centred on. Shared with the painter, so what
        the cursor can land on is what the eye can see."""
        if self.split_rows:
            return self._row_y(baseline_y, round(note.time) in self.shiny_times)
        return baseline_y if self.symmetric else baseline_y - radius

    def _press_is_on_note_body(self, note, x: float, y: float) -> bool:
        """Whether (x, y) is inside the circle drawn for `note`.

        `_note_near_x` deliberately ignores y off a split layer -- a right
        click anywhere in the band should still delete what is in that column.
        Starting a *move* is the one thing that has to be stricter: on a normal
        chart every column has a note in it, so a press that begins a
        rubber-band selection would grab one instead. Requiring the press to be
        on the object itself leaves the whole height above and below the row
        for selecting.
        """
        normal_radius, finisher_radius = self.note_radii()
        radius = finisher_radius if (note.is_finisher or note.is_spinner) else normal_radius
        centre_y = self.note_center_y(note, self._baseline_y(), radius)
        dx = self.x_for_time(note.time) - x
        dy = centre_y - y
        if note.is_spinner and self._on_spinner_band(note, x) and abs(dy) <= radius * 0.5:
            return True  # the band, the same rect the painter draws
        return dx * dx + dy * dy <= radius * radius

    def _row_y(self, baseline_y: float, lower: bool) -> float:
        """Centre of the upper or lower object row on a split layer.

        A quarter of the band either side of the middle: the snap ticks are
        drawn across the middle, so the rows sit clear of them rather than
        being cut in half by the grid.
        """
        offset = self.height() * 0.24
        return baseline_y + offset if lower else baseline_y - offset

    def _draw_move_ghost(
        self, painter: QPainter, note, x: float, baseline_y: int,
        normal_radius: float, finisher_radius: float,
    ) -> None:
        """Translucent copy of a dragged note, where it started.

        The drag is only committed on release, so during it the object needs to
        say two things at once: where it is going (the solid sprite, drawn at
        the destination) and where it would go back to if the drag were
        abandoned. The same ghost brushes the placement preview uses, so a
        faded object means the same thing throughout the view.
        """
        radius = finisher_radius if (note.is_finisher or note.is_spinner) else normal_radius
        shiny = self.split_rows and round(note.time) in self.shiny_times
        if note.is_spinner:
            # Its own art, faded, as the placement ghost draws it. Through the
            # brush below it came out a yellow drumroll head.
            pixmap = spinner_pixmap()
            if not pixmap.isNull():
                centre_y = self.note_center_y(note, baseline_y, radius)
                painter.setOpacity(0.45)
                painter.drawPixmap(
                    QRectF(x - radius, centre_y - radius, radius * 2, radius * 2), pixmap, QRectF(pixmap.rect()))
                painter.setOpacity(1.0)
                return
        brush = (
            self.ghost_shiny_brush if shiny
            else self.ghost_slider_brush if (note.is_slider or note.is_spinner)
            else self.ghost_kat_brush if note.is_kat
            else self.ghost_don_brush
        )
        draw_note_sprite(
            painter, brush, self.ghost_pen, x,
            self._row_y(baseline_y, shiny) if self.split_rows
            else baseline_y if self.symmetric
            else baseline_y - radius,
            radius, self.skin, big=note.is_finisher,
        )

    def _draw_extend_ghost(
        self, painter: QPainter, baseline_y: int, normal_radius: float, finisher_radius: float,
        tool: str, start_time: float, end_time: float, big: bool,
    ) -> None:
        """Translucent preview of a slider/spinner being placed or resized."""
        radius = finisher_radius if (big or tool == "spinner") else normal_radius
        note_center_y = baseline_y if self.symmetric else baseline_y - radius
        x1 = self.x_for_time(start_time)
        x2 = self.x_for_time(max(start_time, end_time))

        if tool == "spinner":
            pixmap = spinner_pixmap()
            if not pixmap.isNull():
                painter.setOpacity(0.45)
                size = radius * 2
                painter.drawPixmap(QRectF(x1 - radius, note_center_y - radius, size, size), pixmap, QRectF(pixmap.rect()))
                painter.setOpacity(1.0)
            if x2 > x1:
                painter.setPen(QPen(theme.screen_rgba(255, 255, 255, 110), 3))
                painter.drawLine(QPointF(x1, note_center_y), QPointF(x2, note_center_y))
            return

        if x2 > x1:
            bar = QRectF(x1, note_center_y - radius * 0.4, x2 - x1, radius * 0.8)
            painter.setBrush(self.ghost_slider_brush)
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(bar, radius * 0.4, radius * 0.4)
        painter.setBrush(self.ghost_slider_brush)
        painter.setPen(self.ghost_pen)
        painter.drawEllipse(QPointF(x1, note_center_y), radius, radius)

    def _draw_placement_ghost(self, painter: QPainter, baseline_y: int, normal_radius: float, finisher_radius: float) -> None:
        # Paint time, so there is no event to read: the live hardware state
        # rather than Qt's cached one, or a stale Shift bit draws every
        # ghost at finisher size. See `shift_is_held`.
        shift_held = shift_is_held() or self.finisher

        if self._placing_tool in ("slider", "spinner") and self._placing_start_time is not None:
            self._draw_extend_ghost(
                painter, baseline_y, normal_radius, finisher_radius, self._placing_tool,
                self._placing_start_time, self._placing_end_time or self._placing_start_time, shift_held,
            )
            return
        if self._resizing_note is not None and self._resize_new_end_time is not None:
            note = self._resizing_note
            tool = "spinner" if note.is_spinner else "slider"
            self._draw_extend_ghost(
                painter, baseline_y, normal_radius, finisher_radius, tool,
                note.time, self._resize_new_end_time, note.is_finisher,
            )
            return
        # "regular" is the fake slider layer's own tool -- it writes a shape
        # rather than a note, so it previews through _draw_layer_ghost below.
        # Without it here the one tool whose whole output is a drawn object was
        # the one tool that showed nothing under the cursor.
        if self.tool not in ("don", "kat", "regular", "shiny", "multi", "slider", "spinner") or self._hover_time is None:
            return

        snapped = self.snap_ms(self._hover_time)
        x = self.x_for_time(snapped)
        # "multi" places a run either side of the click, so its own x can be off
        # screen while the run is not; it does its own bounds check per ghost.
        if self.tool != "multi" and (x < -60 or x > self.width() + 60):
            return

        if self.tool == "multi":
            self._draw_multi_run_ghost(painter, baseline_y, normal_radius, finisher_radius, snapped, shift_held)
            return

        if self.tool == "slider":
            self._draw_extend_ghost(painter, baseline_y, normal_radius, finisher_radius, "slider", snapped, snapped, shift_held)
            return

        radius = finisher_radius if (self.tool == "spinner" or shift_held) else normal_radius
        note_center_y = baseline_y if self.symmetric else baseline_y - radius

        if self.ghost_style != "note" and self.tool in ("don", "kat", "regular", "shiny"):
            # Don is the small variant and Kat the big one in both layers, the
            # way the finisher bit decides size in a normal chart -- here the
            # tool decides it, since these structures carry no hitsound of their
            # own until they are written. A plain fake slider and a shiny have
            # no such pair, so Shift picks their size the same way it does for a
            # Don or Kat in a normal chart -- see gimmick_session.fake_slider.
            shift_sizes = shift_held and self.tool in ("regular", "shiny")
            self._draw_layer_ghost(
                painter, x, baseline_y,
                finisher_radius if (self.tool == "kat" or shift_sizes) else normal_radius,
            )
            return
        if self.tool == "spinner":
            pixmap = spinner_pixmap()
            if not pixmap.isNull():
                painter.setOpacity(0.45)
                size = radius * 2
                painter.drawPixmap(QRectF(x - radius, note_center_y - radius, size, size), pixmap, QRectF(pixmap.rect()))
                painter.setOpacity(1.0)
                return
            brush = theme.screen_rgba(255, 255, 255, 70)
        else:
            brush = self.ghost_kat_brush if self.tool == "kat" else self.ghost_don_brush
        painter.setBrush(brush)
        painter.setPen(self.ghost_pen)
        painter.drawEllipse(QPointF(x, note_center_y), radius, radius)

    def _draw_multi_run_ghost(
        self, painter: QPainter, baseline_y: int, normal_radius: float,
        finisher_radius: float, snapped: float, shift_held: bool,
    ) -> None:
        """The Multiple Fake Slider tool's preview: the whole run, not one dot.

        The tool writes N structures at `click+start .. click+end` stepped by
        `distance`, so a single ghost under the cursor said nothing about what
        a click was about to place -- and Shift, which sizes every one of them,
        showed nothing at all because "multi" was missing from the tool list in
        `_draw_placement_ghost`.

        Positions are the tool's own (`_multi_fake_slider_commands`), plus the
        `fake_slider_offset_ms` that puts each drawn object one offset after
        its structure -- the ghost stands where the slider lands, not where the
        structure is anchored. Shift sizes them the same way it sizes a
        Don or Kat in a normal chart; see `gimmick_session.fake_slider`, which
        honours `big` for `kind="regular"`.
        """
        if not self.multi_run:
            return
        start, end, distance, offset = self.multi_run
        if distance <= 0:
            return
        radius = finisher_radius if shift_held else normal_radius
        position = start
        while position <= end:
            x = self.x_for_time(snapped + position + offset)
            # Per ghost, because a long run reaches well past both edges.
            if -60 <= x <= self.width() + 60:
                if self.ghost_style != "note":
                    self._draw_layer_ghost(painter, x, baseline_y, radius)
                else:
                    painter.setBrush(self.ghost_don_brush)
                    painter.setPen(self.ghost_pen)
                    y = baseline_y if self.symmetric else baseline_y - radius
                    painter.drawEllipse(QPointF(x, y), radius, radius)
            position += distance

    def _draw_layer_ghost(self, painter: QPainter, x: float, baseline_y: float, radius: float) -> None:
        """Don/Kat previewed as what *this layer* writes, not as a note.

        Same translucency as the normal chart's ghost -- what changes is the
        shape, because the structure does:

        * **fake_slider** -- the yellow drumroll body a fake slider is drawn as.
        * **barline** -- a bar, since a run of red lines is what that layer
          writes and a circle would promise a note that is not there. Kat's
          three mirrored pairs make it the wider bar to Don's one.
        """
        # A shiny previews white and on the lower row, which is exactly where it
        # will be drawn once written -- the two tools in this layer place the
        # same shape a millisecond apart, so the row is the only thing under the
        # cursor that says which one is about to happen.
        shiny = self.tool == "shiny"
        painter.setBrush(self.ghost_shiny_brush if shiny else self.ghost_slider_brush)
        painter.setPen(self.ghost_pen)
        if self.ghost_style == "fake_slider":
            y = self._row_y(baseline_y, shiny) if self.split_rows else baseline_y
            painter.drawEllipse(QPointF(x, y), radius, radius)
            return
        half_width = max(2.0, radius * 0.22)
        painter.drawRect(QRectF(x - half_width, baseline_y - radius, half_width * 2, radius * 2))


# Minimum horizontal gap between two SV value labels on the graph. A generated
# sweep can put hundreds of points in one screen; labelling every one is both
# unreadable and a drawText per point per frame.
SV_LABEL_MIN_SPACING_PX = 46.0


# The editor timeline's own finisher ratio: a normal note is 75% of a big one.
# Deliberately not the ruleset's 1.538x (which is 65%) -- see
# `TimelineGameplay.note_radii`. Between the two: far enough apart that a
# finisher reads as one at a glance, close enough that the normal note does not
# have to shrink for it. The gameplay preview and the gimmick bands are
# unaffected and keep the ruleset's own number.
EDITOR_STRONG_SCALE = 1.0 / 0.75


def timeline_bar_data(document, duration_ms: int):
    """Everything a TimingOverviewBar draws, derived once.

    Three bars exist -- one per page -- and they all show the same document, so
    this is computed once per edit and applied to each of them rather than each
    walking the whole map for itself. Returned as a plain tuple so a bar can
    adopt it without knowing where it came from.
    """
    duration = max(duration_ms, document.hit_objects[-1].time if document.hit_objects else 1, 1)
    kiai = kiai_ranges(document, duration)
    bookmarks, preview_time = editor_timeline_metadata(document)
    markers = [(round(point.time), point.uninherited) for point in document.timing_points]
    # Grouped once per edit, not per frame: a gimmick map carries tens of
    # thousands of points and the bar repaints on every clock tick.
    groups: dict[int, set] = {}
    for time_ms, uninherited in markers:
        groups.setdefault(time_ms, set()).add(uninherited)
    kinds = sorted(
        (time_ms, "yellow" if len(flags) > 1 else "red" if True in flags else "green")
        for time_ms, flags in groups.items()
    )
    return duration, kiai, markers, bookmarks, preview_time, kinds


class TimingOverviewBar(QWidget):
    seek_requested = Signal(int)
    def __init__(self) -> None:
        super().__init__()
        self.duration_ms=1; self.current_time=0; self.viewport_start=0; self.viewport_end=0
        self.kiai=[]; self.timing_markers=[]; self._marker_kinds=[]; self.bookmarks=[]; self.preview_time=None; self.dragging=False
        self._marker_cache_key=None; self._marker_lines=[]; self._last_seek_ms=None
        # Everything but the playhead, the viewport box and the preview line is
        # fixed for the life of a document, so it is painted once into a pixmap
        # rather than 120 times a second. Same idiom as DensityOverview below.
        self.static_layer=QPixmap(); self.static_layer_dirty=True
        # Two sources say how long the bar is: the document (its last object,
        # via timeline_bar_data) and the decoded track (set_duration). They
        # differ -- 170343 against 170793ms on can you hear my voice -- and each
        # used to overwrite the other, so every edit squeezed the bar to the
        # document's length and the info timer stretched it back: dragging a
        # green line made every marker jump sideways and back on each step.
        # Both are kept and the longer one drawn.
        self._document_ms=1; self._media_ms=0
        self.setFixedHeight(28); self.setCursor(Qt.PointingHandCursor)
    def load_document(self,document,duration_ms:int)->None:
        self.apply_document_data(timeline_bar_data(document,duration_ms))

    def apply_document_data(self,data)->None:
        """Adopt data already derived by `timeline_bar_data`.

        Split out because three of these bars exist -- one per page -- all
        showing the same document. Each one deriving it for itself walked every
        timing point in the map three times over on every edit.
        """
        self._document_ms,self.kiai,self.timing_markers,self.bookmarks,self.preview_time,self._marker_kinds=data
        self.duration_ms=max(self._document_ms,self._media_ms)
        self._marker_cache_key=None
        self.static_layer_dirty=True
        self.update()

    def set_duration(self,value:int)->None:
        # Guarded on an actual change, like DensityOverview's. The duration is
        # re-asserted by the timeline info refresh many times a second, and
        # dirtying the static layer each time rebuilt ~1150 marker lines for a
        # value that had not moved -- which is exactly the cost that layer
        # exists to avoid.
        if value<=0:return
        self._media_ms=value
        duration=max(self._document_ms,value)
        if duration!=self.duration_ms:
            self.duration_ms=duration;self.static_layer_dirty=True;self.update()
    def set_time(self,value:int)->None:self.current_time=max(0,min(value,self.duration_ms));self.update()
    def set_viewport(self,center:int,window_ms:float)->None:
        self.viewport_start=max(0,center-window_ms/2);self.viewport_end=min(self.duration_ms,center+window_ms/2);self.update()
    def _seek(self,x:float)->None:
        """Emit a seek, but only for a millisecond that is not already in force.

        One plain click is a press *and* a release at the same pixel, and both
        used to emit. The audio engine coalesces a burst (`SEEK_COALESCE_MS`),
        so the duplicate was not dropped but *parked*: it landed 50ms later as
        a second full sink teardown and rebuild, which is the one hiccup heard
        just after every click on the bar. A slow drag that stays inside one
        millisecond was paying the same way.
        """
        value=round(max(0,min(1,x/max(1,self.width())))*self.duration_ms)
        if value==self._last_seek_ms:return
        self._last_seek_ms=value;self.seek_requested.emit(value)
    def _marker_line_positions(self):
        """(x, kind) per pixel column, cached until the bar or the map changes.

        The bar is a few hundred pixels wide, so a gimmick map's thousands of
        markers collapse onto the same handful of columns -- recomputing that
        every clock tick was the widget's whole paint cost.

        A column holding both kinds is yellow, the same rule `timeline_bar_data`
        applies to two points at the same millisecond. Kept per-column and not
        per-kind because the paint loop draws green last, so a red point sharing
        a pixel with an inherited one was overpainted and read as green.
        """
        key=(self.width(),self.duration_ms,len(self._marker_kinds))
        if key!=self._marker_cache_key:
            columns={}
            for time_ms,kind in self._marker_kinds:
                x=round(time_ms/self.duration_ms*self.width())
                prior=columns.get(x)
                columns[x]=kind if prior is None or prior==kind else "yellow"
            self._marker_cache_key=key;self._marker_lines=sorted(columns.items())
        return self._marker_lines
    def mousePressEvent(self,event)->None:
        # Cleared first so a second click on the same pixel still seeks:
        # playback has moved on since, so it is a real request to go back.
        if event.button()==Qt.LeftButton:self.dragging=True;self._last_seek_ms=None;self._seek(event.position().x())
    def mouseMoveEvent(self,event)->None:
        if self.dragging:self._seek(event.position().x())
    def mouseReleaseEvent(self,event)->None:
        if self.dragging:self._seek(event.position().x())
        self.dragging=False
    def resizeEvent(self,event)->None:
        self.static_layer_dirty=True
        super().resizeEvent(event)
    def _rebuild_static_layer(self)->None:
        """The bar minus the three things that move.

        A gimmick map puts a marker on most of the bar's ~1000 pixel columns,
        and each one is a setPen and a drawLine -- measured at 1150 of each per
        frame, 1.29ms, on a 28-pixel-tall widget that cost more than the full
        timeline above it. None of it changes while the song plays.
        """
        if self.width()<=0 or self.height()<=0:return
        layer=QPixmap(self.size());layer.fill(theme.screen("#0d1219"))
        painter=QPainter(layer);painter.setRenderHint(QPainter.Antialiasing,False)
        center=self.height()//2
        kiai_height=max(3,self.height()//3);kiai_top=center-kiai_height//2
        painter.setPen(Qt.NoPen)
        for start,end in self.kiai:
            x=start/self.duration_ms*self.width();w=max(1,(end-start)/self.duration_ms*self.width())
            painter.fillRect(QRectF(x,kiai_top,w,kiai_height),QColor(255,170,0,82))
        painter.setPen(QPen(theme.screen_rgba(255,255,255,190),1));painter.drawLine(0,center,self.width(),center)
        colors={"yellow":QColor("#ffd400"),"red":QColor("#ff4545"),"green":QColor("#45d65a")}
        # Grouped by colour so the pen is set three times instead of once per
        # marker: setPen was measured at the same call count as drawLine.
        for kind,color in colors.items():
            painter.setPen(QPen(color,1))
            for x,marker_kind in self._marker_line_positions():
                if marker_kind==kind:painter.drawLine(x,1,x,center-1)
        painter.setPen(QPen(QColor("#3e9bff"),1))
        for bookmark in self.bookmarks:
            x=round(bookmark/self.duration_ms*self.width());painter.drawLine(x,center+1,x,self.height()-2)
        painter.end();self.static_layer=layer;self.static_layer_dirty=False
    def paintEvent(self,event)->None:
        if self.static_layer_dirty or self.static_layer.size()!=self.size():self._rebuild_static_layer()
        painter=QPainter(self);painter.drawPixmap(0,0,self.static_layer)
        painter.setRenderHint(QPainter.Antialiasing,False)
        center=self.height()//2
        if self.preview_time is not None:
            x=round(self.preview_time/self.duration_ms*self.width());painter.setPen(QPen(QColor("#ffd400"),1));painter.drawLine(x,center+1,x,self.height()-2)
        if self.viewport_end>self.viewport_start:
            x=self.viewport_start/self.duration_ms*self.width();w=(self.viewport_end-self.viewport_start)/self.duration_ms*self.width()
            painter.setPen(QPen(theme.screen_rgba(255,255,255,65),1));painter.setBrush(Qt.NoBrush);painter.drawRect(QRectF(x,1,w,self.height()-2))
        x=round(self.current_time/self.duration_ms*self.width());painter.setPen(QPen(theme.screen("#ffffff"),1));painter.drawLine(x,0,x,self.height())


# The density histogram (owner, 2026-10-08): one bar per 1% of the song, each
# as tall as its note rate against a fixed ceiling -- DENSITY_FULL_NOTES in
# DENSITY_FULL_BEATS beats of the chart's most common BPM, a 1/4 stream. Read
# against the chart's own tempo rather than against its busiest bar, so a
# chart with one dense burst no longer draws everything else as nothing, and
# a gimmick's 60000 BPM line cannot set the scale.
DENSITY_BARS = 100


DENSITY_FULL_NOTES = 16


DENSITY_FULL_BEATS = 4


def density_bars(times, duration_ms: float, beat_length: float | None) -> list[tuple[float, float, float]]:
    """[(start, end, level 0..1)], one bar per DENSITY_BARS-th of the song.
    `times` sorted. Level 1 is DENSITY_FULL_NOTES notes per DENSITY_FULL_BEATS
    beats of `beat_length` or more; without a tempo, against the busiest bar."""
    duration_ms = max(1.0, float(duration_ms))
    step = duration_ms / DENSITY_BARS
    counts = []
    for index in range(DENSITY_BARS):
        start, end = index * step, (index + 1) * step
        counts.append((start, end, bisect_left(times, end) - bisect_left(times, start)))
    if beat_length:
        full = DENSITY_FULL_NOTES * step / (DENSITY_FULL_BEATS * beat_length)
    else:
        full = max((count for _start, _end, count in counts), default=1)
    full = max(full, 1e-9)
    return [(start, end, min(1.0, count / full)) for start, end, count in counts]


class DensityOverview(QWidget):
    seek_requested = Signal(int)
    def __init__(self) -> None:
        super().__init__(); self.duration_ms=1; self.current_time=0; self.viewport_start=0; self.viewport_end=0; self.windows=[]; self.dragging=False
        self._last_seek_ms=None
        self.static_layer = QPixmap()
        self.static_layer_dirty = True
        self.setFixedHeight(58); self.setCursor(Qt.PointingHandCursor)
    def load_document(self,document,duration_ms:int)->None:
        self.duration_ms=max(duration_ms,document.hit_objects[-1].time if document.hit_objects else 1,1)
        times=[n.time for n in document.hit_objects]
        beat=most_common_beat_length(extract_timing_points(document), times[-1] if times else 0)
        self.windows=density_bars(times, self.duration_ms, beat)
        self.static_layer_dirty=True; self.update()
    def set_duration(self,value:int)->None:
        if value>0 and value!=self.duration_ms:
            self.duration_ms=value; self.static_layer_dirty=True; self.update()
    def set_time(self,value:int)->None:self.current_time=max(0,min(value,self.duration_ms));self.update()
    def set_viewport(self,center:int,window_ms:float)->None:
        self.viewport_start=max(0,center-window_ms/2); self.viewport_end=min(self.duration_ms,center+window_ms/2); self.update()
    def _seek(self,x:float)->None:
        """See `TimingOverviewBar._seek`: a press and a release at the same
        pixel are one click, and the duplicate became a delayed sink rebuild."""
        value=round(max(0,min(1,x/max(1,self.width())))*self.duration_ms)
        if value==self._last_seek_ms:return
        self._last_seek_ms=value;self.seek_requested.emit(value)
    def mousePressEvent(self,event)->None:
        # Cleared first so a second click on the same pixel still seeks:
        # playback has moved on since, so it is a real request to go back.
        if event.button()==Qt.LeftButton:self.dragging=True;self._last_seek_ms=None;self._seek(event.position().x())
    def mouseMoveEvent(self,event)->None:
        if self.dragging:self._seek(event.position().x())
    def mouseReleaseEvent(self,event)->None:
        if self.dragging:self._seek(event.position().x())
        self.dragging=False
    def resizeEvent(self,event)->None:
        self.static_layer_dirty=True
        super().resizeEvent(event)
    def _rebuild_static_layer(self)->None:
        if self.width()<=0 or self.height()<=0:return
        layer=QPixmap(self.size()); layer.fill(theme.screen("#000000")); painter=QPainter(layer); painter.setPen(Qt.NoPen)
        # One gradient over the whole height in the theme's focus colour, faint
        # at the foot and bright at the top, so a denser bar reaches further up
        # it (owner, 2026-10-12). It was a fixed white-to-yellow in every theme;
        # mixing two of a theme's colours went muddy where they are far apart
        # (taiko's red into blue).
        fill=QLinearGradient(0,self.height(),0,0)
        top=theme.color("#ff66aa"); foot=QColor(top); foot.setAlpha(60)
        # Lightened only on a dark lane: on NSO's pink it washed out.
        fill.setColorAt(0.0,foot); fill.setColorAt(1.0,top if theme.has_screens() else top.lighter(118))
        for start,end,level in self.windows:
            if not level:continue
            x=start/self.duration_ms*self.width();w=max(1.0,(end-start)/self.duration_ms*self.width());h=max(1.0,level*(self.height()-8))
            painter.fillRect(QRectF(x,self.height()-h,w,h),fill)
        painter.end(); self.static_layer=layer; self.static_layer_dirty=False
    def paintEvent(self,event)->None:
        if self.static_layer_dirty or self.static_layer.size()!=self.size(): self._rebuild_static_layer()
        painter=QPainter(self); painter.drawPixmap(0,0,self.static_layer)
        if self.viewport_end>self.viewport_start:
            vx=self.viewport_start/self.duration_ms*self.width(); vw=(self.viewport_end-self.viewport_start)/self.duration_ms*self.width()
            painter.setPen(QPen(theme.screen_rgba(255,255,255,85),1)); painter.setBrush(Qt.NoBrush); painter.drawRect(QRectF(vx,1,vw,self.height()-2))
        x=self.current_time/self.duration_ms*self.width();painter.setPen(QPen(theme.screen("#ffffff"),2));painter.drawLine(round(x),0,round(x),self.height())


def view_type_caption(view_type: str) -> str:
    """The name AddViewDialog gives `view_type`, for a view's header.

    Literal tr() calls, one per type, for the same reason AddViewDialog uses
    them: the i18n gate scans for tr("context", "constant") pairs.
    """
    return {
        "chart": tr("MainWindow", "Chart"),
        "chart_regular": tr("MainWindow", "Regular Chart Only"),
        "chart_fake_slider": tr("MainWindow", "Fake Sliders Only"),
        "chart_barline": tr("MainWindow", "Barlines Only"),
        "sv": tr("MainWindow", "SV Editor"),
        "kiai_sound": tr("MainWindow", "Kiai and Sound Volume"),
        "gameplay": tr("MainWindow", "Gameplay Viewer"),
        "gameplay_regular": tr("MainWindow", "Gameplay: Regular Chart Only"),
        "gameplay_fake_slider": tr("MainWindow", "Gameplay: Fake Sliders Only"),
        "gameplay_barline": tr("MainWindow", "Gameplay: Barlines Only"),
        "density": tr("MainWindow", "Density"),
    }.get(view_type, "")


class EditorViewFrame(QWidget):
    """One view with a header on its left: what it is, and close/move/lock.

    The header used to be a row of four pink buttons above every view, with
    the difficulty at the far right of it. Stacked seven deep on the gimmick
    page that was ~180px of chrome and 28 pink buttons, all equally loud. It
    is now a strip beside the view (HEADER_WIDTH): the kind of view, then the
    name -- the difficulty in pink on the Editor page, the layer's own name on
    the gimmick page -- and the four buttons, quiet, shown while the pointer
    is over the view or the view is locked. Every view loses the same width,
    so the hit positions, each centred in its own view, still line up.

    The difficulty is still shown once per view and nowhere else; the group
    header above a difficulty's views carries no label.

    Holds no MainWindow reference; the owner wires content in via set_content
    and listens for `closed`.
    """

    closed = Signal(object)
    # (frame, -1 up / +1 down). Two buttons rather than one, because a single
    # glyph cannot say which way -- and a drag handle would have to fight the
    # views underneath it, every one of which already drags.
    move_requested = Signal(object, int)
    # (frame, where the pointer is, global). The header *is* a handle after
    # all: it is the one part of a view that does not drag notes. Pressed and
    # carried past the system's drag distance, the owner starts a ReorderDrag.
    drag_started = Signal(object, object)

    HEADER_WIDTH = 148
    # The frame's own sheet is selector-less, so it reaches every descendant:
    # each rule below names its own border to undo that.
    #
    # Edge to edge since 2026-10-07: square, with one divider under each view
    # and no gap between them, so the page's backdrop shows only where View
    # opacity lets it. It used to show in a strip between every pair of views.
    FRAME_STYLE = "background: {ground}; border: 0; border-bottom: 1px solid #303947; border-radius: 0;"
    # The approved mockup's focused lane: a 2px rim in the theme's focus colour
    # rather than a 1px one in a muted shade of it, which beside the other
    # lanes' 1px #303947 read as no difference at all.
    # The 2px rim is drawn *over* the view by `_FrameOverlay`, so it can carry
    # the song list's running lights -- and so focusing a view changes nothing
    # about its box. It used to inset the lane by 2px a side, which narrowed
    # it by 4px and slid every snap tick off-centre (owner, 2026-10-08).
    # How far the other views fade while one is focused (owner, 2026-10-08):
    # a wash of the page's own ground over them, not View opacity, which is
    # how much of the map shows through and stays as set.
    UNFOCUSED_WASH = 0.45
    HEADER_STYLE = "QWidget#viewHeader { border: 0; border-right: 1px solid #303947; border-radius: 0; }"
    # The song select's selection: a pink rim at the left edge and a tint.
    FOCUSED_HEADER_STYLE = (
        "QWidget#viewHeader { border: 0; border-left: 3px solid #ff66aa;"
        " border-right: 1px solid #303947; border-radius: 0;"
        " background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
        " stop:0 rgba(255,102,170,46), stop:0.7 rgba(255,102,170,10)); }"
    )
    # Under a theme with light screens (NSO) the header is chrome, not lane:
    # a box in the chrome's own ground, or its pink name sat on pink.
    SCREEN_HEADER_STYLE = (
        "QWidget#viewHeader { border: 0; border-right: 1px solid #303947; border-radius: 0;"
        " background: #1b212b; }"
    )
    SCREEN_FOCUSED_HEADER_STYLE = (
        "QWidget#viewHeader { border: 0; border-left: 3px solid #ff66aa;"
        " border-right: 1px solid #303947; border-radius: 0;"
        " background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2a2230, stop:0.7 #1b212b); }"
    )

    def __init__(self, view_type: str, difficulty_label: str, compact: bool = False) -> None:
        super().__init__()
        self.view_type = view_type
        self.locked = False
        self.compact = compact
        self.focused = False
        self.content: QWidget | None = None

        self.setStyleSheet(self._frame_style())
        # A plain QWidget does not paint its own sheet: without this the rim
        # above was never drawn, focused or not, and nothing marked the view
        # the tool row acts on but the header's left edge. Its content paints
        # opaque (WA_OpaquePaintEvent), so this costs nothing per frame.
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 1)
        layout.setSpacing(0)

        self.header = QWidget()
        self.header.setObjectName("viewHeader")
        self.header.setAttribute(Qt.WA_StyledBackground, True)
        self.header.setFixedWidth(self.HEADER_WIDTH)
        self.header.setStyleSheet(self._header_style())
        header = QVBoxLayout(self.header)
        header.setContentsMargins(10, 5 if compact else 7, 6, 4 if compact else 6)
        header.setSpacing(1)

        # A gimmick layer's name already says what it is ("SV (barlines)"),
        # so only the Editor page's views, named by difficulty, get a kind.
        self.kind_label = QLabel("" if compact else view_type_caption(view_type).upper())
        self.kind_label.setStyleSheet(self._caption_style(False))
        header.addWidget(self.kind_label)
        # Hidden once parented, never shown here: setVisible(True) on a label
        # with no parent yet makes it a top-level window of its own, which
        # flashed on screen and hung test_gimmick_editor's setUp.
        self.kind_label.setHidden(not self.kind_label.text())

        # Wrapped, not elided: two lines fit in the shortest view, and
        # "The Pharaoh's C..." cut off the part that told two apart.
        self.difficulty_name_label = QLabel()
        self.difficulty_name_label.setTextFormat(Qt.PlainText)
        self.difficulty_name_label.setWordWrap(True)
        self.difficulty_name_label.setStyleSheet(self._name_style(False))
        # Word wrap only breaks at spaces, so a one-word name wider than the
        # header ("worldwidesuperstar") ran under the view beside it instead.
        # Under a stylesheet font() still reports the base weight, so the
        # measure is a bold copy -- the weight the label is drawn in.
        bold = QFont(self.difficulty_name_label.font())
        bold.setBold(True)
        room = self.HEADER_WIDTH - header.contentsMargins().left() - header.contentsMargins().right()
        self.difficulty_name_label.setText(breakable_words(difficulty_label, QFontMetrics(bold), room))
        self.difficulty_name_label.setToolTip(difficulty_label)
        header.addWidget(self.difficulty_name_label)
        header.addStretch(1)

        # Quiet until wanted: shown while the pointer is over this view, and
        # always while it is locked, so a locked view says so.
        self.controls = QWidget()
        self.controls.setStyleSheet("border: 0; background: transparent;")
        controls = QHBoxLayout(self.controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(1)
        chrome_button_style = (
            "QPushButton { background: transparent; color: #7d8794; border: 0;"
            " border-radius: 4px; font-weight: 600; padding: 1px 5px; }"
            "QPushButton:hover { background: #2a3341; color: #e8edf3; }"
            "QPushButton:checked { color: #ffb347; }"
        )

        self.close_button = QPushButton("\u2715")
        self.close_button.setToolTip(tr("MainWindow", "Close view"))
        self.close_button.clicked.connect(lambda: self.closed.emit(self))
        self.move_up_button = QPushButton("\u25b2")
        self.move_up_button.setToolTip(tr("MainWindow", "Move view up"))
        self.move_up_button.clicked.connect(lambda: self.move_requested.emit(self, -1))
        self.move_down_button = QPushButton("\u25bc")
        self.move_down_button.setToolTip(tr("MainWindow", "Move view down"))
        self.move_down_button.clicked.connect(lambda: self.move_requested.emit(self, 1))
        self.lock_button = QPushButton("\U0001f512")
        self.lock_button.setCheckable(True)
        self.lock_button.setToolTip(tr("MainWindow", "Lock view (read-only)"))
        self.lock_button.toggled.connect(self._set_locked)
        # In the order the row reads: move it, freeze it, get rid of it.
        for button in (self.move_up_button, self.move_down_button, self.lock_button, self.close_button):
            button.setFocusPolicy(Qt.NoFocus)
            button.setStyleSheet(chrome_button_style)
            controls.addWidget(button)
        controls.addStretch(1)
        header.addWidget(self.controls)
        # Their room kept while hidden: shown on hover, they took it from a
        # long wrapped name and pushed it down under the pointer.
        policy = self.controls.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.controls.setSizePolicy(policy)
        self.controls.setVisible(False)

        layout.addWidget(self.header)
        self._content_layout = QVBoxLayout()
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._content_layout, 1)
        tick_while_shown(self, self._repaint_rim)
        # The labels take no clicks, so a press on them reaches the header.
        # The buttons take theirs, and keep the arrow.
        self.header.setCursor(Qt.OpenHandCursor)
        for button in self.controls.findChildren(QPushButton):
            button.setCursor(Qt.ArrowCursor)
        self._drag_from: QPoint | None = None
        self.header.installEventFilter(self)
        self.dimmed = False
        self._overlay = _FrameOverlay(self)

    def eventFilter(self, watched, event) -> bool:
        if watched is not self.header:
            return False
        kind = event.type()
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._drag_from = event.globalPosition().toPoint()
            # A click on the handle selects the view as a click inside it
            # does (owner, 2026-10-17): through Qt's focus, which is what
            # rims the frame and swaps the tool row.
            # Consumed: left to propagate, it reached the page under the views,
            # whose press is "let go of the focused view".
            if self.content is not None:
                self.content.setFocus(Qt.MouseFocusReason)
                return True
        elif kind == QEvent.MouseMove and self._drag_from is not None:
            here = event.globalPosition().toPoint()
            if (here - self._drag_from).manhattanLength() >= QApplication.startDragDistance():
                self._drag_from = None
                self.drag_started.emit(self, here)
                return True
        elif kind == QEvent.MouseButtonRelease:
            self._drag_from = None
        return False

    def _rim_region(self) -> QRegion:
        outer = self.rect()
        return QRegion(outer).subtracted(QRegion(outer.adjusted(2, 2, -2, -2)))

    def _repaint_rim(self) -> None:
        # The rim only: the lanes under it are repainted by the playhead, and
        # a frame-wide update here would repaint them all a second time.
        if self.focused:
            self._overlay.update(self._rim_region())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._overlay.setGeometry(self.rect())

    def _show_overlay(self) -> None:
        # Hidden when it has nothing to draw, so an ordinary view pays nothing.
        self._overlay.setVisible(self.focused or self.dimmed)
        self._overlay.raise_()
        self._overlay.update()

    def set_dimmed(self, dimmed: bool) -> None:
        """Fade this view while another one on its page is focused.

        Never the gameplay preview (owner, 2026-10-09): it is watched, not
        edited, and the point of it is seeing the result of an edit made in
        another view -- the moment that faded it."""
        dimmed = dimmed and self.view_type != "gameplay"
        if dimmed != self.dimmed:
            self.dimmed = dimmed
            self._show_overlay()

    def showEvent(self, event) -> None:
        # Measured after polish (and after parenting, which is what makes the
        # app-wide sheet apply): a typed width once left the glyphs negative
        # room and Qt drew nothing.
        super().showEvent(event)
        equalize_button_widths((
            self.close_button, self.move_up_button, self.move_down_button,
            self.lock_button,
        ))

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self.controls.setVisible(True)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.controls.setVisible(self.locked)

    def set_focused(self, focused: bool) -> None:
        """Mark this as the view the tool row acts on. Nothing on screen said
        which one that was; the row follows whichever view last had focus."""
        if focused == self.focused:
            return
        self.focused = focused
        self.header.setStyleSheet(self._header_style())
        self._show_overlay()
        # The caption names what is focused, in the accent, as in the mockup.
        self.kind_label.setStyleSheet(self._caption_style(focused))
        self.difficulty_name_label.setStyleSheet(self._name_style(focused))

    def _frame_style(self) -> str:
        ground = view_ground("#1b212b")
        rgba = f"rgba({ground.red()}, {ground.green()}, {ground.blue()}, {ground.alpha()})"
        return self.FRAME_STYLE.format(ground=rgba)

    def refresh_ground(self) -> None:
        """Re-read View opacity (`set_view_opacity`), and the theme."""
        self.setStyleSheet(self._frame_style())
        self.header.setStyleSheet(self._header_style())

    def _header_style(self) -> str:
        if theme.has_screens():
            return self.SCREEN_FOCUSED_HEADER_STYLE if self.focused else self.SCREEN_HEADER_STYLE
        return self.FOCUSED_HEADER_STYLE if self.focused else self.HEADER_STYLE

    def _caption_style(self, focused: bool) -> str:
        colour = "#ff9dcc" if focused else "#7d8794"
        return f"color: {colour}; font-size: 10px; font-weight: 700; border: 0; background: transparent;"

    def _name_style(self, focused: bool) -> str:
        # A gimmick layer has no caption, so there the name carries the mark.
        colour = "#ff9dcc" if focused and self.compact else ("#e8edf3" if self.compact else ACCENT_PINK)
        return f"color: {colour}; font-weight: 700; border: 0; background: transparent;"

    def set_content(self, widget: QWidget) -> None:
        self.content = widget
        self._content_layout.addWidget(widget)
        self._overlay.raise_()  # a child added later stacks above it

    def _set_locked(self, locked: bool) -> None:
        self.locked = locked
        if self.content is not None:
            self.content.setEnabled(not locked)
        self.controls.setVisible(locked or self.underMouse())


class _FrameOverlay(QWidget):
    """What lies over a view without being part of it: the focused view's
    living rim, or the wash that fades the others. Takes no input and has no
    layout space, so neither changes where anything in the view is."""

    def __init__(self, frame: EditorViewFrame) -> None:
        super().__init__(frame)
        self.frame = frame
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.hide()

    def paintEvent(self, event) -> None:
        frame = self.frame
        painter = QPainter(self)
        if frame.focused:
            paint_living_rim(painter, QRectF(self.rect()), 0, 2, True)
        elif frame.dimmed:
            wash = theme.screen("#11151c")
            wash.setAlphaF(EditorViewFrame.UNFOCUSED_WASH)
            painter.fillRect(event.rect(), wash)


def breakable_words(text: str, metrics: QFontMetrics, width: int) -> str:
    """`text` with a zero-width break point between the letters of any word
    wider than `width`, so word wrap can split it. Words that fit are left
    whole: breaking every word would split ordinary names mid-word too."""
    def split(word: str) -> str:
        return ZERO_WIDTH_SPACE.join(word) if metrics.horizontalAdvance(word) > width else word
    return " ".join(split(word) for word in text.split(" "))


ZERO_WIDTH_SPACE = "\u200b"
