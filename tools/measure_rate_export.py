"""Whether a rate-changed map's notes still sit on the music.

    python tools/measure_rate_export.py <map.osu> [rate ...] [--pitch] [--out DIR]

For each rate: render the song (`audio.rate_render`), write it as osu! would
get it, decode that file back, and measure where the attack sits against the
*converted* chart's notes with `measure_note_vs_music.attack_offset_ms`. The
original's own number is printed first; **every rate must match it within a
millisecond or so**, because what a mapper timed to has to travel with the
note. The absolute number is not the point (it carries the detector's bias and
osu!'s +22ms) -- the difference is.

Also prints what `rate_change.time_map` had to bump to keep 1ms structures
apart (count, worst displacement against plain rounding), and the cost.
Files go to --out (default: a temp folder), never beside the map.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, __file__.rsplit("tools", 1)[0])
sys.path.insert(0, __file__.rsplit("tools", 1)[0] + "tools")

from PySide6.QtWidgets import QApplication  # noqa: E402

from audio import rate_render  # noqa: E402
from audio.audio_engine import CHANNELS, downmix_to_mono, frame_for_ms  # noqa: E402
from chart import rate_change  # noqa: E402
from chart.time_axis import osu_round  # noqa: E402
from measure_note_vs_music import attack_offset_ms  # noqa: E402
from measure_stretch_quality import decode  # noqa: E402
from osu_io.parser import parse_osu  # noqa: E402


def offset(pcm, times) -> str:
    value = attack_offset_ms(downmix_to_mono(pcm), [frame_for_ms(t) for t in times])
    return "none" if value is None else f"{value:+.1f}ms"


def main() -> None:
    args = sys.argv[1:]
    pitch = "--pitch" in args
    out_dir = Path(args[args.index("--out") + 1]) if "--out" in args else Path(tempfile.mkdtemp())
    positional = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--out")]
    osu_path, rates = positional[0], [float(r) for r in positional[1:]] or [1.1, 1.2, 1.3, 1.4, 1.5, 0.75]
    app = QApplication.instance() or QApplication(sys.argv[:1])
    doc = parse_osu(osu_path)
    pcm = decode(str(doc.source_path.parent / doc.audio_filename), app)
    circles = sorted({note.time for note in doc.hit_objects if note.is_circle})
    print(f"{doc.source_path.parent.name} [{doc.version}]  pitch={pitch}  out={out_dir}")
    print(f"  1x       attack={offset(pcm, circles)}  notes={len(circles)}")
    for rate in rates:
        started = time.perf_counter()
        rendered = rate_render.render(pcm.tobytes(), rate, pitch)
        render_s = time.perf_counter() - started
        written = rate_render.write_audio(rendered, out_dir / f"rate {rate:g}{' p' if pitch else ''}")
        write_s = time.perf_counter() - started - render_s
        back = decode(str(written), app)
        scaled = rate_change.scaled_document(doc, rate)
        times = sorted({note.time for note in scaled.hit_objects if note.is_circle})
        mapped = rate_change.time_map([n.time for n in doc.hit_objects] + [p.time for p in doc.timing_points], rate)
        bumped = [new - osu_round(t / rate) for t, new in mapped.items() if new != osu_round(t / rate)]
        shared = len(mapped) - len(set(mapped.values()))  # distinct source times now on one ms
        length = len(back) // CHANNELS - len(rendered) // (CHANNELS * 2)
        print(f"  {rate:<5g}x  attack={offset(back, times)} (1x/rate expected)  frames={len(back) // CHANNELS}  render={render_s:.1f}s "
              f"write={write_s:.1f}s ({written.suffix})  decoded-length-diff={length} frames  "
              f"bumped={len(bumped)} worst={max(bumped, default=0)}ms shared={shared}")


if __name__ == "__main__":
    main()
