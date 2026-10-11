"""How far the music's attack sits from each note's millisecond, per map.

    python tools/measure_note_vs_music.py <map.osu> [map.osu ...]
    python tools/measure_note_vs_music.py --selftest

The slow-rate "hitsound lands before the kick" report was a constant song-time
offset, not the stretch: inaudible at 1.0x and four times longer in wall time
at 0.25x. This measures it where the stretch cannot be involved -- straight off
the decoded source, exactly as `QAudioDecoder` hands it to the engine -- so the
number is the gap between where the chart says a hit is and where this app's
decode of the song puts it.

Mappers time notes to the attack as osu! plays it, so across a whole
difficulty the median gap is the decode's disagreement with osu!, plus this
detector's own bias. The bias is the same for every file, which is why the
number to read is **MP3 against OGG**, and why `--selftest` has to read about
zero on a source whose attacks are known before any real map is believed.

For MP3s it also prints the LAME tag's encoder delay, the gapless-playback
field one decoder honours and another may not.
"""
from __future__ import annotations

import array
import math
import random
import statistics
import sys

sys.path.insert(0, __file__.rsplit("tools", 1)[0])
sys.path.insert(0, __file__.rsplit("tools", 1)[0] + "tools")

from PySide6.QtWidgets import QApplication  # noqa: E402

from audio.audio_engine import CHANNELS, SAMPLE_RATE, downmix_to_mono, frame_for_ms  # noqa: E402
from measure_stretch_quality import decode  # noqa: E402
from osu_io.parser import parse_osu  # noqa: E402

BLOCK = SAMPLE_RATE // 1000        # 1ms of energy per step
REACH_MS = 60                      # search either side of the note
RISE_BLOCKS = 3                    # an attack is a jump over this many ms


def envelope(mono: array.array, frame: int) -> list[float] | None:
    """Log energy per 1ms block over [frame - REACH_MS - RISE, frame + REACH_MS)."""
    low = frame - REACH_MS * BLOCK - RISE_BLOCKS * BLOCK
    high = frame + REACH_MS * BLOCK
    if low < 0 or high > len(mono):
        return None
    return [math.log(sum(v * v for v in mono[start:start + BLOCK]) + 1.0)
            for start in range(low, high, BLOCK)]


def steepest_rise_ms(energies: list[float]) -> float | None:
    """Signed ms from the note to the steepest rise in `energies`."""
    best_k, best_rise = None, 0.0
    for k in range(RISE_BLOCKS, len(energies)):
        rise = energies[k] - energies[k - RISE_BLOCKS]
        if rise > best_rise:
            best_k, best_rise = k, rise
    if best_k is None:
        return None
    # The first block of the jump: comparing k against k - RISE_BLOCKS peaks
    # at the block the energy arrives in, and ties go to the earliest.
    return float(best_k - RISE_BLOCKS - REACH_MS)


def attack_offset_ms(mono: array.array, frames: list[int]) -> float | None:
    """One number for a whole difficulty, from the *averaged* envelope.

    Per note, busy music has an attack somewhere in any 120ms window, so each
    note's own steepest rise scatters +-30ms and the median of that is pulled
    to the middle of the window whatever the truth is -- the first version of
    this read ~0 on every map for exactly that reason. Averaged across every
    note, unrelated attacks smear flat and the one the mapper timed to stays
    sharp.
    """
    total = None
    count = 0
    for frame in frames:
        energies = envelope(mono, frame)
        if energies is None:
            continue
        total = energies if total is None else [a + b for a, b in zip(total, energies)]
        count += 1
    if not count:
        return None
    return steepest_rise_ms([value / count for value in total])


def lame_delay_ms(path: str) -> float | None:
    with open(path, "rb") as handle:
        head = handle.read(16384)
    at = head.find(b"LAME")
    if at < 0 or at + 24 > len(head):
        return None
    packed = head[at + 21:at + 24]
    delay = (packed[0] << 4) | (packed[1] >> 4)
    return delay * 1000.0 / SAMPLE_RATE


def report(name: str, mono: array.array, frames: list[int], extra: str = "") -> None:
    """The whole-difficulty number, plus the same from odd and even notes
    alone: two halves that disagree mean the average never settled."""
    whole = attack_offset_ms(mono, frames)
    odd = attack_offset_ms(mono, frames[1::2])
    even = attack_offset_ms(mono, frames[0::2])
    if whole is None:
        print(f"{name}: no measurable notes")
        return
    print(f"{name}: notes={len(frames)} attack={whole:+.0f}ms "
          f"halves=({odd:+.0f}, {even:+.0f}){extra}")


def selftest() -> None:
    """Noise bed with sharp decaying bursts at known frames. Must read ~0."""
    rng = random.Random(1)
    seconds = 20
    mono = array.array("h", (rng.randint(-300, 300) for _ in range(SAMPLE_RATE * seconds)))
    times = list(range(500, seconds * 1000 - 500, 287))
    for t in times:
        start = frame_for_ms(t)
        for i in range(SAMPLE_RATE // 20):
            value = mono[start + i] + int(12000 * math.exp(-i / 800.0)
                                          * math.sin(i * 0.05))
            mono[start + i] = max(-32768, min(32767, value))
    report("selftest (expect +0)", mono, [frame_for_ms(t) for t in times])
    report("selftest shifted (expect +25)", mono, [frame_for_ms(t - 25) for t in times])


def main() -> None:
    if sys.argv[1:] == ["--selftest"]:
        selftest()
        return
    app = QApplication.instance() or QApplication(sys.argv[:1])
    for osu_path in sys.argv[1:]:
        doc = parse_osu(osu_path)
        audio = str(doc.source_path.parent / doc.audio_filename)
        pcm = decode(audio, app)
        if len(pcm) < CHANNELS:
            print(f"{osu_path}: decode failed")
            continue
        mono = downmix_to_mono(pcm)
        times = sorted({note.time for note in doc.hit_objects if note.is_circle})
        delay = lame_delay_ms(audio) if audio.lower().endswith(".mp3") else None
        extra = f"  lame_delay={delay:.2f}ms" if delay is not None else ""
        ext = audio.rsplit(".", 1)[-1].lower()
        report(f"[{ext}] {doc.source_path.parent.name} [{doc.version}]",
               mono, [frame_for_ms(t) for t in times], extra)


if __name__ == "__main__":
    main()
