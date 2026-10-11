"""The Settings dialog's building blocks: sidebar tiles, card groups, switch rows and the note opacity preview."""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from ui.config_sheet import scrub_label, ui_asset
from ui import theme
from ui.smooth_scroll import smooth


def _nav_tile(page_id: str, tint: QColor, selected: bool) -> QPixmap:
    """The page's glyph on a rounded tile: tinted on a faint tile, or ink on
    a solid one when the page is showing. Drawn at 2x so it stays sharp on a
    scaled display."""
    ratio, side, glyph_side = 2, 26, 16
    glyph = QIcon(ui_asset(f"settings-{page_id}.svg")).pixmap(QSize(glyph_side * ratio, glyph_side * ratio), 1.0)
    glyph.setDevicePixelRatio(1.0)
    ink = QPixmap(glyph.size())
    ink.fill(Qt.transparent)
    painter = QPainter(ink)
    painter.drawPixmap(0, 0, glyph)
    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
    painter.fillRect(ink.rect(), theme.color("#17191f") if selected else tint)
    painter.end()
    tile = QPixmap(side * ratio, side * ratio)
    tile.fill(Qt.transparent)
    painter = QPainter(tile)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(tint if selected else QColor(255, 255, 255, 16))
    painter.drawRoundedRect(QRectF(0, 0, side * ratio, side * ratio), 7 * ratio, 7 * ratio)
    offset = (side - glyph_side) // 2 * ratio
    painter.drawPixmap(offset, offset, ink)
    painter.end()
    tile.setDevicePixelRatio(ratio)
    return tile


def _theme_swatch(steps, primary: str, focus: str) -> QPixmap:
    """A theme in miniature: its ground, a bar of its fill colour, and a dot
    each of fill and rim. Painted rather than styled, so the theme being
    previewed never recolours the other themes' swatches."""
    # Five abreast in the narrowest page the dialog allows.
    ratio, width, height = 2, 80, 40
    pixmap = QPixmap(width * ratio, height * ratio)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(ratio, ratio)
    painter.setPen(Qt.NoPen)
    # None keeps the chrome as written: the navy steps.
    painter.setBrush(QColor(steps[2] if steps else "#191f29"))
    painter.drawRoundedRect(QRectF(0, 0, width, height), 7, 7)
    painter.setBrush(QColor(primary))
    painter.drawRoundedRect(QRectF(9, 10, width * 0.55, 7), 3.5, 3.5)
    painter.drawEllipse(QRectF(9, 26, 13, 13))
    painter.setBrush(QColor(focus))
    painter.drawEllipse(QRectF(26, 26, 13, 13))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


class _SwitchRow(QFrame):
    """A row whose whole width flips its switch, as a label beside a switch
    does everywhere else -- the switch alone is a 40px target at the far end."""

    def __init__(self, check: QCheckBox) -> None:
        super().__init__()
        self.check = check

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.check.isEnabled():
            self.check.toggle()
            return
        super().mousePressEvent(event)


