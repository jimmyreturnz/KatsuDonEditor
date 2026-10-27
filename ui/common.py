"""Helpers and constants shared by the main window and the modules split out of it."""
from __future__ import annotations
import math
import operator
from pathlib import Path
from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QImage, QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap, QIcon
from PySide6.QtWidgets import (
    QFrame, QPushButton, QScrollArea, QSizePolicy, QStyle, QStyleOptionButton, QWidget,
)
from app.security_utils import resource_roots
from ui import theme
from osu_io.timing import (
    SLIDER_MULTIPLIER_ASSUMED, active_uninherited_at, duration_for_slider_length, sv_at,
)
from library.song_library import most_common_beat_length as library_common_beat_length


def application_icon() -> QIcon:
    relative_candidates=(
        Path("assets/icons/FancyTaikoEditor_Logo.ico"),
        Path("assets/icons/FancyTaikoEditor_Logo.png"),
        Path("assets/FancyTaikoEditor_Logo.ico"),
        Path("FancyTaikoEditor_Logo.ico"),
        Path("icon.ico"),
    )
    for root in resource_roots():
        for relative in relative_candidates:
            candidate=root/relative
            if candidate.is_file():
                icon=QIcon(str(candidate))
                if not icon.isNull():return icon
    return QIcon()


def button_chrome_width(button: QPushButton) -> int:
    """Pixels `button` spends on padding and border before any text at all.

    Measured, not assumed: the gap between a rect and the contents rect the
    button's *own current style* hands back for it, which is where the window
    stylesheet's `padding: 8px 16px` and any per-button override of it
    actually show up. Measured against a rect widened by the label, because
    the buttons this exists for are exactly the ones currently too narrow to
    hold their own padding, and a style clamps the contents rect at zero
    rather than reporting the overflow.

    **In the checked state as well as the current one**, which is the half of
    the clipping report that survived every previous fix. `QPushButton:checked`
    is `border: 1px solid` against the base rule's `border: 0`, so checking a
    button costs it two pixels of chrome that were never in the width it was
    pinned to -- and a tool row's selected button is the one every gimmick
    layer is looking at. `button_text_width` already measured the checked
    *font*; this is the other thing that rule changes.
    """
    button.ensurePolished()
    option = QStyleOptionButton()
    option.initFrom(button)
    text_width = button.fontMetrics().horizontalAdvance(button.text())
    height = max(1, option.rect.height())
    width = option.rect.width() + text_width
    states = [option.state]
    if button.isCheckable():
        states.append(option.state | QStyle.StateFlag.State_On)
    widest = 0
    for state in states:
        option.state = state
        option.rect = QRect(0, 0, width, height)
        contents = button.style().subElementRect(QStyle.SE_PushButtonContents, option, button)
        widest = max(widest, width - contents.width())
    return max(0, widest)


def button_text_width(button: QPushButton) -> int:
    """The width `button` needs before its own body starts covering its label.

    This is the fix for the typed widths that used to clip. A 28px "+" under
    the app-wide 16px of padding either side has -4px left for the glyph, so
    Qt drew the pink body straight over both ends of it -- the "buttons eat
    their own text at the left and right" report -- and every attempt to nudge
    the number was a guess at a total the stylesheet already decides. Both
    halves are read back instead: the label from the button's font metrics,
    the padding and border from its style. A longer translation, a bigger font
    or a different padding each widen the button by exactly their cost.

    sizeHint() is still the floor, so an icon, a menu indicator or a style
    minimum is never sized away.

    A second cause layers on top of the first for any checkable button: the
    `QPushButton:checked` rule bumps `font-weight` to 700, but this function
    was measuring with the button's own (unchecked, lighter) font -- so the
    width was pinned before the button ever got heavier, and checking it
    clipped the label again. The advance is now measured with the current
    font AND a bold copy of it, and the wider of the two wins.

    The bold label's extra width is added to the sizeHint floor as well: a
    menu button's hint holds its arrow, and the floor won over the bold
    label without paying for the bold. Exo 2's bold is a few pixels wider;
    NSO's pixel face has no bold, Qt's synthetic one is eight wider, and the
    checked Autoplay lost its first letter.
    """
    metrics_width = button.fontMetrics().horizontalAdvance(button.text())
    bold_width = 0
    if button.isCheckable():  # only a checked button is drawn bold
        bold_font = QFont(button.font())
        bold_font.setWeight(QFont.Weight.Bold)
        bold_width = QFontMetrics(bold_font).horizontalAdvance(button.text())
    return max(
        button.sizeHint().width() + max(0, bold_width - metrics_width),
        max(metrics_width, bold_width) + button_chrome_width(button),
    )


