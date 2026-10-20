import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import launcher_entry


class FakeSettings(dict):
    def value(self, key, default=None):
        return self.get(key, default)

    def set_value(self, key, value):
        self[key] = value


class LauncherEntryTests(unittest.TestCase):
    def test_linux_entry_is_searchable_and_rewritten_when_moved(self):
        with tempfile.TemporaryDirectory() as data, \
                mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": data}):
            settings = FakeSettings()
            launcher_entry.register(settings)
            entry = Path(data) / "applications" / "taiko-fancy-arranger.desktop"
            text = entry.read_text(encoding="utf-8")
            self.assertIn("Name=Taiko Fancy Arranger", text)
            self.assertIn("Keywords=TaikoFancyArranger;", text)
            self.assertIn("main.py", text)
            # A moved folder is a stale target: written again.
            settings["launcher/target"] = "/old/place"
            entry.write_text("stale", encoding="utf-8")
            launcher_entry.register(settings)
            self.assertIn("[Desktop Entry]", entry.read_text(encoding="utf-8"))

    def test_windows_from_source_writes_nothing(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(launcher_entry.subprocess, "Popen") as popen:
            launcher_entry.register(FakeSettings())
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
