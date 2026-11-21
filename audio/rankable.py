"""Whether a song file meets osu!'s ranking criteria for audio, and a
converter that makes one that does (owner, 2026-11-21).

The rules (osu.ppy.sh/wiki/en/Ranking_criteria, Audio): MP3 or Ogg Vorbis,
average bitrate at most 192 kbps (MP3) / 208 kbps (Ogg), at least 128 kbps
if the source has that, sample rate at most 48 kHz, and never encoded upward
from a lower bitrate. The last is why every writer here takes its target from
`target_kbps`: LAME V0 (~245 kbps) and Vorbis q8 (~256 kbps), which the Rate
Changer and the lead-in once wrote, were over the cap on every song. "Not
upward" cannot be proven from a file alone, so it is only honoured: an output
is never given more bits than its source had.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_KBPS = {".mp3": 192, ".ogg": 208}
MIN_KBPS = 128
MAX_RATE = 48000
# What is written: under the cap with room for an ABR encoder to overshoot.
WRITE_CAP_KBPS = 184
MP3_STEPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class AudioInfo:
    codec: str
    kbps: float | None
    sample_rate: int
    channels: int


def probe(path: Path) -> AudioInfo | None:
    """What ffprobe says about `path`'s audio, None if it cannot be read."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    done = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,sample_rate,channels,bit_rate:format=bit_rate", "-of", "json", str(path)],
        capture_output=True, creationflags=_NO_WINDOW)
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout)
        stream = data["streams"][0]
        # The container's figure is the file's average; a stream's own can be absent (VBR).
        rate = data.get("format", {}).get("bit_rate") or stream.get("bit_rate")
        return AudioInfo(stream["codec_name"], float(rate) / 1000 if rate else None,
                         int(stream["sample_rate"]), int(stream["channels"]))
    except (KeyError, IndexError, ValueError):
        return None


def problems(path: Path) -> list[str]:
    """Each way `path` breaks the audio rules; empty means rankable."""
    suffix = Path(path).suffix.lower()
    info = probe(Path(path))
    if info is None:
        return ["ffprobe could not read the file (is ffmpeg installed and on PATH?)"]
    found = []
    if suffix not in MAX_KBPS or info.codec not in ("mp3", "vorbis"):
        found.append(f"Format is {info.codec} ({suffix or 'no extension'}); it must be MP3 or Ogg Vorbis.")
    cap = MAX_KBPS.get(suffix, WRITE_CAP_KBPS)
    if info.kbps is not None and info.kbps > cap + 0.5:
        found.append(f"Average bitrate is {info.kbps:.0f} kbps; the cap is {cap}.")
    if info.kbps is not None and info.kbps < MIN_KBPS - 0.5:
        found.append(f"Average bitrate is {info.kbps:.0f} kbps, under {MIN_KBPS}; fine only if no better source exists.")
    if info.sample_rate > MAX_RATE:
        found.append(f"Sample rate is {info.sample_rate} Hz; the cap is {MAX_RATE}.")
    return found


def target_kbps(source: Path | None, suffix: str = ".mp3") -> int:
    """The bitrate to write at: the cap, or the source's own if lower."""
    info = probe(Path(source)) if source else None
    cap = min(WRITE_CAP_KBPS, MAX_KBPS.get(suffix, WRITE_CAP_KBPS))
    if info is None or info.kbps is None:
        return cap
    wanted = int(info.kbps * 0.98)  # container overhead counts toward the average
    if suffix == ".mp3":
        # CBR only comes in these steps; anything between rounds *up*, over the cap.
        return max((rate for rate in MP3_STEPS if rate <= min(cap, wanted)), default=MP3_STEPS[0])
    return max(32, min(cap, wanted))


def convert(source: Path, stem: Path) -> Path:
    """`source` re-encoded as a rankable MP3 or Ogg beside `stem`. Raises
    RuntimeError with ffmpeg's message; never overwrites."""
    from audio import rate_render

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not on PATH")
    suffix = source.suffix.lower() if source.suffix.lower() in rate_render.ENCODERS else ".mp3"
    info = probe(source)
    target = stem.with_name(stem.name + suffix)
    rate = min(info.sample_rate, MAX_RATE) if info else 44100
    codec = rate_render.ENCODERS[suffix]
    done = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-i", str(source), "-vn", "-ar", str(rate),
         "-c:a", codec, "-b:a", f"{target_kbps(source, suffix)}k", str(target)],
        capture_output=True, creationflags=_NO_WINDOW)
    if done.returncode != 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(done.stderr.decode("utf-8", "replace").strip() or "ffmpeg failed")
    return target
