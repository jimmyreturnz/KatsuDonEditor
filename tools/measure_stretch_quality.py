"""Score the time-stretch, so tuning it is not done by ear and argument.

    python tools/measure_stretch_quality.py <audio file> [rate]

Two numbers, both reference-free so they work on real music:

- **splice ratio** -- the largest sample-to-sample jump in the output over the
  largest one in the source. A time-stretch can only ever rearrange existing
  audio, so it cannot legitimately produce a steeper edge than the source
  contains: anything above 1.0 is a discontinuity the splice invented, which is
  what a click is. This is the number that tracks "harsh".
- **warble** -- on a constant-amplitude sine, how much the output envelope
  moves. Splices that land out of phase cancel, and the dip is heard as
  warbling rather than as a click, so a click detector alone misses it. Ideal
  is 0.

Plus the realtime factor, because every quality lever here costs CPU and the
budget is one core minus whatever the editor needs to paint.
"""
from __future__ import annotations

import array
import math
import random
import sys
import time

sys.path.insert(0, __file__.rsplit("tools", 1)[0])

from PySide6.QtCore import QUrl, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat  # noqa: E402

import os  # noqa: E402

from audio import audio_engine  # noqa: E402
from audio.audio_engine import (  # noqa: E402
    CHANNELS, SAMPLE_RATE, TimeStretcher, downmix_to_mono,
)

# For sweeping the search cost against these numbers without editing the
# module: TAIKO_CORRELATION_STEP=2 TAIKO_SEARCH_STEP=4 python tools/measure_stretch_quality.py ...
if os.environ.get("TAIKO_CORRELATION_STEP"):
    audio_engine.CORRELATION_STEP = int(os.environ["TAIKO_CORRELATION_STEP"])
if os.environ.get("TAIKO_SEARCH_STEP"):
    audio_engine.SEARCH_STEP = int(os.environ["TAIKO_SEARCH_STEP"])


def decode(path: str, app: QApplication) -> array.array:
    fmt = QAudioFormat()
    fmt.setSampleRate(SAMPLE_RATE)
    fmt.setChannelCount(CHANNELS)
    fmt.setSampleFormat(QAudioFormat.Int16)
    decoder = QAudioDecoder()
    decoder.setAudioFormat(fmt)
    chunks: list[bytes] = []
    decoder.bufferReady.connect(lambda: chunks.append(bytes(decoder.read().constData())))
    decoder.finished.connect(app.quit)
    decoder.setSource(QUrl.fromLocalFile(path))
    decoder.start()
    QTimer.singleShot(60000, app.quit)
    app.exec()
    pcm = array.array("h")
    pcm.frombytes(b"".join(chunks))
    return pcm


def max_step(frames: array.array) -> int:
    """Largest absolute first difference on the left channel."""
    left = frames[::CHANNELS]
    return max((abs(b - a) for a, b in zip(left, left[1:])), default=0)


def warble(frames: array.array, window: int = 256) -> float:
    """Spread of the output envelope, as a fraction of its mean."""
    left = frames[::CHANNELS]
    peaks = [max(map(abs, left[i:i + window]))
             for i in range(0, len(left) - window, window)]
    peaks = [p for p in peaks if p > 0]
    if len(peaks) < 4:
        return 0.0
    mean = sum(peaks) / len(peaks)
    variance = sum((p - mean) ** 2 for p in peaks) / len(peaks)
    return math.sqrt(variance) / mean


# Deliberately inharmonic. A single sine is the case WSOLA is best at -- the
# splice search can always find a whole period to land on -- so it scores well
# no matter how the stretch is tuned. Three partials that share no common
# period cannot all be aligned by one offset, and the ones that miss beat
# against each other. That beating is the artifact being hunted.
CHORD_HZ = (220.0, 311.1, 466.2)


def tone(seconds: float, partials=CHORD_HZ, amplitude: int = 9000) -> array.array:
    out = array.array("h")
    scale = amplitude / len(partials)
    for i in range(int(SAMPLE_RATE * seconds)):
        value = int(sum(
            scale * math.sin(2.0 * math.pi * hz * i / SAMPLE_RATE) for hz in partials))
        out.append(value)
        out.append(value)
    return out


# A kick every half second over a quiet pad. The metrics above it are all
# measured on *stationary* signals, which is the one case WSOLA is best at --
# they score the shipped build at its ceiling (tonality 0.999, warble at the
# source's own 0.150, click 1.00) at every rate, and they scored CORRELATION_STEP
# = 2 as "no change" because they could not have seen one. What a listener
# actually hears at 0.25x is a drum hit coming out two, three or four times:
# stretching 4x means re-emitting the same source region four times, and when
# that region holds an attack, the attack repeats. Loud transients against a
# quiet bed is exactly the contrast a tone has none of.
PERCUSSIVE_PERIOD_MS = 500.0


def percussive(seconds: float) -> array.array:
    pad_hz, kick_hz = 196.0, 70.0
    noise = random.Random(11)
    frames = int(SAMPLE_RATE * seconds)
    mono = array.array("h", (
        int(1800 * math.sin(2.0 * math.pi * pad_hz * i / SAMPLE_RATE))
        for i in range(frames)))
    step = int(PERCUSSIVE_PERIOD_MS / 1000.0 * SAMPLE_RATE)
    for start in range(int(0.25 * SAMPLE_RATE), frames - step, step):
        for k in range(int(0.030 * SAMPLE_RATE)):
            decay = math.exp(-k / (0.006 * SAMPLE_RATE))
            mono[start + k] = audio_engine._clip(mono[start + k] + int(
                21000 * decay * (0.6 * math.sin(2.0 * math.pi * kick_hz * k / SAMPLE_RATE)
                                 + 0.4 * noise.uniform(-1.0, 1.0))))
    out = array.array("h")
    for value in mono:
        out.append(value)
        out.append(value)
    return out


