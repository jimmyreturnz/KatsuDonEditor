"""What *sustained* slow-rate playback delivers to the device, continuously,
as opposed to `measure_rate_change.py` (a one-time rate change) or
`bench_timestretch.py` (a standalone re-implementation with different grain
sizes than `TimeStretcher`'s real `SEQUENCE_FRAMES`/`OVERLAP_FRAMES`/
`SEARCH_FRAMES` -- so it measures a different amount of work per second of
output than the real code, and cannot answer this question by itself).

    python tools/measure_slow_rate_playback.py [audio file] [rate] [seconds]

Built for report #2: "the speed is ok but the stuttering really affects the
quality" at slower-than-1.0x. Plays continuously at `rate` (default 0.5) with
no seeks, and taps `_device.write()` and `_Engine._fill()` directly on the
real production classes -- the same technique `measure_seek.py` uses -- to
answer three separable questions rather than assuming one:

  1. Does output fall behind wall-clock (an underrun -- WSOLA or the decoder
     not producing fast enough)?
  2. Are there gaps between writes bigger than the pump interval (the device
     starved for one tick or more)?
  3. Is any single `_fill()` call itself slow enough to blow the 10ms pump
     budget (the search costing more than the budget on this machine)?

No audio file lying around is fine: like `measure_seek.py`, the question here
is about the pipeline's timing, not what the file happens to sound like, so a
generated tone is reused if none is given.
"""
import os
import sys
import wave
import array
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QCoreApplication, QElapsedTimer, QTimer  # noqa: E402

from audio import audio_engine  # noqa: E402

# For sweeping tunables against the numbers below without editing the module:
# TAIKO_SINK_BUFFER_MS_OVERRIDE=<ms> TAIKO_CORRELATION_STEP=<n> TAIKO_SEARCH_STEP=<n>
# TAIKO_TIMING_BUDGET_MS=<ms>
#   python tools/measure_slow_rate_playback.py ...
_buffer_override = os.environ.get("TAIKO_SINK_BUFFER_MS_OVERRIDE")
if _buffer_override:
    audio_engine.SINK_BUFFER_MS = float(_buffer_override)
if os.environ.get("TAIKO_CORRELATION_STEP"):
    audio_engine.CORRELATION_STEP = int(os.environ["TAIKO_CORRELATION_STEP"])
if os.environ.get("TAIKO_SEARCH_STEP"):
    audio_engine.SEARCH_STEP = int(os.environ["TAIKO_SEARCH_STEP"])
if os.environ.get("TAIKO_TIMING_BUDGET_MS"):
    audio_engine.TIMING_BUDGET_MS = float(os.environ["TAIKO_TIMING_BUDGET_MS"])
# TAIKO_SEQUENCE_MS pins the grain to one length at every rate, for walking a
# ladder of grain lengths by ear. The overlap is scaled with it rather than
# left at SoundTouch's fixed 8ms: at a 10ms grain that would be 80% of the
# output cross-faded, which is comb filtering (measured: a 440Hz sine came out
# with 30% of its energy off 440Hz) and would be heard as the short grain's
# fault rather than the window's. 1/8 keeps roughly upstream's ratio.
_sequence_ms = os.environ.get("TAIKO_SEQUENCE_MS")
if _sequence_ms:
    _seq = int(audio_engine.SAMPLE_RATE * float(_sequence_ms) / 1000.0)
    audio_engine.SEQUENCE_FRAMES = _seq
    audio_engine.OVERLAP_FRAMES = max(16, _seq // 8)
    audio_engine.GRAIN_FRAMES = _seq + audio_engine.OVERLAP_FRAMES
# TAIKO_CLAMP_REACH=1 stops the splice search reaching further forward than the
# read head advances (`sequence * rate`). Past that a grain may read source the
# previous grain already emitted, so a transient is partially replayed -- the
# suspect for "the kick distorts a bit" at 0.25x, where the hop is 13ms and
# SoundTouch's reach is 25ms.
if os.environ.get("TAIKO_CLAMP_REACH"):
    _search = audio_engine.search_frames
    audio_engine.search_frames = lambda rate, _f=_search: max(
        16, min(_f(rate), int(audio_engine.SEQUENCE_FRAMES * rate)))


def _make_test_wav(path: str, seconds: float = 30.0) -> None:
    rate = 44100
    frames = int(rate * seconds)
    samples = array.array("h", (
        int(12000 * math.sin(2 * math.pi * 220.0 * i / rate)) for i in range(frames)
    ))
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())


raw = sys.argv[1:]
if raw and os.path.exists(raw[0]):
    PATH, raw = raw[0], raw[1:]
else:
    PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_measure_seek_tone.wav")
    if not os.path.exists(PATH):
        _make_test_wav(PATH)

RATE = float(raw[0]) if len(raw) > 0 else 0.5
SECONDS = float(raw[1]) if len(raw) > 1 else 8.0

app = QCoreApplication([])

# Tap _fill() itself (wall cost per pump tick -- did the search blow the
# budget?) and every device write (what actually reached the sink).
fill_timings = []  # (wall_ms, elapsed_ms)
_orig_fill = audio_engine._Engine._fill


def _timed_fill(self):
    t = QElapsedTimer()
    t.start()
    _orig_fill(self)
    fill_timings.append((wall.elapsed(), t.elapsed()))


audio_engine._Engine._fill = _timed_fill

written = []  # (wall_ms, frame_count, bytes_free_before, buffer_bytes)
_orig_open_sink = audio_engine._Engine._open_sink


