"""Measure the cost of one playback frame, the way stutter is actually caused.

    python tools/profile_playback.py <a real .osu> [frames] [--gimmick]
                                     [--gameplay] [--skin NAME] [--profile]
                                     [--at MS] [--playing RATE] [--view-opacity N]
                                     [--focused] [--no-motion] [--autoplay STYLE]

--autoplay plays the gameplay preview's input drum in STYLE (autoplay.STYLES,
or "off"); without it the run uses whatever the settings last saved.

--playing plays the song for real at RATE and reports the gaps between the
app's own rendered frames instead of timing synthetic ones.

--at starts the run at a millisecond rather than at the first hit object. A
gimmick is a *section* of a map, and the first note is almost never in it.

The editor renders at 120fps, so every frame has an 8.33ms budget: the clock
advance, the hitsound scan, and a synchronous repaint of every open view. A
frame that overruns is a dropped frame, and dropped frames are the stutter.

Reports the distribution rather than the mean -- stutter is the tail. With
--profile it also prints the cumulative-time hot list for the same run.
"""
from __future__ import annotations

import cProfile
import os
import pstats
import sys
from pathlib import Path
from time import perf_counter, sleep

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from ui import gui

path = Path(sys.argv[1])
frames = int(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else 400
profile = "--profile" in sys.argv

app = QApplication.instance() or QApplication([])
window = gui.MainWindow()
# Paint cost scales with pixels, and the offscreen platform's default screen
# is far smaller than a real maximized window -- measuring at 758x180 would
# understate every number here by the ratio of the areas.
window.resize(1920, 1080)
window.show()
window._load_map_path(path, refresh_difficulties=True)
app.processEvents()

if "--gimmick" in sys.argv:
    # The heavy layout: the gimmick page opens a band per layer, so the frame
    # cost is multiplied by however many are on screen. Entering it normally
    # asks a question in a dialog; answer it the way the tests do.
    from unittest.mock import patch

    class _Entry:
        CREATE = gui.GimmickEntryDialog.CREATE
        USE_CURRENT = gui.GimmickEntryDialog.USE_CURRENT
        CANCEL = gui.GimmickEntryDialog.CANCEL

        def __init__(self, version, parent=None, references=None): pass
        def exec(self): return 1
        def selected_action(self): return self.USE_CURRENT
        def selected_reference(self): return None
        def deleteLater(self): pass

    with patch.object(gui, "GimmickEntryDialog", _Entry):
        entered = window._enter_gimmick_page()
    if entered:
        window._show_page(gui.PAGE_GIMMICK)
    print(f"     gimmick page entered: {entered}")
    app.processEvents()

if "--gameplay" in sys.argv:
    # The gameplay preview is the only view that draws the skin's playfield --
    # a full-width bar blit, a tiled scrolling background and a per-note kiai
    # silhouette -- and none of that is measured by a run without one open.
    window._add_editor_view("gameplay", path)
    app.processEvents()

if "--all-charts" in sys.argv:
    # "Open a chart for every difficulty" multiplies the per-frame broadcast:
    # _render_gameplay_frame calls set_time(force=True) on every entry of
    # _chart_views and each one repaints. One or two views is what every
    # earlier run here measured, so the cost of five to ten is a number this
    # harness did not have.
    for _label, difficulty in window._taiko_difficulty_entries():
        if any(
            getattr(frame, "difficulty_path", None) == difficulty
            and frame.view_type == "chart"
            for frame in window._editor_views
        ):
            continue
        window._add_editor_view("chart", difficulty)
    app.processEvents()
    charts = len(window._chart_views)
    print(f"     {charts} chart views open")

if "--skin" in sys.argv:
    # The skinned path costs more than the built-in one (blits instead of
    # cached ellipses), so "is it fast enough" has to be asked with a skin on.
    name = sys.argv[sys.argv.index("--skin") + 1]
    window.settings.set_value("appearance/skin", name)
    window._apply_appearance_settings()
    print(f"     skin: {window.skin.name or '(built-in)'}")
    app.processEvents()

if "--autoplay" in sys.argv:
    style = sys.argv[sys.argv.index("--autoplay") + 1]
    window._set_autoplay_choice(None if style == "off" else style, "1234", False)
    print(f"     autoplay: {style}")
    app.processEvents()

if "--focused" in sys.argv:
    # The focused view carries the living rim (motion.paint_living_rim), the
    # only per-frame motion on these pages; nothing is focused until a click.
    frames_on_page = [*window._editor_views, *window._gimmick_views]
    window._mark_focused_view(frames_on_page[0].content)
    print("     focused: first view (living rim on)")
    app.processEvents()

if "--no-motion" in sys.argv:
    # What "Animation effects" off does: the rim stands still, its clock stops.
    from ui import motion
    motion.reduced_motion = lambda: True
    motion.RimClock.shared()._timer.stop()
    print("     motion: off")

if "--view-opacity" in sys.argv:
    # Under 100% every lane gives up WA_OpaquePaintEvent, so each frame also
    # repaints the frame sheet and the page's backdrop beneath it.
    percent = int(sys.argv[sys.argv.index("--view-opacity") + 1])
    window._apply_view_opacity(percent)
    print(f"     view opacity: {percent}%")
    app.processEvents()

# The track is still decoding when the window is ready, and the decode
# delivers every buffer through Python on the engine thread -- array building
# and a mono downmix, all holding the GIL -- while each one also re-announces
# the growing duration to every timing bar. For about a second after a load,
# every frame here competes with that. Measured: with six charts open, frames
# 0-95 in a row ran over budget and 1 of the next 205 did, and every figure this
# harness printed before this wait included that second -- "34% over budget"
# for six charts was the load, not the charts. `--no-settle` measures it on
# purpose.
#
# Settled on decoded *samples*, not on `player.duration()`: the decoder reports
# the full duration from the file's metadata almost at once, so waiting for the
# duration to stop changing waited for nothing while the decode ran on. Read
# off the engine's own buffer -- a private attribute, and fine for a harness,
# which exists to look at exactly what the app does not expose.
if "--no-settle" not in sys.argv:
    began = perf_counter()
    from audio import audio_engine
    engine = window.player._engine
    last, last_change = -1, perf_counter()
    while perf_counter() - began < 60.0:
        app.processEvents()
        sleep(0.01)
        decoded = len(engine._mono)
        if decoded != last:
            last, last_change = decoded, perf_counter()
        elif perf_counter() - last_change >= 0.5:
            break
    print(f"     audio decoded: {last / audio_engine.SAMPLE_RATE:.1f}s of track, "
          f"waited {perf_counter() - began:.1f}s")

state = window.state
print(f"map: {path.name}")
print(f"     {len(state.document.hit_objects)} hit objects, "
      f"{len(state.document.timing_points)} timing points")

views = []
for name in ("_chart_views", "_sv_views", "_gameplay_views", "_density_views", "_timing_bars"):
    group = list(getattr(window, name, ()) or ())
    views.append((name, group))
    print(f"     {name}: {len(group)} ({sum(1 for v in group if v.isVisible())} visible)")


if "--drag" in sys.argv:
    # A view carried by its header (motion.ReorderDrag), down the stack and
    # back for `frames` frames: the gaps between the overlay's own paints and
    # what each costs. The owner asked for this one to be "sooooo smooth".
    from PySide6.QtCore import QEventLoop, QPoint, QTimer
    frames_on_page = [f for f in (*window._gimmick_views, *window._editor_views) if f.isVisible()]
    frame = frames_on_page[0]
    start = frame.header.mapToGlobal(QPoint(10, 10))
    window._begin_view_drag(frame, start)
    drag = window._view_drag
    paints, stamps = [], []
    original_paint = type(drag).paintEvent

    def timed_paint(event, drag=drag):
        began = perf_counter()
        original_paint(drag, event)
        paints.append((perf_counter() - began) * 1000.0)
        stamps.append(began)

    drag.paintEvent = timed_paint
    span = drag.bounds.height()
    step = [0]

    def carry() -> None:
        # A triangle wave over the whole stack, one pass each way.
        t = step[0] / max(1, frames - 1)
        drag.follow(start.y() + round(span * (1 - abs(1 - 2 * t)) * 0.9))
        step[0] += 1
        if step[0] >= frames:
            pointer.stop()
            loop.quit()

    pointer = QTimer()
    pointer.setTimerType(gui.Qt.PreciseTimer)
    pointer.setInterval(8)
    pointer.timeout.connect(carry)
    loop = QEventLoop()
    pointer.start()
    loop.exec()
    drag.order = list(range(len(drag.items)))  # nothing moved for real
    drag.drop()
    gaps = sorted((b - a) * 1000.0 for a, b in zip(stamps, stamps[1:]))
    costs = sorted(paints)

    def q(values, p): return values[min(len(values) - 1, int(len(values) * p))]
    print(f"\ndrag over {len(drag.items)} views ({drag.bounds.width()}x{span}) for {frames} pointer moves:")
    print(f"  paint       median {q(costs, .5):6.2f}ms   p95 {q(costs, .95):6.2f}ms   max {costs[-1]:6.2f}ms")
    print(f"  frame gap   median {q(gaps, .5):6.2f}ms   p95 {q(gaps, .95):6.2f}ms   max {gaps[-1]:6.2f}ms")
    window.close()
    sys.exit(0)


if "--playing" in sys.argv:
    # Every run below this block renders with the song *stopped*, so the
    # audio thread's time-stretch -- pure Python, holding the GIL -- has never
    # been in the same measurement as a frame. Here the song really plays and
    # the app's own 4ms PreciseTimer drives its own _render_gameplay_frame.
    # The timer already holds a bound method, so replacing the attribute would
    # time nothing (tests/test_window_lifecycle.py); reconnect the signal.
    from PySide6.QtCore import QEventLoop, QTimer
    rate = float(sys.argv[sys.argv.index("--playing") + 1])
    seconds = frames / 120.0
    stamps = []
    timer = window.gameplay_render_timer
    timer.timeout.disconnect()

    def timed_frame() -> None:
        before = window._last_broadcast_position
        began = perf_counter()
        window._render_gameplay_frame()
        if window._last_broadcast_position != before:
            # update() only schedules the paint; it lands in this same
            # event-loop pass, so the gap between frames is what shows a drop.
            stamps.append(began)

    timer.timeout.connect(timed_frame)
    at = float(state.document.hit_objects[0].time)
    if "--at" in sys.argv:
        at = float(sys.argv[sys.argv.index("--at") + 1])
    window.seek_audio(at)
    window._change_playback_speed(rate)
    window.toggle_playback()
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()
    window.toggle_playback()
    timeline = [((a - stamps[0]), (b - a) * 1000.0) for a, b in zip(stamps, stamps[1:])]
    gaps = sorted(gap for _at, gap in timeline)

    def q(values, p): return values[min(len(values) - 1, int(len(values) * p))]
    print(f"\nplaying at {rate}x for {seconds:.1f}s from {at:.0f} ms: "
          f"{len(stamps)} frames ({len(stamps) / seconds:.1f}/s, 120 wanted)")
    # Only the gap means anything here: update() queues the paint for later in
    # the same loop pass, so timing the broadcast itself measures set_time and
    # none of the painting.
    # The render timer ticks every 4ms, so a healthy gap is 8 or 12ms, never
    # 8.33 -- a gap past 12.5 is a frame that was due and did not happen.
    print(f"  frame gap   median {q(gaps, .5):6.2f}ms   p95 {q(gaps, .95):6.2f}ms   "
          f"p99 {q(gaps, .99):6.2f}ms   max {gaps[-1]:6.2f}ms")
    late = [(t, g) for t, g in timeline if g > 12.5]
    print(f"  dropped (gap > 12.5ms): {len(late)}/{len(gaps)} ({100.0 * len(late) / len(gaps):.1f}%)")
    for t, g in sorted(late, key=lambda item: -item[1])[:6]:
        print(f"    {g:6.2f}ms at {t:5.2f}s")
    window.close()
    sys.exit(0)


def render_one(position: float) -> None:
    """One frame's worth of work, painted synchronously so it is really done."""
    for view in window._chart_views:
        view.set_time(position, force=True)
    for view in window._sv_views:
        view.set_time(position, force=True)
    for view in window._gameplay_views:
        view.set_time(position, force=True)
    for view in window._density_views:
        view.set_time(position)
        view.set_viewport(position, window.timeline.window_ms)
    for bar in window._timing_bars:
        bar.set_time(position)
        bar.set_viewport(position, window.timeline.window_ms)
    # update() + one processEvents, not repaint() per widget: the app never
    # forces a synchronous per-widget backing-store flush, and measuring that
    # way charges every view its own compositing pass instead of the single
    # coalesced one a real frame gets.
    for _name, group in views:
        for view in group:
            if view.isVisible():
                view.update()
    app.processEvents()


start = float(state.document.hit_objects[0].time)
if "--at" in sys.argv:
    start = float(sys.argv[sys.argv.index("--at") + 1])
print(f"     from {start:.0f} ms")
step = 1000.0 / 120.0

render_one(start)  # warm up caches, JIT-free but Qt has its own first-paint cost

times = []
if profile:
    profiler = cProfile.Profile()
    profiler.enable()
for index in range(frames):
    at = start + index * step
    began = perf_counter()
    render_one(at)
    times.append((perf_counter() - began) * 1000.0)
if profile:
    profiler.disable()

# Per view: which of them actually costs the frame. Same positions, one view
# at a time, so an expensive one cannot hide behind the others.
print()
print("per view, median paint of 120 frames:")
rows = []
for _name, group in views:
    for view in group:
        if not view.isVisible():
            continue
        each = []
        for index in range(120):
            at = start + index * step
            if hasattr(view, "set_viewport"):
                view.set_time(at)
                view.set_viewport(at, window.timeline.window_ms)
            else:
                view.set_time(at, force=True)
            began = perf_counter()
            view.repaint()
            each.append((perf_counter() - began) * 1000.0)
        each.sort()
        rows.append((each[len(each) // 2], type(view).__name__,
                     view.width(), view.height(), getattr(view, "gimmick_layer", "")))
for median, name, w, h, layer in sorted(rows, reverse=True):
    print(f"  {median:6.2f}ms  {name}{'/' + layer if layer else ''}  {w}x{h}")

times.sort()
def pct(p): return times[min(len(times) - 1, int(len(times) * p))]
over = sum(1 for t in times if t > 8.33)
print(f"\n{frames} frames, 8.33ms budget (120fps)")
print(f"  median {pct(0.5):6.2f}ms   p95 {pct(0.95):6.2f}ms   p99 {pct(0.99):6.2f}ms   max {times[-1]:6.2f}ms")
print(f"  over budget: {over}/{frames} ({100.0 * over / frames:.1f}%)")

if profile:
    print()
    stats = pstats.Stats(profiler)
    stats.sort_stats("cumulative").print_stats(14)
    stats.sort_stats("tottime").print_stats(16)
    if "--callers" in sys.argv:
        stats.print_callers("timing.py:66|timing.py:320|uninherited_points")
