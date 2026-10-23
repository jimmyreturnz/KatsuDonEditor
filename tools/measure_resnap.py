"""What Resnap would move on a map, and against which grid.

    python tools/measure_resnap.py <map.osu> [map.osu ...]

Resnap puts every object within RESNAP_TOLERANCE_MS of its own grid onto it.
That is only right if the grid is the map's real one. This runs `resnap_time`
against two grids per map and counts the objects each would move:

- `file`: every red line in the file, which is what an Editor page view snaps
  to (it has no base snapshot);
- `clean`: only the red lines that are not gimmick lines (60000 BPM and
  friends, 1ms-beat squashes), which is the grid the mapper actually timed to.

A map whose `file` column moves objects its `clean` column does not is being
taken apart by its own gimmick lines.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

from chart.time_axis import resnap_time  # noqa: E402
from osu_io.parser import parse_osu  # noqa: E402


def moves(points, notes):
    moved = Counter()
    for note in notes:
        target = resnap_time(points, note.time)
        if target is not None and int(target) != note.time:
            moved[int(target) - int(note.time)] += 1
    return moved


def main() -> None:
    for name in sys.argv[1:]:
        document = parse_osu(Path(name))
        points = sorted((p for p in document.timing_points if p.uninherited), key=lambda p: p.time)
        reds = [p for p in points if p.uninherited]
        beats = Counter(round(p.beat_length, 3) for p in reds)
        main_beat = beats.most_common(1)[0][0]
        # Anything far faster than the main beat is a gimmick line, not timing.
        clean = [p for p in points if p.beat_length >= main_beat / 8]
        notes = [n for n in document.hit_objects]
        file_moves, clean_moves = moves(points, notes), moves(clean, notes)
        print(f"{Path(name).stem}: {len(notes)} objects, {len(reds)} red lines, "
              f"{len(reds) - sum(1 for p in clean if p.uninherited)} of them gimmick")
        print(f"  file grid moves  {sum(file_moves.values()):5}  {dict(sorted(file_moves.items()))}")
        print(f"  clean grid moves {sum(clean_moves.values()):5}  {dict(sorted(clean_moves.items()))}")


if __name__ == "__main__":
    main()