def equalize_button_widths(buttons, heights: bool = False, widths: bool = True) -> None:
    """Give every button the width of the widest one.

    Polished first: before the style has been applied a button reports the
    unpadded hint, and the widest label is exactly the one that then no longer
    fits. The width itself comes from `button_text_width`, which measures the
    label and the real padding separately rather than trusting that hint.

    `heights` does the same for the tallest, and is how the tool rows are sized:
    a typed height clipped descenders once the window stylesheet's padding was
    applied, and the hint is the one number that already accounts for the font,
    the padding and the border together.

    `widths=False` keeps every button at its own natural width and equalizes
    only the height. Uniform width is worth having while a row is a handful of
    short labels, and unaffordable once it is not: the gimmick fake slider row
    reached ten buttons, and giving all of them the width of "Multiple Fake
    Slider" -- across all six layers, since they are sized as one set -- came
    to 2798px, which is wider than the monitor. A ragged row that fits beats a
    tidy one you cannot reach the end of.
    """
    for button in buttons:
        button.ensurePolished()
    hints = [button.sizeHint() for button in buttons]
    # button_text_width rather than the hint: the hint is what let the widest
    # label clip, since it is the one that then has no padding left over.
    width = max((button_text_width(button) for button in buttons), default=0)
    height = max((hint.height() for hint in hints), default=0)
    for button in buttons:
        if widths:
            button.setFixedWidth(width)
        else:
            # Not "leave it to sizeHint". Qt measures that hint in the font the
            # button reports, and under a stylesheet a *checked* button is
            # painted at font-weight 700 while `font()` still says 600 -- so
            # the hint is a couple of pixels short of what the selected button
            # in every tool row is actually drawn with, and it clipped the last
            # letter of its own label. A floor rather than a fixed width, so a
            # ragged row still is one.
            button.setMinimumWidth(button_text_width(button))
        if heights:
            button.setFixedHeight(height)


def scroll_row(row: QWidget) -> QScrollArea:
    """`row` on one line, scrolling sideways when the window is narrower.

    A row of controls in a plain layout is the window's minimum width: the
    Fancy sub-toolbar alone was ~1800px and ran anything narrower off the
    screen. Here it keeps its line and gets a thin bar instead. Call it once
    the row is full: the height is the row's own.
    """
    scroll = QScrollArea()
    scroll.setWidget(row)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    # The area and its viewport are clear; the row keeps its own fill, which
    # for the Editor's `opaque_strip` is the whole point of it.
    scroll.setStyleSheet(
        "QScrollArea, QScrollArea > QWidget { background: transparent; }"
        "QScrollBar:horizontal { background: transparent; height: 6px; }"
        "QScrollBar::handle:horizontal { background: #3a4554; border-radius: 3px; min-width: 32px; }"
        "QScrollBar::handle:horizontal:hover { background: #ff66aa; }"
        "QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page { width: 0; background: none; }")
    # Its own height, plus the scroll bar's when the row is too wide.
    scroll.setFixedHeight(row.sizeHint().height() + 8)
    return scroll


# Kiai flash. Notes in a kiai section brighten on every 1/1 beat and fade out
# across it. Stacked objects -- a fake slider dropped on top of a note -- each
# paint their own overlay, so a stack reads brighter than a lone note without
# anything having to count them. Drumroll yellow rather than white: the shine
# mappers stack fake sliders for is that yellow showing through.
# The note colours, and the ones every note part is tinted from. One place
# each, because the editor timeline and the gameplay preview draw the same
# objects and had drifted to four slightly different values between them.
DON_COLOR = (229, 76, 46)


