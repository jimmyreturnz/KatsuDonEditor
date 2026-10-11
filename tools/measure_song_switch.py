"""Where the time goes when song select moves to another song.

    python tools/measure_song_switch.py [switches] [--seed N]

Loads the real library from the saved index (the scan is not run), then
selects `switches` random songs one after another, letting each settle long
enough for its chart preview and audio to start. Every phase is timed --
"GUI:" ones on the GUI thread, the rest wherever they run -- and a 1ms timer
records the gaps in the event loop: a gap over a frame is a frame the chart
preview did not draw, which is what "it stutters before loading the chart"
is. The worst gap of each switch is then put down to what ran inside it.

Run it twice: the first run reads every .osu cold, which is what browsing to
a song you have not opened lately does; the second finds them in the OS
cache. Hashing was 62ms of a switch cold and 0.6ms warm.
"""
from __future__ import annotations

import os
import random
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median
from time import perf_counter, sleep

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from ui import gui
from ui import song_select

switches = int(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else 20
seed = int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 1

timings: dict[str, list[float]] = defaultdict(list)
current: dict[str, float] = defaultdict(float)
# (label, start, end) of every timed call, to say what ran inside a gap.
spans: list[tuple[str, float, float]] = []


def timed(owner, name: str, label: str) -> None:
    original = getattr(owner, name)

    def wrapper(*args, **kwargs):
        start = perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            end = perf_counter()
            current[label] += (end - start) * 1000
            spans.append((label, start, end))

    setattr(owner, name, wrapper)


# On the classes, before the window exists: the settle timers connect bound
# methods at construction, and a wrapper set afterwards is never called.
timed(song_select.LibraryPageController, "_song_selected", "GUI: song_selected (all of it)")
timed(song_select.LibraryPageController, "_load_chart_preview", "GUI: load chart (all of it)")
timed(song_select.LibraryPageController, "start_preview", "GUI: start audio")
if hasattr(song_select.LibraryPageController, "_banner_art_decoded"):
    timed(song_select.LibraryPageController, "_banner_art_decoded", "GUI: art to pixmap")
timed(song_select, "parse_osu", "GUI: parse .osu")
timed(song_select, "heard_tempo", "GUI: read red lines")
timed(song_select.GameplayViewerView, "refresh_notes", "GUI: preview refresh_notes")
# Before the fix these ran on the GUI thread; after it, on a worker.
timed(song_select.osu_db, "file_md5", "md5 of every difficulty")
timed(song_select, "decode_background_image" if hasattr(song_select, "decode_background_image") else "decode_background",
      "decode background")
# Per frame, so a gap can be put down to painting rather than to loading.
timed(song_select.SongBanner, "paintEvent", "paint: banner")
timed(song_select.GameplayViewerView, "paintEvent", "paint: chart preview")
timed(song_select.SongRowDelegate, "paint", "paint: song rows")
timed(song_select.DifficultyRowDelegate, "paint", "paint: difficulty rows")

app = QApplication.instance() or QApplication([])
# main()'s identity, or AppDataLocation is not where the saved index lives.
app.setOrganizationName(gui.ORGANIZATION_NAME)
app.setApplicationName(gui.APPLICATION_NAME)
window = gui.MainWindow()
# Silent: the run plays every preview it opens, at nobody.
_int_value = window.settings.int_value
window.settings.int_value = lambda key, default=0: 0 if key == "audio/music_volume" else _int_value(key, default)
window.resize(1920, 1080)
window.show()
library = window._library
library.start_scan()
library.scan_timer.stop()  # the saved index only: the walk would be its own stutter
app.processEvents()

gaps: list[tuple[float, float, float]] = []  # (ms, from, to)
last = [perf_counter()]


def beat() -> None:
    now = perf_counter()
    gaps.append(((now - last[0]) * 1000, last[0], now))
    last[0] = now


ticker = QTimer()
ticker.setTimerType(Qt.PreciseTimer)
ticker.setInterval(1)
ticker.timeout.connect(beat)
ticker.start()


def settle(seconds: float) -> None:
    end = perf_counter() + seconds
    while perf_counter() < end:
        app.processEvents()
        sleep(0.001)


rows = [row for row in range(library.song_list.count())
        if library.song_list.item(row).data(Qt.UserRole) is not None]
print(f"{len(rows)} songs listed; {switches} switches, seed {seed}")
if not rows:
    sys.exit("no songs in the saved index")
picks = random.Random(seed).sample(rows, min(switches, len(rows)))
library.song_list.setCurrentRow(picks[-1])
settle(2.0)
# The floor: the same windows with a song playing and nothing switching. A
# gap this size is the page's ordinary frame, not something a switch did.
idle: list[float] = []
for _ in range(5):
    gaps.clear()
    last[0] = perf_counter()
    settle(1.2)
    idle.append(max(gaps)[0] if gaps else 0.0)
print(f"no switch, song playing: longest gap per window median {median(idle):.1f}ms, max {max(idle):.1f}ms")
worst: list[float] = []
blame: dict[str, float] = defaultdict(float)
for row in picks:
    current.clear()
    gaps.clear()
    spans.clear()
    last[0] = perf_counter()
    library.song_list.setCurrentRow(row)
    settle(1.2)
    for label, ms in current.items():
        timings[label].append(ms)
    if not gaps:
        continue
    gap_ms, start, end = max(gaps)
    worst.append(gap_ms)
    # What ran inside the worst gap; the rest is Qt and the media backend,
    # which no Python wrapper sees.
    inside = defaultdict(float)
    for label, a, b in spans:
        overlap = min(b, end) - max(a, start)
        # GUI-thread work only: a worker running alongside a gap is not its
        # cause (decoding measured to leave the GIL free: 0.7ms worst stall).
        if overlap > 0 and label.startswith(("GUI:", "paint:")) and "all of it" not in label:
            inside[label] += overlap * 1000
    for label, ms in inside.items():
        blame[label] += ms
    blame["(native: Qt, media, unseen)"] += max(0.0, gap_ms - sum(inside.values()))

library.stop_preview()
print(f"\n{'phase':30} {'median':>8} {'p90':>8} {'max':>8}   (ms per switch)")
for label, values in sorted(timings.items(), key=lambda item: -median(item[1])):
    values.sort()
    p90 = values[min(len(values) - 1, int(len(values) * 0.9))]
    print(f"{label:30} {median(values):8.1f} {p90:8.1f} {values[-1]:8.1f}")
print()
print("what the worst gap of each switch was spent on, summed over the run (ms):")
for label, ms in sorted(blame.items(), key=lambda item: -item[1]):
    print(f"  {label:34} {ms:8.1f}")
worst.sort()
print(f"\nlongest event-loop gap per switch: median {median(worst):.1f}ms, "
      f"p90 {worst[min(len(worst) - 1, int(len(worst) * 0.9))]:.1f}ms, max {worst[-1]:.1f}ms "
      f"(a frame is 8.3ms at 120Hz)")
