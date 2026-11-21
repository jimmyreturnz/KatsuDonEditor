"""Audio the app writes meets osu!'s ranking criteria (owner, 2026-11-21):
MP3 <= 192 kbps, never encoded upward from its source, sample rate <= 48 kHz."""
from __future__ import annotations

import array
import shutil
import tempfile
import unittest
from pathlib import Path

from audio import rankable, rate_render


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class RankableTests(unittest.TestCase):
    def _song(self, folder: str, kbps: int) -> Path:
        noise = array.array("h", [(i * 7919) % 20000 - 10000 for i in range(44100 * 2)]).tobytes() * 30
        return rate_render.write_audio(noise, Path(folder) / f"src{kbps}", ".mp3", kbps)

    def test_a_lead_in_is_written_under_the_cap_and_not_above_its_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self._song(folder, 160)
            kbps = rankable.target_kbps(source, ".mp3")
            self.assertLessEqual(kbps, 160)
            out = Path(rate_render.lead_in_to_file(b"\0" * 4000, 500, str(Path(folder) / "out"), ".mp3", kbps))
            self.assertEqual(rankable.problems(out), [])

    def test_the_cap_applies_to_a_louder_source_and_the_converter_fixes_it(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self._song(folder, 320)
            self.assertTrue(any("cap" in text for text in rankable.problems(source)))
            fixed = rankable.convert(source, Path(folder) / "fixed")
            self.assertEqual(rankable.problems(fixed), [])


if __name__ == "__main__":
    unittest.main()
