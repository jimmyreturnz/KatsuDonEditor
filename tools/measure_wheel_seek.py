"""What wheel-scrolling during playback does to the frames and the playhead.

    python tools/measure_wheel_seek.py <map.osu> [notch_interval_ms ...] [--seconds S]
                                       [--paused] [--editor | --gimmick] [--from MS] [--burst] [--profile]

`--rate R` plays at a speed button's rate (0.75, 0.5, 0.25).

`--paused` scrolls a stopped track, which is how most scrolling happens while
editing; `--editor` aims the notches at the Editor page's chart view and
`--gimmick` at the Gimmick page's first layer (entered on the current
difficulty), instead of the Fancy Arranger's timeline.

Plays the map for real (real `TrackPlayer`, real frame timer, real event loop)
and posts mouse-wheel notches at the main timeline every `notch_interval_ms`,
forward only. Per interval, plus a no-wheel baseline, it reports:

- **frames**: the gap between rendered frames (p50/p95/max) and how many
  exceeded 1.5x the 8.33ms cadence, i.e. a dropped frame; and the work inside
  `_render_gameplay_frame` itself.
- **backward**: how often the position broadcast to the views went *down*.
  Every notch moves forward, so any backward step is the playhead snapping
  back to a stale audio report -- the "jumps" half of the report.
- **seek_audio**: what one notch costs synchronously in the wheel handler.

Runs on the real platform with the window kept off-screen
(`WA_DontShowOnScreen`): the offscreen plugin delivers no paint events, so a
run there measures no painting at all (see `tools/check_view_refresh.py`).
Sized 1920x1080 for the same reason `profile_playback.py` is.
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import gui  # noqa: E402

FRAME_MS = 1000.0 / 120.0


def main() -> None:
    flags = {"--burst", "--paused", "--editor", "--gimmick", "--profile", "--callers"}
    args = [a for a in sys.argv[1:] if a not in flags]
    paused = "--paused" in sys.argv
    seconds = 3.0
    if "--seconds" in args:
        at = args.index("--seconds")
        seconds = float(args[at + 1])
        del args[at:at + 2]
    # The speed buttons' rate (owner, 2026-10-09: "when I play while at
    # slowdown, it frozen"): the time-stretched path is a different engine
    # path from 1.0x, and a run at 1.0x measures none of it.
    rate = 1.0
    if "--rate" in args:
        at = args.index("--rate")
        rate = float(args[at + 1])
        del args[at:at + 2]
    start_at = None
    if "--from" in args:
        at = args.index("--from")
        start_at = float(args[at + 1])
        del args[at:at + 2]
    path = Path(args[0])
    intervals = [float(a) for a in args[1:]] or [150.0, 60.0, 25.0]

    app = QApplication.instance() or QApplication([])
    window = gui.MainWindow()
    window.setAttribute(Qt.WA_DontShowOnScreen)
    window.resize(1920, 1080)
    window.show()
    window._load_map_path(path, refresh_difficulties=True)
    pump(app, 1.5)  # decode
    if rate != 1.0:
        window._choose_speed(rate)
        pump(app, 0.3)
    target = window.timeline
    if "--editor" in sys.argv:
        window._show_page(gui.PAGE_EDITOR)
        if not any(f.view_type == "chart" for f in window._editor_views):
            # Saved layouts can leave a difficulty with none; the run needs one.
            window._add_editor_view("chart", window.state.source_path)
        target = next(f for f in window._editor_views if f.view_type == "chart").chart_view
        pump(app, 0.3)
    if "--gimmick" in sys.argv:
        enter_gimmick_page(window)
        target = window._gimmick_views[0].chart_view
        pump(app, 0.5)

    # (wall ms, work ms, broadcast, a seek parked in the engine)
    frames: list[tuple[float, float, float, bool]] = []
    original_render = window._render_gameplay_frame
    engine = window.player._engine

    def render_hook() -> None:
        due = window._next_frame_due_ns
        began = time.perf_counter()
        original_render()
        if window._next_frame_due_ns == due:
            return  # a timer tick that was not a frame
        frames.append((began * 1000.0, (time.perf_counter() - began) * 1000.0,
                       window._last_broadcast_position,
                       getattr(engine, "_pending_seek_ms", None) is not None))

    window.gameplay_render_timer.timeout.disconnect()
    window.gameplay_render_timer.timeout.connect(render_hook)

    seek_costs: list[float] = []
    original_seek = window.seek_audio

    def seek_hook(position: float) -> None:
        began = time.perf_counter()
        original_seek(position)
        seek_costs.append((time.perf_counter() - began) * 1000.0)

    window.seek_audio = seek_hook
    # Views connected their seek_requested to the bound method before this
    # swap, and a stored bound-method connection is not redirected by replacing
    # the attribute (tests/test_window_lifecycle.py). Reconnect the timeline.
    target.seek_requested.disconnect()
    target.seek_requested.connect(seek_hook)

    # --from MS: start somewhere heavy, so a no-wheel run and a wheel run cover
    # the same content -- from the first note, a fast wheel run reaches 30s in
    # and a baseline plays three seconds of intro, which compares two places.
    start = start_at if start_at is not None else float(window.state.document.hit_objects[0].time)
    print(f"map: {path.name}   from {start:.0f}ms   {seconds:.1f}s per run   "
          f"{'paused' if paused else 'playing'}   {type(target).__name__}")
    print(f"{'run':>10}  {'gap p50':>7} {'p95':>6} {'max':>6}  {'late':>9}  "
          f"{'frozen':>6} {'parked':>6}  {'work p95':>8}  {'backward':>8} {'worst':>7}  "
          f"{'notches':>7} {'seek p95':>8}")

    for interval in [None, *intervals]:
        if window.player.playbackState() == QMediaPlayer.PlayingState:
            window.toggle_playback()
        pump(app, 0.3)
        window.seek_audio(start)
        pump(app, 0.3)
        frames.clear()
        seek_costs.clear()
        if not paused:
            window.toggle_playback()
        pump(app, 0.5)  # past the sink's own startup
        frames.clear()
        seek_costs.clear()
        profiler = None
        if "--profile" in sys.argv and interval == intervals[-1]:
            # The last (fastest) run only: where the main thread's time goes
            # while the wheel is spinning.
            import cProfile
            profiler = cProfile.Profile()
            profiler.enable()
        began = time.perf_counter()
        next_notch = began
        while time.perf_counter() - began < seconds:
            now = time.perf_counter()
            if interval is not None and now >= next_notch:
                notch(target)
                next_notch += interval / 1000.0
            app.processEvents()
            time.sleep(0.0005)
        report("baseline" if interval is None else f"{interval:.0f}ms", frames, seek_costs)
        if profiler is not None:
            import pstats
            profiler.disable()
            stats = pstats.Stats(profiler).sort_stats("tottime")
            stats.print_stats(18)
            if "--callers" in sys.argv:
                stats.print_callers(8)

    if "--burst" in sys.argv:
        burst(app, window, frames, start)

    if window.player.playbackState() == QMediaPlayer.PlayingState:
        window.toggle_playback()
    window.close()
    pump(app, 0.3)


def burst(app, window, frames, start, notch_ms: float = 8.0, burst_s: float = 0.5,
          after_s: float = 2.0) -> None:
    """A fast spin, then nothing: the freeze reported happens *after* it.

    Prints, per 100ms window from the end of the burst, how many frames moved
    the playhead, how many engine reports arrived, and whether the engine had
    a sink and a parked seek -- enough to see what the playhead is waiting on.
    """
    engine = window.player._engine
    reports: list[float] = []
    window.player.positionChanged.connect(lambda _p: reports.append(time.perf_counter()))
    if window.player.playbackState() == QMediaPlayer.PlayingState:
        window.toggle_playback()
    pump(app, 0.3)
    window.seek_audio(start)
    pump(app, 0.3)
    window.toggle_playback()
    pump(app, 0.5)
    frames.clear()
    reports.clear()
    began = time.perf_counter()
    next_notch = began
    notches = 0
    while time.perf_counter() - began < burst_s:
        if time.perf_counter() >= next_notch:
            notch(window.timeline)
            notches += 1
            next_notch += notch_ms / 1000.0
        app.processEvents()
        time.sleep(0.0005)
    burst_end = time.perf_counter()
    states = []
    while time.perf_counter() - burst_end < after_s:
        app.processEvents()
        states.append((time.perf_counter(), engine._sink is not None,
                       engine._pending_seek_ms is not None, engine._hold_frames))
        time.sleep(0.0005)
    print(f"\nburst: {notches} notches {notch_ms:.0f}ms apart over {burst_s}s, then {after_s}s idle")
    print(f"{'window':>11}  {'frames':>6} {'moved':>5} {'reports':>7}  {'no sink':>7} {'parked':>6} {'holding':>7}")
    end_ms = burst_end * 1000.0
    for w in range(int(after_s * 10)):
        lo, hi = end_ms + w * 100.0, end_ms + (w + 1) * 100.0
        win = [f for f in frames if lo <= f[0] < hi]
        before = [f for f in frames if f[0] < lo]
        prev = before[-1][2] if before else None
        moved = 0
        for f in win:
            if prev is not None and f[2] != prev:
                moved += 1
            prev = f[2]
        rep = sum(1 for r in reports if lo <= r * 1000.0 < hi)
        st = [s for s in states if lo <= s[0] * 1000.0 < hi]
        print(f"{w * 100:5d}-{(w + 1) * 100:<5d}  {len(win):6d} {moved:5d} {rep:7d}  "
              f"{sum(1 for s in st if not s[1]):7d} {sum(1 for s in st if s[2]):6d} "
              f"{sum(1 for s in st if s[3] > 0):7d}")


class _UseCurrent:
    """Answers the gimmick entry dialog with "use the current difficulty",
    the way the test suite's stub does, so a run needs no clicking."""
    CREATE = gui.GimmickEntryDialog.CREATE
    USE_CURRENT = gui.GimmickEntryDialog.USE_CURRENT
    CANCEL = gui.GimmickEntryDialog.CANCEL

    def __init__(self, version, parent=None, references=None):
        self.version = version

    def exec(self):
        return 1

    def selected_action(self):
        return self.USE_CURRENT

    def selected_reference(self):
        return None

    def deleteLater(self):
        pass


