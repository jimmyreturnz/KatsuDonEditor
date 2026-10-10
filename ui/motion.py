"""The approved mockup's motion (2026-10-07): a light running round whatever
is selected, a sheen across the Gimmick and Fancy Arranger tabs on hover, and
a rectangle that flies between the song list and the difficulty list on
Left/Right, and a view carried by its header to a new place in the stack.

Every piece is off under `reduced_motion()` -- Windows' "Animation effects",
the switch the eased scrolling already obeys -- and costs nothing while the
thing it animates is not on screen.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QColor, QConicalGradient, QCursor, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractScrollArea, QWidget

from ui import theme
from ui.smooth_scroll import reduced_motion

# The pink theme's focus colour; theme.color() moves it to the active theme's.
FOCUS = "#ff66aa"
# The pink theme's filled-button colour. A sheen lights a *button*, so it is
# the button's own hue: in pink both are pink, but Monokai's tabs fill green
# and its rims are #f92672, and a pink glow on a green tab read as wrong.
PRIMARY = "#f3a6bd"

# One lap of the two lights round a rim, and one breath of its glow. Slow on
# purpose: the owner's verdict on the first, faster version was "more subtle".
RIM_LAP_MS = 5000
RIM_BREATH_MS = 3200
# 30 frames a second is plenty for a light this slow, and half the wakeups.
RIM_FRAME_MS = 33


class RimClock(QObject):
    """One timer for every living rim in the window, running only while at
    least one is on screen. A subscriber is a callable that repaints its own
    rim -- a rect, never a whole view."""

    _instance: "RimClock | None" = None

    def __init__(self) -> None:
        super().__init__()
        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._timer = QTimer(self)
        self._timer.setInterval(RIM_FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self._subscribers: list = []

    @classmethod
    def shared(cls) -> "RimClock":
        if cls._instance is None:
            cls._instance = RimClock()
        return cls._instance

    def phase(self, period_ms: int) -> float:
        """0..1 through the current `period_ms`; 0 under reduced motion, so
        a still rim is always drawn in the same place."""
        if reduced_motion():
            return 0.0
        return (self._elapsed.elapsed() % period_ms) / period_ms

    def subscribe(self, callback) -> None:
        if callback not in self._subscribers:
            self._subscribers.append(callback)
        if not reduced_motion() and not self._timer.isActive():
            self._timer.start()

    def unsubscribe(self, callback) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)
        if not self._subscribers:
            self._timer.stop()

    def _tick(self) -> None:
        for callback in list(self._subscribers):
            try:
                callback()
            except RuntimeError:
                # Its widget was deleted without unsubscribing.
                self._subscribers.remove(callback)
        if not self._subscribers:
            self._timer.stop()


class _WhileShown(QObject):
    def __init__(self, widget: QWidget, callback) -> None:
        super().__init__(widget)
        self.callback = callback
        widget.installEventFilter(self)
        if widget.isVisible():
            RimClock.shared().subscribe(callback)

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Show:
            RimClock.shared().subscribe(self.callback)
        elif kind in (QEvent.Hide, QEvent.Destroy):
            RimClock.shared().unsubscribe(self.callback)
        return False


def tick_while_shown(widget: QWidget, callback) -> None:
    """Call `callback` every rim frame while `widget` is on screen."""
    _WhileShown(widget, callback)


def paint_living_rim(painter: QPainter, rect: QRectF, radius: float, width: float, active: bool) -> None:
    """A rim in the focus colour with two small lights running round it and a
    faint glow just inside that breathes. `active` False is the same rim,
    dimmer and still: the selection in a list that does not have the keys."""
    focus = theme.color(FOCUS)
    clock = RimClock.shared()
    inner = rect.adjusted(width / 2, width / 2, -width / 2, -width / 2)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(Qt.NoBrush)
    if not active:
        dim = QColor(focus)
        dim.setAlphaF(0.45)
        painter.setPen(QPen(dim, width))
        painter.drawRoundedRect(inner, radius, radius)
        painter.restore()
        return

    # The glow first, under the rim: a wider, faint stroke just inside it.
    breath = 0.5 - 0.5 * math.cos(2 * math.pi * clock.phase(RIM_BREATH_MS))
    glow = QColor(focus)
    glow.setAlphaF(0.06 + 0.12 * breath)
    painter.setPen(QPen(glow, width + 3))
    painter.drawRoundedRect(inner.adjusted(1.5, 1.5, -1.5, -1.5), radius, radius)

    base = QColor(focus)
    base.setAlphaF(0.55)
    bright = QColor(focus)
    bright.setAlphaF(0.95)
    white = QColor(255, 255, 255, 178)
    gradient = QConicalGradient(rect.center(), -360.0 * clock.phase(RIM_LAP_MS))
    # Two lights half a lap apart, each a narrow peak on the steady rim.
    for start in (0.0, 0.5):
        gradient.setColorAt(start + 0.00, base)
        gradient.setColorAt(start + 0.37, base)
        gradient.setColorAt(start + 0.42, bright)
        gradient.setColorAt(start + 0.44, white)
        gradient.setColorAt(start + 0.46, bright)
        gradient.setColorAt(min(1.0, start + 0.49), base)
    painter.setPen(QPen(gradient, width))
    painter.drawRoundedRect(inner, radius, radius)
    painter.restore()


class HoverSheen(QObject):
    """A band of light that sweeps once across a button as the pointer
    arrives, and a soft glow that holds while it stays. Drawn by a child
    widget laid over the button, so the button keeps its own paint."""

    SWEEP_MS = 700
    GLOW_MS = 350

    def __init__(self, button: QWidget) -> None:
        super().__init__(button)
        self.button = button
        self.overlay = _SheenOverlay(button, self)
        self.sweep = 1.0
        self.glow = 0.0
        self._sweep = QVariantAnimation(self)
        self._sweep.setDuration(self.SWEEP_MS)
        self._sweep.setStartValue(0.0)
        self._sweep.setEndValue(1.0)
        self._sweep.setEasingCurve(QEasingCurve.OutCubic)
        self._sweep.valueChanged.connect(self._set_sweep)
        self._glow = QVariantAnimation(self)
        self._glow.setDuration(self.GLOW_MS)
        self._glow.setEasingCurve(QEasingCurve.OutCubic)
        self._glow.valueChanged.connect(self._set_glow)
        button.installEventFilter(self)

    def _set_sweep(self, value) -> None:
        self.sweep = float(value)
        self.overlay.update()

    def _set_glow(self, value) -> None:
        self.glow = float(value)
        self.overlay.update()

    def _fade_glow(self, to: float) -> None:
        self._glow.stop()
        self._glow.setStartValue(self.glow)
        self._glow.setEndValue(to)
        self._glow.start()

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Resize:
            self.overlay.setGeometry(self.button.rect())
        elif kind == QEvent.Enter and self.button.isEnabled():
            if reduced_motion():
                self.glow = 1.0
                self.overlay.update()
            else:
                self._sweep.stop()
                self._sweep.start()
                self._fade_glow(1.0)
        elif kind == QEvent.Leave:
            if reduced_motion():
                self.glow = 0.0
                self.overlay.update()
            else:
                self._fade_glow(0.0)
        return False


class _SheenOverlay(QWidget):
    def __init__(self, button: QWidget, sheen: HoverSheen) -> None:
        super().__init__(button)
        self.sheen = sheen
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setGeometry(button.rect())
        self.show()

    def paintEvent(self, event) -> None:
        sheen = self.sheen
        if sheen.glow <= 0.0 and sheen.sweep >= 1.0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(rect, 5, 5)
        painter.setClipPath(clip)
        focus = theme.color(PRIMARY)
        if sheen.glow > 0.0:
            # The glow rises from the button's foot, as in the mockup.
            rise = QLinearGradient(rect.bottomLeft(), rect.topLeft())
            low = QColor(focus)
            low.setAlphaF(0.32 * sheen.glow)
            rise.setColorAt(0.0, low)
            rise.setColorAt(0.7, QColor(0, 0, 0, 0))
            painter.fillRect(rect, rise)
            rim = QColor(focus)
            rim.setAlphaF(0.7 * sheen.glow)
            painter.setPen(QPen(rim, 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        if sheen.sweep < 1.0:
            # A skewed band of light crossing left to right.
            width = rect.width() * 0.45
            x = -width + (rect.width() + width * 2) * sheen.sweep
            band = QLinearGradient(QPointF(x, 0), QPointF(x + width, 0))
            edge = QColor(focus)
            edge.setAlphaF(0.0)
            mid = QColor(focus)
            mid.setAlphaF(0.45)
            band.setColorAt(0.0, edge)
            band.setColorAt(0.35, mid)
            band.setColorAt(0.5, QColor(255, 255, 255, 110))
            band.setColorAt(0.65, mid)
            band.setColorAt(1.0, edge)
            painter.translate(x + width / 2, rect.center().y())
            painter.shear(-0.4, 0)
            painter.translate(-(x + width / 2), -rect.center().y())
            painter.fillRect(QRectF(x, rect.top() - 4, width, rect.height() + 8), band)


class MorphRect(QWidget):
    """A rim that flies from one rect to another over its parent: the
    song list's selection to the difficulty list's, and back, so Left and
    Right show where the keys went. Cancelled by the next one."""

    DURATION_MS = 240

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.hide()
        self._from = QRect()
        self._to = QRect()
        self._progress = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(self.DURATION_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.valueChanged.connect(self._step)
        self._animation.finished.connect(self.hide)

    def fly(self, start: QRect, end: QRect) -> None:
        """From `start` to `end`, both in the parent's coordinates."""
        if reduced_motion() or start.isNull() or end.isNull():
            return
        self._animation.stop()
        self._from, self._to = QRect(start), QRect(end)
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self._animation.start()

    def _step(self, value) -> None:
        self._progress = float(value)
        self.update()

    def current_rect(self) -> QRectF:
        t = self._progress
        a, b = QRectF(self._from), QRectF(self._to)
        rect = QRectF(
            a.left() + (b.left() - a.left()) * t,
            a.top() + (b.top() - a.top()) * t,
            a.width() + (b.width() - a.width()) * t,
            a.height() + (b.height() - a.height()) * t,
        )
        # Pinched in the middle of the flight, so it reads as moving rather
        # than as a box being resized.
        pinch = math.sin(math.pi * t)
        return rect.adjusted(rect.width() * 0.06 * pinch, rect.height() * 0.1 * pinch,
                             -rect.width() * 0.06 * pinch, -rect.height() * 0.1 * pinch)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.current_rect()
        focus = theme.color(FOCUS)
        fill = QColor(focus)
        fill.setAlphaF(0.10)
        halo = QColor(focus)
        halo.setAlphaF(0.25)
        radius = 4 + 6 * math.sin(math.pi * self._progress)
        painter.setPen(QPen(halo, 5))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)
        painter.setPen(QPen(focus, 2))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)


