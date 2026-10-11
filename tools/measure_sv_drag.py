"""What dragging a green line's SV vertically actually costs, per mouse-move.

    python tools/measure_sv_drag.py [point_count] [note_count]

Built for one specific report: dragging a green line up/down feels laggy, and
the SV value changes at 8 decimal places (`SV_DECIMALS`) while it moves.
Measures whether the decimal precision itself is the cost, or whether it is
`_edit_sv_point` -> `_refresh_difficulty_sv_views` running in full on *every*
mouse-move event -- which nothing used to skip even when the new value was the
same as the old one, and at 8 decimals of float precision it almost never was.

Generates a synthetic gimmick-sized map (many green lines, many notes) rather
than needing a real one on disk: the question is about the cost of the
refresh pipeline over N objects, which is the same whichever objects they are.

This measures the per-call cost, which is what tells you *whether cutting the
call count is worth doing at all* -- 17ms against a mouse that can deliver a
move every few ms is the actual lag. It does not exercise the fix itself
(`SVEditorView.mouseMoveEvent` quantizing to `SV_DRAG_STEP` and skipping an
unchanged value): that is cheap, in-process signal-emission logic with no
audio device or paint cost of its own, and is covered directly by
`tests/test_sv_editor.py`'s `test_a_drag_quantizes_to_the_step_and_skips_unchanged_moves`.
Both calls below still go through `_edit_sv_point` directly and so both cost
about the same per call -- what the fix changes is how many of these 3000-point,
~17ms calls a real drag actually makes, not what any one of them costs.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from time import perf_counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication

from ui import gui

POINT_COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
NOTE_COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 3000


def _make_map(path: Path) -> None:
    lines = [
        "osu file format v14", "", "[General]", "AudioFilename: audio.mp3",
        "Mode: 1", "", "[Metadata]", "Title:Stress", "Artist:Stress",
        "Creator:tool", "Version:Stress", "", "[Difficulty]",
        "HPDrainRate:5", "CircleSize:5", "OverallDifficulty:5", "ApproachRate:5",
        "SliderMultiplier:1.4", "SliderTickRate:1", "", "[TimingPoints]",
        "0,500,4,1,0,100,1,0",
    ]
    for i in range(POINT_COUNT):
        # Spread across the map at 200ms apart, inherited (green) points --
        # what a gimmick's own SV layer looks like, densely.
        lines.append(f"{200 + i * 200},-100,4,1,0,100,0,0")
    lines.append("")
    lines.append("[HitObjects]")
    for i in range(NOTE_COUNT):
        lines.append(f"256,192,{200 + i * 200},1,0,0:0:0:0:")
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


app = QApplication.instance() or QApplication([])
window = gui.MainWindow()
window.resize(1920, 1080)
window.show()

with tempfile.TemporaryDirectory() as tmp:
    directory = Path(tmp)
    (directory / "audio.mp3").write_bytes(b"\x00")
    map_path = directory / "stress.osu"
    _make_map(map_path)
    window._load_map_path(map_path, refresh_difficulties=True)
    app.processEvents()

    window.song_list.setCurrentRow(0)
    window._open_selected_difficulty()
    app.processEvents()

    document = window.state.document
    green_points = [p for p in document.timing_points if p.inherited]
    print(f"map: {len(document.timing_points)} timing points, "
          f"{len(green_points)} green, {len(document.hit_objects)} notes")

    target_uid = green_points[len(green_points) // 2].uid

    def run(values, label):
        t0 = perf_counter()
        for value in values:
            window._edit_sv_point(map_path, target_uid, value)
            app.processEvents()
        total = (perf_counter() - t0) * 1000
        print(f"{label}: {len(values)} calls, {total:.1f}ms total, "
              f"{total / len(values):.2f}ms/call")

    # A drag from SV 1.0 to SV 3.0 sampled every pixel a mouse-move might
    # actually deliver -- one distinct float per call, 8 decimals of it,
    # exactly what `_y_to_sv` hands `_edit_sv_point` today.
    fine = [1.0 + i * (2.0 / 300) for i in range(300)]
    run(fine, "300 distinct values (today's behaviour)")

    # The same range, but only 0.01-quantized values are ever distinct --
    # which is what the report is asking for.
    coarse_seen = set()
    coarse = []
    for value in fine:
        q = round(value, 2)
        if q not in coarse_seen:
            coarse_seen.add(q)
            coarse.append(q)
    run(coarse, "0.01-quantized, deduplicated")

window.close()
