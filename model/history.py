"""Undo/redo stacks.

One History per open difficulty, which is what makes independent per-difficulty
undo fall out for free once several are open at once.
"""
from __future__ import annotations

from model.commands import Command, EditTarget

# The old stacks were unbounded and held a full copy of every note position per
# entry. security_utils defines this constant and nothing imported it.
MAX_UNDO_STATES = 50


class History:
    def __init__(self, limit: int = MAX_UNDO_STATES) -> None:
        self.limit = limit
        self.undo_stack: list[Command] = []
        self.redo_stack: list[Command] = []
        # Replaces commit_revision. Bumped on every change so the preview cache
        # key goes stale.
        self.revision = 0
        self.saved_revision = 0

    # -- state -------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self.revision != self.saved_revision

    def mark_saved(self) -> None:
        self.saved_revision = self.revision

    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def clear(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.revision += 1

    def touch(self) -> None:
        """Mark the target dirty for a change made outside any Command.

        Only for state that intentionally has no undo step of its own (the
        Fancy Arranger's background drop is the one caller) -- anything a
        user expects Ctrl+Z to reverse belongs in a Command instead.
        """
        self.revision += 1

    # -- mutation ----------------------------------------------------------

    def push(self, command: Command, target: EditTarget, *, allow_merge: bool = False) -> None:
        """Apply `command` and record it.

        With allow_merge, a command that reports itself compatible with the one
        on top replaces it instead of stacking. That is what keeps a mouse drag,
        which fires per move event, to a single undo step.
        """
        command.apply(target)
        merged = None
        if allow_merge and self.undo_stack:
            merged = self.undo_stack[-1].merge_with(command)
        if merged is not None:
            self.undo_stack[-1] = merged
        else:
            self.undo_stack.append(command)
            if len(self.undo_stack) > self.limit:
                del self.undo_stack[0]
        self.redo_stack.clear()
        self.revision += 1

    def undo(self, target: EditTarget) -> Command | None:
        if not self.undo_stack:
            return None
        command = self.undo_stack.pop()
        command.revert(target)
        self.redo_stack.append(command)
        self.revision += 1
        return command

    def redo(self, target: EditTarget) -> Command | None:
        if not self.redo_stack:
            return None
        command = self.redo_stack.pop()
        command.apply(target)
        self.undo_stack.append(command)
        self.revision += 1
        return command