class ReorderDrag(QWidget):
    """A view lifted by its header and carried to a new place in its stack.

    Drawn, not laid out: each item is snapshotted once and the snapshots move
    on an overlay, so nothing relays out while the pointer moves and a lane
    mid-playback is not repainted into a moving widget every frame. The real
    layout changes once, on drop (`on_drop(new index)`).

    Every snapshot *follows* its slot rather than animating to it: each frame
    it closes a fixed share of the distance left (FOLLOW_MS), so a pointer
    that changes its mind mid-glide retargets the glide without a restart --
    the owner's "sooooo smooth". The lifted one sits a little larger with a
    shadow under it. Esc puts everything back.

    `items` are siblings stacked top to bottom with no gap between them, which
    is what both pages' edge-to-edge views are.
    """

    FOLLOW_MS = 45.0
    LIFT_SCALE = 1.02
    FRAME_MS = 8
    # Within this far of the scroll area's top or bottom, the stack scrolls,
    # faster the nearer the edge.
    EDGE_PX = 48

    def __init__(self, container: QWidget, items: list, dragged: QWidget, global_pos: QPoint, on_drop) -> None:
        super().__init__(container)
        self.items = list(items)
        self.index = self.items.index(dragged)
        self.on_drop = on_drop
        self.rects = [QRect(item.mapTo(container, QPoint(0, 0)), item.size()) for item in self.items]
        self.pixmaps = [item.grab() for item in self.items]
        self.top = min(rect.top() for rect in self.rects)
        self.bounds = QRect(self.rects[0])
        for rect in self.rects[1:]:
            self.bounds = self.bounds.united(rect)
        self.order = list(range(len(self.items)))
        self.ys = [float(rect.top()) for rect in self.rects]
        self.grab_offset = container.mapFromGlobal(global_pos).y() - self.rects[self.index].top()
        self.drag_y = float(self.rects[self.index].top())
        self.lift = 0.0
        self.dropping = False
        parent = container.parentWidget()
        while parent is not None and not isinstance(parent, QAbstractScrollArea):
            parent = parent.parentWidget()
        self.scroll_area = parent
        self.setGeometry(container.rect())
        self.setCursor(Qt.ClosedHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.show()
        self.raise_()
        self.grabMouse()
        self.grabKeyboard()
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # -- where everything wants to be -------------------------------------------

    def slots(self) -> dict[int, float]:
        y, slots = float(self.top), {}
        for index in self.order:
            slots[index] = y
            y += self.rects[index].height()
        return slots

    def follow(self, global_y: int) -> None:
        """Carry the lifted view to the pointer at `global_y`, and work out
        which slot that puts it in."""
        y = self.parentWidget().mapFromGlobal(QPoint(0, global_y)).y() - self.grab_offset
        height = self.rects[self.index].height()
        # Held inside the stack, give or take half a view, so it cannot be
        # carried off into the empty page below the last one.
        self.drag_y = max(self.bounds.top() - height / 2, min(self.bounds.bottom() - height / 2, y))
        centre = self.drag_y + height / 2
        others = [index for index in self.order if index != self.index]
        y, place = float(self.top), len(others)
        for position, index in enumerate(others):
            if centre < y + self.rects[index].height() / 2:
                place = position
                break
            y += self.rects[index].height()
        others.insert(place, self.index)
        self.order = others

    def new_index(self) -> int:
        return self.order.index(self.index)

    # -- input --------------------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:
        if not self.dropping:
            self.follow(round(event.globalPosition().y()))

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.drop()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.order = list(range(len(self.items)))
            self.drop()

    def drop(self) -> None:
        if self.dropping:
            return
        self.dropping = True
        self.releaseMouse()
        self.releaseKeyboard()

    # -- frames -------------------------------------------------------------------

    def _autoscroll(self) -> None:
        area = self.scroll_area
        if area is None:
            return
        viewport = area.viewport()
        y = viewport.mapFromGlobal(QCursor.pos()).y()
        if y < self.EDGE_PX:
            step = -(self.EDGE_PX - y)
        elif y > viewport.height() - self.EDGE_PX:
            step = y - (viewport.height() - self.EDGE_PX)
        else:
            return
        bar = area.verticalScrollBar()
        before = bar.value()
        bar.setValue(before + round(step * 0.4))
        if bar.value() != before:
            self.follow(QCursor.pos().y())

    def _tick(self) -> None:
        elapsed = self._clock.restart()
        share = 1.0 if reduced_motion() else 1.0 - math.exp(-elapsed / self.FOLLOW_MS)
        if not self.dropping:
            self._autoscroll()
        slots = self.slots()
        settled = True
        for index in range(len(self.items)):
            if index == self.index and not self.dropping:
                self.ys[index] = self.drag_y
                continue
            gap = slots[index] - self.ys[index]
            self.ys[index] += gap * share
            if abs(gap) > 0.5:
                settled = False
        lift_to = 0.0 if self.dropping else 1.0
        self.lift += (lift_to - self.lift) * share
        if abs(lift_to - self.lift) > 0.02:
            settled = False
        self.update()
        if self.dropping and settled:
            self._finish()

    def _finish(self) -> None:
        self._timer.stop()
        self.hide()
        try:
            self.on_drop(self.new_index())
        finally:
            self.deleteLater()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.bounds, theme.color("#191f29"))
        for index in self.order:
            if index != self.index:
                painter.drawPixmap(QPointF(self.rects[index].left(), self.ys[index]), self.pixmaps[index])
        rect = QRectF(self.rects[self.index])
        rect.moveTop(self.ys[self.index])
        # The shadow under the lifted view: a few soft steps of black.
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, round(22 * self.lift)))
        for step in range(1, 7):
            painter.drawRect(rect.adjusted(-step, -step + 3, step, step + 3))
        scale = 1.0 + (self.LIFT_SCALE - 1.0) * self.lift
        painter.setRenderHint(QPainter.SmoothPixmapTransform, self.lift > 0.01)
        painter.translate(rect.center())
        painter.scale(scale, scale)
        painter.translate(-rect.center())
        painter.drawPixmap(rect.topLeft(), self.pixmaps[self.index])
        rim = QColor(theme.color(FOCUS))
        rim.setAlphaF(0.8 * self.lift)
        painter.setPen(QPen(rim, 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect.adjusted(1, 1, -1, -1))


# ---------------------------------------------------------------------------
# The second mockup (2026-10-09): rows that rise in, a selection that glides,
# a ripple under the pointer, a tab pill that slides, a page that slides.

def out_cubic(t: float) -> float:
    """QEasingCurve.OutCubic, for progress computed rather than animated."""
    t = min(1.0, max(0.0, t))
    return 1.0 - (1.0 - t) ** 3


class RowEntrance(QObject):
    """Rows rising `RISE_PX` into place one after another: a filtered song
    list, a new song's difficulties. The delegate asks `progress(row)` and
    draws that row lifted and faded by it.

    Only rows on screen at the start are staggered -- everything below them
    is off screen and is drawn settled -- so a 4000-song list costs what a
    screenful does, and the run ends when the last visible row has landed.
    """

    RISE_PX = 8.0
    FRAME_MS = 16

    def __init__(self, view: QAbstractScrollArea, row_ms: int = 200, stagger_ms: int = 22,
                 delay_ms: int = 0) -> None:
        super().__init__(view)
        self.view = view
        self.row_ms, self.stagger_ms, self.delay_ms = row_ms, stagger_ms, delay_ms
        self._elapsed = QElapsedTimer()
        self._first = 0
        self._total_ms = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def start(self) -> None:
        if reduced_motion() or not self.view.isVisible():
            return
        viewport = self.view.viewport()
        top = self.view.indexAt(QPoint(4, 1))
        bottom = self.view.indexAt(QPoint(4, viewport.height() - 2))
        self._first = top.row() if top.isValid() else 0
        last = bottom.row() if bottom.isValid() else self._first + viewport.height() // 28
        self._total_ms = self.delay_ms + self.row_ms + self.stagger_ms * max(0, last - self._first)
        self._elapsed.start()
        self._timer.start()
        viewport.update()

    def progress(self, row: int) -> float:
        """0 (not yet risen) to 1 (in place) for `row` right now."""
        if not self._timer.isActive() or row < self._first:
            return 1.0
        local = self._elapsed.elapsed() - self.delay_ms - self.stagger_ms * (row - self._first)
        return out_cubic(local / self.row_ms)

    def _tick(self) -> None:
        if self._elapsed.elapsed() >= self._total_ms:
            self._timer.stop()
        self.view.viewport().update()


class SelectionGlide(QWidget):
    """The selected row's tint and rim sliding from the row it was on to the
    one it is on now, instead of jumping. Drawn on an overlay in the
    viewport; while it flies, the delegate leaves the destination row's
    selection to it (`covers`).

    Only between two rows that are both on screen: Next/Previous lands on a
    song a thousand rows away, and a rim flying in from off the list says
    nothing. `paint_selected(painter, rect)` is the delegate's own drawing,
    so the flying rim is the resting one.
    """

    DURATION_MS = 240

    def __init__(self, view, paint_selected, inset=(0, 0, 0, 0)) -> None:
        super().__init__(view.viewport())
        self.view = view
        self.paint_selected = paint_selected
        self.inset = inset
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.hide()
        self._from = QRectF()
        self._row = -1
        self._progress = 1.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(self.DURATION_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.valueChanged.connect(self._step)
        self._animation.finished.connect(self._land)
        view.selectionModel().currentChanged.connect(self._moved)

    def covers(self, row: int) -> bool:
        return self._animation.state() == QVariantAnimation.Running and row == self._row

    def _moved(self, current, previous) -> None:
        if reduced_motion() or not self.view.isVisible() or not current.isValid() or not previous.isValid():
            self._animation.stop()
            self._land()
            return
        area = QRectF(self.view.viewport().rect())
        start = QRectF(self.view.visualRect(previous))
        end = QRectF(self.view.visualRect(current))
        if not (area.intersects(start) and area.intersects(end)):
            self._animation.stop()
            self._land()
            return
        if self._animation.state() == QVariantAnimation.Running:
            start = self._current_rect()  # retarget mid-flight from where it is
        self._from = start
        self._row = current.row()
        self.setGeometry(self.view.viewport().rect())
        self.raise_()
        self.show()
        self._animation.stop()
        self._animation.start()

    def _step(self, value) -> None:
        self._progress = float(value)
        self.update()

    def _land(self) -> None:
        self._progress = 1.0
        self.hide()
        self.view.viewport().update()

    def _current_rect(self) -> QRectF:
        # The end is read live: the list scrolls under a flight to keep the
        # new row in view, and the rim has to land where the row is now.
        end = QRectF(self.view.visualRect(self.view.model().index(self._row, 0)))
        t = self._progress
        a, b = self._from, end
        return QRectF(a.left() + (b.left() - a.left()) * t, a.top() + (b.top() - a.top()) * t,
                      a.width() + (b.width() - a.width()) * t, a.height() + (b.height() - a.height()) * t)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        left, top, right, bottom = self.inset
        self.paint_selected(painter, self._current_rect().adjusted(left, top, -right, -bottom))


class PressRipple(QObject):
    """A circle of light spreading from where a button was pressed. Its
    overlay is made on the first press, so the hundreds of buttons that are
    never pressed in a session cost an event filter and nothing else."""

    DURATION_MS = 500

    def __init__(self, button: QWidget) -> None:
        super().__init__(button)
        self.button = button
        self.overlay: _RippleOverlay | None = None
        self.origin = QPointF()
        self.progress = 1.0
        self._animation: QVariantAnimation | None = None
        button.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if (event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton
                and self.button.isEnabled() and not reduced_motion()):
            self._start(event.position())
        return False

    def _start(self, origin: QPointF) -> None:
        if self.overlay is None:
            self.overlay = _RippleOverlay(self.button, self)
            self._animation = QVariantAnimation(self)
            self._animation.setDuration(self.DURATION_MS)
            self._animation.setStartValue(0.0)
            self._animation.setEndValue(1.0)
            self._animation.setEasingCurve(QEasingCurve.OutCubic)
            self._animation.valueChanged.connect(self._step)
            self._animation.finished.connect(self.overlay.hide)
        self.origin = QPointF(origin)
        self.overlay.setGeometry(self.button.rect())
        self.overlay.raise_()
        self.overlay.show()
        self._animation.stop()
        self._animation.start()

    def _step(self, value) -> None:
        self.progress = float(value)
        self.overlay.update()


class _RippleOverlay(QWidget):
    def __init__(self, button: QWidget, ripple: PressRipple) -> None:
        super().__init__(button)
        self.ripple = ripple
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)

    def paintEvent(self, event) -> None:
        ripple = self.ripple
        if ripple.progress >= 1.0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(rect, 5, 5)
        painter.setClipPath(clip)
        # Far enough to reach the farthest corner from where it started.
        reach = max(math.hypot(ripple.origin.x() - x, ripple.origin.y() - y)
                    for x in (rect.left(), rect.right()) for y in (rect.top(), rect.bottom()))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, round(70 * (1.0 - ripple.progress))))
        radius = reach * ripple.progress
        painter.drawEllipse(ripple.origin, radius, radius)


