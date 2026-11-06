"""Helpers and constants shared by the main window and the modules split out of it."""
from __future__ import annotations
import math
import operator
from pathlib import Path
from PySide6.QtCore import QLine, QPoint, QPointF, QRect, QRectF, Qt
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
# drifted to two slightly different yellows. Stable's own drumroll is
# `Color(252, 184, 6)`, read from osu!.exe (2026-10-30).
DRUMROLL_COLOR = (252, 184, 6)
# `sliderscorepoint` on a drumroll: its yellow, halfway to white so it shows on
# the body. taiko-rs's ROLL_TICK.
DRUMROLL_TICK_COLOR = (255, 255, 0)
# A drumroll tick as stable tints it (owner's screenshot, IT'S SOOOO
# CREATIVE): DRUMROLL_TICK_COLOR on the beat, this on the half beat, and the
# image's own colour elsewhere (`tick_tint`). taiko-rs's ROLL_TICK_* match.
DRUMROLL_HALF_TICK_COLOR = (255, 230, 150)


def tick_tint(time_ms: float, origin_ms: float, beat_length: float) -> tuple | None:
    """A roll tick's tint at `time_ms` on its red line's beat grid: yellow on
    a beat, pale orange on a half beat, None (untinted) elsewhere."""
    if beat_length <= 0:
        return None
    for division, colour in ((1.0, DRUMROLL_TICK_COLOR), (2.0, DRUMROLL_HALF_TICK_COLOR)):
        x = (time_ms - origin_ms) / beat_length * division
        if abs(x - round(x)) < 0.02 * division:
            return colour
    return None


def format_time(time_ms: int) -> str:
    value = max(0, int(time_ms))
    return f"{value // 60000:02d}.{(value % 60000) // 1000:02d}.{value % 1000:03d}"


def break_periods(lines) -> list[tuple[float, float]]:
    """[Events]' breaks (`2,start,end` or `Break,start,end`) in a .osu's lines."""
    out, section = [], None
    for line in lines:
        text = line.strip()
        if text.startswith("["):
            section = text
        elif section == "[Events]":
            fields = text.split(",")
            if fields[0] in ("2", "Break") and len(fields) >= 3:
                try:
                    out.append((float(fields[1]), float(fields[2])))
                except ValueError:
                    pass
    return out


def combo_numbers(hit_objects, breaks=()):
    """(combo number, object) in file order, as osu!'s editor numbers them for
    a timestamp: counting from 1 again at every new combo and after every
    break (owner, 2026-10-28). A spinner, and the object after one, always
    start a new combo there."""
    number, after_spinner, previous = 0, False, None
    for index, note in enumerate(hit_objects):
        after_break = previous is not None and any(
            previous <= start and note.time >= end for start, end in breaks)
        if index == 0 or note.is_new_combo or note.is_spinner or after_spinner or after_break:
            number = 0
        previous = note.time
        number += 1
        after_spinner = note.is_spinner
        yield number, note


def osu_timestamp(time_ms: float, indexes=()) -> str:
    """osu!'s editor link, `01:27:273 (777,778) - `, as the beatmap discussion
    page reads it. The numbers are combo numbers (`combo_numbers`). No
    objects, no brackets."""
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



def spans_circles(start_ms: float, end_ms: float, circle_times) -> bool:
    """Whether a don or kat (`circle_times`, sorted) lies in [start, end]. A
    drumroll mapped across notes is played -- and heard -- as the notes alone
    (owner, 2026-10-28): autoplay skips its ticks and so do its hitsounds.
    taiko-rs's `roll_over_notes` is the same rule."""
    from bisect import bisect_left, bisect_right
    return bisect_right(circle_times, end_ms) > bisect_left(circle_times, start_ms)


