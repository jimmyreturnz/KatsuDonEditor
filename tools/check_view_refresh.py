"""Does an open view actually show an edit, or is it left holding the old map?

    python tools/check_view_refresh.py [a real .osu]

With no argument it builds a small fixture; scale is exactly the kind of thing
that changes the answer here, so point it at a real gimmick map too.

Opens a gameplay preview on the gimmick page the way its "+" button does, runs
every edit path through it and compares the widget's **pixels** before and
after. Pixels, because the two cheaper questions each answer only half of it:
the view's data can be current while the widget never repaints, and `update()`
can be called on a widget that is not the one on screen.

Two traps this was caught by, both worth repeating:

- **Not on the offscreen platform.** It delivers no paint events at all -- not
  even for an explicit `repaint()` -- so every view reads as stale and the
  result means nothing. `WA_DontShowOnScreen` plus `grab()` gets real painting
  out of the real platform without a window appearing.
- **Add the view the way the app does.** `_add_editor_view` without a container
  puts the frame on the *Editor* page; on the gimmick page that frame is never
  shown, which reads as "it never repaints" for a completely different reason.
"""
import sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, r"D:/Jimmy/CodingProject/taiko_arranger")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from ui import gui
from tests.osu_fixtures import write_fixture

app = QApplication.instance() or QApplication([])
tmp = tempfile.TemporaryDirectory(); d = Path(tmp.name)
if len(sys.argv) > 1:
    path = Path(sys.argv[1])
else:
    (d / "audio.mp3").write_bytes(b"\x00")
    path = write_fixture(d, "full_v14")
w = gui.MainWindow(); w.resize(1600, 900)
w.setAttribute(Qt.WA_DontShowOnScreen, True); w.show()
w._load_map_path(path, refresh_difficulties=True)

class _E:
    CREATE = gui.GimmickEntryDialog.CREATE
    USE_CURRENT = gui.GimmickEntryDialog.USE_CURRENT
    CANCEL = gui.GimmickEntryDialog.CANCEL
    def __init__(self, *a, **k): pass
    def exec(self): return 1
    def selected_action(self): return self.USE_CURRENT
    def selected_reference(self): return None
    def deleteLater(self): pass

with patch.object(gui, "GimmickEntryDialog", _E):
    w._enter_gimmick_page()
w._show_page(gui.PAGE_GIMMICK)
target = w._gimmick_pairing.target
w._add_editor_view("gameplay", target, container=w.gimmick_views_layout)
app.processEvents()
gv = w._gameplay_views[-1]
document = w._states[target].document
print(f"map: {path.name} -- {len(document.hit_objects)} objects, "
      f"{len(document.timing_points)} timing points")
notes = sorted((n for n in document.hit_objects if n.is_circle), key=lambda n: n.time)
# Park the playhead where the edit will land, or the change is off screen.
for view in (gv,):
    view.current_time = float(notes[0].time)
app.processEvents()

class _Convert(gui.ConvertNotesDialog):
    def exec(self):
        return gui.QDialog.DialogCode.Accepted

class _Sweep(gui.SVFunctionDialog):
    def exec(self):
        return gui.QDialog.DialogCode.Accepted


class _Barlines(gui.BarlineFunctionDialog):
    def exec(self):
        return gui.QDialog.DialogCode.Accepted


def probe(name, run):
    before = gv.grab().toImage()
    pts = len(gv.timing_points)
    run()
    app.processEvents()
    after = gv.grab().toImage()
    print(f"  {'ok   ' if before != after else 'STALE'} {name}"
          f"  (points {pts} -> {len(gv.timing_points)})")


a, b = notes[0].time, notes[min(2, len(notes) - 1)].time
with patch.object(gui, "ConvertNotesDialog", _Convert):
    probe("Convert -> barline notes", lambda: w._convert_notes_to_gimmick(target, "barline", a, b))
    probe("Convert -> fake sliders", lambda: w._convert_notes_to_gimmick(target, "fake_slider", a, b))
with patch.object(gui, "SVFunctionDialog", _Sweep):
    probe("SV Function", lambda: w._open_sv_function_dialog(target, a, b))
with patch.object(gui, "BarlineFunctionDialog", _Barlines):
    probe("Barline Function", lambda: w._generate_barlines(target, a, b))
probe("Kiai range", lambda: w._set_kiai_range(target, a, b))
probe("Undo", lambda: w.undo())
probe("Redo", lambda: w.redo())
w.close(); tmp.cleanup()
