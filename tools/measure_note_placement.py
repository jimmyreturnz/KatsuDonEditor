"""Latency of one placement on a real map: layer-1 note and fake slider.

    python tools/measure_note_placement.py <map.osu> [samples] [--profile]

Written against "placing a note on a heavy gimmick map lags 200-300ms", per
CLAUDE.md's measure-first rule. Nothing in gui.py changes: the suspect stages
are wrapped with a stopwatch at attribute level, so the numbers are what the
real `_place_note` / `_place_gimmick` bodies spend in them.

Stage times are **inclusive** (a stage called inside another counts in both)
and summed per placement, so a stage that runs once per gimmick layer shows
the whole layer loop. `paint` is the `processEvents()` after the call, which
is where the queued `update()`s land.

Each placement is undone before the next, so the document stays the size it
was loaded at and every sample measures the same map.
"""
from __future__ import annotations

import cProfile
import io
import os
import pstats
import statistics
import sys
import tempfile
from collections import defaultdict
from functools import wraps
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from ui import gui
from tools.measure_gimmick_placement import _StubEntryDialog

args = [a for a in sys.argv[1:] if not a.startswith("--")]
PROFILE = "--profile" in sys.argv
if not args:
    sys.exit(__doc__)
MAP = Path(args[0])
SAMPLES = int(args[1]) if len(args) > 1 else 15

stage_ms: dict[str, float] = defaultdict(float)
stage_calls: dict[str, int] = defaultdict(int)


def _timed(name, fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        start = perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            stage_ms[name] += (perf_counter() - start) * 1000.0
            stage_calls[name] += 1
    return wrapper


def instrument(window) -> None:
    # Instance attributes: gui.py calls these as self.x(...), looked up per call.
    for name in (
        "_next_original_index", "_reschedule_hitsounds", "_refresh_gimmick_views",
        "_refresh_difficulty_views", "_refresh_difficulty_sv_views", "_reload_timing_bars",
        "_gimmick_commands", "refresh_canvas", "_after_history_change",
    ):
        setattr(window, name, _timed(name, getattr(window, name)))
    window.timeline.refresh_notes = _timed("timeline.refresh_notes", window.timeline.refresh_notes)
    window.hitsounds.set_schedule = _timed("hitsounds.set_schedule", window.hitsounds.set_schedule)
    # Class level: one entry per view kind, summed over every layer holding one.
    for cls, method in (
        (gui.TimelineGameplay, "refresh_notes"),
        (gui.SVEditorView, "refresh_points"),
        (gui.GameplayViewerView, "refresh_notes"),
        (gui.InsertHitObjects, "apply"),
        (gui.InsertTimingPoints, "apply"),
        (gui.RemoveHitObjects, "apply"),
        (gui.RemoveTimingPoints, "apply"),
    ):
        setattr(cls, method, _timed(f"{cls.__name__}.{method}", getattr(cls, method)))


def build_window(tmpdir: Path):
    app = QApplication.instance() or QApplication([])
    window = gui.MainWindow()
    window.resize(1920, 1080)  # CLAUDE.md: offscreen's 758x180 understates paint
    # Keep the pairings MainWindow loaded -- the reference this map is really
    # edited against is the one worth measuring -- but send any write to tmp.
    window._gimmick_pairings = dict(window._gimmick_pairings)
    window._gimmick_index_path = lambda: tmpdir / "gimmick_index.json"
    window.show()
    start = perf_counter()
    window._load_map_path(MAP, refresh_difficulties=True)
    app.processEvents()
    print(f"open map: {(perf_counter() - start) * 1000:.0f}ms")
    # Only asked when this map has no saved pairing: take the dialog's default,
    # which is the difficulty's own timing (listed first).
    choices = window._timing_reference_choices(window.state.source_path)

    class Entry(_StubEntryDialog):
        def selected_reference(self):
            return choices[0][1] if choices else None

    # "This difficulty already has gimmicks" is a modal warning, and offscreen
    # there is nobody to dismiss it -- the first run of this harness hung there.
    with patch.object(gui, "GimmickEntryDialog", Entry), \
            patch.object(gui.QMessageBox, "warning", lambda *a, **k: None):
        if not window._enter_gimmick_page():
            raise RuntimeError("failed to enter the gimmick page")
    print(f"timing reference: {window._gimmick_pairing.reference}")
    start = perf_counter()
    window._show_page(gui.PAGE_GIMMICK)
    app.processEvents()
    print(f"show gimmick page: {(perf_counter() - start) * 1000:.0f}ms")
    return app, window


def free_times(state, count: int) -> list[int]:
    """Midpoints between existing notes, spread over the map: empty columns."""
    notes = sorted({n.time for n in state.document.hit_objects})
    gaps = [(a + b) // 2 for a, b in zip(notes, notes[1:]) if b - a >= 4]
    step = max(1, len(gaps) // count)
    return gaps[::step][:count]


def undo(app, state, window) -> float:
    start = perf_counter()
    state.history.undo(state)
    window._after_history_change(state)
    app.processEvents()
    return (perf_counter() - start) * 1000.0


def run(app, window, state, label, place, times) -> None:
    totals, calls_ms, paints, undos = [], [], [], []
    per_stage: dict[str, list[float]] = defaultdict(list)
    for t in times:
        stage_ms.clear()
        stage_calls.clear()
        start = perf_counter()
        place(t)
        mid = perf_counter()
        app.processEvents()
        end = perf_counter()
        calls_ms.append((mid - start) * 1000.0)
        paints.append((end - mid) * 1000.0)
        totals.append((end - start) * 1000.0)
        for name, ms in stage_ms.items():
            per_stage[f"{name} (x{stage_calls[name]})"].append(ms)
        undos.append(undo(app, state, window))
    print(f"\n=== {label}: {len(times)} placements ===")
    for name, values in (("total (call + paint)", totals), ("call", calls_ms), ("paint", paints), ("undo + paint", undos)):
        print(f"  {name:<44} median={statistics.median(values):8.2f}ms max={max(values):8.2f}ms")
    print("  -- stages (inclusive, median per placement) --")
    for name, values in sorted(per_stage.items(), key=lambda kv: -statistics.median(kv[1])):
        print(f"  {name:<44} median={statistics.median(values):8.2f}ms")


def profile(app, window, state, label, place, t) -> None:
    profiler = cProfile.Profile()
    profiler.enable()
    place(t)
    app.processEvents()
    profiler.disable()
    undo(app, state, window)
    buffer = io.StringIO()
    pstats.Stats(profiler, stream=buffer).sort_stats("tottime").print_stats(20)
    print(f"\n=== cProfile {label} (tottime) ===\n{buffer.getvalue()}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app, window = build_window(Path(tmp))
        target = window._gimmick_pairing.target
        state = window._states[target]
        print(
            f"{MAP.name}: {len(state.document.timing_points)} timing points, "
            f"{len(state.document.hit_objects)} hit objects, "
            f"{len(window._gimmick_views)} gimmick layers"
        )
        instrument(window)
        times = free_times(state, SAMPLES * 2 + 2)
        note = lambda t: window._place_note(target, "don", t, False, False)
        fake = lambda t: window._place_gimmick("fake_slider", "regular", t)
        run(app, window, state, "layer-1 don (_place_note)", note, times[0:SAMPLES * 2:2])
        run(app, window, state, "fake slider (_place_gimmick)", fake, times[1:SAMPLES * 2:2])
        if PROFILE:
            profile(app, window, state, "layer-1 don", note, times[-2])
            profile(app, window, state, "fake slider", fake, times[-1])
        window.close()


if __name__ == "__main__":
    main()
