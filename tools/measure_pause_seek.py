"""What a timeline-bar drag followed by a pause does to the audio.

    python tools/measure_pause_seek.py <map.osu> [--repeats N] [--paused-drag]

The report (owner, 2026-10-18): drag the timeline bar, pause, and *sometimes*
the audio is muted, and on unpause it plays from the last pause position while
the playhead is somewhere else.

Plays the map for real (real `TrackPlayer`, real event loop, real sink), drags
the Editor page's timing bar the way a mouse does -- one `_seek` per move, 15ms
apart -- and pauses `delay` ms after the last move, for each delay in DELAYS:
inside and outside `SEEK_COALESCE_MS`, where a parked seek may still be armed.
`--paused-drag` drags while already paused instead.

Per trial it prints where the window's playhead is and where the engine's audio
is, after the pause and again after resuming, plus how many frames the engine
wrote to the sink in the `RESUME_S` after Play. The two positions must agree,
and a resume that writes nothing is the mute.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from audio.audio_engine import SAMPLE_RATE  # noqa: E402
from ui import gui  # noqa: E402

DELAYS = (0.0, 10.0, 30.0, 60.0, 120.0)
MOVE_MS = 15.0
MOVES = 12
RESUME_S = 0.4


def pump(app, seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.001)


def engine_ms(engine) -> float:
    return engine._stretcher.source_frame / SAMPLE_RATE * 1000.0


def drag(app, bar, start_ms: float, end_ms: float) -> None:
    width = max(1, bar.width())
    bar._last_seek_ms = None
    for index in range(1, MOVES + 1):
        target = start_ms + (end_ms - start_ms) * index / MOVES
        bar._seek(target / max(1, bar.duration_ms) * width)
        pump(app, MOVE_MS / 1000.0)


def trial(app, window, bar, start: float, target: float, delay: float, paused_drag: bool) -> dict:
    engine = window.player._engine
    decoded = len(engine._mono) / SAMPLE_RATE * 1000.0
    if window.player.playbackState() == QMediaPlayer.PlayingState:
        window.toggle_playback()
    pump(app, 0.2)
    window.seek_audio(start)
    pump(app, 0.2)
    window.toggle_playback()
    pump(app, 0.8)
    if paused_drag:
        window.toggle_playback()
        pump(app, delay / 1000.0)
        drag(app, bar, start, target)
    else:
        drag(app, bar, start, target)
        pump(app, delay / 1000.0)
        window.toggle_playback()
    pump(app, 0.4)
    after_pause = (window._predicted_audio_position(), engine_ms(engine),
                   engine._pending_seek_ms is not None, engine._sink is not None)
    written = engine._written
    sink_before = id(engine._sink)
    window.toggle_playback()
    pump(app, RESUME_S)
    resumed_window = window._predicted_audio_position()
    resumed_engine = engine._position_ms() if engine._sink is not None else engine_ms(engine)
    wrote = engine._written - written if id(engine._sink) == sink_before else engine._written
    return {
        "target": target, "pause_window": after_pause[0], "pause_engine": after_pause[1],
        "parked": after_pause[2], "sink": after_pause[3],
        "resume_window": resumed_window, "resume_engine": resumed_engine, "wrote": wrote,
        "decoded": decoded,
    }


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    repeats = 3
    if "--repeats" in sys.argv:
        repeats = int(sys.argv[sys.argv.index("--repeats") + 1])
        args = [a for a in args if a != str(repeats)]
    paused_drag = "--paused-drag" in sys.argv
    # How long after loading the first trial starts. The track decodes in the
    # background (an .ogg at ~100x realtime: 2.8s for a 5-minute song), and a
    # seek past what is decoded so far has nothing to play.
    settle = 1.5
    if "--settle" in sys.argv:
        settle = float(sys.argv[sys.argv.index("--settle") + 1])
        args.remove(sys.argv[sys.argv.index("--settle") + 1])
    path = Path(args[0])

    app = QApplication.instance() or QApplication([])
    window = gui.MainWindow()
    window.setAttribute(Qt.WA_DontShowOnScreen)
    window.resize(1920, 1080)
    window.show()
    window._load_map_path(path, refresh_difficulties=True)
    pump(app, settle)
    window._show_page(gui.PAGE_EDITOR)
    bar = window.timing_bar
    # The map's own length: mid-decode the player reports only what is decoded.
    duration = float(max(note.time for note in window.state.document.hit_objects))
    start, target = duration * 0.2, duration * 0.9
    print(f"map: {path.name}  drag {start:.0f} -> {target:.0f}ms, {MOVES} moves {MOVE_MS:.0f}ms apart, "
          f"{'paused' if paused_drag else 'playing'} drag")
    print(f"{'delay':>6} {'#':>2}  {'pause: window':>13} {'engine':>8} {'parked':>6} {'sink':>4}  "
          f"{'resume: window':>14} {'engine':>8} {'apart':>7} {'wrote':>6}")
    bad = 0
    for delay in DELAYS:
        for repeat in range(repeats):
            r = trial(app, window, bar, start, target, delay, paused_drag)
            apart = r["resume_engine"] - r["resume_window"]
            silent = r["wrote"] == 0
            flag = "  <-- apart" if abs(apart) > 20 else ""
            # Silent while the decode has not reached the target is waiting,
            # not a bug -- as long as the playhead waits too (no "apart").
            if silent and r["decoded"] < r["target"]:
                flag += "  (waiting on decode)"
            elif silent:
                flag += "  <-- silent"
            bad += "<--" in flag
            print(f"{delay:>6.0f} {repeat:>2}  {r['pause_window']:>13.0f} {r['pause_engine']:>8.0f} "
                  f"{str(r['parked']):>6} {str(r['sink']):>4}  {r['resume_window']:>14.0f} "
                  f"{r['resume_engine']:>8.0f} {apart:>7.0f} {r['wrote']:>6}  decoded {r['decoded']:.0f}{flag}")
    print(f"bad trials: {bad} of {len(DELAYS) * repeats}")
    if window.player.playbackState() == QMediaPlayer.PlayingState:
        window.toggle_playback()
    window.close()
    pump(app, 0.3)


if __name__ == "__main__":
    main()