class SegmentGlide(QWidget):
    """The checked pill of a segmented row, sliding to the newly checked
    button instead of jumping. Painted under the buttons, whose own checked
    fill the row's sheet makes transparent (`SEGMENT_GLIDE_STYLE`)."""

    DURATION_MS = 260

    def __init__(self, frame: QWidget, buttons) -> None:
        super().__init__(frame)
        self.buttons = list(buttons)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._from = QRectF()
        self._to = QRectF()
        self._progress = 1.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(self.DURATION_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.valueChanged.connect(self._step)
        for button in self.buttons:
            button.toggled.connect(lambda checked, button=button: checked and self._glide_to(button))
        frame.installEventFilter(self)
        self.lower()

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Resize, QEvent.Show, QEvent.LayoutRequest):
            self.setGeometry(watched.rect())
            self.lower()
            # Wherever the layout put the checked button now, with no flight.
            checked = next((button for button in self.buttons if button.isChecked()), None)
            if checked is not None and self._animation.state() != QVariantAnimation.Running:
                self._from = self._to = QRectF(checked.geometry())
                self._progress = 1.0
            self.update()
        return False

    def _glide_to(self, button: QWidget) -> None:
        target = QRectF(button.geometry())
        if reduced_motion() or self._to.isNull() or not self.isVisible():
            self._animation.stop()
            self._from = self._to = target
            self._progress = 1.0
            self.update()
            return
        self._from = self.current_rect()
        self._to = target
        self._animation.stop()
        self._animation.start()

    def _step(self, value) -> None:
        self._progress = float(value)
        self.update()

    def current_rect(self) -> QRectF:
        a, b, t = self._from, self._to, self._progress
        return QRectF(a.left() + (b.left() - a.left()) * t, a.top() + (b.top() - a.top()) * t,
                      a.width() + (b.width() - a.width()) * t, a.height() + (b.height() - a.height()) * t)

    def paintEvent(self, event) -> None:
        rect = self.current_rect()
        if rect.isNull():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 4, 4)
        painter.setClipPath(clip)
        # SEGMENT_STYLE's checked button: the focus fill with a white foot.
        painter.fillRect(rect, theme.color(FOCUS))
        painter.fillRect(QRectF(rect.left(), rect.bottom() - 2, rect.width(), 2), QColor("#ffffff"))


