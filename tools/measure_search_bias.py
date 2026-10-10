"""Where does the WSOLA splice search actually land, on real music?

    python tools/measure_search_bias.py <audio file> [audio file ...] [rate ...]

`grain_offset_ms` corrects for two things: the sawtooth from emitting a whole
grain of unstretched source (`SEQUENCE_FRAMES * (1 - rate)`, a real centring
problem with an exact answer), and the forward displacement the splice search
adds on top of that (`SEARCH_FRAMES`, one candidate of many in `[low, high)`).
The second term was never measured -- it assumed the search lands uniformly in
its reach and used half of it. This is that measurement.

**Black-box, no production code touched.** `_produce` does not expose
`best_offset` directly, but it does expose `_tail_mono`, and that is an exact
copy of `mono[best_offset + sequence : best_offset + grain_len]` -- a
110-frame (`OVERLAP_FRAMES`) slice of real audio, copied verbatim. Real audio
does not repeat itself byte-for-byte at that length by chance, so searching
for that exact slice within `mono[low + sequence : high + grain_len]` recovers
`best_offset` exactly, from outside the class, the same way
`test_the_search_is_forward_only_and_the_reference_width` already reads
`self._read` from outside it.

Reports the displacement (`best_offset - low`) as a fraction of `SEARCH_FRAMES`
per track per rate, because that fraction is what the formula needs -- and
whether it holds steady across rate and across tracks, because a number this
measurement fits to one track is exactly how the "half" it is replacing became
wrong in the first place.
"""
from __future__ import annotations

import array
import statistics
import sys

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

from PySide6.QtWidgets import QApplication  # noqa: E402

from audio import audio_engine  # noqa: E402
from audio.audio_engine import (  # noqa: E402
    CHANNELS, GRAIN_FRAMES, SAMPLE_RATE, SEARCH_FRAMES, SEQUENCE_FRAMES,
    TimeStretcher, downmix_to_mono,
)

sys.path.insert(0, __file__.rsplit("tools", 1)[0] + "tools")
from measure_stretch_quality import decode  # noqa: E402

SECONDS = 20.0


def find_offset(mono: array.array, tail_mono: array.array, low: int, high: int) -> int:
    """Recover `best_offset` from the tail it left behind.

    `_tail_mono` is `mono[best_offset + SEQUENCE_FRAMES : best_offset + GRAIN_FRAMES]`,
    so scanning `mono` for that exact slice in the corresponding window and
    subtracting `SEQUENCE_FRAMES` inverts it. `array.array` slice equality is a
    single C-level memcmp per candidate, over at most `SEARCH_FRAMES` of them.
    """
    span = len(tail_mono)
    scan_low = low + SEQUENCE_FRAMES
    scan_high = high + SEQUENCE_FRAMES
    for candidate in range(scan_low, scan_high):
        if mono[candidate:candidate + span] == tail_mono:
            return candidate - SEQUENCE_FRAMES
    raise LookupError("tail not found -- did the geometry constants change?")


def measure(path: str, rate: float, app: QApplication) -> list[int]:
    """Displacement (`best_offset - low`) for every grain over SECONDS of
    `path` at `rate`. Starts a minute in, same reasoning as the other
    harnesses: an intro is often quiet and flatters a search that has nothing
    to correlate against.
    """
    music = decode(path, app)
    start = min(len(music) // 2, SAMPLE_RATE * CHANNELS * 60)
    music = music[start:start + SAMPLE_RATE * CHANNELS * int(SECONDS + 5)]
    mono = downmix_to_mono(music)

    stretcher = TimeStretcher()
    stretcher.reset(0.0)
    displacements = []
    real_produce = audio_engine.TimeStretcher._produce

    def recording_produce(self, source, mn, produce_rate):
        low = int(self._read)
        ok = real_produce(self, source, mn, produce_rate)
        if ok and produce_rate != 1.0:
            high = min(len(mn) - GRAIN_FRAMES, low + SEARCH_FRAMES)
            if high > low:
                best_offset = find_offset(mn, self._tail_mono, low, high)
                displacements.append(best_offset - low)
        return ok

    audio_engine.TimeStretcher._produce = recording_produce
    try:
        produced = 0
        wanted = int(SAMPLE_RATE * SECONDS)
        while produced < wanted:
            block = stretcher.pull(music, mono, 4096, rate)
            if not block:
                break
            produced += len(block) // (CHANNELS * 2)
    finally:
        audio_engine.TimeStretcher._produce = real_produce
    return displacements


def main() -> None:
    args = sys.argv[1:]
    paths = [a for a in args if not a.replace(".", "", 1).isdigit()]
    rates = [float(a) for a in args if a.replace(".", "", 1).isdigit()] or [0.25, 0.5, 0.75]
    if not paths:
        raise SystemExit(__doc__)

    app = QApplication.instance() or QApplication([])
    print(f"SEARCH_FRAMES={SEARCH_FRAMES} ({SEARCH_FRAMES / SAMPLE_RATE * 1000:.1f}ms), "
          f"SEQUENCE_FRAMES={SEQUENCE_FRAMES}")
    print(f"{'track':>28} {'rate':>5} {'grains':>7} {'mean':>8} {'median':>8} "
          f"{'as % of reach':>14}")
    all_fractions = []
    for path in paths:
        label = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        folder = path.rsplit("\\", 2)[-2] if "\\" in path else path.rsplit("/", 2)[-2]
        for rate in rates:
            displacements = measure(path, rate, app)
            if not displacements:
                print(f"{folder:>28} {rate:>5}   no grains produced")
                continue
            mean = statistics.mean(displacements)
            median = statistics.median(displacements)
            fraction = mean / SEARCH_FRAMES
            all_fractions.append(fraction)
            print(f"{folder:>28} {rate:>5} {len(displacements):>7} "
                  f"{mean:>7.1f}f {median:>7.1f}f {fraction * 100:>12.1f}%")
    if all_fractions:
        print()
        print(f"overall mean fraction of reach: {statistics.mean(all_fractions):.4f} "
              f"(stdev {statistics.stdev(all_fractions):.4f})"
              if len(all_fractions) > 1 else
              f"overall mean fraction of reach: {all_fractions[0]:.4f}")


if __name__ == "__main__":
    main()
