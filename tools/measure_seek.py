"""What a seek during playback actually costs -- wall-clock and the position trace.

    python tools/measure_seek.py [audio file] [seek_to_ms] [seconds]

`audio file` is optional -- omit it (or pass just the numbers) to fall back to
a generated tone; see the note at the bottom of this docstring for why that is
fine here.

Built for one specific report: clicking the timeline bar while a song is
playing makes playback stutter once. `_Engine.seek` in audio_engine.py tears
the QAudioSink down and rebuilds it while playing -- its own docstring says
why: the sink is holding up to SINK_BUFFER_MS of the *old* position with no
way to discard it but a restart. This measures what that restart actually
costs: how long `_open_sink()` (QAudioSink() + .start(), on the audio thread)
takes while the seek sits waiting on it, and whether the song-position trace
shows a stall or jump big enough to be an audible click rather than a smooth
handoff.

No audio file lying around handy is fine here: unlike `measure_audio_backend`
(which cares about codec-dependent decode chunking) this is asking about the
sink restart itself, which does not care what produced the PCM -- so this
generates a short sine-wave WAV next to this script on first run.
"""
import os
import sys
import wave
import array
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QCoreApplication, QElapsedTimer, QTimer  # noqa: E402

from audio import audio_engine  # noqa: E402


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

SEEK_TO_MS = float(raw[0]) if len(raw) > 0 else 20000.0
SECONDS = float(raw[1]) if len(raw) > 1 else 4.0

app = QCoreApplication([])

# Instrument the real methods on the class itself, before any engine exists,
# so the production path -- TrackPlayer's own _Engine on its own QThread --
# is exactly what gets timed rather than a stand-in.
timings = []  # (label, wall_ms)
_orig_open_sink = audio_engine._Engine._open_sink
_orig_seek = audio_engine._Engine.seek


def _timed_open_sink(self):
    t = QElapsedTimer()
    t.start()
    _orig_open_sink(self)
    timings.append(("_open_sink", t.elapsed()))


def _timed_seek(self, position_ms):
    t = QElapsedTimer()
    t.start()
    _orig_seek(self, position_ms)
    timings.append(("seek", t.elapsed()))


audio_engine._Engine._open_sink = _timed_open_sink
audio_engine._Engine.seek = _timed_seek

# What actually reaches the device, not just the clock. _open_sink's own cost
# says how long the restart takes; this says what it does to the waveform --
# `sink.stop()` throws away whatever was already decided for the speaker
# rather than fading it, and the new stream picks up at an unrelated sample
# value, which is a textbook click cause independent of how fast the restart
# itself is. Tapped at `_device.write()`, wrapped fresh every time
# `_open_sink` builds a new device, since that IO object is replaced on every
# seek and there is no earlier, stable hook to attach to.
import array as _array  # noqa: E402

written = []  # (wall_ms, first_sample, last_sample, frame_count)


def _wrap_device_write(engine) -> None:
    device = engine._device
    real_write = device.write

    def tapped(data):
        samples = _array.array("h")
        samples.frombytes(bytes(data)[:len(bytes(data)) // 2 * 2])
        if samples:
            written.append((wall.elapsed(), samples[0], samples[-1], len(samples) // 2))
        return real_write(data)

    device.write = tapped


_after_open_sink = audio_engine._Engine._open_sink


def _open_sink_and_tap(self):
    _after_open_sink(self)
    _wrap_device_write(self)


audio_engine._Engine._open_sink = _open_sink_and_tap

player = audio_engine.TrackPlayer()
player.setSource(PATH)

wall = QElapsedTimer()
wall.start()
positions = []  # (wall_ms, position_ms)
QTimer(app, timeout=lambda: positions.append((wall.elapsed(), player.position())),
       interval=5).start()

events = []
player.playbackStateChanged.connect(lambda s: events.append((wall.elapsed(), f"state={s}")))
player.errorOccurred.connect(lambda e, m: events.append((wall.elapsed(), f"error={e} {m}")))

seek_at = [0]


def do_seek():
    events.append((wall.elapsed(), f"setPosition({SEEK_TO_MS})"))
    seek_at[0] = wall.elapsed()
    player.setPosition(SEEK_TO_MS)


QTimer.singleShot(500, player.play)
QTimer.singleShot(int(500 + SECONDS * 1000 / 2), do_seek)
QTimer.singleShot(int(500 + SECONDS * 1000), app.quit)
app.exec()

player.shutdown()

print(f"{os.path.basename(PATH)}  seek to {SEEK_TO_MS:.0f}ms mid-playback")
for t, label in events:
    print(f"  {t:7d}ms  {label}")
print()
for label, ms in timings:
    print(f"{label}: {ms}ms")

change_at = seek_at[0]
print()
after = [(t, p) for t, p in positions if t >= change_at]
if after:
    first_t, first_p = after[0]
    print(f"first position sample after setPosition(): {first_p:.1f}ms at t={first_t - change_at:+d}ms")
    reached = next((t for t, p in after if abs(p - SEEK_TO_MS) < 5), None)
    if reached is not None:
        print(f"position reads within 5ms of the seek target at t={reached - change_at:+d}ms")
    else:
        print("position never reads within 5ms of the seek target in the sampled window")

# A gap here is what a stopped pump / a blocking sink restart looks like from
# outside the audio thread: positionChanged should arrive every PUMP_INTERVAL_MS
# (10ms) while playing, so any interval well past that around the seek is the
# restart's cost leaking into what the window would have shown on screen --
# and, if the sink itself sat stopped for it, into what came out of the
# speakers.
gaps = [(t, b_t - t) for (t, _p), (b_t, _bp) in zip(positions, positions[1:])]
window = [(t - change_at, gap) for t, gap in gaps if -100 <= t - change_at <= 600]
if window:
    longest = max(window, key=lambda x: x[1])
    print(f"longest gap between position samples in the 700ms around the seek: "
          f"{longest[1]}ms at t={longest[0]:+d}ms (pump interval is {audio_engine.PUMP_INTERVAL_MS}ms)")

frozen = [(t, p) for t, p in positions if -100 <= t - change_at <= 600]
stalls = [(a[0], b[0] - a[0]) for a, b in zip(frozen, frozen[1:]) if b[1] == a[1]]
if stalls:
    total = sum(g for _t, g in stalls)
    longest_stall = max(stalls, key=lambda x: x[1])
    print(f"position frozen for {total}ms total in that window; "
          f"longest single freeze {longest_stall[1]}ms at t={longest_stall[0] - change_at:+d}ms")

# The splice itself: whatever was last written before the old sink was
# stopped versus whatever the new sink's very first write starts with. Full
# scale for 16-bit PCM is 32768, so this is the jump as a fraction of that --
# which is what a click's loudness actually tracks, independent of how long
# the restart around it took.
print()
before_seek = [w for w in written if w[0] < change_at]
after_seek = [w for w in written if w[0] >= change_at]
if before_seek and after_seek:
    last_before = before_seek[-1]
    first_after = after_seek[0]
    jump = abs(first_after[1] - last_before[2])
    print(f"last write before the seek: ends at wall={last_before[0]}ms, "
          f"last sample={last_before[2]} ({last_before[3]} frames)")
    print(f"first write after the seek: starts at wall={first_after[0]}ms, "
          f"first sample={first_after[1]} ({first_after[3]} frames)")
    print(f"splice jump: {jump} / 32768 = {jump / 32768 * 100:.1f}% of full scale")
    gap_ms = first_after[0] - last_before[0]
    print(f"wall time between those two writes: {gap_ms}ms")
else:
    print("did not capture writes on both sides of the seek -- widen the window")
