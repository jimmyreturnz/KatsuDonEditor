"""Measure what one Fancy Arranger canvas paint costs while the notes glide.

    python tools/profile_fancy_glide.py <a real .osu> [glides] [--legacy]

A transform change starts a 260ms glide, and the canvas repaints every frame of
it. This drives `set_state` to start real glides on the full 1920x1080 window
(offscreen defaults to 758x180, which understates paint cost by the ratio of the
areas), steps the animation the way the timer would (`_glide_step` -> update(),
then the event loop paints), and times `TransformCanvas.paintEvent` itself so
the numbers are the canvas and not the docks around it.

--legacy swaps in the paintEvent as it was before the background was cached and
the draws were batched, so a before/after comparison is one tool and one map.
Reports the distribution: a dropped frame is the tail, not the mean.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from time import perf_counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication

from ui import gui


def legacy_paint(self, event) -> None:
    """TransformCanvas.paintEvent as it stood before the cache and batching."""
    self._update_view_geometry()
    painter = QPainter(self)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.fillRect(self.rect(), QColor("#11151c"))
    if not self.background_pixmap.isNull():
        target = self.render_rect
        source_width = self.background_pixmap.width()
        source_height = self.background_pixmap.height()
        if source_width / max(1, source_height) > 16 / 9:
            crop_width = source_height * 16 / 9
            source = QRectF((source_width - crop_width) / 2, 0, crop_width, source_height)
        else:
            crop_height = source_width * 9 / 16
            source = QRectF(0, (source_height - crop_height) / 2, source_width, crop_height)
        painter.save()
        painter.setOpacity(self.background_opacity)
        painter.drawPixmap(target, self.background_pixmap, source)
        painter.restore()
    painter.setPen(QPen(QColor("#465164"), 1))
    painter.drawRect(self.playfield_rect)
    for note in self.notes:
        position = self._drawn_position(note)
        x = self.view_offset_x + position[0] * self.view_scale
        y = self.view_offset_y + position[1] * self.view_scale
        radius = gui.osu_circle_radius(self.circle_size) * self.view_scale
        painter.setBrush(QColor("#4aa3ff") if note.is_kat else QColor("#ff4f5e"))
        painter.setPen(QPen(QColor("#ffd166") if note.original_index in self.selected else QColor("#f4f7fb"), 2))
        painter.drawEllipse(QPointF(x, y), radius, radius)
        if note.is_finisher:
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(x, y), radius * 0.72, radius * 0.72)


path = Path(sys.argv[1])
glides = int(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else 20
legacy = "--legacy" in sys.argv

app = QApplication.instance() or QApplication([])
if legacy:
    gui.TransformCanvas.paintEvent = legacy_paint
real_paint = gui.TransformCanvas.paintEvent
paint_ms: list[float] = []


def timed_paint(self, event) -> None:
    start = perf_counter()
    real_paint(self, event)
    paint_ms.append((perf_counter() - start) * 1000.0)


gui.TransformCanvas.paintEvent = timed_paint

def _fail(*args):
    # A modal "Open failed" would sit on the offscreen platform forever.
    raise SystemExit(f"open failed: {args[1:]}")


gui.QMessageBox.critical = _fail
window = gui.MainWindow()
window.resize(1920, 1080)
window.show()
window._load_map_path(path, refresh_difficulties=True)
window._show_page(gui.PAGE_FANCY)
app.processEvents()
canvas = window.canvas
print(f"notes={len(canvas.notes)} canvas={canvas.width()}x{canvas.height()} "
      f"background={'yes' if not canvas.background_pixmap.isNull() else 'NO'} "
      f"{'legacy' if legacy else 'current'} paintEvent")

base = {note.original_index: (note.x, note.y) for note in canvas.notes}
STEPS = 16  # a 260ms glide at 60fps
for glide in range(glides):
    shift = 40.0 if glide % 2 == 0 else 0.0
    target = {index: (x + shift, y) for index, (x, y) in base.items()}
    canvas.set_state(canvas.notes, target, set())
    for step in range(1, STEPS + 1):
        canvas._glide_step(step / STEPS)
        app.processEvents()
        app.sendPostedEvents(None, 0)
    if glide == 0:
        paint_ms.clear()  # first glide pays the one-off cache build; report steady state

paint_ms.sort()
count = len(paint_ms)
print(f"paints={count} mean={sum(paint_ms) / count:.2f}ms "
      f"p95={paint_ms[int(count * 0.95)]:.2f}ms max={paint_ms[-1]:.2f}ms (budget 8.33ms)")
window.close()