# The checked button's own fill and foot handed to SegmentGlide. The border
# stays 2px so the label does not move by a pixel when it is checked.
SEGMENT_GLIDE_STYLE = (
    "QFrame#segment QPushButton:checked { background: transparent; border-bottom: 2px solid transparent; }"
)


class PageSlide(QWidget):
    """The page being left slides back and fades while the new one arrives
    from the side its tab is on. Two snapshots on an overlay, taken once:
    moving the live page would repaint every view on it each frame, and the
    editor page is the most expensive thing the app draws."""

    LEAVE_MS = 160
    ENTER_DELAY_MS = 60
    ENTER_MS = 260
    LEAVE_PX = 18
    ENTER_PX = 28

    def __init__(self, stack: QWidget, ground: str) -> None:
        super().__init__(stack)
        self.ground = ground
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.hide()
        self._old = None
        self._new = None
        self._direction = 1
        self._elapsed = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)

    def play(self, old, new, direction: int) -> None:
        """`old` and `new` are pixmaps of the stack's area; `direction` is +1
        moving to a tab further right, -1 further left."""
        self._old, self._new, self._direction = old, new, direction
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()
        self._elapsed.start()
        self._timer.start()

    def _tick(self) -> None:
        if self._elapsed.elapsed() >= self.ENTER_DELAY_MS + self.ENTER_MS:
            self._timer.stop()
            self.hide()
            self._old = self._new = None
            return
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.color(self.ground))
        now = self._elapsed.elapsed()
        leave = min(1.0, now / self.LEAVE_MS)
        enter = out_cubic((now - self.ENTER_DELAY_MS) / self.ENTER_MS)
        if self._old is not None and leave < 1.0:
            painter.setOpacity(1.0 - leave)
            # Ease-in on the way out: it is leaving, so it accelerates.
            painter.drawPixmap(QPointF(-self._direction * self.LEAVE_PX * leave * leave, 0), self._old)
        if self._new is not None and enter > 0.0:
            painter.setOpacity(enter)
            painter.drawPixmap(QPointF(self._direction * self.ENTER_PX * (1.0 - enter), 0), self._new)


