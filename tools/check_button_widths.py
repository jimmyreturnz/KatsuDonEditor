"""Every button whose own label does not fit inside it.

    python tools/check_button_widths.py [--dialogs] [--gimmick <a real .osu>]

"The pink buttons eat their own text" has been diagnosed by eye several times
and fixed by nudging numbers. This asks the question the way Qt answers it:
for each QPushButton, what does its label cost in the font the button is
*actually painted in*, plus the padding and border its own style charges, and
is the button that wide?

Both halves have to be read after `ensurePolished()`. Before that a button
reports the application font and no stylesheet padding at all, which is the
trap every previous round of this fell into -- the width was pinned from a
measurement taken before the style that decides it had been applied.

--dialogs also opens the dialogs, which is where most of the pink buttons are;
without it only the main window's own chrome is walked.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Real platform unless one is forced: the offscreen plugin ships no fonts,
# so every glyph measures as an identical tofu box and this reports nothing.
if "--offscreen" in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication, QPushButton

from ui import gui
from ui import common


def needed(button: QPushButton) -> int:
    """Width the label needs in the widest state the button can be painted in.

    A checkable button has two: `QPushButton:checked` is both heavier (700
    against the base 600) and bordered (1px against 0), so a row's *selected*
    button needs more room than the same button unchecked -- which is exactly
    the one that was clipping.
    """
    button.ensurePolished()
    font = QFont(button.font())
    if button.isCheckable():
        font.setWeight(QFont.Weight.Bold)
    metrics = QFontMetrics(font, button)
    return metrics.horizontalAdvance(button.text()) + common.button_chrome_width(button)


def report(name: str, root) -> int:
    bad = 0
    for button in root.findChildren(QPushButton):
        if not button.text():
            continue
        want = needed(button)
        have = button.width() or button.sizeHint().width()
        if have < want:
            bad += 1
            print(f"  {want - have:3d}px short  {name}: {button.text()!r} "
                  f"(has {have}, needs {want})")
    return bad


app = QApplication.instance() or QApplication([])
window = gui.MainWindow()
window.resize(1920, 1080)
window.show()
app.processEvents()

short = report("MainWindow", window)

if "--gimmick" in sys.argv:
    # Where most of the app's checkable pink buttons live: six tool rows, one
    # per layer, and the selected button in each is the widest state there is.
    from unittest.mock import patch

    class _Entry:
        CREATE = gui.GimmickEntryDialog.CREATE
        USE_CURRENT = gui.GimmickEntryDialog.USE_CURRENT
        CANCEL = gui.GimmickEntryDialog.CANCEL

        def __init__(self, *args, **kwargs): pass
        def exec(self): return 1
        def selected_action(self): return self.USE_CURRENT
        def selected_reference(self): return None
        def deleteLater(self): pass

    window._load_map_path(Path(sys.argv[sys.argv.index("--gimmick") + 1]),
                          refresh_difficulties=True)
    app.processEvents()
    with patch.object(gui, "GimmickEntryDialog", _Entry):
        window._enter_gimmick_page()
    window._show_page(gui.PAGE_GIMMICK)
    app.processEvents()
    short += report("gimmick page", window)

if "--dialogs" in sys.argv:
    config = gui.GimmickConfig()
    dialogs = [
        ("ConvertNotesDialog", gui.ConvertNotesDialog(config, window)),
        ("GimmickConfigDialog", gui.GimmickConfigDialog(config, "barline", config, window)),
        ("BarlineFunctionDialog", gui.BarlineFunctionDialog(0.0, 1000.0, window)),
    ]
    for name, dialog in dialogs:
        dialog.show()
        app.processEvents()
        short += report(name, dialog)
        dialog.close()

print(f"{short} button(s) narrower than their own label")