def extra_attacks(frames: array.array, expected_ms: float) -> int:
    """Attacks the stretch invented, i.e. a repeated drum hit.

    Onsets off the envelope with hysteresis, then anything landing well inside
    the expected spacing is a hit that was not in the source at that time. The
    shipped build scores 18 at 0.25x and 11 at 0.5x over 12s -- the number to
    beat, and the one that stays flat for every change the metrics above call
    free.
    """
    left = frames[::CHANNELS]
    window = 128
    envelope = [max(map(abs, left[i:i + window]))
                for i in range(0, len(left) - window, window)]
    if not envelope or max(envelope) <= 0:
        return 0
    threshold = 0.45 * max(envelope)
    onsets, armed = [], True
    for index, value in enumerate(envelope):
        if value > threshold and armed:
            onsets.append(index * window / SAMPLE_RATE * 1000.0)
            armed = False
        elif value < threshold * 0.5:
            armed = True
    return sum(1 for a, b in zip(onsets, onsets[1:]) if b - a < expected_ms * 0.6)


def sine(seconds: float, hz: float = 440.0, amplitude: int = 12000) -> array.array:
    out = array.array("h")
    for i in range(int(SAMPLE_RATE * seconds)):
        value = int(amplitude * math.sin(2.0 * math.pi * hz * i / SAMPLE_RATE))
        out.append(value)
        out.append(value)
    return out


def tonality(frames: array.array, hz: float = 440.0) -> float:
    """Share of the output's energy still sitting at the input frequency.

    Goertzel rather than a full transform: only one bin is wanted, and this is
    a dozen lines with no dependency. A clean stretch of a pure tone scores
    1.0; splices smear energy into sidebands and drag it down.
    """
    left = frames[::CHANNELS]
    count = len(left)
    if count < 1000:
        return 0.0
    coefficient = 2.0 * math.cos(2.0 * math.pi * hz / SAMPLE_RATE)
    first = second = 0.0
    for sample in left:
        first, second = sample + coefficient * first - second, first
    power = first * first + second * second - coefficient * first * second
    energy = sum(float(sample) * sample for sample in left)
    if energy <= 0:
        return 0.0
    return power / (energy * count / 2.0)


def run(source: array.array, rate: float, seconds: float):
    mono = downmix_to_mono(source)
    stretcher = TimeStretcher()
    stretcher.reset(0.0)
    wanted = int(SAMPLE_RATE * seconds)
    collected = bytearray()
    started = time.perf_counter()
    while len(collected) // (2 * CHANNELS) < wanted:
        block = stretcher.pull(source, mono, 8192, rate)
        if not block:
            break
        collected += block
    elapsed = time.perf_counter() - started
    out = array.array("h")
    out.frombytes(bytes(collected))
    produced = len(out) // CHANNELS / SAMPLE_RATE
    return out, elapsed / produced if produced else 0.0


def main() -> None:
    app = QApplication([])
    path = sys.argv[1]
    rates = [float(sys.argv[2])] if len(sys.argv) > 2 else [0.75, 0.5, 0.25]
    music = decode(path, app)
    # A slice from a minute in: intros are often quiet, and a quiet excerpt
    # flatters every one of these numbers.
    start = min(len(music) // 2, SAMPLE_RATE * CHANNELS * 60)
    music = music[start:start + SAMPLE_RATE * CHANNELS * 12]
    chord = tone(8.0)
    pure = sine(8.0)
    drums = percussive(24.0)

    sequence = audio_engine.SEQUENCE_FRAMES
    overlap = audio_engine.OVERLAP_FRAMES
    print(f"sequence={sequence} ({sequence / SAMPLE_RATE * 1000:.1f}ms) "
          f"overlap={overlap} ({overlap / SAMPLE_RATE * 1000:.1f}ms) "
          f"search=+{audio_engine.SEARCH_FRAMES} (forward-only) "
          f"step={audio_engine.SEARCH_STEP}/{audio_engine.CORRELATION_STEP}")
    print(f"{SAMPLE_RATE / sequence:.0f} splices/sec, "
          f"{overlap / sequence * 100:.0f}% of output cross-faded")
    print(f"  source tonality={tonality(pure):.3f} warble={warble(chord):.3f} "
          f"(the ceiling every row below is measured against)")
    print(f"{'rate':>6} {'tonality':>9} {'warble':>8} {'click':>7} {'repeats':>8} {'cpu':>7}")
    for rate in rates:
        out, cost = run(music, rate, 4.0)
        chord_out, _ = run(chord, rate, 3.0)
        pure_out, _ = run(pure, rate, 3.0)
        drums_out, _ = run(drums, rate, 12.0)
        # On the tone, not the music: a clipped master already contains steps
        # near full scale, so the ratio there is swamped and says nothing.
        click = max_step(pure_out) / max(1, max_step(pure))
        repeats = extra_attacks(drums_out, PERCUSSIVE_PERIOD_MS / rate)
        print(f"{rate:>6} {tonality(pure_out):>9.3f} {warble(chord_out):>8.3f} "
              f"{click:>7.2f} {repeats:>8} {cost:>6.2f}x")


if __name__ == "__main__":
    main()
