"""Does the main window fit the screen, per page, and what holds it open?

    python tools/measure_window_fit.py [<a real .osu>]

"The window overextends the screen border" is a minimum-size problem: a
maximized window whose layout cannot shrink to the available geometry is
drawn past it. This prints, per page, the screen's available size, the
window's frame, and the layout's minimum -- then walks down from the page
following the child with the largest minimum, which names the widget that
sets it. Real platform only: the offscreen plugin measures tofu glyphs.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QWidget

from ui import gui


def chain(widget: QWidget, axis: str, depth: int = 0) -> None:
    size = lambda w: getattr(w.minimumSizeHint().expandedTo(w.minimumSize()), axis)()
    print(f"    {'  ' * depth}{type(widget).__name__} {widget.objectName()!r}: {size(widget)}")
    children = [c for c in widget.findChildren(QWidget, options=gui.Qt.FindDirectChildrenOnly)
                if c.isVisibleTo(widget) and not c.isWindow()]
    if children and depth < 25:
        chain(max(children, key=size), axis, depth + 1)


app = QApplication.instance() or QApplication([])
window = gui.MainWindow()
window.showMaximized()
app.processEvents()
if len(sys.argv) > 1:
    window._load_map_path(Path(sys.argv[1]))
    app.processEvents()
screen = window.screen().availableGeometry()
print(f"screen available {screen.width()}x{screen.height()}, dpr {window.devicePixelRatio()}")
for index, name in ((gui.PAGE_LIBRARY, "library"), (gui.PAGE_EDITOR, "editor"),
                    (gui.PAGE_GIMMICK, "gimmick"), (gui.PAGE_FANCY, "fancy"), (gui.PAGE_MISC, "misc")):
    window._show_page(index)
    for _ in range(3):
        app.processEvents()
    frame, minimum = window.frameGeometry(), window.minimumSizeHint()
    over = max(0, minimum.width() - screen.width()), max(0, minimum.height() - screen.height())
    # Windows' quarter snap on a 1080p screen: the frame, title bar included.
    chrome = frame.size() - window.size()
    snap = minimum.width() + chrome.width() <= 960 and minimum.height() + chrome.height() <= 540
    print(f"{name}: frame {frame.width()}x{frame.height()}, minimum {minimum.width()}x{minimum.height()}, "
          f"over by {over[0]}x{over[1]}, fits 960x540: {'yes' if snap else 'no'}")
    page = window.page_stack.currentWidget()
    for axis in ("width", "height"):
        print(f"  {axis} chain:")
        chain(window, axis)
