"""Does a transient come out where the playhead says it does?

    python tools/measure_stretch_timing.py [rate ...]

The question `measure_stretch_quality.py` cannot answer. That one scores what
the stretch *sounds* like; this one scores whether the song time the engine
reports is the song time you are hearing, which is the whole reason an editor
slows down at all.

Built because three throwaway probes of this disagreed with each other during
one investigation -- exactly the trap CLAUDE.md warns about -- so the number
lives in a harness now instead of being re-derived each time.

**A click train, not a tone.** On a sine many splice offsets are equally
correct, so the error hides inside content that is identical either way and the
stretch scores well no matter how far off it is. An impulse is somewhere or it
is not.

Two numbers per rate, and the *signed* one is the one that matters:

- **signed** -- the bias. A one-sided error is a constant lag between the audio
  and the cursor, which is what a mapper hears as "the playhead is lying".
  Should sit near zero.
- **absolute** -- the spread that is left once the bias is gone. Bounded below
  by the stretch itself: inside one grain the audio advances at 1.0x while the
  playhead advances at `rate`, so `SEQUENCE_FRAMES * (1 - rate)` of slew is
  real and cannot be reported away, only centred. Shortening the grain is
  therefore the only lever on it, which is what `SEQUENCE_MS` is about.

Measured baselines, 0.25 / 0.5 / 0.75 / 1.0, absolute error:

- before any of this:                       40.8 / 32.1 / 20.3 / 0.00 ms
- after centring on the grain:               15.8 /  5.1 /  5.8 / 0.00 ms
- at SoundTouch's 117ms grain:                21.2 / 15.0 /  5.4 / 0.00 ms
- at the 20ms grain, half-of-reach guess:      7.7 /  2.9 /  1.7 / 0.00 ms
- at the 20ms grain, calibrated on real music: 9.4 /  4.2 /  1.6 / 0.00 ms

**Careful with `absolute` on this signal.** At the 20ms grain `worst` comes
out barely above `absolute` (9.70 against 9.44 at 0.25x), which would mean the
sawtooth has no spread at all -- and it must have 15ms of it there. The likely
reason is that WSOLA's search aligns transients, so a train of impulses gets
every click snapped to the same phase of its grain. Read a small spread here as
the bias being small, not as the spread being gone.

**The last row went up, on purpose, and this is not a regression.**
`grain_offset_ms` used to assume the splice search lands uniformly across its
reach (half of it) and was later found to disagree with itself when checked --
so `tools/measure_search_bias.py` measured where the search *actually* lands,
on three real tracks, using the exact `best_offset` the search chose (not an
inference from where a click landed). The click train's own search behaviour
turned out to sit at 26-28% of the reach; real music's sits at 50-58%. This
harness's numbers moved to match what happens on the signal nobody actually
plays being deliberately left worse, in exchange for what real playback
measures being better. If this harness's numbers and `measure_search_bias.py`
ever disagree about which way to tune the correction, believe the real music.

And the column that this harness was missing when it mattered:

- **snaps** -- ticks where the reported position moves so far from a smooth
  advance that gui.py's interpolating clock gives up and jumps to it. The
  timing columns above are blind to this: a correction can place every
  transient perfectly and still arrive as a step at each grain boundary, which
  on screen is the playhead teleporting a dozen times a second. It measured
  146 snaps in 1199 ticks at 0.25x while the timing columns read 2.0ms, and
  the report was "it teleports like hell". **Must be 0.**

1.0x must always read exactly 0.00: `_produce` takes a straight-copy path there
with no grain and no search, so anything else means the correction has leaked
into the one rate that never needed it.
"""
from __future__ import annotations

import array
import math
import statistics
import sys

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

from audio.audio_engine import (  # noqa: E402
    CHANNELS, PUMP_INTERVAL_MS, SAMPLE_RATE, TimeStretcher, downmix_to_mono,
    grain_offset_ms,
)

# gui.py's POSITION_ALLOWABLE_ERROR_MS. Past this much disagreement (times the
# rate) the interpolating clock stops interpolating and snaps to the source,
# which on screen is the playhead teleporting.
SNAP_TOLERANCE_MS = 1000.0 / 60.0 * 2.0

# Pulled a pump tick at a time, not in big blocks: `_Engine._position_ms` reads
# the correction every PUMP_INTERVAL_MS, so sampling it any coarser than a grain
# attributes one grain's correction to a transient emitted by another.
PUMP_FRAMES = int(SAMPLE_RATE * PUMP_INTERVAL_MS / 1000)

CLICK_PERIOD_MS = 500.0
SOURCE_SECONDS = 25.0
OUTPUT_SECONDS = 12.0


