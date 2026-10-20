"""The Miscellaneous page (owner, 2026-10-18): set-wide chores that have no
view of their own -- the beatmapset's metadata, shifting the whole map's
offset, and scaling every green line's SV.

Everything here is an ordinary undoable edit on each difficulty's own history,
so the header's Save counts it and the Save dialog can still discard it.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from app.i18n import tr
from model.commands import CompositeCommand, ReplaceDocumentLines, ShiftChartTimes
from osu_io import metadata
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
        for widget in (self.apply_metadata_button, self.shift_button, self.scale_all_button):
            widget.setEnabled(state is not None)
        self._load_fields(current)

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
