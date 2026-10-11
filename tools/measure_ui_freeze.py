"""How long the GUI thread freezes on opening a map, Settings, or a theme.

    python tools/measure_ui_freeze.py [map.osu] [--runs N] [--profile WHAT]

Real settings and library (main()'s organization and application names), a
1920x1080 window kept off screen, on the real platform. Each action is timed
from the call to the end of the paint it causes -- the freeze the owner sees
is both. WHAT is `map`, `settings` or `theme`: that action runs once more
under cProfile and the top of it is printed by cumulative time.

Without a map argument the first difficulty in the song list is opened.
The first run of each is cold (first .osu read, first stylesheet polish);
the rest show what a repeat costs.
"""
from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
from ui import gui, theme  # noqa: E402
from ui.settings_dialog import SettingsDialog  # noqa: E402

app.setOrganizationName(gui.ORGANIZATION_NAME)
app.setApplicationName(gui.APPLICATION_NAME)


def settle() -> None:
    """Run the event loop until the paints the action queued are done."""
    for _ in range(5):
        app.processEvents()
    # processEvents() never runs deleteLater: without this every closed
    # Settings dialog lived on, and each theme switch restyled all of them
    # (1.4s where the real switch is 0.5s).
    app.sendPostedEvents(None, QEvent.DeferredDelete)


def open_map(window, path: Path) -> None:
    window._load_map_path(path, refresh_difficulties=True)
    settle()


def open_settings(window) -> None:
    # open_settings() is modal; this is what it builds and shows before exec.
    dialog = SettingsDialog(window.settings, window.shortcuts, window)
    dialog.setAttribute(Qt.WA_DontShowOnScreen, True)
    dialog.show()
    settle()
    dialog.reject()
    dialog.deleteLater()
    settle()


def switch_theme(window, name: str) -> None:
    theme.switch(name)
    window.retheme()
    settle()


def timed(label: str, action, runs: int) -> None:
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        action()
        times.append((time.perf_counter() - start) * 1000)
    rest = sorted(times[1:])
    median = rest[len(rest) // 2] if rest else times[0]
    print(f"{label:10} cold {times[0]:7.1f}ms   repeat median {median:7.1f}ms   "
          f"all {' '.join(f'{t:.0f}' for t in times)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("map", nargs="?")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--profile", choices=("map", "settings", "theme"))
    args = parser.parse_args()

    window = gui.MainWindow()
    window.resize(1920, 1080)
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.show()
    settle()
    if args.map:
        path = Path(args.map).resolve()
    else:
        window._library.start()
        deadline = time.perf_counter() + 30
        while not window._library.song_list.count() and time.perf_counter() < deadline:
            app.processEvents()
            time.sleep(0.002)
        settle()
        # The song select picks a song once the list has rows; its
        # difficulties are what an "Edit This Difficulty" would open.
        shown = getattr(window._library, "_shown_difficulties", {})
        path = Path(next(iter(shown))) if shown else None
        if path is None:
            raise SystemExit("pass a map: no difficulty found in the song list")
    print(f"map: {path}")

    original = theme.active()
    other = next(name for name in theme.THEMES if name != original)
    state = {"name": original}

    def toggle():
        state["name"] = other if state["name"] == original else original
        switch_theme(window, state["name"])

    actions = {
        "map": lambda: open_map(window, path),
        "settings": lambda: open_settings(window),
        "theme": toggle,
    }
    for label, action in actions.items():
        timed(label, action, args.runs)
    if state["name"] != original:
        switch_theme(window, original)

    if args.profile:
        profile = cProfile.Profile()
        profile.runcall(actions[args.profile])
        if args.profile == "theme":
            switch_theme(window, original)
        pstats.Stats(profile).sort_stats("cumulative").print_stats(35)


if __name__ == "__main__":
    main()