BED_HZ = 196.0
BED_AMPLITUDE = 1500


def click_train(seconds: float) -> tuple[array.array, list[float]]:
    """Impulses over a quiet bed, and the millisecond each one sits at.

    The bed is not decoration. In *pure* silence every candidate window has zero
    energy, so the normalised correlation has nothing to divide by and no
    candidate can be scored at all -- the search degenerates to "do not move",
    the read head stops tracking the content, and the measured bias blows out to
    ~125ms on a signal no real track resembles. Music always has a floor. This
    one is 27dB under the impulses, quiet enough to leave every onset
    unambiguous.
    """
    frames = int(SAMPLE_RATE * seconds)
    samples = array.array("h", bytes(2 * CHANNELS * frames))
    for i in range(frames):
        value = int(BED_AMPLITUDE * math.sin(2.0 * math.pi * BED_HZ * i / SAMPLE_RATE))
        samples[i * CHANNELS] = value
        samples[i * CHANNELS + 1] = value
    step = int(CLICK_PERIOD_MS / 1000.0 * SAMPLE_RATE)
    at = []
    for start in range(0, frames - step, step):
        at.append(start / SAMPLE_RATE * 1000.0)
        for k in range(60):
            value = int(22000 * math.cos(math.pi * k / 120))
            samples[(start + k) * CHANNELS] = value
            samples[(start + k) * CHANNELS + 1] = value
    return samples, at


def errors(source, mono, clicks, rate: float):
    stretcher = TimeStretcher()
    stretcher.reset(0.0)
    out = array.array("h")
    produced = 0
    # The smoothed splice offset as it stood while each stretch of output was
    # being handed over. Sampled rather than read once at the end, because
    # `_Engine._position_ms` reads it live on every pump tick -- taking the
    # final value and applying it backwards over the whole run is a harness
    # artifact that reads as a ~90ms bias the engine does not have.
    samples: list[tuple[int, float]] = [(0, 0.0)]
    reported: list[float] = []
    # Constant for a given rate, so there is nothing to model: see
    # `grain_offset_ms` for why it is deliberately not per-grain.
    correction = grain_offset_ms(rate)
    while produced < int(SAMPLE_RATE * OUTPUT_SECONDS):
        block = stretcher.pull(source, mono, PUMP_FRAMES, rate)
        if not block:
            break
        chunk = array.array("h")
        chunk.frombytes(block)
        out += chunk
        produced += len(block) // (CHANNELS * 2)
        samples.append((produced, correction))
        reported.append(produced * rate / SAMPLE_RATE * 1000.0 + correction)
    found = []
    frame, total = 0, len(out) // CHANNELS
    while frame < total:
        if abs(out[frame * CHANNELS]) > 15000:
            found.append(frame)
            frame += int(0.05 * SAMPLE_RATE)
        else:
            frame += 1
    out_errors = []
    for at in found:
        correction_then = next(
            (value for edge, value in reversed(samples) if edge <= at), 0.0)
        # The same expression `_Engine._position_ms` builds.
        where = at * rate / SAMPLE_RATE * 1000.0 + correction_then
        out_errors.append(where - min(clicks, key=lambda c: abs(c - where)))
    # How the playhead is asked to move between reports. A smooth one advances
    # PUMP_INTERVAL_MS * rate every tick; anything beyond the tolerance is a
    # snap, and anything negative is the playhead running backwards.
    ideal = PUMP_INTERVAL_MS * rate
    steps = [b - a for a, b in zip(reported, reported[1:])]
    snaps = sum(1 for s in steps if abs(s - ideal) > SNAP_TOLERANCE_MS * rate)
    return out_errors, snaps, len(steps), (min(steps) if steps else 0.0)


def main() -> None:
    rates = [float(a) for a in sys.argv[1:]] or [0.25, 0.5, 0.75, 1.0]
    source, clicks = click_train(SOURCE_SECONDS)
    mono = downmix_to_mono(source)
    print(f"{'rate':>6} {'signed':>10} {'absolute':>10} {'worst':>10} "
          f"{'snaps':>12} {'backwards':>10}")
    for rate in rates:
        found, snaps, ticks, worst_step = errors(source, mono, clicks, rate)
        if not found:
            print(f"{rate:>6} {'no transients detected':>32}")
            continue
        absolute = [abs(e) for e in found]
        print(f"{rate:>6} {statistics.mean(found):>+9.2f}ms "
              f"{statistics.mean(absolute):>8.2f}ms {max(absolute):>8.2f}ms "
              f"{snaps:>5}/{ticks:<6} {'YES' if worst_step < 0 else 'no':>10}")


if __name__ == "__main__":
    main()