class _Group:
    """One titled card of rows: label and note on the left, control on the right."""

    def __init__(self, page_layout: QVBoxLayout, title: str) -> None:
        self.heading = heading = QLabel(title)
        heading.setObjectName("groupTitle")
        heading.setContentsMargins(4, 10, 4, 0)
        self.card = QFrame()
        self.card.setObjectName("settingsCard")
        self.rows = QVBoxLayout(self.card)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(0)
        # Before the page's closing stretch.
        page_layout.insertWidget(page_layout.count() - 1, heading)
        page_layout.insertWidget(page_layout.count() - 1, self.card)

    def set_visible(self, visible: bool) -> None:
        self.heading.setVisible(visible)
        self.card.setVisible(visible)

    def _frame(self, frame: QFrame) -> QHBoxLayout:
        frame.setObjectName("settingsRow")
        # A hairline between rows, not above the first.
        frame.setProperty("divider", self.rows.count() > 0)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(16)
        self.rows.addWidget(frame)
        return layout

    @staticmethod
    def _text(label: str, note: str) -> tuple[QVBoxLayout, QLabel]:
        column = QVBoxLayout()
        column.setSpacing(2)
        caption = QLabel(label)
        caption.setObjectName("rowLabel")
        caption.setWordWrap(True)
        column.addWidget(caption)
        if note:
            line = QLabel(note)
            line.setObjectName("rowNote")
            line.setWordWrap(True)
            column.addWidget(line)
        return column, caption

    def row(self, label: str, control: QWidget, note: str = "",
            scrub: QAbstractSpinBox | None = None) -> QFrame:
        frame = QFrame()
        layout = self._frame(frame)
        column, caption = self._text(label, note)
        layout.addLayout(column, 1)
        layout.addWidget(control, 0, Qt.AlignVCenter)
        target = scrub or (control if isinstance(control, QAbstractSpinBox) else None)
        if target is not None:
            # The label drag every other sheet in the app has.
            scrub_label(caption, target)
        return frame

    def switch(self, check: QCheckBox, note: str = "") -> QFrame:
        label = check.text()
        # The label is drawn beside the row instead, so the switch can sit
        # at the end of it; the name stays for screen readers.
        check.setText("")
        check.setAccessibleName(label)
        frame = _SwitchRow(check)
        layout = self._frame(frame)
        column, _caption = self._text(label, note)
        layout.addLayout(column, 1)
        layout.addWidget(check, 0, Qt.AlignVCenter)
        return frame

    def wide(self, widget: QWidget, own_width: bool = False) -> QFrame:
        """`widget` across the whole row, or at its own width on the left."""
        frame = QFrame()
        layout = self._frame(frame)
        layout.addWidget(widget)
        if own_width:
            layout.addStretch(1)
        return frame


class _Page(QScrollArea):
    """A page: its title, then cards of rows. Scrolls itself, so a short
    screen never pushes the buttons at the bottom off the edge."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("settingsPage")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("settingsPageBody")
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(28, 18, 28, 24)
        self.body_layout.setSpacing(8)
        if title:
            heading = QLabel(title)
            heading.setObjectName("pageTitle")
            self.body_layout.addWidget(heading)
        self.body_layout.addStretch(1)
        self.setWidget(body)
        smooth(self)

    def group(self, title: str) -> _Group:
        return _Group(self.body_layout, title)


def _control_width(widget: QWidget, width: int = 280) -> QWidget:
    widget.setFixedWidth(width)
    return widget


class _NoteOpacityPreview(QWidget):
    """Three notes at the chosen opacity, over a snap grid, so the percent
    means something before Apply."""

    def __init__(self, slider: QSlider, skin_combo: QComboBox | None = None) -> None:
        super().__init__()
        self.slider = slider
        self.skin_combo = skin_combo
        # Set by MainWindow.open_settings: (painter, rect, skin_name, percent).
        # The notes are drawn by the editor timeline's own code, which lives in
        # gui, and gui imports this module -- so it is handed in rather than
        # imported. None (a dialog built alone) keeps the plain circles below.
        self.paint_notes = None
        self.setFixedHeight(64)
        slider.valueChanged.connect(self.update)
        if skin_combo is not None:
            skin_combo.currentIndexChanged.connect(self.update)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), theme.color("#151b24"))
        painter.setPen(QPen(theme.color("#e8edf3"), 1))
        for x in range(12, self.width(), 18):
            painter.setOpacity(0.35 if (x // 18) % 4 else 0.8)
            painter.drawLine(x, 6, x, self.height() - 6)
        painter.setOpacity(1.0)
        if self.paint_notes is not None:
            name = str(self.skin_combo.currentData() or "") if self.skin_combo is not None else ""
            self.paint_notes(painter, self.rect(), name, self.slider.value())
            return
        painter.setOpacity(self.slider.value() / 100.0)
        y = self.height() / 2
        for index, (colour, radius) in enumerate(((QColor(229, 76, 46), 17), (QColor(67, 141, 171), 17),
                                                  (QColor(229, 76, 46), 25), (QColor(251, 183, 6), 17))):
            painter.setPen(QPen(QColor("#ffffff"), 2.5))
            painter.setBrush(colour)
            painter.drawEllipse(40 + index * 62, y - radius, radius * 2, radius * 2)
