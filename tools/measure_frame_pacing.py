"""Whether the gameplay preview moves by the same step on every refresh.

    python tools/measure_frame_pacing.py <map.osu> [--seconds S] [--rate R] [--at MS] [--timer]

The stutter a fast machine had as much as a slow one (2026-11-04): paint cost
is not it, so this measures *pacing*. A thread of its own waits on the real
vblanks (`common.VBlankWaiter`) and stamps each one; the gameplay view stamps
the end of every paint with the time it drew. DWM shows, at each vblank, the
newest frame finished before it, so per refresh this reports

- how many new frames landed in it: exactly 1 is smooth, 0 is a repeated
  frame, 2+ is one thrown away;
- the content step between the frames shown on consecutive refreshes, over
  what one refresh of song time is (period x rate): 1.00 every time is smooth,
  and its spread is the judder;
- the gaps between the app's own frames, which on the old 4ms timer could only
  be multiples of 4ms.

--at plays from a millisecond rather than the first note: a gimmick is a
section of a map, and the first note is almost never in it.

--timer forces the old timer-driven loop for a before/after on one build.
Real platform only: the offscreen plugin has no vblanks.
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import gui  # noqa: E402
from ui.common import VBlankWaiter  # noqa: E402


def option(name: str, default: float) -> float:
    return float(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def main() -> None:
    path = Path(sys.argv[1])
    seconds = option("--seconds", 8.0)
    rate = option("--rate", 1.0)
    app = QApplication.instance() or QApplication([])
    window = gui.MainWindow()
    window.resize(1920, 1080)
    window.show()
    window._load_map_path(path, refresh_difficulties=True)
    window._add_editor_view("gameplay", path)
    app.processEvents()
    if "--timer" in sys.argv and getattr(window, "_vblank", None) is not None:
        window._stop_vsync()
    view = window._gameplay_views[-1]
    print(f"loop: {'vsync (' + window._vblank.method + ')' if getattr(window, '_vblank', None) else '4ms timer'}, "
          f"view {view.width()}x{view.height()}, screen {window.screen().refreshRate():.2f}Hz")

    vblanks: list[int] = []
    waiter = VBlankWaiter(lambda: vblanks.append(time.perf_counter_ns()))
    if not waiter.start(window.screen()):
        sys.exit("no vblank wait on this platform")
    paints: list[tuple[int, float]] = []
    original = type(view).paintEvent

    def stamped(event) -> None:
        original(view, event)
        paints.append((time.perf_counter_ns(), float(view.current_time)))

    view.paintEvent = stamped
    window.seek_audio(option("--at", float(window.state.document.hit_objects[0].time)))
    window._change_playback_speed(rate)
    window.toggle_playback()
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.0002)
    window.toggle_playback()
    waiter.stop()

    # The first second is load and warm-up, as measure_song_switch learned.
    start = paints[0][0] + 1_000_000_000
    vblanks = [v for v in vblanks if v > start]
    periods = [(b - a) / 1e6 for a, b in zip(vblanks, vblanks[1:])]
    period = statistics.median(periods)
    counts, shown = [], []
    index = 0
    for a, b in zip(vblanks, vblanks[1:]):
        landed = 0
        while index < len(paints) and paints[index][0] < b:
            if paints[index][0] >= a:
                landed += 1
            index += 1
        counts.append(landed)
        if index:
            shown.append(paints[index - 1][1])
    steps = [(b - a) / (period * rate) for a, b in zip(shown, shown[1:])]
    gaps = sorted((b[0] - a[0]) / 1e6 for a, b in zip(paints, paints[1:]) if a[0] > start)
    refreshes = len(counts)
    print(f"{refreshes} refreshes at {period:.3f}ms ({1000 / period:.2f}Hz), {rate}x")
    for n in (0, 1, 2):
        label = f"{n}+" if n == 2 else str(n)
        hit = sum(1 for c in counts if (c >= 2 if n == 2 else c == n))
        print(f"  {label} new frames: {hit:5d} ({100.0 * hit / refreshes:5.1f}%)")
    print(f"  step / refresh: mean {statistics.mean(steps):.3f}  stdev {statistics.pstdev(steps):.3f}  "
          f"min {min(steps):.2f}  max {max(steps):.2f}")

    def q(p: float) -> float:
        return gaps[min(len(gaps) - 1, int(len(gaps) * p))]

    buckets: dict[int, int] = {}
    for gap in gaps:
        buckets[round(gap)] = buckets.get(round(gap), 0) + 1
    common = sorted(buckets.items(), key=lambda item: -item[1])[:5]
    print(f"  frame gap: median {q(.5):.2f}ms  p5 {q(.05):.2f}  p95 {q(.95):.2f}  "
          f"most common {', '.join(f'{ms}ms x{n}' for ms, n in common)}")
    window.close()


if __name__ == "__main__":
    main()
