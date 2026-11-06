"""Whether a lead-in file starts its music exactly `ms` later, decoded back.

    python tools/measure_lead_in.py <real song> [ms]

Decodes the song with ffmpeg, writes it through `rate_render.lead_in_to_file`
(the Misc page's path), decodes that back, and finds the shift that lines the
two up best over the first ten seconds of music. It must be `ms` to within a
frame: MP3's encoder delay is LAME's gapless header's job, and this is where
it would show if it were not taken back off. Needs ffmpeg on PATH.
"""
from __future__ import annotations

import array
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio import rate_render  # noqa: E402
from audio.audio_engine import CHANNELS, SAMPLE_RATE, frame_for_ms  # noqa: E402


def decode(path: Path) -> array.array:
    raw = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(path),
                          "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "pipe:1"],
                         capture_output=True, check=True).stdout
    samples = array.array("h")
    samples.frombytes(raw)
    return samples


def envelope(samples: array.array, start: int, frames: int) -> list[int]:
    return [abs(samples[(start + i) * CHANNELS]) for i in range(frames)]


def main() -> None:
    source = Path(sys.argv[1])
    ms = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    original = decode(source)
    suffix = source.suffix.lower() if source.suffix.lower() in rate_render.ENCODERS else ".mp3"
    with tempfile.TemporaryDirectory() as folder:
        written = Path(rate_render.lead_in_to_file(original.tobytes(), ms, str(Path(folder) / "out"), suffix))
        back = decode(written)
    expected = frame_for_ms(ms)
    # The first loud stretch of music, then the shift that matches it best,
    # searched 20ms either side of the expected one.
    start = next(i for i in range(len(original) // CHANNELS) if abs(original[i * CHANNELS]) > 2000)
    window = SAMPLE_RATE // 4
    reference = envelope(original, start, window)
    best = min(range(expected - 882, expected + 883),
               key=lambda shift: sum(abs(a - b) for a, b in zip(reference, envelope(back, start + shift, window))))
    print(f"{source.name} -> {written.suffix}: asked {ms} ms ({expected} frames), "
          f"music lands at {best} frames, {best - expected:+d} frames ({(best - expected) / SAMPLE_RATE * 1000:+.2f} ms)")


if __name__ == "__main__":
    main()
