"""The Miscellaneous page (owner, 2026-10-18): set-wide chores that have no
view of their own -- the beatmapset's metadata, shifting the whole map's
offset, scaling every green line's SV, and converting a difficulty to
osu!mania (`chart.taiko_to_mania`); a difficulty at other rates, song and
all (`chart.rate_change`, `audio.rate_render`); and the Mappack Creator
(`library.mappack`), which gathers difficulties from other sets into a new one.

Everything here is an ordinary undoable edit on each difficulty's own history,
so the header's Save counts it and the Save dialog can still discard it.
"""
from __future__ import annotations

import copy
from html import escape
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QRectF, QSize, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from app.i18n import tr
from audio import rate_render
from audio.audio_engine import CHANNELS, SAMPLE_RATE
from chart import autoplay, rate_change, taiko_to_mania
from library import mappack, osu_memory
from model.commands import CompositeCommand, ReplaceDocumentLines, ShiftChartTimes
from osu_io import metadata
from osu_io.parser import parse_osu
from ui.common import equalize_button_widths
from ui.config_sheet import SEGMENT_STYLE, ConfigSheet, bind_segments, segmented, set_warning
from ui.song_select import BackgroundJobs, SongBanner, decode_background_image


def shifted_lines(lines: list[str], delta_ms: int, ending: str) -> list[str]:
    """`lines` with [Editor] Bookmarks, [General] PreviewTime and [Events]
    breaks and video moved by `delta_ms`. A PreviewTime of -1 means "none"
    and stays."""
    return rate_change.mapped_lines(lines, lambda time: time + delta_ms, ending)