KAT_COLOR = (67, 141, 171)


# The drumroll yellow, and the colour every drumroll part is tinted from --
# head, `taiko-roll-middle` and `taiko-roll-end` alike. One constant because
# the editor timeline and the gameplay preview draw the same object and had
# drifted to two slightly different yellows.
DRUMROLL_COLOR = (251, 183, 6)
# `sliderscorepoint` on a drumroll: its yellow, halfway to white so it shows on
# the body. taiko-rs's ROLL_TICK.
DRUMROLL_TICK_COLOR = (253, 219, 131)


def format_time(time_ms: int) -> str:
    value = max(0, int(time_ms))
    return f"{value // 60000:02d}.{(value % 60000) // 1000:02d}.{value % 1000:03d}"


def osu_timestamp(time_ms: float, indexes=()) -> str:
    """osu!'s editor link, `01:27:273 (777,778) - `, as the beatmap discussion
    page reads it. The numbers are 1-based object indexes in file order, not
    combo numbers: taiko has no combos to count. No objects, no brackets."""
    value = max(0, int(time_ms))
    stamp = f"{value // 60000:02d}:{(value % 60000) // 1000:02d}:{value % 1000:03d}"
    if indexes:
        stamp += f" ({','.join(str(index) for index in indexes)})"
    return stamp + " - "


_SPINNER_PIXMAP: QPixmap | None = None


def spinner_pixmap() -> QPixmap:
    """assets/skins/spinner.png, loaded once and cached.

    Constructing a QPixmap requires a live QApplication, so this is loaded
    lazily on first paint rather than at import time.
    """
    global _SPINNER_PIXMAP
    if _SPINNER_PIXMAP is None:
        _SPINNER_PIXMAP = QPixmap()
        for root in resource_roots():
            candidate = root / "assets" / "skins" / "spinner.png"
            if candidate.is_file():
                loaded = QPixmap(str(candidate))
                if not loaded.isNull():
                    _SPINNER_PIXMAP = loaded
                    break
    return _SPINNER_PIXMAP



def drumroll_tick_times(note, beat_points, timing_points, slider_multiplier) -> list[float]:
    """Where a real drumroll's ticks fall: every 1/4 beat of its red line,
    from the head to the end -- 1/8 at 125 BPM or slower, 1/2 above 250, as
    osu!taiko does (taiko-rs's `beatmap.rs` has the same rule). A fake slider
    -- no positive duration -- has none. SliderTickRate 3 (1/3 beat) is not
    read by the parser, so not here.
    """
    if not note.is_slider or note.length is None or note.slides is None or note.is_nan_slider:
        return []
    timing = active_uninherited_at(beat_points, note.time)
    duration = duration_for_slider_length(
        note.length * note.slides, timing.beat_length,
        sv_at(timing_points, note.time), slider_multiplier)
    divisor = 4
    if timing.beat_length > 0:
        bpm = 60000.0 / timing.beat_length
        divisor = 8 if bpm <= 125 else 2 if bpm > 250 else 4
    tick = timing.beat_length / divisor
    if duration <= 0 or tick <= 0:
        return []
    count = int(duration / tick + 1e-6) + 1
    return [note.time + index * tick for index in range(count)]


_NOTE_SPRITES: dict[tuple, QPixmap] = {}


# Quarter-pixel horizontal placement for anything that scrolls.
SUBPIXEL_PHASES = 4


_SUBPIXEL_SHIFTED: dict[tuple[int, int], QPixmap] = {}


