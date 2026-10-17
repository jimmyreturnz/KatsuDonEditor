"""Song select: the library list, the banner with its preview, Continue cards, and the page controller."""
from __future__ import annotations
import math
import os
import random
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
import struct
import json
from time import perf_counter, time as wall_clock
from PySide6.QtCore import (
    QCoreApplication,
    QElapsedTimer,
    QEvent,
    QObject,
    QVariantAnimation,
    QPointF,
    QRect,
    QRectF,
    QSize,
    QStandardPaths,
    Qt,
    QEasingCurve,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QImage,
    QImageReader,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QMenu,
    QSizePolicy,
)
from library import osu_db
from ui.motion import (
    MorphRect,
    PlayPauseGlyph,
    RowEntrance,
    ScanShimmer,
    SelectionGlide,
    out_cubic,
    paint_living_rim,
    tick_while_shown,
)
from ui.smooth_scroll import SmoothScroller, reduced_motion
from ui.config_sheet import segmented
from library.skin import UI_SOUNDS
from app.i18n import tr
from ui import theme, windose
from osu_io.parser import parse_osu
from library.song_library import (
    group_by_song,
    heard_tempo,
    load_cache,
    matches_search,
    most_common_bpm,
    read_header,
    save_cache,
    scan,
    songs_from_cache,
)
from ui.common import (
    ACCENT_PINK,
    SONG_SELECT_PREVIEW_DEFAULT_PERCENT,
    display_frame_interval_ns,
    equalize_button_widths,
    next_frame_due,
    osu_screen_height_for,
    resource_roots,
    tabular,
)
from ui.gameplay_view import GameplayViewerView


SCAN_SLICE_SECONDS = 0.008


# The song select's Continue row: cards shown, paths remembered. Four cards of
# one size (owner, 2026-10-08), filled from a longer memory: a chart deleted or
# moved since is skipped, and with only four kept the row came up a card short
# until four more were opened (owner, 2026-10-09: "up to 4 continue cards").
RECENT_CHARTS_SHOWN = 4


RECENT_CHARTS_KEPT = 16


SONG_SELECT_PREVIEW_MIN_PERCENT = 50


SONG_SELECT_PREVIEW_MAX_PERCENT = 75


# Its hit target, in playfield units from the left: osu!'s is 256, which
# leaves the input drum's width empty in a view that draws no drum. 120 still
# clears a finisher's radius (73) with room for the target's rim.
SONG_SELECT_HIT_X_UNITS = 120.0


# osu!'s star-rating colours (OsuColour.STAR_DIFFICULTY_SPECTRUM, as osu!-web
# draws them): the list dots and the difficulty pills both read these, so a
# set's spread shows before it is opened.
STAR_SPECTRUM = (
    (0.1, "#4290fb"), (1.25, "#4fc0ff"), (2.0, "#4fffd5"), (2.5, "#7cff4f"),
    (3.3, "#f6f05c"), (4.2, "#ff8068"), (4.9, "#ff4e6f"), (5.8, "#c645b8"),
    (6.7, "#6563de"), (7.7, "#18158e"), (9.0, "#000000"),
)


STAR_TEXT_SPECTRUM = (
    (9.0, "#f6f05c"), (9.9, "#ff8068"), (10.6, "#ff4e6f"), (11.5, "#c645b8"), (12.4, "#6563de"),
)


STAR_UNRATED = "#aaaaaa"


def _sample_spectrum(stops, value: float) -> QColor:
    """osu!-web's d3 scale: clamped, interpolated at gamma 2.2."""
    if value <= stops[0][0]:
        return QColor(stops[0][1])
    for (low, low_hex), (high, high_hex) in zip(stops, stops[1:]):
        if value <= high:
            t = (value - low) / (high - low)
            a, b = QColor(low_hex), QColor(high_hex)
            mix = lambda x, y: round(255 * ((x / 255) ** 2.2 + t * ((y / 255) ** 2.2 - (x / 255) ** 2.2)) ** (1 / 2.2))
            return QColor(mix(a.red(), b.red()), mix(a.green(), b.green()), mix(a.blue(), b.blue()))
    return QColor(stops[-1][1])


