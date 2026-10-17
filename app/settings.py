from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QKeySequenceEdit,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

# How opaque the editor layers draw their notes, as a percentage. The notes
# are translucent so the snap grid, the barlines and the SV curve stay readable
# *through* them, and where that balance sits is a preference: a dense gimmick
# map wants the grid showing, a sparse one wants the notes solid. The alphas in
# TimelineGameplay.__init__ are the built-in look, so this is the number that
# reproduces them -- see TimelineGameplay.set_note_opacity.
NOTE_OPACITY_DEFAULT_PERCENT = 70
NOTE_OPACITY_MIN_PERCENT = 20

ORGANIZATION_NAME = "jimmyreturnz"
APPLICATION_NAME = "TaikoFancyArranger"


class SettingsManager:
    """Small typed wrapper around QSettings for Taiko Fancy Arranger."""

    def __init__(self) -> None:
        self._settings = QSettings(ORGANIZATION_NAME, APPLICATION_NAME)

    def value(self, key: str, default=None):
        return self._settings.value(key, default)

    def set_value(self, key: str, value) -> None:
        self._settings.setValue(key, value)

    def sync(self) -> None:
        self._settings.sync()

    def clear_all(self) -> None:
        self._settings.clear()
        self._settings.sync()

    def bool_value(self, key: str, default: bool = False) -> bool:
        value = self._settings.value(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def int_value(self, key: str, default: int = 0) -> int:
        """Like bool_value: QSettings hands back a str on Windows, so coerce it.

        Through float first, because `int("70.0")` raises: a value that has ever
        round-tripped through a float -- a volume written by an older build, a
        hand-edited ini -- would otherwise silently reset to the default.
        """
        value = self._settings.value(key, default)
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def float_value(self, key: str, default: float = 0.0) -> float:
        """Like int_value: QSettings hands back a str on Windows."""
        try:
            return float(self._settings.value(key, default))
        except (TypeError, ValueError):
            return default

    def string_value(self, key: str, default: str = "") -> str:
        value = self._settings.value(key, default)
        return default if value is None else str(value)

    def storage_name(self) -> str:
        return self._settings.fileName()


@dataclass(frozen=True, slots=True)
class ShortcutDefinition:
    action_id: str
    label: str
    category: str
    default: str
    # Which toolbox owns this binding, "" for one that is live everywhere.
    # Only one toolbox is ever reachable at a time -- the Editor page's, or
    # the one gimmick layer that has focus -- so every one of them can bind
    # its first tool to "1" without those being conflicts. `duplicate_shortcuts`
    # and the QShortcut enabling in gui.py both key off this.
    scope: str = ""


# The Editor page's own toolbox. A per-layer category is built the same way
# in gui.py, so every toolbox reads as "Tools -- <the toolbox>" in Settings.
TOOLS_EDITOR_CATEGORY = "Tools — Editor"

# Every keyboard action the app registers as a real QShortcut, so the Settings
# dialog's list is the whole truth rather than three of them. Keys handled
# inside a widget's own keyPressEvent (Delete, Ctrl+A) stay out: they are
# scoped to the focused view, not app-level bindings, and listing them here
# would promise a rebind the widget would ignore. `back_to_songs` is the one
# exception -- it is compared against in MainWindow.keyPressEvent rather than
# registered, because a real shortcut would take Escape away from the views.
SHORTCUT_DEFINITIONS: tuple[ShortcutDefinition, ...] = (
    ShortcutDefinition("play_pause", "Play/Pause", "Playback", "Space"),
    # osu!'s editor transport keys (owner, 2026-10-17): Z to the first note,
    # X to 00:00:000 and playing, C a second Play/Pause.
    ShortcutDefinition("seek_first_note", "Seek to the first note", "Playback", "Z"),
    ShortcutDefinition("play_from_start", "Play from the start", "Playback", "X"),
    ShortcutDefinition("play_pause_alt", "Play/Pause (second key)", "Playback", "C"),
    # Song select's F5, in the editor: re-read the chart from its .osu.
    ShortcutDefinition("reload_chart", "Reload the chart from disk", "Playback", "F5"),
    # Tuned by ear while a chart plays at a slow rate, where the gap between a
    # note and its music is several times longer than at 1.0x.
    ShortcutDefinition("hitsound_offset_earlier", "Hitsound offset: 1ms earlier", "Playback", "Ctrl+["),
    ShortcutDefinition("hitsound_offset_later", "Hitsound offset: 1ms later", "Playback", "Ctrl+]"),
    ShortcutDefinition("undo", "Undo", "Editing", "Ctrl+Z"),
    ShortcutDefinition("redo", "Redo", "Editing", "Ctrl+Y"),
    ShortcutDefinition("copy", "Copy", "Editing", "Ctrl+C"),
    ShortcutDefinition("paste", "Paste", "Editing", "Ctrl+V"),
    ShortcutDefinition("resnap", "Resnap to each object's own grid", "Editing", "Ctrl+R"),
    # osu!'s editor keys (ui/editor_keys.py; chosen with the owner against
    # current osu!lazer and the stable wiki, 2026-10-11).
    ShortcutDefinition("toggle_new_combo", "Toggle new combo on the selection", "Editing", "Q"),
    ShortcutDefinition("toggle_whistle", "Toggle whistle on the selection (kat)", "Editing", "W"),
    ShortcutDefinition("toggle_finish", "Toggle finish on the selection (big note)", "Editing", "E"),
    ShortcutDefinition("toggle_clap", "Toggle clap on the selection (kat)", "Editing", "R"),
    ShortcutDefinition("reverse_selection", "Reverse the selection in time", "Editing", "Ctrl+G"),
    ShortcutDefinition("cut", "Cut", "Editing", "Ctrl+X"),
    ShortcutDefinition("clone_selection", "Clone the selection one measure later", "Editing", "Ctrl+D"),
    ShortcutDefinition("save_all", "Save all changed difficulties", "File", "Ctrl+S"),
    ShortcutDefinition("back_to_songs", "Back to the song list", "Navigation", "Esc"),
    ShortcutDefinition("seek_back", "Seek back one snap", "Navigation", "Left"),
    ShortcutDefinition("seek_forward", "Seek forward one snap", "Navigation", "Right"),
    ShortcutDefinition("seek_back_far", "Seek back four snaps", "Navigation", "Shift+Left"),
    ShortcutDefinition("seek_forward_far", "Seek forward four snaps", "Navigation", "Shift+Right"),
    ShortcutDefinition("seek_previous_note", "Seek to the previous note", "Navigation", "Ctrl+Left"),
    ShortcutDefinition("seek_next_note", "Seek to the next note", "Navigation", "Ctrl+Right"),
    ShortcutDefinition("seek_previous_bookmark", "Seek to the previous bookmark", "Navigation", "Alt+Left"),
    ShortcutDefinition("seek_next_bookmark", "Seek to the next bookmark", "Navigation", "Alt+Right"),
    ShortcutDefinition("focus_previous_view", "Focus the view above", "Navigation", "Up"),
    ShortcutDefinition("focus_next_view", "Focus the view below", "Navigation", "Down"),
    ShortcutDefinition("snap_1", "Beat snap 1/1", "Beat snap", "Shift+1"),
    ShortcutDefinition("snap_2", "Beat snap 1/2", "Beat snap", "Shift+2"),
    ShortcutDefinition("snap_3", "Beat snap 1/3", "Beat snap", "Shift+3"),
    ShortcutDefinition("snap_4", "Beat snap 1/4", "Beat snap", "Shift+4"),
    ShortcutDefinition("snap_5", "Beat snap 1/5", "Beat snap", "Shift+5"),
    ShortcutDefinition("snap_6", "Beat snap 1/6", "Beat snap", "Shift+6"),
    ShortcutDefinition("snap_7", "Beat snap 1/7", "Beat snap", "Shift+7"),
    ShortcutDefinition("snap_8", "Beat snap 1/8", "Beat snap", "Shift+8"),
    ShortcutDefinition("snap_9", "Beat snap 1/9", "Beat snap", "Shift+9"),
    ShortcutDefinition("tool_1", "Tool 1: Select", TOOLS_EDITOR_CATEGORY, "1", "editor"),
    ShortcutDefinition("tool_2", "Tool 2: Don / Green line", TOOLS_EDITOR_CATEGORY, "2", "editor"),
    ShortcutDefinition("tool_3", "Tool 3: Kat / Function", TOOLS_EDITOR_CATEGORY, "3", "editor"),
    ShortcutDefinition("tool_4", "Tool 4: Slider", TOOLS_EDITOR_CATEGORY, "4", "editor"),
    ShortcutDefinition("tool_5", "Tool 5: Spinner", TOOLS_EDITOR_CATEGORY, "5", "editor"),
    ShortcutDefinition("tool_6", "Tool 6: New combo", TOOLS_EDITOR_CATEGORY, "6", "editor"),
)

# Definitions owned by a module this one must not import. gui.py imports
# settings, so the gimmick page's per-layer toolboxes -- one set of tool keys
# per layer, derived from the layer tables that live in gui.py -- can only be
# handed over in this direction. Registering them rather than restating them
# here is what makes a seventh layer arrive with its own keys for free.
_REGISTERED_DEFINITIONS: list[ShortcutDefinition] = []


def register_shortcut_definitions(definitions) -> None:
    """Add definitions to the set Settings shows and the registry stores.

    Called at import time, before any ShortcutRegistry is built. Ignores an
    action_id that is already known, so a re-import cannot double a category.
    """
    known = {item.action_id for item in all_shortcut_definitions()}
    for definition in definitions:
        if definition.action_id not in known:
            known.add(definition.action_id)
            _REGISTERED_DEFINITIONS.append(definition)


def all_shortcut_definitions() -> tuple[ShortcutDefinition, ...]:
    """Built-in definitions first, then whatever registered itself."""
    return SHORTCUT_DEFINITIONS + tuple(_REGISTERED_DEFINITIONS)


def shortcut_scope(action_id: str) -> str:
    """The scope of a known action; "" for an unknown or unscoped one."""
    for definition in all_shortcut_definitions():
        if definition.action_id == action_id:
            return definition.scope
    return ""


class ShortcutRegistry:
    """Stores configurable keyboard shortcuts while keeping stable action IDs."""

    def __init__(self, settings: SettingsManager) -> None:
        self.settings = settings
        self.definitions = {item.action_id: item for item in all_shortcut_definitions()}

    @staticmethod
    def key(action_id: str) -> str:
        return f"shortcuts/{action_id}"

    def default_sequence(self, action_id: str) -> str:
        return self.definitions[action_id].default

    def sequence(self, action_id: str) -> str:
        default = self.default_sequence(action_id)
        value = self.settings.string_value(self.key(action_id), default).strip()
        return value or default

    def set_sequence(self, action_id: str, sequence: str) -> None:
        normalized = normalize_sequence(sequence)
        self.settings.set_value(self.key(action_id), normalized or self.default_sequence(action_id))


def normalize_sequence(value: str | QKeySequence) -> str:
    sequence = value if isinstance(value, QKeySequence) else QKeySequence(str(value))
    return sequence.toString(QKeySequence.PortableText).strip()


def duplicate_shortcuts(values: dict[str, str]) -> list[tuple[str, str, str]]:
    """Conflicting bindings, ignoring pairs that can never both be live.

    Scope-aware because the toolboxes overlap on purpose: the Editor page and
    each of the gimmick layers number their own tools from 1, and only one
    toolbox is reachable at a time, so "1 = Select" in seven places is seven
    working bindings rather than six conflicts. Two actions still clash when
    they share a scope, and an unscoped action clashes with everything -- a
    global Play/Pause moved onto "1" really would take the key from every
    toolbox.
    """
    seen: dict[str, list[str]] = {}
    duplicates: list[tuple[str, str, str]] = []
    for action_id, raw_sequence in values.items():
        sequence = normalize_sequence(raw_sequence)
        if not sequence:
            continue
        scope = shortcut_scope(action_id)
        holders = seen.setdefault(sequence, [])
        for other in holders:
            other_scope = shortcut_scope(other)
            if not scope or not other_scope or scope == other_scope:
                duplicates.append((sequence, other, action_id))
                break
        holders.append(action_id)
    return duplicates


def should_ignore_shortcut_focus(widget: QWidget | None) -> bool:
    """Return True when app-level shortcuts should not override text editing."""
    while widget is not None:
        if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QKeySequenceEdit)):
            return True
        if isinstance(widget, QAbstractSpinBox):
            return True
        if isinstance(widget, QComboBox) and widget.isEditable():
            return True
        widget = widget.parentWidget()
    return False