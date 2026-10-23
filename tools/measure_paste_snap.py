"""Whether pasted notes land on their own snap, on the Editor page.

    python tools/measure_paste_snap.py <map.osu> [map.osu ...] [--trials N]

Copies random runs of 8 circles at the view's divisor, pastes each at a random
playhead, and asks Resnap about every pasted note and the note it was copied
from: `resnap_time(t) - t` is how far each sits from its own gridline as osu!
would write it. **`changed` must be 0**: a pasted note has to be exactly as far
off its snap as its source was. Against the grid alone this was wrong -- 65 of
Hemisphere's own 731 notes are a millisecond off already, and a paste is
supposed to carry that, not fix it.

Hemisphere still reports `changed 40` of 480, and every one of them is a
paste whose *first* note is such a stray (2026-10-24): the anchor lands
exactly on the playhead's snap, which straightens it and moves the rest of the
run by the same millisecond. That is paste's specified behaviour (pinned by
`test_copy_and_paste_notes_at_the_playhead` and three more), not this bug.

Pasted notes are found by uid, not by time: a paste over existing notes
replaces them, and a before/after diff of times cannot see those at all.
"""
from __future__ import annotations

import os
import random
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, __file__.rsplit("tools", 1)[0])

from PySide6.QtWidgets import QApplication  # noqa: E402

from chart.time_axis import resnap_time  # noqa: E402
from ui import gui  # noqa: E402


def measure(app, path: Path, trials: int) -> None:
    window = gui.MainWindow()
    window.resize(1920, 1080)
    window.show()
    window._load_map_path(path, refresh_difficulties=True)
    view = next(f for f in window._editor_views if f.view_type == "chart").chart_view
    window._active_chart_view = view
    state = window.state
    circles = sorted((n for n in state.document.hit_objects if n.is_circle), key=lambda n: n.time)
    rnd = random.Random(1)
    off = Counter()
    changed = 0
    for _ in range(trials):
        start = rnd.randrange(len(circles) - 8)
        run = circles[start:start + 8]
        view.selected = {n.original_index for n in run}
        window._copy_notes()
        view.current_time = rnd.uniform(circles[0].time, circles[-1].time)
        before = {n.uid for n in state.document.hit_objects}
        window._paste_notes()
        pasted = sorted((n for n in state.document.hit_objects if n.uid not in before), key=lambda n: n.time)

        def off_snap(time):
            target = resnap_time(view.snap_points, time)
            return "off grid" if target is None else int(target) - int(time)

        for source, note in zip(run, pasted):
            off[off_snap(note.time)] += 1
            changed += off_snap(note.time) != off_snap(source.time)
        state.history.undo(state)
    print(f"{path.stem[:60]}: divisor {view.snap_divisor}, {sum(off.values())} pasted, "
          f"changed {changed}  off their snap {dict(sorted(off.items(), key=str))}")
    window.close()
    app.processEvents()


def main() -> None:
    args = sys.argv[1:]
    trials = 30
    if "--trials" in args:
        index = args.index("--trials")
        trials = int(args[index + 1])
        del args[index:index + 2]
    app = QApplication.instance() or QApplication([])
    for name in args:
        measure(app, Path(name), trials)


if __name__ == "__main__":
    main()