def star_colour(stars: float | None) -> QColor:
    """The pill and dot colour. osu! rounds to 2dp away from zero first, so
    1.245 samples as 1.25, and anything under 0.1 is its grey."""
    if stars is None:
        return QColor(STAR_UNRATED)
    stars = float(Decimal(str(stars)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return QColor(STAR_UNRATED) if stars < 0.1 else _sample_spectrum(STAR_SPECTRUM, stars)


def star_text_colour(stars: float | None) -> QColor:
    """Dark below 6.5, gold below 9, then its own spectrum -- osu!-web's pill."""
    if stars is None or stars < 6.5:
        return QColor(0, 0, 0, 191)
    if stars < 9.0:
        return QColor("#ffd966")
    return _sample_spectrum(STAR_TEXT_SPECTRUM, stars)


def format_length(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_bpm(low: float, high: float, common: float | None = None) -> str:
    # A gimmick's beat length can be subnormal (EGTS 2022's
    # `1.14514535393084E-319`), and 60000 over that is inf: round() raised,
    # and the song's difficulty list stopped filling at its first chart.
    def text(bpm: float) -> str:
        return str(round(bpm)) if math.isfinite(bpm) else "∞"

    if not high:
        return "–"
    if text(low) == text(high):
        return text(high)
    # A range says which tempo it mostly is, as lazer's "a-b (mostly c)" and
    # the density view do: aleph-0 is "125–400 (250)" (owner, 2026-10-09).
    if common:
        return f"{text(low)}–{text(high)} ({text(common)})"
    return f"{text(low)}–{text(high)}"


def decode_background_image(path: Path, max_width: int) -> QImage | None:
    """A beatmap background, decoded no wider than `max_width`: a 4K
    background at full size costs a visible pause on every selection.
    A QImage, so it can be decoded off the GUI thread (a QPixmap cannot)."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > max_width:
        reader.setScaledSize(size.scaled(max_width, max_width, Qt.KeepAspectRatio))
    image = reader.read()
    return image if not image.isNull() else None


def decode_background(path: Path, max_width: int) -> QPixmap | None:
    image = decode_background_image(path, max_width)
    return QPixmap.fromImage(image) if image is not None else None


class BackgroundJobs(QObject):
    """Work song select must not do on the GUI thread, handed back to it.

    Measured (tools/measure_song_switch.py, 4101 songs): picking a song held
    the event loop for 99ms median and 208ms at worst -- 62ms of it hashing
    every difficulty for osu!.db's staleness check, 33ms decoding the
    background. That is the stutter before the chart preview loads. Both
    release the GIL (file reads, hashlib over large buffers, Qt's image
    decoders), so a thread really does take them off the frame.

    `deliver` runs on the GUI thread: the done signal is emitted from the
    worker, and Qt queues a cross-thread emit to the receiver's thread.
    """

    _done = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="song-select")
        self._pending: set = set()
        self._done.connect(self._deliver)

    def submit(self, work, deliver) -> None:
        try:
            future = self._pool.submit(work)
        except RuntimeError:
            return  # shut down with the window: a late selection has nobody to show it to
        self._pending.add(future)
        future.add_done_callback(lambda done: self._emit(done, deliver))

    def _emit(self, future, deliver) -> None:
        try:
            self._done.emit((future, deliver))
        except RuntimeError:
            pass  # the window, and this object with it, is already gone

    def _deliver(self, payload) -> None:
        future, deliver = payload
        self._pending.discard(future)
        if future.cancelled() or future.exception() is not None:
            return  # an unreadable file keeps what was shown, as before
        deliver(future.result())

    def drain(self) -> None:
        """Wait for everything submitted and deliver it (tests)."""
        while self._pending:
            futures_wait(list(self._pending))
            QCoreApplication.processEvents()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# The song and difficulty rows carry what they paint under this role; their
# display text stays the plain label, which is what search, tests and screen
# readers see.
ROW_ROLE = Qt.UserRole + 1


ROW_INK, ROW_INK_2, ROW_INK_3 = "#e8edf3", "#aeb8c5", "#7d8794"


ROW_HOVER, ROW_PINK_ON = "#2a3341", "#ff66aa"


# The detail pane keeps room for DIFFICULTY_RESERVED_SLOTS difficulties when
# it can (owner, 2026-10-08), the banner having the rest up to 16:9; a window
# too short for both gives the list up to its floor of DIFFICULTY_SLOTS.
DIFFICULTY_SLOTS, DIFFICULTY_ROW_HEIGHT = 2, 48


DIFFICULTY_RESERVED_SLOTS = 5


class ResizeHook(QObject):
    """Call `callback` whenever `widget` is resized."""

    def __init__(self, widget: QWidget, callback) -> None:
        super().__init__(widget)
        self.callback = callback
        widget.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Resize:
            self.callback()
        return False


# The song preview: how long the selection has to rest before it starts (so
# holding an arrow down does not open every song on the way), and its fade in.
PREVIEW_SETTLE_MS = 250


PREVIEW_FADE_MS = 400


# The chart preview's notes fading out of the song left and into the next
# (owner, 2026-10-09). Out is shorter than the settle, so it is gone before
# the next chart is read; in is the mockup's entering 250ms.
CHART_FADE_OUT_MS = 150


CHART_FADE_IN_MS = 250


# The song list's difficulty dots, one per chart in its star colour.
SONG_DOT_RADIUS, SONG_DOT_PITCH = 5.0, 14


# A pack lists dozens of charts, and a dot each pushed the title off its own
# row. Past this many, or past the share of the row they may take, the rest
# are a "+n" (owner, 2026-10-08).
SONG_DOTS_MAX = 20


SONG_DOTS_SHARE = 0.4


def song_dots_shown(count: int, row_width: float) -> tuple[int, int]:
    """(dots drawn, how many the "+n" stands for) for a row this wide."""
    room = min(SONG_DOTS_MAX, int(row_width * SONG_DOTS_SHARE // SONG_DOT_PITCH))
    if count <= room:
        return count, 0
    # One slot (and a bit) gives way to the "+n" itself.
    shown = max(0, room - 2)
    return shown, count - shown


def paint_selected_row(painter: QPainter, rect: QRectF, active: bool) -> None:
    tint = QLinearGradient(rect.topLeft(), rect.topRight())
    tint.setColorAt(0.0, theme.rgba(255, 102, 170, 46))
    tint.setColorAt(0.7, theme.rgba(255, 102, 170, 10))
    painter.setPen(Qt.NoPen)
    painter.setBrush(tint)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
    paint_living_rim(painter, rect, 4, 1.5, active)


def glide_selection(rows: QListWidget, inset: tuple[int, int, int, int]) -> None:
    """Give `rows` a selection that glides between rows (SelectionGlide);
    `inset` is how far inside the item rect its delegate draws one."""
    rows.selection_glide = SelectionGlide(
        rows, lambda painter, rect: paint_selected_row(painter, rect, rows.hasFocus()), inset)


def _enter_row(painter: QPainter, option, entrance) -> None:
    """Draw the row lifted and faded by its RowEntrance progress."""
    if entrance is None:
        return
    progress = entrance.progress(option.index.row())
    if progress < 1.0:
        painter.setOpacity(progress)
        painter.translate(0, RowEntrance.RISE_PX * (1.0 - progress))


def _paint_selection(painter: QPainter, rect: QRectF, option) -> None:
    """The selected tint fades left to right, as in the mockup, inside a rim
    with two small lights running round it (motion.paint_living_rim). In the
    list that has the keys; the other list's selection keeps a dimmer, still
    rim, so it is clear which one Up/Down will move.

    While the list's SelectionGlide is flying to this row, the flight draws
    the selection and the row draws only itself."""
    widget = option.widget
    glide = getattr(widget, "selection_glide", None)
    if option.state & QStyle.State_Selected:
        if glide is None or not glide.covers(option.index.row()):
            paint_selected_row(painter, rect, widget is not None and widget.hasFocus())
    elif option.state & QStyle.State_MouseOver:
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.color(ROW_HOVER))
        painter.drawRoundedRect(rect, 4, 4)


class SongRowDelegate(QStyledItemDelegate):
    """A song: bold title over "artist · mapped by X", a dot per difficulty in
    its star colour. A group header: small caps in the accent pink."""

    # Set by the page: rows rising in after a search (RowEntrance).
    entrance = None

    def sizeHint(self, option, index):
        header = index.data(Qt.UserRole) is None
        return QSize(option.rect.width(), 28 if header else 46)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(option.rect)
        row = index.data(ROW_ROLE)
        font = QFont(option.font)
        _enter_row(painter, option, self.entrance)
        if row is None:  # a group header
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() * 0.8)
            font.setCapitalization(QFont.AllUppercase)
            font.setLetterSpacing(QFont.PercentageSpacing, 106)
            painter.setFont(font)
            painter.setPen(QColor(index.data(Qt.ForegroundRole).color()))
            painter.drawText(rect.adjusted(8, 0, -8, -2), Qt.AlignLeft | Qt.AlignBottom, index.data())
            painter.setPen(theme.color("#303947"))
            painter.drawLine(rect.bottomLeft() + QPointF(4, 0), rect.bottomRight() - QPointF(4, 0))
            painter.restore()
            return
        _paint_selection(painter, rect.adjusted(2, 1, -2, -1), option)
        stars = row["stars"]
        small = QFont(option.font)
        small.setPointSizeF(small.pointSizeF() * 0.86)
        shown, more = song_dots_shown(len(stars), rect.width())
        more_text = f"+{more}" if more else ""
        more_width = QFontMetrics(small).horizontalAdvance(more_text) + 4 if more else 0
        dots_width = shown * SONG_DOT_PITCH + more_width
        text = rect.adjusted(13, 5, -(dots_width + 18), -5)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(theme.color(ROW_INK))
        metrics = QFontMetrics(font)
        painter.drawText(text, Qt.AlignLeft | Qt.AlignTop, metrics.elidedText(row["title"], Qt.ElideRight, int(text.width())))
        painter.setFont(small)
        painter.setPen(theme.color(ROW_INK_2))
        painter.drawText(text, Qt.AlignLeft | Qt.AlignBottom,
                         QFontMetrics(small).elidedText(row["subtitle"], Qt.ElideRight, int(text.width())))
        x = rect.right() - dots_width - 8
        y = rect.center().y()
        if more:
            painter.drawText(QRectF(rect.right() - 8 - more_width, rect.top(), more_width, rect.height()),
                             Qt.AlignRight | Qt.AlignVCenter, more_text)
        for value in stars[:shown]:
            if value is None:
                painter.setPen(QPen(QColor(STAR_UNRATED), 1.5))
                painter.setBrush(Qt.NoBrush)
            else:
                # A faint rim: osu!'s colours run to black past 9 stars, which
                # on this navy is no dot at all.
                painter.setPen(QPen(QColor(255, 255, 255, 46), 1))
                painter.setBrush(star_colour(value))
            painter.drawEllipse(QPointF(x + SONG_DOT_PITCH / 2, y), SONG_DOT_RADIUS, SONG_DOT_RADIUS)
            x += SONG_DOT_PITCH
        painter.restore()


class DifficultyRowDelegate(QStyledItemDelegate):
    """A difficulty: star pill and name, "mapped by X" under it, notes,
    length and BPM on the right in tabular figures."""

    # Set by the page: a new song's rows arriving one by one, their note
    # counts rolling up as they come (RowEntrance).
    entrance = None

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), DIFFICULTY_ROW_HEIGHT)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(option.rect).adjusted(2, 2, -2, -2)
        row = index.data(ROW_ROLE) or {}
        _enter_row(painter, option, self.entrance)
        _paint_selection(painter, rect, option)
        inner = rect.adjusted(10, 6, -10, -6)
        stats_font = tabular(option.font)
        stats_font.setPointSizeF(stats_font.pointSizeF() * 0.86)
        stats_font.setWeight(QFont.DemiBold)
        stats_metrics = QFontMetrics(stats_font)
        stats = final_stats = row.get("stats", ("", ""))
        notes = row.get("notes")
        if notes and self.entrance is not None:
            shown = round(notes * out_cubic(self.entrance.progress(index.row()) * 1.5))
            if shown != notes:
                # Measured against the final count below, so the column does
                # not shift as the digits roll.
                stats = (tr("MainWindow", "{n} notes").format(n=shown), stats[1])
        stats_width = max(stats_metrics.horizontalAdvance(line) for line in final_stats) if any(final_stats) else 0
        painter.setFont(stats_font)
        painter.setPen(theme.color(ROW_INK_2))
        painter.drawText(inner, Qt.AlignRight | Qt.AlignTop, stats[0])
        painter.drawText(inner, Qt.AlignRight | Qt.AlignBottom, stats[1])

        stars = row.get("stars")
        pill_font = tabular(option.font)
        pill_font.setBold(True)
        pill_font.setPointSizeF(pill_font.pointSizeF() * 0.82)
        pill_text = f"★ {stars:.2f}" if stars is not None else "★ –"
        pill_metrics = QFontMetrics(pill_font)
        # Centred: the row's second line was the mapper, which the song row
        # above already names (owner, 2026-10-12).
        pill_height = pill_metrics.height() + 4
        pill = QRectF(inner.left(), inner.center().y() - pill_height / 2,
                      max(52, pill_metrics.horizontalAdvance(pill_text) + 16), pill_height)
        painter.setOpacity(0.5 if row.get("stale") else 1.0)
        painter.setPen(QPen(QColor(255, 255, 255, 102), 1, Qt.DashLine) if row.get("stale") else Qt.NoPen)
        painter.setBrush(star_colour(stars))
        painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
        painter.setFont(pill_font)
        painter.setPen(star_text_colour(stars))
        painter.drawText(pill, Qt.AlignCenter, pill_text)
        painter.setOpacity(1.0)

        text_left = pill.right() + 8
        width = int(inner.right() - stats_width - 12 - text_left)
        name_font = QFont(option.font)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(theme.color(ROW_INK))
        painter.drawText(QRectF(text_left, pill.top(), max(0, width), pill.height()), Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(name_font).elidedText(index.data() or "", Qt.ElideRight, max(0, width)))
        painter.restore()


class SongBanner(QWidget):
    """The selected song's own background, heading the detail pane the way
    osu!'s song select does, with the title over a wash at its foot.

    Along the bottom runs the seek band (mockup, 2026-10-09): the elapsed
    time, a track that is the whole song, and the total. A playhead circle
    in the focus colour rides it, growing and glowing on each beat of the
    selected difficulty. Kiai sections
    and the preview point are marked on the track, and inside a kiai the art
    itself lifts on the beat. The beats are the difficulty's own red lines,
    read against `clock` (song milliseconds), so a BPM change or a second
    audio file in the folder keeps time. A new song crossfades its art in
    and its title rises in.

    Under reduced motion the playhead still moves -- it is a position, not
    an effect -- and everything that pulses holds still.
    """

    BEAT_REST = 0.15
    KIAI_LIFT = 0.06
    MIN_HEIGHT = 150
    # The strip the band takes at the foot, which is also the height that
    # takes the pointer: a track you have to land a 6px line on is not one
    # anybody uses.
    BAND_HEIGHT = 30
    TRACK_REST, TRACK_HOVER = 6.0, 10.0
    HEAD_RADIUS = 8.0
    # The playhead flies to a new song's preview point rather than jumping:
    # back to the start of the track first, then out to the preview point
    # (owner, 2026-10-09), REWIND_SHARE of the flight on the way back.
    GLIDE_MS = 320
    REWIND_GLIDE_MS = 560
    REWIND_SHARE = 0.4
    TEXT_RISE_PX = 8.0
    seek_fraction = Signal(float)
    seek_step = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._seeking = False
        # The height the difficulty slots leave; see DIFFICULTY_SLOTS.
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.title = ""
        self.subtitle = ""
        self._art: QPixmap | None = None
        self._previous: QPixmap | None = None
        self._scaled: dict[int, QPixmap] = {}
        self._fade = 1.0
        self._beat_phase = 1.0
        self.fade = QVariantAnimation(self)
        self.fade.setStartValue(0.0)
        self.fade.setEndValue(1.0)
        self.fade.setDuration(250)
        self.fade.valueChanged.connect(self._set_fade)
        self._tempo: list[tuple[float, float]] = []
        self._tempo_times: list[float] = []
        self._clock = None
        self._playing = lambda: True
        self._now = 0.0
        self._duration_ms = 0.0
        self._preview_ms = -1.0
        self._kiai: list[tuple[float, float]] = []
        self._hover_x: float | None = None
        self._grow = 0.0
        self.grow = QVariantAnimation(self)
        self.grow.setDuration(160)
        self.grow.setEasingCurve(QEasingCurve.OutCubic)
        self.grow.valueChanged.connect(self._set_grow)
        self._glide_from = 0.0
        self._glide_t = 1.0
        self._glide_rewinds = False
        # Linear: each leg of the flight eases itself (_shown_fraction).
        self.glide_animation = QVariantAnimation(self)
        self.glide_animation.setDuration(self.GLIDE_MS)
        self.glide_animation.setStartValue(0.0)
        self.glide_animation.setEndValue(1.0)
        self.glide_animation.valueChanged.connect(self._set_glide)
        self._text_in = 1.0
        self.text_animation = QVariantAnimation(self)
        self.text_animation.setDuration(300)
        self.text_animation.setStartValue(0.0)
        self.text_animation.setEndValue(1.0)
        self.text_animation.setEasingCurve(QEasingCurve.OutCubic)
        self.text_animation.valueChanged.connect(self._set_text_in)
        self.beat = QTimer(self)
        self.beat.setInterval(16)
        self.beat.timeout.connect(self._tick_beat)

    def _set_fade(self, value) -> None:
        self._fade = float(value)
        self.update()

    def _set_grow(self, value) -> None:
        self._grow = float(value)
        self.update()

    def _set_glide(self, value) -> None:
        self._glide_t = float(value)
        self.update()

    def _set_text_in(self, value) -> None:
        self._text_in = float(value)
        self.update()

    def beat_phase_at(self, time_ms: float) -> float:
        """0 on a beat of the red line in force at `time_ms`, rising to 1."""
        if not self._tempo:
            return 1.0
        index = max(0, bisect_right(self._tempo_times, time_ms) - 1)
        start, beat = self._tempo[index]
        # Past 300 BPM a flash per beat is a flicker, not a pulse.
        while beat < 200:
            beat *= 2
        return ((time_ms - start) % beat) / beat

    def _tick_beat(self) -> None:
        if self._clock is None:
            return
        self._now = self._clock()
        self._beat_phase = self.beat_phase_at(self._now)
        self.update()

    def set_tempo(self, tempo: list[tuple[float, float]], clock, playing=None) -> None:
        """Keep time with `tempo`'s red lines, read against `clock()`.
        `playing()` False holds the pulse still: a paused song has no beat."""
        self._tempo = tempo
        self._tempo_times = [time for time, _beat in tempo]
        self._clock = clock
        if playing is not None:
            self._playing = playing
        self._beat_phase = 1.0
        self._sync_beat()

    def set_duration(self, duration_ms: float) -> None:
        """The song's length, which the track's width stands for."""
        duration_ms = max(0.0, float(duration_ms))
        # A flight already under way reads the new length live, so it is left
        # to finish: restarted, a new song's rewind would be cut short.
        if (self._duration_ms > 0 and abs(duration_ms - self._duration_ms) > self._duration_ms * 0.005
                and self.glide_animation.state() != QVariantAnimation.Running):
            self.glide()
        self._duration_ms = duration_ms
        self.update()

    def set_preview(self, preview_ms: float) -> None:
        self._preview_ms = float(preview_ms)
        self.update()

    def set_kiai(self, spans) -> None:
        self._kiai = list(spans)
        self.update()

    def glide(self, rewind: bool = False) -> None:
        """Fly the playhead from where it is drawn to wherever the clock
        says next, instead of jumping there: a new length, or with `rewind`
        a new song -- back to the start of the track, then out to it."""
        if reduced_motion() or self._seeking:
            return
        self._glide_from = self._shown_fraction()
        self._glide_rewinds = rewind
        self.glide_animation.stop()
        self.glide_animation.setDuration(self.REWIND_GLIDE_MS if rewind else self.GLIDE_MS)
        self._glide_t = 0.0
        self.glide_animation.start()

    def _fraction(self) -> float:
        if self._duration_ms <= 0:
            return 0.0
        return min(1.0, max(0.0, self._now / self._duration_ms))

    def _shown_fraction(self) -> float:
        target = self._fraction()
        if self.glide_animation.state() != QVariantAnimation.Running:
            return target
        t = self._glide_t
        if not self._glide_rewinds:
            return self._glide_from + (target - self._glide_from) * out_cubic(t)
        if t < self.REWIND_SHARE:
            # In and out on the way back, so it settles at the start before
            # setting off again rather than bouncing off it.
            back = t / self.REWIND_SHARE
            eased = 4 * back ** 3 if back < 0.5 else 1 - (-2 * back + 2) ** 3 / 2
            return self._glide_from * (1.0 - eased)
        return target * out_cubic((t - self.REWIND_SHARE) / (1.0 - self.REWIND_SHARE))

    def _sync_beat(self) -> None:
        running = self._clock is not None and self.isVisible()
        if running and not self.beat.isActive():
            self.beat.start()
        elif not running:
            self.beat.stop()
            self._beat_phase = 1.0
            self.update()

    def set_text(self, title: str, subtitle: str) -> None:
        if (title, subtitle) == (self.title, self.subtitle):
            return
        self.title, self.subtitle = title, subtitle
        self.text_animation.stop()
        if reduced_motion():
            self._text_in = 1.0
        else:
            self._text_in = 0.0
            self.text_animation.start()
        self.update()

    def set_art(self, art: QPixmap | None) -> None:
        self._previous, self._art = self._art, art
        self._scaled = {}
        self.fade.stop()
        if reduced_motion():
            self._fade = 1.0
        else:
            self._fade = 0.0
            self.fade.start()
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_beat()

    def hideEvent(self, event) -> None:
        self.beat.stop()  # nothing to keep time for on a page nobody is looking at
        super().hideEvent(event)

    # -- the seek band ------------------------------------------------------

    def _band_font(self) -> QFont:
        font = tabular(self.font())
        font.setPointSizeF(font.pointSizeF() * 0.82)
        font.setWeight(QFont.DemiBold)
        return font

    def _label_width(self) -> float:
        return QFontMetrics(self._band_font()).horizontalAdvance("88:88") + 10

    def track_rect(self) -> QRectF:
        """The track, at the height it is drawn right now."""
        label = self._label_width()
        height = self.TRACK_REST + (self.TRACK_HOVER - self.TRACK_REST) * self._grow
        centre = self.height() - self.BAND_HEIGHT / 2
        return QRectF(12 + label, centre - height / 2, max(1.0, self.width() - 24 - 2 * label), height)

    def _in_seek_band(self, position) -> bool:
        return position.y() >= self.height() - self.BAND_HEIGHT

    def _seek_to(self, position) -> None:
        # The track is read here, per event, so a pane the splitter has
        # widened or narrowed maps the same 0-100% across whatever it is now.
        track = self.track_rect()
        self.seek_fraction.emit(min(1.0, max(0.0, (position.x() - track.left()) / track.width())))

    def _hover(self, inside: bool, x: float | None = None) -> None:
        self._hover_x = x if inside else None
        target = 1.0 if inside else 0.0
        if reduced_motion():
            self._grow = target
        elif self._grow != target and self.grow.endValue() != target:
            self.grow.stop()
            self.grow.setStartValue(self._grow)
            self.grow.setEndValue(target)
            self.grow.start()
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._in_seek_band(event.position()):
            self._seeking = True
            self.glide_animation.stop()  # dragged, the head is under the pointer
            self._seek_to(event.position())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        position = event.position()
        self._hover(self._seeking or self._in_seek_band(position), position.x())
        if self._seeking:
            self._seek_to(position)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._seeking and event.button() == Qt.LeftButton:
            self._seeking = False
            self._hover(self._in_seek_band(event.position()), event.position().x())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._seeking:
            self._hover(False)
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        if not self._in_seek_band(event.position()):
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            self.seek_step.emit(-1 if delta > 0 else 1)
        event.accept()

    def _cover(self, art: QPixmap) -> QPixmap:
        key = art.cacheKey()
        if key not in self._scaled:
            self._scaled[key] = art.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        return self._scaled[key]

    def resizeEvent(self, event) -> None:
        self._scaled = {}
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(rect, 6, 6)
        painter.setClipPath(clip)
        painter.fillRect(rect, theme.color("#222a36"))
        for art, opacity in ((self._previous, 1.0 - self._fade), (self._art, self._fade)):
            if art is not None and not art.isNull() and opacity > 0:
                cover = self._cover(art)
                painter.setOpacity(opacity)
                painter.drawPixmap(int((rect.width() - cover.width()) / 2),
                                   int((rect.height() - cover.height()) / 2), cover)
        painter.setOpacity(1.0)

        pulsing = self._playing() and not reduced_motion() and bool(self._tempo)
        phase = self._beat_phase
        beat = max(0.0, 1.0 - phase / 0.6) if pulsing else 0.0
        if beat > 0.0 and any(start <= self._now < end for start, end in self._kiai):
            # The kiai lifts the art on the beat, additively: lighting it, not
            # laying a film over it. KIAI_LIFT, not the preview's 0.15 -- over
            # a whole background that read too bright (owner, 2026-10-09).
            painter.setCompositionMode(QPainter.CompositionMode_Plus)
            painter.fillRect(rect, QColor(255, 255, 255, round(255 * self.KIAI_LIFT * beat)))
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        wash = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        wash.setColorAt(0.25, QColor(0, 0, 0, 0))
        wash.setColorAt(0.6, theme.rgba(255, 102, 170, 20))
        wash.setColorAt(1.0, QColor(13, 17, 23, 242))
        painter.fillRect(rect, wash)

        text = rect.adjusted(14, 10, -14, -(self.BAND_HEIGHT + 2))
        title_font = QFont(self.font())
        title_font.setPointSizeF(title_font.pointSizeF() * 1.55)
        title_font.setWeight(QFont.ExtraBold)
        sub_font = QFont(self.font())
        sub_font.setPointSizeF(sub_font.pointSizeF() * 0.93)
        sub_height = QFontMetrics(sub_font).height()
        painter.save()
        if self._text_in < 1.0:
            painter.setOpacity(self._text_in)
            painter.translate(0, self.TEXT_RISE_PX * (1.0 - self._text_in))
        painter.setFont(sub_font)
        # White in every theme: the wash under it is a fixed near-black, and
        # NSO's ink once drew the title navy on it (owner, 2026-10-12).
        painter.setPen(QColor("#d5dce5"))
        painter.drawText(text, Qt.AlignLeft | Qt.AlignBottom,
                         QFontMetrics(sub_font).elidedText(self.subtitle, Qt.ElideRight, int(text.width())))
        painter.setFont(title_font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(text.adjusted(0, 0, 0, -sub_height), Qt.AlignLeft | Qt.AlignBottom | Qt.TextWordWrap,
                         self.title)
        painter.restore()
        self._paint_band(painter, pulsing, phase, beat)

    def _paint_band(self, painter: QPainter, pulsing: bool, phase: float, beat: float) -> None:
        focus = theme.color(ROW_PINK_ON)
        track = self.track_rect()
        radius = track.height() / 2
        shape = QPainterPath()
        shape.addRoundedRect(track, radius, radius)
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.color("#2a3341"))
        painter.drawPath(shape)
        dim = QColor(focus)
        dim.setAlphaF(0.18)
        painter.setBrush(dim)
        painter.drawPath(shape)

        band_font = self._band_font()
        painter.setFont(band_font)
        painter.setPen(theme.color(ROW_INK_3))
        labels = QRectF(12, track.center().y() - 10, self.width() - 24, 20)
        painter.drawText(labels, Qt.AlignLeft | Qt.AlignVCenter, format_length(self._now))
        if self._duration_ms <= 0:
            return
        painter.drawText(labels, Qt.AlignRight | Qt.AlignVCenter, format_length(self._duration_ms))

        def x_of(ms: float) -> float:
            return track.left() + track.width() * min(1.0, max(0.0, ms / self._duration_ms))

        painter.save()
        painter.setClipPath(shape, Qt.IntersectClip)
        kiai = QColor(focus)
        kiai.setAlphaF(0.34)
        for start, end in self._kiai:
            if start < self._duration_ms:
                painter.fillRect(QRectF(x_of(start), track.top(), x_of(end) - x_of(start), track.height()), kiai)
        # No fill for the part already played: a gradient trailing behind the
        # head covered the kiai marks it ran over (owner, 2026-10-09). The
        # head alone says where the song is, and its pop and glow keep time.
        head_x = track.left() + track.width() * self._shown_fraction()
        painter.restore()

        centre_y = track.center().y()
        if self._preview_ms >= 0:
            mark = QColor(theme.color(ROW_INK_2))
            mark.setAlphaF(0.5)
            painter.setPen(QPen(mark, 2, Qt.SolidLine, Qt.RoundCap))
            x = x_of(self._preview_ms)
            painter.drawLine(QPointF(x, track.top() - 4), QPointF(x, track.bottom() + 4))

        if self._hover_x is not None and not self._seeking:
            hover_x = min(track.right(), max(track.left(), self._hover_x))
            ghost = QColor(theme.color(ROW_INK))
            ghost.setAlphaF(0.85 * self._grow)
            painter.setPen(Qt.NoPen)
            painter.setBrush(ghost)
            painter.drawEllipse(QPointF(hover_x, centre_y), 5, 5)
            time_text = format_length((hover_x - track.left()) / track.width() * self._duration_ms)
            metrics = QFontMetrics(band_font)
            bubble = QRectF(0, 0, metrics.horizontalAdvance(time_text) + 12, metrics.height() + 4)
            bubble.moveCenter(QPointF(hover_x, track.top() - 8 - bubble.height() / 2))
            bubble.moveLeft(min(self.width() - 4 - bubble.width(), max(4.0, bubble.left())))
            painter.setOpacity(self._grow)
            painter.setBrush(theme.color("#3a4554"))
            painter.drawRoundedRect(bubble, 4, 4)
            painter.setPen(theme.color(ROW_INK))
            painter.drawText(bubble, Qt.AlignCenter, time_text)
            painter.setOpacity(1.0)

        centre = QPointF(head_x, centre_y)
        head = self.HEAD_RADIUS
        # No ring shed on the beat: spreading over the track it hid the kiai
        # marks under it (owner, 2026-10-09). The pop and the glow keep time.
        glow_radius = head + 6 + 10 * beat
        glow = QRadialGradient(centre, glow_radius)
        halo = QColor(focus)
        halo.setAlphaF(0.35 + 0.4 * beat)
        glow.setColorAt(0.0, halo)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(centre, glow_radius, glow_radius)
        scale = 1.3 if self._seeking else 1.0 + 0.3 * beat
        painter.setPen(QPen(theme.color(ROW_INK), 2))
        painter.setBrush(focus)
        painter.drawEllipse(centre, head * scale, head * scale)


class ContinueCard(QFrame):
    """One recent chart: title and difficulty, over its own background very
    dim. "Opened N min ago" went on 2026-10-07 (owner's call): the order of
    the cards already says which was most recent."""

    # How much of the chart's art shows through. Enough to tell two cards
    # apart at a glance, not so much that it competes with the title.
    ART_OPACITY = 0.12  # 0.16 until 2026-10-08: the owner wanted it fainter

    clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("continueCard")
        self.setCursor(Qt.PointingHandCursor)
        self.path = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(0)
        # The words at the foot, as on the banner: the card is as tall as its
        # art's 16:9 now, and the art is what fills the top of it.
        layout.addStretch(1)
        self.title = QLabel()
        self.title.setStyleSheet(f"font-weight: 700; color: {ROW_INK};")
        self.version = QLabel()
        self.version.setStyleSheet(f"color: {ACCENT_PINK};")
        for label in (self.title, self.version):
            # Ignored: the card's width is a share of the row, not its text's.
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            layout.addWidget(label)
        self.art: QPixmap | None = None
        self._cover: tuple[tuple, QPixmap] | None = None
        self._texts = ("", "")
        # Hovered, the art slowly comes forward: zooms a little, brightens,
        # and the rim lights (mockup, 2026-10-09). The card stays where the
        # layout put it -- a lift would fight the layout for its position.
        self._lift = 0.0
        self.lift = QVariantAnimation(self)
        self.lift.setEasingCurve(QEasingCurve.OutCubic)
        self.lift.valueChanged.connect(self._set_lift)

    def _set_lift(self, value) -> None:
        self._lift = float(value)
        self.update()

    def _lift_to(self, target: float, duration_ms: int) -> None:
        self.lift.stop()
        if reduced_motion():
            self._set_lift(target)
            return
        self.lift.setStartValue(self._lift)
        self.lift.setEndValue(target)
        self.lift.setDuration(duration_ms)
        self.lift.start()

    def enterEvent(self, event) -> None:
        self._lift_to(1.0, 500)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._lift_to(0.0, 200)
        super().leaveEvent(event)

    def show_chart(self, path: str, title: str, version: str, art: QPixmap | None = None) -> None:
        self.path = path
        self._texts = (title, version)
        self._elide()
        self.art = art
        self.setToolTip(path)
        self.update()

    def _elide(self) -> None:
        room = max(0, self.contentsRect().width() - 20)
        for label, text in zip((self.title, self.version), self._texts):
            # The title is bold by stylesheet, which font() does not report.
            font = QFont(label.font())
            font.setBold(label is self.title)
            label.setText(QFontMetrics(font).elidedText(text, Qt.ElideRight, room))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 16:9 of its width, a background's own shape (owner, 2026-10-08),
        # and never too short for its two lines. Height follows width only,
        # so this settles in one pass.
        height = max(self.layout().minimumSize().height(), round(self.width() * 9 / 16))
        if height != self.height():
            self.setFixedHeight(height)
        self._elide()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)  # the sheet's fill and rim
        if self.art is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        # Inside the 1px rim, on the same 6px corners, cropped to fill.
        inner = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(inner, 5, 5)
        painter.setClipPath(clip)
        painter.setOpacity(self.ART_OPACITY + 0.14 * self._lift)
        # Scaled once per size, not per paint: the hover redraws it every frame.
        key = (self.art.cacheKey(), inner.size().toSize().width(), inner.size().toSize().height())
        if self._cover is None or self._cover[0] != key:
            self._cover = (key, self.art.scaled(
                inner.size().toSize(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
        cover = self._cover[1]
        zoom = 1.0 + 0.06 * self._lift
        painter.translate(inner.center())
        painter.scale(zoom, zoom)
        painter.drawPixmap(QPointF(-cover.width() / 2, -cover.height() / 2), cover)
        painter.resetTransform()
        if self._lift > 0.0:
            rim = QColor(theme.color(ROW_PINK_ON))
            rim.setAlphaF(0.55 * self._lift)
            painter.setOpacity(1.0)
            painter.setClipping(False)
            painter.setPen(QPen(rim, 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.path)
        super().mouseReleaseEvent(event)


class LibraryKeys(QObject):
    """The song select's keyboard: type anywhere to search, arrows between
    the songs and their difficulties, Enter opens, Left/Esc goes back."""

    def __init__(self, controller: "LibraryPageController") -> None:
        super().__init__(controller.window)
        self.controller = controller

    def eventFilter(self, watched, event) -> bool:
        if event.type() != QEvent.KeyPress:
            return False
        c = self.controller
        key = event.key()
        if key == Qt.Key_F5:
            c.reload_current_song()
            return True
        # Space pauses and resumes the preview, as in a music player -- except
        # mid-query, where it is a space in what is being searched for.
        if key == Qt.Key_Space and not event.modifiers() and not (
            watched is c.library_search and c.library_search.text()
        ):
            c.toggle_preview()
            return True
        if watched is c.library_search:
            if key in (Qt.Key_Up, Qt.Key_Down):
                c.move_song(-1 if key == Qt.Key_Up else 1)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter) and c.difficulty_list.count():
                c.difficulty_list.setFocus()
                return True
            return False
        if watched is c.song_list:
            if key in (Qt.Key_Up, Qt.Key_Down):
                c.move_song(-1 if key == Qt.Key_Up else 1)
                return True
            if key == Qt.Key_Right and c.difficulty_list.count():
                c.move_focus(c.difficulty_list)
                return True
            if key == Qt.Key_Escape and c.library_search.text():
                c.library_search.clear()
                return True
        elif watched is c.difficulty_list:
            if key in (Qt.Key_Left, Qt.Key_Escape):
                c.move_focus(c.song_list)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                c.open_selected_difficulty()
                return True
        text = event.text()
        if text and text.isprintable() and not text.isspace() and not event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            c.library_search.setFocus()
            c.library_search.insert(text)
            return True
        return False


class LibraryPageController:
    """Library page: browse every taiko chart under the osu! Songs folder,
    song then difficulty, and hand the chosen one back to the window.

    Owns the page's widgets and the scan state behind them. `_library_cache_path`
    stays on MainWindow itself rather than moving here -- tests monkeypatch it
    per-instance (`window._library_cache_path = lambda: ...`) to point a scan at
    a temp file, and a copy made here would not see that patch.
    """

    def __init__(self, window: "MainWindow") -> None:
        self.window = window
        # {song folder: [TaikoDifficulty, ...]} for every mode=1 chart found
        # under the configured osu! Songs folder. Filled by the sliced scan
        # (scan_step), persisted between runs in library_cache.
        self.library_songs: dict[Path, list] = {}
        self.library_cache: dict[str, list] = {}
        self._scan_songs: dict[Path, list] = {}
        self.listed_paths: set[Path] = set()
        # osu!stable's own star ratings, {(folder, file): DbBeatmap}; None when
        # there is no readable osu!.db beside the Songs folder.
        self.star_ratings: dict | None = None
        # The song preview. A QMediaPlayer rather than TrackPlayer: it streams,
        # so a preview starts at once instead of after decoding the whole
        # track, and nothing here needs the editor's sample-accurate clock.
        # Made on first use -- most windows (every test's) never preview.
        self.preview_player: QMediaPlayer | None = None
        self._preview_audio: Path | None = None
        self._preview_ms = -1
        self._preview_pending: tuple[Path, int] | None = None
        # One start somewhere other than the preview point: the editor's
        # playhead, handed over on the way back to the song list.
        self._resume_ms: int | None = None
        self._preview_loading = False
        # The player's Next/Previous walk: every song once, shuffled the first
        # time it is wanted and then kept, so Previous retraces Next.
        self._song_order: list[Path] | None = None
        self.preview_timer = QTimer(window)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(PREVIEW_SETTLE_MS)
        self.preview_timer.timeout.connect(self.start_preview)
        self._scan_iterator = None
        self._scan_files = 0
        self._scan_since_refresh = 0
        self.scan_timer = QTimer(window)
        self.scan_timer.setInterval(0)
        self.scan_timer.timeout.connect(self.scan_step)
        # Hashing and image decoding, off the GUI thread (BackgroundJobs).
        self.jobs = BackgroundJobs(window)
        # Each audio file's real length once the player has opened it: the
        # seek band stands for the song, and a chart's length stops at its
        # last note.
        self._audio_durations: dict[Path, float] = {}
        # Each difficulty's most common BPM, once read (_show_common_bpm).
        self._common_bpm: dict[Path, float | None] = {}

    def build_page(self) -> QWidget:
        """Browse every taiko chart under the osu! Songs folder: song, then difficulty.

        Laid out as the approved mockup (docs/DEVELOPMENT_PLAN.md, 3d): search
        and pills on one row, a Continue row of recent charts, the song list
        beside a detail pane headed by the map's own background.
        """
        page = QWidget()
        page.setObjectName("libraryPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 6)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.library_search = QLineEdit()
        self.library_search.setPlaceholderText(
            tr("MainWindow", "Search artist, title, difficulty, mapper or tags")
        )
        self.library_search.setToolTip(tr("MainWindow", "Type anywhere on this page to search"))
        self.library_search.setClearButtonEnabled(True)
        self.library_search.setMinimumWidth(240)
        self.library_search.textChanged.connect(lambda _text: self.rebuild_song_list_entering())

        # The combos are the model the pills drive: everything that reads the
        # grouping or the sort (and the tests) reads them.
        self.library_group_combo = QComboBox(page)
        self.library_group_combo.addItem(tr("MainWindow", "Nothing"), "none")
        self.library_group_combo.addItem(tr("MainWindow", "Mapper"), "mapper")
        self.library_group_combo.addItem(tr("MainWindow", "Artist"), "artist")
        self.library_group_combo.hide()
        self.library_sort_combo = QComboBox(page)
        self.library_sort_combo.addItem(tr("MainWindow", "A to Z"), "az")
        self.library_sort_combo.addItem(tr("MainWindow", "Z to A"), "za")
        self.library_sort_combo.hide()

        caption = QLabel(tr("MainWindow", "Group"))
        caption.setStyleSheet(f"color: {ROW_INK_3}; font-size: 11px; font-weight: 600;")
        top.addWidget(caption)
        for combo, labels in (
            (self.library_group_combo, (tr("MainWindow", "None"), tr("MainWindow", "Mapper"), tr("MainWindow", "Artist"))),
            (self.library_sort_combo, ("A–Z", "Z–A")),
        ):
            buttons = []
            group = QButtonGroup(page)
            for index, label in enumerate(labels):
                button = QPushButton(label)
                button.setCheckable(True)
                button.setToolTip(combo.itemText(index))
                group.addButton(button, index)
                buttons.append(button)
            buttons[combo.currentIndex()].setChecked(True)
            group.idClicked.connect(combo.setCurrentIndex)
            combo.currentIndexChanged.connect(lambda index, buttons=buttons: buttons[index].setChecked(True))
            combo.currentIndexChanged.connect(lambda _index: self.rebuild_song_list_entering())
            top.addWidget(segmented(buttons))

        self.original_metadata_check = QPushButton("原文")
        self.original_metadata_check.setCheckable(True)
        self.original_metadata_check.setToolTip(
            tr("MainWindow", "Show titles and artists in the song's own script instead of the romanized fields")
        )
        self.original_metadata_check.setChecked(self.window.settings.bool_value("library/original_metadata", False))
        self.original_metadata_check.toggled.connect(self.original_metadata_toggled)
        top.addWidget(segmented([self.original_metadata_check]))

        # osu!'s player: previous, play/pause, next -- no stop since
        # 2026-10-09 (owner: unnecessary for now). In the top row with
        # the other pills, where it is found at a glance -- under the banner it
        # read as part of the selected song's details. Space toggles it too.
        self.player_buttons = {}
        for key, glyph, tip, slot in (
            ("previous", "◀◀", tr("MainWindow", "Previous song"), lambda: self.step_song(-1)),
            ("play", "▶", tr("MainWindow", "Play or pause the preview"), self.toggle_preview),
            ("next", "▶▶", tr("MainWindow", "Next song"), lambda: self.step_song(1)),
        ):
            button = QPushButton(glyph)
            button.setToolTip(tip)
            # Not focusable, so Space reaches the song list's toggle instead
            # of re-clicking whichever transport button was pressed last.
            button.setFocusPolicy(Qt.NoFocus)
            # A glyph, not a word: the window's 16px side padding made the four
            # of them as wide as the search box could spare.
            button.setStyleSheet("padding-left: 8px; padding-right: 8px;")
            button.clicked.connect(lambda _checked=False, slot=slot: slot())
            self.player_buttons[key] = button
        equalize_button_widths(self.player_buttons.values())
        top.addWidget(segmented(list(self.player_buttons.values())))
        # Play is drawn rather than typed, so it can morph into pause and
        # back (PlayPauseGlyph). Its "▶" stays, invisible: a button's height
        # comes from its label, and emptied it came out shorter than the
        # buttons beside it (owner, 2026-10-09).
        play = self.player_buttons["play"]
        play.setAccessibleName(play.toolTip())
        play.setStyleSheet("color: transparent;")
        self.play_glyph = PlayPauseGlyph(play, ROW_INK_2, ROW_INK)

        # One quiet menu for three rare actions. Quick Scan is incremental and
        # Rescan throws the index away -- see `rescan` for why both exist.
        self.library_folder_label = QToolButton()
        self.library_folder_label.setObjectName("folderMenu")
        self.library_folder_label.setPopupMode(QToolButton.InstantPopup)
        self.library_folder_label.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.library_folder_label.setFocusPolicy(Qt.NoFocus)
        folder_menu = QMenu(self.library_folder_label)
        quick = folder_menu.addAction(tr("MainWindow", "Quick scan"))
        quick.setToolTip(tr("MainWindow", "Look for songs added or changed since the last scan"))
        quick.triggered.connect(self.start_scan)
        full = folder_menu.addAction(tr("MainWindow", "Rescan everything"))
        full.setToolTip(tr("MainWindow", "Rebuild the whole index from scratch (slower; use if Quick Scan missed a change)"))
        full.triggered.connect(self.rescan)
        reload_song = folder_menu.addAction(tr("MainWindow", "Reload this beatmap") + "\tF5")
        reload_song.setToolTip(tr("MainWindow", "Read the selected song's files again, and nothing else"))
        reload_song.triggered.connect(self.reload_current_song)
        change =folder_menu.addAction(tr("MainWindow", "Change folder…"))
        change.triggered.connect(self.choose_songs_folder)
        folder_menu.setToolTipsVisible(True)
        self.library_folder_label.setMenu(folder_menu)
        self.set_folder_text("")
        top.addStretch(1)
        top.addWidget(self.library_folder_label)

        # Two columns (owner, 2026-10-08): the search, Continue and the song
        # list share one width on the left, so the list is exactly as wide as
        # the box that filters it; the pills head the right, and the detail
        # pane rises to sit right under them.
        left = QWidget()
        left.setObjectName("libraryColumn")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(self.library_search)
        right = QWidget()
        right.setObjectName("libraryColumn")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.addLayout(top)

        # Continue: the last charts opened here, one click from reopening.
        self.continue_row = QWidget()
        self.continue_row.setObjectName("continueRow")
        continue_layout = QHBoxLayout(self.continue_row)
        continue_layout.setContentsMargins(0, 0, 0, 0)
        continue_layout.setSpacing(8)
        continue_caption = QLabel(tr("MainWindow", "Continue"))
        continue_caption.setStyleSheet(f"color: {ROW_INK_3}; font-size: 11px; font-weight: 600;")
        continue_layout.addWidget(continue_caption)
        # A fixed set of cards, refilled rather than rebuilt: see
        # OrphanedLayoutItemTests for what churning a layout's items costs here.
        self.continue_cards = [ContinueCard() for _ in range(RECENT_CHARTS_SHOWN)]
        for card in self.continue_cards:
            card.clicked.connect(self.show_recent)
            # One size each, the column's width shared out, and the same size
            # with fewer recent charts than cards: a hidden card keeps its slot.
            policy = card.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Ignored)
            policy.setRetainSizeWhenHidden(True)
            card.setSizePolicy(policy)
            continue_layout.addWidget(card, 1)
        left_layout.addWidget(self.continue_row)

        split = QSplitter(Qt.Horizontal)
        split.setObjectName("librarySplit")
        split.setChildrenCollapsible(False)
        self.song_list = QListWidget()
        song_rows = SongRowDelegate(self.song_list)
        self.song_list.setItemDelegate(song_rows)
        # Rows rising in after a search, a selection gliding between rows.
        self.song_entrance = song_rows.entrance = RowEntrance(self.song_list)
        glide_selection(self.song_list, (2, 1, 2, 1))
        self.scan_shimmer = ScanShimmer(self.song_list)
        self.song_list.setMouseTracking(True)
        self.song_list.setUniformItemSizes(False)
        # Rows elide rather than scroll sideways; the delegate paints to width.
        self.song_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.song_scroller = SmoothScroller(self.song_list)
        # The select sounds, and what they last sounded for.
        self._ui_players: dict[Path, QMediaPlayer] = {}
        self._sounded_song: Path | None = None
        self._sounded_difficulty: Path | None = None
        self._quiet_difficulty = False
        # The banner's beat: red lines per difficulty file, read once each.
        self._tempo_cache: dict[Path, list[tuple[float, float]]] = {}
        self._beat_audio: Path | None = None
        self._beat_anchor_ms = 0.0
        self._beat_elapsed = QElapsedTimer()
        self._beat_elapsed.start()
        self.song_list.currentRowChanged.connect(self.song_selected)
        self.song_list.itemActivated.connect(lambda _item: self.difficulty_list.setFocus())
        left_layout.addWidget(self.song_list, 1)
        split.addWidget(left)

        detail = QFrame()
        detail.setObjectName("songDetail")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(0)
        self.song_banner = SongBanner()
        # Far the larger stretch: the banner takes the height first, up to
        # 16:9 of its width (SongBanner.resizeEvent), and the difficulty list
        # has what is left past that.
        detail_layout.addWidget(self.song_banner, 100)
        # The selected difficulty, scrolling as it does in game -- SV, BPM and
        # SliderMultiplier, the editor's own preview -- at a share of its
        # usual height (owner's call, 2026-10-07: 67% by default, 50-75%).
        # It follows this page's song, not the editor's.
        self.chart_preview = GameplayViewerView()
        self.chart_preview.follows_view_opacity = False
        self.chart_preview.plain_stage = True
        self.chart_preview.hit_x_units = SONG_SELECT_HIT_X_UNITS
        # Shown with the first chart it has to show, like the difficulties:
        # before a song is picked it was an empty black band.
        self.chart_preview.hide()
        self.chart_preview.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.chart_preview.setFocusPolicy(Qt.NoFocus)  # the arrows are the lists'
        self.chart_preview.setMinimumHeight(0)
        if getattr(self.window, "skin", None) is not None:
            self.chart_preview.set_skin(self.window.skin)
        self.set_chart_preview_share(self.window.settings.int_value(
            "song_select/preview_percent", SONG_SELECT_PREVIEW_DEFAULT_PERCENT))
        self.chart_preview.seek_requested.connect(self.seek_preview_to)
        self.chart_preview.height_changed = self._fit_banner_to_preview
        self.song_banner.seek_fraction.connect(self._seek_preview_fraction)
        self.song_banner.seek_step.connect(self._step_preview)
        detail_layout.addWidget(self.chart_preview)
        self._chart_documents: dict[Path, object] = {}
        self._chart_pending: Path | None = None
        self._chart_shown: Path | None = None
        # After the same settle as the audio: arrowing down a list of
        # difficulties parses none of the ones passed on the way.
        self.chart_timer = QTimer(self.window)
        self.chart_timer.setSingleShot(True)
        self.chart_timer.setInterval(PREVIEW_SETTLE_MS)
        self.chart_timer.timeout.connect(self._load_chart_preview)
        # A frame for the preview, only while this page shows it. Precise and
        # 4ms, like the editor's render timer: a coarse 16ms timer held it to
        # 62.5 frames a second against the editor's 120 (owner: "a bit laggy").
        # At 4 it once painted 250 times a second; the tick now gates on the
        # display's refresh instead, so the poll rate no longer sets the paints.
        self.chart_frame = QTimer(self.window)
        self.chart_frame.setTimerType(Qt.PreciseTimer)
        # Polled at 4ms; _tick_chart_preview paints once per display refresh.
        self.chart_frame.setInterval(4)
        self._chart_frame_clock = QElapsedTimer()
        self._chart_frame_clock.start()
        self._chart_frame_due_ns = 0
        self.chart_frame.timeout.connect(self._tick_chart_preview)
        self.difficulty_list = QListWidget()
        self.difficulty_list.setObjectName("difficultyList")
        difficulty_rows = DifficultyRowDelegate(self.difficulty_list)
        self.difficulty_list.setItemDelegate(difficulty_rows)
        self.difficulty_entrance = difficulty_rows.entrance = RowEntrance(
            self.difficulty_list, row_ms=240, stagger_ms=40, delay_ms=60)
        glide_selection(self.difficulty_list, (2, 2, 2, 2))
        self.difficulty_list.setMouseTracking(True)
        self.difficulty_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.difficulty_list.itemActivated.connect(lambda _item: self.open_selected_difficulty())
        self.difficulty_list.currentItemChanged.connect(lambda *_: self.difficulty_changed())
        # Eased like the song list: every vertical scroll in the program is.
        self.difficulty_scroller = SmoothScroller(self.difficulty_list)
        # Whatever the 16:9 banner leaves, and never fewer than
        # DIFFICULTY_SLOTS rows: a bigger set scrolls.
        self.difficulty_list.setMinimumHeight(DIFFICULTY_SLOTS * DIFFICULTY_ROW_HEIGHT + 14)
        detail_layout.addWidget(self.difficulty_list, 1)
        # The selected row's rim is alive in the list that has the keys:
        # one rect repainted per frame, only while the list is on screen.
        for rows in (self.song_list, self.difficulty_list):
            tick_while_shown(rows, lambda rows=rows: self._repaint_selected_row(rows))
        actions = QFrame()
        actions.setObjectName("detailActions")
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(10, 10, 10, 10)
        self.open_difficulty_button = QPushButton(tr("MainWindow", "Edit This Difficulty"))
        self.open_difficulty_button.setObjectName("editDifficulty")
        self.open_difficulty_button.clicked.connect(self.open_selected_difficulty)
        actions_layout.addWidget(self.open_difficulty_button)
        detail_layout.addWidget(actions)
        right_layout.addWidget(detail, 1)
        self._detail, self._detail_actions = detail, actions
        ResizeHook(detail, self._fit_banner)
        split.addWidget(right)
        split.setStretchFactor(0, 145)
        split.setStretchFactor(1, 100)
        split.setSizes([740, 510])
        layout.addWidget(split, 1)
        self._split, self._handle_width = split, split.handleWidth()

        footer = QHBoxLayout()
        self.library_status = QLabel(tr("MainWindow", "No songs folder selected yet."))
        self.library_status.setStyleSheet(f"color: {ROW_INK_2}; font-size: 12px;")
        footer.addWidget(self.library_status, 1)
        self.library_rating_note = QLabel()
        self.library_rating_note.setStyleSheet(f"color: {ROW_INK_3}; font-size: 12px;")
        self.library_rating_note.hide()
        footer.addWidget(self.library_rating_note)
        self.scan_progress = QProgressBar()
        # Indeterminate: the walk discovers files as it goes, so there is no
        # honest total to count towards until it has already finished.
        self.scan_progress.setRange(0, 0)
        self.scan_progress.setFixedSize(140, 4)
        self.scan_progress.setTextVisible(False)
        self.scan_progress.setVisible(False)
        footer.addWidget(self.scan_progress)
        layout.addLayout(footer)

        self.keys = LibraryKeys(self)
        for widget in (self.library_search, self.song_list, self.difficulty_list):
            widget.installEventFilter(self.keys)

        self.page = page
        self.restyle()
        self.refresh_continue_row()
        self.update_open_button()
        return page

    def restyle(self) -> None:
        """The page's sheet, with NSO's desktop under a theme that draws
        window chrome. Again on a theme switch (MainWindow.retheme)."""
        desktop = windose.active()
        self.page.setStyleSheet(self._sheet() + (windose.library_chrome() if desktop else ""))
        # Room around the two windows for the wallpaper to read as a desktop:
        # at the page's own 12px it was a striped border, not a floor.
        self.page.layout().setContentsMargins(*((28, 20, 28, 12) if desktop else (12, 10, 12, 6)))
        self._split.setHandleWidth(20 if desktop else self._handle_width)

    def _sheet(self) -> str:
        return (
            f"""
            QListWidget {{ background: #222a36; border: 1px solid #303947; border-radius: 6px; padding: 4px; outline: 0; }}
            QFrame#songDetail {{ background: #222a36; border: 1px solid #303947; border-radius: 6px; }}
            QFrame#songDetail QListWidget {{ border: 0; background: transparent; padding: 6px; }}
            QFrame#detailActions {{ border: 0; border-top: 1px solid #303947; border-radius: 0; background: transparent; }}
            QPushButton#editDifficulty {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #f6b3c7, stop:0.6 #f3a6bd, stop:1 #ee97b1);
                color: #17191f; border: 0; border-radius: 6px; padding: 8px 14px; font-weight: 700; text-align: left;
            }}
            QPushButton#editDifficulty:hover {{ background: #f7bccd; }}
            QPushButton#editDifficulty:disabled {{ background: #39414d; color: #7d8794; }}
            QLineEdit {{ background: #252d39; border: 1px solid #3a4554; border-radius: 6px; padding: 7px 10px; font-size: 14px; }}
            QLineEdit:focus {{ border: 2px solid #ff9dcc; padding: 6px 9px; }}
            QToolButton#folderMenu {{ background: transparent; border: 1px solid #3a4554; border-radius: 6px;
                color: {ROW_INK_2}; padding: 5px 10px; font-weight: 600; }}
            QToolButton#folderMenu:hover {{ background: {ROW_HOVER}; color: {ROW_INK}; }}
            QToolButton#folderMenu::menu-indicator {{ image: none; }}
            QFrame#continueCard {{ background: #222a36; border: 1px solid #303947; border-radius: 6px; }}
            QFrame#continueCard:hover {{ background: {ROW_HOVER}; border-color: #ff66aa55; }}
            QFrame#continueCard QLabel {{ background: transparent; }}
            QProgressBar {{ background: #252d39; border: 0; border-radius: 2px; }}
            QProgressBar::chunk {{ background: #f3a6bd; border-radius: 2px; }}
            """
        )

    def set_folder_text(self, folder: str) -> None:
        metrics = self.library_folder_label.fontMetrics()
        # 160, not 260: this row heads the right column now, and its width is
        # that column's minimum -- at 1366 wide the song list had 360px.
        shown = metrics.elidedText(folder, Qt.ElideMiddle, 160) if folder else tr("MainWindow", "Songs folder")
        self.library_folder_label.setText(f"{shown}  ▾")
        self.library_folder_label.setToolTip(folder)

    def move_song(self, delta: int) -> None:
        """Up/Down from the search box: the next *song*, over group headers."""
        row = self.song_list.currentRow()
        while True:
            row += delta
            if not 0 <= row < self.song_list.count():
                return
            if self.song_list.item(row).data(Qt.UserRole) is not None:
                self.song_list.setCurrentRow(row)
                return

    def song_order(self) -> list[Path]:
        """The shuffled walk, fixed for the session. Songs the scan finds
        later are appended rather than reshuffled in, so nothing already
        walked moves."""
        if self._song_order is None:
            self._song_order = sorted(self.library_songs)
            random.Random().shuffle(self._song_order)
        else:
            known = set(self._song_order)
            self._song_order += [folder for folder in sorted(self.library_songs) if folder not in known]
        return self._song_order

    def step_song(self, delta: int) -> None:
        """Next/Previous in the shuffled order. Selecting the song in the list
        is what starts its preview (difficulty_changed), so nothing here plays.
        A song a search has filtered out of the list is stepped over."""
        order = self.song_order()
        item = self.song_list.currentItem()
        current = item.data(Qt.UserRole) if item is not None else None
        try:
            index = order.index(Path(current))
        except (ValueError, TypeError):
            index = -1 if delta > 0 else 0
        rows = {self.song_list.item(row).data(Qt.UserRole): row for row in range(self.song_list.count())}
        for step in range(1, len(order) + 1):
            row = rows.get(str(order[(index + delta * step) % len(order)]))
            if row is not None:
                self.song_list.setCurrentRow(row)
                return

    def toggle_preview(self) -> None:
        player = self.preview_player
        if player is not None and player.playbackState() == QMediaPlayer.PlayingState:
            player.pause()
        elif player is not None and player.playbackState() == QMediaPlayer.PausedState:
            player.play()
        elif player is not None and self._preview_audio is not None:
            self._play_preview()  # stopped: from the preview point again
        else:
            # Nothing loaded yet (or stopped by opening a chart): load the
            # selected difficulty's song now rather than after the settle delay.
            if self.difficulty_list.currentItem() is not None:
                self.difficulty_changed()
                self.preview_timer.stop()
                self.start_preview()

    def rewind_preview(self) -> None:
        """Stop, parked at the preview point so Play starts where song select does."""
        player = self.preview_player
        if player is None or self._preview_audio is None:
            return
        self.preview_fade.stop()
        player.stop()
        player.setPosition(max(0, self._preview_ms if self._preview_ms >= 0 else int(player.duration() * 0.4)))

    # -- recent charts ----------------------------------------------------

    def recent_charts(self) -> list[tuple[str, float | None]]:
        """[(path, when it was opened here)], newest first. An entry from
        before the times were kept is a bare path, with no time."""
        try:
            recent = json.loads(self.window.settings.string_value("library/recent", "[]") or "[]")
        except ValueError:
            return []
        charts = []
        for entry in recent:
            if isinstance(entry, str):
                charts.append((entry, None))
            elif isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str):
                charts.append((entry[0], entry[1] if isinstance(entry[1], (int, float)) else None))
        return charts

    def remember_recent(self, path: Path) -> None:
        recent = [[str(path), wall_clock()]] + [
            [item, opened] for item, opened in self.recent_charts()
            if os.path.normcase(item) != os.path.normcase(path)]
        self.window.settings.set_value("library/recent", json.dumps(recent[:RECENT_CHARTS_KEPT]))
        self.refresh_continue_row()

    def refresh_continue_row(self) -> None:
        original = self.original_metadata_check.isChecked()
        # Case-folded: the saved path is resolved and the scanned one is as
        # the Songs folder was typed, and "D:/osu!/songs" missed "D:\osu!\Songs".
        known = {os.path.normcase(d.path): d for group in self.library_songs.values() for d in group}
        shown = 0
        for path, opened in self.recent_charts():
            if shown == len(self.continue_cards):
                break
            if not Path(path).is_file():
                continue  # deleted or moved since: nothing to reopen
            difficulty = known.get(os.path.normcase(path))
            if difficulty is not None:
                title, version = difficulty.display_title(original), difficulty.version
                background = difficulty.folder / difficulty.background if difficulty.background else None
            else:
                # Opened here but not in the index (another folder, or not
                # scanned yet): its own header, rather than the file name.
                header = read_header(Path(path)) or {}
                pair = ("title_unicode", "title") if original else ("title", "title_unicode")
                title = next((header.get(key) for key in pair if header.get(key)), Path(path).parent.name)
                version = header.get("version") or Path(path).stem
                background = Path(path).parent / header["background"] if header.get("background") else None
            self.continue_cards[shown].show_chart(path, title, version, self._card_art(background))
            self.continue_cards[shown].show()
            shown += 1
        for card in self.continue_cards[shown:]:
            card.hide()
        self.continue_row.setVisible(shown > 0)

    def _card_art(self, path: Path | None) -> QPixmap | None:
        """A Continue card's background, decoded once per file: the row is
        refreshed on every return to this page, and three backgrounds decoded
        each time is a pause on a tab switch."""
        if path is None:
            return None
        cache = self.__dict__.setdefault("_card_art_cache", {})
        if path not in cache:
            # Cards are ~240px wide; twice that keeps the crop sharp.
            cache[path] = decode_background(path, 480) if path.is_file() else None
        return cache[path]

    def show_recent(self, path: str) -> None:
        """Select a Continue card's song and difficulty, and stop there.

        Opening it straight away skipped the one thing this page is for: the
        card is often the song and not the difficulty wanted, and picking a
        sibling meant opening, backing out and choosing again. Enter or a
        double click opens it from here, as for any other selection.
        """
        if not self.select_chart(path):
            # Not in the library at all (a chart outside the Songs folder):
            # there is nothing to select, so opening is the only thing to do.
            self.open_recent(path)
            return
        self.difficulty_list.setFocus()

    def select_chart(self, path) -> bool:
        """Put the lists on `path`'s song and difficulty; False when the song
        is not in the library."""
        wanted = os.path.normcase(str(Path(path).resolve()))
        folder = os.path.normcase(str(Path(path).resolve().parent))
        row = self._song_row_for(folder)
        if row is None and self.library_search.text():
            # Filtered out by a search, not missing: show it.
            self.library_search.clear()
            row = self._song_row_for(folder)
        if row is None:
            return False
        self.song_list.setCurrentRow(row)
        self.song_list.scrollToItem(self.song_list.item(row))
        for index in range(self.difficulty_list.count()):
            item = self.difficulty_list.item(index)
            if os.path.normcase(str(Path(item.data(Qt.UserRole)).resolve())) == wanted:
                self.difficulty_list.setCurrentRow(index)
                break
        return True

    def _song_row_for(self, folder: str) -> int | None:
        for row in range(self.song_list.count()):
            data = self.song_list.item(row).data(Qt.UserRole)
            if data is not None and os.path.normcase(str(Path(data).resolve())) == folder:
                return row
        return None

    def open_recent(self, path: str) -> None:
        self.stop_preview()
        self.window._load_map_path(Path(path).resolve(), refresh_difficulties=True)
        if self.window.state.source_path == Path(path).resolve():
            self.remember_recent(Path(path))

    # -- folder, scan, cache --------------------------------------------------

    def start(self) -> None:
        """First thing after the window is shown: get a folder, then scan it.

        Called from main() rather than __init__ so constructing a MainWindow
        (tests, and the Fancy Arranger's own file-dialog flow) never triggers a
        multi-thousand-file walk of someone's disk.
        """
        folder = self.window.settings.string_value("library/songs_folder", "")
        if folder and Path(folder).is_dir() and self.window.settings.bool_value("setup/completed", False):
            self.start_scan()
            return
        # First start: say what the folder picker about to open is for, in the
        # language just chosen, before dropping someone into a bare dialog.
        QMessageBox.information(
            self.window,
            tr("MainWindow", "Select your osu! Songs folder"),
            tr(
                "MainWindow",
                "Please select your osu! Songs folder.\n\n"
                "Every beatmap in it is checked once for taiko difficulties, and the result is "
                "remembered, so this only takes a while the first time.",
            ),
        )
        self.choose_songs_folder()

    def default_songs_folder(self) -> str:
        candidate = Path(
            QStandardPaths.writableLocation(QStandardPaths.HomeLocation) or str(Path.home())
        ) / "AppData" / "Local" / "osu!" / "Songs"
        return str(candidate) if candidate.is_dir() else ""

    def choose_songs_folder(self) -> None:
        current = self.window.settings.string_value("library/songs_folder", "") or self.default_songs_folder()
        folder = QFileDialog.getExistingDirectory(
            self.window, tr("MainWindow", "Select your osu! Songs folder"), current
        )
        if not folder:
            return
        self.window.settings.set_value("library/songs_folder", folder)
        # Only now is first-time setup really done; cancelling the picker
        # leaves the flag unset so the next launch offers it again.
        self.window.settings.set_value("setup/completed", True)
        self.window.settings.sync()
        self.start_scan()

    def rescan(self) -> None:
        """Rescan means rescan: throw the index away and walk the folder again.

        The ordinary startup scan is incremental -- it reuses the cached entry
        for any file whose size and mtime are unchanged, which is what makes it
        fast. That also means a file the cache is *wrong* about (edited by
        another program, restored from a backup, saved with the same size in the
        same second) stays wrong however many times Rescan is pressed. Deleting
        the file is what makes the button mean what it says.
        """
        try:
            self.window._library_cache_path().unlink()
        except OSError:
            pass  # Never there, or not ours to delete: the walk still rebuilds it.
        self.library_cache = {}
        self.library_songs = {}
        self.listed_paths = set()
        self.start_scan()

    def reload_current_song(self) -> None:
        """Re-read only the selected song's folder, from disk, ignoring the cache.

        For the chart just saved in osu!'s editor: Quick scan walks every file
        in Songs to find one, and trusts mtime and size, which an edit can
        leave unchanged. osu!.db is re-read too, so stars osu! has recomputed
        since show up.
        """
        item = self.song_list.currentItem()
        if item is None or item.data(Qt.UserRole) is None:
            return
        folder = Path(item.data(Qt.UserRole))
        # A fresh dict, not the library's: `scan` drops every cache entry it
        # did not walk past, which over one folder is everything else.
        fresh: dict[str, list] = {}
        found = [d for d in scan(folder, fresh) if d is not None]
        for key in [key for key in self.library_cache if Path(key).parent == folder]:
            del self.library_cache[key]
        self.library_cache.update(fresh)
        self.listed_paths -= {d.path for d in self.library_songs.pop(folder, [])}
        if found:
            self.library_songs[folder] = found
            self.listed_paths |= {d.path for d in found}
        for difficulty in found:
            self._tempo_cache.pop(difficulty.path, None)
        songs_folder = self.window.settings.string_value("library/songs_folder", "")
        if songs_folder:
            self.load_star_ratings(Path(songs_folder))
        try:
            save_cache(self.window._library_cache_path(), self.library_cache)
        except OSError:
            pass  # Same trade as finish_scan: the list matters more.
        # Selection is kept by folder, but the row is re-set from -1 so the
        # difficulty list refills from the new entries.
        self.rebuild_song_list()
        # And the editor's copies of them, or opening one shows this session's
        # version rather than the file just re-read.
        resolved = folder.resolve()
        self.window._reload_from_disk([path for path in list(self.window._states) if path.parent == resolved])

    def start_scan(self) -> None:
        folder = self.window.settings.string_value("library/songs_folder", "")
        self.set_folder_text(folder)
        if not folder or not Path(folder).is_dir():
            self.library_status.setText(tr("MainWindow", "No songs folder selected yet."))
            return
        self.scan_timer.stop()
        self.load_star_ratings(Path(folder))
        self.library_cache = load_cache(self.window._library_cache_path())
        # Show the saved index first, then verify it: on every start after the
        # first, the list is complete before a single file has been reopened.
        self.library_songs = group_by_song(songs_from_cache(self.library_cache, Path(folder)))
        self.listed_paths = {
            difficulty.path for group in self.library_songs.values() for difficulty in group
        }
        self.rebuild_song_list()
        # The walk fills a separate dict that becomes the authoritative one at
        # the end, so difficulties deleted since the last run disappear -- while
        # anything genuinely new is added to the visible list as it is found.
        self._scan_songs = {}
        self._scan_files = 0
        self._scan_since_refresh = 0
        self._scan_iterator = scan(Path(folder), self.library_cache)
        self.scan_progress.setVisible(True)
        self.update_scan_status(
            tr("MainWindow", "{songs} taiko songs from the saved index. Checking for new maps...")
            if self.library_songs
            else tr("MainWindow", "Scanning {files} files, {songs} taiko songs found")
        )
        self.scan_timer.start()

    def scan_step(self) -> None:
        """Consume one frame's worth of the scan, then hand the UI back."""
        deadline = perf_counter() + SCAN_SLICE_SECONDS
        while perf_counter() < deadline:
            try:
                found = next(self._scan_iterator)
            except StopIteration:
                self.finish_scan()
                return
            self._scan_files += 1
            if found is not None:
                self._scan_songs.setdefault(found.folder, []).append(found)
                if found.path not in self.listed_paths:
                    self.listed_paths.add(found.path)
                    self.library_songs.setdefault(found.folder, []).append(found)
                    self._scan_since_refresh += 1
        # Redrawing a list of thousands of rows costs far more than the scan
        # slice itself, so it happens on found songs, not on every tick.
        if self._scan_since_refresh >= 200:
            self._scan_since_refresh = 0
            self.rebuild_song_list()
        self.update_scan_status(tr("MainWindow", "Scanning {files} files, {songs} taiko songs found"))

    def finish_scan(self) -> None:
        self.scan_timer.stop()
        self._scan_iterator = None
        self.scan_progress.setVisible(False)
        # What the walk actually found wins: this is where songs deleted since
        # the last run leave the list.
        self.library_songs = self._scan_songs
        try:
            save_cache(self.window._library_cache_path(), self.library_cache)
        except OSError:
            pass  # A browsable list now matters more than a fast start next time.
        self.rebuild_song_list()
        self.update_scan_status(tr("MainWindow", "{songs} taiko songs, {files} files scanned"))

    def load_star_ratings(self, songs_folder: Path) -> None:
        """osu!stable's osu!.db, one level above Songs. Read once per scan
        (0.14s for 24,348 maps, measured 2026-10-11); a missing or unreadable one means every
        difficulty shows unrated, and the footer says why, once."""
        try:
            self.star_ratings = osu_db.read(songs_folder.parent / "osu!.db")
        except (OSError, ValueError, IndexError, struct.error):
            self.star_ratings = None
        self.library_rating_note.setText(tr(
            "MainWindow", "Star ratings come from osu!stable's osu!.db, and none was found beside this Songs folder."))
        self.library_rating_note.setVisible(self.star_ratings is None)

    def rating_entry(self, difficulty):
        """osu!.db's entry for this file, when it has a taiko rating."""
        if not self.star_ratings:
            return None
        entry = self.star_ratings.get((difficulty.path.parent.name.lower(), difficulty.path.name.lower()))
        return entry if entry is not None and entry.taiko_stars is not None else None

    def update_scan_status(self, template: str) -> None:
        self.library_status.setText(
            template.format(files=self._scan_files, songs=len(self.library_songs))
        )
        # The first scan, with nothing found yet: placeholder rows, not a void.
        self.scan_shimmer.setVisible(self._scan_iterator is not None and not self.library_songs)

    def rebuild_song_list_entering(self) -> None:
        """A rebuild the user asked for -- a search, a grouping, a sort --
        with the rows rising into place. A scan's rebuilds stay still."""
        self.rebuild_song_list()
        self.song_entrance.start()

    # -- the two lists ----------------------------------------------------

    def original_metadata_toggled(self, checked: bool) -> None:
        self.window.settings.set_value("library/original_metadata", checked)
        self.window.settings.sync()
        self.rebuild_song_list()
        self.refresh_continue_row()

    def song_entries(self) -> list[tuple[str, str, str, Path]]:
        """(group, sort label, row text, folder) for the songs the filters keep.
        The row text is what the list reports; `SongRowDelegate` paints it.

        Sorted by group first so the list can be walked once and broken into
        headers; Z-to-A reverses the groups too, which is what "sort by
        artist, Z to A" has to mean once songs are grouped by artist.
        """
        original = self.original_metadata_check.isChecked()
        grouping = str(self.library_group_combo.currentData())
        query = self.library_search.text()
        entries: list[tuple[str, str, str, Path]] = []
        for folder, difficulties in self.library_songs.items():
            if not matches_search(difficulties, query):
                continue
            first = difficulties[0]
            label = first.song_label(original)
            parts = [label]
            if first.creator:
                parts.append(first.creator)
            parts.append(str(len(difficulties)))
            group = ""
            if grouping == "mapper":
                group = first.creator or tr("MainWindow", "Unknown mapper")
            elif grouping == "artist":
                group = first.display_artist(original) or tr("MainWindow", "Unknown artist")
            entries.append((group, label, "   ·   ".join(parts), folder))
        entries.sort(key=lambda entry: (entry[0].lower(), entry[1].lower()))
        if str(self.library_sort_combo.currentData()) == "za":
            entries.reverse()
        return entries

    def rebuild_song_list(self) -> None:
        previous = self.song_list.currentItem()
        previous_folder = previous.data(Qt.UserRole) if previous else None
        self.song_list.blockSignals(True)
        self.song_list.clear()
        current_group = None
        for group, _label, text, folder in self.song_entries():
            if group and group != current_group:
                current_group = group
                header = QListWidgetItem(group)
                # Enabled but not selectable: a divider you cannot land on,
                # while still painting in the enabled palette -- NoItemFlags
                # would grey the text out and lose the accent colour.
                header.setFlags(Qt.ItemIsEnabled)
                font = header.font()
                font.setBold(True)
                header.setFont(font)
                header.setForeground(theme.text(ACCENT_PINK))
                self.song_list.addItem(header)
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, str(folder))
            item.setData(ROW_ROLE, self.song_row(folder))
            item.setToolTip(str(folder))
            self.song_list.addItem(item)
        self.song_list.blockSignals(False)
        for row in range(self.song_list.count()):
            if self.song_list.item(row).data(Qt.UserRole) == previous_folder:
                self.song_list.setCurrentRow(row)
                return
        self.difficulty_list.clear()

    def song_row(self, folder: Path) -> dict:
        difficulties = self.library_songs.get(folder, [])
        original = self.original_metadata_check.isChecked()
        first = difficulties[0]
        artist = first.display_artist(original)
        subtitle = " · ".join(part for part in (
            artist, tr("MainWindow", "mapped by {name}").format(name=first.creator) if first.creator else "") if part)
        # The dots skip the MD5 check: only the selected song's files are hashed.
        entries = [self.rating_entry(d) for d in difficulties]
        stars = sorted((e.taiko_stars if e else None for e in entries), key=lambda value: (value is None, value or 0))
        return {"title": first.display_title(original), "subtitle": subtitle, "stars": stars}

    def song_selected(self, row: int) -> None:
        # The difficulty list is refilled below and lands on its first row by
        # itself: that is part of picking a song, not a second pick.
        self._quiet_difficulty = True
        try:
            self._song_selected(row)
        finally:
            self._quiet_difficulty = False

    def _song_selected(self, row: int) -> None:
        self.difficulty_list.clear()
        item = self.song_list.item(row)
        if item is None or item.data(Qt.UserRole) is None:
            self.update_open_button()
            # No song, no chart: the preview goes with the difficulties.
            self.chart_preview.hide()
            self._fit_banner()
            self.chart_frame.stop()
            self._chart_shown = self._chart_pending = None
            return  # a group header
        folder = Path(item.data(Qt.UserRole))
        # A different song, not the same one re-listed by a search keystroke.
        new_song = folder != self._sounded_song
        if new_song:
            self._sounded_song = folder
            self.play_ui_sound("select_expand")
        difficulties = self.library_songs.get(folder, [])
        if not difficulties:
            return
        original = self.original_metadata_check.isChecked()
        rated = []
        rated_md5: dict[Path, str] = {}
        for difficulty in difficulties:
            entry = self.rating_entry(difficulty)
            stars = entry.taiko_stars if entry else None
            if entry is not None:
                rated_md5[difficulty.path] = entry.md5
            rated.append((difficulty, stars, False))
        # Easiest first, as osu! lists them; unrated after, by name.
        rated.sort(key=lambda entry: (entry[1] is None, entry[1] or 0.0, entry[0].version.lower()))
        for difficulty, stars, stale in rated:
            entry_item = QListWidgetItem(difficulty.version or difficulty.path.stem)
            entry_item.setData(Qt.UserRole, str(difficulty.path))
            notes = (tr("MainWindow", "{n} notes").format(n=difficulty.notes) if difficulty.notes else "–")
            timing = self._timing_text(difficulty, self._common_bpm.get(difficulty.path))
            entry_item.setData(ROW_ROLE, {
                "stars": stars, "stale": stale, "stats": (notes, timing), "notes": difficulty.notes,
            })
            tip = difficulty.path.name
            if stale:
                tip += "\n" + tr("MainWindow", "Changed since osu! rated it: open it in osu! to update the stars.")
            elif stars is None:
                tip += "\n" + tr("MainWindow", "Not rated yet: open it in osu! (or press F5 in song select), then Quick scan.")
            entry_item.setToolTip(tip)
            self.difficulty_list.addItem(entry_item)
        first = difficulties[0]
        artist = first.display_artist(original)
        # The song's words; the art and the beat are the selected
        # difficulty's (see _show_banner_art, _keep_time_with), set once
        # difficulty_changed below runs.
        self._banner_song = (
            first.display_title(original),
            " · ".join(part for part in (artist, tr("MainWindow", "mapped by {name}").format(name=first.creator)
                                         if first.creator else "") if part),
        )
        self._banner_art_key = None
        self._shown_difficulties = {str(d.path): d for d in difficulties}
        if self.difficulty_list.count():
            self.difficulty_list.setCurrentRow(0)
        if new_song:
            self.difficulty_entrance.start()
        # A range's most common BPM reads the whole file, so it is found off
        # the GUI thread the first time and remembered after.
        unknown = [d for d in difficulties if d.notes and round(d.bpm_min) != round(d.bpm_max)
                   and d.path not in self._common_bpm]
        if unknown:
            self.jobs.submit(lambda: {d.path: most_common_bpm(d.path) for d in unknown},
                             lambda found: self._show_common_bpm(found, unknown))
        if rated_md5:
            # osu! keys its rating to the file's hash: an edit since, here or
            # anywhere, leaves the number describing a chart that is gone.
            # Hashed off the GUI thread -- every difficulty of the song, on
            # every selection, was 62ms of the stutter -- and marked when done.
            self.jobs.submit(lambda: {path: osu_db.file_md5(path) for path in rated_md5},
                             lambda found: self._mark_stale(found, rated_md5))
        self.difficulty_changed()
        if self._banner_art_key is None:
            self._show_banner_art(first)

    @staticmethod
    def _timing_text(difficulty, common: float | None) -> str:
        if not difficulty.notes:
            return ""
        return (f"{format_length(difficulty.length_ms)} · "
                f"{format_bpm(difficulty.bpm_min, difficulty.bpm_max, common)} BPM")

    def _show_common_bpm(self, found: dict, difficulties) -> None:
        self._common_bpm.update(found)
        by_path = {d.path: d for d in difficulties}
        for row in range(self.difficulty_list.count()):
            item = self.difficulty_list.item(row)
            difficulty = by_path.get(Path(item.data(Qt.UserRole)))
            if difficulty is not None:
                data = dict(item.data(ROW_ROLE) or {})
                notes, _timing = data.get("stats", ("", ""))
                data["stats"] = (notes, self._timing_text(difficulty, found.get(difficulty.path)))
                item.setData(ROW_ROLE, data)

    def _mark_stale(self, found: dict, rated_md5: dict) -> None:
        """Dash the pill of every listed difficulty changed since osu! rated
        it. A song moved away from has none of these rows left to mark."""
        for row in range(self.difficulty_list.count()):
            item = self.difficulty_list.item(row)
            path = Path(item.data(Qt.UserRole))
            if path in found and found[path] != rated_md5.get(path):
                data = dict(item.data(ROW_ROLE) or {})
                data["stale"] = True
                item.setData(ROW_ROLE, data)
                item.setToolTip(path.name + "\n" + tr(
                    "MainWindow", "Changed since osu! rated it: open it in osu! to update the stars."))

    def _show_banner_art(self, difficulty) -> None:
        """The banner shows the selected difficulty's own background.

        Like its audio: a folder can hold several songs, or one song with a
        background per difficulty, and the first difficulty's art headed all
        of them. Reloaded -- and crossfaded -- only when the file changes, so
        moving between difficulties that share one does nothing visible.
        """
        key = (difficulty.folder, difficulty.background)
        if key == self._banner_art_key:
            return
        self._banner_art_key = key
        title, subtitle = self._banner_song
        self.song_banner.set_text(title, subtitle)
        if not difficulty.background:
            self.song_banner.set_art(None)
            return
        # Decoded off the GUI thread (33ms median, measured), and crossfaded
        # in when it lands -- unless the selection has moved on by then.
        path = difficulty.folder / difficulty.background
        self.jobs.submit(lambda: decode_background_image(path, 1280),
                         lambda image: self._banner_art_decoded(key, image))

    def _banner_art_decoded(self, key, image: QImage | None) -> None:
        if key == self._banner_art_key:
            self.song_banner.set_art(QPixmap.fromImage(image) if image is not None else None)

    def _keep_time_with(self, difficulty) -> None:
        """The banner's beat, from this difficulty's red lines and audio.

        Its own, not the song's first difficulty's: one folder can hold a cut
        and a full version, or songs at different tempos, each difficulty
        naming its own AudioFilename and timing.
        """
        audio = difficulty.folder / difficulty.audio if difficulty.audio else None
        tempo = self._tempo_cache.get(difficulty.path)
        if tempo is None:
            tempo = self._tempo_cache[difficulty.path] = heard_tempo(difficulty.path)
        try:
            start = float(difficulty.preview)
        except ValueError:
            start = -1.0
        banner = self.song_banner
        banner.set_preview(start)
        if audio == self._beat_audio and banner._clock is not None:
            # Same audio: the clock already runs on it, only the lines change.
            banner.set_tempo(tempo, self._banner_clock, self._banner_playing)
            return
        self._beat_audio = audio
        # Until the preview is heard, count from where it will start.
        self._beat_anchor_ms = start if start >= 0 else (tempo[0][0] if tempo else 0.0)
        self._beat_elapsed.start()
        banner.set_tempo(tempo, self._banner_clock, self._banner_playing)
        # The track is the song: its real length once the player has opened
        # it, and until then the chart's, which stops at the last note.
        banner.set_duration(self._audio_durations.get(audio) or float(difficulty.length_ms or 0))
        banner.set_kiai([])  # the new chart's arrive with it (_load_chart_preview)
        banner.glide(rewind=True)

    def _banner_has_song(self) -> bool:
        """The player holds this difficulty's audio, loaded."""
        player = self.preview_player
        return (player is not None and not self._preview_loading and self._preview_audio is not None
                and self._preview_audio == self._beat_audio)

    def _banner_playing(self) -> bool:
        """The band moves and pulses only while this song is really heard.
        Paused, stopped, loading or not yet started, it holds still: it once
        counted wall time from the preview point whenever the player was not
        playing this audio, so after a pick it ran on in silence (owner,
        2026-10-09)."""
        return self._banner_has_song() and self.preview_player.playbackState() == QMediaPlayer.PlayingState

    def _banner_clock(self) -> float:
        if self._banner_playing():
            return self._beat_clock()
        if self._banner_has_song():
            return float(self.preview_player.position())  # where it was paused or stopped
        return self._beat_anchor_ms  # where it will start

    def _beat_clock(self) -> float:
        """Song milliseconds for the beat line: the preview's position while it
        plays this audio, and a running count from the preview point before.

        Wall time between re-anchors, because the player reports its position
        in coarse steps (CLAUDE.md, the FFmpeg backend): read directly, the
        flash would step with it.

        Compared only on the frame a *new* step arrives. The steps are ~52ms
        apart, so between them the reported value is up to 52ms stale, and
        checking it every frame against a 40ms bound snapped the clock back
        40ms about five times a second -- measured, the chart preview's
        "a bit laggy". A fresh step is the true position to within a frame:
        a small error is eased out, a large one (a seek, the loop back to
        the preview point) is taken at once.
        """
        player = self.preview_player
        # Wall time runs at 1x; the song runs at the rate mod's rate.
        rate = player.playbackRate() if player is not None else 1.0
        now = self._beat_anchor_ms + self._beat_elapsed.elapsed() * rate
        if (player is not None and self._preview_audio == self._beat_audio
                and player.playbackState() == QMediaPlayer.PlayingState):
            position = float(player.position())
            if position != getattr(self, "_beat_seen_position", None):
                self._beat_seen_position = position
                error = position - now
                if abs(error) > 100:
                    self._beat_anchor_ms = position
                    self._beat_elapsed.restart()
                    now = position
                else:
                    self._beat_anchor_ms += error * 0.2
                    now += error * 0.2
        return now

    def _repaint_selected_row(self, rows: QListWidget) -> None:
        if rows.hasFocus():
            rows.viewport().update(rows.visualRect(rows.currentIndex()))

    def move_focus(self, to: QListWidget) -> None:
        """Hand the keys to the other list, with the morph flying between the
        two selections so it is visible where they went (Left/Right)."""
        source = self.difficulty_list if to is self.song_list else self.song_list
        page = to.window().findChild(QWidget, "libraryPage")
        if page is not None:
            if getattr(self, "_morph", None) is None:
                self._morph = MorphRect(page)

            def mapped(rows: QListWidget) -> QRect:
                rect = rows.visualRect(rows.currentIndex())
                return QRect(rows.viewport().mapTo(page, rect.topLeft()), rect.size())

            self._morph.fly(mapped(source), mapped(to))
        to.setFocus()
        for rows in (self.song_list, self.difficulty_list):
            rows.viewport().update()

    def difficulty_changed(self) -> None:
        """The preview follows the *difficulty*: one folder can hold several
        songs (a pack, a cut and a full version), each difficulty naming its
        own AudioFilename. Two on the same file keep playing where they are."""
        self.update_open_button()
        item = self.difficulty_list.currentItem()
        difficulty = item and getattr(self, "_shown_difficulties", {}).get(item.data(Qt.UserRole))
        if difficulty is not None and difficulty.path != self._sounded_difficulty:
            self._sounded_difficulty = difficulty.path
            if not self._quiet_difficulty:
                self.play_ui_sound("select_difficulty")
        if difficulty is not None:
            self.schedule_preview(difficulty)
            self._show_banner_art(difficulty)
            self._keep_time_with(difficulty)
            self._chart_pending = difficulty.path
            # The chart on screen fades while the next one settles and loads,
            # so it never sits there scrolling to the wrong song's clock; one
            # come back to before it was replaced fades straight back in.
            if difficulty.path == self._chart_shown:
                self.chart_preview.fade_chart(1.0, CHART_FADE_IN_MS)
            else:
                self.chart_preview.fade_chart(0.0, CHART_FADE_OUT_MS)
            self.chart_timer.start()

    # -- the chart preview under the banner -------------------------------

    def set_chart_preview_share(self, percent: int) -> None:
        percent = max(SONG_SELECT_PREVIEW_MIN_PERCENT, min(SONG_SELECT_PREVIEW_MAX_PERCENT, int(percent)))
        self.chart_preview.height_share = percent / 100.0
        width = self.chart_preview.width()
        height = round(osu_screen_height_for(width) * percent / 100.0)
        self.chart_preview.setFixedHeight(height)
        self._fit_banner_to_preview(height)

    def _fit_banner_to_preview(self, preview_height: int) -> None:
        """The preview's height comes out of the banner's minimum, not on top
        of it: added on top, it raised the whole window's minimum height by
        114px (836 -> 950), past a 1366x768 screen's. At any ordinary window
        size the banner is far taller than its minimum anyway."""
        self.song_banner.setMinimumHeight(max(0, SongBanner.MIN_HEIGHT - preview_height))
        self._fit_banner()

    def _fit_banner(self) -> None:
        """The banner's ceiling: 16:9 of its width, a background's own shape,
        and never so tall that the difficulty list has less than
        DIFFICULTY_RESERVED_SLOTS rows -- which is room it keeps until the
        window is too short for both, when the list gives way first."""
        detail = getattr(self, "_detail", None)
        if detail is None:
            return
        preview = self.chart_preview.height() if self.chart_preview.isVisibleTo(detail) else 0
        reserved = DIFFICULTY_RESERVED_SLOTS * DIFFICULTY_ROW_HEIGHT + 14
        room = detail.height() - preview - self._detail_actions.sizeHint().height() - reserved
        ceiling = min(round(detail.width() * 9 / 16), room)
        self.song_banner.setMaximumHeight(max(self.song_banner.minimumHeight(), ceiling))

    def _load_chart_preview(self) -> None:
        path = self._chart_pending
        if path is None or path == self._chart_shown:
            return
        document = self._chart_documents.get(path)
        if document is None:
            # Parsed on a worker: up to 20ms of pure Python on the GUI thread
            # was the longest single block left in a switch (measured). It
            # still shares the GIL, but in the interpreter's 5ms slices rather
            # than one frame-eating piece. Unreadable: the job fails, and the
            # preview keeps what it had.
            self.jobs.submit(lambda: parse_osu(path), lambda parsed: self._chart_parsed(path, parsed))
            return
        self._show_chart(path, document)

    def _chart_parsed(self, path: Path, document) -> None:
        # A handful, not the library: each holds every hit object.
        if len(self._chart_documents) >= 8:
            self._chart_documents.pop(next(iter(self._chart_documents)))
        self._chart_documents[path] = document
        if path == self._chart_pending:  # still the one wanted
            self._show_chart(path, document)

    def _show_chart(self, path: Path, document) -> None:
        self._chart_shown = path
        self.chart_preview.refresh_notes(document)
        self.song_banner.set_kiai(self.chart_preview.kiai_bands)
        # In from nothing whatever the last one was doing: its fade out may
        # not have finished if the load was asked for directly (Play).
        self.chart_preview._set_chart_opacity(0.0)
        self.chart_preview.show()
        self._fit_banner()
        self._tick_chart_preview(force=True)
        self.chart_preview.fade_chart(1.0, CHART_FADE_IN_MS)
        self.chart_frame.start()

    def _chart_clock(self) -> float:
        """Song milliseconds for the chart preview: the beat line's clock
        while the song plays, and the player's own position while it is
        paused -- the beat clock counts wall time and would scroll on."""
        player = self.preview_player
        if player is None:
            return self._beat_anchor_ms
        if player.playbackState() == QMediaPlayer.PlayingState:
            return self._beat_clock()
        return float(player.position())

    def _tick_chart_preview(self, force: bool = False) -> None:
        if not force and not self.chart_preview.isVisible():
            return
        if not force:
            # Painted once per refresh of the screen it is on, not per tick of
            # the 4ms timer -- 125 paints a second judders on a 100Hz screen
            # just as the editor's fixed 120 did (display_frame_interval_ns).
            due = next_frame_due(
                self._chart_frame_due_ns, self._chart_frame_clock.nsecsElapsed(),
                display_frame_interval_ns(self.chart_preview.screen()))
            if due is None:
                return
            self._chart_frame_due_ns = due
        now = self._chart_clock()
        if force or abs(now - self.chart_preview.current_time) > 0.5:
            self.chart_preview.current_time = now
            self.chart_preview.update()

    def seek_preview_to(self, position_ms: int) -> None:
        """Move this page's song (and so the beat and the chart) to
        `position_ms`. Never the editor's."""
        player = self.preview_player
        if player is None or player.duration() <= 0:
            return
        position_ms = max(0, min(int(position_ms), player.duration() - 1))
        player.setPosition(position_ms)
        self._beat_anchor_ms = float(position_ms)
        self._beat_elapsed.restart()
        self._tick_chart_preview(force=True)

    def _seek_preview_fraction(self, fraction: float) -> None:
        player = self.preview_player
        if player is not None:
            self.seek_preview_to(round(fraction * player.duration()))

    def _step_preview(self, direction: int) -> None:
        player = self.preview_player
        if player is not None:
            self.seek_preview_to(round(self._chart_clock() + direction * player.duration() * 0.01))

    def play_ui_sound(self, key: str, volume: float | None = None) -> None:
        """select-expand / select-difficulty: the skin's own, or the app's.

        Per sound, like the hitsounds: a skin with only one of the two keeps
        the app's other. At the effects (hitsound) volume, not the music's --
        these are the interface's sounds, not the song's -- unless `volume`
        says otherwise.
        """
        path = getattr(self.window, "skin", None) and self.window.skin.ui_sounds.get(key)
        if path is None:
            path = next((candidate for candidate in
                         (root / "assets" / "se" / f"{UI_SOUNDS[key]}.wav" for root in resource_roots())
                         if candidate.is_file()), None)
        if path is None:
            return
        player = self._ui_players.get(path)
        if player is None:
            # QMediaPlayer rather than QSoundEffect: a skin's may be .ogg or
            # .mp3, which QSoundEffect cannot open. A click this long can take
            # the extra latency.
            player = QMediaPlayer(self.window)
            player.setAudioOutput(QAudioOutput(player))
            player.setSource(QUrl.fromLocalFile(str(path)))
            self._ui_players[path] = player
        if volume is None:
            volume = self.window.settings.int_value("audio/hitsound_volume", 70) / 100.0
        player.audioOutput().setVolume(volume)
        player.stop()
        player.play()

    # -- song preview -----------------------------------------------------

    def schedule_preview(self, difficulty) -> None:
        if not difficulty.audio:
            return
        try:
            preview_ms = int(float(difficulty.preview))
        except ValueError:
            preview_ms = -1
        self._preview_pending = (difficulty.folder / difficulty.audio, preview_ms)
        self.preview_timer.start()

    def start_preview(self) -> None:
        if self._preview_pending is None or not self.song_list.isVisible():
            return
        audio, preview_ms = self._preview_pending
        self._preview_pending = None
        if audio == self._preview_audio and self.preview_player is not None \
                and self.preview_player.playbackState() == QMediaPlayer.PlayingState:
            return  # another difficulty of the song already playing
        if not audio.is_file():
            self.stop_preview()
            return
        if self.preview_player is None:
            self.preview_player = QMediaPlayer(self.window)
            self.preview_player.setAudioOutput(QAudioOutput(self.preview_player))
            self.preview_player.mediaStatusChanged.connect(self._preview_status)
            self.preview_player.durationChanged.connect(self._preview_duration)
            self.preview_player.playbackStateChanged.connect(self._preview_state_changed)
            self.preview_fade = QVariantAnimation(self.preview_player)
            self.preview_fade.setDuration(PREVIEW_FADE_MS)
            self.preview_fade.valueChanged.connect(
                lambda value: self.preview_player.audioOutput().setVolume(float(value)))
        self.preview_player.stop()
        self._preview_audio, self._preview_ms = audio, preview_ms
        self.preview_player.audioOutput().setVolume(0.0)
        # Set after stop(): stopping reports LoadedMedia too, and taken as a
        # fresh load it started the song again the moment the page was left.
        self._preview_loading = True
        self.preview_player.setSource(QUrl.fromLocalFile(str(audio)))

    def _preview_state_changed(self, state) -> None:
        self.play_glyph.set_playing(state == QMediaPlayer.PlayingState)
        # Re-anchor the beat clock on the player's own position whenever it
        # starts or stops. Left alone, the anchor was wherever playback last
        # was, and the wall time spent paused counted as song time: resumed
        # after a long pause, the playhead (and the chart) showed where the
        # song would have been had it kept playing, until the next position
        # report snapped it back (owner, 2026-10-09).
        if self._preview_audio is not None and self._preview_audio == self._beat_audio:
            self._beat_anchor_ms = float(self.preview_player.position())
            self._beat_elapsed.restart()
            self._beat_seen_position = None

    def _preview_duration(self, duration_ms: int) -> None:
        audio = self._preview_audio
        if duration_ms > 0 and audio is not None:
            self._audio_durations[audio] = float(duration_ms)
            if audio == self._beat_audio:
                self.song_banner.set_duration(float(duration_ms))

    def _preview_status(self, status) -> None:
        if status == QMediaPlayer.LoadedMedia and self._preview_loading:
            self._preview_loading = False
            self._play_preview()
        elif status == QMediaPlayer.EndOfMedia and self._preview_audio is not None:
            self._play_preview()  # round again from the preview point, as song select does

    def continue_song(self, audio: Path, position_ms: int, preview_ms: int | None) -> None:
        """Play `audio` from `position_ms` now, looping to its preview point."""
        self.preview_timer.stop()
        self._preview_pending = (audio, -1 if preview_ms is None else preview_ms)
        self._resume_ms = position_ms
        self.start_preview()

    def _play_preview(self) -> None:
        player = self.preview_player
        # osu! starts an unset PreviewTime at 40% of the track.
        start = self._preview_ms if self._preview_ms >= 0 else int(player.duration() * 0.4)
        if self._resume_ms is not None:
            start, self._resume_ms = self._resume_ms, None
        player.setPosition(max(0, start))
        # Always the original speed and pitch: the rate mod is an editing/play
        # choice, and the player object persists across songs.
        player.setPlaybackRate(1.0)
        player.play()
        if self._chart_shown is not None:
            self.chart_frame.start()
        volume = self.window.settings.int_value("audio/music_volume", 65) / 100.0
        self.preview_fade.stop()
        self.preview_fade.setStartValue(0.0)
        self.preview_fade.setEndValue(volume)
        self.preview_fade.start()

    def set_preview_volume(self, volume: float) -> None:
        """Reach a preview already playing. `_play_preview` reads the setting
        only as each loop starts, so a Settings change was not heard until the
        song was selected again. Mid-fade, retarget the fade instead: set
        directly, the next fade step would overwrite it."""
        if self.preview_player is None or self._preview_loading:
            return
        if self.preview_fade.state() == QVariantAnimation.Running:
            self.preview_fade.setEndValue(volume)
        else:
            self.preview_player.audioOutput().setVolume(volume)

    def stop_preview(self) -> None:
        self.chart_frame.stop()
        self.preview_timer.stop()
        self._preview_pending = None
        self._preview_loading = False
        self._preview_audio = None
        self._resume_ms = None
        if self.preview_player is not None:
            self.preview_fade.stop()
            self.preview_player.stop()
            self.preview_player.setSource(QUrl())

    def update_open_button(self) -> None:
        item = self.difficulty_list.currentItem()
        self.open_difficulty_button.setEnabled(item is not None)
        self.open_difficulty_button.setText(
            tr("MainWindow", "Edit {version}  ↵").format(version=item.text()) if item is not None
            else tr("MainWindow", "Edit This Difficulty")
        )

    def open_selected_difficulty(self) -> None:
        item = self.difficulty_list.currentItem()
        if item is None:
            return
        path = Path(item.data(Qt.UserRole)).resolve()
        self.stop_preview()
        # _load_map_path reports its own failure, and moves to the Editor page
        # itself once the difficulty really loaded. From here, and only here,
        # the default views include a gameplay preview.
        self.window._gameplay_with_defaults = True
        try:
            self.window._load_map_path(path, refresh_difficulties=True)
        finally:
            self.window._gameplay_with_defaults = False
        if self.window.state.source_path == path:
            self.remember_recent(path)
