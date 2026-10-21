"""The Miscellaneous page (owner, 2026-10-18): set-wide chores that have no
view of their own -- the beatmapset's metadata, shifting the whole map's
offset, scaling every green line's SV, and converting a difficulty to
osu!mania (`chart.taiko_to_mania`); and the Mappack Creator
(`library.mappack`), which gathers difficulties from other sets into a new one.

Everything here is an ordinary undoable edit on each difficulty's own history,
so the header's Save counts it and the Save dialog can still discard it.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from app.i18n import tr
from chart import autoplay, taiko_to_mania
from library import mappack, osu_memory
from model.commands import CompositeCommand, ReplaceDocumentLines, ShiftChartTimes
from osu_io import metadata
from osu_io.parser import parse_osu
from ui.common import equalize_button_widths
from ui.config_sheet import ConfigSheet, set_warning


def shifted_lines(lines: list[str], delta_ms: int, ending: str) -> list[str]:
    """`lines` with [Editor] Bookmarks and [General] PreviewTime moved by
    `delta_ms`. A PreviewTime of -1 means "none" and stays."""
    def as_int(value: str) -> int | None:
        # A value that is not a whole number (a hand-edited file) is kept as
        # it was: raising here left the set half shifted. Parsed, not
        # pre-checked: isdigit() passes "²" and "--5", which int() rejects.
        try:
            return int(value)
        except ValueError:
            return None

    bookmarks = metadata.read_keys(lines, "Editor").get("Bookmarks", "")
    if bookmarks:
        values = [value.strip() for value in bookmarks.split(",") if value.strip()]
        moved = ",".join(value if as_int(value) is None else str(as_int(value) + delta_ms) for value in values)
        lines = metadata.with_keys(lines, "Editor", {"Bookmarks": moved}, ending)
    preview = as_int(metadata.read_keys(lines, "General").get("PreviewTime", ""))
    if preview is not None and preview >= 0:
        lines = metadata.with_keys(lines, "General", {"PreviewTime": str(preview + delta_ms)}, ending)
    return lines


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

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setFixedSize(224, 126)  # 16:9, the shape osu! shows it in
        self.setObjectName("fieldNote")
        self.setStyleSheet("QLabel { border: 1px dashed #f3a6bd; border-radius: 6px; }")
        self.show_image(None)

    def show_image(self, path: Path | None) -> None:
        pixmap = QPixmap(str(path)) if path else QPixmap()
        if pixmap.isNull():
            self.setPixmap(QPixmap())
            self.setText(tr("MainWindow", "Drop an image here"))
        else:
            self.setPixmap(pixmap.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))

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
        self.fields: dict[str, QLineEdit] = {}
        for key in metadata.SET_KEYS:
            edit = QLineEdit()
            self.fields[key] = edit
            meta.field(labels[key], edit, tr("MainWindow", "Left blank, the field is not changed.")
                       if key != "Tags" else tr(
                           "MainWindow", "Separate tags with spaces, not commas: that is what a tag is "
                           "in the .osu file. Left blank, the tags are not changed."),
                       span=2 if key == "Tags" else 1)
        self.tags_count = QLabel()
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
        self.mania_keys.addItem(tr("MainWindow", "4K: the autoplay's hands"), 4)
        self.mania_keys.addItem(tr("MainWindow", "1K: one column"), 1)
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
        self.mania_finisher_single = QCheckBox(tr("MainWindow", "Hit finishers single"))
        mania.field(tr("MainWindow", "Finishers"), self.mania_finisher_single, tr(
            "MainWindow", "On, a finisher is one note in the hand's column. Off, it is two, "
            "on both halves of its colour."))
        self.mania_button = QPushButton(tr("MainWindow", "Convert to a new difficulty"))
        self.mania_button.clicked.connect(self.convert_to_mania)
        mania.add(self.mania_button, span=1)
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
        self.apply_metadata_button.setEnabled(not over and getattr(self.window, "state", None) is not None)

    def _done(self, paths, message: str) -> None:
        with self.window._refresh_cycle():
            for path in paths:
                self.window._refresh_difficulty_views(path)
                self.window._refresh_difficulty_sv_views(path)
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

    def convert_to_mania(self) -> None:
        state = getattr(self.window, "state", None)
        if state is None:
            return
        document = state.document
        hitsounds = getattr(self.window, "hitsounds", None)
        try:
            samples = taiko_to_mania.place_samples(
                document.source_path.parent,
                {key: Path(path) for key, path in (hitsounds.sample_paths if hitsounds else {}).items()})
            path = taiko_to_mania.convert(
                document, keys=int(self.mania_keys.currentData()),
                style=self.mania_style.currentData(), binding=self.mania_binding.currentData(),
                left_handed=self.mania_left.isChecked(), single_tap_bpm=self.mania_bpm.value(),
                finisher_single=self.mania_finisher_single.isChecked(), samples=samples)
        except (OSError, ValueError) as error:
            # FileExistsError is an OSError: converting twice with the same
            # options would overwrite the first, and that is refused.
            self.window.show_toast(tr("MainWindow", "Not converted: {0}").format(error))
            return
        self.window.status.setText(tr("MainWindow", "Wrote {0}. Press F5 in osu! to see it.").format(path.name))

    # -- mappack --------------------------------------------------------------

    def _build_mappack(self) -> None:
        self.pack_entries: list[mappack.Entry] = []
        self.pack_background: Path | None = None
        self._pack_lengths: dict[Path, int] = {}
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
        self.pack_remove = QPushButton(tr("MainWindow", "Remove selected"))
        self.pack_remove.clicked.connect(self._remove_pack_rows)
        row.addWidget(self.pack_add_osu)
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
        pack.add(self.pack_table, span=2)

        self.pack_well = BackgroundWell()
        self.pack_well.dropped.connect(lambda path: self._set_pack_background(Path(path)))
        pack.field(tr("MainWindow", "Background"), self.pack_well, tr(
            "MainWindow", "One image for every difficulty. Without one, each keeps its own. "
            "Identical files are stored once, whichever difficulties use them."), span=2)
        # A row of its own, not inside the field: the field's box is
        # transparent all the way down, and a button in it lost its body.
        well_buttons = QWidget()
        well_row = QHBoxLayout(well_buttons)
        well_row.setContentsMargins(0, 0, 0, 0)
        select = QPushButton(tr("MainWindow", "Select…"))
        select.clicked.connect(self._choose_pack_background)
        clear = QPushButton(tr("MainWindow", "Each keeps its own"))
        clear.clicked.connect(lambda: self._set_pack_background(None))
        self._pack_side_buttons = (select, clear)
        well_row.addWidget(select)
        well_row.addWidget(clear)
        well_row.addStretch(1)
        pack.add(well_buttons, span=2)

        self.pack_delete = QCheckBox(tr("MainWindow", "Include a \"Delete\" difficulty"))
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
        self.pack_create = QPushButton(tr("MainWindow", "Create the pack"))
        self.pack_create.clicked.connect(self.create_pack)
        pack.add(self.pack_create, span=1)
        self.pack_result = QLabel()
        self.pack_result.setObjectName("caution")
        self.pack_result.setWordWrap(True)
        self.pack_result.setVisible(False)
        pack.add(self.pack_result, span=2)
        self.hotkey = OsuHotkey(self.add_from_osu)
        self._update_pack()

    def fit_buttons(self) -> None:
        """Called from MainWindow.showEvent: measured once the window's sheet
        has given the buttons their padding, or the longer labels clip."""
        equalize_button_widths((self.pack_add_osu, self.pack_remove), widths=False)
        equalize_button_widths(self._pack_side_buttons)

    def release_hotkey(self) -> None:
        self.hotkey.release()

    def add_difficulty(self, path: Path) -> None:
        """Add `path` to the pack, and say so wherever the user is."""
        path = Path(path).resolve()
        if any(entry.path == path for entry in self.pack_entries):
            self.window.show_toast(tr("MainWindow", "Already in the Mappack Creator."))
            return
        try:
            document = parse_osu(path)
            self._pack_lengths[path] = mappack.length_ms(path)
        except (OSError, ValueError) as error:
            self.window.show_toast(tr("MainWindow", "Could not read that difficulty: {0}").format(error))
            return
        values = metadata.read_keys(document.lines, "Metadata")
        entry = mappack.Entry(path, mappack.default_version(values))
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
        self.window.show_toast(tr("MainWindow", "Added {0} to the Mappack Creator.").format(
            values.get("Version", path.stem)))

    def add_from_osu(self) -> None:
        try:
            folder, name = osu_memory.current_difficulty()
        except osu_memory.OsuNotFound as error:
            self.window.show_toast(str(error))
            return
        songs = self.window.settings.string_value("library/songs_folder", "")
        path = mappack.resolve_ci(Path(songs) / folder, name) if songs else None
        if path is None:
            self.window.show_toast(tr("MainWindow", "osu! has {0} selected, but it is not in the "
                                      "Songs folder set on song select.").format(name))
            return
        self.add_difficulty(path)

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
        self._update_pack()

    def _pack_estimate(self) -> tuple[int, int]:
        """(bytes, longest ms) the pack would have, before writing it.
        ponytail: the same song in two folders counts twice here; `build_pack`
        stores it once, so the meter errs high, never low."""
        files: dict[Path, int] = {}
        for entry in self.pack_entries:
            try:
                files[entry.path] = entry.path.stat().st_size
                document = parse_osu(entry.path)
                audio = mappack.resolve_ci(entry.path.parent, document.audio_filename)
                own = None if self.pack_background else mappack.background_name(document.lines)
                for extra in (audio, mappack.resolve_ci(entry.path.parent, own) if own else None):
                    if extra is not None:
                        files[extra.resolve()] = extra.stat().st_size
            except (OSError, ValueError):
                continue
        if self.pack_background and self.pack_background.is_file():
            files[self.pack_background] = self.pack_background.stat().st_size
        total = sum(files.values())
        longest = max((self._pack_lengths.get(e.path, 0) for e in self.pack_entries), default=0)
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
        self.pack_count.setValue(min(count, mappack.DIFFICULTY_LIMIT))
        self.pack_count.setFormat(f"{count} / {mappack.DIFFICULTY_LIMIT}")
        set_warning(self.pack_size, total > limit)
        set_warning(self.pack_count, count > mappack.DIFFICULTY_LIMIT)
        names = [entry.version for entry in self.pack_entries]
        self.pack_create.setEnabled(
            bool(self.pack_entries) and bool(self.pack_title.text().strip())
            and bool(self.pack_creator.text().strip()) and count <= mappack.DIFFICULTY_LIMIT
            and all(names) and len(set(names)) == len(names))
        if total > limit and not self.pack_delete.isChecked():
            self.pack_size.setToolTip(tr("MainWindow", "Over the limit: include the \"Delete\" difficulty."))
        else:
            self.pack_size.setToolTip("")

    def create_pack(self) -> None:
        songs = self.window.settings.string_value("library/songs_folder", "")
        if not songs or not Path(songs).is_dir():
            self.window.show_toast(tr("MainWindow", "Set the osu! Songs folder on song select first."))
            return
        creator = self.pack_creator.text().strip()
        self.window.settings.set_value("mappack/creator", creator)
        try:
            report = mappack.build_pack(
                self.pack_entries, self.pack_title.text().strip(), creator, Path(songs),
                self.pack_background, self.pack_delete.isChecked())
        except (OSError, ValueError) as error:
            self.window.show_toast(tr("MainWindow", "The pack was not created: {0}").format(error))
            return
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
