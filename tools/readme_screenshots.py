"""Take the README's screenshots from the running app.

    python tools/readme_screenshots.py <chart.osu> <gimmick.osu> [out_dir]
        [--at MS] [--gimmick-at MS]

Real platform (the offscreen one has no fonts), at 1920x1080. Writes nothing
of the user's: settings come from the test suite's private copy (importing
`tests` swaps it in), and the song index and gimmick index are read from
temporary copies. Nothing is saved to the map.

The README uses Ph0eNiiXZ's Tanchiky vs. siromaru - Crystal Gravity for the
charting shots -- every difficulty stacked on the Editor page, then
[Dimensional Distortion] alone with its SV and the gameplay preview, at 0:47
(kiai) -- and Hyper Bass (RENKA chan Drop) [Drop the GIMMICK], which has every
gimmick the editor writes, for the Gimmick page at 1:55.8. The built-in skin
throughout.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401,E402  -- private settings, before any window exists

from PySide6.QtCore import QStandardPaths  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import gui  # noqa: E402
from app.settings import APPLICATION_NAME, ORGANIZATION_NAME  # noqa: E402

def option(name: str, default: float) -> float:
    return float(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


args = [a for i, a in enumerate(sys.argv[1:], 1)
        if not a.startswith("--") and not sys.argv[i - 1].startswith("--")]
MAP = Path(args[0]).resolve()
GIMMICK_MAP = Path(args[1]).resolve()
OUT = Path(args[2]) if len(args) > 2 else ROOT / "docs" / "screenshots"
AT_MS = option("--at", 47000.0)
GIMMICK_AT_MS = option("--gimmick-at", 115800.0)
OUT.mkdir(parents=True, exist_ok=True)

app = QApplication.instance() or QApplication([])
app.setOrganizationName(ORGANIZATION_NAME)
app.setApplicationName(APPLICATION_NAME)

# The real song index and gimmick index, copied, so a warm scan and the map's
# pairing are there and nothing writes back.
scratch = Path(tempfile.mkdtemp(prefix="taiko-readme-"))
real_root = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))
copies = {}
for name in ("song_index.json", "gimmick_index.json"):
    source = real_root / name
    copies[name] = scratch / name
    if source.is_file():
        shutil.copy2(source, copies[name])
gui.MainWindow._library_cache_path = lambda self: copies["song_index.json"]
gui.MainWindow._gimmick_index_path = lambda self: copies["gimmick_index.json"]


class _UseCurrent:
    """Answers the gimmick entry question with "use this difficulty"."""

    CREATE = gui.GimmickEntryDialog.CREATE
    USE_CURRENT = gui.GimmickEntryDialog.USE_CURRENT
    CANCEL = gui.GimmickEntryDialog.CANCEL
    reference = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    def exec(self) -> int:
        return 1

    def selected_action(self) -> str:
        return self.USE_CURRENT

    def selected_reference(self):
        return None

    def deleteLater(self) -> None:
        pass


def settle(rounds: int = 12, wait_ms: int = 0) -> None:
    """Let the window catch up. `wait_ms` is real time, for what animates
    or loads on a timer: the notes' glide, the banner's crossfade."""
    for _ in range(rounds):
        app.processEvents()
    if wait_ms:
        QTest.qWait(wait_ms)


def shoot(widget, name: str) -> None:
    settle()
    path = OUT / f"{name}.png"
    widget.grab().save(str(path))
    print(f"{path.name}: {path.stat().st_size // 1024} KB", flush=True)


# The built-in note art, not whatever skin the user plays with: the README
# shows the app, and a personal skin is theirs. Private copy, so theirs stays.
from app import settings as _settings  # noqa: E402
_settings.SettingsManager().set_value("appearance/skin", "")
# And the defaults for what the screenshots are of, whatever the copy holds.
_settings.SettingsManager().set_value("appearance/view_opacity", 100)
# The default theme, which is what a new user sees: the test settings pin the
# colours as written, which stopped being the default when it moved to #1f1e33.
from ui import theme  # noqa: E402
theme.install(theme.DEFAULT)

window = gui.MainWindow()
window.resize(1920, 1080)
window.showMaximized()
settle()

# -- song select ---------------------------------------------------------------
window.start_library()
library = window._library
for _ in range(20000):
    if getattr(library, "_scan_iterator", None) is None:
        break
    library.scan_step()
settle()
for row in range(window.song_list.count()):
    folder = window.song_list.item(row).data(gui.Qt.UserRole)
    if folder and Path(folder).resolve() == MAP.parent:
        window.song_list.setCurrentRow(row)
        break
settle(40, wait_ms=1500)
library.stop_preview()
shoot(window, "song-select")

def only_views(*views: tuple[str, Path]) -> None:
    """Close every Editor view, then open `views` in order."""
    for frame in list(window._editor_views):
        window._close_editor_view(frame)
    for view_type, path in views:
        window._add_editor_view(view_type, path)


# -- editor: every difficulty of the song, easiest first ---------------------------
window._load_map_path(MAP)
window._show_page(gui.PAGE_EDITOR)
difficulties = sorted(
    MAP.parent.glob("*.osu"),
    key=lambda path: len(window._ensure_state(path.resolve()).document.hit_objects),
)
only_views(*[("chart", path.resolve()) for path in difficulties])
window.seek_audio(AT_MS)
shoot(window, "editor")

# -- one difficulty: its chart, its SV and the gameplay preview ----------------------
only_views(("chart", MAP), ("sv", MAP), ("gameplay", MAP))
window.seek_audio(AT_MS)
shoot(window, "gameplay-preview")

# -- fancy arranger: a run of notes as a star --------------------------------------
window._show_page(gui.PAGE_FANCY)
state = window.state
window.seek_audio(AT_MS - 2000)
chosen = {
    note.original_index for note in state.document.hit_objects
    if note.is_circle and AT_MS - 4000 <= note.time <= AT_MS
}
window._selection_changed(chosen)
page = window.control_tabs.widget(0)
page.combo.setCurrentIndex(page.combo.findData("star"))
settle(30, wait_ms=1200)
shoot(window, "fancy-arranger")

# -- gimmick editor ----------------------------------------------------------------
window._load_map_path(GIMMICK_MAP)
original_entry = gui.GimmickEntryDialog
gui.GimmickEntryDialog = _UseCurrent
try:
    if window._enter_gimmick_page():
        window._show_page(gui.PAGE_GIMMICK)
        window.seek_audio(GIMMICK_AT_MS)
        shoot(window, "gimmick-editor")
finally:
    gui.GimmickEntryDialog = original_entry

# -- dialogs -----------------------------------------------------------------------
config = window._gimmick_config("barline")
dialogs = {
    "config-barline": lambda: gui.GimmickConfigDialog(config, "barline", window._gimmick_config("fake_slider"), window),
    "convert-notes": lambda: gui.ConvertNotesDialog(config, "barline", window, GIMMICK_AT_MS - 4000, GIMMICK_AT_MS),
    "generate-sv": lambda: gui.SVFunctionDialog(AT_MS - 4000, AT_MS, window, initial_rate=1.0, final_rate=2.5),
    "settings": lambda: gui.SettingsDialog(window.settings, window.shortcuts, window),
}
for name, make in dialogs.items():
    dialog = make()
    if name == "generate-sv":
        dialog.function_buttons["sin_in"].setChecked(True)
    if name == "settings":
        dialog.nav.setCurrentRow(1)
    if name == "config-barline":
        dialog.show()
        settle()
        dialog.kat_spacing2_spin.setFocus()
    dialog.show()
    shoot(dialog, name)
    dialog.close()

shutil.rmtree(scratch, ignore_errors=True)
# The audio engine's thread would hold the process open; nothing here needs a
# clean shutdown.
os._exit(0)
