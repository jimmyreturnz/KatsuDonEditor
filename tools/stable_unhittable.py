"""The circles osu! stable never reaches on a map, as the preview's autoplay
skips them -- for checking against stable's Auto by eye.

    python tools/stable_unhittable.py <map.osu>

Prints each as an osu! timestamp. See `chart/stable_window.py` for why: a
NaN slider derails stable's object-window search, and the circles it hides
are a run, not only the one before it. On Ruby My Dear - Spectacle (Mew)
[FeelsTaikoMan] that is 21, owner-checked in stable (2026-11-05).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from chart.stable_window import unhittable_circles  # noqa: E402
from osu_io.parser import parse_osu  # noqa: E402
from ui.gameplay_view import GameplayViewerView  # noqa: E402


def main() -> None:
    app = QApplication.instance() or QApplication([])  # noqa: F841
    view = GameplayViewerView()
    view.load_document(parse_osu(Path(sys.argv[1])))
    notes = view.notes
    lost = sorted(unhittable_circles(
        [(int(n.time), view._stable_end_time(n), n.is_circle) for n in notes], view._preempt_ms()))
    nan = sum(1 for n in notes if n.is_nan_slider)
    print(f"{len(notes)} objects, {nan} NaN sliders, PreEmpt {view._preempt_ms()}ms: "
          f"{len(lost)} circles stable never reaches")
    for k in lost:
        t = int(notes[k].time)
        print(f"  {t // 60000:02d}:{t // 1000 % 60:02d}:{t % 1000:03d} ({t})")


if __name__ == "__main__":
    main()
