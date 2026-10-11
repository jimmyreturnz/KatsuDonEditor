"""How long the track's mono downmix holds the GIL, old against new.

`downmix_to_mono` runs on the audio engine's thread for every decoded buffer,
right after a map opens. It used to be a Python generator over every stereo
frame; with the GIL handed over only every `sys.getswitchinterval()` (5ms),
that was the UI thread stalling in 5ms slices for the whole decode -- a third
of all frames over budget for the first second or so after every load.

Checks the two produce identical samples before timing anything: the mono
buffer is the WSOLA splice search's input, so a different number there is a
different sound, and "faster" means nothing if it is not also "the same".

Usage: python tools/bench_downmix.py [seconds of stereo audio, default 131]
"""
from __future__ import annotations

import array
import random
import sys
from time import perf_counter

sys.path.insert(0, ".")

from audio import audio_engine

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 131.0
frames = int(seconds * audio_engine.SAMPLE_RATE)
rng = random.Random(1)
# Full int16 range, extremes included: -32768 + -32768 is where a floor
# division and a shift could disagree if either were done wrong.
source = array.array("h", (rng.randint(-32768, 32767) for _ in range(frames * 2)))
source[0], source[1] = -32768, -32768
source[2], source[3] = 32767, 32767
source[4], source[5] = -32768, 32767


def old(block: array.array) -> array.array:
    channels = audio_engine.CHANNELS
    return array.array("h", (
        (block[i] + block[i + 1]) // 2 for i in range(0, len(block), channels)
    ))


new = audio_engine.downmix_to_mono

# The decoder hands buffers over in pieces; time it the way it is called.
BUFFER = 4096 * 2
pieces = [source[i:i + BUFFER] for i in range(0, len(source), BUFFER)]

assert old(source) == new(source), "the two downmixes disagree"
for piece in pieces[:50]:
    assert old(piece) == new(piece)
print(f"identical over {frames} frames ({seconds:.0f}s stereo)")

for name, fn in (("old", old), ("new", new)):
    began = perf_counter()
    longest = 0.0
    for piece in pieces:
        one = perf_counter()
        fn(piece)
        longest = max(longest, perf_counter() - one)
    total = perf_counter() - began
    print(f"{name}: {total * 1000:8.1f} ms total, longest single buffer "
          f"{longest * 1000:.3f} ms")
