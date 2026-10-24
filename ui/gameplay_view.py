"""The gameplay preview: osu!taiko's playfield, notes and autoplay drawn the way the ruleset draws them."""
from __future__ import annotations
import math
from bisect import bisect_left, bisect_right
from PySide6.QtCore import QVariantAnimation, QPointF, QRectF, Qt, QEasingCurve, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QWidget
from ui.smooth_scroll import reduced_motion
from library.skin import TaikoSkin
from ui import theme
from osu_io.timing import (
    TimingPoint,
    active_uninherited_at,
    kiai_spans,
    sorted_by_time,
    sv_at,
    uninherited_points,
)
from chart import autoplay
from chart.time_axis import KIAI_OPEN_END_MS, TimeAxisMixin, wheel_seek_time
from ui.common import (
    DON_COLOR,
    DRUMROLL_COLOR,
    GAMEPLAY_MAX_ASPECT,
    GAMEPLAY_MIN_HEIGHT,
    KAT_COLOR,
    PLAYFIELD_UNIT,
    SCROLL_MODS,
    SLIDER_MULTIPLIER_ASSUMED,
    STABLE_UNIT_PER_PLAYFIELD,
    TAIKO_STRONG_SCALE,
    draw_note_sprite,
    draw_pixmap_subpixel,
    drumroll_tick_times,
    duration_for_slider_length,
    most_common_beat_length,
    osu_screen_height_for,
    spinner_pixmap,
    view_ground,
    view_ground_is_opaque,
)


# **A fake slider is #fbb706 and nothing else, outside a chorus.** There is no
# separate "shiny" effect and no brightening with stack depth: a pile of them
# is a pile of identical opaque heads and reads as one, which is what it is.
# Inside a chorus they pulse like everything else -- each object on the pile
# takes its own stamp in the flash pass below, so a deep stack pulses harder
# than a lone one, and outside a chorus there is nothing to see.
#
# Several richer models were tried against real readings and none of them
# closed; this is the one that is simply true by construction.
#
# A drumroll head's pulse is the drumroll's own yellow, and a pile of them
# (a shiny) adds that yellow to the note on top: the owner's Hyper Bass in
# game reads #ea452c -> #ff9236 on a shiny don, green +77 against blue +10,
# which is yellow light and not white.
KIAI_PULSE_COLOR = DRUMROLL_COLOR


# A don or kat on its own flashes **white**, as ppy/osu `CirclePiece`'s flash
# box does (`Colour = Color4.White`, additive): the note lifts toward a paler
# version of itself rather than toward yellow.
KIAI_CIRCLE_PULSE_COLOR = (255, 255, 255)


# osu!taiko's CirclePiece flashes at `kiai_flash_opacity = 0.15f` on each kiai
# beat, and does it with `BlendingParameters.Additive`. Both matter: 0.31 (what
# this was) is twice the brightness, and drawn as ordinary alpha it lays a film
# over the note's colour instead of lighting it, which is why kiai used to wash
# don and kat toward the same pale orange.
# osu!'s own `kiai_flash_opacity` is 0.15. This is deliberately well above it,
# and the reason is `TaikoSkin.flash`: the light is masked to the *colourable*
# part of a note and then weighted by how bright the artwork is there, so it
# reaches far less of the disc than a flat overlay did. 0.15 across what is
# left read as almost nothing. Measured on a don in kiai, on the beat:
#
#     stack       0         1         3         8
#             #ea6c2f   #f19d31   #f9d435   #fef93f
KIAI_PULSE_ALPHA = round(0.337 * 255)


# Deliberately far below KIAI_PULSE_ALPHA: this one washes the *entire* view
# rect rather than a single note-sized circle, so the same alpha that reads as
# a glow on a note would read as a floodlight over the whole lane.
# Halved on 2026-10-07 with KIAI_GLOW_STRENGTH: the owner found the lane's kiai
# glow too bright, and this is its stand-in when a skin has none.
PLAYFIELD_PULSE_ALPHA = 8


# How much of `taiko-bar-right-glow` a kiai beat shows at its peak. The art is
# drawn at full strength in game too, but there it sits under a playfield the
# player is looking *through*; here it is most of what the view is, and at
# full opacity every beat of a chorus flashed the whole preview.
KIAI_GLOW_STRENGTH = 0.5


# A circle's share of that, so a note glows without its colour being replaced.
# 0.55 until 2026-10-08, when the owner found the kiai flash too bright. Only
# the circle's share moved: a fake slider stamps at full KIAI_PULSE_ALPHA, so
# the shiny stack readings above are unchanged.
CIRCLE_KIAI_STRENGTH = 0.3


# Under this a "beat" is a gimmick, not a pulse: an invisible-note section at
# 0.0001ms per beat would strobe once per frame.
KIAI_PULSE_MIN_BEAT_MS = 50.0


# 60 BPM. Only a beat longer than this lets a kiai section that ends mid-pulse
# carry it to the end -- see GameplayViewerView._unfinished_pulse_anchor. Above
# 60 BPM the beat is short enough that a section ending inside one just stops,
# which is what a chorus ending should look like; below it the beat is long
# enough that the same cut lands on a flash still visibly fading.
KIAI_PULSE_CARRY_MIN_BEAT_MS = 1000.0


def beat_pulse(
    timing_points: list[TimingPoint], time_ms: float, anchor_ms: float | None = None,
) -> float:
    """Flash strength at `time_ms`: 1.0 on the beat, fading to 0 by the next.

    Walks back past any point whose beat_length reads as a gimmick rather than
    a real beat (a 60000 BPM section, a barline run) to the most recent point
    that is one, and pulses against that instead. Without this, the section
    active *right under the playhead* decided whether every note on screen
    flashed at all -- which is why "some shiny notes have no glow" depended on
    where the playhead happened to be sitting, not on the note. Returns 0.0
    only when the map has no real section behind the playhead at all, which
    keeps the original guard against strobing at 0.0001ms per beat.

    `anchor_ms` is the start of the kiai section `time_ms` falls in (the
    caller already has this from its kiai bands, so this doesn't search for
    it). With an anchor, the pulse hits 1.0 there and repeats **every 1/1
    beat**, fading linearly across it -- so a slower BPM fades slower for free,
    with no separate constant. It ran on a measure for a while, which at 4/4 is
    three beats out of four with nothing happening on them; a chorus pulses on
    the beat. Without an anchor, falls back to the same one-beat period phased
    from the timing point instead of from the section.
    """
    point = pulse_beat_point(timing_points, time_ms)
    if point is None:
        return 0.0
    phase = point.time if anchor_ms is None else anchor_ms
    return 1.0 - ((time_ms - phase) / point.beat_length) % 1.0


def pulse_beat_point(
    timing_points: list[TimingPoint], time_ms: float,
) -> TimingPoint | None:
    """The section the pulse beats against: the most recent point at or before
    `time_ms` whose beat_length is a real beat rather than a gimmick.

    Binary search to the last point at or before the playhead, then walk back
    from there -- not a scan from the end of the list. This runs once per
    frame at 120Hz and a barline gimmick carries tens of thousands of points,
    which is the difference between a repaint and a freeze (the same reason
    `active_uninherited_at` is a binary search).
    """
    index = bisect_right(timing_points, time_ms, key=lambda point: point.time) - 1
    while index >= 0:
        point = timing_points[index]
        if point.beat_length >= KIAI_PULSE_MIN_BEAT_MS:
            return point
        index -= 1
    return None


def kiai_flash_strength(note) -> float:
    """How brightly `note` takes the kiai flash, relative to a drawn object.

    A circle glows *slightly*: it is already a solid, saturated red or blue and
    a full-strength overlay on top of it reads as the note changing colour
    rather than as the chorus lighting up. A drumroll -- every fake slider and
    every shiny -- takes the flash at full strength, because brightening that
    yellow is the effect mappers stack them for in the first place. A shiny
    then compounds all by itself: it is several drumrolls on one millisecond
    and each one takes its own stamp, so a deep pile pulses harder than a lone
    fake slider. Outside a chorus none of this happens and a fake slider is
    flat #fbb706 -- see KIAI_PULSE_COLOR.
    """
    return CIRCLE_KIAI_STRENGTH if note.is_circle else 1.0


