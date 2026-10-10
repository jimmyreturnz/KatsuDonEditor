"""How long the playhead sits still after Play, at each speed, and whether it
stalls once moving.

    python tools/measure_play_start.py <map.osu> [rate ...] [--editor] [--gameplay] [--repeats N]

Owner, 2026-10-09: "when I play while at slowdown, it frozen". Presses Play
the way the app does (`toggle_playback`) at each speed button's rate and
records, every frame for two seconds, where the views were told the playhead
is. Reports the wait until it first moves, and the longest stretch it then
held still -- the freeze. Real platform, window off screen, as
`measure_wheel_seek.py` (the offscreen plugin paints nothing).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import gui  # noqa: E402


def pump(app, seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.0005)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    repeats = 3
    if "--repeats" in sys.argv:
        repeats = int(sys.argv[sys.argv.index("--repeats") + 1])
        args = [a for a in args if a != str(repeats)]
    path = Path(args[0])
    rates = [float(a) for a in args[1:]] or [1.0, 0.75, 0.5, 0.25]

    app = QApplication.instance() or QApplication([])
    window = gui.MainWindow()
    window.setAttribute(Qt.WA_DontShowOnScreen)
    window.resize(1920, 1080)
    window.show()
    window._load_map_path(path, refresh_difficulties=True)
    pump(app, 1.5)  # decode
    if "--editor" in sys.argv:
        window._show_page(gui.PAGE_EDITOR)
        pump(app, 0.3)
    if "--gameplay" in sys.argv:
        # What song select opens since 2026-10-09: chart, SV and a gameplay
        # viewer, the one view that draws the skinned playfield.
        window._add_editor_view("gameplay", window.state.source_path)
        pump(app, 0.5)
    notes = sorted(note.time for note in window.state.document.hit_objects)
    start = notes[len(notes) // 3]

    frames: list[tuple[float, float]] = []
    original_render = window._render_gameplay_frame

    def render_hook() -> None:
        original_render()
        frames.append((time.perf_counter() * 1000.0, window.timeline.current_time))

    window._render_gameplay_frame = render_hook
    # The frame timer holds a bound method; reconnect it to the hook.
    try:
        window.gameplay_render_timer.timeout.disconnect()
    except (RuntimeError, TypeError):
        pass
    window.gameplay_render_timer.timeout.connect(render_hook)

    print(f"map: {path.name}  from {start:.0f}ms")
    print(f"{'rate':>5}  {'try':>3}  {'first move':>10}  {'longest still after':>19}  {'frames':>6}")
    for rate in rates:
        window._choose_speed(rate)
        pump(app, 0.3)
        for attempt in range(repeats):
            if window.player.playbackState() == QMediaPlayer.PlayingState:
                window.toggle_playback()
            pump(app, 0.3)
            window.seek_audio(start)
            pump(app, 0.3)
            frames.clear()
            pressed = time.perf_counter() * 1000.0
            window.toggle_playback()
            pump(app, 2.0)
            first = next((wall for wall, pos in frames if abs(pos - frames[0][1]) > 0.01), None) if frames else None
            longest, still_since, last_pos = 0.0, None, None
            for wall, pos in frames:
                if first is None or wall < first:
                    last_pos = pos
                    continue
                if last_pos is not None and abs(pos - last_pos) <= 0.01:
                    still_since = still_since or prev_wall
                    longest = max(longest, wall - still_since)
                else:
                    still_since = None
                last_pos, prev_wall = pos, wall
                prev_wall = wall
            wait = "never" if first is None else f"{first - pressed:8.0f}ms"
            print(f"{rate:5.2f}  {attempt + 1:3d}  {wait:>10}  {longest:16.0f}ms  {len(frames):6d}")
    if window.player.playbackState() == QMediaPlayer.PlayingState:
        window.toggle_playback()
    window.close()
    pump(app, 0.3)


if __name__ == "__main__":
    main()