def _shifted_image(image: QImage, f: float) -> QImage:
    """`image` moved `f` of a pixel right: each pixel (1 - f) of itself plus
    f of its left neighbour, on premultiplied bytes, one pixel wider.

    Byte arithmetic rather than a QPainter blend: CompositionMode_Plus with an
    opacity does not add linearly in Qt (a half-shifted opaque sprite came out
    at alpha 191 with its colours brightened), and a shifted note has to be
    exactly the colour it was. Two 256-entry tables and a C-level map add, so
    a sprite costs about a millisecond, once.
    """
    source = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    width, height = source.width(), source.height()
    stride = source.bytesPerLine()
    data = bytes(source.constBits())
    keep = bytes(int(v * (1.0 - f) + 0.5) for v in range(256))
    # What `keep` left of v, so equal neighbours -- a sprite's flat interior --
    # come back exactly v, and no sum can pass 255.
    carry = bytes(v - keep[v] for v in range(256))
    pad = b"\0\0\0\0"
    rows = []
    for y in range(height):
        row = data[y * stride:y * stride + width * 4]
        rows.append(bytes(map(operator.add, (row + pad).translate(keep), (pad + row).translate(carry))))
    shifted = QImage(b"".join(rows), width + 1, height, (width + 1) * 4,
                     QImage.Format_ARGB32_Premultiplied)
    # QImage over a Python buffer does not own it; copy before it is freed.
    return shifted.copy()


def draw_pixmap_subpixel(painter: QPainter, pixmap: QPixmap, x: float, y: float) -> None:
    """`pixmap` with its top-left at `x, y`, placed to a quarter pixel across.

    Qt's raster engine puts an unscaled pixmap on a whole pixel whatever the
    QPointF says -- measured, with and without SmoothPixmapTransform, and via
    a same-size QRectF target too. A note scrolling 1.4px a frame therefore
    moved 1, 1, 2, 1, 2 pixels: the horizontal judder in the gameplay preview
    (owner, 2026-10-11), which osu! does not have because it draws on the GPU
    at sub-pixel positions.

    So each sprite gets copies shifted by 1/4, 1/2 and 3/4 of a pixel, made
    once: (1 - f) of the sprite plus f of it one pixel over, added with
    CompositionMode_Plus, which on premultiplied pixels is exactly a linear
    resample. Drawing is still one unscaled blit.
    """
    ratio = pixmap.devicePixelRatio()
    if painter.device().devicePixelRatioF() != ratio:
        # A pixmap at another density is resampled by the draw already.
        painter.drawPixmap(QPointF(x, y), pixmap)
        return
    device_x = x * ratio
    whole = math.floor(device_x)
    phase = round((device_x - whole) * SUBPIXEL_PHASES)
    if phase == SUBPIXEL_PHASES:
        whole, phase = whole + 1, 0
    if phase:
        key = (pixmap.cacheKey(), phase)
        shifted = _SUBPIXEL_SHIFTED.get(key)
        if shifted is None:
            image = _shifted_image(pixmap.toImage(), phase / SUBPIXEL_PHASES)
            image.setDevicePixelRatio(ratio)
            # Cleared wholesale like _NOTE_SPRITES: keys only multiply when the
            # view is resized, which makes every sprite size new anyway.
            if len(_SUBPIXEL_SHIFTED) > 512:
                _SUBPIXEL_SHIFTED.clear()
            _SUBPIXEL_SHIFTED[key] = shifted = QPixmap.fromImage(image)
        pixmap = shifted
    painter.drawPixmap(QPointF(whole / ratio, y), pixmap)


