import tempfile
import unittest
from pathlib import Path
from app.security_utils import UnsafeInputError, resolve_child_asset, read_limited

class SecurityTests(unittest.TestCase):
    def test_asset_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "map"; base.mkdir()
            outside = Path(td) / "outside.mp3"; outside.write_bytes(b"x")
            with self.assertRaises(UnsafeInputError):
                resolve_child_asset(base, "../outside.mp3", suffixes={".mp3"}, max_bytes=10)
    def test_oversized_read_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"map.osu"; path.write_bytes(b"12345")
            with self.assertRaises(UnsafeInputError): read_limited(path, 4)

if __name__ == "__main__": unittest.main()
