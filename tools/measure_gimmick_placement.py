"""Measure the latency of one gimmick placement: fake slider layer, regular and shiny.

    python tools/measure_gimmick_placement.py [--profile]

Written against the report "placing fake sliders or shiny lags" per CLAUDE.md's
measure-first rule. Only times `MainWindow._place_gimmick`; nothing in gui.py
or gimmick_session.py is changed.

No real dense gimmick map was available to run this against -- there are no
.osu files checked into the repo (tests build fixtures from byte literals in
tests/osu_fixtures.py) and this machine has no osu! Songs folder. The "dense"
document below is synthesized instead: it copies the *shape* CLAUDE.md
documents for a real gimmick map (an anti-barline wall is one uninherited
point roughly every beat/anti_lines_per_beat ms; a hidden anti-barline sheet
adds a green line for every note) rather than random data, but it is still
synthetic. If a real dense map turns up, re-run this against it before trusting
these numbers over that harness's own.

Times two things per kind ("regular", "shiny"):
  1. the whole `_place_gimmick` call, on a light document and then on one
     carrying ~40k timing points and ~15k hit objects, to see whether cost
     scales with existing gimmick density;
  2. the same placement broken into its phases -- structure build
     (`_gimmick_commands`), the undo push (`CompositeCommand.apply`),
     `_refresh_difficulty_views`, `_refresh_difficulty_sv_views`, and the
     repaint each schedules (`app.processEvents()`) -- by replicating
     `_place_gimmick`'s own body with a stopwatch between each call rather
     than by modifying it.

With --profile, also runs cProfile over one dense placement of each kind and
prints the cumulative-time hot list, the way tools/profile_playback.py does.
"""
from __future__ import annotations

import cProfile
import io
import itertools
import os
import pstats
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from ui import gui
from model.hit_object import HitObject, HITSOUND_CLAP
from osu_io.timing import TimingPoint
from tests.osu_fixtures import write_fixture

PROFILE = "--profile" in sys.argv


class _StubEntryDialog:
    """Answers the gimmick-entry question with USE_CURRENT, no dialog shown.

    Same stand-in tests/test_gimmick_editor.py uses -- carries the real
    class's constants because gui.py reads them off whatever name
    `GimmickEntryDialog` currently binds to, which under patch is this.
    """

    CREATE = gui.GimmickEntryDialog.CREATE
    USE_CURRENT = gui.GimmickEntryDialog.USE_CURRENT
    CANCEL = gui.GimmickEntryDialog.CANCEL

    def __init__(self, version, parent=None, references=None):
        pass

    def exec(self):
        return 1

    def selected_action(self):
        return self.USE_CURRENT

    def selected_reference(self):
        return None

    def deleteLater(self):
        pass


def build_window(tmpdir: Path):
    app = QApplication.instance() or QApplication([])
    window = gui.MainWindow()
    # Paint cost scales with pixels -- the offscreen platform's default
    # screen is far smaller than a real maximized window (see CLAUDE.md's
    # "Two traps"). This harness ends in app.processEvents() after every
    # placement, which is where that would show up.
    window.resize(1920, 1080)
    window._gimmick_index_path = lambda: tmpdir / "gimmick_index.json"
    window._gimmick_pairings = {}
    window.show()

    (tmpdir / "audio.mp3").write_bytes(b"\x00")
    path = write_fixture(tmpdir, "full_v14")
    window._load_map_path(path, refresh_difficulties=True)
    app.processEvents()

    with patch.object(gui, "GimmickEntryDialog", _StubEntryDialog):
        entered = window._enter_gimmick_page()
    if not entered:
        raise RuntimeError("failed to enter the gimmick page")
    window._show_page(gui.PAGE_GIMMICK)
    app.processEvents()
    return app, window


def build_dense_content(start_ms: float, span_ms: float, wall_step_ms: float, note_step_ms: float, first_index: int):
    """Timing points and hit objects shaped like CLAUDE.md's anti-barline wall.

    Not a copy of any one real map -- a wall line roughly every
    `wall_step_ms`, alternating uninherited (the wall) and inherited (a slit
    or a hidden-anti-barline SV line) the way the documented structures do,
    plus a run of circles at `note_step_ms` so the hit-object list is dense
    too. Good enough to scale InsertTimingPoints/InsertHitObjects' sort, the
    existing_red/is_fake_slider scans in _gimmick_commands, and each view's
    refresh_notes/refresh_points filtering -- which is what a real dense map
    would also exercise.
    """
    points: list[TimingPoint] = []
    t = start_ms
    end = start_ms + span_ms
    toggle = 0
    while t < end:
        if toggle % 4 == 0:
            points.append(TimingPoint.uninherited_at(round(t), 12345.0, meter=1, omit_first_barline=True))
        else:
            points.append(TimingPoint.inherited_at(round(t), 0.9 if toggle % 2 else 1.5))
        toggle += 1
        t += wall_step_ms

    notes: list[HitObject] = []
    t = start_ms
    index = first_index
    while t < end:
        hit_sound = HITSOUND_CLAP if index % 2 else 0
        notes.append(HitObject(256, 192, round(t), 1, hit_sound, original_index=index))
        index += 1
        t += note_step_ms

    return points, notes


