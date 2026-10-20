"""Import Image as Drawing: trace a picture into Drawing strokes.

Laid out like a macOS inspector -- the picture on the left, the four settings
on the right -- and every setting carries a small live example drawn from the
tracer's own rules, so what it does is visible before the picture changes:

- Trace Mode: three tiles, each a miniature of the kind of artwork it suits.
- Threshold: a tone bar split where the slider is, the traced side lit.
- Ignore Specks: specks of growing size, the ones the length drops faded.
- Smoothing: a pixel circle run through `refine_loops` itself, points counted.

Settings apply as they move. Only mode, threshold and invert redo the slow
outline pass (`trace_loops`, ~2s on a 1024px screenshot), behind a short
debounce; specks and smoothing refine the kept outlines, which is instant.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF,
)
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QVBoxLayout,
    QWidget,
)

from chart.image_to_drawing import (
    ALLOWED_SUFFIXES, TraceOptions, load_image, refine_loops, to_playfield, trace_loops,
)
from ui import theme
from ui.config_sheet import SEGMENT_STYLE, segmented
from ui.motion import SEGMENT_GLIDE_STYLE, SegmentGlide
from ui.smooth_scroll import smooth

FOCUS = "#ff66aa"
WARN = "#ffb347"

TRACE_STYLE = """
QDialog#traceDialog { background: #191f29; }
QScrollArea#traceInspector, QWidget#traceInspectorBody { background: transparent; border: 0; }
QLabel { background: transparent; }
QLabel#groupTitle { color: #aeb8c5; font-size: 12.5px; font-weight: 600; }
QFrame#traceCard { background: #222a36; border: 0; border-radius: 12px; }
QLabel#rowLabel { font-size: 13px; font-weight: 600; }
QLabel#rowNote { color: #aeb8c5; font-size: 12px; }
QLabel#fileName { color: #e8edf3; font-size: 13px; font-weight: 600; }
QLabel#traceStatus { color: #7d8794; font-size: 12px; }
QLabel#traceStatus[warn="true"] { color: #ffb347; }
QFrame#traceFoot { background: #191f29; border: 0; border-top: 1px solid #222a36; }
QCheckBox { background: transparent; spacing: 0; }
QSlider { background: transparent; min-height: 20px; }
QSlider::groove:horizontal { height: 4px; border-radius: 2px; background: #3a4554; }
QSlider::sub-page:horizontal { border-radius: 2px; background: #ff66aa; }
QSlider::handle:horizontal { width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; background: #e8edf3; }
"""

# The specks drawn by IgnoreSpecksExample, as square sides in image pixels.
SPECK_SIDES = (1, 2, 3, 4, 6, 9, 13)
SPECK_SLIDER_MAX = 200
SMOOTHING_SLIDER_STEPS = 2  # slider units per pixel: half-pixel steps, as the old spin box


def speck_outline_points(side: int) -> int:
    """The outline length `trace_loops` gives an s x s square: 4s edges and
    the start point repeated to close it -- the number Ignore Specks compares."""
    return 4 * side + 1


def _sample_circle_loop():
    """A pixel circle's outline, traced by the real tracer, for SmoothingExample."""
    size, radius = 20, 8.6
    image = QImage(size, size, QImage.Format_RGBA8888)
    image.fill(QColor(255, 255, 255))
    for y in range(size):
        for x in range(size):
            if (x + 0.5 - size / 2) ** 2 + (y + 0.5 - size / 2) ** 2 <= radius * radius:
                image.setPixelColor(x, y, QColor(0, 0, 0))
    return trace_loops(image, TraceOptions())[0], size


def _font(widget: QWidget, size: float, weight=QFont.Weight.DemiBold) -> QFont:
    font = QFont(widget.font())
    font.setPixelSize(round(size))
    font.setWeight(weight)
    return font


def paint_mode_example(painter: QPainter, rect: QRectF, mode: str) -> None:
    """A ring of artwork the way each mode expects to find it, outlined in
    pink the way the trace will come out."""
    painter.save()
    clip = QPainterPath()
    clip.addRoundedRect(rect, 6, 6)
    painter.setClipPath(clip)
    if mode == "alpha":
        cell = 6
        painter.fillRect(rect, QColor("#ffffff"))
        y = rect.top()
        while y < rect.bottom():
            x = rect.left() + (cell if int((y - rect.top()) / cell) % 2 else 0)
            while x < rect.right():
                painter.fillRect(QRectF(x, y, cell, cell), QColor("#d6d9de"))
                x += cell * 2
            y += cell
        art = QColor("#5aa7c7")
    else:
        painter.fillRect(rect, QColor("#ffffff" if mode == "dark" else "#0b0d10"))
        art = QColor("#1b1d22" if mode == "dark" else "#ffffff")
    outer = min(rect.width(), rect.height()) * 0.74
    centre = rect.center()
    ring = QPainterPath()
    ring.setFillRule(Qt.OddEvenFill)
    ring.addEllipse(centre, outer / 2, outer / 2)
    ring.addEllipse(centre, outer / 4.5, outer / 4.5)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.fillPath(ring, art)
    painter.strokePath(ring, QPen(theme.color(FOCUS), 1.6))
    painter.restore()


class ModeTile(QAbstractButton):
    """One choice of Trace Mode: its example above, its name below."""

    def __init__(self, mode: str, text: str, parent=None) -> None:
        super().__init__(parent)
        self.mode = mode
        self.setText(text)
        self.setAccessibleName(text)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(96)

    def sizeHint(self) -> QSize:
        return QSize(84, 96)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        checked = self.isChecked()
        if checked:
            rim = QPen(theme.color(FOCUS), 2)
        elif self.underMouse() or self.hasFocus():
            rim = QPen(theme.color("#4a5668"), 1)
        else:
            rim = QPen(theme.color("#2f3947"), 1)
        painter.setPen(rim)
        painter.setBrush(theme.color("#1b212b"))
        painter.drawRoundedRect(rect, 10, 10)
        paint_mode_example(painter, QRectF(rect.left() + 8, rect.top() + 8, rect.width() - 16, rect.height() - 38), self.mode)
        painter.setFont(_font(self, 12, QFont.Weight.Bold if checked else QFont.Weight.DemiBold))
        painter.setPen(theme.color("#e8edf3" if checked else "#aeb8c5"))
        painter.drawText(QRectF(rect.left() + 4, rect.bottom() - 28, rect.width() - 8, 24),
                         Qt.AlignCenter | Qt.TextWordWrap, self.text())


class ThresholdExample(QWidget):
    """The tone bar: which tones count as artwork at this threshold."""

    def __init__(self, traced: str, ignored: str, parent=None) -> None:
        super().__init__(parent)
        self.labels = (traced, ignored)
        self.mode, self.threshold, self.invert = "dark", 128, False
        self.setFixedHeight(44)

    def set_state(self, mode: str, threshold: int, invert: bool) -> None:
        self.mode, self.threshold, self.invert = mode, threshold, invert
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        bar = QRectF(1, 2, self.width() - 2, 16)
        gradient = QLinearGradient(bar.topLeft(), bar.topRight())
        if self.mode == "alpha":
            # Transparent to opaque: the same artwork, fading in over a checker.
            painter.fillRect(bar, QColor("#d6d9de"))
            gradient.setColorAt(0, QColor(90, 167, 199, 0))
            gradient.setColorAt(1, QColor(90, 167, 199, 255))
        else:
            gradient.setColorAt(0, QColor("#000000"))
            gradient.setColorAt(1, QColor("#ffffff"))
        path = QPainterPath()
        path.addRoundedRect(bar, 4, 4)
        painter.fillPath(path, QBrush(gradient))
        painter.setPen(QPen(theme.color("#3a4554"), 1))
        painter.drawPath(path)

        split = bar.left() + bar.width() * self.threshold / 255
        # _mask: dark traces at or below the threshold, light and alpha at or above.
        traced_low = (self.mode == "dark") != self.invert
        low = QRectF(bar.left(), bar.bottom() + 4, split - bar.left(), 3)
        high = QRectF(split, bar.bottom() + 4, bar.right() - split, 3)
        traced, ignored = (low, high) if traced_low else (high, low)
        painter.fillRect(traced, theme.color(FOCUS))
        painter.fillRect(ignored, theme.color("#3a4554"))

        painter.setPen(QPen(theme.color("#e8edf3"), 2))
        painter.drawLine(QPointF(split, bar.top() - 1), QPointF(split, bar.bottom() + 7))

        painter.setFont(_font(self, 11))
        for rect, text, colour in ((traced, self.labels[0], FOCUS), (ignored, self.labels[1], "#7d8794")):
            if rect.width() >= painter.fontMetrics().horizontalAdvance(text) + 6:
                painter.setPen(theme.color(colour))
                painter.drawText(QRectF(rect.left(), rect.bottom() + 2, rect.width(), 14), Qt.AlignCenter, text)


class IgnoreSpecksExample(QWidget):
    """Specks of growing size; the ones shorter than the minimum go faint."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.minimum = 12
        self.setFixedHeight(44)

    def set_minimum(self, minimum: int) -> None:
        self.minimum = minimum
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        # Offset so a one-pixel speck is still big enough to see dropped.
        smallest, largest = 7.0, self.height() - 8.0
        step = (largest - smallest) / (max(SPECK_SIDES) - 1)
        slot = self.width() / len(SPECK_SIDES)
        floor = max(4, self.minimum)
        for index, side in enumerate(SPECK_SIDES):
            size = smallest + (side - 1) * step
            rect = QRectF(slot * index + (slot - size) / 2, self.height() - 4 - size, size, size)
            if speck_outline_points(side) >= floor:
                painter.setPen(QPen(theme.color(FOCUS), 1.4))
                painter.setBrush(theme.color("#e8edf3"))
            else:
                painter.setPen(QPen(theme.color("#4a5668"), 1, Qt.DashLine))
                painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)


class SmoothingExample(QWidget):
    """A traced pixel circle at this smoothing, its points dotted and counted."""

    def __init__(self, count_text: str, parent=None) -> None:
        super().__init__(parent)
        self.count_text = count_text
        self.loop, self.size = _sample_circle_loop()
        self.distance = 2.0
        self.setFixedHeight(70)

    def set_distance(self, distance: float) -> None:
        self.distance = distance
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        scale = (self.height() - 6) / self.size
        origin = QPointF(3, 3)

        def poly(points):
            return QPolygonF([QPointF(origin.x() + x * scale, origin.y() + y * scale) for x, y in points])

        smoothed = refine_loops([self.loop], TraceOptions(minimum_component=4, simplify_distance=self.distance))
        smoothed = smoothed[0] if smoothed else []
        painter.setPen(QPen(theme.color("#3a4554"), 1))
        painter.drawPolyline(poly(self.loop))
        painter.setPen(QPen(theme.color(FOCUS), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolyline(poly(smoothed))
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.color("#e8edf3"))
        for point in poly(smoothed):
            painter.drawEllipse(point, 2, 2)

        text_left = origin.x() + self.size * scale + 14
        painter.setPen(theme.color("#e8edf3"))
        painter.setFont(_font(self, 15, QFont.Weight.Bold))
        painter.drawText(QRectF(text_left, 10, self.width() - text_left, 24), Qt.AlignLeft | Qt.AlignVCenter,
                         f"{len(self.loop)} → {len(smoothed)}")
        painter.setPen(theme.color("#7d8794"))
        painter.setFont(_font(self, 11))
        painter.drawText(QRectF(text_left, 34, self.width() - text_left, 18), Qt.AlignLeft | Qt.AlignVCenter, self.count_text)


class TraceCanvas(QWidget):
    """The picture, its trace, or the trace over the picture. Empty, it is a
    drop target that also opens the file picker when clicked."""

    clicked = Signal()

    def __init__(self, empty_title: str, empty_note: str, parent=None) -> None:
        super().__init__(parent)
        self.empty_title, self.empty_note = empty_title, empty_note
        self.image: QImage | None = None
        self.strokes = []
        self.view = "both"
        self.drag_over = False
        self.setMinimumSize(420, 320)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.PointingHandCursor)

    def set_content(self, image: QImage | None, strokes) -> None:
        self.image, self.strokes = image, strokes
        self.setCursor(Qt.ArrowCursor if image is not None else Qt.PointingHandCursor)
        self.update()

    def set_view(self, view: str) -> None:
        self.view = view
        self.update()

    def set_drag_over(self, over: bool) -> None:
        self.drag_over = over
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self.image is None and event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        card = QPainterPath()
        card.addRoundedRect(QRectF(self.rect()), 12, 12)
        painter.fillPath(card, theme.screen("#11151c"))
        if self.image is None:
            self._paint_empty(painter)
            return
        area = QRectF(self.rect()).adjusted(20, 20, -20, -20)
        scale = min(area.width() / self.image.width(), area.height() / self.image.height())
        target = QRectF(0, 0, self.image.width() * scale, self.image.height() * scale)
        target.moveCenter(area.center())
        if self.view != "outline":
            painter.setOpacity(1.0 if self.view == "original" else 0.3)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawImage(target, self.image)
            painter.setOpacity(1.0)
        if self.view != "original":
            painter.setPen(QPen(theme.screen(FOCUS), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            for stroke in self.strokes:
                painter.drawPolyline(QPolygonF([QPointF(target.x() + x * scale, target.y() + y * scale) for x, y in stroke]))
        if self.drag_over:
            self._paint_drop_rim(painter)

    def _paint_drop_rim(self, painter: QPainter) -> None:
        painter.setPen(QPen(theme.screen(FOCUS), 2, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(14, 14, -14, -14), 10, 10)

    def _paint_empty(self, painter: QPainter) -> None:
        zone = QRectF(self.rect()).adjusted(14, 14, -14, -14)
        hot = self.drag_over or self.underMouse()
        painter.setPen(QPen(theme.screen(FOCUS if hot else "#3a4554"), 1.6, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(zone, 10, 10)
        # A picture glyph: frame, hill, sun.
        centre = zone.center()
        glyph = QRectF(centre.x() - 26, centre.y() - 56, 52, 40)
        tone = theme.screen("#ff9dcc" if hot else "#7d8794")
        painter.setPen(QPen(tone, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawRoundedRect(glyph, 6, 6)
        hill = QPolygonF([QPointF(glyph.left() + 6, glyph.bottom() - 6), QPointF(glyph.left() + 20, glyph.top() + 18),
                          QPointF(glyph.left() + 30, glyph.top() + 28), QPointF(glyph.left() + 36, glyph.top() + 22),
                          QPointF(glyph.right() - 6, glyph.bottom() - 6)])
        painter.drawPolyline(hill)
        painter.drawEllipse(QPointF(glyph.right() - 13, glyph.top() + 11), 4, 4)
        painter.setPen(theme.screen("#e8edf3"))
        painter.setFont(_font(self, 15, QFont.Weight.Bold))
        painter.drawText(QRectF(zone.left(), centre.y() - 8, zone.width(), 22), Qt.AlignCenter, self.empty_title)
        painter.setPen(theme.screen("#7d8794"))
        painter.setFont(_font(self, 12))
        painter.drawText(QRectF(zone.left(), centre.y() + 16, zone.width(), 18), Qt.AlignCenter, self.empty_note)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)


class ImageTraceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("traceDialog")
        self.setStyleSheet(TRACE_STYLE)
        self.setWindowTitle(self.tr("Import Image as Drawing"))
        self.resize(1080, 700)
        self.setMinimumSize(780, 480)
        self.setAcceptDrops(True)
        self.source_path: Path | None = None
        self.image: QImage | None = None
        self._loops = None
        self.strokes = []
        self.accepted_strokes = []
        # Mode, threshold and invert redo the slow pass; a slider drag
        # restarts this, so only where it comes to rest is traced.
        self._retrace = QTimer(self)
        self._retrace.setSingleShot(True)
        self._retrace.setInterval(220)
        self._retrace.timeout.connect(self._trace)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(20, 18, 12, 14)
        body.setSpacing(16)
        body.addLayout(self._build_preview(), 1)
        body.addWidget(self._build_inspector())
        root.addLayout(body, 1)
        root.addWidget(self._build_footer())
        self._sync_examples()
        self._set_status(self.tr("Choose a picture to begin."))

    # -- layout ---------------------------------------------------------

    def _build_preview(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.view_buttons = {}
        group = QButtonGroup(self)
        for view, text in (("original", self.tr("Original")), ("outline", self.tr("Outline")), ("both", self.tr("Both"))):
            button = QPushButton(text)
            button.setCheckable(True)
            group.addButton(button)
            button.clicked.connect(lambda _checked=False, view=view: self.canvas.set_view(view))
            self.view_buttons[view] = button
        self.view_buttons["both"].setChecked(True)
        views = segmented(self.view_buttons.values())
        views.setStyleSheet(SEGMENT_STYLE + SEGMENT_GLIDE_STYLE)
        self._view_glide = SegmentGlide(views, self.view_buttons.values())
        top.addWidget(views)
        top.addStretch(1)
        self.file_name = QLabel()
        self.file_name.setObjectName("fileName")
        top.addWidget(self.file_name)
        self.choose_button = QPushButton(self.tr("Choose Image..."))
        self.choose_button.clicked.connect(self.choose_image)
        top.addWidget(self.choose_button)
        column.addLayout(top)
        self.canvas = TraceCanvas(self.tr("Drop a picture here"), self.tr("or click to choose  ·  PNG, JPG, WebP, BMP"))
        self.canvas.clicked.connect(self.choose_image)
        column.addWidget(self.canvas, 1)
        self.status = QLabel()
        self.status.setObjectName("traceStatus")
        column.addWidget(self.status)
        return column

    def _card(self, layout: QVBoxLayout, title: str) -> QVBoxLayout:
        heading = QLabel(title)
        heading.setObjectName("groupTitle")
        heading.setContentsMargins(4, 8, 4, 0)
        card = QFrame()
        card.setObjectName("traceCard")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(14, 12, 14, 14)
        inner.setSpacing(8)
        layout.addWidget(heading)
        layout.addWidget(card)
        return inner

    @staticmethod
    def _note(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("rowNote")
        label.setWordWrap(True)
        return label

    def _slider_row(self, inner: QVBoxLayout, label: str, slider: QSlider, spin, scale: int = 1) -> None:
        """A slider with a box to type into. The box is the value: it reaches
        past the slider's end (a speck limit of 500, smoothing of 30px), and
        the slider just pins at its end and follows it back."""
        row = QHBoxLayout()
        caption = QLabel(label)
        caption.setObjectName("rowLabel")
        row.addWidget(caption)
        row.addStretch(1)
        row.addWidget(spin)
        inner.addLayout(row)
        slider.setAccessibleName(label)
        spin.setAccessibleName(label)
        spin.setFixedWidth(96)
        inner.addWidget(slider)
        slider.setValue(round(spin.value() * scale))
        slider.valueChanged.connect(lambda v: spin.setValue(v / scale if scale != 1 else v))

        def follow(value) -> None:
            slider.blockSignals(True)
            slider.setValue(round(value * scale))
            slider.blockSignals(False)

        spin.valueChanged.connect(follow)

    def _build_inspector(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("traceInspector")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(344)
        body = QWidget()
        body.setObjectName("traceInspectorBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(6)

        inner = self._card(layout, self.tr("Trace Mode"))
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.mode_tiles = QButtonGroup(self)
        for mode, text in (("dark", self.tr("Dark Lines")), ("light", self.tr("Light Lines")), ("alpha", self.tr("Alpha Outline"))):
            tile = ModeTile(mode, text)
            self.mode_tiles.addButton(tile)
            tiles.addWidget(tile)
        self.mode_tiles.buttons()[0].setChecked(True)
        self.mode_tiles.buttonToggled.connect(lambda _button, checked: checked and self._outline_settings_changed())
        inner.addLayout(tiles)
        self.mode_note = self._note("")
        inner.addWidget(self.mode_note)

        inner = self._card(layout, self.tr("What Counts as Artwork"))
        self.threshold = QSpinBox()
        self.threshold.setRange(0, 255)
        self.threshold.setValue(128)
        self.threshold_slider = QSlider(Qt.Horizontal)
        self.threshold_slider.setRange(0, 255)
        self._slider_row(inner, self.tr("Threshold"), self.threshold_slider, self.threshold)
        self.threshold_example = ThresholdExample(self.tr("Traced"), self.tr("Ignored"))
        inner.addWidget(self.threshold_example)
        self.threshold_note = self._note("")
        inner.addWidget(self.threshold_note)
        self.threshold.valueChanged.connect(self._outline_settings_changed)
        invert_row = QHBoxLayout()
        invert_text = QVBoxLayout()
        invert_text.setSpacing(2)
        invert_caption = QLabel(self.tr("Invert"))
        invert_caption.setObjectName("rowLabel")
        invert_text.addWidget(invert_caption)
        invert_text.addWidget(self._note(self.tr("Trace the background instead. The bar above swaps sides.")))
        self.invert = QCheckBox()
        self.invert.setAccessibleName(self.tr("Invert"))
        self.invert.toggled.connect(self._outline_settings_changed)
        invert_row.addLayout(invert_text, 1)
        invert_row.addWidget(self.invert, 0, Qt.AlignVCenter)
        inner.addSpacing(4)
        inner.addLayout(invert_row)

        inner = self._card(layout, self.tr("Clean Up"))
        self.minimum = QSpinBox()
        self.minimum.setRange(4, 100000)
        self.minimum.setValue(12)
        self.minimum_slider = QSlider(Qt.Horizontal)
        self.minimum_slider.setRange(4, SPECK_SLIDER_MAX)
        self._slider_row(inner, self.tr("Ignore Specks"), self.minimum_slider, self.minimum)
        self.specks_example = IgnoreSpecksExample()
        inner.addWidget(self.specks_example)
        inner.addWidget(self._note(self.tr(
            "Drops outlines shorter than this many points: dust, scan noise, stray pixels. Faded specks are removed.")))
        self.minimum.valueChanged.connect(self._refine_settings_changed)
        inner.addSpacing(6)
        self.simplify = QDoubleSpinBox()
        self.simplify.setRange(0.0, 50.0)
        self.simplify.setDecimals(1)
        self.simplify.setSingleStep(1 / SMOOTHING_SLIDER_STEPS)
        self.simplify.setSuffix(" px")
        self.simplify.setValue(2.0)
        self.simplify_slider = QSlider(Qt.Horizontal)
        self.simplify_slider.setRange(0, 20 * SMOOTHING_SLIDER_STEPS)
        self._slider_row(inner, self.tr("Smoothing"), self.simplify_slider, self.simplify, SMOOTHING_SLIDER_STEPS)
        self.smoothing_example = SmoothingExample(self.tr("points in this circle"))
        inner.addWidget(self.smoothing_example)
        inner.addWidget(self._note(self.tr(
            "Skips points closer together than this. Higher is smoother, lighter and faster to draw; too high cuts corners.")))
        self.simplify.valueChanged.connect(self._refine_settings_changed)

        layout.addStretch(1)
        scroll.setWidget(body)
        smooth(scroll)
        return scroll

    def _build_footer(self) -> QFrame:
        foot = QFrame()
        foot.setObjectName("traceFoot")
        row = QHBoxLayout(foot)
        row.setContentsMargins(20, 10, 20, 12)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.import_button = self.buttons.button(QDialogButtonBox.Ok)
        self.import_button.setText(self.tr("Import into Drawing"))
        self.import_button.setEnabled(False)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        row.addWidget(self.buttons)
        return foot

    # -- state ----------------------------------------------------------

    def mode(self) -> str:
        return self.mode_tiles.checkedButton().mode

    def options(self) -> TraceOptions:
        return TraceOptions(self.mode(), self.threshold.value(), self.minimum.value(),
                            self.simplify.value(), self.invert.isChecked())

    def _sync_examples(self) -> None:
        mode, threshold, invert = self.mode(), self.threshold.value(), self.invert.isChecked()
        self.mode_note.setText({
            "dark": self.tr("Black or dark artwork on a light background: sketches, scanned line art, most logos."),
            "light": self.tr("White or bright artwork on a dark background: glowing symbols, inverted line art."),
            "alpha": self.tr("Transparent PNGs: traces where the picture stops, whatever its colours. Icons and silhouettes."),
        }[mode])
        if mode == "alpha":
            note = self.tr("Pixels at least this opaque are the shape.")
        elif mode == "dark":
            note = self.tr("Pixels this dark or darker are lines. Raise it to catch faint grey strokes.")
        else:
            note = self.tr("Pixels this bright or brighter are lines. Lower it to catch dimmer strokes.")
        self.threshold_note.setText(note)
        self.threshold_example.set_state(mode, threshold, invert)
        self.specks_example.set_minimum(self.minimum.value())
        self.smoothing_example.set_distance(self.simplify.value())

    def _outline_settings_changed(self, *_args) -> None:
        self._sync_examples()
        if self.image is not None:
            self._loops = None
            self._retrace.start()

    def _refine_settings_changed(self, *_args) -> None:
        self._sync_examples()
        if self._loops is not None:
            self._refine()

    def _set_status(self, text: str, warn: bool = False) -> None:
        self.status.setText(text)
        self.status.setProperty("warn", warn)
        self.status.style().polish(self.status)

    # -- tracing --------------------------------------------------------

    def choose_image(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, self.tr("Choose Image"), "", self.tr("Images (*.png *.jpg *.jpeg *.webp *.bmp)"))
        if filename:
            self.open_image(Path(filename))

    def open_image(self, path: Path) -> None:
        try:
            image = load_image(path)
        except Exception as error:
            self._set_status(self.tr("Could not open this picture: {error}").format(error=error), warn=True)
            return
        self.source_path, self.image, self._loops = path, image, None
        self.file_name.setText(path.name)
        self.choose_button.setText(self.tr("Replace..."))
        self._trace()

    def _trace(self) -> None:
        self._retrace.stop()
        if self.image is None:
            return
        self._set_status(self.tr("Tracing..."))
        # The pass holds the GUI thread for up to ~2s; say so before it does.
        self.status.repaint()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._loops = trace_loops(self.image, self.options())
        finally:
            QApplication.restoreOverrideCursor()
        self._refine()

    def _refine(self) -> None:
        self.strokes = refine_loops(self._loops, self.options())
        self.accepted_strokes = to_playfield(self.strokes) if self.strokes else []
        self.canvas.set_content(self.image, self.strokes)
        self.import_button.setEnabled(bool(self.strokes))
        if self.strokes:
            points = sum(len(stroke) for stroke in self.strokes)
            self._set_status(self.tr("{outlines} outlines · {points} points").format(
                outlines=f"{len(self.strokes):,}", points=f"{points:,}"))
        else:
            self._set_status(self.tr("No outlines found. Try another Trace Mode, or move Threshold."), warn=True)

    # -- drag and drop --------------------------------------------------

    @staticmethod
    def _dropped_image(event) -> Path | None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in ALLOWED_SUFFIXES:
                return path
        return None

    def dragEnterEvent(self, event) -> None:
        if self._dropped_image(event) is not None:
            event.acceptProposedAction()
            self.canvas.set_drag_over(True)

    def dragLeaveEvent(self, event) -> None:
        self.canvas.set_drag_over(False)

    def dropEvent(self, event) -> None:
        self.canvas.set_drag_over(False)
        path = self._dropped_image(event)
        if path is not None:
            event.acceptProposedAction()
            self.open_image(path)

    def accept(self):
        if not self.accepted_strokes:
            return
        super().accept()
