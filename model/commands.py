"""Undo/redo commands.

Replaces full snapshots of `applied_positions`, which could only express a
coordinate change, scaled as O(notes) per undo entry, and had no way to
represent a timing-point edit or a note being added.

Nothing here imports Qt, so every command is unit-testable without an offscreen
QApplication.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EditTarget(Protocol):
    """What a command is allowed to touch.

    DifficultyState satisfies this. Keeping it structural means commands never
    import editor_state, so the dependency runs one way only.
    """

    document: Any
    applied_positions: dict[int, tuple[int, int]]


class Command:
    """Base class. `label_id` is a stable identifier, translated at display time."""

    label_id = "command"

    def apply(self, target: EditTarget) -> None:
        raise NotImplementedError

    def revert(self, target: EditTarget) -> None:
        raise NotImplementedError

    def merge_with(self, later: "Command") -> "Command | None":
        """Return a combined command, or None if the two cannot merge."""
        return None


@dataclass
class MoveNotes(Command):
    """Coordinate changes, keyed by note identity.

    `changes` maps key -> (old_xy, new_xy). The key is whatever
    applied_positions is keyed by, which is original_index today and becomes uid
    once insertion exists.
    """

    changes: dict[int, tuple[tuple[int, int], tuple[int, int]]]
    label_id: str = "move_notes"

    def apply(self, target: EditTarget) -> None:
        for key, (_old, new) in self.changes.items():
            target.applied_positions[key] = new

    def revert(self, target: EditTarget) -> None:
        for key, (old, _new) in self.changes.items():
            target.applied_positions[key] = old

    def merge_with(self, later: "Command") -> "Command | None":
        """Coalesce consecutive drags of the same notes.

        The canvas drag handler fires per mouse-move. Without merging, a single
        drag would push dozens of entries and undo would rewind it one pixel at
        a time.
        """
        if not isinstance(later, MoveNotes) or later.changes.keys() != self.changes.keys():
            return None
        merged = {
            key: (self.changes[key][0], later.changes[key][1])
            for key in self.changes
        }
        return MoveNotes(merged, self.label_id)


@dataclass
class SetNotePositions(Command):
    """Replace the whole position map, for Reset applied transforms."""

    before: dict[int, tuple[int, int]]
    after: dict[int, tuple[int, int]]
    label_id: str = "set_positions"

    def apply(self, target: EditTarget) -> None:
        target.applied_positions.clear()
        target.applied_positions.update(self.after)

    def revert(self, target: EditTarget) -> None:
        target.applied_positions.clear()
        target.applied_positions.update(self.before)


@dataclass
class SetNoteFields(Command):
    """Field changes on one hit object: don<->kat, big toggle, retime, type."""

    uid: int
    changes: dict[str, tuple[Any, Any]]
    label_id: str = "set_note_fields"

    def _note(self, target: EditTarget):
        for note in target.document.hit_objects:
            if note.uid == self.uid:
                return note
        raise KeyError(f"No hit object with uid {self.uid}")

    def apply(self, target: EditTarget) -> None:
        note = self._note(target)
        for name, (_old, new) in self.changes.items():
            setattr(note, name, new)
        self._reorder(target)

    def revert(self, target: EditTarget) -> None:
        note = self._note(target)
        for name, (old, _new) in self.changes.items():
            setattr(note, name, old)
        self._reorder(target)

    def _reorder(self, target: EditTarget) -> None:
        """Keep the list in time order after a retime.

        Every view bisects `hit_objects` by time to find what is on screen, so a
        note dragged past its neighbour and left in place is a note that stops
        being drawn. Done here rather than at the call sites because both apply
        *and* revert can move it, and an undo has no call site of its own.
        """
        if "time" in self.changes:
            target.document.hit_objects.sort(key=lambda note: note.time)


@dataclass
class InsertHitObjects(Command):
    """Add hit objects. Stores them so revert can remove exactly these.

    Also registers each inserted note in `applied_positions`, which is what
    every write path reads a note's final x/y from. Without that, saving a map
    containing an inserted note either raised KeyError or silently wrote some
    *other* note's coordinates, depending on whether the new note's
    original_index happened to collide with an existing one.

    Only keys this command actually created are removed again on revert, so an
    insert whose note reuses an existing key cannot delete that key's real
    entry.
    """

    notes: list[Any]
    label_id: str = "insert_notes"
    _created_keys: set = field(default_factory=set)

    def apply(self, target: EditTarget) -> None:
        target.document.hit_objects.extend(self.notes)
        target.document.hit_objects.sort(key=lambda note: note.time)
        self._created_keys = set()
        for note in self.notes:
            if note.original_index not in target.applied_positions:
                target.applied_positions[note.original_index] = (note.x, note.y)
                self._created_keys.add(note.original_index)

    def revert(self, target: EditTarget) -> None:
        removing = {note.uid for note in self.notes}
        target.document.hit_objects[:] = [
            note for note in target.document.hit_objects if note.uid not in removing
        ]
        for key in self._created_keys:
            target.applied_positions.pop(key, None)
        self._created_keys = set()


@dataclass
class RemoveHitObjects(Command):
    """Delete hit objects, keeping the objects themselves for revert."""

    notes: list[Any]
    label_id: str = "remove_notes"

    def apply(self, target: EditTarget) -> None:
        removing = {note.uid for note in self.notes}
        target.document.hit_objects[:] = [
            note for note in target.document.hit_objects if note.uid not in removing
        ]

    def revert(self, target: EditTarget) -> None:
        target.document.hit_objects.extend(self.notes)
        target.document.hit_objects.sort(key=lambda note: note.time)


@dataclass
class InsertTimingPoints(Command):
    """Add timing points. One SV sweep or barline pass is one of these."""

    points: list[Any]
    label_id: str = "insert_timing_points"

    def apply(self, target: EditTarget) -> None:
        target.document.timing_points.extend(self.points)
        target.document.timing_points.sort(key=lambda point: point.time)

    def revert(self, target: EditTarget) -> None:
        removing = {point.uid for point in self.points}
        target.document.timing_points[:] = [
            point for point in target.document.timing_points if point.uid not in removing
        ]


@dataclass
class RemoveTimingPoints(Command):
    points: list[Any]
    label_id: str = "remove_timing_points"
    # Where each removed point stood, so an undo puts it back in the same file
    # order. A sort on time alone put a restored red line *after* the green
    # line sharing its millisecond -- and osu! reads a tie by file order, so
    # the undo reset the SV the green line was there to set.
    _positions: list[tuple[int, Any]] = field(default_factory=list, init=False, repr=False, compare=False)

    def apply(self, target: EditTarget) -> None:
        removing = {point.uid for point in self.points}
        points = target.document.timing_points
        self._positions = [(index, point) for index, point in enumerate(points) if point.uid in removing]
        points[:] = [point for point in points if point.uid not in removing]

    def revert(self, target: EditTarget) -> None:
        points = target.document.timing_points
        if len(self._positions) != len(self.points):
            # Never applied (or applied to a list that lacked some): no order to restore.
            points.extend(self.points)
            points.sort(key=lambda point: point.time)
            return
        for index, point in self._positions:
            points.insert(index, point)


@dataclass
class EditTimingPoint(Command):
    """Field changes on one timing point, e.g. dragging a green line's SV."""

    uid: int
    changes: dict[str, tuple[Any, Any]]
    label_id: str = "edit_timing_point"

    def _point(self, target: EditTarget):
        for point in target.document.timing_points:
            if point.uid == self.uid:
                return point
        raise KeyError(f"No timing point with uid {self.uid}")

    def apply(self, target: EditTarget) -> None:
        point = self._point(target)
        for name, (_old, new) in self.changes.items():
            setattr(point, name, new)
        self._reorder(target)

    def revert(self, target: EditTarget) -> None:
        point = self._point(target)
        for name, (old, _new) in self.changes.items():
            setattr(point, name, old)
        self._reorder(target)

    def _reorder(self, target: EditTarget) -> None:
        """Keep the list in time order after a retime -- see SetNoteFields.

        `sv_at` and every view's window slice both assume it, and an undo of a
        retime has no call site that could sort on its behalf.
        """
        if "time" in self.changes:
            target.document.timing_points.sort(key=lambda point: point.time)

    def merge_with(self, later: "Command") -> "Command | None":
        """Coalesce a continuous drag on the same point and fields."""
        if not isinstance(later, EditTimingPoint):
            return None
        if later.uid != self.uid or later.changes.keys() != self.changes.keys():
            return None
        merged = {
            name: (self.changes[name][0], later.changes[name][1])
            for name in self.changes
        }
        return EditTimingPoint(self.uid, merged, self.label_id)


