"""A whole song at another rate, to a file osu! plays (the Rate Changer, owner
2026-10-23).

The playback engine's own `TimeStretcher`, run offline: the WSOLA the owner
tuned by ear for the speed buttons when the pitch is kept, its resample path
(osu!'s Frequency, NC/DC) when it is not. Nothing here touches Qt, so it runs
in a worker *process* -- the search holds the GIL (0.22x realtime stereo), and
in a thread a four-minute song is a minute of the window stuttering.

Written as Ogg Vorbis through `ffmpeg` when it is on PATH, else as WAV. Not MP3
through Qt: Qt hands MP3 to Media Foundation, which is fixed at 96 kbps and
wrote a header that decoded 0.89s of a 3s file (measured 2026-10-23). And not
MP3 at all by preference, because MP3's encoder delay is an offset of its own
that the decoder may or may not take back off.
"""
from __future__ import annotations

import array
import shutil
import subprocess
import wave
from pathlib import Path

from audio.audio_engine import (
    CHANNELS, SAMPLE_RATE, SEQUENCE_FRAMES, TimeStretcher, downmix_to_mono, frame_for_ms, grain_offset_ms,
)

# LAME V0 (~245 kbps VBR), as osu-trainer writes MP3 (owner, 2026-10-23),
# above its MEDIUM. The encoder's leading delay is not trimmed here: LAME
# writes it into its gapless header and osu!'s BASS removes it on decode
# (BASS_CONFIG_MP3_OLDGAPS exists only to turn that off).
MP3_QUALITY = "0"
OGG_QUALITY = "8"  # libvorbis q8, ~256 kbps
# The user's choice (owner, 2026-10-23): suffix -> ffmpeg's codec arguments.
ENCODERS = {".mp3": ("libmp3lame", MP3_QUALITY), ".ogg": ("libvorbis", OGG_QUALITY)}


def render(pcm: bytes, rate: float, pitch: bool) -> bytes:
    """`pcm` (44.1kHz stereo s16) played at `rate`, as the same."""
    source = array.array("h")
    source.frombytes(pcm)
    stretcher = TimeStretcher()
    stretcher.pitch = pitch
    stretcher.reset(0.0)
    mono = downmix_to_mono(source)
    out = bytearray()
    while True:
        block = stretcher.pull(source, mono, SEQUENCE_FRAMES * 16, rate)
        if not block:
            break
        out += block
    if not pitch:
        # The splice search lands ahead of the read head, so the audio runs
        # `grain_offset_ms` of song time early (see `_MEASURED_OFFSET_ANCHORS_MS`).
        # The engine corrects its playhead for it; a file has no playhead, so
        # the audio itself is moved back by that much output time.
        out[:0] = bytes(frame_for_ms(grain_offset_ms(rate) / rate) * CHANNELS * 2)
    return bytes(out)


def write_audio(pcm: bytes, path_stem: Path, suffix: str = ".mp3") -> Path:
    """`pcm` as `path_stem` + `suffix` (.mp3 or .ogg) if ffmpeg can make
    one, else .wav."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        codec, quality = ENCODERS[suffix]
        target = path_stem.with_name(path_stem.name + suffix)
        done = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-f", "s16le", "-ar", str(SAMPLE_RATE),
             "-ac", str(CHANNELS), "-i", "pipe:0", "-c:a", codec, "-q:a", quality, str(target)],
            input=pcm, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode == 0:
            return target
        target.unlink(missing_ok=True)  # a build without that encoder: WAV it is
    target = path_stem.with_name(path_stem.name + ".wav")
    with wave.open(str(target), "xb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)
    return target


def render_to_file(pcm: bytes, rate: float, pitch: bool, path_stem: str, suffix: str = ".mp3") -> str:
    """Worker-process entry point: render, write, return the file written."""
    return str(write_audio(render(pcm, rate, pitch), Path(path_stem), suffix))