def draw_note_light(
    painter: QPainter, x: float, y: float, radius: float, alpha: int,
    colour: tuple[int, int, int], skin=None, big: bool = False,
    element: str | None = None, top_left: bool = False,
) -> None:
    """Add `alpha` of `colour` to the note at `x, y`, clipped to its own shape.

    **Additive**, like `CirclePiece`'s flash box: it adds light to whatever the
    note already is, so a don stays red and a kat stays blue while both
    brighten. Plain alpha blending replaced the colour instead, which is what
    made every note in a kiai section drift toward the same pale orange.

    And **clipped to the artwork**, not a disc laid over it. osu!'s flashBox is
    a child of the circle piece and inherits its masking, so the light stops
    exactly where the note does. Drawn as a plain ellipse it lit the whole
    bounding circle including the transparent rim, which reads as a glow
    radiating from behind the note rather than the note pulsing -- and it
    ignored the shape of every skin that does not draw a full disc.

    `element` names the artwork to take the shape from, defaulting to the note
    circle `big` selects; `top_left` anchors it the way the wiki anchors that
    element rather than centring it. Both exist for `taiko-roll-end`, which is
    a cap butted onto the end of a roll -- lighting a centred circle there
    would put the glow somewhere nothing is drawn.

    Shared by the kiai pulse and the shiny stack, which are the same operation
    with a different colour and a different reason.
    """
    if alpha <= 0:
        return
    previous = painter.compositionMode()
    painter.setCompositionMode(QPainter.CompositionMode_Plus)
    stamp = None
    if skin is not None:
        if element is None:
            element = "taikobigcircle" if big else "taikohitcircle"
        stamp = skin.flash(element, round(radius * 2), QColor(*colour))
    if stamp is not None:
        # Opacity rather than the colour's alpha: the silhouette is already the
        # right colour at the artwork's own alpha, and multiplying a second
        # alpha into it is what the painter's opacity does for free.
        opacity = painter.opacity()
        painter.setOpacity(opacity * min(255, alpha) / 255.0)
        # Centred at the artwork's own size rather than forced into a square:
        # a note circle is square and comes out identical either way, but
        # `taiko-roll-end` is 64x128 and squaring it would squash the light off
        # the shape it is meant to be lighting.
        left = x if top_left else x - stamp.width() / 2.0
        painter.drawPixmap(QPointF(left, y - stamp.height() / 2.0), stamp)
        painter.setOpacity(opacity)
    else:
        # No skin: the built-in note *is* a full disc, so the ellipse is its
        # own shape rather than an approximation of one.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*colour, min(255, alpha)))
        painter.drawEllipse(QPointF(x, y), radius, radius)
    painter.setCompositionMode(previous)


def draw_kiai_flash(
    painter: QPainter, x: float, y: float, radius: float, pulse: float,
    strength: float = 1.0, skin=None, big: bool = False,
    element: str | None = None, top_left: bool = False,
    colour: tuple[int, int, int] = KIAI_PULSE_COLOR,
) -> None:
    """The kiai beat pulse. See `draw_note_light` and `KIAI_PULSE_COLOR`."""
    draw_note_light(
        painter, x, y, radius, round(KIAI_PULSE_ALPHA * pulse * strength),
        colour, skin, big, element, top_left,
    )


# Screen distance one whole beat covers at 1.0x SV. osu!taiko's scroll speed is
# proportional to BPM x SV, so a beat occupies the same distance at every BPM --
# which is what makes this a constant rather than a function of beat length.
# Object sizes, taken from osu!taiko rather than chosen. `TaikoHitObject
# .DEFAULT_SIZE = 0.475f` is a fraction of the playfield height (its
# `BASE_HEIGHT` is 200), so a note's diameter is 0.475 of the lane and its
# radius half of that. What was here before -- `min(30.0, height * 0.19)` --
# was both too small and capped at a pixel count, so the notes stopped growing
# with the view and the proportions drifted further the taller it got.
TAIKO_NOTE_SIZE = 0.475


# taiko-barline, 4x175, centred on the bar.
PLAYFIELD_BARLINE_WIDTH = 4.0 / PLAYFIELD_UNIT


PLAYFIELD_BARLINE_HEIGHT = 175.0 / PLAYFIELD_UNIT


# The hit target's centre, in playfield units from the left edge: the right
# area is padded by INPUT_DRUM_WIDTH (180), and the hit target is a 200-wide
# container at X = -24 in it (`TaikoPlayfield`). 256 playfield units is exactly
# the 160 stable units `ComputeTimeRange` measures from, so the two agree.
GAMEPLAY_HIT_X_UNITS = 180.0 - 24.0 + 200.0 / 2.0


# The input drum, ppy/osu `TaikoPlayfield.INPUT_DRUM_WIDTH`, drawn only by the
# autoplay. Its pieces as `LegacyInputDrum` places them, in playfield units:
# (half, element, x, y, mirrored), with halves numbered 1 left kat, 2 left
# don, 3 right don, 4 right kat. Stable's offsets are 480-space, hence 1.6.
INPUT_DRUM_WIDTH = 180.0


_RIGHT_HALF_X = INPUT_DRUM_WIDTH - (INPUT_DRUM_WIDTH / 1.6 - 56) * 1.6


DRUM_LAYOUT = (
    (2, "taiko-drum-inner", 0.0, 0.0, False),
    (1, "taiko-drum-outer", 0.0, 0.0, True),
    (3, "taiko-drum-inner", _RIGHT_HALF_X, 0.0, True),
    (4, "taiko-drum-outer", _RIGHT_HALF_X, 0.0, False),
)


DRUM_LAYOUT_PRE_2_1 = (
    (2, "taiko-drum-inner", 18 * 1.6, 31 * 1.6, False),
    (1, "taiko-drum-outer", 8 * 1.6, 23 * 1.6, True),
    (3, "taiko-drum-inner", INPUT_DRUM_WIDTH - (INPUT_DRUM_WIDTH / 1.6 - 54) * 1.6, 31 * 1.6, True),
    (4, "taiko-drum-outer", INPUT_DRUM_WIDTH - (INPUT_DRUM_WIDTH / 1.6 - 53) * 1.6, 23 * 1.6, False),
)


# LegacyHalfDrum's press: `FadeTo(1, 80 * (1 - Alpha)).Delay(100)
# .FadeOut(50)`. osu-framework's Delay counts from the *start* of the
# sequence, not the end of the fade before it, so the half goes dark at 100ms
# and is gone at 150ms. Read as consecutive steps it held for 230ms.
DRUM_HIT_DOWN_MS = 80.0


DRUM_HIT_OUT_AT_MS = 100.0


DRUM_HIT_UP_MS = 50.0


DRUM_HIT_LIT_MS = DRUM_HIT_OUT_AT_MS + DRUM_HIT_UP_MS


def _drum_hit_alpha(since: float, start: float) -> float:
    """A drum half `since` ms after a hit that found it at alpha `start`."""
    if since < 0.0 or since >= DRUM_HIT_LIT_MS:
        return 0.0
    if since >= DRUM_HIT_OUT_AT_MS:
        return 1.0 - (since - DRUM_HIT_OUT_AT_MS) / DRUM_HIT_UP_MS
    fade_in = DRUM_HIT_DOWN_MS * (1.0 - start)
    if since >= fade_in:
        return 1.0
    t = since / fade_in
    return start + (1.0 - start) * (1.0 - (1.0 - t) ** 2)


# LegacyKiaiGlow.
KIAI_GLOW_IN_MS = 100.0


KIAI_GLOW_OUT_MS = 600.0


KIAI_GLOW_SCALE = 0.8  # TaikoLegacyHitTarget.SCALE


HIT_TARGET_NOTE_ALPHA = 0.22  # its taikobigcircle


# Its approachcircle: `SCALE + 0.03` of the art's own size, not fitted to the
# note. Fitting it was wrong both ways (owner, 2026-10-11): by canvas, a ring
# drawn on a 192px canvas came out a different size from one on 128; by
# visible ink, riun's soft rainbow glow counted as ring and the line inside it
# landed smaller than the note.
HIT_TARGET_RING_SCALE = KIAI_GLOW_SCALE + 0.03


KIAI_GLOW_KICK = 0.15


KIAI_GLOW_KICK_MS = 80.0


KIAI_GLOW_COLOR = QColor(255, 228, 0)


# Scroll distance per beat at 1.0x SV, in stable units:
# `100 * SliderMultiplier * VELOCITY_MULTIPLIER(1.4)`. `_scroll_scale` already
# carries SliderMultiplier / SLIDER_MULTIPLIER_ASSUMED, so the assumed value is
# folded back in here and the product is osu!'s number.
GAMEPLAY_STABLE_UNITS_PER_BEAT = 100.0 * 1.4 * SLIDER_MULTIPLIER_ASSUMED


GAMEPLAY_LOCK_ASPECT = True


# TaikoModEasy / TaikoModHardRock multiply the map's SliderMultiplier, so the
# scroll speed and every drumroll's length change together, as in game.
MOD_SLIDER_MULTIPLIER = {"EZ": 0.8, "HR": 1.4 * 4.0 / 3.0}


# TaikoModHidden: a hit starts fading when it is a full scroll length out
# (fade_out_start_time 1) and is gone 0.375 of that length later. Hits only:
# drumrolls and swells are not in its list and stay visible.
HD_FADE_START = 1.0


HD_FADE_DURATION = 0.375


# TaikoModFlashlight: a circle of radius 200 playfield units (the playfield's
# height), black by 1.4x that (`CircularFlashlight`'s smoothstep, smoothness
# 1.4). Centred at stable (208, 208): `new Vector2(208 * 1.6 * Scale.X)` sets
# both axes. The playfield's top is at stable y 135
# (`TaikoPlayfieldAdjustmentContainer.Y = 135 / 480`), so the centre sits
# slightly right of the hit target and slightly below the lane's middle.
# Not modelled: the 2.5x it opens to during a break (breaks are not parsed).
FL_RADIUS_UNITS = 200.0


FL_SMOOTHNESS = 1.4


FL_CENTRE_STABLE = 208.0


FL_PLAYFIELD_TOP_STABLE = 135.0


def flashlight_combo_scale(combo: int) -> float:
    """`ModFlashlight`'s combo scale: smaller from 100 combo and from 200."""
    if combo >= 200:
        return 0.625
    if combo >= 100:
        return 0.8125
    return 1.0


# Cap on how far the visible-note lookup will search either way. A near-zero SV
# section makes one screen cover an unbounded amount of *time*, and scanning a
# whole map for it would cost a frame.
GAMEPLAY_MAX_LOOKAHEAD_MS = 30000.0