def draw_note_sprite(
    painter: QPainter, fill: QColor, outline: QPen, x: float, y: float, radius: float,
    skin=None, big: bool = False, selected: bool = False,
) -> None:
    """Draw a note circle from a cached pixmap instead of stroking an ellipse.

    With a skin loaded the circle is the skin's own artwork: its base with
    `fill` multiplied in, and its overlay laid on top untinted. `fill`'s
    **alpha** is carried across as painter opacity rather than into the tint,
    because the editor's chart views deliberately draw notes semi-transparent
    so the snap grid stays visible through them -- skin art is opaque, and
    multiplying by a translucent colour would not have reproduced that.

    `selected` strokes `outline` over the artwork. Only the unskinned path bakes
    the pen into its sprite, so with a skin loaded the selection outline was
    silently dropped and a selected note looked exactly like an unselected one.
    Gated rather than always drawn, because a skin's circle has its own rim and
    ringing every note would be drawing a selection that is not there.

    A filled, stroked, antialiased ellipse costs the raster engine ~70us; the
    same circle blitted costs ~2us. A screenful of a dense map is 3ms a frame
    against 0.3ms, which is the difference between stuttering and not. Notes are
    identical circles in a handful of colours and two sizes, so there is nothing
    to redraw per frame in the first place.
    """
    if skin is not None:
        element = "taikobigcircle" if big else "taikohitcircle"
        diameter = round(radius * 2)
        base = skin.scaled(element, diameter, QColor(fill.red(), fill.green(), fill.blue()))
        if base is not None:
            previous = painter.opacity()
            painter.setOpacity(previous * fill.alphaF())
            # At the art's own scaled size, not stretched into a radius*2 box:
            # the box was the same size give or take a fraction of a pixel, so
            # the draw snapped to a whole pixel and scrolling notes judder.
            draw_pixmap_subpixel(painter, base, x - base.width() / 2.0, y - base.height() / 2.0)
            overlay = skin.scaled(element + "overlay", diameter)
            if overlay is not None:
                draw_pixmap_subpixel(
                    painter, overlay, x - overlay.width() / 2.0, y - overlay.height() / 2.0)
            painter.setOpacity(previous)
            if selected:
                painter.setPen(outline)
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(x, y), radius, radius)
            return

    ratio = painter.device().devicePixelRatioF()
    radius_px = round(radius)
    key = (fill.rgba(), outline.color().rgba(), round(outline.widthF() * 2), radius_px, ratio)
    sprite = _NOTE_SPRITES.get(key)
    if sprite is None:
        size = radius_px * 2 + 4
        sprite = QPixmap(round(size * ratio), round(size * ratio))
        sprite.setDevicePixelRatio(ratio)
        sprite.fill(Qt.transparent)
        sprite_painter = QPainter(sprite)
        sprite_painter.setRenderHint(QPainter.Antialiasing, True)
        sprite_painter.setPen(outline)
        sprite_painter.setBrush(fill)
        sprite_painter.drawEllipse(QPointF(size / 2, size / 2), radius_px, radius_px)
        sprite_painter.end()
        # ponytail: cleared wholesale rather than evicted by age. Keys are
        # (colour, size, dpr) combinations, so this only grows while the window
        # is being resized; an LRU would be more code than the problem.
        if len(_NOTE_SPRITES) > 64:
            _NOTE_SPRITES.clear()
        _NOTE_SPRITES[key] = sprite
    half = sprite.width() / (2 * ratio)
    draw_pixmap_subpixel(painter, sprite, x - half, y - half)


# `TaikoStrongableHitObject.STRONG_SCALE = 1 / 0.65f`, i.e. a finisher is about
# 1.538x a normal note, not the 1.4x assumed here before.
TAIKO_STRONG_SCALE = 1.0 / 0.65


# --- Playfield geometry, in osu!'s own units --------------------------------
# `TaikoPlayfield.BASE_HEIGHT = 200`, and every playfield element in the
# skinning wiki is sized against that same 200: the bar is 1024x200, the
# barline 4x175, the scrolling background 776x162. So
# this view's height *is* the 200, and each fraction below is the wiki's number
# over 200 -- which is also what keeps the note at `TAIKO_NOTE_SIZE` of the
# height without the two systems disagreeing.
PLAYFIELD_UNIT = 200.0


# One osu!stable gamefield unit (a 480-tall screen), as a fraction of the
# playfield height. The playfield is BASE_HEIGHT 200 of lazer's 768-tall screen
# (`TaikoPlayfieldAdjustmentContainer`), so a stable unit is 768/480/200 of it.
# Horizontal scroll and the visible range are stable units in osu!'s own code,
# so this is what ties them to the view height the notes are already sized by.
STABLE_UNIT_PER_PLAYFIELD = 768.0 / 480.0 / PLAYFIELD_UNIT


