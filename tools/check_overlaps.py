"""Find laid-out widgets that overlap a sibling or spill out of their parent.

    python tools/check_overlaps.py [--lang ja] [--theme nso] [--width 1920] [--height 1040] [--shots DIR]

Walks every page, and on the Fancy Arranger page every transformation in both
groups and both modes, since each one rebuilds the parameter column. Only
widgets a layout placed are compared -- overlays (glides, sheens, ripples)
and scroll-area bodies sit outside layouts on purpose.

Run it on the **real** platform, like check_button_widths.py: the offscreen
plugin ships no fonts, every label measures as tofu, and nothing overflows.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=freetype")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget


def settle(app: QApplication, ms: int = 450) -> None:
    """Long enough for a page slide or a segment glide to finish."""
    end = time.perf_counter() + ms / 1000
    while time.perf_counter() < end:
        app.processEvents()


def laid_out_children(widget: QWidget) -> list[QWidget]:
    layout = widget.layout()
    if layout is None:
        return []
    children = []
    for child in widget.findChildren(QWidget, options=Qt.FindDirectChildrenOnly):
        if child.isVisibleTo(widget) and child.isVisible() and layout.indexOf(child) >= 0:
            children.append(child)
    return children


def describe(widget: QWidget) -> str:
    text = ""
    for attribute in ("text", "title", "placeholderText"):
        getter = getattr(widget, attribute, None)
        if callable(getter):
            try:
                value = getter()
            except TypeError:
                continue
            if isinstance(value, str) and value:
                text = f" {value[:40]!r}"
                break
    name = widget.objectName()
    return f"{type(widget).__name__}{'#' + name if name else ''}{text}"


def problems(root: QWidget) -> list[str]:
    found = []
    for widget in [root, *root.findChildren(QWidget)]:
        if not widget.isVisible() or widget.width() <= 0:
            continue
        children = laid_out_children(widget)
        bounds = widget.rect()
        for child in children:
            geometry = child.geometry()
            # A pixel of slack: rounded borders and focus rims sit on the edge.
            if geometry.right() > bounds.right() + 1 or geometry.left() < bounds.left() - 1:
                found.append(f"spills {geometry.right() - bounds.right()}px: {describe(child)} in {describe(widget)}")
        for index, first in enumerate(children):
            for second in children[index + 1:]:
                overlap = first.geometry().intersected(second.geometry())
                if overlap.width() > 1 and overlap.height() > 1:
                    found.append(f"overlaps {overlap.width()}x{overlap.height()}px: "
                                 f"{describe(first)} / {describe(second)} in {describe(widget)}")
    return found


class FakeSettings:
    def __init__(self, language: str) -> None:
        self.language = language

    def string_value(self, key: str, default: str = "") -> str:
        return self.language if key == "language/current" else default


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="en")
    parser.add_argument("--theme", help="a theme's own face measures differently (NSO's pixel font)")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1040)
    parser.add_argument("--shots")
    args = parser.parse_args()

    app = QApplication([])
    from app.i18n import install_translator
    install_translator(app, FakeSettings(args.lang))
    from ui import theme
    if args.theme:
        theme.install(args.theme)
    from ui import gui
    from tests.osu_fixtures import write_fixture
    theme.apply_palette(app)

    temp = tempfile.TemporaryDirectory()
    (Path(temp.name) / "audio.mp3").write_bytes(b"\x00")
    window = gui.MainWindow()
    window.resize(args.width, args.height)
    window.show()
    window._load_map_path(write_fixture(Path(temp.name), "full_v14"), refresh_difficulties=True)
    shots = Path(args.shots) if args.shots else None
    seen: dict[str, str] = {}

    def check(where: str) -> None:
        settle(app)
        for problem in problems(window):
            # Report each problem once, under the first place it showed up.
            seen.setdefault(problem, where)
            if shots is not None and seen[problem] == where:
                window.grab().save(str(shots / f"overlap_{len(seen):02d}.png"))

    for page, name in ((gui.PAGE_LIBRARY, "Songs"), (gui.PAGE_EDITOR, "Editor"), (gui.PAGE_GIMMICK, "Gimmick")):
        window._show_page(page)
        check(name)
    window._show_page(gui.PAGE_FANCY)
    for mode_index in range(window.mode_combo.count()):
        window.mode_combo.setCurrentIndex(mode_index)
        for group, refs in window._transform_page_refs.items():
            combo = refs["combo"]
            for index in range(combo.count()):
                combo.setCurrentIndex(index)
                check(f"Fancy Arranger / {window.mode_combo.itemText(mode_index)} / {group} / {combo.currentData()}")

    for problem, where in seen.items():
        print(f"[{where}] {problem}")
    print(f"{len(seen)} problem(s), language {args.lang}, theme {theme.active()}, {args.width}x{args.height}")
    window.close()
    temp.cleanup()
    return 1 if seen else 0


if __name__ == "__main__":
    sys.exit(main())
