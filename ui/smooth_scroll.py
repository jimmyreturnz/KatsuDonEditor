"""Eased vertical scrolling, for every scrolling widget in the program.

The song list had it first; the owner's rule since is that anything that
scrolls vertically glides the same way. It lives here rather than in gui.py
because the settings dialog and the updater scroll too, and neither can
import gui.
"""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt
from PySide6.QtWidgets import QAbstractItemView, QAbstractScrollArea, QApplication

SMOOTH_SCROLL_MS = 200


def reduced_motion() -> bool:
    """Windows' "Animation effects" switch, as Qt reports it. Off under the
    offscreen platform, which keeps the tests free of running animations."""
    return not QApplication.isEffectEnabled(Qt.UI_General)


class SmoothScroller(QObject):
    """Eases `view`'s vertical scroll to where the wheel or the selection
    asks for, instead of jumping there. Instant under reduced motion.

    An item view scrolls per pixel, so a glide has positions between rows to
    pass through, and its own autoScroll is switched off -- it is what jumps
    to the current item -- with `follow_current` doing that job, eased.

    The wheel is caught on the viewport and on the scroll bar. A child that
    reads the wheel as a value (a spin box, a combo) takes it before the
    viewport ever sees it, so those keep working inside a scroll area.
    """

    WHEEL_ROWS = 3  # Windows' default lines per notch

    def __init__(self, view: QAbstractScrollArea) -> None:
        super().__init__(view)
        self.view = view
        self.items = isinstance(view, QAbstractItemView)
        if self.items:
            view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
            view.setAutoScroll(False)
            view.selectionModel().currentChanged.connect(
                lambda current, _previous: self.follow_current(current))
        self.bar = view.verticalScrollBar()
        self.animation = QPropertyAnimation(self.bar, b"value", self)
        self.animation.setEasingCurve(QEasingCurve.OutCubic)
        self.target = self.bar.value()
        view.viewport().installEventFilter(self)
        self.bar.installEventFilter(self)

    def glide_to(self, value: int) -> None:
        value = max(self.bar.minimum(), min(self.bar.maximum(), int(value)))
        self.target = value
        self.animation.stop()
        if reduced_motion():
            self.bar.setValue(value)
            return
        self.animation.setDuration(SMOOTH_SCROLL_MS)
        self.animation.setStartValue(self.bar.value())
        self.animation.setEndValue(value)
        self.animation.start()

    def _base(self) -> int:
        # From where a running glide is heading, so notches and rows passed
        # mid-glide add up instead of restarting from wherever the bar is.
        return self.target if self.animation.state() == QPropertyAnimation.Running else self.bar.value()

    def follow_current(self, index) -> None:
        if not index.isValid():
            return
        rect = self.view.visualRect(index)
        height = self.view.viewport().height()
        base = self._base()
        top = rect.top() + self.bar.value() - base
        if top < 0:
            self.glide_to(base + top)
        elif top + rect.height() > height:
            self.glide_to(base + top + rect.height() - height)

    def _step(self) -> int:
        if self.items:
            row = self.view.sizeHintForRow(0) if self.view.model().rowCount() else 40
            return self.WHEEL_ROWS * max(20, row)
        return QApplication.wheelScrollLines() * max(20, self.bar.singleStep())

    def eventFilter(self, watched, event) -> bool:
        if event.type() != QEvent.Wheel or event.modifiers() != Qt.NoModifier:
            return False
        notches = event.angleDelta().y() / 120
        if not notches or self.bar.maximum() <= self.bar.minimum():
            return False
        self.glide_to(self._base() - notches * self._step())
        return True


def smooth(*views: QAbstractScrollArea) -> None:
    """Give each of `views` a SmoothScroller (kept alive by its parent)."""
    for view in views:
        SmoothScroller(view)