def enter_gimmick_page(window) -> None:
    real = gui.GimmickEntryDialog
    gui.GimmickEntryDialog = _UseCurrent
    try:
        window._enter_gimmick_page()
    finally:
        gui.GimmickEntryDialog = real
    window._show_page(gui.PAGE_GIMMICK)


def notch(view) -> None:
    centre = QPointF(view.width() / 2.0, view.height() / 2.0)
    event = QWheelEvent(
        centre, view.mapToGlobal(centre), QPoint(0, 0), QPoint(0, -120),
        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
    )
    QApplication.sendEvent(view, event)


def pump(app, seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.001)


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))] if ordered else 0.0


def report(name: str, frames, seek_costs) -> None:
    """Late frames (the timer itself fell behind) are counted apart from
    frozen ones (a frame ran on time and the playhead did not move), because
    both look like "not smooth" and they have unrelated causes."""
    if len(frames) < 3:
        print(f"{name:>10}  too few frames ({len(frames)})")
        return
    gaps = [b[0] - a[0] for a, b in zip(frames, frames[1:])]
    work = [f[1] for f in frames]
    steps = [(b[2] - a[2], b[3]) for a, b in zip(frames, frames[1:])]
    backward = [s for s, _parked in steps if s < 0]
    frozen = [parked for s, parked in steps if s == 0]
    late = sum(1 for g in gaps if g > FRAME_MS * 1.5)
    print(f"{name:>10}  {statistics.median(gaps):7.2f} {pct(gaps, 0.95):6.2f} {max(gaps):6.1f}  "
          f"{late:4d}/{len(gaps):<4d}  {len(frozen):6d} {sum(frozen):6d}  "
          f"{pct(work, 0.95):8.2f}  {len(backward):8d} {min(backward, default=0.0):7.1f}  "
          f"{len(seek_costs):7d} {pct(seek_costs, 0.95):8.2f}")


if __name__ == "__main__":
    main()
