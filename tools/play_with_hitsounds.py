"""Play a real difficulty with its notes, through the real device.

    python tools/play_with_hitsounds.py <map.osu> [rate] [seconds] [start_ms]

The end-to-end check no unit test can make. `test_hitsound_mixing.py` proves a
note lands on the output sample its music does; this proves the whole path --
parse, schedule, decode, stretch, mix, sink -- puts a hearable note on a
hearable drum, at a rate where being a few milliseconds out is audible.

It also reports what the mixer actually did, because "I heard notes" and "every
note sounded once" are different claims:

- **voiced** -- how many times the mixer started a sample. Counted by watching
  the voice list get appended to, not by diffing its length, which cancels out
  when one voice finishes in the same grain another starts.
- **offered** -- distinct notes whose source frame the mixer was handed at
  least once. **These two must be equal.** Fewer voiced means notes were
  dropped; more means the dedupe leaked, and at 0.25x a leak plays every note
  four times 5ms apart, which is a flam rather than a hit.

Read the source span, not the wall clock: at 0.25x ten seconds of playback is
two and a half seconds of song, so a sparse intro legitimately yields two
notes.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from audio import audio_engine  # noqa: E402
from audio.audio_engine import SAMPLE_RATE, TrackPlayer, frame_for_ms  # noqa: E402
from osu_io.parser import parse_osu  # noqa: E402

# gui.py's, without importing it: pulling in the window to read four filenames
# would need a QApplication with a real platform plugin and half the editor.
SAMPLES = {
    "normal": "taiko-normal-hitnormal.wav",
    "clap": "taiko-normal-hitclap.wav",
    "finish": "taiko-normal-hitfinish.wav",
    "whistle": "taiko-normal-hitwhistle.wav",
}
HITSOUND_CLAP = 8
HITSOUND_FINISH = 4


def keys_for(note) -> tuple[str, ...]:
    """gui.hitsound_keys, for circles only: a finisher is its small note's
    sample with the big one over it."""
    if not note.is_circle:
        return ()
    kat = bool(note.hit_sound & HITSOUND_CLAP)
    small = "clap" if kat else "normal"
    if note.hit_sound & HITSOUND_FINISH:
        return (small, "whistle" if kat else "finish")
    return (small,)


def main() -> None:
    path = sys.argv[1]
    rate = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    start_ms = float(sys.argv[4]) if len(sys.argv) > 4 else None

    app = QApplication([])
    document = parse_osu(path)
    folder = os.path.dirname(os.path.abspath(path))
    audio = os.path.join(folder, document.audio_filename)

    times, keys, volumes = [], [], []
    ordered = sorted(
        (float(note.time), key)
        for note in document.hit_objects
        for key in keys_for(note)
    )
    points = sorted(document.timing_points, key=lambda p: p.time)
    index, current = 0, 1.0
    for time_ms, key in ordered:
        while index < len(points) and points[index].time <= time_ms:
            current = max(0, min(100, int(points[index].volume))) / 100.0
            index += 1
        times.append(time_ms)
        keys.append(key)
        volumes.append(current)

    # Default to the first note rather than the file's start: an intro with no
    # notes in it proves nothing about note placement.
    if start_ms is None:
        start_ms = max(0.0, times[0] - 500.0) if times else 0.0

    print(f"{os.path.basename(path)}")
    print(f"  audio      {document.audio_filename}")
    print(f"  notes      {len(times)} voiced of {len(document.hit_objects)} objects")
    print(f"  playing    {seconds}s from {start_ms:.0f}ms at {rate}x")

    player = TrackPlayer()
    player.set_hitsound_samples({
        key: os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "assets", "se", filename)
        for key, filename in SAMPLES.items()
    })
    player.set_hitsounds_enabled(True)
    player.set_hitsound_volume(0.7)
    player.set_hitsound_offset_ms(0)
    player.set_hitsound_schedule(times, keys, volumes)
    player.setVolume(0.65)
    player.setSource(QUrl.fromLocalFile(audio))
    player.setPlaybackRate(rate)

    # Count what the mixer does by watching the shipped path, not by
    # reimplementing it.
    #
    # Two things here were wrong in the first version and are worth keeping
    # written down. Diffing `len(mixer._voices)` across a `mix` call
    # *under*-counts, because a voice finishing in the same grain another
    # starts cancels out; counting appends is exact. And "notes in the seconds
    # played" is the wrong denominator, because the engine runs ahead of the
    # play cursor by the sink buffer plus a grain -- so notes get voiced before
    # they are heard, and the run ends with a few voiced that never reached the
    # speaker. The denominator is the source the mixer was actually handed.
    mixer = player._engine._mixer

    class _CountingVoices(list):
        def append(self, item):
            starts[0] += 1
            super().append(item)

    starts = [0]
    covered: list[tuple[int, int]] = []
    real_mix, real_reset = mixer.mix, mixer.reset

    def counting_mix(out, source_start, frames):
        covered.append((source_start, source_start + frames))
        real_mix(out, source_start, frames)

    def counting_reset(source_frame):
        real_reset(source_frame)
        mixer._voices = _CountingVoices()

    mixer.mix = counting_mix
    mixer.reset = counting_reset
    mixer._voices = _CountingVoices()

    def begin() -> None:
        player.setPosition(start_ms)
        player.play()

    def finish() -> None:
        # Every note whose source frame the mixer was handed at least once. A
        # note covered by several grains (which at rate < 1 is every note,
        # about 1/rate times) counts once here, so this is the number the
        # dedupe has to reproduce.
        frames = [frame_for_ms(time_ms) for time_ms in times]
        offered = set()
        for low, high in covered:
            for index, frame in enumerate(frames):
                if low <= frame < high:
                    offered.add(index)
        # Song time, so it scales with the rate: 10s of playback at 0.25x is
        # 2.5s of the chart, and comparing against 10s of chart would call a
        # correct run a dropout.
        heard = frame_for_ms(start_ms + seconds * 1000.0 * rate)
        print(f"  grains     {len(covered)}, "
              f"{sum(h - l for l, h in covered) / SAMPLE_RATE:.1f}s of source")
        print(f"  voiced     {starts[0]}")
        print(f"  offered    {len(offered)}"
              + ("" if starts[0] == len(offered) else "   <-- MISMATCH"))
        inside = sum(1 for f in frames
                     if frame_for_ms(start_ms) <= f < heard)
        print(f"  heard      {inside} of them inside the "
              f"{seconds * rate:.1f}s of song that {seconds}s at {rate}x covers"
              f" (the rest is the engine's lookahead)")
        player.shutdown()
        app.quit()

    # After the decode has had a moment: seeking into a track that has not been
    # decoded that far yet lands on silence.
    QTimer.singleShot(1500, begin)
    QTimer.singleShot(int(1500 + seconds * 1000) + 500, finish)
    app.exec()


if __name__ == "__main__":
    main()