def drumroll_tick_times(note, beat_points, timing_points, slider_multiplier) -> list[float]:
    """Where a real drumroll's ticks fall, from the head to the end: 1/8 of
    its red line's beat, doubled while under 60ms and halved while over
    120ms, so always 60-120ms apart whatever the BPM -- osu! stable's
    drumroll, read from osu!.exe (2026-11-04). The 1/4-beat rule this
    replaced (1/8 at 125 BPM or slower, 1/2 above 250) agrees between 63 and
    500 BPM and nowhere else: under a gimmick's 60000 BPM line it ticked
    every 1.5ms, and rolls stacked on one place were hit as a buzz (owner,
    2026-11-04). The spacing uses the line's own beat, unclamped, as stable
    does. A fake slider
    -- no positive duration -- has none. SliderTickRate 3 (1/3 beat) is not
    read by the parser, so not here. Nor does one written 0.1 to -0.1 long:
    +0.0001 is a fake slider too, and three stacked on a note sounded three
    ticks over it (owner, 2026-10-28).

    The beat and SV are clamped as osu! (and taiko-rs) clamp them, 6..60000ms
    and 0.1..10x: a 600px roll under a 0.06ms gimmick line and a near-zero SV
    came to 10^12 ticks, and opening We Love Hardcore (katacheh) at its
    preview point froze the editor (owner, 2026-10-28).
    """
    if not note.is_slider or note.length is None or note.slides is None or note.is_nan_slider:
        return []
    if abs(note.length) <= 0.1:
        return []
    timing = active_uninherited_at(beat_points, note.time)
    if timing.beat_length <= 0:
        return []
    beat_length = min(60000.0, max(6.0, timing.beat_length))
    sv = min(10.0, max(0.1, sv_at(timing_points, note.time)))
    duration = duration_for_slider_length(note.length * note.slides, beat_length, sv, slider_multiplier)
    # ponytail: stable's /6 for SliderTickRate 3, 6 or 1.5 and its pre-v8
    # SV-scaled beat are not read; add with the parser fields.
    tick = timing.beat_length / 8
    while tick < 60.0:
        tick *= 2.0
    while tick > 120.0:
        tick /= 2.0
    if duration <= 0:
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
    resample. Drawing is still one unscaled blit -- so paint with
    SmoothPixmapTransform off: it changes no pixel of an unscaled blit and
    makes it 5x slower (525 barlines of a wall, 9.0ms -> 1.7ms). Toggled
    per call instead it cost a millisecond a frame of its own.
    """
    if is_blank(pixmap):
        return  # a gimmick skin's see-through cap or circle base
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


# (pen, antialiasing, y0, y1) -> (first row, last row) a vertical line covers,
# or None when its rows differ (an antialiased end); see draw_vertical_lines.
_LINE_ROWS: dict[tuple, tuple[int, int] | None] = {}


def _line_rows(pen: QPen, antialiased: bool, y0: int, y1: int) -> tuple[int, int] | None:
    key = (pen.color().rgba(), pen.widthF(), int(pen.capStyle().value), antialiased, y0, y1)
    if key not in _LINE_ROWS:
        top, bottom = min(y0, y1), max(y0, y1)
        margin = int(math.ceil(pen.widthF())) + 2
        image = QImage(16, bottom - top + 2 * margin + 1, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        p = QPainter(image)
        p.setRenderHint(QPainter.Antialiasing, antialiased)
        p.setPen(pen)
        p.drawLine(8, y0 - top + margin, 8, y1 - top + margin)
        p.end()
        stride = image.bytesPerLine()
        bits = bytes(image.constBits())
        rows = [bits[r * stride:r * stride + 64] for r in range(image.height())]
        covered = [r for r, row in enumerate(rows) if any(row)]
        same = bool(covered) and covered == list(range(covered[0], covered[-1] + 1)) and all(
            rows[r] == rows[covered[0]] for r in covered)
        if len(_LINE_ROWS) > 256:
            _LINE_ROWS.clear()
        _LINE_ROWS[key] = (covered[0] + top - margin, covered[-1] + top - margin) if same else None
    return _LINE_ROWS[key]


def draw_vertical_lines(painter: QPainter, pen: QPen, xs, y0: int, y1: int) -> None:
    """`painter.drawLine(x, y0, x, y1)` for every x in `xs`, in one blit.

    The lines share their rows, so the picture is one row repeated: painted
    once with the same pen, hint and order, then stretched. Qt rasterises a
    vertical line row by row, and a 2px pen through its stroker -- the snap
    grid's ticks were 4.8ms of a 7ms chart lane on Nbt-Hwt [The Pharaoh's
    Curse], the barline wall of mekurume [eclosion] 5.9ms of a preview frame.
    Where the rows a line covers are not all alike (an antialiased end cap),
    it draws the lines one by one as before."""
    xs = list(xs)
    if not xs:
        return
    antialiased = painter.testRenderHint(QPainter.Antialiasing)
    rows = _line_rows(pen, antialiased, y0, y1)
    if rows is None:
        painter.setPen(pen)
        painter.drawLines([QLine(x, y0, x, y1) for x in xs])
        return
    width = painter.device().width()
    row = QImage(width, 1, QImage.Format_ARGB32_Premultiplied)
    row.fill(Qt.transparent)
    p = QPainter(row)
    p.setRenderHint(QPainter.Antialiasing, antialiased)
    p.setPen(pen)
    reach = int(math.ceil(pen.widthF())) + 2
    for x in xs:
        p.drawLine(x, -reach, x, reach)
    p.end()
    painter.drawImage(QRect(0, rows[0], width, rows[1] - rows[0] + 1), row)


# pixmap.cacheKey() -> whether every pixel is transparent; see draw_stretched.
_BLANK_PIXMAPS: dict[int, bool] = {}


def is_blank(pixmap: QPixmap) -> bool:
    """Whether every pixel of `pixmap` is transparent, checked once per pixmap."""
    if pixmap.isNull():
        return True  # and constBits() of its image is a null pointer: a crash
    key = pixmap.cacheKey()
    blank = _BLANK_PIXMAPS.get(key)
    if blank is None:
        alpha = pixmap.toImage().convertToFormat(QImage.Format_Alpha8)
        # Row by row: the padding to 4 bytes is not zeroed, and read with the
        # pixels it called Kikuo - Gakkou o Yasunda Hi no Koto's see-through
        # 1x1 track drawn, 150 filtered stretches a frame.
        bits, stride, width = bytes(alpha.constBits()), alpha.bytesPerLine(), alpha.width()
        blank = not any(any(bits[row * stride:row * stride + width]) for row in range(alpha.height()))
        if len(_BLANK_PIXMAPS) > 512:
            _BLANK_PIXMAPS.clear()  # like _SUBPIXEL_SHIFTED: grows only on resize
        _BLANK_PIXMAPS[key] = blank
    return blank


def draw_stretched(painter: QPainter, rect: QRectF, pixmap: QPixmap) -> None:
    """`pixmap` stretched across `rect`, filtered -- unless it is blank.

    A gimmick map hides its roll tracks by shipping a transparent 1x1
    `taiko-roll-middle`, and the stretch is filtered over the whole area
    regardless: 58 invisible pop-up tracks a frame were 3ms of senpai,
    notice me! [w]. Checked once per pixmap."""
    if is_blank(pixmap):
        return
    smooth = painter.testRenderHint(QPainter.SmoothPixmapTransform)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))
    painter.setRenderHint(QPainter.SmoothPixmapTransform, smooth)


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


def view_underlay(widget, colour: str) -> QPixmap | None:
    """`widget`'s ground below 100% View opacity, `colour` at that alpha
    over what shows through it -- the page's map backdrop and the view
    frame's own ground (`view_frame_ground`) -- as one opaque, cached pixmap.
    Everything else between is see-through chrome (`see_through`). Composed
    by hand rather than through `QWidget.render`, which lays out what it
    renders: a lane moved 148px inside its frame mid-test.

    The lane draws this itself and stays WA_OpaquePaintEvent. Left see-through
    it made Qt repaint everything under it each frame -- the backdrop, the
    page, the scroll area, the view frames, ten layers of a full window's
    pixels for a lane that changed -- 1.9ms of every 1080p frame inside
    processEvents, and four times the pixels at 4K (2026-11-04). taiko-rs
    has no such cost because it draws into one surface; this is that, for
    the one region a lane covers. None off a page with no backdrop.

    Rebuilt when the lane moves against the backdrop (a scroll, a layout),
    resizes, or View opacity, the picture or the theme changes -- the last
    two re-bake the backdrop, which gives its pixmap a new cacheKey.
    """
    chain, ancestor = [], widget.parentWidget()
    while ancestor is not None and not hasattr(ancestor, "baked"):
        chain.append(ancestor)
        ancestor = ancestor.parentWidget()
    if ancestor is None or widget.width() <= 0 or widget.height() <= 0:
        return None
    baked = ancestor.baked()
    origin = widget.mapTo(ancestor, QPoint(0, 0))
    key = (widget.width(), widget.height(), origin.x(), origin.y(), _view_ground_alpha, baked.cacheKey(), colour)
    cached = getattr(widget, "_view_underlay", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    layer = QPixmap(widget.size())
    painter = QPainter(layer)
    painter.drawPixmap(0, 0, baked, origin.x(), origin.y(), widget.width(), widget.height())
    for container in reversed(chain):
        ground = getattr(container, "view_frame_ground", None)
        if ground is not None:
            painter.fillRect(layer.rect(), ground())
    # Baked in: blended once here, a frame is one opaque blit, which is
    # what the 100% path costs.
    painter.fillRect(layer.rect(), view_ground(colour))
    painter.end()
    widget._view_underlay = (key, layer)
    return layer


def paint_view_ground(widget, painter: QPainter, colour: str) -> None:
    """Fill a lane's whole rect with its ground at View opacity, over the
    backdrop it lets through (`view_underlay`), so the lane can stay opaque
    to Qt at any opacity."""
    # Re-asserted, not set once: the view frame's sheet gives its children a
    # translucent `background`, and Qt's stylesheet polish clears
    # WA_OpaquePaintEvent on any widget it styles that way -- measured, a
    # lane was see-through again from the moment its frame was built, and
    # every frame repainted the whole page under it. A polish costs at most
    # the one frame before this paint.
    underlay = None if view_ground_is_opaque() else view_underlay(widget, colour)
    was_opaque = widget.testAttribute(Qt.WA_OpaquePaintEvent)
    # Off a backdrop (the Fancy page's docks) the lane is see-through to Qt
    # as it always was; that takes effect from the next frame, so this one,
    # whose parents were not repainted, is filled opaque rather than smeared.
    wants_opaque = view_ground_is_opaque() or underlay is not None
    if was_opaque != wants_opaque:
        widget.setAttribute(Qt.WA_OpaquePaintEvent, wants_opaque)
    if underlay is not None:
        painter.drawPixmap(0, 0, underlay)
        return
    if was_opaque and not wants_opaque:
        painter.fillRect(widget.rect(), theme.screen(colour))
    painter.fillRect(widget.rect(), view_ground(colour))


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


class VsyncPacer:
    """taiko-rs's `VsyncPacer` (clock.rs, 2026-11-04): each frame's time on
    the refresh grid rather than the moment it was woken for.

    A frame woken a little early or late around a vblank is still *shown* one
    refresh after the last, so content drawn for the wake time moves by
    uneven steps on even refreshes. The refresh it goes by is the one the
    wakeups show, not the one Qt reports, and it glides to the fraction of a
    millisecond (99.93Hz is not 100). All times are in ms.
    """

    PACE_STEPS = 24
    PACE_STEPS_KEPT = 120

    def __init__(self) -> None:
        from collections import deque
        self.period = 1000.0 / 60.0
        self.told_hz = 0.0
        self.last: tuple[float, float] | None = None  # (frame time, when it was woken)
        self.steps = deque(maxlen=self.PACE_STEPS_KEPT)

    def frame_time(self, acquired: float, refresh_hz: float) -> float:
        if refresh_hz != self.told_hz:
            self.told_hz = refresh_hz
            self.period = 1000.0 / min(max(refresh_hz, 24.0), 1000.0)
            self.last = None
            self.steps.clear()
        if self.last is None:
            self.last = (acquired, acquired)
            return acquired
        previous, previous_acquired = self.last
        step = acquired - previous_acquired
        if 0.5 <= step < 250.0:
            self.steps.append(step)
        seen = self.seen_refresh()
        if seen is not None:
            if abs(seen - self.period) > 0.15 * self.period:
                # The wakeups come at another refresh than the one told.
                self.period = seen
                self.last = (acquired, acquired)
                return acquired
            self.period += (seen - self.period) * 0.05
        period = self.period
        time = previous + period
        error = acquired - time
        if error > 0.5 * period:
            # Came late: the frame before stayed up for whole refreshes more.
            missed = round(error / period)
            if missed > 8:
                # A stall, or the window was hidden: pick up from now.
                self.last = (acquired, acquired)
                return acquired
            time += missed * period
            error -= missed * period
        if abs(error) < 0.5 * period:
            # Near the grid: drift toward the real clock slowly enough that
            # its jitter does not show.
            time += 0.05 * error
        elif error < -2.0 * period:
            time = acquired
        self.last = (time, acquired)
        return time

    def seen_refresh(self) -> float | None:
        """The mean of the wakeup steps near their median: the median alone
        lands on one step's jitter, the mean of all of them counts slow frames."""
        if len(self.steps) < self.PACE_STEPS:
            return None
        ordered = sorted(self.steps)
        median = ordered[len(ordered) // 2]
        near = [step for step in ordered if abs(step - median) < 0.5 * median]
        return sum(near) / len(near)


class VBlankWaiter:
    """Calls `on_vblank()` from a thread of its own once per refresh of a
    screen, so frames can be rendered on vsync.

    Qt's widgets have no vsync: the render loop was a 4ms timer, so frames
    could only start on a 4ms grid, which beats against every refresh that is
    not a multiple of it -- 8 and 12ms frames on a 100Hz screen, 8, 8, 8, 4 on
    144Hz -- and DWM showed whichever was newest at each vblank. That is the
    stutter a fast machine had as much as a slow one. taiko-rs renders under
    wgpu's Fifo present and never had it.

    `D3DKMTWaitForVerticalBlankEvent` on the screen's own adapter, as Chromium
    waits; `DwmFlush` if that cannot be opened. `on_vblank` must be cheap and
    thread-safe: it is meant to post to the GUI thread, and the caller drops a
    tick the GUI thread has not consumed yet rather than queueing a burst.
    """

    def __init__(self, on_vblank) -> None:
        import threading
        self._on_vblank = on_vblank
        self._lock = threading.Lock()
        self._adapter = None  # (hAdapter, VidPnSourceId)
        self._running = False
        self._thread = None
        self.method = None

    @staticmethod
    def available() -> bool:
        import sys
        from PySide6.QtGui import QGuiApplication
        return sys.platform == "win32" and QGuiApplication.platformName() == "windows"

    def start(self, screen) -> bool:
        """Begin waiting on `screen`'s vblanks; False if neither wait works here."""
        import threading
        if not self.available():
            return False
        self.retarget(screen)
        if self.method is None:
            return False
        self._running = True
        self._thread = threading.Thread(target=self._run, name="vblank", daemon=True)
        self._thread.start()
        return True

    def retarget(self, screen) -> None:
        """Wait on `screen` from now on: a 60Hz panel beside a 144Hz monitor."""
        import ctypes
        from ctypes import wintypes
        with self._lock:
            self._close_adapter()
            name = self._device_name(screen)
            try:
                gdi32 = ctypes.WinDLL("gdi32")
                gdi32.CreateDCW.restype = wintypes.HDC
                gdi32.CreateDCW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p)
                hdc = gdi32.CreateDCW(None, name, None, None) if name else None
                if hdc:
                    request = _OpenAdapterFromHdc(hDc=hdc)
                    status = gdi32.D3DKMTOpenAdapterFromHdc(ctypes.byref(request))
                    gdi32.DeleteDC(wintypes.HDC(hdc))
                    if status == 0:
                        self._adapter = (request.hAdapter, request.VidPnSourceId)
                        self.method = "D3DKMT"
                        return
            except (OSError, AttributeError):
                pass
            try:
                ctypes.WinDLL("dwmapi").DwmFlush
                self.method = "DwmFlush"
            except (OSError, AttributeError):
                self.method = None

    @staticmethod
    def _device_name(screen) -> str:
        """`\\\\.\\DISPLAYn` for `screen`. QScreen.name() is the monitor's
        model ("27G2G4") on Windows, which CreateDC cannot open, so the
        monitor is found from the screen's top-left -- Qt keeps that corner
        native under display scaling, where the logical size is not."""
        import ctypes
        from ctypes import wintypes
        if screen is None:
            return ""

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]
        user32 = ctypes.WinDLL("user32")
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        user32.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
        corner = screen.geometry().topLeft()
        monitor = user32.MonitorFromPoint(wintypes.POINT(corner.x() + 1, corner.y() + 1), 2)  # NEAREST
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        if not monitor or not user32.GetMonitorInfoW(wintypes.HMONITOR(monitor), ctypes.byref(info)):
            return ""
        return info.szDevice

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            # A wait never outlasts a refresh, so this is a frame at most.
            self._thread.join(0.5)
            self._thread = None
        with self._lock:
            self._close_adapter()

    def _close_adapter(self) -> None:
        import ctypes
        if self._adapter is not None:
            close = _CloseAdapter(hAdapter=self._adapter[0])
            ctypes.WinDLL("gdi32").D3DKMTCloseAdapter(ctypes.byref(close))
            self._adapter = None

    def _run(self) -> None:
        import ctypes
        import time
        gdi32 = ctypes.WinDLL("gdi32")
        dwm = ctypes.WinDLL("dwmapi")
        while self._running:
            with self._lock:
                adapter = self._adapter
            # Both calls release the GIL while they block.
            if adapter is not None:
                wait = _WaitForVerticalBlank(hAdapter=adapter[0], hDevice=0, VidPnSourceId=adapter[1])
                status = gdi32.D3DKMTWaitForVerticalBlankEvent(ctypes.byref(wait))
            else:
                status = dwm.DwmFlush()
            if status != 0:
                # A display mode change or a locked session: no vblanks to
                # wait for, and a failing wait returns at once. Don't spin.
                time.sleep(0.005)
            if self._running:
                self._on_vblank()


try:
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    class _OpenAdapterFromHdc(_ctypes.Structure):
        _fields_ = [("hDc", _wintypes.HDC), ("hAdapter", _ctypes.c_uint),
                    ("AdapterLuidLow", _ctypes.c_ulong), ("AdapterLuidHigh", _ctypes.c_long),
                    ("VidPnSourceId", _ctypes.c_uint)]

    class _WaitForVerticalBlank(_ctypes.Structure):
        _fields_ = [("hAdapter", _ctypes.c_uint), ("hDevice", _ctypes.c_uint), ("VidPnSourceId", _ctypes.c_uint)]

    class _CloseAdapter(_ctypes.Structure):
        _fields_ = [("hAdapter", _ctypes.c_uint)]
except (ImportError, ValueError):  # pragma: no cover - ctypes is stdlib everywhere we run
    pass


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