# osu!lazer's `LockPlayfieldAspectRange` (on by default) clamps the screen
# aspect it computes the visible time from to at most 16:9: a wider screen
# makes the playfield taller rather than showing more of the chart. So a view
# wider than that has a right edge past which osu! shows nothing, and the
# preview dims it. False is osu!stable's (and lazer-unlocked's) behaviour, where
# a wider screen simply shows more -- one switch, pending the owner's call.
GAMEPLAY_MAX_ASPECT = 16.0 / 9.0


# The gameplay preview's height floor, below which a note is too small to read.
GAMEPLAY_MIN_HEIGHT = 170


def osu_screen_height_for(width: int) -> int:
    """The preview height at which `width` is exactly a 16:9 osu! screen:
    the screen is 480 stable units high and 16/9 of that wide, and the view's
    height is the playfield's 200 units (`STABLE_UNIT_PER_PLAYFIELD`)."""
    screen_width_per_height = STABLE_UNIT_PER_PLAYFIELD * 480.0 * GAMEPLAY_MAX_ASPECT
    return max(GAMEPLAY_MIN_HEIGHT, round(width / screen_width_per_height))


SCROLL_MODS = ("HR", "EZ")


def most_common_beat_length(points, last_time: float) -> float | None:
    """`song_library.most_common_beat_length` over timing points: the density
    view's tempo and the difficulty rows' "(250)" are one rule."""
    return library_common_beat_length(
        [(point.time, point.beat_length) for point in points if point.uninherited], last_time)


_view_ground_alpha = 255


def set_view_ground_alpha(percent: int) -> None:
    """Set View opacity for every ground drawn after this. Here rather than in
    set_view_opacity, which stays with the widgets in gui.py: a `global` there
    rebinds gui's copy of the name, and view_ground reads this module's."""
    global _view_ground_alpha
    _view_ground_alpha = round(max(0, min(100, int(percent))) * 2.55)


def view_ground_is_opaque() -> bool:
    return _view_ground_alpha >= 255


def view_ground(colour: str) -> QColor:
    """A lane's ground colour at the View opacity in force, in every theme.

    NSO's lanes were held opaque while its floor was the sticker wallpaper;
    the floor is a pink tint now, and the setting did nothing there (owner,
    2026-10-12).
    """
    ground = theme.screen(colour)
    ground.setAlpha(_view_ground_alpha)
    return ground


# The app's accent, used by the global button style and anywhere a widget has
# to reproduce it in code rather than in a stylesheet.
ACCENT_PINK = "#f3a6bd"


def display_frame_interval_ns(screen) -> int:
    """One refresh of `screen`, the cadence anything scrolling should paint at.

    A fixed 120fps on the owner's 99.93Hz monitor is 1.2 frames per refresh,
    so every fifth refresh showed a frame that had moved twice as far -- a
    20Hz judder in the gameplay preview (owner, 2026-10-11) that no amount of
    paint speed fixes. A rate Qt cannot report falls back to 120.
    """
    rate = screen.refreshRate() if screen is not None else 0.0
    if not 30.0 <= rate <= 500.0:
        rate = 120.0
    return round(1_000_000_000 / rate)


def next_frame_due(due_ns: int, now_ns: int, interval_ns: int) -> int | None:
    """When the frame after this one is due, or None if this one is not yet.

    Scheduled from the fixed cadence, not from "now": a timer tick firing a
    few hundred microseconds late then no longer pushes every later frame
    later too, which is what made a restart()-every-tick scheme's spacing
    uneven. Behind by more than a whole interval (a stall), it resyncs
    rather than firing a burst of catch-up frames.
    """
    if now_ns < due_ns:
        return None
    due_ns += interval_ns
    return due_ns if due_ns >= now_ns else now_ns + interval_ns


# The song select's chart preview, as a share of a gameplay view's height.
SONG_SELECT_PREVIEW_DEFAULT_PERCENT = 67


def tabular(font: QFont) -> QFont:
    """Exo 2's tabular figures, so the stat columns line up digit for digit."""
    font = QFont(font)
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
    except (AttributeError, TypeError):
        pass  # A Qt without OpenType features: proportional figures, still legible.
    return font