def inject_dense_content(window, state) -> None:
    points, notes = build_dense_content(
        start_ms=20_000, span_ms=380_000, wall_step_ms=10.417, note_step_ms=25.0,
        first_index=window._next_original_index(state),
    )
    state.document.timing_points.extend(points)
    state.document.timing_points.sort(key=lambda p: p.time)
    state.document.hit_objects.extend(notes)
    state.document.hit_objects.sort(key=lambda n: n.time)
    print(
        f"  injected {len(points)} timing points, {len(notes)} hit objects "
        f"(document now has {len(state.document.timing_points)} timing points, "
        f"{len(state.document.hit_objects)} hit objects)"
    )
    with window._refresh_cycle():
        window._refresh_difficulty_views(window._gimmick_pairing.target)
        window._refresh_difficulty_sv_views(window._gimmick_pairing.target)


def time_placement(app, window, kind: str, t: float) -> float:
    start = perf_counter()
    window._place_gimmick("fake_slider", kind, t)
    app.processEvents()
    return (perf_counter() - start) * 1000.0


def time_placement_phases(app, window, state, pairing, kind: str, t: float) -> dict[str, float]:
    """Replicate `_place_gimmick`'s body with a stopwatch between phases.

    Calls the same private methods `_place_gimmick` calls, in the same order
    -- it does not reimplement their logic, only adds timing around the
    calls gui.py already makes.
    """
    phases: dict[str, float] = {}

    t0 = perf_counter()
    snapped = gui.osu_snap_ms(gui.snap_time(pairing.base_timing, t, window._gimmick_snap_divisor()))
    t1 = perf_counter()
    phases["snap"] = (t1 - t0) * 1000.0

    indices = itertools.count(window._next_original_index(state))
    t2 = perf_counter()
    phases["next_original_index"] = (t2 - t1) * 1000.0

    commands = window._gimmick_commands(state, pairing, "fake_slider", kind, snapped, indices)
    t3 = perf_counter()
    phases["gimmick_commands"] = (t3 - t2) * 1000.0

    if not commands:
        phases["history_push"] = 0.0
        phases["refresh_views"] = 0.0
        phases["refresh_sv_views"] = 0.0
        phases["process_events"] = 0.0
        phases["total"] = sum(phases.values())
        return phases

    state.history.push(gui.CompositeCommand(commands, "place_gimmick"), state)
    t4 = perf_counter()
    phases["history_push"] = (t4 - t3) * 1000.0

    with window._refresh_cycle():
        window._refresh_difficulty_views(pairing.target)
        t5 = perf_counter()
        phases["refresh_views"] = (t5 - t4) * 1000.0
        window._refresh_difficulty_sv_views(pairing.target)
        t6 = perf_counter()
        phases["refresh_sv_views"] = (t6 - t5) * 1000.0

    app.processEvents()
    t7 = perf_counter()
    phases["process_events"] = (t7 - t6) * 1000.0

    phases["total"] = (t7 - t0) * 1000.0
    return phases


def report(label: str, samples: list[float]) -> None:
    print(
        f"  {label}: n={len(samples)} "
        f"mean={statistics.mean(samples):.2f}ms "
        f"median={statistics.median(samples):.2f}ms "
        f"max={max(samples):.2f}ms "
        f"min={min(samples):.2f}ms"
    )


def profile_one(window, state, pairing, kind: str, t: float) -> None:
    profiler = cProfile.Profile()
    profiler.enable()
    window._place_gimmick("fake_slider", kind, t)
    profiler.disable()
    buffer = io.StringIO()
    stats = pstats.Stats(profiler, stream=buffer).sort_stats("cumulative")
    stats.print_stats(15)
    print(buffer.getvalue())


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        app, window = build_window(tmpdir)
        pairing = window._gimmick_pairing
        state = window._states[pairing.target]

        print("=== light document ===")
        print(
            f"  {len(state.document.timing_points)} timing points, "
            f"{len(state.document.hit_objects)} hit objects"
        )
        regular_times = [time_placement(app, window, "regular", 10_000 + i * 4_000) for i in range(20)]
        report("regular fake slider", regular_times)
        shiny_times = [time_placement(app, window, "shiny", 200_000 + i * 4_000) for i in range(20)]
        report("shiny", shiny_times)

        print("\n=== injecting dense gimmick content ===")
        inject_dense_content(window, state)

        print("\n=== dense document ===")
        regular_dense = [time_placement(app, window, "regular", 900_000 + i * 4_000) for i in range(20)]
        report("regular fake slider", regular_dense)
        shiny_dense = [time_placement(app, window, "shiny", 1_100_000 + i * 4_000) for i in range(20)]
        report("shiny", shiny_dense)

        print("\n=== dense document, phase breakdown (mean of 10 calls each) ===")
        phase_samples_regular = [
            time_placement_phases(app, window, state, pairing, "regular", 1_300_000 + i * 4_000)
            for i in range(10)
        ]
        phase_samples_shiny = [
            time_placement_phases(app, window, state, pairing, "shiny", 1_500_000 + i * 4_000)
            for i in range(10)
        ]
        for label, samples in (("regular", phase_samples_regular), ("shiny", phase_samples_shiny)):
            print(f"  {label}:")
            for key in (
                "snap", "next_original_index", "gimmick_commands", "history_push",
                "refresh_views", "refresh_sv_views", "process_events", "total",
            ):
                values = [s[key] for s in samples]
                print(f"    {key:<22} mean={statistics.mean(values):.3f}ms max={max(values):.3f}ms")

        if PROFILE:
            print("\n=== cProfile, one dense placement each (cumulative time) ===")
            print("-- regular --")
            profile_one(window, state, pairing, "regular", 1_700_000)
            print("-- shiny --")
            profile_one(window, state, pairing, "shiny", 1_700_000)

        window.close()


if __name__ == "__main__":
    main()
