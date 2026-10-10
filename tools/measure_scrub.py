"""What a timeline *drag* (not a single click) does -- repeated seeks in a
burst, the way `mouseMoveEvent` in gui.py fires `seek_requested` on every
move while the button is held (gui.py ~2434-2437, 3992, 5380/5470), each of
which reaches `TrackPlayer.setPosition` -> `_Engine.seek`.

    python tools/measure_scrub.py [audio file] [n_seeks] [interval_ms] [seconds]

`measure_seek.py` already showed a *single* seek's sink-restart costs about
10-40ms and is mostly harmless once landed (the position freeze it reports
turns out to be 5ms-poll-vs-10ms-pump aliasing, not a real stall -- see
`tools/measure_position_baseline.py`... except that harness lives in the
session scratchpad, not tracked; the number was 348ms/695ms frozen with NO
seek at all, against 421ms/700ms around a seek, which is the same thing
within noise). What a *single* seek does not cover is a drag: N seeks fired
faster than `_open_sink()`'s own ~10ms lands, back to back, each tearing the
sink down and rebuilding it. This taps `_device.write()` the same way
`measure_seek.py` does, across the whole burst, and reports the total silent
wall time in it -- which is what a scrub actually sounds like, as opposed to
what one click sounds like.
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

N_SEEKS = int(raw[0]) if len(raw) > 0 else 20
INTERVAL_MS = int(raw[1]) if len(raw) > 1 else 15
SECONDS = float(raw[2]) if len(raw) > 2 else 3.0

app = QCoreApplication([])

# Tap every device write across the whole run, re-wrapped on every _open_sink
# the way measure_seek.py does, since the IO object is replaced each time.
written = []  # (wall_ms, frame_count)
_orig_open_sink = audio_engine._Engine._open_sink


def _wrap_device_write(engine) -> None:
    device = engine._device
    real_write = device.write

    def tapped(data):
        n = len(bytes(data)) // (2 * audio_engine.CHANNELS)
        if n:
            written.append((wall.elapsed(), n))
        return real_write(data)

    device.write = tapped


def _open_sink_and_tap(self):
    _orig_open_sink(self)
    _wrap_device_write(self)


audio_engine._Engine._open_sink = _open_sink_and_tap

player = audio_engine.TrackPlayer()
player.setSource(PATH)

wall = QElapsedTimer()
wall.start()

scrub_start = [0]
scrub_end = [0]


def do_scrub():
    scrub_start[0] = wall.elapsed()
    base = 10000.0

    def fire(i):
        # A drag sweeping the mouse across the bar: position changes every
        # move, same as gui.py's mouseMoveEvent -> seek_requested chain.
        player.setPosition(base + i * 200.0)
        if i == N_SEEKS - 1:
            scrub_end[0] = wall.elapsed()

    for i in range(N_SEEKS):
        QTimer.singleShot(i * INTERVAL_MS, lambda i=i: fire(i))


QTimer.singleShot(500, player.play)
QTimer.singleShot(700, do_scrub)
QTimer.singleShot(int(700 + N_SEEKS * INTERVAL_MS + SECONDS * 1000), app.quit)
app.exec()

player.shutdown()

print(f"{os.path.basename(PATH)}  {N_SEEKS} seeks, {INTERVAL_MS}ms apart "
      f"(a drag firing seek_requested on every mouse move)")
print(f"scrub burst: wall {scrub_start[0]}ms to ~{scrub_start[0] + N_SEEKS * INTERVAL_MS}ms")

# Gaps between consecutive writes during and shortly after the burst: any gap
# well over PUMP_INTERVAL_MS (10ms) is the device receiving nothing -- silence
# -- for that long, which is a real dropout, not a readout artifact (unlike
# the position-freeze number, this taps the actual bytes handed to the sink).
burst_lo = scrub_start[0] - 50
burst_hi = scrub_start[0] + N_SEEKS * INTERVAL_MS + 300
window = [(t, n) for t, n in written if burst_lo <= t <= burst_hi]
print(f"writes in [{burst_lo}, {burst_hi}]ms: {len(window)}")
if len(window) > 1:
    gaps = [(a[0], b[0] - a[0]) for a, b in zip(window, window[1:])]
    total_gap = sum(g for _t, g in gaps)
    span = window[-1][0] - window[0][0]
    longest = max(gaps, key=lambda x: x[1])
    print(f"span covered by writes: {span}ms; sum of inter-write gaps: {total_gap}ms")
    print(f"longest single gap: {longest[1]}ms at t={longest[0] - scrub_start[0]:+d}ms "
          f"relative to scrub start")
    over_pump = [g for _t, g in gaps if g > audio_engine.PUMP_INTERVAL_MS + 2]
    print(f"gaps > pump interval + 2ms (a real stall, not just pump jitter): "
          f"{len(over_pump)}, totalling {sum(over_pump)}ms")
else:
    print("not enough writes captured in the window -- widen it")