@dataclass
class ShiftChartTimes(Command):
    """Every hit object and timing point `delta_ms` later (earlier if negative).

    One command rather than a SetNoteFields/EditTimingPoint per object: each of
    those re-sorts its list, which over a gimmick difficulty's thousands of
    lines is quadratic. A uniform shift keeps every order as it is.
    """

    delta_ms: int
    label_id: str = "shift_offset"
    # A spinner's end is an absolute time in its extras; the originals, by
    # uid, so the undo puts back the exact text rather than a recomputation.
    _extras: dict[int, tuple[str, ...]] = field(default_factory=dict, init=False, repr=False, compare=False)

    def _shift(self, target: EditTarget, delta: int, forward: bool) -> None:
        for note in target.document.hit_objects:
            note.time += delta
            if note.is_spinner and note.extras:
                if forward:
                    self._extras[note.uid] = note.extras
                    end = note.end_time
                    if end is not None:
                        note.extras = (str(end + delta), *note.extras[1:])
                elif note.uid in self._extras:
                    note.extras = self._extras[note.uid]
        for point in target.document.timing_points:
            point.time += delta

    def apply(self, target: EditTarget) -> None:
        self._shift(target, self.delta_ms, True)

    def revert(self, target: EditTarget) -> None:
        self._shift(target, -self.delta_ms, False)


@dataclass
class ReplaceDocumentLines(Command):
    """Swap the passthrough lines -- [Metadata], [General], [Editor] edits.

    The writer keeps those lines verbatim (`osu_io.metadata`), so the line
    list *is* the data, and a whole-list swap is the simplest exact undo.
    """

    old: list[str]
    new: list[str]
    label_id: str = "edit_metadata"

    def _set(self, target: EditTarget, lines: list[str]) -> None:
        from osu_io.writer import _section_spans

        target.document.lines[:] = lines
        target.document.section_spans = _section_spans(target.document.lines)

    def apply(self, target: EditTarget) -> None:
        self._set(target, self.new)

    def revert(self, target: EditTarget) -> None:
        self._set(target, self.old)


@dataclass
class CompositeCommand(Command):
    """Several commands as one undo step.

    This is what makes a 400-point eased SV sweep, or a full barline pass over a
    selection, a single Ctrl+Z rather than 400 of them.
    """

    commands: list[Command] = field(default_factory=list)
    label_id: str = "composite"

    def apply(self, target: EditTarget) -> None:
        for command in self.commands:
            command.apply(target)

    def revert(self, target: EditTarget) -> None:
        for command in reversed(self.commands):
            command.revert(target)