def metadata_values(texts: dict[str, str]) -> dict[str, str]:
    """What Apply writes, from what the boxes hold.

    Ported from the owner's osu-metadata tool, with both of its rules: a blank
    box is *left alone* rather than written -- loading the form from a
    difficulty whose Source happened to be empty and pressing Apply otherwise
    blanked Source on every other difficulty -- and Tags are one space-separated
    run, whatever was pasted in (newlines, double spaces).
    """
    values = {}
    for key, text in texts.items():
        text = " ".join(text.split()) if key == "Tags" else text.strip()
        if text:
            values[key] = text
    return values


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class BackgroundWell(QLabel):
    """The pack's background: a thumbnail, and somewhere to drop an image.
    Dropping is never the only way in -- Select sits under it."""
    dropped = Signal(str)
    WIDTH = 224

    def __init__(self, drops: bool = True) -> None:
        super().__init__()
        self.setAcceptDrops(drops)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setFixedSize(self.WIDTH, 126)  # 16:9, the shape osu! shows it in
        self.setObjectName("fieldNote")
        # Dashed says "drop here", so only a well that takes drops draws it.
        self.setStyleSheet("QLabel { border: 1px %s #f3a6bd; border-radius: 6px; }"
                           % ("dashed" if drops else "solid"))
        self.show_image(None)

    def show_image(self, path: "Path | QImage | None", empty: str = "") -> None:
        if isinstance(path, QImage):
            pixmap = QPixmap.fromImage(path)
        else:
            pixmap = QPixmap(str(path)) if path else QPixmap()
        if pixmap.isNull():
            self.setPixmap(QPixmap())
            self.setText(empty or tr("MainWindow", "Drop an image here"))
        else:
            # Inside the 1px border, cropped to fill and clipped to its
            # rounded corner: scaled to the whole label, the image was drawn
            # over the border, which then showed on two sides only.
            size = self.size() - QSize(2, 2)
            scaled = pixmap.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            framed = QPixmap(size)
            framed.fill(Qt.transparent)
            painter = QPainter(framed)
            painter.setRenderHint(QPainter.Antialiasing)
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(0, 0, size.width(), size.height()), 5, 5)
            painter.setClipPath(clip)
            painter.drawPixmap((size.width() - scaled.width()) // 2, (size.height() - scaled.height()) // 2, scaled)
            painter.end()
            self.setPixmap(framed)

    def dragEnterEvent(self, event) -> None:
        if any(Path(u.toLocalFile()).suffix.lower() in IMAGE_SUFFIXES for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
                self.dropped.emit(str(path))
                event.acceptProposedAction()
                return


# The Rate Changer's pills (owner, 2026-10-23): a rate below 0.8 or above 1.5
# is typed instead.
PRESET_RATES = (0.8, 0.9, 1.1, 1.15, 1.2, 1.25, 1.3, 1.35, 1.4, 1.45, 1.5)

# The Rate Changer's table. Length is derived; every other cell is the row's
# own (owner, 2026-10-23), and the column widths are the user's to drag.
COL_RATE, COL_BPM, COL_LENGTH, COL_PITCH, COL_OD, COL_HP, COL_NAME, COL_REMOVE = range(8)
RATE_COLUMN_WIDTHS = {COL_RATE: 80, COL_BPM: 80, COL_LENGTH: 64, COL_PITCH: 100, COL_OD: 64, COL_HP: 64}
# The ceiling before a chart says what its BPM is: 1000 BPM is the real one.
DEFAULT_MAX_RATE = 100.0
# The banner is as wide as the page, so its art is decoded wider than the
# mappack's thumbnail.
RATE_ART_WIDTH = 960

# The typed rate and its + Add sit in a pill group of their own, reading as
# pills: the box as a filled one, + Add in the bright ink rather than the
# group's grey, since it is an action and not a choice.
RATE_INPUT_STYLE = """
QFrame#segment QDoubleSpinBox#rateInput {
    background: #2f3947; color: #e8edf3; border: 0; border-radius: 4px;
    padding: 0; font-size: 12px; font-weight: 600;
}
QFrame#segment QPushButton#rateAdd { color: #e8edf3; }
"""


@dataclass
class RateRow:
    """One difficulty the Rate Changer will write."""
    rate: float
    pitch: bool
    od: float
    hp: float
    name: str | None = None  # typed in the table; None is the name template's


# How often the Rate Changer asks osu! what it has selected, while it is on
# screen. The read is five 4-byte reads once attached; a second is quick
# enough to follow a click in song select and costs nothing between.
OSU_POLL_MS = 1000


def rate_background(document) -> QImage | None:
    """The map's background at thumbnail size. Decoded off the GUI thread
    where it can be: a 4K one at full size is a visible pause (song select,
    tools/measure_song_switch.py)."""
    name = mappack.background_name(document.lines)
    path = mappack.resolve_ci(document.source_path.parent, name) if name else None
    return decode_background_image(path, RATE_ART_WIDTH) if path is not None else None


def read_osu_selection(songs: Path, known):
    """Worker thread: (key, document, background) for the difficulty osu! has
    selected, the document None when the key is still `known`; None when
    osu! cannot say (not running, nothing selected, not under `songs`)."""
    try:
        folder, name = osu_memory.current_difficulty()
    except osu_memory.OsuNotFound:
        return None
    key = (folder, name)
    if key == known:
        return key, None, None
    path = mappack.resolve_ci(songs / folder, name)
    if path is None:
        return None
    document = parse_osu(path)
    return key, document, rate_background(document)


def _clock(ms: float) -> str:
    seconds = int(ms // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


COUNT_WARN_SHEET = 'QLabel[warn="true"] { color: #ff5c5c; }'


class TagsEdit(QPlainTextEdit):
    """Tall enough to show the whole 1000-character allowance at its width,
    so a full set of tags never scrolls out of sight. Red past the limit.
    Has QLineEdit's text()/setText(), so it stands in for one."""

    WARN_SHEET = 'QPlainTextEdit[warn="true"] { border: 1px solid #ff5c5c; color: #ff5c5c; }'

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(self.WARN_SHEET)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:
        self.setPlainText(text)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit_height()

    def fit_height(self) -> None:
        metrics = self.fontMetrics()
        per_line = max(1, self.viewport().width() // max(1, metrics.averageCharWidth()))
        # +1 line: words wrap whole, so lines are never quite full.
        lines = -(-metadata.TAGS_LIMIT // per_line) + 1
        height = lines * metrics.lineSpacing() + 2 * self.frameWidth() + 12
        if self.minimumHeight() != height:
            self.setMinimumHeight(height)


class OsuHotkey(QAbstractNativeEventFilter):
    """Alt+Shift+A from anywhere, osu! included: the old MapPackCreator's key.

    `RegisterHotKey` with no window posts WM_HOTKEY to this thread's queue,
    which Qt hands to native event filters before anything else sees it.
    Windows only, and never on the offscreen platform, where every test
    window would otherwise compete for the one system-wide key.
    """
    ID = 0x7A1
    WM_HOTKEY = 0x0312

    def __init__(self, callback) -> None:
        super().__init__()
        self.callback = callback
        self.registered = False
        if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
            return
        import ctypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        # MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, "A"
        self.registered = bool(self._user32.RegisterHotKey(None, self.ID, 0x1 | 0x4 | 0x4000, ord("A")))
        if self.registered:
            QGuiApplication.instance().installNativeEventFilter(self)

    def nativeEventFilter(self, event_type, message):
        if event_type != QByteArray(b"windows_generic_MSG"):
            return False, 0
        from ctypes import wintypes
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == self.WM_HOTKEY and msg.wParam == self.ID:
            self.callback()
            return True, 0
        return False, 0

    def release(self) -> None:
        if self.registered:
            self._user32.UnregisterHotKey(None, self.ID)
            app = QGuiApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self)
            self.registered = False


class MiscPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.sheet = ConfigSheet(rail=True, explain=True, paged=True)
        layout.addWidget(self.sheet)
        self.sheet.set_help_default(
            tr("MainWindow", "Miscellaneous"),
            tr("MainWindow", "Changes here are edits like any other: Save writes them, and Ctrl+Z undoes them."))

        meta = self.sheet.add_section("metadata", tr("MainWindow", "Metadata"))
        # One difficulty or all of them, as the osu-metadata tool offers.
        self.target_combo = QComboBox()
        self.target_combo.currentIndexChanged.connect(self._target_changed)
        meta.field(tr("MainWindow", "Apply to"), self.target_combo, span=2)
        labels = {
            "Title": tr("MainWindow", "Title"), "TitleUnicode": tr("MainWindow", "Title (Unicode)"),
            "Artist": tr("MainWindow", "Artist"), "ArtistUnicode": tr("MainWindow", "Artist (Unicode)"),
            "Creator": tr("MainWindow", "Creator"), "Source": tr("MainWindow", "Source"),
            "Tags": tr("MainWindow", "Tags"),
        }
        self.fields: dict[str, QLineEdit | TagsEdit] = {}
        for key in metadata.SET_KEYS:
            edit = TagsEdit() if key == "Tags" else QLineEdit()
            self.fields[key] = edit
            meta.field(labels[key], edit, tr("MainWindow", "Left blank, the field is not changed.")
                       if key != "Tags" else tr(
                           "MainWindow", "Separate tags with spaces, not commas: that is what a tag is "
                           "in the .osu file. Left blank, the tags are not changed."),
                       span=2 if key == "Tags" else 1)
        self.tags_count = QLabel()
        self.tags_count.setStyleSheet(COUNT_WARN_SHEET)
        meta.add(self.tags_count, span=1)
        self.apply_metadata_button = QPushButton(tr("MainWindow", "Apply"))
        self.apply_metadata_button.clicked.connect(self.apply_metadata)
        meta.add(self.apply_metadata_button, span=1)
        self.fields["Tags"].textChanged.connect(self._count_tags)

        timing = self.sheet.add_section("offset", tr("MainWindow", "Offset"),
                                        tr("MainWindow", "Every difficulty in the set"))
        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(-100000, 100000)
        self.offset_spin.setSuffix(" ms")
        timing.field(tr("MainWindow", "Shift by"), self.offset_spin, tr(
            "MainWindow", "Moves every note, timing line, bookmark and the preview point. "
            "Positive is later."))
        self.shift_button = QPushButton(tr("MainWindow", "Shift the whole map"))
        self.shift_button.clicked.connect(self.shift_offset)
        timing.add(self.shift_button, span=1)

        sv = self.sheet.add_section("sv", tr("MainWindow", "SV"), tr("MainWindow", "The difficulty being edited"))
        self.scale_all_button = QPushButton(tr("MainWindow", "Scale every green line…"))
        self.scale_all_button.clicked.connect(self.scale_all_sv)
        sv.add(self.scale_all_button, span=1)

        mania = self.sheet.add_section("mania", tr("MainWindow", "Taiko to Mania"),
                                       tr("MainWindow", "The difficulty being edited"))
        self.mania_keys = QComboBox()
        self.mania_keys.addItem(tr("MainWindow", "4 Keys"), 4)
        self.mania_keys.addItem(tr("MainWindow", "1 Key"), 1)
        self.mania_keys.currentIndexChanged.connect(self._mania_keys_changed)
        mania.field(tr("MainWindow", "Keys"), self.mania_keys, tr(
            "MainWindow", "4K puts each note in the column of the drum half the autoplay hits. "
            "1K puts every note in one column, with no colours."))
        self.mania_style = QComboBox()
        for value, label in (
                (autoplay.FULL_ALT, tr("MainWindow", "kddk full alternate")),
                (autoplay.SEMI_ALT, tr("MainWindow", "kddk semi alternate")),
                (autoplay.SINGLE_TAP, tr("MainWindow", "kddk single tap")),
                (autoplay.ROLL, tr("MainWindow", "kddk roll")),
                (autoplay.DDKK, "ddkk")):
            self.mania_style.addItem(label, value)
        mania.field(tr("MainWindow", "Playstyle"), self.mania_style)
        self.mania_binding = QComboBox()
        for binding in autoplay.BINDINGS:
            self.mania_binding.addItem(binding, binding)
        mania.field(tr("MainWindow", "Binding (kddk)"), self.mania_binding)
        self.mania_left = QCheckBox(tr("MainWindow", "Left-handed"))
        mania.add(self.mania_left, span=1)
        self.mania_bpm = QDoubleSpinBox()
        self.mania_bpm.setRange(1.0, 10000.0)
        self.mania_bpm.setDecimals(2)
        self.mania_bpm.setSuffix(" BPM")
        mania.field(tr("MainWindow", "Single tap BPM"), self.mania_bpm, tr(
            "MainWindow", "The beat the playstyle groups notes against: notes half a beat or "
            "more apart are a rest, and a group after a rest starts on the dominant hand. "
            "Starts at the chart's BPM."))
        self.mania_finisher_single = QCheckBox(tr("MainWindow", "Hit finishers as single note"))
        mania.field(tr("MainWindow", "Finishers"), self.mania_finisher_single, tr(
            "MainWindow", "On, a finisher is one note in the hand's column. Off, it is two, "
            "on both halves of its colour."))
        self.mania_skin_sounds = QCheckBox(tr("MainWindow", "Include hitsounds from the current skin"))
        self.mania_skin_sounds.setChecked(True)
        mania.field(tr("MainWindow", "Hitsounds"), self.mania_skin_sounds, tr(
            "MainWindow", "On, every note plays the current skin's taiko sound, copied into the set. "
            "Off, the set's own taiko samples are used where it has them, and other notes keep "
            "their whistle, clap and finish for the player's skin."), span=2)
        self.mania_button = QPushButton(tr("MainWindow", "Convert to a new difficulty"))
        self.mania_button.clicked.connect(lambda: self.convert_to_mania())
        mania.add(self.mania_button, span=1)
        self.mania_osu = QPushButton(tr("MainWindow", "Convert osu!'s current difficulty"))
        self.mania_osu.setToolTip(tr("MainWindow", "The difficulty selected in osu!, without opening "
                                     "it here. Its single tap BPM is its own chart BPM."))
        self.mania_osu.clicked.connect(self.convert_osu_to_mania)
        self.mania_osu.setVisible(sys.platform == "win32")
        mania.add(self.mania_osu, span=1)
        self._build_rates()
        self._build_mappack()
        self.refresh()

    # -- state ----------------------------------------------------------------

    def _paths(self) -> list[Path]:
        return list(getattr(self.window, "song_difficulties", []) or [])

    def _states(self):
        """Every difficulty of the set, parsed on demand like the dropdown does."""
        states = []
        for path in self._paths():
            try:
                states.append(self.window._ensure_state(path))
            except (OSError, ValueError):
                continue
        return states

    def refresh(self) -> None:
        state = getattr(self.window, "state", None)
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItem(tr("MainWindow", "All difficulties"), None)
        # The window's own difficulty list, names and all: parsing every file
        # again for its Version is the cost the dropdown already paid.
        combo = getattr(self.window, "difficulty_combo", None)
        for index in range(combo.count() if combo is not None else 0):
            self.target_combo.addItem(combo.itemText(index), combo.itemData(index))
        current = state.source_path if state is not None else None
        self.target_combo.blockSignals(False)
        for widget in (self.apply_metadata_button, self.shift_button, self.scale_all_button,
                       self.mania_button):
            widget.setEnabled(state is not None)
        self._load_mania_defaults(state)
        self._set_rate_source()
        self._load_fields(current)

    def _load_mania_defaults(self, state) -> None:
        """The autoplay's own choice, Off read as full alt, and the chart BPM."""
        style, binding, left = getattr(self.window, "_autoplay_choice", (None, autoplay.BINDINGS[0], False))
        self.mania_style.setCurrentIndex(max(0, self.mania_style.findData(style or autoplay.FULL_ALT)))
        self.mania_binding.setCurrentIndex(max(0, self.mania_binding.findData(binding)))
        self.mania_left.setChecked(bool(left))
        if state is not None:
            self.mania_bpm.setValue(60000.0 / taiko_to_mania.chart_beat_length(state.document))
        self._mania_keys_changed()

    def _mania_keys_changed(self) -> None:
        four = self.mania_keys.currentData() == 4
        for widget in (self.mania_style, self.mania_binding, self.mania_left, self.mania_bpm,
                       self.mania_finisher_single):
            widget.setEnabled(four)

    def _target_changed(self) -> None:
        data = self.target_combo.currentData()
        state = getattr(self.window, "state", None)
        # "All" reads the open difficulty, as the osu-metadata tool reads the first.
        self._load_fields(Path(data) if data else (state.source_path if state is not None else None))

    def _load_fields(self, path: Path | None) -> None:
        values = {}
        if path is not None:
            try:
                values = metadata.read_keys(self.window._ensure_state(path).document.lines, "Metadata")
            except (OSError, ValueError):
                values = {}
        for key, edit in self.fields.items():
            edit.blockSignals(True)
            edit.setText(values.get(key, ""))
            edit.blockSignals(False)
        self._count_tags()

    def _count_tags(self) -> None:
        # Counted as it will be written: normalised, as osu! will see it.
        count = len(" ".join(self.fields["Tags"].text().split()))
        over = count > metadata.TAGS_LIMIT
        self.tags_count.setText(tr("MainWindow", "{0} / {1} characters").format(count, metadata.TAGS_LIMIT))
        set_warning(self.tags_count, over)
        set_warning(self.fields["Tags"], over)
        self.apply_metadata_button.setEnabled(not over and getattr(self.window, "state", None) is not None)

    def _done(self, paths, message: str) -> None:
        with self.window._refresh_cycle():
            for path in paths:
                self.window._refresh_difficulty_views(path)
                self.window._refresh_difficulty_sv_views(path)
        state = getattr(self.window, "state", None)
        if state is not None and state.source_path in paths:
            self.window._show_chart_identity(state)
        self.window.status.setText(message)

    # -- actions --------------------------------------------------------------

    def apply_metadata(self) -> None:
        values = metadata_values({key: edit.text() for key, edit in self.fields.items()})
        if len(values.get("Tags", "")) > metadata.TAGS_LIMIT:
            return
        if not values:
            self.window.show_toast(tr("MainWindow", "Nothing typed, nothing to apply."))
            return
        target = self.target_combo.currentData()
        states = self._states() if not target else [self.window._ensure_state(Path(target))]
        changed = []
        for state in states:
            document = state.document
            new = metadata.with_keys(document.lines, "Metadata", values, document.line_ending)
            if new != document.lines:
                state.history.push(ReplaceDocumentLines(list(document.lines), new), state)
                changed.append(state.source_path)
        self._done(changed, tr("MainWindow", "Metadata written to {0} difficulties.").format(len(changed)))

    def shift_offset(self) -> None:
        delta = int(self.offset_spin.value())
        if delta == 0:
            return
        paths = []
        for state in self._states():
            document = state.document
            commands = [ShiftChartTimes(delta)]
            lines = shifted_lines(document.lines, delta, document.line_ending)
            if lines != document.lines:
                commands.append(ReplaceDocumentLines(list(document.lines), lines))
            state.history.push(CompositeCommand(commands, "shift_offset"), state)
            paths.append(state.source_path)
        self._done(paths, tr("MainWindow", "Shifted {0} difficulties by {1} ms.").format(len(paths), delta))

    def scale_all_sv(self) -> None:
        state = getattr(self.window, "state", None)
        if state is None:
            return
        greens = [point for point in state.document.timing_points if not point.uninherited]
        if not greens:
            self.window.show_toast(tr("MainWindow", "This difficulty has no green lines."))
            return
        self.window.scale_sv_points(state.source_path, greens)

    def convert_osu_to_mania(self) -> None:
        path = self._osu_current_path()
        if path is None:
            return
        try:
            document = parse_osu(path)
        except (OSError, ValueError) as error:
            self.window.show_toast(tr("MainWindow", "Could not read that difficulty: {0}").format(error))
            return
        self.convert_to_mania(document)

    def convert_to_mania(self, document=None) -> None:
        """The open chart, or `document` read from osu! -- at its own chart
        BPM, since the BPM box holds the open chart's."""
        single_tap_bpm = None
        if document is None:
            state = getattr(self.window, "state", None)
            if state is None:
                return
            document = state.document
            single_tap_bpm = self.mania_bpm.value()
        hitsounds = getattr(self.window, "hitsounds", None)
        try:
            samples = taiko_to_mania.place_samples(
                document.source_path.parent,
                {key: Path(path) for key, path in (hitsounds.sample_paths if hitsounds else {}).items()},
                self.mania_skin_sounds.isChecked())
            path = taiko_to_mania.convert(
                document, keys=int(self.mania_keys.currentData()),
                style=self.mania_style.currentData(), binding=self.mania_binding.currentData(),
                left_handed=self.mania_left.isChecked(), single_tap_bpm=single_tap_bpm,
                finisher_single=self.mania_finisher_single.isChecked(), samples=samples)
        except (OSError, ValueError) as error:
            # FileExistsError is an OSError: converting twice with the same
            # options would overwrite the first, and that is refused.
            self.window.show_toast(tr("MainWindow", "Not converted: {0}").format(error))
            return
        self.window.status.setText(tr("MainWindow", "Wrote {0}. Press F5 in osu! to see it.").format(path.name))

    # -- rate changer ---------------------------------------------------------

    def _build_rates(self) -> None:
        # The pool is made on the first conversion, not here: one per window
        # would start worker processes in every test that builds a window.
        self._rate_pool: ProcessPoolExecutor | None = None
        self.rate_jobs: BackgroundJobs | None = None
        self._rate_run: dict | None = None
        self.rate_rows: list[RateRow] = []
        # The source chart's own numbers: what a new row starts from.
        self._rate_chart = (0.0, 0, 5.0, 5.0)  # BPM, length ms, OD, HP
        # The difficulty osu! has selected, while it has one (owner,
        # 2026-10-23); otherwise the one being edited.
        self._od_typing = False
        self._osu_document = None
        self._osu_background = None
        self._osu_key = None
        self._osu_jobs: BackgroundJobs | None = None
        self._osu_polling = False
        self._rate_document = None
        self._osu_timer = QTimer(self)
        self._osu_timer.setInterval(OSU_POLL_MS)
        self._osu_timer.timeout.connect(self._poll_osu)
        # Never on the offscreen platform: a test window would load whatever
        # osu! has selected on the machine running the suite.
        if sys.platform == "win32" and QGuiApplication.platformName() == "windows":
            self._osu_timer.start()
        rates = self.sheet.add_section("rate", tr("MainWindow", "Rate Changer"),
                                       tr("MainWindow", "osu!'s selected difficulty, or else the one being edited"))
        # Song select's banner without its seek band (owner, 2026-10-23): the
        # same art, title and font, and nothing here plays the song.
        self.rate_banner = SongBanner(show_band=False)
        self.rate_banner.setFixedHeight(SongBanner.MIN_HEIGHT)
        self.rate_original = QLabel()
        self.rate_original.setTextFormat(Qt.RichText)
        original = QWidget()
        original_column = QVBoxLayout(original)
        original_column.setContentsMargins(0, 0, 0, 0)
        original_column.setSpacing(6)
        original_column.addWidget(self.rate_banner)
        original_column.addWidget(self.rate_original)
        rates.field(tr("MainWindow", "Original"), original, span=2)

        # One pill per rate; a rate typed below joins as a pill of its own.
        # A pill is on while some row is at its rate, so the table's own
        # edits and its remove button move the pills too (`_sync_rate_pills`).
        self.rate_buttons: dict[float, QPushButton] = {}
        self.rate_presets = segmented([self._rate_pill(rate) for rate in PRESET_RATES])
        self.rate_customs = segmented([])
        self.rate_customs.setVisible(False)
        self.rate_custom = QDoubleSpinBox()
        self.rate_custom.setObjectName("rateInput")
        self.rate_custom.setRange(rate_change.MIN_RATE, DEFAULT_MAX_RATE)
        self.rate_custom.setDecimals(2)
        self.rate_custom.setSingleStep(0.05)
        self.rate_custom.setValue(1.05)
        self.rate_custom.setSuffix("x")
        self.rate_add = QPushButton(tr("MainWindow", "+ Add"))
        self.rate_add.setObjectName("rateAdd")
        self.rate_add.clicked.connect(lambda: self._add_custom_rate(self.rate_custom.value()))
        self.rate_custom.lineEdit().returnPressed.connect(self.rate_add.click)
        manual = segmented([self.rate_add])
        manual.layout().insertWidget(0, self.rate_custom)
        # A BPM to reach, for "320 from a 269 BPM map": the rate is worked
        # out here rather than by the mapper with a calculator.
        self.rate_bpm = QDoubleSpinBox()
        self.rate_bpm.setObjectName("rateInput")
        self.rate_bpm.setRange(0.1, rate_change.MAX_BPM)
        self.rate_bpm.setDecimals(1)
        self.rate_bpm.setValue(200.0)
        self.rate_bpm.setSuffix(" BPM")
        self.rate_add_bpm = QPushButton(tr("MainWindow", "+ Add"))
        self.rate_add_bpm.setObjectName("rateAdd")
        self.rate_add_bpm.clicked.connect(self._add_bpm_rate)
        self.rate_bpm.lineEdit().returnPressed.connect(self.rate_add_bpm.click)
        by_bpm = segmented([self.rate_add_bpm])
        by_bpm.layout().insertWidget(0, self.rate_bpm)
        for spin in (self.rate_custom, self.rate_bpm):
            spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
            spin.setAlignment(Qt.AlignCenter)
            spin.setKeyboardTracking(False)
        for frame in (self.rate_presets, self.rate_customs, manual, by_bpm):
            # The source, never styleSheet(): that is the active theme's
            # translation, and appending to it pins these to that theme.
            frame.setStyleSheet(SEGMENT_STYLE + RATE_INPUT_STYLE)
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for frame in (self.rate_presets, self.rate_customs, manual, by_bpm):
            layout.addWidget(frame)
        layout.addStretch(1)
        rates.field(tr("MainWindow", "Rates"), row, tr(
            "MainWindow", "Pick as many as you like, or type a rate or the BPM you want and add it, up to "
            "1000 BPM. Each becomes a difficulty of its own, with its own audio file, timed as the rate "
            "mod would play it."), span=2)

        self.rate_pitch = QComboBox()
        self.rate_pitch.addItem(tr("MainWindow", "Keep"), False)
        self.rate_pitch.addItem(tr("MainWindow", "Change"), True)
        self.rate_pitch.currentIndexChanged.connect(self._pitch_for_every_row)
        rates.field(tr("MainWindow", "Pitch"), bind_segments(self.rate_pitch), tr(
            "MainWindow", "Keep plays the song as Double Time does; Change plays it higher as it gets "
            "faster, as Nightcore does. Sets every rate; the table can change one."))
        self.rate_od_mode = QComboBox()
        self.rate_od_mode.addItem(tr("MainWindow", "Manual"), False)
        self.rate_od_mode.addItem(tr("MainWindow", "Scale"), True)
        self.rate_od_mode.currentIndexChanged.connect(self._od_mode_changed)
        rates.field(tr("MainWindow", "OD"), bind_segments(self.rate_od_mode), tr(
            "MainWindow", "Manual: each rate's OD is what its row says. Scale: each row gets the OD that "
            "makes its timing windows as strict as the rate mod would, up to OD 10."))
        self.rate_format = QComboBox()
        self.rate_format.addItem("MP3", ".mp3")
        self.rate_format.addItem("OGG", ".ogg")
        self.rate_format.setCurrentIndex(max(0, self.rate_format.findData(
            self.window.settings.string_value("rates/audio_format", ".mp3"))))
        self.rate_format.currentIndexChanged.connect(self._format_changed)
        rates.field(tr("MainWindow", "Audio"), bind_segments(self.rate_format), tr(
            "MainWindow", "The format each rate's song is written in. Both land on the same millisecond in "
            "osu!; MP3 is what osu-trainer writes. Needs ffmpeg, without which it is WAV."))

        # The name as a template (owner, 2026-10-23): "Distracting at 300BPM",
        # "... (1.2x)" and "Distracting at 1.2x" are all this box and its chips.
        self.rate_name_template = QLineEdit(self.window.settings.string_value(
            "rates/name_template", rate_change.DEFAULT_NAME_TEMPLATE))
        self.rate_name_template.setPlaceholderText(rate_change.DEFAULT_NAME_TEMPLATE)
        self.rate_name_template.textChanged.connect(self._name_template_changed)
        chips = []
        for label, token in ((tr("MainWindow", "Name"), "{name}"), (tr("MainWindow", "Rate"), "{rate}"),
                             (tr("MainWindow", "BPM"), "{bpm}")):
            chip = QPushButton(label)
            chip.clicked.connect(partial(self._insert_name_token, token))
            chips.append(chip)
        naming = QWidget()
        naming_row = QHBoxLayout(naming)
        naming_row.setContentsMargins(0, 0, 0, 0)
        naming_row.setSpacing(8)
        naming_row.addWidget(self.rate_name_template, 1)
        naming_row.addWidget(segmented(chips))
        rates.field(tr("MainWindow", "Name"), naming, tr(
            "MainWindow", "Each row's difficulty name: {name} is the original's, {rate} and {bpm} the row's. "
            "A name typed into the table wins over this."), span=2)

        columns = [tr("MainWindow", "Rate"), tr("MainWindow", "BPM"), tr("MainWindow", "Length"),
                   tr("MainWindow", "Change pitch"), tr("MainWindow", "OD"), tr("MainWindow", "HP"),
                   tr("MainWindow", "Difficulty name"), ""]
        self.rate_table = QTableWidget(0, len(columns))
        self.rate_table.setHorizontalHeaderLabels(columns)
        header = self.rate_table.horizontalHeader()
        # Dragged wider or narrower by the user (owner, 2026-10-23); the name
        # takes what is left, and the remove button keeps its own size.
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_REMOVE, QHeaderView.Fixed)
        for column, width in RATE_COLUMN_WIDTHS.items():
            header.resizeSection(column, max(width, header.sectionSizeHint(column)))
        header.resizeSection(COL_REMOVE, 34)
        self.rate_table.verticalHeader().setVisible(False)
        self.rate_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.rate_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.rate_table.setFocusPolicy(Qt.NoFocus)
        rates.field(tr("MainWindow", "Difficulties"), self.rate_table, tr(
            "MainWindow", "One row per difficulty. Its rate, BPM, pitch, OD, HP and name are its own: change "
            "the rate or the BPM and the other follows."), span=2)

        actions = QWidget()
        actions_row = QHBoxLayout(actions)
        actions_row.setContentsMargins(0, 0, 0, 0)
        actions_row.setSpacing(12)
        self.rate_button = QPushButton(tr("MainWindow", "Create Map(s)"))
        self.rate_button.clicked.connect(self.convert_rates)
        self.rate_note = QLabel()
        self.rate_note.setObjectName("fieldNote")
        self.rate_note.setWordWrap(True)
        actions_row.addWidget(self.rate_button)
        actions_row.addWidget(self.rate_note, 1)
        rates.add(actions, span=2)
        self.rate_progress = QProgressBar()
        self.rate_progress.setTextVisible(True)
        self.rate_progress.setVisible(False)
        rates.add(self.rate_progress, span=2)
        self._fill_rate_table()

    def _rate_pill(self, rate: float) -> QPushButton:
        button = QPushButton(f"{rate_change.rate_text(rate)}x")
        button.setCheckable(True)
        button.toggled.connect(lambda on, rate=rate: self._rate_toggled(rate, on))
        self.rate_buttons[rate] = button
        return button

    def _fit_rate_pills(self) -> None:
        """Every pill, the typed rate and + Add the same size: measured once
        the sheet has given them their padding (see `fit_buttons`)."""
        widgets = [*self.rate_buttons.values(), self.rate_add, self.rate_add_bpm]
        width = max(button.sizeHint().width() for button in widgets)
        width = max(width, self.rate_custom.fontMetrics().horizontalAdvance("0.55x") + 20)
        height = max(button.sizeHint().height() for button in widgets)
        for widget in (*widgets, self.rate_custom):
            widget.setFixedSize(width, height)
        self.rate_bpm.setFixedSize(self.rate_bpm.fontMetrics().horizontalAdvance("1000.0 BPM") + 20, height)

    def _poll_osu(self) -> None:
        # Only while the section is on screen, and one read at a time.
        if self._osu_polling or not self.sheet.sections["rate"].isVisible():
            return
        songs = self.window.settings.string_value("library/songs_folder", "")
        if not songs:
            return
        if self._osu_jobs is None:
            # A thread, not the GUI: attaching scans osu!'s memory (11ms
            # measured, up to a second when the code sits late in it), and
            # finding osu! is not running costs ~4ms a poll. Once attached a
            # poll is 0.024ms (2026-10-23).
            self._osu_jobs = BackgroundJobs(self)
        self._osu_polling = True
        self._osu_jobs.submit(partial(read_osu_selection, Path(songs), self._osu_key),
                              self._osu_read, lambda _error: self._osu_read(None))

    def _osu_read(self, result) -> None:
        self._osu_polling = False
        if result is not None and result[1] is None:
            return  # the same difficulty as last time
        had = self._osu_document is not None
        self._osu_key, self._osu_document, self._osu_background = result if result is not None else (None,) * 3
        if had or self._osu_document is not None:
            self._set_rate_source()

    def _set_rate_source(self) -> None:
        state = getattr(self.window, "state", None)
        self._rate_document = self._osu_document or (state.document if state is not None else None)
        # The edited chart's is decoded here: it changes once per opened map.
        image = (self._osu_background if self._osu_document is not None
                 else rate_background(self._rate_document) if self._rate_document is not None else None)
        self.rate_banner.set_art(QPixmap.fromImage(image) if image is not None else None)
        self._load_rate_chart(self._rate_document)

    def _load_rate_chart(self, document) -> None:
        if document is None:
            self._rate_chart = (0.0, 0, 5.0, 5.0)
        else:
            self._rate_chart = (60000.0 / taiko_to_mania.chart_beat_length(document), mappack.length_ms(document),
                                rate_change.difficulty_setting(document.lines, "OverallDifficulty"),
                                rate_change.difficulty_setting(document.lines, "HPDrainRate"))
        self.rate_custom.setMaximum(self._max_rate())
        # A new chart: every row starts again from its numbers.
        for row in self.rate_rows:
            row.rate = min(row.rate, self._max_rate())
            row.od, row.hp = self._row_od(row.rate), self._rate_chart[3]
        self._fill_rate_table()

    def _max_rate(self) -> float:
        bpm = self._rate_chart[0]
        return round(rate_change.MAX_BPM / bpm, rate_change.RATE_DECIMALS) if bpm > 0 else DEFAULT_MAX_RATE

    def _row_od(self, rate: float) -> float:
        od = self._rate_chart[2]
        return rate_change.dt_od(od, rate) if self.rate_od_mode.currentData() else od

    def _rate_toggled(self, rate: float, on: bool) -> None:
        if on:
            if all(row.rate != rate for row in self.rate_rows):
                self.rate_rows.append(RateRow(rate, bool(self.rate_pitch.currentData()), self._row_od(rate),
                                              self._rate_chart[3]))
                self.rate_rows.sort(key=lambda row: row.rate)
        else:
            self.rate_rows = [row for row in self.rate_rows if row.rate != rate]
        self._fill_rate_table()

    def _sync_rate_pills(self) -> None:
        rates = {row.rate for row in self.rate_rows}
        for rate, button in self.rate_buttons.items():
            # The pill's own signal only: it would add or remove a row again.
            button.blockSignals(True)
            button.setChecked(rate in rates)
            button.blockSignals(False)

    def _add_custom_rate(self, rate: float) -> None:
        # Rounded once, here: a rate is the key of its pill, its row and its
        # audio file, and 320/269 has no end. 4 places is 0.03 BPM at 269.
        rate = round(min(rate, self._max_rate()), rate_change.RATE_DECIMALS)
        if rate == 1.0 or rate < rate_change.MIN_RATE:
            return
        if rate not in self.rate_buttons:
            pill = self._rate_pill(rate)
            pill.setFixedSize(self.rate_add.size())
            self.rate_customs.layout().addWidget(pill)
            self.rate_customs.setVisible(True)
        self.rate_buttons[rate].setChecked(True)

    def _add_bpm_rate(self) -> None:
        bpm = self._rate_chart[0]
        if bpm > 0:
            self._add_custom_rate(self.rate_bpm.value() / bpm)

    def _pitch_for_every_row(self) -> None:
        for row in self.rate_rows:
            row.pitch = bool(self.rate_pitch.currentData())
        self._fill_rate_table()

    def _format_changed(self) -> None:
        self.window.settings.set_value("rates/audio_format", self.rate_format.currentData())
        self._fill_rate_table()

    def _od_mode_changed(self) -> None:
        # Both ways: Manual is the chart's own OD again, not the scaled one
        # left behind (owner, 2026-10-23) -- except when a typed OD is what
        # switched it, which must neither be undone nor have its box rebuilt
        # under the cursor.
        if self._od_typing:
            return
        for row in self.rate_rows:
            row.od = self._row_od(row.rate)
        self._fill_rate_table()

    def _row_od_typed(self, row: "RateRow", value: float) -> None:
        row.od = value
        # A typed OD is not the scaled one any more, and Scale would say it was.
        # Not blockSignals: the pills follow the combo by that same signal,
        # and blocked they went on showing Scale, so the next click on Manual
        # changed nothing and reverted nothing.
        if self.rate_od_mode.currentData():
            self._od_typing = True
            self.rate_od_mode.setCurrentIndex(0)
            self._od_typing = False

    def _row_pitch_toggled(self, row: "RateRow", on: bool) -> None:
        row.pitch = on
        self._fill_rate_table()

    def _row_rate_typed(self, row: "RateRow", rate: float) -> None:
        row.rate = round(rate, rate_change.RATE_DECIMALS)
        if self.rate_od_mode.currentData():
            row.od = self._row_od(row.rate)
        # The box is committed by now (keyboard tracking is off), so the table
        # is rebuilt from the rows: BPM, length, OD and name all follow.
        self._fill_rate_table()

    def _row_bpm_typed(self, row: "RateRow", bpm: float) -> None:
        if self._rate_chart[0] > 0:
            self._row_rate_typed(row, bpm / self._rate_chart[0])

    def _row_name_typed(self, row: "RateRow", text: str) -> None:
        # Empty is "the template's again", not a difficulty with no name.
        row.name = text.strip() or None

    def _remove_rate_row(self, row: "RateRow") -> None:
        self.rate_rows = [other for other in self.rate_rows if other is not row]
        self._fill_rate_table()

    def _name_template(self) -> str:
        return self.rate_name_template.text().strip() or rate_change.DEFAULT_NAME_TEMPLATE

    def _name_template_changed(self) -> None:
        self.window.settings.set_value("rates/name_template", self.rate_name_template.text())
        document = self._rate_document
        if document is None:
            return
        # The placeholders only: rebuilt, the table would take the focus out
        # of the box being typed in.
        for index, row in enumerate(self.rate_rows):
            edit = self.rate_table.cellWidget(index, COL_NAME)
            if edit is not None:
                edit.setPlaceholderText(self._row_version(document, replace(row, name=None)))

    def _insert_name_token(self, token: str) -> None:
        self.rate_name_template.insert(token)
        self.rate_name_template.setFocus()

    def _row_version(self, document, row: "RateRow") -> str:
        return row.name or rate_change.version_name(document, row.rate, self._name_template())

    def _fill_rate_table(self) -> None:
        bpm, length, od, hp = self._rate_chart
        document = self._rate_document
        self._sync_rate_pills()
        self.rate_banner.setVisible(document is not None)
        if document is None:
            self.rate_original.setText(tr("MainWindow", "Select a map in osu!, or open one here."))
        else:
            values = metadata.read_keys(document.lines, "Metadata")
            where = (tr("MainWindow", "selected in osu!") if document is self._osu_document
                     else tr("MainWindow", "being edited"))
            creator = values.get("Creator", "")
            self.rate_banner.set_text(values.get("Title", ""), " · ".join(part for part in (
                values.get("Artist", ""),
                tr("MainWindow", "mapped by {name}").format(name=creator) if creator else "") if part))
            # Under the banner: which difficulty, then the numbers every row
            # starts from.
            self.rate_original.setText("  ·  ".join((
                f"<b>[{escape(values.get('Version', document.version))}]</b>",
                tr("MainWindow", "{0:.0f} BPM  ·  {1}  ·  OD {2:g}  ·  HP {3:g}").format(bpm, _clock(length), od, hp),
                f"<i>{where}</i>")))
        table = self.rate_table
        table.setRowCount(len(self.rate_rows))
        for index, row in enumerate(self.rate_rows):
            for column, value, low, high, decimals, typed in (
                    (COL_RATE, row.rate, rate_change.MIN_RATE, self._max_rate(), rate_change.RATE_DECIMALS,
                     self._row_rate_typed),
                    (COL_BPM, bpm * row.rate, 0.1, rate_change.MAX_BPM, 1, self._row_bpm_typed)):
                spin = QDoubleSpinBox()
                spin.setRange(low, high)
                spin.setDecimals(decimals)
                spin.setValue(value)
                spin.setFrame(False)
                spin.setAlignment(Qt.AlignCenter)
                spin.setButtonSymbols(QDoubleSpinBox.NoButtons)
                # Committed on Enter or on leaving the box, not per keystroke:
                # "3" on the way to "320" is a 3 BPM row.
                spin.setKeyboardTracking(False)
                spin.setEnabled(bpm > 0 or column == COL_RATE)
                spin.valueChanged.connect(partial(typed, row))
                table.setCellWidget(index, column, spin)
            item = QTableWidgetItem(_clock(length / row.rate))
            item.setTextAlignment(Qt.AlignCenter)
            table.setItem(index, COL_LENGTH, item)
            pitch = QCheckBox()
            pitch.setChecked(row.pitch)
            pitch.toggled.connect(partial(self._row_pitch_toggled, row))
            holder = QWidget()
            centre = QHBoxLayout(holder)
            centre.setContentsMargins(0, 0, 0, 0)
            centre.addWidget(pitch, 0, Qt.AlignCenter)
            table.setCellWidget(index, COL_PITCH, holder)
            for column, value, typed in ((COL_OD, row.od, self._row_od_typed), (COL_HP, row.hp, None)):
                spin = QDoubleSpinBox()
                spin.setRange(0.0, 10.0)
                spin.setDecimals(1)
                spin.setSingleStep(0.1)
                spin.setValue(value)
                spin.setFrame(False)
                spin.valueChanged.connect(partial(typed, row) if typed else partial(setattr, row, "hp"))
                table.setCellWidget(index, column, spin)
            name = QLineEdit(row.name or "")
            name.setFrame(False)
            # The template's name shows through until one is typed over it.
            name.setPlaceholderText(self._row_version(document, replace(row, name=None))
                                    if document is not None else "")
            name.textEdited.connect(partial(self._row_name_typed, row))
            table.setCellWidget(index, COL_NAME, name)
            remove = QPushButton("✕")
            remove.setFlat(True)
            remove.setToolTip(tr("MainWindow", "Remove this difficulty"))
            remove.clicked.connect(partial(self._remove_rate_row, row))
            table.setCellWidget(index, COL_REMOVE, remove)
            table.setRowHeight(index, 34)
        # The table is as tall as its rows: no scrolling inside a scrolling page.
        table.setFixedHeight(table.horizontalHeader().height() + 34 * max(1, len(self.rate_rows)) + 4)
        count = len(self.rate_rows)
        self.rate_button.setEnabled(document is not None and count > 0 and self._rate_run is None)
        notes = [tr("MainWindow", "Audio is written as {0}.").format(self.rate_format.currentText())
                 if shutil.which("ffmpeg")
                 else tr("MainWindow", "Audio is written as WAV: install ffmpeg for MP3 or OGG, a fifth the size.")]
        if document is not None:
            reused = [row for row in self.rate_rows if self._existing_audio(document, row) is not None]
            if reused:
                notes.append(tr("MainWindow", "Already rendered, reused: {0}").format(
                    ", ".join(f"{rate_change.rate_text(row.rate)}x" for row in reused)))
        self.rate_note.setText("  ·  ".join(notes))

    def _existing_audio(self, document, row: "RateRow") -> str | None:
        stem = rate_change.audio_name(document.audio_filename, row.rate, row.pitch, "")
        folder = document.source_path.parent
        # The chosen format only: picking OGG and getting an old MP3 back
        # would ignore the choice. WAV is what either becomes without ffmpeg.
        return next((stem + ext for ext in (self.rate_format.currentData(), ".wav")
                     if (folder / (stem + ext)).exists()), None)

    def convert_rates(self) -> None:
        """Decode the song once, render each (rate, pitch) missing its audio in
        a worker process, then write every row as its own difficulty. Audio
        already in the folder (another difficulty converted earlier) is reused."""
        document = self._rate_document
        if self._rate_run is not None or document is None or not self.rate_rows:
            return
        # Each row's name fixed now, from the template as it stands.
        rows = [replace(row, name=self._row_version(document, row)) for row in self.rate_rows]
        # Refused before the render rather than after it, as mania refuses:
        # one name twice in a batch, or the original's own, would otherwise
        # be found out a minute in, by the write.
        # Folded, because the file names are, on Windows.
        own = metadata.read_keys(document.lines, "Metadata").get("Version", document.version).casefold()
        names = [row.name.casefold() for row in rows]
        clash = next((rows[index].name for index, name in enumerate(names)
                      if name in names[:index] or name == own), None)
        if clash is not None:
            self.window.show_toast(tr("MainWindow", "Not converted: more than one difficulty would be "
                                      "called {0}.").format(clash))
            return
        taken = [rate_change.destination(document, row.rate, row.name) for row in rows]
        taken = [path for path in taken if path.exists()]
        if taken:
            self.window.show_toast(tr("MainWindow", "Not converted: {0} already exists.").format(taken[0].name))
            return
        source = mappack.resolve_ci(document.source_path.parent, document.audio_filename)
        if source is None:
            self.window.show_toast(tr("MainWindow", "This difficulty's audio file is missing."))
            return
        audio = {(row.rate, row.pitch): self._existing_audio(document, row) for row in rows}
        suffix = self.rate_format.currentData()
        # A snapshot: the chart may be edited while the song renders, and what
        # is written is the chart as it was when Create was pressed.
        self._rate_run = {"document": copy.deepcopy(document), "rows": rows, "audio": audio, "format": suffix,
                          "failed": None, "decoder": None}
        self.rate_button.setEnabled(False)
        missing = [key for key, name in audio.items() if name is None]
        if not missing:
            self._finish_rates()
            return
        self.rate_progress.setRange(0, 0)  # busy until the decode is in
        self.rate_progress.setFormat(tr("MainWindow", "Reading the song…"))
        self.rate_progress.setVisible(True)
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(CHANNELS)
        fmt.setSampleFormat(QAudioFormat.Int16)
        decoder = self._rate_run["decoder"] = QAudioDecoder(self)
        decoder.setAudioFormat(fmt)
        chunks: list[bytes] = []
        decoder.bufferReady.connect(lambda: chunks.append(bytes(decoder.read().constData())))
        decoder.finished.connect(lambda: self._render_rates(b"".join(chunks), missing))
        decoder.error.connect(lambda _error: self._rates_failed(decoder.errorString()))
        decoder.setSource(QUrl.fromLocalFile(str(source)))
        decoder.start()

    def _render_rates(self, pcm: bytes, missing: list[tuple[float, bool]]) -> None:
        run = self._rate_run
        run.pop("decoder").deleteLater()
        if self.rate_jobs is None:
            # Three, as the test suite runs three: more made the owner's
            # machine lag while he was using it (2026-10-08).
            self._rate_pool = ProcessPoolExecutor(max_workers=3)
            self.rate_jobs = BackgroundJobs(self, executor=self._rate_pool)
        run["left"] = len(missing)
        self.rate_progress.setRange(0, len(missing))
        self.rate_progress.setValue(0)
        self.rate_progress.setFormat(tr("MainWindow", "Rendering: %v of %m rates done"))
        document = run["document"]
        for rate, pitch in missing:
            stem = document.source_path.parent / rate_change.audio_name(document.audio_filename, rate, pitch, "")
            self.rate_jobs.submit(partial(rate_render.render_to_file, pcm, rate, pitch, str(stem), run["format"]),
                                  partial(self._rate_rendered, (rate, pitch)), self._rate_render_failed)

    def _rate_rendered(self, key: tuple[float, bool], path: str) -> None:
        self._rate_run["audio"][key] = Path(path).name
        self._rate_step()

    def _rate_render_failed(self, error: BaseException) -> None:
        self._rate_run["failed"] = str(error)
        self._rate_step()

    def _rate_step(self) -> None:
        run = self._rate_run
        run["left"] -= 1
        self.rate_progress.setValue(self.rate_progress.maximum() - run["left"])
        if run["left"] == 0:
            self._finish_rates()

    def _rates_failed(self, message: str) -> None:
        decoder = self._rate_run.pop("decoder", None)
        if decoder is not None:
            decoder.deleteLater()
        self._rate_run = None
        self.rate_progress.setVisible(False)
        self._fill_rate_table()
        self.window.show_toast(tr("MainWindow", "Not converted: {0}").format(message))

    def _finish_rates(self) -> None:
        run = self._rate_run
        self._rate_run = None
        self.rate_progress.setVisible(False)
        written = []
        for row in run["rows"]:
            audio_file = run["audio"][(row.rate, row.pitch)]
            if audio_file is None:
                continue  # its render failed, and a chart without its song is no use
            try:
                written.append(rate_change.write(run["document"], row.rate, row.pitch, audio_file,
                                                 od=row.od, hp=row.hp, version=row.name))
            except (OSError, ValueError) as error:
                run["failed"] = str(error)
        self._fill_rate_table()
        if run["failed"]:
            self.window.show_toast(tr("MainWindow", "Not converted: {0}").format(run["failed"]))
        if written:
            self.window.status.setText(tr("MainWindow", "Wrote {0} difficulties. Press F5 in osu! to see them.")
                                       .format(len(written)))

    # -- mappack --------------------------------------------------------------

    def _build_mappack(self) -> None:
        self.pack_entries: list[mappack.Entry] = []
        self.pack_background: Path | None = None
        self._pack_creators: dict[Path, str] = {}
        self.pack_source: Path | None = None  # the pack loaded for editing, if any
        # Read once when a difficulty is added: (length, its .osu and audio,
        # its own background). Re-parsing every row per keystroke in Title
        # was 28ms a difficulty, 562ms a key with 20 of them.
        self._pack_info: dict[Path, tuple[int, dict[Path, int], dict[Path, int]]] = {}
        pack = self.sheet.add_section("mappack", tr("MainWindow", "Mappack Creator"),
                                      tr("MainWindow", "Difficulties from any set, as one new set"))
        self.pack_title = QLineEdit()
        self.pack_title.setPlaceholderText(tr("MainWindow", "Pack title"))
        pack.field(tr("MainWindow", "Title"), self.pack_title, tr(
            "MainWindow", "The same on every difficulty. The artist is Various Artists; "
            "each difficulty keeps its own tags and source."))
        self.pack_creator = QLineEdit()
        self.pack_creator.setText(self.window.settings.string_value("mappack/creator", ""))
        pack.field(tr("MainWindow", "Creator"), self.pack_creator, tr(
            "MainWindow", "Your osu! username: the set is submitted as yours."))
        for edit in (self.pack_title, self.pack_creator):
            edit.textChanged.connect(self._update_pack)
        self.pack_tag_creators = QCheckBox(tr("MainWindow", "Add the original mappers to the tags"))
        self.pack_tag_creators.setChecked(True)
        self.pack_tag_creators.toggled.connect(self._pack_creators_toggled)
        pack.field(tr("MainWindow", "Mappers"), self.pack_tag_creators, tr(
            "MainWindow", "Every difficulty's tags gain each original mapper in the pack, once, "
            "so searching any of them finds the set."))

        self.pack_hint = QLabel(tr(
            "MainWindow", "Add a difficulty in any of three ways: the button below while it is "
            "selected in osu!; Alt+Shift+A from anywhere, osu! included; or Ctrl+Shift+= on "
            "this app's song select."))
        self.pack_hint.setObjectName("fieldNote")
        self.pack_hint.setWordWrap(True)
        pack.add(self.pack_hint, span=2)

        buttons = QWidget()
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        self.pack_add_osu = QPushButton(tr("MainWindow", "Add osu!'s current difficulty"))
        self.pack_add_osu.clicked.connect(self.add_from_osu)
        self.pack_add_osu.setVisible(sys.platform == "win32")
        self.pack_load = QPushButton(tr("MainWindow", "Load osu!'s current pack"))
        self.pack_load.setToolTip(tr("MainWindow", "Edit a pack made earlier: select any of its "
                                     "difficulties in osu!, load it, add or remove difficulties, "
                                     "then Update pack."))
        self.pack_load.clicked.connect(self.load_from_osu)
        self.pack_remove = QPushButton(tr("MainWindow", "Remove selected"))
        self.pack_remove.clicked.connect(self._remove_pack_rows)
        row.addWidget(self.pack_add_osu)
        row.addWidget(self.pack_load)
        row.addWidget(self.pack_remove)
        row.addStretch(1)
        pack.add(buttons, span=2)

        self.pack_table = QTableWidget(0, 2)
        self.pack_table.setHorizontalHeaderLabels(
            [tr("MainWindow", "Difficulty name in the pack"), tr("MainWindow", "From")])
        header = self.pack_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        # The source folder is long and secondary: elided, the whole path in its tooltip.
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        self.pack_table.setColumnWidth(1, 280)
        self.pack_table.verticalHeader().setVisible(False)
        self.pack_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pack_table.setMinimumHeight(180)
        self.pack_table.itemChanged.connect(self._pack_item_changed)
        self.pack_table.currentCellChanged.connect(lambda *_: self._show_original_background())
        pack.add(self.pack_table, span=2)

        # The selected difficulty's own background beside the pack's: with
        # "Use original background" the left one is what that row gets.
        self.pack_original = BackgroundWell(drops=False)
        self.pack_well = BackgroundWell()
        self.pack_well.dropped.connect(lambda path: self._set_pack_background(Path(path)))
        images = QWidget()
        images_row = QHBoxLayout(images)
        images_row.setContentsMargins(0, 0, 0, 0)
        # Top-aligned, and the strip takes the cell's height: the Tags box
        # beside them sets the row's, and the grid centres anything shorter.
        images.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        images_row.addWidget(self.pack_original, 0, Qt.AlignTop)
        images_row.addWidget(self.pack_well, 0, Qt.AlignTop)
        images_row.addStretch(1)
        pack.field(tr("MainWindow", "Background"), images, tr(
            "MainWindow", "Left: the selected difficulty's own background. Right: one image for "
            "every difficulty; without one, each keeps its own. Identical files are stored once, "
            "whichever difficulties use them."))
        # The pack's tags in one place, for the Delete difficulty: it is the
        # one the set is submitted from, so its tags are what the set is
        # searched by. Filled as difficulties come in, and the user's to edit.
        self.pack_tags = TagsEdit()
        self.pack_tags.textChanged.connect(self._update_pack)
        pack.field(tr("MainWindow", "Tags"), self.pack_tags, tr(
            "MainWindow", "Every difficulty's tags, each word once, collected as they are added. "
            "Edit them freely; the \"Delete\" difficulty is written with exactly these."))
        # A row of its own, not inside the field: the field's box is
        # transparent all the way down, and a button in it lost its body.
        well_buttons = QWidget()
        well_row = QHBoxLayout(well_buttons)
        well_row.setContentsMargins(0, 0, 0, 0)
        select = QPushButton(tr("MainWindow", "Select…"))
        select.clicked.connect(self._choose_pack_background)
        clear = QPushButton(tr("MainWindow", "Use original background"))
        clear.clicked.connect(lambda: self._set_pack_background(None))
        self._pack_side_buttons = (select, clear)
        well_row.addWidget(select)
        well_row.addWidget(clear)
        well_row.addStretch(1)
        pack.add(well_buttons, span=1)
        tags_bar = QWidget()
        tags_row = QHBoxLayout(tags_bar)
        tags_row.setContentsMargins(0, 0, 0, 0)
        self.pack_tags_count = QLabel()
        self.pack_tags_count.setStyleSheet(COUNT_WARN_SHEET)
        self.pack_dedup = QPushButton(tr("MainWindow", "Remove duplicates"))
        self.pack_dedup.setToolTip(tr("MainWindow", "Keep each tag once, ignoring capitals: "
                                      "the first spelling stays."))
        self.pack_dedup.clicked.connect(
            lambda: self.pack_tags.setPlainText(mappack.merge_tags("", self.pack_tags.toPlainText())))
        tags_row.addWidget(self.pack_tags_count)
        tags_row.addStretch(1)
        tags_row.addWidget(self.pack_dedup)
        pack.add(tags_bar, span=1)

        self.pack_delete = QCheckBox(tr("MainWindow", "Include a \"Delete\" difficulty"))
        self.pack_delete.setChecked(True)
        self.pack_delete.toggled.connect(self._update_pack)
        pack.field(tr("MainWindow", "Size limit"), self.pack_delete, tr(
            "MainWindow", "A set may weigh 5 MB plus 10 MB per minute of its longest difficulty, "
            "at most 100 MB. \"Delete\" is 15 minutes long with two objects, which lifts a pack "
            "of short songs to the 100 MB cap. It is the well-known workaround; delete it after "
            "submitting."), span=2)
        self.pack_size = QProgressBar()
        self.pack_count = QProgressBar()
        self.pack_count.setRange(0, mappack.DIFFICULTY_LIMIT)
        for bar in (self.pack_size, self.pack_count):
            bar.setTextVisible(True)
        pack.field(tr("MainWindow", "Size"), self.pack_size)
        pack.field(tr("MainWindow", "Difficulties"), self.pack_count)
        actions = QWidget()
        actions_row = QHBoxLayout(actions)
        actions_row.setContentsMargins(0, 0, 0, 0)
        self.pack_create = QPushButton(tr("MainWindow", "Export new pack"))
        self.pack_create.clicked.connect(lambda: self.create_pack())
        self.pack_update = QPushButton(tr("MainWindow", "Update pack"))
        self.pack_update.setToolTip(tr("MainWindow", "Rewrite the loaded pack in the Songs folder. "
                                       "The old one goes to the Recycle Bin."))
        self.pack_update.clicked.connect(lambda: self.create_pack(update=True))
        actions_row.addWidget(self.pack_create)
        actions_row.addWidget(self.pack_update)
        actions_row.addStretch(1)
        pack.add(actions, span=2)
        self.pack_result = QLabel()
        self.pack_result.setObjectName("caution")
        self.pack_result.setWordWrap(True)
        self.pack_result.setVisible(False)
        pack.add(self.pack_result, span=2)
        self.hotkey = OsuHotkey(self.add_from_osu)
        self._show_original_background()
        self._update_pack()

    def fit_buttons(self) -> None:
        """Called from MainWindow.showEvent: measured once the window's sheet
        has given the buttons their padding, or the longer labels clip."""
        equalize_button_widths((self.pack_add_osu, self.pack_load, self.pack_remove), widths=False)
        equalize_button_widths((self.pack_create, self.pack_update), widths=False)
        equalize_button_widths(self._pack_side_buttons)
        equalize_button_widths((self.pack_dedup,), widths=False)
        equalize_button_widths((self.mania_button, self.mania_osu), widths=False)
        equalize_button_widths((self.rate_button,), widths=False)
        self._fit_rate_pills()

    def release(self) -> None:
        """The window is closing: the hotkey, and any render still running."""
        self.hotkey.release()
        self._osu_timer.stop()
        if self._osu_jobs is not None:
            self._osu_jobs.shutdown()
        if self._rate_pool is not None:
            self._rate_pool.shutdown(wait=False, cancel_futures=True)

    def add_difficulty(self, path: Path, version: str | None = None) -> None:
        """Add `path` to the pack, and say so wherever the user is. A
        `version` is a difficulty loaded back from the pack: its name is kept,
        and its Creator is the pack's own, not a mapper to tag."""
        path = Path(path).resolve()
        if any(entry.path == path for entry in self.pack_entries):
            self.window.show_toast(tr("MainWindow", "Already in the Mappack Creator."))
            return
        try:
            document = parse_osu(path)
            own_files = {path: path.stat().st_size}
            audio = mappack.resolve_ci(path.parent, document.audio_filename)
            if audio is not None:
                own_files[audio.resolve()] = audio.stat().st_size
            name = mappack.background_name(document.lines)
            image = mappack.resolve_ci(path.parent, name) if name else None
            own_background = {image.resolve(): image.stat().st_size} if image else {}
            self._pack_info[path] = (mappack.length_ms(document), own_files, own_background)
        except (OSError, ValueError) as error:
            self.window.show_toast(tr("MainWindow", "Could not read that difficulty: {0}").format(error))
            return
        values = metadata.read_keys(document.lines, "Metadata")
        creator = values.get("Creator", "").strip()
        self._pack_creators[path] = creator
        extra = values.get("Tags", "")
        if self.pack_tag_creators.isChecked() and version is None:
            extra += " " + creator
        self.pack_tags.setPlainText(mappack.merge_tags(self.pack_tags.toPlainText(), extra))
        entry = mappack.Entry(path, version or mappack.default_version(values))
        self.pack_entries.append(entry)
        row = self.pack_table.rowCount()
        self.pack_table.blockSignals(True)
        self.pack_table.insertRow(row)
        self.pack_table.setItem(row, 0, QTableWidgetItem(entry.version))
        source = QTableWidgetItem(path.parent.name)
        source.setFlags(source.flags() & ~Qt.ItemIsEditable)
        source.setToolTip(str(path))
        self.pack_table.setItem(row, 1, source)
        self.pack_table.blockSignals(False)
        self.pack_result.setVisible(False)
        self._update_pack()
        if version is None:
            self.window.show_toast(tr("MainWindow", "Added {0} to the Mappack Creator.").format(
                values.get("Version", path.stem)))

    def load_pack(self, folder: Path) -> None:
        """Put a pack made earlier back on the page, its tags collected."""
        try:
            values, paths, delete = mappack.load_pack(folder)
            delete_tags = metadata.read_keys(parse_osu(delete).lines, "Metadata").get("Tags", "") if delete else ""
        except (OSError, ValueError) as error:
            self.window.show_toast(tr("MainWindow", "Could not read that difficulty: {0}").format(error))
            return
        self.pack_table.setRowCount(0)
        self.pack_entries.clear()
        self.pack_source = Path(folder).resolve()
        self.pack_title.setText(values.get("Title", ""))
        self.pack_creator.setText(values.get("Creator", ""))
        self.pack_delete.setChecked(delete is not None)
        self.pack_tags.setPlainText("")
        self._set_pack_background(None)
        for path in paths:
            version = metadata.read_keys(parse_osu(path).lines, "Metadata").get("Version", path.stem)
            self.add_difficulty(path, version)
        if delete is not None:
            # Delete's tags are the list as the user last edited it: merging
            # every difficulty's back in would restore a tag they removed,
            # and the next Update would write it again.
            self.pack_tags.setPlainText(mappack.merge_tags("", delete_tags))
        self.window.show_toast(tr("MainWindow", "Loaded {0}: {1} difficulties.").format(
            Path(folder).name, len(self.pack_entries)))

    def load_from_osu(self) -> None:
        songs = self.window.settings.string_value("library/songs_folder", "")
        try:
            folder, _ = osu_memory.current_difficulty()
            folder = Path(songs) / folder if songs else None
        except osu_memory.OsuNotFound:
            # Without osu!, or off Windows: pick the pack's folder instead.
            chosen = QFileDialog.getExistingDirectory(self, tr("MainWindow", "Load osu!'s current pack"), songs)
            if not chosen:
                return
            folder = Path(chosen)
        if folder is None or not folder.is_dir():
            self.window.show_toast(tr("MainWindow", "Set the osu! Songs folder on song select first."))
            return
        self.load_pack(folder)

    def _osu_current_path(self) -> Path | None:
        """The .osu osu! has selected, or None having said why."""
        try:
            folder, name = osu_memory.current_difficulty()
        except osu_memory.OsuNotFound as error:
            self.window.show_toast(str(error))
            return None
        songs = self.window.settings.string_value("library/songs_folder", "")
        path = mappack.resolve_ci(Path(songs) / folder, name) if songs else None
        if path is None:
            self.window.show_toast(tr("MainWindow", "osu! has {0} selected, but it is not in the "
                                      "Songs folder set on song select.").format(name))
        return path

    def add_from_osu(self) -> None:
        path = self._osu_current_path()
        if path is not None:
            self.add_difficulty(path)

    def _pack_creators_toggled(self, on: bool) -> None:
        creators = " ".join(self._pack_creators[e.path] for e in self.pack_entries)
        if on:
            self.pack_tags.setPlainText(mappack.merge_tags(self.pack_tags.toPlainText(), creators))
        else:
            # ponytail: also drops a mapper's name that a difficulty had tagged itself.
            gone = {word.lower() for word in creators.split()}
            self.pack_tags.setPlainText(" ".join(w for w in self.pack_tags.toPlainText().split() if w.lower() not in gone))

    def _remove_pack_rows(self) -> None:
        for row in sorted({index.row() for index in self.pack_table.selectedIndexes()}, reverse=True):
            self.pack_table.removeRow(row)
            del self.pack_entries[row]
        self._update_pack()

    def _pack_item_changed(self, item) -> None:
        if item.column() == 0 and item.row() < len(self.pack_entries):
            self.pack_entries[item.row()].version = item.text().strip()
            self._update_pack()

    def _choose_pack_background(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("MainWindow", "Background"), "",
            tr("MainWindow", "Images") + " (*.jpg *.jpeg *.png *.webp *.bmp)")
        if path:
            self._set_pack_background(Path(path))

    def _set_pack_background(self, path: Path | None) -> None:
        self.pack_background = path
        self.pack_well.show_image(path)
        self._show_original_background()
        self._update_pack()

    def _show_original_background(self) -> None:
        row = self.pack_table.currentRow()
        if self.pack_background is not None:
            self.pack_original.show_image(None, tr("MainWindow", "Every difficulty uses the image on the right"))
        elif not 0 <= row < len(self.pack_entries):
            self.pack_original.show_image(None, tr("MainWindow", "Select a difficulty to see its background"))
        else:
            own = next(iter(self._pack_info[self.pack_entries[row].path][2]), None)
            self.pack_original.show_image(own, tr("MainWindow", "This difficulty has no background"))

    def _pack_estimate(self) -> tuple[int, int]:
        """(bytes, longest ms) the pack would have, before writing it.
        ponytail: the same song in two folders counts twice here; `build_pack`
        stores it once, so the meter errs high, never low."""
        files: dict[Path, int] = {}
        longest = 0
        for entry in self.pack_entries:
            length, own_files, own_background = self._pack_info[entry.path]
            longest = max(longest, length)
            files.update(own_files)
            if not self.pack_background:
                files.update(own_background)
        if self.pack_background and self.pack_background.is_file():
            files[self.pack_background] = self.pack_background.stat().st_size
        total = sum(files.values())
        if self.pack_delete.isChecked():
            total += len(mappack.silent_mp3(mappack.DELETE_LENGTH_MS))
            longest = mappack.DELETE_LENGTH_MS
        return total, longest

    def _update_pack(self) -> None:
        total, longest = self._pack_estimate()
        limit = mappack.size_limit_bytes(longest)
        mb = mappack.MB
        self.pack_size.setRange(0, max(1, limit // 1024))
        self.pack_size.setValue(min(limit, total) // 1024)
        self.pack_size.setFormat(tr("MainWindow", "{0:.1f} of {1:.0f} MB").format(total / mb, limit / mb))
        count = len(self.pack_entries) + self.pack_delete.isChecked()
        # Counted as written: normalised, as osu! will see it.
        tags_length = len(" ".join(self.pack_tags.toPlainText().split()))
        tags_over = self.pack_delete.isChecked() and tags_length > metadata.TAGS_LIMIT
        self.pack_tags.setEnabled(self.pack_delete.isChecked())
        self.pack_tags_count.setText(tr("MainWindow", "{0} / {1} characters").format(
            tags_length, metadata.TAGS_LIMIT))
        for widget in (self.pack_tags, self.pack_tags_count):
            set_warning(widget, tags_length > metadata.TAGS_LIMIT)
        self.pack_count.setValue(min(count, mappack.DIFFICULTY_LIMIT))
        self.pack_count.setFormat(f"{count} / {mappack.DIFFICULTY_LIMIT}")
        set_warning(self.pack_size, total > limit)
        set_warning(self.pack_count, count > mappack.DIFFICULTY_LIMIT)
        names = [entry.version for entry in self.pack_entries]
        ready = (bool(self.pack_entries) and bool(self.pack_title.text().strip())
                 and bool(self.pack_creator.text().strip()) and count <= mappack.DIFFICULTY_LIMIT
                 and all(names) and len(set(names)) == len(names) and not tags_over)
        self.pack_create.setEnabled(ready)
        self.pack_update.setEnabled(ready and self.pack_source is not None)
        if total > limit and not self.pack_delete.isChecked():
            self.pack_size.setToolTip(tr("MainWindow", "Over the limit: include the \"Delete\" difficulty."))
        else:
            self.pack_size.setToolTip("")

    def create_pack(self, update: bool = False) -> None:
        songs = self.window.settings.string_value("library/songs_folder", "")
        if not songs or not Path(songs).is_dir():
            self.window.show_toast(tr("MainWindow", "Set the osu! Songs folder on song select first."))
            return
        creator = self.pack_creator.text().strip()
        self.window.settings.set_value("mappack/creator", creator)
        try:
            report = mappack.build_pack(
                self.pack_entries, self.pack_title.text().strip(), creator, Path(songs),
                self.pack_background, self.pack_delete.isChecked(),
                self.pack_tag_creators.isChecked(), self.pack_tags.toPlainText(),
                self.pack_source if update else None)
        except (OSError, ValueError) as error:
            self.window.show_toast(tr("MainWindow", "The pack was not created: {0}").format(error))
            return
        if update:
            # Every row pointed into the old folder, now in the Recycle Bin.
            self.load_pack(report.folder)
        lines = [tr("MainWindow", "Created {0} ({1:.1f} MB). Press F5 in osu! to see it.").format(
                     report.folder.name, report.total_bytes / mappack.MB),
                 tr("MainWindow", "Submit it from the longest difficulty, [{0}]. The submission "
                    "measures the set by the difficulty it is submitted from, and a shorter one "
                    "gets its size budget and cuts or pads the others.").format(report.longest[0])]
        if report.clashes:
            lines.append(tr("MainWindow", "These hitsounds share a name with a different sound "
                            "from another set, and the first one is used: {0}").format(
                                ", ".join(report.clashes)))
        self.pack_result.setText("\n\n".join(lines))
        self.pack_result.setVisible(True)
