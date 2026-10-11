"""Per-difficulty editing state.

MainWindow holds exactly one of everything, and _load_map_path wipes selection,
both undo stacks and the preview cache on every difficulty switch. That single
line is what makes editing more than one difficulty impossible, and what makes
switching away and back silently discard uncommitted work.

Everything that belongs to a specific difficulty moves here. What stays on
MainWindow is what genuinely belongs to the application: settings, shortcuts,
the widget registries bound to the single control panel, and the one audio
device, render loop and timer set.

No Qt import, so this is testable without an offscreen QApplication.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from model.history import History


def _zero_offsets() -> dict[str, list[float]]:
    return {"all": [0.0, 0.0], "don": [0.0, 0.0], "kat": [0.0, 0.0]}


@dataclass
class DifficultyState:
    document: Any
    source_path: Path
    audio_path: Path | None = None
    background_path: Path | None = None

    original_positions: dict[int, tuple[int, int]] = field(default_factory=dict)
    applied_positions: dict[int, tuple[int, int]] = field(default_factory=dict)
    preview_positions: dict[int, tuple[int, int]] = field(default_factory=dict)
    selected: set[int] = field(default_factory=set)
    preview_offsets: dict[str, list[float]] = field(default_factory=_zero_offsets)
    preview_cache: dict = field(default_factory=dict)

    history: History = field(default_factory=History)
    # Per-difficulty cursor, so switching back returns to where you were.
    playhead_ms: float = 0.0
    duration_hint: int = 1

    # Export values are per-difficulty in the file format, even though the
    # toolbar currently exposes them as one global pair.
    approach_rate: float = 10.0
    circle_size: float = 7.0

    @classmethod
    def from_document(cls, document, source_path: Path, **kwargs) -> "DifficultyState":
        original = {note.original_index: (note.x, note.y) for note in document.hit_objects}
        duration = max((note.time for note in document.hit_objects), default=0) or 1
        return cls(
            document=document,
            source_path=Path(source_path),
            original_positions=original,
            applied_positions=dict(original),
            preview_positions=dict(original),
            duration_hint=duration,
            **kwargs,
        )

    # -- queries -----------------------------------------------------------

    @property
    def version(self) -> str:
        return self.document.version

    @property
    def dirty(self) -> bool:
        return self.history.dirty

    def is_modified(self) -> bool:
        """Whether any note has moved from where it was parsed."""
        return self.applied_positions != self.original_positions

    def sync_preview_to_applied(self) -> None:
        self.preview_positions = dict(self.applied_positions)

    def notes_in_time_order(self) -> list[int]:
        """Selected keys ordered by note time.

        sorted(selected) is time-ordered today only because original_index is
        assigned in file order. That stops being true the moment notes can be
        inserted, and every chunked transformation depends on this ordering, so
        callers should use this rather than sorting the raw keys.
        """
        by_key = {note.original_index: note for note in self.document.hit_objects}
        return sorted(
            (key for key in self.selected if key in by_key),
            key=lambda key: (by_key[key].time, key),
        )
