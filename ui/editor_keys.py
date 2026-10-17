"""osu!'s editor keys, on the Editor and Gimmick pages.

Chosen with the owner (2026-10-11) against current ppy/osu
(`GlobalActionContainer.editorKeyBindings`) and the osu! wiki's stable editor
table, where the two disagree:

- Left/Right seek, Shift for four steps; Ctrl+Left/Right the previous/next
  note and Alt+Left/Right the previous/next bookmark -- lazer's split. Stable
  puts bookmarks on Ctrl and nudges a selection with it.
- Up/Down move focus between the page's views (the owner's, not osu!'s: there
  they seek between timing points).
- Q/W/E/R toggle new combo, whistle, finish, clap on the selection, as both
  osu!s do; in taiko whistle or clap is what makes a kat, finish a big note.
- Shift+1..9 set the snap divisor, Ctrl+G reverses the selection in time,
  Ctrl+X cuts, Ctrl+D clones one measure on -- stable's.
- Z seeks to the first note, X to 00:00:000 and plays, C is a second
  Play/Pause (owner, 2026-10-17). F5 re-reads the chart from its .osu, as
  it does on song select.

Every binding is a `ShortcutDefinition` in `app/settings.py`, so Settings lists
and rebinds them like the rest.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QAbstractItemView, QAbstractSlider, QApplication, QComboBox

from app.settings import should_ignore_shortcut_focus
from chart.time_axis import wheel_seek_time
from model.commands import CompositeCommand, SetNoteFields
from model.hit_object import HITSOUND_CLAP, HITSOUND_FINISH, HITSOUND_WHISTLE, TYPE_NEW_COMBO
from osu_io.timing import active_uninherited_at
from ui.chart_views import editor_timeline_metadata

# What osu! steps by with Shift held (`Editor.seek`: amount 4 instead of 1).
SHIFT_SEEK_STEPS = 4


def takes_editor_keys_from(widget) -> bool:
    """Whether a focused `widget` needs these keys more than the chart does.

    A text or spin box edits with them, and a slider, a combo box or a list
    moves with the arrows. A shortcut is matched before the focused widget
    sees its key, so leaving one enabled there would take the key away.
    """
    return should_ignore_shortcut_focus(widget) or isinstance(
        widget, (QAbstractSlider, QComboBox, QAbstractItemView))


def reversed_times(times: list[float]) -> list[float]:
    """Each time mirrored inside the span of all of them, first becoming last
    -- osu!stable's Ctrl+G. Returned in the input's order."""
    if not times:
        return []
    first, last = min(times), max(times)
    return [first + last - time for time in times]


def toggled_bits(values: list[int], bit: int) -> list[int]:
    """osu!'s toggle over a selection: clear `bit` everywhere if every value
    has it already, otherwise set it everywhere."""
    if all(value & bit for value in values):
        return [value & ~bit for value in values]
    return [value | bit for value in values]


class EditorKeys(QObject):
    """Builds the shortcuts and carries out what they ask on `window`.

    A QObject child of the window, so the focus connection below goes with it.
    """

    # (action_id, method, args, pages) -- pages: "both" or "editor". The
    # selection edits stay on the Editor page: on the Gimmick page a note is
    # part of a structure, and what a key should do to a structure is not
    # something osu! has an answer for.
    ACTIONS = (
        ("seek_first_note", "_seek_first_note", (), "both"),
        ("play_from_start", "_play_from_start", (), "both"),
        ("play_pause_alt", "_play_pause", (), "both"),
        ("reload_chart", "_reload_chart", (), "both"),
        ("seek_back", "_seek_steps", (-1, 1), "both"),
        ("seek_forward", "_seek_steps", (1, 1), "both"),
        ("seek_back_far", "_seek_steps", (-1, SHIFT_SEEK_STEPS), "both"),
        ("seek_forward_far", "_seek_steps", (1, SHIFT_SEEK_STEPS), "both"),
        ("seek_previous_note", "_seek_note", (-1,), "both"),
        ("seek_next_note", "_seek_note", (1,), "both"),
        ("seek_previous_bookmark", "_seek_bookmark", (-1,), "both"),
        ("seek_next_bookmark", "_seek_bookmark", (1,), "both"),
        ("focus_previous_view", "_focus_view", (-1,), "both"),
        ("focus_next_view", "_focus_view", (1,), "both"),
        *((f"snap_{d}", "_set_snap", (d,), "both") for d in range(1, 10)),
        ("toggle_new_combo", "_toggle_type", (TYPE_NEW_COMBO,), "editor"),
        ("toggle_whistle", "_toggle_hitsound", (HITSOUND_WHISTLE,), "editor"),
        ("toggle_finish", "_toggle_hitsound", (HITSOUND_FINISH,), "editor"),
        ("toggle_clap", "_toggle_hitsound", (HITSOUND_CLAP,), "editor"),
        ("reverse_selection", "_reverse_selection", (), "editor"),
        ("cut", "_cut", (), "editor"),
        ("clone_selection", "_clone_selection", (), "editor"),
    )

    def __init__(self, window, editor_page, gimmick_page) -> None:
        super().__init__(window)
        self.window = window
        self._pages = {"editor": editor_page, "gimmick": gimmick_page}
        self.shortcuts: list[QShortcut] = []
        self._by_action: dict[str, list[QShortcut]] = {}
        for action_id, method, args, pages in self.ACTIONS:
            sequence = QKeySequence(window.shortcuts.sequence(action_id))
            for name, page in self._pages.items():
                if pages == "editor" and name != "editor":
                    continue
                # On the page, not the window: Qt then only matches it while
                # focus is somewhere on that page, which is what keeps the
                # arrows off song select (it has its own).
                shortcut = QShortcut(sequence, page)
                shortcut.setContext(Qt.WidgetWithChildrenShortcut)
                shortcut.activated.connect(
                    lambda m=method, a=args: getattr(self, m)(*a))
                self.shortcuts.append(shortcut)
                self._by_action.setdefault(action_id, []).append(shortcut)
        QApplication.instance().focusChanged.connect(self._focus_changed)
        self._focus_changed(None, QApplication.focusWidget())

    @property
    def shortcut_for(self) -> dict[str, QShortcut]:
        """One shortcut per action (the Editor page's), for the check that
        every registered binding is wired."""
        return {action_id: shortcuts[0] for action_id, shortcuts in self._by_action.items()}

    def reload(self) -> None:
        """Re-read every binding after Settings applies."""
        for action_id, shortcuts in self._by_action.items():
            sequence = QKeySequence(self.window.shortcuts.sequence(action_id))
            for shortcut in shortcuts:
                shortcut.setKey(sequence)

    def _focus_changed(self, _old, new) -> None:
        # Disabled outright rather than declining in the handler: a matched
        # shortcut has consumed its key either way.
        enabled = not takes_editor_keys_from(new)
        for shortcut in self.shortcuts:
            shortcut.setEnabled(enabled)

    # -- where the keys act --------------------------------------------------

    def _page(self) -> str:
        from ui.gui import PAGE_GIMMICK  # gui imports this module
        return "gimmick" if self.window.page_stack.currentIndex() == PAGE_GIMMICK else "editor"

    def _frames(self) -> list:
        """The page's views that run on the time axis, top to bottom."""
        frames = self.window._gimmick_views if self._page() == "gimmick" else self.window._editor_views
        return [
            frame for frame in frames
            if frame.isVisible() and hasattr(self._content(frame), "snap_points")
        ]

    @staticmethod
    def _content(frame):
        return getattr(frame, "chart_view", None) or getattr(frame, "sv_view", None)

    def _view(self):
        """The focused view on this page, else its first."""
        frames = self._frames()
        if not frames:
            return None
        focused = next((frame for frame in frames if getattr(frame, "focused", False)), frames[0])
        return self._content(focused)

    def _seek(self, view, target: float) -> None:
        view.current_time = max(0.0, target)
        view.seek_requested.emit(view.current_time)
        view.update()

    # -- seeking ---------------------------------------------------------------

    def _seek_steps(self, direction: int, steps: int) -> None:
        view = self._view()
        if view is None:
            return
        # The wheel's own step, so a key and a notch land on the same
        # millisecond: one snap paused, one beat while playing.
        divisor = 1 if view.is_playing else view.snap_divisor
        target = view.current_time
        for _ in range(steps):
            target = wheel_seek_time(view.snap_points, target, direction, divisor)
        self._seek(view, target)

    def _seek_note(self, direction: int) -> None:
        view = self._view()
        state = self._state_for(view)
        if state is None:
            return
        times = [note.time for note in state.document.hit_objects]
        self._seek_to_neighbour(view, times, direction)

    # -- osu!'s transport: Z, X, C ---------------------------------------------

    def _seek_first_note(self) -> None:
        view = self._view()
        state = self._state_for(view)
        if state is None or not state.document.hit_objects:
            return
        self._seek(view, min(note.time for note in state.document.hit_objects))

    def _play_from_start(self) -> None:
        view = self._view()
        if view is None:
            return
        self._seek(view, 0.0)
        if not view.is_playing:
            self.window.toggle_playback()

    def _play_pause(self) -> None:
        self.window._toggle_playback_from_shortcut()

    def _reload_chart(self) -> None:
        state = self._state_for(self._view()) or self.window.state
        if state is not None:
            self.window._reload_from_disk([state.source_path])

    def _seek_bookmark(self, direction: int) -> None:
        view = self._view()
        state = self._state_for(view)
        if state is None:
            return
        bookmarks, _preview = editor_timeline_metadata(state.document)
        self._seek_to_neighbour(view, bookmarks, direction)

    def _seek_to_neighbour(self, view, times, direction: int) -> None:
        """The nearest of `times` strictly before or after the playhead. Half
        a millisecond either side counts as on it, so repeating the key moves
        on rather than landing where it already is."""
        times = sorted(times)
        now = view.current_time
        if direction < 0:
            index = bisect_left(times, now - 0.5) - 1
        else:
            index = bisect_right(times, now + 0.5)
        if 0 <= index < len(times):
            self._seek(view, times[index])

    def _focus_view(self, direction: int) -> None:
        frames = self._frames()
        if not frames:
            return
        current = next((i for i, frame in enumerate(frames) if getattr(frame, "focused", False)), None)
        if current is None:
            index = 0 if direction > 0 else len(frames) - 1
        else:
            index = max(0, min(len(frames) - 1, current + direction))
        # Through Qt's focus, so the window's own focus handling rims the
        # frame and brings up its tool row exactly as a click would.
        self._content(frames[index]).setFocus(Qt.OtherFocusReason)

    def _set_snap(self, divisor: int) -> None:
        combo = (self.window.gimmick_snap_combo if self._page() == "gimmick"
                 else self.window.editor_snap_combo)
        index = combo.findData(divisor)
        if index >= 0:
            combo.setCurrentIndex(index)

    # -- editing the selection -------------------------------------------------

    def _state_for(self, view):
        if view is None:
            return None
        path = self.window._difficulty_for_view(view)
        return self.window._states.get(path) if path is not None else None

    def _selection(self):
        """(view, state, selected notes) on the Editor page's chart view."""
        view = self.window._active_chart_view
        state = self._state_for(view)
        if state is None or not hasattr(view, "selected_notes"):
            return None, None, []
        return view, state, view.selected_notes()

    def _push(self, view, state, commands, label: str) -> None:
        if not commands:
            return
        state.history.push(CompositeCommand(commands, label), state)
        path = self.window._difficulty_for_view(view)
        with self.window._refresh_cycle():
            self.window._refresh_difficulty_views(path)

    def _toggle_hitsound(self, bit: int) -> None:
        view, state, notes = self._selection()
        if not notes:
            return
        values = toggled_bits([note.hit_sound for note in notes], bit)
        self._push(view, state, [
            SetNoteFields(note.uid, {"hit_sound": (note.hit_sound, value)})
            for note, value in zip(notes, values) if value != note.hit_sound
        ], "set_note_fields")

    def _toggle_type(self, bit: int) -> None:
        view, state, notes = self._selection()
        if not notes:
            return
        values = toggled_bits([note.type for note in notes], bit)
        self._push(view, state, [
            SetNoteFields(note.uid, {"type": (note.type, value)})
            for note, value in zip(notes, values) if value != note.type
        ], "set_note_fields")

    def _reverse_selection(self) -> None:
        from ui.gui import extras_at  # gui imports this module
        view, state, notes = self._selection()
        if len(notes) < 2:
            return
        targets = reversed_times([float(note.time) for note in notes])
        self._push(view, state, [
            SetNoteFields(note.uid, {
                "time": (note.time, int(target)),
                "extras": (note.extras, extras_at(note, int(target))),
            })
            for note, target in zip(notes, targets) if int(target) != note.time
        ], "move_objects")

    def _cut(self) -> None:
        view, _state, notes = self._selection()
        if view is None or not (notes or view.selected_timing_uids):
            return
        self.window.copy_selection()
        # Then exactly what Delete does in the view, so a cut is a copy plus
        # the same removal, structures and undo steps included.
        if notes:
            view.notes_delete_requested.emit([note.uid for note in notes])
        if view.selected_timing_uids:
            view.timing_lines_delete_requested.emit(sorted(view.selected_timing_uids))

    def _clone_selection(self) -> None:
        view, state, notes = self._selection()
        if not notes:
            return
        last = max(notes, key=lambda note: note.time)
        timing = active_uninherited_at(state.document.timing_points, last.time)
        measure = timing.beat_length * max(1, int(timing.meter))
        self.window.copy_selection()
        self.window._paste_notes(at_time=last.time + measure)