def _open_sink_and_tap(self):
    _orig_open_sink(self)
    sink = self._sink
    device = self._device
    real_write = device.write
    buffer_bytes = sink.bufferSize()

    def tapped(data):
        n = len(bytes(data)) // (2 * audio_engine.CHANNELS)
        if n:
            # bytesFree() *before* this write is what the hardware ring
            # buffer had already drained to since the last write -- if it
            # reads as the full buffer size, the ring was completely empty
            # (real silence already reached the speaker) before this write
            # topped it back up, which a write-to-write timing gap alone
            # cannot tell apart from "the buffer merely got a bit thin".
            written.append((wall.elapsed(), n, sink.bytesFree(), buffer_bytes))
        return real_write(data)

    device.write = tapped


audio_engine._Engine._open_sink = _open_sink_and_tap

player = audio_engine.TrackPlayer()
player.setSource(PATH)

wall = QElapsedTimer()


def start_playback():
    wall.start()
    player.setPlaybackRate(RATE)
    player.play()


QTimer.singleShot(300, start_playback)
QTimer.singleShot(int(300 + SECONDS * 1000), app.quit)
app.exec()

player.shutdown()

print(f"{os.path.basename(PATH)}  sustained playback at rate={RATE} for {SECONDS:.1f}s")
print(f"_fill() calls: {len(fill_timings)}  writes: {len(written)}")

# Q1: does delivered audio-time keep pace with wall-clock? Output plays at
# real (44100Hz) speed regardless of rate -- the rate only changes how much
# *source* one output frame consumes -- so total frames written should track
# wall-elapsed 1:1 (minus the FADE_FRAMES/startup latency) at ANY rate. A
# growing deficit here is an underrun, independent of what caused it.
if written:
    total_frames = sum(n for _t, n, _bf, _buf in written)
    delivered_ms = total_frames / audio_engine.SAMPLE_RATE * 1000.0
    elapsed_ms = written[-1][0] - written[0][0] + (
        # first write's own duration, so the window covers what it produced too
        written[0][1] / audio_engine.SAMPLE_RATE * 1000.0)
    print(f"audio delivered: {delivered_ms:.0f}ms over {elapsed_ms:.0f}ms of wall clock "
          f"covered by writes ({delivered_ms - elapsed_ms:+.0f}ms)")

# Q2: gaps between writes bigger than one pump tick -- our own feeding
# cadence. This alone conflates "the hardware buffer got a bit thin" with
# "the hardware buffer ran fully dry" -- both show the same write-to-write
# gap, because the gap is dominated by how long the slow _fill() call itself
# took, not by how much slack the buffer had. See the bytesFree() check below
# for which one actually happened.
if len(written) > 1:
    gaps = [(a[0], b[0] - a[0]) for a, b in zip(written, written[1:])]
    over_pump = [(t, g) for t, g in gaps if g > audio_engine.PUMP_INTERVAL_MS + 2]
    total_over = sum(g for _t, g in over_pump)
    print(f"write gaps > pump interval + 2ms: {len(over_pump)}, totalling {total_over}ms")
    if over_pump:
        worst = max(over_pump, key=lambda x: x[1])
        print(f"  worst gap: {worst[1]}ms at wall={worst[0]}ms "
              f"({(worst[0] - wall.elapsed() + SECONDS * 1000) / 1000.0:.2f}s into playback)")

# The number that actually answers "did the speaker go silent": bytesFree()
# read right before each write. If it ever equals the full buffer size, the
# hardware ring had nothing left to play at that moment -- true silence,
# not just a thin buffer -- independent of how long the gap to get there was.
#
# **The first write does not count.** Nothing has been written yet, so the ring
# is empty by construction and `bf >= buf` is true for reasons that have
# nothing to do with keeping up. Counted, it reported exactly one drain in
# every run this harness has ever produced -- a constant, which is the shape of
# an artifact rather than a measurement, and it hid the question of how long
# the device spends empty *before* that write instead (`time to first write`).
if written:
    full_drains = [(t, bf, buf) for t, _n, bf, buf in written[1:] if bf >= buf]
    print(f"writes where the hardware buffer had fully drained first: "
          f"{len(full_drains)} / {len(written) - 1}  (buffer={written[0][3]} bytes = "
          f"{written[0][3] / (audio_engine.CHANNELS * 2) / audio_engine.SAMPLE_RATE * 1000:.0f}ms"
          f", first write excluded)")
    if full_drains:
        print(f"  first at wall={full_drains[0][0]}ms")
    # What `_open_sink`'s priming is for: the device is open and silent from
    # `start()` until this, so it is the startup dropout in milliseconds.
    print(f"time to first write: {written[0][0]}ms after playback began")

# Q3: is any single _fill() call itself slow enough to blow the pump budget?
if fill_timings:
    costs = [c for _t, c in fill_timings]
    costs_sorted = sorted(costs)
    p50 = costs_sorted[len(costs_sorted) // 2]
    p99 = costs_sorted[int(len(costs_sorted) * 0.99)]
    over_budget = [c for c in costs if c > audio_engine.PUMP_INTERVAL_MS]
    print(f"_fill() cost: median={p50}ms p99={p99}ms max={max(costs)}ms  "
          f"(budget is {audio_engine.PUMP_INTERVAL_MS}ms)")
    print(f"_fill() calls over budget: {len(over_budget)} / {len(costs)}")