class PlayPauseGlyph(QWidget):
    """Play's triangle splitting into pause's two bars, and back. Each shape
    is two quads -- the triangle as its upper and lower halves -- so every
    corner has a partner to move to."""

    DURATION_MS = 220
    # In a 24-unit box: the triangle's two halves, and the two bars.
    PLAY = (((7, 4), (13, 8), (13, 16), (7, 20)), ((13, 8), (19, 12), (19, 12), (13, 16)))
    PAUSE = (((6, 4), (10, 4), (10, 20), (6, 20)), ((14, 4), (18, 4), (18, 20), (14, 20)))

    def __init__(self, button: QWidget, ink: str, hover_ink: str) -> None:
        super().__init__(button)
        self.button = button
        self.ink, self.hover_ink = ink, hover_ink
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.progress = 0.0  # 0 play, 1 pause
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(self.DURATION_MS)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.valueChanged.connect(self._step)
        button.installEventFilter(self)
        self.setGeometry(button.rect())
        self.show()

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Resize:
            self.setGeometry(self.button.rect())
        elif kind in (QEvent.Enter, QEvent.Leave):
            self.update()
        return False

    def set_playing(self, playing: bool) -> None:
        target = 1.0 if playing else 0.0
        if reduced_motion() or not self.isVisible():
            self._animation.stop()
            self.progress = target
            self.update()
            return
        self._animation.stop()
        self._animation.setStartValue(self.progress)
        self._animation.setEndValue(target)
        self._animation.start()

    def _step(self, value) -> None:
        self.progress = float(value)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        # As tall as the neighbouring buttons' typed glyphs: the shapes span
        # 16 of the 24 units, sized to the font's cap height rather than to
        # half the button, which drew it smaller than "◀◀" beside it.
        side = self.button.fontMetrics().capHeight() * 1.25 * 24 / 16
        painter.translate((self.width() - side) / 2, (self.height() - side) / 2)
        painter.scale(side / 24.0, side / 24.0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.color(self.hover_ink if self.button.underMouse() else self.ink))
        t = self.progress
        for start, end in zip(self.PLAY, self.PAUSE):
            painter.drawPolygon(QPolygonF([QPointF(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                                           for a, b in zip(start, end)]))


class ScanShimmer(QWidget):
    """Placeholder rows shimmering in the focus colour over a song list that
    is still empty because the first scan has not found anything yet.
    A loading state, so it is the one animation here that loops."""

    PERIOD_MS = 1400
    ROWS = 8

    def __init__(self, view: QAbstractScrollArea) -> None:
        super().__init__(view.viewport())
        self.view = view
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.hide()
        tick_while_shown(self, self.update)

    def showEvent(self, event) -> None:
        self.setGeometry(self.view.viewport().rect())
        super().showEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        base = theme.color("#2a3341")
        light = QColor(theme.color(FOCUS))
        light.setAlphaF(0.22)
        sweep = RimClock.shared().phase(self.PERIOD_MS)
        width = self.width() - 26
        for row in range(self.ROWS):
            top = 10 + row * 46
            # Two bars a row, title over subtitle, at lengths that read as text.
            for offset, height, share in ((0, 10, 0.55 + 0.3 * ((row * 37) % 10) / 10), (18, 8, 0.35)):
                rect = QRectF(13, top + offset, width * share, height)
                gradient = QLinearGradient(QPointF(-width + 2 * width * sweep, 0), QPointF(2 * width * sweep, 0))
                gradient.setColorAt(0.3, base)
                gradient.setColorAt(0.5, light if not reduced_motion() else base)
                gradient.setColorAt(0.7, base)
                painter.setPen(Qt.NoPen)
                painter.setBrush(gradient)
                painter.drawRoundedRect(rect, height / 2, height / 2)