class GameplayViewerView(QWidget):
    """M6: read-only preview that scrolls the way osu!taiko does.

    Chart and SV views plot on a *time* axis, where an SV change is invisible
    by construction -- spacing there only ever reflects the clock. Here x is a
    distance integrated from the scroll velocity (`px_per_beat * SV /
    beat_length`), so a 2.0x section really is twice as spread out and a
    generated sweep can be judged without exporting and opening osu!.

    Read-only: no tools, no selection, no editing signals. The wheel seeks the
    shared playhead like every other view, and Ctrl+wheel changes the base
    scroll speed -- a property of this preview alone, never of the map.
    """

    seek_requested = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        # paintEvent fills the whole rect; saying so keeps the page's map
        # backdrop out of every per-frame repaint -- while View opacity is
        # 100%, which is what `view_ground_is_opaque` says.
        self.setAttribute(Qt.WA_OpaquePaintEvent, view_ground_is_opaque())
        self.notes = []
        self.note_times: list[int] = []
        # Predicate deciding which hit objects scroll past; see refresh_notes.
        self.object_filter = None
        # Both kinds, sorted: SV needs the inherited points, the beat grid and
        # barlines need the uninherited ones.
        self.timing_points: list[TimingPoint] = []
        self.beat_points: list[TimingPoint] = []
        self._beat_times: list[float] = []
        # (start_ms, end_ms) kiai sections; see refresh_notes and paintEvent's
        # pulse block. kiai_bands_for lets a host share the cached bands the
        # way other views do -- see MainWindow._share_kiai_bands.
        self.kiai_bands: list[tuple[int, int]] = []
        self.kiai_bands_for = None
        # The map's [Difficulty] SliderMultiplier, until a document is loaded.
        self.slider_multiplier = SLIDER_MULTIPLIER_ASSUMED
        # Scroll velocity in beats per millisecond, one entry per timing-point
        # timestamp, so velocity_at is a binary search instead of a walk.
        self._velocity_times: list[float] = []
        self._velocities: list[float] = []
        self._slowest_velocity = 1.0 / 500.0
        self._max_extend_ms = 0.0
        # Slider/spinner end time by note uid, rebuilt per edit -- see
        # refresh_notes. Circles are simply absent.
        self._end_times: dict[int, float] = {}

        self.current_time = 0.0
        self.snap_divisor = 4
        self.wheel_accumulator = 0.0
        self.last_rendered_time = -1.0
        # The mods in force (see MOD_ORDER). HR/EZ change the map's own
        # SliderMultiplier, so the document is kept to re-derive from.
        self.mods: frozenset[str] = frozenset()
        self._document = None

        self.don_brush = QColor(*DON_COLOR)
        self.kat_brush = QColor(*KAT_COLOR)
        self.slider_brush = QColor(*DRUMROLL_COLOR)
        self.spinner_band_brush = QColor(190, 195, 205, 90)
        self.note_pen = QPen(QColor(15, 18, 24, 220), 2)
        # An osu! skin's own note art, or the built-in drawing when no skin is
        # chosen and for any element a chosen skin does not ship.
        self.skin = TaikoSkin()

        self.restyle_pens()

        self.setMinimumHeight(GAMEPLAY_MIN_HEIGHT)
        self.setFocusPolicy(Qt.StrongFocus)
        # A fraction of the osu!-screen height below, for the song select's
        # smaller preview; None is the whole of it.
        self.height_share: float | None = None
        # Called with the new height whenever resizeEvent sets one.
        self.height_changed = None
        # The song select's preview sits on the detail pane, not on a page
        # backdrop, so Settings > View opacity is not about it.
        self.follows_view_opacity = True
        # The song select's bare stage (owner, 2026-10-08): black, no skin
        # playfield, no cover past osu!'s screen edge, and the hit target
        # nearer the left -- a glance at the chart, not a picture of the game.
        self.plain_stage = False
        # The chart's opacity, apart from the playfield's: song select fades
        # one chart out and the next in (owner, 2026-10-09) while the stage
        # and the hit target stay put. Barlines and notes; never the lane.
        self.chart_opacity = 1.0
        self._chart_fade: QVariantAnimation | None = None
        self._chart_fade_then = None
        self.hit_x_units = GAMEPLAY_HIT_X_UNITS
        # (style, binding, left_handed) while an autoplay plays this preview,
        # else None. Its hits, per drum half (1-4), sorted: see _rebuild_hits.
        self.autoplay: tuple[str, str, bool] | None = None
        self._half_hits: dict[int, list[float]] = {}
        self._all_hits: list[float] = []

    def restyle_pens(self) -> None:
        """The built-in lane's pens and fill, for when no skin draws its own:
        the lane through `theme.screen`, so NSO's is the pastel pink. The
        barline and hit ring stay osu!'s white in every theme: a skin's dark
        lane art is drawn over the lane, and NSO's violet on it vanished
        (owner, 2026-10-12). Rebuilt on retheme."""
        self.barline_pen = QPen(QColor(225, 230, 240, 90), 1)
        self.hit_pen = QPen(QColor(255, 255, 255, 120), 2)
        self.lane_brush = theme.screen_rgba(28, 33, 43)

    def resizeEvent(self, event) -> None:
        """Take the height that makes this view exactly an osu! screen.

        The playfield's width on a 16:9 screen is 6.83x its height (see
        `osu_screen_height_for`), and a dock is usually much wider than that
        against its height -- 1882x170 is 11:1 -- so the preview showed osu!'s
        screen and then a dark band where osu! shows nothing. Owner's call,
        2026-09-26, against an osu!stable screenshot of the same frame: fit the
        view instead. Height follows width only, so setting it here settles in
        one pass. The floor stays: a panel too narrow for it reads as a
        narrower screen rather than shrinking to nothing.
        """
        super().resizeEvent(event)
        height = osu_screen_height_for(self.width())
        if self.height_share is not None:
            height = round(height * self.height_share)
        if height != self.height():
            self.setFixedHeight(height)
            if self.height_changed is not None:
                self.height_changed(height)

    def fade_chart(self, to: float, duration_ms: int, then=None) -> None:
        """Take `chart_opacity` to `to` over `duration_ms`, eased out, and
        call `then` once it is there. At once under reduced motion."""
        if self._chart_fade is None:
            self._chart_fade = QVariantAnimation(self)
            self._chart_fade.setEasingCurve(QEasingCurve.OutCubic)
            self._chart_fade.valueChanged.connect(self._set_chart_opacity)
            self._chart_fade.finished.connect(lambda: self._chart_fade_then and self._chart_fade_then())
        fade = self._chart_fade
        fade.stop()  # a fade cut short never calls its `then`
        self._chart_fade_then = then
        if reduced_motion() or duration_ms <= 0 or not self.isVisible():
            self._set_chart_opacity(to)
            if then is not None:
                then()
            return
        fade.setStartValue(self.chart_opacity)
        fade.setEndValue(float(to))
        fade.setDuration(duration_ms)
        fade.start()

    def _set_chart_opacity(self, value) -> None:
        self.chart_opacity = float(value)
        self.update()

    # -- document ------------------------------------------------------------

    def load_document(self, document) -> None:
        self.refresh_notes(document)
        self.current_time = float(self.notes[0].time if self.notes else 0)
        self.update()

    def refresh_notes(self, document) -> None:
        # Same predicate TimelineGameplay.object_filter is: the gimmick page's
        # per-layer gameplay previews show one layer's objects scrolling, so a
        # fake slider can be judged without the real chart's notes over it.
        # Timing is never filtered -- it is what moves the notes.
        if self.object_filter is None:
            self.notes = document.hit_objects
        else:
            self.notes = [note for note in document.hit_objects if self.object_filter(note)]
        self.note_times = [note.time for note in self.notes]
        self.timing_points = sorted_by_time(document.timing_points)
        if self.kiai_bands_for is not None:
            self.kiai_bands = self.kiai_bands_for(document)
        else:
            self.kiai_bands = kiai_spans(self.timing_points, KIAI_OPEN_END_MS)
        self._document = document
        self.slider_multiplier = document.slider_multiplier * self._mod_slider_factor()
        self.beat_points = uninherited_points(self.timing_points)
        self._beat_times = [point.time for point in self.beat_points]
        self._rebuild_velocities()
        # End times once per edit, not per note per frame. Every visible note
        # asked for one while painting, and each answer cost two binary searches
        # over the full point list.
        self._max_extend_ms = 0.0
        self._end_times = {}
        self._phantom_ends = {}
        for note in self.notes:
            end = self._compute_end_time(note)
            if end is not None:
                self._end_times[note.uid] = end
                self._max_extend_ms = max(self._max_extend_ms, end - note.time)
                continue
            # A fake slider's body runs *backwards* from its head. Cached in the
            # same pass rather than recomputed while painting, because a shiny
            # note is a stack of twenty of them on one millisecond.
            phantom = self._compute_phantom_end(note)
            if phantom is not None:
                self._phantom_ends[note.uid] = phantom
                # It is drawn forward of its head all the same -- the negative
                # duration becomes a track reaching *right* -- so it widens the
                # slice like any other body. Without this a long one vanished
                # the moment its head left the window, taking a thousand pixels
                # of still-visible track with it.
                self._max_extend_ms = max(
                    self._max_extend_ms, abs(phantom - note.time))
        self._rebuild_hits()
        self.update()

    def set_autoplay(self, setting: tuple[str, str, bool] | None) -> None:
        if setting == self.autoplay:
            return
        self.autoplay = setting
        self._rebuild_hits()
        self.update()

    def _rebuild_hits(self) -> None:
        """Every drum half the autoplay hits and when, once per edit -- a frame
        only bisects these. Rest and break lengths are judged against the
        map's main tempo: a gimmick's 60000 BPM line would make every gap in
        the chart a rest."""
        self._half_hits = {half: [] for half in (1, 2, 3, 4)}
        self._all_hits = []
        if self.autoplay is None or not self.notes:
            return
        style, binding, left = self.autoplay
        circles = [note for note in self.notes if note.is_circle]
        beat = most_common_beat_length(self.timing_points, self.notes[-1].time) or 500.0
        hits = list(zip(
            (float(note.time) for note in circles),
            autoplay.note_halves(
                [(note.time, note.is_kat, note.is_finisher) for note in circles],
                beat, style, binding, left)))
        for note in self.notes:
            if note.is_slider and note.uid in self._end_times:
                hits.extend(autoplay.roll_halves(drumroll_tick_times(
                    note, self.beat_points, self.timing_points, self.slider_multiplier), left))
        hits.sort(key=lambda hit: hit[0])
        for time_ms, halves in hits:
            self._all_hits.append(time_ms)
            for half in halves:
                self._half_hits[half].append(time_ms)

    # Timing points move the notes here (that is the whole point of the view),
    # so an SV edit refreshes exactly like a note edit does.
    refresh_points = refresh_notes

    def set_skin(self, skin: TaikoSkin) -> None:
        self.skin = skin
        self.update()

    def set_snap_divisor(self, divisor: int) -> None:
        self.snap_divisor = divisor

    def set_time(self, time_ms: int, force: bool = False) -> None:
        self.current_time = max(0.0, float(time_ms))
        if force or abs(self.current_time - self.last_rendered_time) >= 16.0:
            self.last_rendered_time = self.current_time
            self.update()

    def _note_end_time(self, note) -> float | None:
        """End time of a slider or spinner; None for a circle."""
        return self._end_times.get(note.uid)

    def _phantom_end_time(self, note) -> float | None:
        """Where a negative-length slider's body would reach, behind its head.

        Only fake sliders have one. See `_compute_phantom_end`.
        """
        return self._phantom_ends.get(note.uid)

    def _compute_phantom_end(self, note) -> float | None:
        """The end time of a fake slider, which is *before* its start.

        A fake slider is a drumroll with a negative `length` -- the canonical
        one is `256,192,54692,2,12,L|624:192,643,-0.0001`. The duration derived
        from it is negative, so the drumroll ends before it begins, no tick is
        ever generated and nothing is hittable, while osu! still draws the head.
        That is the whole trick, and it is why `GimmickConfig.__post_init__`
        rejects a non-negative length instead of clamping it: a positive one is
        a real, scoreable drumroll.

        The negative extent is real geometry, not an error to swallow, so it is
        computed and handed to the painter rather than discarded. Drawn, it
        shows how far behind the head the body reaches -- which is what decides
        whether two stacked fake sliders overlap.
        """
        if not note.is_slider or note.length is None or note.slides is None:
            return None
        timing = active_uninherited_at(self.beat_points, note.time)
        duration = duration_for_slider_length(
            note.length * note.slides, timing.beat_length,
            sv_at(self.timing_points, note.time), self.slider_multiplier,
        )
        return note.time + duration if duration < 0 else None

    def _compute_end_time(self, note) -> float | None:
        if note.is_spinner:
            return float(note.end_time) if note.end_time is not None else None
        if note.is_slider and note.length is not None and note.slides is not None:
            # beat_points, not timing_points: active_uninherited_at steps back
            # over inherited points one at a time, and an SV-heavy map puts
            # thousands of them between two red lines.
            timing = active_uninherited_at(self.beat_points, note.time)
            duration = duration_for_slider_length(
                note.length * note.slides, timing.beat_length,
                sv_at(self.timing_points, note.time), self.slider_multiplier,
            )
            return note.time + duration if duration > 0 else None
        return None

    # -- scroll velocity -----------------------------------------------------

    def _rebuild_velocities(self) -> None:
        """Scroll velocity in beats per millisecond, per timing-point timestamp.

        Velocities, not an integrated distance: in osu!taiko each object
        approaches the hit position at the speed in force **at its own time**,
        so a green line moves everything after it (including a note sitting on
        it) and objects can pass each other. Integrating one shared scroll
        position across the whole chart is osu!mania's model, not this one.

        Stored in beats rather than pixels so the base scroll speed stays a
        pure render-time scale: Ctrl+wheel costs a repaint, not a rebuild.
        """
        # Read once for the whole table rather than per point: it is a
        # property of the map, and it cannot change inside one rebuild.
        scroll_scale = self._scroll_scale
        points = self.timing_points
        beat_length = active_uninherited_at(points, points[0].time if points else 0.0).beat_length
        sv = 1.0
        times: list[float] = []
        velocities: list[float] = []
        index = 0
        while index < len(points):
            time_ms = points[index].time
            group_end = index
            while group_end < len(points) and points[group_end].time == time_ms:
                group_end += 1
            group = points[index:group_end]
            index = group_end
            for point in group:
                if point.uninherited:
                    beat_length = point.beat_length
            # An inherited point sharing the timestamp wins over the 1.0x reset
            # an uninherited one would apply -- the same rule as sv_series, for
            # the same reason: a green line stacked on a red one is how mappers
            # set the speed a new BPM section starts at.
            inherited = [point for point in group if point.inherited]
            if inherited:
                sv = inherited[-1].sv_multiplier
            else:
                sv = 1.0
            times.append(time_ms)
            velocities.append(self._beats_per_ms(beat_length, sv) * scroll_scale)
        if not times:
            times, velocities = [0.0], [self._beats_per_ms(beat_length, 1.0) * scroll_scale]
        self._velocity_times = times
        self._velocities = velocities
        # The slowest section decides how far ahead a full screen can reach,
        # and so how many notes the visible-range lookup has to consider.
        self._slowest_velocity = min(velocities)

    @property
    def _scroll_scale(self) -> float:
        """The map's own SliderMultiplier, as a factor on scroll speed.

        osu!taiko's scroll distance is
        `base_distance(100) * SliderMultiplier * VELOCITY_MULTIPLIER(1.4) *
        ScrollSpeed / beatLength` (`DrumRoll.cs`, `TaikoBeatmapConverter.cs`),
        so SliderMultiplier moves every object on screen. This view used to
        leave it out entirely, which meant two maps whose SliderMultiplier
        differed -- 0.6 against 2.0 is an ordinary spread -- were previewed at
        identical spacing while scrolling more than three times apart in game.
        The map's own value only reached the drumroll *durations*, so the
        bodies were right and the space between notes was not.

        Normalised against the value assumed before the parser read the real
        one, so a typical map previews at the scale it always has and only maps
        that actually differ move. The absolute speed is the Ctrl+wheel zoom's
        business; this is about the ratios between maps.
        """
        return max(0.01, self.slider_multiplier) / SLIDER_MULTIPLIER_ASSUMED

    @staticmethod
    def _beats_per_ms(beat_length: float, sv: float) -> float:
        # Guarded rather than trusted: the invisible-note gimmick authors
        # beat_length = 0.0001, and a hand-edited map can carry a zero.
        return max(0.01, sv) / max(1e-4, beat_length)

    def velocity_at(self, time_ms: float) -> float:
        """Scroll velocity in force at `time_ms`, in scroll units per ms.

        Beats per millisecond scaled by `_scroll_scale`, so the map's
        SliderMultiplier is already in it -- see that property.
        """
        index = max(0, bisect_right(self._velocity_times, time_ms) - 1)
        return self._velocities[index]

    def slowest_velocity_in(self, start_ms: float, end_ms: float) -> float:
        """Slowest velocity in force anywhere in `[start_ms, end_ms)`.

        Barline spacing has to be judged over a whole timing section, and the
        velocity at its red line does not describe it: a green line partway
        through moves every measure line after it, `x_for_time` resolves each
        one at its *own* time, and taking the red line's speed for the whole
        section made the two disagree. Slowest is the safe direction for both
        things this feeds -- the widest on-screen window and the largest line
        count -- so an SV change inside a section can never drop a line.
        """
        if not self._velocities:
            return self._slowest_velocity
        first = max(0, bisect_right(self._velocity_times, start_ms) - 1)
        last = bisect_left(self._velocity_times, end_ms)
        return min(self._velocities[first:max(last, first + 1)])

    def x_for_time(self, time_ms: float) -> float:
        """Where an object at `time_ms` currently sits.

        Its own velocity, not the playhead's: that is what makes a green line
        move every object after it while leaving the ones before it alone.
        """
        return self._hit_x() + (time_ms - self.current_time) * self.velocity_at(time_ms) * self.px_per_beat

    @property
    def px_per_beat(self) -> float:
        """Pixels one beat of scroll covers at 1.0x SV -- osu!'s distance,
        measured in the view's own height.

        This was a flat 200 screen pixels whatever the view's size, while every
        other size in it scaled with the height, so a wide, short dock (1882x170
        in the profiling runs) put a whole in-game screen of notes into a
        corner and filled the rest with chart osu! would not show yet.
        """
        return self.height() * STABLE_UNIT_PER_PLAYFIELD * GAMEPLAY_STABLE_UNITS_PER_BEAT

    def _hit_x(self) -> float:
        return self.height() * self.hit_x_units / PLAYFIELD_UNIT

    def osu_edge_x(self) -> float:
        """Where osu!'s screen ends: 16:9 of a screen whose playfield is this
        tall (see GAMEPLAY_LOCK_ASPECT). Beyond the view's width, the view."""
        if not GAMEPLAY_LOCK_ASPECT or self.plain_stage:
            return float(self.width())
        edge = self.height() * STABLE_UNIT_PER_PLAYFIELD * 480.0 * GAMEPLAY_MAX_ASPECT
        return min(float(self.width()), edge)

    def _mod_slider_factor(self) -> float:
        factor = 1.0
        for mod in SCROLL_MODS:
            if mod in self.mods:
                factor *= MOD_SLIDER_MULTIPLIER[mod]
        return factor

    def set_mods(self, mods) -> None:
        """Apply the mods the preview can show: HR/EZ, HD, FL. Rate mods move
        the song, and the preview follows song time on its own."""
        mods = frozenset(mods)
        if mods == self.mods:
            return
        scroll_changed = (
            {m for m in SCROLL_MODS if m in mods} != {m for m in SCROLL_MODS if m in self.mods}
        )
        self.mods = mods
        if scroll_changed and self._document is not None:
            self.refresh_notes(self._document)
        self.update()

    def _animation_ms(self, song_ms: float) -> float:
        """`song_ms` of song time as the wall-clock ms an animation has run.

        Under DT/NC the song runs 1.5x, so a 150ms drum flash timed on song
        time was over in 100ms. The hit animations keep nomod's length (owner,
        2026-10-19); the kiai pulse is per beat and stays on song time."""
        return song_ms / 1.5 if "DT" in self.mods or "NC" in self.mods else song_ms

    def _scroll_length_px(self) -> float:
        """osu!'s scroll length on this view, hit target to screen edge --
        what TaikoModHidden's preempt is a fraction of."""
        if GAMEPLAY_LOCK_ASPECT:
            edge = self.height() * STABLE_UNIT_PER_PLAYFIELD * 480.0 * GAMEPLAY_MAX_ASPECT
        else:
            edge = float(self.width())
        return max(1.0, edge - self._hit_x())

    def hidden_alpha(self, x: float) -> float:
        """How visible a hit at `x` is under HD: 1 as it enters, 0 once it has
        covered HD_FADE_DURATION of the scroll length. Linear, as the FadeOut
        it copies is."""
        if "HD" not in self.mods:
            return 1.0
        travelled = (x - self._hit_x()) / self._scroll_length_px()
        gone_at = HD_FADE_START - HD_FADE_DURATION
        return max(0.0, min(1.0, (travelled - gone_at) / HD_FADE_DURATION))

    def flashlight_combo(self) -> int:
        """The combo FL sizes itself by. Nobody is playing a preview, so this
        is the full-combo count: every hit before the playhead."""
        return sum(
            1 for note in self.notes[:bisect_left(self.note_times, self.current_time)]
            if note.is_circle
        )

    def _draw_flashlight(self, painter: QPainter) -> None:
        if "FL" not in self.mods:
            return
        unit = self.height() * STABLE_UNIT_PER_PLAYFIELD
        centre = QPointF(FL_CENTRE_STABLE * unit, (FL_CENTRE_STABLE - FL_PLAYFIELD_TOP_STABLE) * unit)
        radius = (self.height() * FL_RADIUS_UNITS / PLAYFIELD_UNIT
                  * flashlight_combo_scale(self.flashlight_combo()))
        outer = radius * FL_SMOOTHNESS
        inner = radius / outer
        gradient = QRadialGradient(centre, outer)
        gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
        gradient.setColorAt(inner, QColor(0, 0, 0, 0))
        # The shader's smoothstep, sampled: a linear ramp between the two radii
        # reads as a harder edge than osu! draws.
        for step in range(1, 8):
            t = step / 8.0
            eased = t * t * (3.0 - 2.0 * t)
            gradient.setColorAt(inner + (1.0 - inner) * t, QColor(0, 0, 0, round(255 * eased)))
        gradient.setColorAt(1.0, QColor(0, 0, 0, 255))
        painter.fillRect(self.rect(), QBrush(gradient))

    def _draw_past_osu_edge(self, painter: QPainter) -> None:
        """Cover what osu! would not show. Opaque: a faint note behind a tint
        still reads as "coming up", and osu! shows nothing there at all. A thin
        line marks the edge itself."""
        edge = self.osu_edge_x()
        if edge >= self.width():
            return
        painter.fillRect(QRectF(edge, 0.0, self.width() - edge, self.height()), QColor(12, 15, 20))
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
        painter.drawLine(QPointF(edge, 0.0), QPointF(edge, float(self.height())))

    def visible_time_range(self) -> tuple[float, float]:
        """Time bounds wide enough to contain everything on screen.

        Bounded by the *slowest* velocity in the map, so it never misses an
        object, and clamped: a near-stopped section would otherwise put an
        unbounded amount of time on one screen and turn this into a full scan.
        """
        reach = self.osu_edge_x() / max(1e-6, self._slowest_velocity * self.px_per_beat)
        reach = min(reach, GAMEPLAY_MAX_LOOKAHEAD_MS)
        return self.current_time - reach, self.current_time + reach

    def barline_times(self, start_ms: float, end_ms: float) -> list[float]:
        """Barlines on screen for the frame at `current_time`, within the range:
        one at every uninherited point, plus one each `meter` beats (4 by
        default) after it until the next one.

        A point with omit-first-barline set contributes its later measure lines
        but not the one on the point itself -- that is the flag's whole purpose,
        and barline gimmicks set it in bulk.

        Each section is walked only across the slice of time it is actually on
        screen for, at the slowest speed in force anywhere in it. The passed
        range is sized for the map's slowest section, and a section past 60000
        BPM crosses the whole of it in a fraction of a millisecond: walking that
        one from `start_ms` spent the entire per-frame line budget far off the
        left edge and drew nothing, which is what made those barlines -- the
        only thing still visible in a gimmick section, since its notes travel
        too fast to see -- go missing.

        There is deliberately no cap on how many lines come back. A dense
        section can legitimately want a great many, and the count that used to
        be capped was spent in time order, so slow sections ahead of the
        playhead exhausted it before the walk reached the section the playhead
        was actually in. The only real ceiling is the screen: two barlines that
        round to the same pixel column are one line, so a column already claimed
        this frame is skipped. That bounds what comes back by the width of the
        widget without a number to pick, and it cannot starve a later section --
        every line that survives is one the user can see.
        """
        times: list[float] = []
        claimed_columns: set[int] = set()
        width = self.width()
        hit_x = self._hit_x()
        first_point = bisect_left(self._beat_times, start_ms) - 1
        for index in range(max(0, first_point), len(self.beat_points)):
            point = self.beat_points[index]
            if point.time > end_ms:
                break
            section_end = (
                self.beat_points[index + 1].time
                if index + 1 < len(self.beat_points)
                else float("inf")
            )
            if section_end < start_ms:
                continue
            if point.beat_length <= 0:
                continue
            # Same floor _beats_per_ms puts on beat length. An invisible-note
            # gimmick authors 1e-9 here, and pairing an unclamped measure with a
            # clamped velocity would ask the walk below for millions of lines.
            measure = max(1e-4, point.beat_length) * max(1, point.meter)
            velocity = self.slowest_velocity_in(point.time, min(section_end, end_ms))
            # How much time this section fits on screen. Generous by a factor of
            # two: the hit position is not centered, so both sides need less
            # than a full width, and over-reaching only ever costs a little walk.
            reach = width / max(1e-9, velocity * self.px_per_beat)
            window_start = max(start_ms, self.current_time - reach)
            window_end = min(end_ms, self.current_time + reach)
            if window_end < point.time or window_end < window_start or section_end <= window_start:
                continue
            # Not a budget -- exactly the measure lines this window holds, which
            # is what stops a minutes-long section from being walked to its end.
            # Measured against the window rather than the widget: `reach` is
            # applied to both sides, so the walk spans two screens' worth of
            # time and a width's worth of lines ran out halfway across it.
            remaining = int((window_end - window_start) / measure) + 2
            # Jump straight to the first measure line on screen: a section is
            # routinely minutes long and walking it from the point is not.
            step = 0 if point.time >= window_start else int((window_start - point.time) // measure)
            while remaining > 0:
                remaining -= 1
                time_ms = point.time + step * measure
                if time_ms > window_end or time_ms >= section_end:
                    break
                if time_ms >= window_start and not (step == 0 and point.omit_first_barline):
                    # Each line's own velocity, matching x_for_time -- a green
                    # line inside the section moves this one but not the ones
                    # before it.
                    column = round(
                        hit_x
                        + (time_ms - self.current_time)
                        * self.velocity_at(time_ms)
                        * self.px_per_beat
                    )
                    if column not in claimed_columns:
                        claimed_columns.add(column)
                        times.append(time_ms)
                step += 1
        return times

    # -- input ---------------------------------------------------------------

    def wheelEvent(self, event) -> None:
        angle = event.angleDelta()
        pixel = event.pixelDelta()
        delta = angle.y() or angle.x() or pixel.y() or pixel.x()
        if delta == 0:
            event.accept()
            return

        modifiers = event.modifiers() | QApplication.keyboardModifiers()
        if modifiers & Qt.ControlModifier:
            # Swallowed, not acted on. This view exists to show the chart at
            # the speed the *player* sees, which is the map's own scroll
            # velocity -- rescaling it is a picture of something the game will
            # never draw, so there is nothing here for Ctrl+wheel to mean.
            event.accept()
            return

        threshold = 40.0 if angle.isNull() == 0 else 120.0
        self.wheel_accumulator += delta
        steps = int(self.wheel_accumulator / threshold)
        if steps:
            self.wheel_accumulator -= steps * threshold
            divisor = 1 if modifiers & Qt.ShiftModifier else self.snap_divisor
            self.current_time = wheel_seek_time(
                self.beat_points, self.current_time, -1 if steps > 0 else 1, divisor
            )
            TimeAxisMixin.wheel_seek_in_progress = True
            try:
                self.seek_requested.emit(round(self.current_time))
            finally:
                TimeAxisMixin.wheel_seek_in_progress = False
            self.update()
        event.accept()

    # -- paint ---------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        if self.plain_stage:
            ground = QColor(0, 0, 0)
        elif self.follows_view_opacity:
            ground = view_ground("#12161d")
        else:
            ground = theme.screen("#12161d")
        painter.fillRect(self.rect(), ground)

        center_y = self.height() / 2
        normal_radius = self.height() * TAIKO_NOTE_SIZE / 2.0
        big_radius = normal_radius * TAIKO_STRONG_SCALE

        # The kiai pulse is needed before anything is drawn -- the playfield
        # itself lights up, not only the notes -- so it is computed here rather
        # than in the flash pass at the bottom. kiai_bands is sorted by start,
        # so bisect straight to the band the playhead could be in.
        band_index = bisect_right(self.kiai_bands, self.current_time, key=lambda band: band[0]) - 1
        anchor_ms = None
        if 0 <= band_index < len(self.kiai_bands):
            band_start, band_end = self.kiai_bands[band_index]
            if band_start <= self.current_time < band_end:
                anchor_ms = band_start
            elif self.current_time >= band_end:
                anchor_ms = self._unfinished_pulse_anchor(band_start, band_end)
        pulse = beat_pulse(self.beat_points, self.current_time, anchor_ms) if anchor_ms is not None else 0.0

        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self._draw_playfield(painter, center_y, normal_radius, big_radius, pulse)

        start_time, end_time = self.visible_time_range()
        hit_x = self._hit_x()
        if self._autoplay_shown():
            self._draw_kiai_glow(painter, hit_x, center_y, band_index)

        first = bisect_left(self.note_times, start_time - self._max_extend_ms)
        after_last = bisect_right(self.note_times, end_time)

        painter.setRenderHint(QPainter.Antialiasing, False)
        # The chart fades (fade_chart); the playfield drawn above does not.
        painter.setOpacity(self.chart_opacity)
        marker = self.skin.stretched(
            "taiko-barline",
            max(1, round(self.height() * PLAYFIELD_BARLINE_WIDTH)),
            round(self.height() * PLAYFIELD_BARLINE_HEIGHT),
        )
        painter.setPen(self.barline_pen)
        top, bottom = round(center_y - big_radius * 1.2), round(center_y + big_radius * 1.2)
        for time_ms in self.barline_times(start_time, end_time):
            x = self.x_for_time(time_ms)
            if not -1.0 <= x <= self.width() + 1.0:
                continue
            if marker is not None:
                draw_pixmap_subpixel(
                    painter, marker, x - marker.width() / 2.0, center_y - marker.height() / 2.0)
            else:
                painter.drawLine(round(x), top, round(x), bottom)

        painter.setOpacity(1.0)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if not self.plain_stage:
            self._draw_hit_target_note(painter, hit_x, center_y, big_radius)
        ring = self._hit_ring()
        if ring is not None:
            painter.drawPixmap(QPointF(hit_x - ring.width() / 2.0, center_y - ring.height() / 2.0), ring)
        else:
            painter.setPen(self.hit_pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(hit_x, center_y), normal_radius, normal_radius)
        # osu!'s own order (ppy/osu HitObjectContainer.Compare): the earlier
        # StartTime is on top, whatever the SV -- a shiny's note at T covers the
        # fake sliders it carries at T+1..3. A tie goes to the object added
        # first -- added when it comes on screen, and two objects on one
        # millisecond scroll at one speed, so that is file order. Barlines and
        # the hit target are an
        # underlay beneath all of it (TaikoPlayfield), drumrolls and spinners
        # included -- they share the one container with the notes.
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setOpacity(self.chart_opacity)
        visible = list(enumerate(self.notes[first:after_last], first))
        visible.sort(key=lambda item: (-item[1].time, -item[0]))
        for _index, note in visible:
            self._draw_note(painter, note, center_y, normal_radius, big_radius)

        # Second pass, so stacked objects compound: a note drawn on top of an
        # earlier note's flash would wipe it, and stacking a fake slider on a
        # note is exactly how a mapper makes that note read brighter.
        #
        # This pass *is* the shine. A shiny note is a stack of fake sliders
        # under one note, and each of them takes its own kiai stamp here -- so
        # the note above them reads brighter the deeper the stack, with no
        # counting and no separate effect. It must not happen outside a chorus:
        # a glow that is always on is not a pulse, it is just a differently
        # coloured note, and it made every stack glow everywhere.
        #
        # Gated on the *playhead* alone. Arriving at a chorus lights up the
        # whole screen, which is what it does in game -- so every visible
        # object flashes, not only the ones whose own millisecond is inside
        # the section. Notes were previously filtered by their own kiai state
        # as well, which meant the section's first beat lit up a handful of
        # notes and left the rest of the screen dark.
        if pulse > 0.0:
            # The built-in stand-in for `taiko-bar-right-glow`: only drawn
            # when the skin has no kiai state of its own for the lane, or the
            # lane would be washed twice over.
            if self.plain_stage or not self.skin.has("taiko-bar-right-glow"):
                playfield_alpha = round(PLAYFIELD_PULSE_ALPHA * pulse)
                if playfield_alpha > 0:
                    painter.setOpacity(1.0)  # the lane lights whatever the chart is doing
                    painter.fillRect(self.rect(), QColor(255, 255, 255, playfield_alpha))
                    painter.setOpacity(self.chart_opacity)
            for note in self.notes[first:after_last]:
                end_time = self._note_end_time(note)
                hit = self._has_been_hit(note)
                # A circle that has been played is gone and has nothing to
                # light. A slider always has something: its cap, which outlives
                # its head -- a fake slider's included, since that passes
                # straight through the hit position.
                if hit and not note.is_slider:
                    continue
                # Not a spinner: its art is not a CirclePiece, and the full
                # drumroll-strength stamp it took lit the whole swell yellow on
                # every beat of a chorus, which osu! does not do.
                if note.is_spinner:
                    continue
                x = self.x_for_time(note.time)
                radius = big_radius if (note.is_finisher or note.is_spinner) else normal_radius
                if not -radius <= x <= self.width() + radius:
                    continue
                # A fake slider takes its own stamp like every other object --
                # that *is* the shiny: a stack of them under one note, each
                # stamping the same millisecond, so the note above reads
                # brighter the deeper the pile with nothing counting it.
                strength = kiai_flash_strength(note)
                big = note.is_finisher or note.is_spinner
                # A real roll past the target has lost its head, so only its
                # cap lights from here on.
                if not hit:
                    alpha = self.hidden_alpha(x) if note.is_circle else 1.0
                    if alpha > 0.0:
                        painter.save()
                        painter.setOpacity(painter.opacity() * alpha)
                        draw_kiai_flash(
                            painter, x, center_y, radius, pulse, strength, self.skin, big,
                            colour=KIAI_CIRCLE_PULSE_COLOR if note.is_circle else KIAI_PULSE_COLOR)
                        painter.restore()
                # A roll's cap is part of the object and pulses with it. Its own
                # shape and its own anchor, or the light lands on the track
                # rather than on the cap -- see draw_note_light.
                #
                # **Only where the cap can be seen.** The head is drawn last and
                # covers it, so on a roll with no track to speak of the cap sits
                # behind the head, and lighting it puts a second stamp on the
                # head's right half and nowhere else -- which reads as a note
                # lit down one side. Once the head is gone the cap is all there
                # is, so then it lights on its own.
                if note.is_slider:
                    if end_time is not None:
                        span = end_time - note.time
                    else:
                        phantom = self._phantom_end_time(note)
                        span = 0.0 if phantom is None else abs(phantom - note.time)
                    end_x = x + span * self.velocity_at(note.time) * self.px_per_beat
                    if hit or end_x > x:
                        draw_kiai_flash(
                            painter, max(end_x, x), center_y, radius, pulse,
                            strength, self.skin, big, "taiko-roll-end", True,
                        )
        painter.setOpacity(1.0)
        if self._autoplay_shown():
            # Over the notes: a roll passing the target slides under the drum.
            self._draw_input_drum(painter, center_y)
        self._draw_flashlight(painter)
        self._draw_past_osu_edge(painter)

    def _draw_hit_target_note(self, painter, hit_x, center_y, big_radius) -> None:
        """The grey note sitting in the hit target (owner, 2026-10-10):
        ppy/osu `TaikoLegacyHitTarget`'s `taikobigcircle`, untinted, at alpha
        0.22 and SCALE (0.8) of its own size. Drawn under the ring rather than
        over it as lazer does -- at 0.22 the order barely shows, and under is
        what was asked for. Without the art, a white disc the size of a
        finisher at the same alpha."""
        previous = painter.opacity()
        painter.setOpacity(previous * HIT_TARGET_NOTE_ALPHA)
        circle = self.skin.natural(
            "taikobigcircle", self.height() / PLAYFIELD_UNIT * KIAI_GLOW_SCALE)
        if circle is not None:
            painter.drawPixmap(
                QPointF(hit_x - circle.width() / 2.0, center_y - circle.height() / 2.0), circle)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255))
            painter.drawEllipse(QPointF(hit_x, center_y), big_radius, big_radius)
        painter.setOpacity(previous)

    # -- autoplay ------------------------------------------------------------

    def _autoplay_shown(self) -> bool:
        # Never on song select's bare stage (owner, 2026-10-10).
        return self.autoplay is not None and not self.plain_stage

    def _half_alpha(self, half: int) -> float:
        """How lit drum half `half` is now: ppy/osu `LegacyHalfDrum`, see
        DRUM_HIT_OUT_AT_MS. A hit landing while the half is still lit fades
        in from where it is, over the part of 80ms that is left -- osu!'s
        `80 * (1 - Alpha)` -- so a stream on one half does not blink."""
        times = self._half_hits.get(half)
        if not times:
            return 0.0
        index = bisect_right(times, self.current_time) - 1
        if index < 0:
            return 0.0
        start = 0.0
        if index > 0:
            start = _drum_hit_alpha(self._animation_ms(times[index] - times[index - 1]), 0.0)
        return _drum_hit_alpha(self._animation_ms(self.current_time - times[index]), start)

    def _draw_input_drum(self, painter, center_y) -> None:
        """`taiko-bar-left` and the four drum halves, lit by the autoplay.

        Laid out as ppy/osu's `LegacyInputDrum` does, in its own units: the
        drum is INPUT_DRUM_WIDTH (180) of the playfield's 200, and each half
        is the same art, the right one mirrored. The rim art is drawn
        mirrored on the *left* -- skinners draw it for the right -- which is
        why `taiko-drum-outer` is flipped where `-inner` is not. Skins before
        2.1 place the pieces at stable's offsets instead.
        """
        unit = self.height() / PLAYFIELD_UNIT
        skin = self.skin
        painter.save()
        painter.setClipRect(QRectF(0.0, 0.0, INPUT_DRUM_WIDTH * unit, float(self.height())))
        bar = skin.natural("taiko-bar-left", unit)
        if bar is not None:
            painter.drawPixmap(QPointF(0.0, 0.0), bar)
        else:
            painter.fillRect(QRectF(0.0, 0.0, INPUT_DRUM_WIDTH * unit, float(self.height())),
                             QColor(18, 21, 28))
        layout = DRUM_LAYOUT if skin.version >= 2.1 else DRUM_LAYOUT_PRE_2_1
        for half, element, x, y, mirrored in layout:
            alpha = self._half_alpha(half)
            if not skin.has(element):
                self._draw_builtin_half(painter, half, center_y, alpha)
                continue
            if alpha <= 0.0:
                continue
            pixmap = skin.natural(element, unit, mirrored)
            if pixmap is not None:
                painter.setOpacity(alpha)
                painter.drawPixmap(QPointF(x * unit, y * unit), pixmap)
        painter.restore()

    def _draw_builtin_half(self, painter, half, center_y, alpha) -> None:
        """A drum half for a skin with no art for it: a half disc (don) or
        half ring (kat), outlined always, filled while lit."""
        height = self.height()
        centre = QPointF(INPUT_DRUM_WIDTH / 2.0 * height / PLAYFIELD_UNIT, center_y)
        outer, inner = height * 0.34, height * 0.24
        path = QPainterPath()
        path.setFillRule(Qt.OddEvenFill)
        if half in (1, 4):
            path.addEllipse(centre, outer, outer)
        path.addEllipse(centre, inner, inner)
        left = half in (1, 2)
        side = QPainterPath()
        side.addRect(QRectF(centre.x() - outer if left else centre.x(), 0.0, outer, float(height)))
        path = path.intersected(side)
        painter.save()
        painter.setOpacity(1.0)
        painter.setPen(QPen(QColor(255, 255, 255, 60), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        if alpha > 0.0:
            painter.setOpacity(alpha)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.kat_brush if half in (1, 4) else self.don_brush)
            painter.drawPath(path)
        painter.restore()

    def _draw_kiai_glow(self, painter, hit_x, center_y, band_index) -> None:
        """`taiko-glow` behind the hit target, ppy/osu `LegacyKiaiGlow`: in over
        100ms when a kiai starts, out over 600ms after it ends, tinted
        (255, 228, 0), at TaikoLegacyHitTarget.SCALE -- and kicked 0.15 larger
        by each hit, easing back over 80ms."""
        if not 0 <= band_index < len(self.kiai_bands):
            return
        band_start, band_end = self.kiai_bands[band_index]
        now = self.current_time
        if now < band_end:
            alpha = min(1.0, self._animation_ms(now - band_start) / KIAI_GLOW_IN_MS)
        else:
            alpha = 1.0 - self._animation_ms(now - band_end) / KIAI_GLOW_OUT_MS
        if alpha <= 0.0:
            return
        unit = self.height() / PLAYFIELD_UNIT
        glow = self.skin.natural("taiko-glow", unit, colour=KIAI_GLOW_COLOR)
        if glow is None:
            return
        scale = KIAI_GLOW_SCALE
        index = bisect_right(self._all_hits, now) - 1
        if index >= 0 and now < band_end:
            since = self._animation_ms(now - self._all_hits[index])
            if since < KIAI_GLOW_KICK_MS:
                t = since / KIAI_GLOW_KICK_MS
                scale += KIAI_GLOW_KICK * (1.0 - t) ** 2
        width, height = glow.width() * scale, glow.height() * scale
        previous = painter.opacity()
        painter.setOpacity(previous * alpha)
        painter.drawPixmap(
            QRectF(hit_x - width / 2.0, center_y - height / 2.0, width, height),
            glow, QRectF(glow.rect()))
        painter.setOpacity(previous)

    def _unfinished_pulse_anchor(self, band_start, band_end) -> float | None:
        """`band_start` while the pulse the section left running is still fading.

        A kiai section shorter than one beat starts a pulse it cannot finish:
        at `band_end` the flash cut to black mid-fade, which reads as the
        chorus being switched off rather than as a beat. The pulse that was in
        progress when the section ended is allowed to run out -- so the
        shortest possible kiai still shows one whole beat of light, and a
        section that ends exactly on a beat is unchanged (`ceil` returns the
        boundary itself).

        **Under 60 BPM only** (`KIAI_PULSE_CARRY_MIN_BEAT_MS`). Above it the
        beat is short enough that a section ending inside one simply stops,
        which is what the end of a chorus should look like; carrying it there
        would hold light past a kiai the map has finished with. Only a beat
        long enough for the cut to land on a flash still visibly fading gets
        to finish.

        Anchored on the section rather than on the playhead: past `band_end`
        there is no kiai to look up, and the fade has to keep the phase it
        started with.
        """
        point = pulse_beat_point(self.beat_points, band_start)
        if point is None or point.beat_length <= KIAI_PULSE_CARRY_MIN_BEAT_MS:
            return None
        span = band_end - band_start
        finish = band_start + math.ceil(span / point.beat_length) * point.beat_length
        return band_start if self.current_time < finish else None

    def _has_been_hit(self, note) -> bool:
        """Whether `note`'s circle head is played, and so off the screen.

        osu!taiko takes a circle away the instant it reaches the hit position:
        that *is* what being hit looks like. Letting it carry on past the
        target is a thing the game never shows.

        A **fake slider** counts too, but only its head: what is left behind is
        the region it draws and never lets anybody hit, which is the whole
        point of the object and the thing worth still seeing.

        A real drumroll is still being hit while its body crosses the target,
        so it travels through and out the far side; a spinner occupies its
        whole span in place. Neither is ever "hit" in this sense.
        """
        if note.is_spinner or note.time > self.current_time:
            return False
        return not (note.is_slider and self._note_end_time(note) is not None)

    def _draw_note(self, painter, note, center_y, normal_radius, big_radius) -> None:
        x = self.x_for_time(note.time)
        radius = big_radius if (note.is_finisher or note.is_spinner) else normal_radius
        end_time = self._note_end_time(note)
        # A slider or spinner body travels with its *head*: it is one object at
        # one speed, not a series of points each reading its own timing.
        end_x = (
            x + (end_time - note.time) * self.velocity_at(note.time) * self.px_per_beat
            if end_time is not None and end_time > note.time
            else x
        )
        # A fake slider is drawn forward of its head and outlives it, so
        # neither this cull nor the hit test below describes it. It does both
        # for itself.
        if note.is_slider and end_time is None:
            self._draw_fake_slider(painter, note, x, center_y, radius)
            return
        # The time-range slice is bounded by the slowest section in the map, so
        # a fast one lands objects well off either edge; drop those here.
        if end_x + radius < 0 or x - radius > self.width():
            return

        if note.is_spinner:
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.spinner_band_brush)
            painter.drawRoundedRect(
                QRectF(x, center_y - radius, max(1.0, end_x - x), radius * 2), radius, radius
            )
            pixmap = spinner_pixmap()
            if not pixmap.isNull():
                size = radius * 2
                painter.drawPixmap(
                    QRectF(x - radius, center_y - radius, size, size), pixmap, QRectF(pixmap.rect())
                )
            return

        if note.is_slider:
            if self._draw_skinned_roll(
                painter, x, end_x, center_y, radius, note.is_finisher):
                return
            painter.setPen(self.note_pen)
            painter.setBrush(self.slider_brush)
            painter.drawRoundedRect(
                QRectF(x - radius, center_y - radius, max(2 * radius, end_x - x + 2 * radius), radius * 2),
                radius,
                radius,
            )
            return

        if self._has_been_hit(note):
            return
        alpha = self.hidden_alpha(x)
        if alpha <= 0.0:
            return
        if alpha < 1.0:
            painter.save()
            painter.setOpacity(painter.opacity() * alpha)
        draw_note_sprite(
            painter, self.kat_brush if note.is_kat else self.don_brush, self.note_pen,
            x, center_y, radius, self.skin,
            big=note.is_finisher or note.is_spinner,
        )
        if alpha < 1.0:
            painter.restore()

    def _draw_playfield(self, painter, center_y, normal_radius, big_radius, pulse) -> None:
        """The skin's playfield, back to front, under everything else.

        The bar, then the bar's kiai overlay, which pulses with the notes. A
        skin without a bar gets the built-in lane, the same per-element
        fallback the notes use.

        No `taiko-glow` or `lighting`: those are a bloom around the hit target,
        and nothing is being judged there. Kiai shows as the notes pulsing and
        the lane taking its kiai state, both of which say something.
        """
        if self.plain_stage:
            return
        width, height = self.width(), self.height()
        skin = self.skin
        # `taiko-slider` (the scrolling background) is not drawn: owner's call,
        # 2026-10-07 -- the bar is the lane. Without a bar, the built-in lane.
        bar = skin.stretched("taiko-bar-right", width, height)
        if bar is None:
            painter.fillRect(
                QRectF(0, center_y - big_radius * 1.2, width, big_radius * 2.4),
                self.lane_brush,
            )
            return

        painter.drawPixmap(QPointF(0.0, 0.0), bar)
        if pulse > 0.0:
            # Pulses with the notes: `pulse` is the same per-beat flash.
            glow = skin.stretched("taiko-bar-right-glow", width, height)
            if glow is not None:
                opacity = painter.opacity()
                painter.setOpacity(opacity * pulse * KIAI_GLOW_STRENGTH)
                painter.drawPixmap(QPointF(0.0, 0.0), glow)
                painter.setOpacity(opacity)


    def _hit_ring(self) -> QPixmap | None:
        """The skin's `approachcircle` as ppy/osu `TaikoLegacyHitTarget` draws
        it: its own size, @2x counted as half, at HIT_TARGET_RING_SCALE of the
        200-unit playfield -- the same rule as the grey note under it."""
        return self.skin.natural("approachcircle", self.height() / PLAYFIELD_UNIT * HIT_TARGET_RING_SCALE)

    def _roll_height(self, element: str, diameter: int, big: bool = False) -> int:
        """A roll piece's drawn height, so its *ink* matches the head's.

        Skins pad the elements differently -- one real skin fills 0.83 of
        `taikohitcircle`'s box and 1.00 of `taiko-roll-end`'s -- so scaling both
        to the note diameter drew a cap visibly larger than the note it caps,
        with its yellow showing past the note's edge. Dividing by each one's own
        ink ratio makes them agree however a skin padded them.

        The track and the cap both, against the head they belong to. Only the
        cap used to be, so in a skin whose track and cap are padded alike
        (0.93 each, head 0.99) the cap stood 3px proud of the body above and
        below, 4-5px on a big roll (owner, 2026-10-24).
        """
        head_ink = self.skin.ink_height("taikobigcircle" if big else "taikohitcircle")
        ink = self.skin.ink_height(element)
        if ink <= 0.0:
            return diameter
        return max(1, round(diameter * head_ink / ink))

    def _draw_skinned_roll(
        self, painter, x, end_x, center_y, radius, big: bool = False,
    ) -> bool:
        """A drumroll as its three skin pieces: head, stretched body, tail cap.

        `taikohitcircle` + `taiko-roll-middle` + `taiko-roll-end`, laid out the
        way the skinning wiki specifies them:

        * the head is the ordinary note circle in the drumroll's colour, which
          is what osu! does (`DrumRollCirclePiece` is a circle piece with the
          drumroll accent);
        * `taiko-roll-middle` is **1px wide, origin TopLeft** -- it is the
          track, meant to be stretched the length of the body rather than
          tiled, so it is drawn from the head's own x across the span;
        * `taiko-roll-end` is **64x128, origin TopLeft** -- "the end part of a
          roll". TopLeft, so it butts onto the far end of the track and reaches
          *past* it by its own width, exactly as the head reaches back past the
          start by its radius. Centred on the end (which is what this did) the
          roll came up half a radius short.

        Only a **real** roll reaches here. A negative length is a fake slider,
        which is a bare track with no head and no cap -- a different object,
        drawn by `_draw_fake_slider`.

        All three tint from the drumroll colour, multiplicatively, which is the
        blend mode the wiki gives for both roll pieces.
        """
        diameter = round(radius * 2)
        cap = self._roll_height("taiko-roll-end", diameter, big)
        track = self._roll_height("taiko-roll-middle", diameter, big)
        middle = self.skin.scaled("taiko-roll-middle", track, self.slider_brush)
        if middle is None:
            return False
        body_end = max(end_x, x)
        if body_end > x:
            painter.drawPixmap(
                QRectF(x, center_y - track / 2.0, body_end - x, track),
                middle, QRectF(middle.rect()),
            )
        tail = self.skin.scaled("taiko-roll-end", cap, self.slider_brush)
        if tail is not None:
            draw_pixmap_subpixel(painter, tail, body_end, center_y - tail.height() / 2.0)
        # `big` matters here as much as it does for a finisher note: a big
        # drumroll's head is `taikobigcircle`, which skinners redraw rather
        # than scale up -- some are a different shape, some carry a second ring
        # the small one does not have. Scaling `taikohitcircle` to the finisher
        # diameter drew the wrong artwork at the right size.
        draw_note_sprite(
            painter, self.slider_brush, self.note_pen, x, center_y, radius,
            self.skin, big=big)
        return True

    def _draw_fake_slider(self, painter, note, x, center_y, radius) -> None:
        """A negative-length fake slider: a head, and a track reaching right.

        `taikohitcircle` in the drumroll colour, the same head a real roll has,
        plus `taiko-roll-middle` laid from the object's own x and reaching to
        its **right** by the magnitude of the negative length. No
        `taiko-roll-end`: the cap belongs to a roll that ends, and this one
        ends before it starts. At the hit position the whole thing disappears
        with nothing left travelling on.

        The track is drawn **to scale**, which is why the two ends of the range
        look like different objects and are not. A `-800` track is over a
        thousand pixels of the same yellow at the same height, so the head sits
        at its left edge and reads as part of the bar; the canonical `-0.0001`
        collapses the track to nothing and leaves the head as the only thing
        on screen. Padding the track to visibility would invent geometry the
        map does not have, and dropping the head because a long one hides it
        made every short fake slider -- which is most of them -- invisible.

        It appears at full opacity like any other object. There is no fade-in:
        a stack of them is a stack of identical opaque objects and reads as
        one, and what makes a shiny note brighter is the kiai stamp each of
        them takes, not any difference in how they are drawn.

        **Only the head is taken away at the hit position.** The track and its
        cap pass straight through and carry on out to the left, the way a real
        roll's do -- so the object outlives its own millisecond and cannot be
        culled on where its head is.
        """
        end_time = self._phantom_end_time(note)
        if end_time is None:
            return
        width = abs(end_time - note.time) * self.velocity_at(note.time) * self.px_per_beat
        cap = self._roll_height("taiko-roll-end", round(radius * 2), note.is_finisher)
        # The cap butts onto the far end of the track and reaches past it by its
        # own width (origin TopLeft), so that -- not the head -- is the right
        # edge of what gets drawn.
        if x + width + cap < 0 or x - radius > self.width():
            return
        if width >= 0.5:
            height = self._roll_height("taiko-roll-middle", round(radius * 2), note.is_finisher)
            track = self.skin.scaled("taiko-roll-middle", height, self.slider_brush)
            if track is not None:
                painter.drawPixmap(
                    QRectF(x, center_y - height / 2.0, width, height),
                    track, QRectF(track.rect()),
                )
            else:
                # No skin: the same track in the drumroll colour, since the
                # built-in lane has no art of its own.
                painter.setPen(Qt.NoPen)
                painter.setBrush(self.slider_brush)
                painter.drawRect(QRectF(x, center_y - radius, width, radius * 2))
        tail = self.skin.scaled("taiko-roll-end", cap, self.slider_brush)
        if tail is not None:
            draw_pixmap_subpixel(painter, tail, x + width, center_y - tail.height() / 2.0)
        else:
            # Without a skin there is no cap art, so the stand-in is the
            # drumroll colour in the shape a built-in drumroll ends in: a disc.
            painter.setPen(self.note_pen)
            painter.setBrush(self.slider_brush)
            painter.drawEllipse(
                QPointF(x + width + radius / 2.0, center_y), radius, radius)
        if not self._has_been_hit(note):
            # Last, so it sits on the near end of its own track the way a real
            # roll's head does -- and gone once played, like any circle.
            draw_note_sprite(
                painter, self.slider_brush, self.note_pen, x, center_y, radius,
                self.skin, big=note.is_finisher,
            )
