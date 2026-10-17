"""Config sheets: the one layout every settings dialog in the editor shares.

The approved mockup (claude.ai/artifact/6ooQaFaXZ7MSb99eoKe6GA, "Config
Sheets"). A sheet is three columns: a rail of sections on the left, the
sections themselves as cards on a scrolling column, and an explain panel on
the right that shows a live picture of what the numbers draw and the help for
whichever field the pointer or the focus is on. The long paragraphs that used
to sit under a form, pushing its buttons down, live there instead.

**Views, not models.** The dialogs keep the widgets their `config()` and the
tests already read -- a `QComboBox` for a choice, a `QCheckBox` for "Custom" --
and the pills drawn here are a second face bound to them both ways
(`bind_segments`, `own_or_custom`). Nothing about what a dialog writes can
drift from what it shows, and a test that drives `mode_combo` still drives the
dialog.

It lives outside gui.py because the settings dialog uses it too, and that
module cannot import gui.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


from app.i18n import tr
from app.security_utils import resource_roots
from ui.smooth_scroll import SmoothScroller

WARN_COLOR = "#ffb347"


def ui_asset(name: str) -> str:
    """`assets/ui/<name>` as a stylesheet `url()` path (forward slashes).

    The bundle's copy first, then the source tree's.
    """
    roots = resource_roots()
    for root in roots:
        path = root / "assets" / "ui" / name
        if path.is_file():
            # As written: the art carries pink-theme colours, and theme.css()
            # swaps in the active theme's copy as the sheet is set.
            return path.as_posix()
    return (roots[-1] / "assets" / "ui" / name).as_posix()


def control_stylesheet() -> str:
    """Spin boxes, switches and scroll bars, for the window stylesheet.

    One rule each, app-wide, so a dialog built anywhere gets them without
    asking -- the reason the old pink +/- pair (`pink_spin_buttons`) kept going
    missing from one dialog or another was that each had to call it.

    - **Spin boxes** carry their step arrows *inside* the box, quiet until
      hovered, instead of two pink squares beside it: pink is for the one
      action in a dialog, and 13 fields put 26 of them in the barline Config.
    - **Combo boxes** draw the same chevron as the spin boxes, a pink rim
      while open, and a popup whose rows are tall enough to aim at.
    - **Checkboxes are switches.** Every checkbox in the app is an on/off
      setting; none is a list of things to tick.
    - **Scroll bars** are a thin handle with no track and no arrows, wider
      under the pointer and pink while held. Qt cannot animate the width, so
      the hover state snaps rather than grows.
    - **Sliders** are a thin track, pink up to a round handle. Without a rule
      they were the platform's own, a white groove on every theme.
    """
    up, up_hover, up_off = ui_asset("chevron-up.svg"), ui_asset("chevron-up-hover.svg"), ui_asset("chevron-up-disabled.svg")
    down, down_hover, down_off = ui_asset("chevron-down.svg"), ui_asset("chevron-down-hover.svg"), ui_asset("chevron-down-disabled.svg")
    return f"""
            QAbstractSpinBox {{
                background: #252d39; border: 1px solid #3a4554; border-radius: 6px;
                padding: 4px 4px 4px 8px; min-height: 20px;
            }}
            QAbstractSpinBox:hover {{ border-color: #4a5668; }}
            QAbstractSpinBox:focus {{ border-color: #ff66aa; }}
            QAbstractSpinBox:disabled {{ color: #4d5664; background: #1f262f; border-color: #303947; }}
            QAbstractSpinBox[warn="true"] {{ border-color: {WARN_COLOR}; }}
            QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
                subcontrol-origin: border; width: 22px; background: transparent;
                border: 0; border-left: 1px solid #303947;
            }}
            QAbstractSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 5px; }}
            QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 5px; }}
            QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: #2a3341; }}
            QAbstractSpinBox::up-arrow {{ image: url({up}); width: 9px; height: 9px; }}
            QAbstractSpinBox::up-arrow:hover {{ image: url({up_hover}); }}
            QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {{ image: url({up_off}); }}
            QAbstractSpinBox::down-arrow {{ image: url({down}); width: 9px; height: 9px; }}
            QAbstractSpinBox::down-arrow:hover {{ image: url({down_hover}); }}
            QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{ image: url({down_off}); }}
            QComboBox {{
                background: #252d39; border: 1px solid #3a4554; border-radius: 8px;
                padding: 5px 28px 5px 10px; min-height: 20px;
            }}
            QComboBox:hover {{ background: #2a3341; border-color: #4a5668; }}
            QComboBox:focus, QComboBox:on {{ border-color: #ff66aa; }}
            QComboBox:disabled {{ color: #4d5664; background: #1f262f; border-color: #303947; }}
            QComboBox::drop-down {{
                subcontrol-origin: padding; subcontrol-position: center right;
                width: 24px; border: 0; background: transparent;
            }}
            QComboBox::down-arrow {{ image: url({down}); width: 10px; height: 10px; }}
            QComboBox::down-arrow:hover, QComboBox::down-arrow:on {{ image: url({down_hover}); }}
            QComboBox::down-arrow:disabled {{ image: url({down_off}); }}
            QComboBox QAbstractItemView {{
                background: #1d2431; border: 1px solid #3a4554; padding: 4px; outline: 0;
                selection-background-color: #2a3341; selection-color: #e8edf3;
            }}
            QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 0 8px; border-radius: 5px; }}
            QComboBox QAbstractItemView::item:hover {{ background: #2a3341; }}
            QComboBox QAbstractItemView::item:selected {{ background: #2a2230; color: #ffffff; }}
            QCheckBox {{ spacing: 10px; }}
            QCheckBox::indicator {{ width: 30px; height: 18px; image: url({ui_asset("switch-off.svg")}); }}
            QCheckBox::indicator:hover {{ image: url({ui_asset("switch-off-hover.svg")}); }}
            QCheckBox::indicator:checked {{ image: url({ui_asset("switch-on.svg")}); }}
            QCheckBox::indicator:checked:hover {{ image: url({ui_asset("switch-on-hover.svg")}); }}
            QCheckBox::indicator:disabled {{ image: url({ui_asset("switch-off-disabled.svg")}); }}
            QCheckBox::indicator:checked:disabled {{ image: url({ui_asset("switch-on-disabled.svg")}); }}
            QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
            QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: #3a4554; border-radius: 2px; min-height: 36px; margin: 2px 4px; }}
            QScrollBar::handle:horizontal {{ background: #3a4554; border-radius: 2px; min-width: 36px; margin: 4px 2px; }}
            QScrollBar::handle:vertical:hover {{ background: #56637a; border-radius: 3px; margin: 2px 3px; }}
            QScrollBar::handle:horizontal:hover {{ background: #56637a; border-radius: 3px; margin: 3px 2px; }}
            QScrollBar::handle:pressed {{ background: #ff66aa; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: 0; background: none; }}
            QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
            QAbstractScrollArea::corner {{ background: transparent; }}
            /* The window's QWidget rule painted each slider's own box in the
               ground colour, a pale bar inside the darker AR/CS steppers. */
            QSlider {{ background: transparent; }}
            QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: #3a4554; }}
            QSlider::sub-page:horizontal {{ border-radius: 2px; background: #ff66aa; }}
            QSlider::sub-page:horizontal:disabled {{ background: #3a4554; }}
            QSlider::handle:horizontal {{
                width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
                background: #f3a6bd; border: 1px solid #ff9dcc;
            }}
    """


# Joined pills: a group of choices reads as one control, and only the pressed
# ones are pink -- with solid pink pills in a row, the pressed one differed by a
# shade. The bottom border is the non-colour cue for "pressed".
SEGMENT_STYLE = """
QFrame#segment { background: #252d39; border: 1px solid #3a4554; border-radius: 6px; }
QFrame#segment QPushButton {
    background: transparent; color: #aeb8c5; border: 0; border-radius: 4px;
    border-bottom: 2px solid transparent; padding: 3px 9px; font-size: 12px; font-weight: 600;
}
QFrame#segment QPushButton:hover { background: #2f3947; color: #e8edf3; }
QFrame#segment QPushButton:checked {
    background: #ff66aa; color: #ffffff; font-weight: 700; border-bottom: 2px solid #ffffff;
}
QFrame#segment QPushButton:disabled { color: #4d5664; background: transparent; }
"""


def segmented(buttons) -> QFrame:
    """`buttons` in one joined pill group (SEGMENT_STYLE)."""
    frame = QFrame()
    frame.setObjectName("segment")
    frame.setStyleSheet(SEGMENT_STYLE)
    # Its own height, centred in the row, rather than the row's full height
    # with the pills floating in the middle of it.
    frame.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(2, 2, 2, 2)
    layout.setSpacing(2)
    for button in buttons:
        button.setStyleSheet("")
        button.setFocusPolicy(Qt.NoFocus)
        layout.addWidget(button)
        if button.isCheckable():
            # A pressed pill is drawn bold, and its size hint was measured at
            # the regular weight -- so the pressed one clipped its own label
            # by a pixel (tools/check_button_widths.py). 9px padding each side.
            bold = QFont(button.font())
            bold.setWeight(QFont.Weight.Bold)
            button.setMinimumWidth(QFontMetrics(bold).horizontalAdvance(button.text()) + 20)
    return frame


def bind_segments(combo: QComboBox, labels: list[str] | None = None) -> QFrame:
    """Pills over `combo`'s items, with `combo` kept as the model.

    The combo is parked hidden inside the pill frame: it stays the widget
    `config()` reads and the tests set, and either side follows the other.
    `labels` overrides the pill text per item (shorter than a combo needs).
    """
    buttons = []
    group = QButtonGroup(combo)
    group.setExclusive(True)
    for index in range(combo.count()):
        text = labels[index] if labels else combo.itemText(index)
        # Doubled: a button reads a lone "&" as a shortcut marker, and
        # "A & B" came out as "A  B".
        button = QPushButton(text.replace("&", "&&"))
        button.setCheckable(True)
        group.addButton(button, index)
        buttons.append(button)
    frame = segmented(buttons)
    combo.setParent(frame)
    combo.hide()
    group.idClicked.connect(combo.setCurrentIndex)

    def follow(index: int) -> None:
        button = group.button(index)
        if button is not None:
            button.setChecked(True)

    combo.currentIndexChanged.connect(follow)
    follow(combo.currentIndex())
    frame.buttons = buttons
    return frame


TILE_STYLE = """
QToolButton#tile {
    background: #222a36; color: #aeb8c5; border: 1px solid #303947; border-radius: 8px;
    padding: 10px; font-size: 12.5px; font-weight: 600; text-align: left;
}
QToolButton#tile:hover { background: #2a3341; color: #e8edf3; }
QToolButton#tile:checked { background: #2a2230; color: #ffffff; border: 1px solid #ff66aa; }
"""


def bind_tiles(combo: QComboBox, tiles: list[tuple[str, QPixmap]]) -> QWidget:
    """A row of picture tiles over `combo`'s items, `combo` kept as the model.

    For a choice between structures, where recognising the picture is faster
    than reading a list: Convert Notes' barline notes / anti-barline / hidden.
    `tiles` is (text, picture) per item, in the combo's order.
    """
    row = QWidget()
    row.setStyleSheet(TILE_STYLE)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    group = QButtonGroup(combo)
    group.setExclusive(True)
    for index, (text, picture) in enumerate(tiles):
        tile = QToolButton()
        tile.setObjectName("tile")
        tile.setCheckable(True)
        tile.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tile.setText(text)
        tile.setIcon(QIcon(picture))
        # Logical size: a picture drawn at 2x for a sharp edge otherwise
        # takes twice its room.
        tile.setIconSize(picture.deviceIndependentSize().toSize())
        tile.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tile.setMinimumWidth(0)
        group.addButton(tile, index)
        layout.addWidget(tile, 1)
    combo.setParent(row)
    combo.hide()
    group.idClicked.connect(combo.setCurrentIndex)

    def follow(index: int) -> None:
        button = group.button(index)
        if button is not None:
            button.setChecked(True)

    combo.currentIndexChanged.connect(follow)
    follow(combo.currentIndex())
    return row


def own_or_custom(check: QCheckBox, spin: QAbstractSpinBox, own_text: str, custom_text: str) -> QWidget:
    """"Chart's own | Custom" pills, then the number -- bound to `check`.

    Replaces "[ ] Custom [180.000]", where the box showed a number that was
    not being used. The spin is disabled while the pill says the chart's own,
    exactly as the checkbox used to do it.
    """
    own, custom = QPushButton(own_text), QPushButton(custom_text)
    group = QButtonGroup(check)
    for index, button in enumerate((own, custom)):
        button.setCheckable(True)
        group.addButton(button, index)
    pills = segmented((own, custom))
    check.setParent(pills)
    check.hide()
    group.idClicked.connect(lambda index: check.setChecked(index == 1))

    def follow(checked: bool) -> None:
        (custom if checked else own).setChecked(True)
        spin.setEnabled(checked)

    check.toggled.connect(follow)
    follow(check.isChecked())
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(pills)
    layout.addWidget(spin, 1)
    return row


class TrimmedDoubleSpinBox(QDoubleSpinBox):
    """Full precision, shown without its trailing zeros.

    SV is typed to 8 decimals because under a 60000 BPM line the rate that
    moves a note a visible distance differs in the seventh; shown at 8 it is
    "1.00000000x" at rest. This keeps every typed digit and drops the padding,
    never below `min_decimals`.
    """

    def __init__(self, min_decimals: int = 2, parent=None) -> None:
        super().__init__(parent)
        self.min_decimals = min_decimals

    def textFromValue(self, value: float) -> str:
        text = f"{value:.{self.decimals()}f}"
        if "." in text:
            whole, fraction = text.split(".")
            fraction = fraction.rstrip("0").ljust(min(self.min_decimals, self.decimals()), "0")
            text = f"{whole}.{fraction}" if fraction else whole
        return self.locale().toString(float(text), "f", len(text.split(".")[1]) if "." in text else 0)


def dot_icon(color: str, size: int = 8) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(0, 0, size, size)
    painter.end()
    return QIcon(pixmap)


def set_field_visible(field: QWidget, visible: bool) -> None:
    """Show or hide a sheet field and close the gap it leaves in its section."""
    if field.isHidden() != visible:
        return
    field.setVisible(visible)
    parent = field.parentWidget()
    while parent is not None and not isinstance(parent, Section):
        parent = parent.parentWidget()
    if parent is not None:
        parent.reflow()


def set_warning(widget: QWidget, on: bool) -> None:
    """Amber rim on a field that clashes with another (QAbstractSpinBox[warn])."""
    if bool(widget.property("warn")) == on:
        return
    widget.setProperty("warn", on)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class _Scrub(QObject):
    """Drag a field's label sideways to change its number: one step per 4px."""

    STEP_PX = 4

    def __init__(self, label: QLabel, spin: QAbstractSpinBox) -> None:
        super().__init__(label)
        self.spin = spin
        self.origin: QPoint | None = None
        self.value = 0.0
        label.setCursor(QCursor(Qt.SizeHorCursor))
        label.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton and self.spin.isEnabled():
            self.origin = event.globalPosition().toPoint()
            self.value = self.spin.value()
            self.spin.setFocus()
            return True
        if kind == QEvent.MouseMove and self.origin is not None:
            steps = (event.globalPosition().toPoint().x() - self.origin.x()) // self.STEP_PX
            self.spin.setValue(self.value + steps * self.spin.singleStep())
            return True
        if kind == QEvent.MouseButtonRelease and self.origin is not None:
            self.origin = None
            return True
        return False


def scrub_label(label: QLabel, spin: QAbstractSpinBox) -> None:
    """Let `label` be dragged sideways to change `spin` (kept alive by label)."""
    _Scrub(label, spin)


class _HelpWatch(QObject):
    """Tell the sheet which field the pointer or the focus is on."""

    def __init__(self, sheet: "ConfigSheet", key: str) -> None:
        super().__init__(sheet)
        self.sheet = sheet
        self.key = key

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Enter, QEvent.FocusIn):
            self.sheet.show_help(self.key)
        return False


SHEET_STYLE = """
QFrame#sheetRail { background: transparent; border: 0; border-right: 1px solid #303947; }
QPushButton#railButton {
    background: transparent; color: #aeb8c5; border: 0; border-left: 3px solid transparent;
    border-radius: 0; text-align: left; padding: 7px 10px 7px 10px; font-size: 12.5px; font-weight: 600;
}
QPushButton#railButton:hover { background: #2a3341; color: #e8edf3; }
QPushButton#railButton:checked { background: #2a2230; color: #ffffff; border-left: 3px solid #ff66aa; }
QPushButton#railButton[warn="true"] { color: #ffb347; }
QScrollArea#sheetColumn, QWidget#sheetColumnBody { background: transparent; border: 0; }
QFrame#sheetSection { background: #1b212b; border: 1px solid #303947; border-radius: 8px; }
QFrame#sheetSection[active="true"] { border-color: #6b3a55; }
QLabel#sectionTitle { font-size: 13px; font-weight: 700; background: transparent; }
QLabel#sectionSub, QLabel#fieldNote { color: #7d8794; font-size: 12px; background: transparent; }
QLabel#fieldLabel { color: #aeb8c5; font-size: 12px; font-weight: 600; background: transparent; }
QLabel#caution {
    color: #ffd39a; background: #2a2518; border: 1px solid #6b5230; border-radius: 6px;
    padding: 8px 10px; font-size: 12px;
}
QFrame#explain { background: #161c25; border: 0; border-left: 1px solid #303947; }
QLabel#explainCap { color: #7d8794; font-size: 10.5px; font-weight: 700; letter-spacing: 1px; background: transparent; }
QLabel#helpTitle { font-size: 13px; font-weight: 700; background: transparent; }
QLabel#helpBody { color: #aeb8c5; font-size: 12.5px; background: transparent; }
QLabel#helpMeta { color: #7d8794; font-size: 11.5px; background: transparent; }
QFrame#diagram { background: #151b24; border: 1px solid #303947; border-radius: 6px; }
"""


class Section(QFrame):
    """One card: a title, a subtitle, and a two-column grid of fields."""

    def __init__(self, sheet: "ConfigSheet", key: str, title: str, subtitle: str, color: str | None) -> None:
        super().__init__()
        self.sheet = sheet
        self.key = key
        self.setObjectName("sheetSection")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 14)
        outer.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(8)
        if color:
            dot = QLabel()
            dot.setPixmap(dot_icon(color).pixmap(8, 8))
            dot.setStyleSheet("background: transparent;")
            head.addWidget(dot)
        name = QLabel(title)
        name.setObjectName("sectionTitle")
        head.addWidget(name)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("sectionSub")
            sub.setWordWrap(True)
            head.addWidget(sub, 1)
        else:
            head.addStretch(1)
        outer.addLayout(head)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(12)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        outer.addLayout(self.grid)
        self._row = 0
        self._column = 0
        # (widget, span) in order, or None for a row break: what `reflow`
        # lays out again when a field is shown or hidden.
        self._items: list[tuple[QWidget, int] | None] = []

    def add(self, widget: QWidget, span: int = 1) -> QWidget:
        """Next cell, left to right; `span=2` takes the whole row."""
        self._items.append((widget, span))
        self._place(widget, span)
        return widget

    def _place(self, widget: QWidget, span: int) -> None:
        if span == 2 and self._column:
            self._row, self._column = self._row + 1, 0
        self.grid.addWidget(widget, self._row, self._column, 1, span)
        self._column += span
        if self._column >= 2:
            self._row, self._column = self._row + 1, 0

    def break_row(self) -> None:
        self._items.append(None)
        if self._column:
            self._row, self._column = self._row + 1, 0

    def reflow(self) -> None:
        """Lay the visible fields out again, so a hidden one leaves no hole.

        The grid is positional: hiding a field emptied its cell and left the
        next one stranded in the other column.
        """
        for item in self._items:
            if item is not None:
                self.grid.removeWidget(item[0])
        self._row = self._column = 0
        for item in self._items:
            if item is None:
                if self._column:
                    self._row, self._column = self._row + 1, 0
            elif not item[0].isHidden():
                self._place(*item)

    def field(self, label: str, widget: QWidget, help_text: str = "", span: int = 1,
              default: str = "", scrub: QAbstractSpinBox | None = None) -> QWidget:
        return self.add(self.sheet.field(label, widget, help_text, default=default, scrub=scrub), span)

    def switch(self, check: QCheckBox, note: str = "", help_text: str = "", span: int = 2,
               default: str = "") -> QWidget:
        return self.add(self.sheet.switch(check, note, help_text, default=default), span)

    def caution(self, text: str = "") -> QLabel:
        label = QLabel(text)
        label.setObjectName("caution")
        label.setWordWrap(True)
        label.hide()
        self.add(label, 2)
        return label


class ConfigSheet(QWidget):
    """Rail | sections | explain panel. See the module docstring."""

    # The key of the section the pointer or focus is now in: a diagram that
    # draws a different picture per section (barline vs anti-barline) listens.
    section_changed = Signal(str)

    def __init__(self, rail: bool = True, explain: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(SHEET_STYLE)
        self.sections: dict[str, Section] = {}
        self._rail_buttons: dict[str, QPushButton] = {}
        self._rail_colors: dict[str, str] = {}
        self._help: dict[str, tuple[str, str, str]] = {}
        self._help_default = ("", "")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.rail = None
        if rail:
            self.rail = QFrame()
            self.rail.setObjectName("sheetRail")
            self.rail.setFixedWidth(176)
            self._rail_layout = QVBoxLayout(self.rail)
            self._rail_layout.setContentsMargins(0, 10, 8, 10)
            self._rail_layout.setSpacing(2)
            self._rail_layout.addStretch(1)
            self._rail_group = QButtonGroup(self)
            self._rail_group.setExclusive(True)
            layout.addWidget(self.rail)

        self.column = QScrollArea()
        self.column.setObjectName("sheetColumn")
        self.column.setWidgetResizable(True)
        self.column.setFrameShape(QFrame.NoFrame)
        self.column.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("sheetColumnBody")
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(16, 14, 16, 24)
        self.body_layout.setSpacing(14)
        self.body_layout.addStretch(1)
        self.column.setWidget(body)
        self.scroller = SmoothScroller(self.column)
        self.column.verticalScrollBar().valueChanged.connect(self._follow_scroll)
        layout.addWidget(self.column, 1)

        self.active_section = ""
        self.explain = None
        if explain:
            self.explain = QFrame()
            self.explain.setObjectName("explain")
            self.explain.setFixedWidth(300)
            panel = QVBoxLayout(self.explain)
            panel.setContentsMargins(14, 14, 14, 14)
            panel.setSpacing(12)
            self.explain_caption = QLabel("PREVIEW")
            self.explain_caption.setObjectName("explainCap")
            panel.addWidget(self.explain_caption)
            self.diagram_frame = QFrame()
            self.diagram_frame.setObjectName("diagram")
            self._diagram_layout = QVBoxLayout(self.diagram_frame)
            self._diagram_layout.setContentsMargins(0, 0, 0, 0)
            self.diagram_frame.hide()
            panel.addWidget(self.diagram_frame)
            self.help_title = QLabel()
            self.help_title.setObjectName("helpTitle")
            self.help_title.setWordWrap(True)
            self.help_body = QLabel()
            self.help_body.setObjectName("helpBody")
            self.help_body.setWordWrap(True)
            self.help_meta = QLabel()
            self.help_meta.setObjectName("helpMeta")
            self.help_meta.setWordWrap(True)
            for label in (self.help_title, self.help_body, self.help_meta):
                panel.addWidget(label)
            panel.addStretch(1)
            layout.addWidget(self.explain)

    # -- building ---------------------------------------------------------

    def add_section(self, key: str, title: str, subtitle: str = "", color: str | None = None,
                    in_rail: bool = True) -> Section:
        section = Section(self, key, title, subtitle, color)
        self.sections[key] = section
        self.body_layout.insertWidget(self.body_layout.count() - 1, section)
        if self.rail is not None and in_rail:
            button = QPushButton(title)
            button.setObjectName("railButton")
            button.setCheckable(True)
            button.setFocusPolicy(Qt.NoFocus)
            button.setIcon(dot_icon(color or "#3a4554"))
            button.clicked.connect(lambda _checked=False, k=key: self.jump_to(k))
            self._rail_group.addButton(button)
            self._rail_layout.insertWidget(self._rail_layout.count() - 1, button)
            self._rail_buttons[key] = button
            self._rail_colors[key] = color or "#3a4554"
            if len(self._rail_buttons) == 1:
                button.setChecked(True)
        return section

    def show_only(self, keys) -> None:
        """Hide every section not in `keys` (a picker's pages)."""
        for key, section in self.sections.items():
            section.setVisible(key in keys)

    def add_widget(self, widget: QWidget) -> None:
        """Something that is not a section (a picker, a plot) in the column."""
        self.body_layout.insertWidget(self.body_layout.count() - 1, widget)

    def field(self, label: str, widget: QWidget, help_text: str = "", default: str = "",
              scrub: QAbstractSpinBox | None = None) -> QWidget:
        """`label` over `widget`, with its help registered.

        A spin box (or `scrub`, for one inside a composite widget) gets the
        label-drag, which is what makes the label worth pointing at.
        """
        box = QWidget()
        box.setStyleSheet("background: transparent;")
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(5)
        caption = QLabel(label)
        caption.setObjectName("fieldLabel")
        column.addWidget(caption)
        column.addWidget(widget)
        target = scrub or (widget if isinstance(widget, QAbstractSpinBox) else None)
        for spin in [widget, *widget.findChildren(QAbstractSpinBox)]:
            if isinstance(spin, QAbstractSpinBox):
                # A spin box sizes its minimum to its whole range: 1000000.000
                # BPM is wider than half a card, and the column cannot scroll
                # sideways, so the card clipped instead.
                spin.setMinimumWidth(96)
        if target is not None:
            _Scrub(caption, target)
            caption.setToolTip(tr("MainWindow", "Drag sideways to change"))
        self.register_help(box, label, help_text, default, extra=(widget, target))
        for each in (widget, target):
            if each is not None:
                # How set_row_visible finds the whole field (label included).
                each.setProperty("sheetField", box)
        return box

    def switch(self, check: QCheckBox, note: str = "", help_text: str = "", default: str = "") -> QWidget:
        """A switch with a one-line description under its label."""
        box = QWidget()
        box.setStyleSheet("background: transparent;")
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        column.addWidget(check)
        if note:
            line = QLabel(note)
            line.setObjectName("fieldNote")
            line.setWordWrap(True)
            # Under the label, not under the switch.
            line.setContentsMargins(40, 0, 0, 0)
            column.addWidget(line)
        self.register_help(box, check.text(), help_text or note, default, extra=(check,))
        check.setProperty("sheetField", box)
        return box

    def register_help(self, box: QWidget, title: str, text: str, default: str = "", extra=()) -> None:
        if not text:
            return
        key = f"{id(box)}"
        self._help[key] = (title, text, default)
        watch = _HelpWatch(self, key)
        for widget in (box, *[w for w in extra if w is not None]):
            widget.installEventFilter(watch)

    def set_help_default(self, title: str, text: str) -> None:
        self._help_default = (title, text)
        self.show_help(None)

    def set_diagram(self, widget: QWidget, caption: str = "") -> None:
        if self.explain is None:
            return
        self._diagram_layout.addWidget(widget)
        self.diagram_frame.show()
        if caption:
            self.explain_caption.setText(caption.upper())

    # -- behaviour --------------------------------------------------------

    def show_help(self, key: str | None) -> None:
        if self.explain is None:
            return
        title, text, default = self._help.get(key, (*self._help_default, "")) if key else (*self._help_default, "")
        self.help_title.setText(title)
        self.help_body.setText(text)
        self.help_meta.setText(tr("MainWindow", "Default: {value}").format(value=default) if default else "")
        self.help_meta.setVisible(bool(default))
        section = self._section_of(QApplication.focusWidget()) if key else None
        if key and section is None:
            section = self._section_of(QApplication.widgetAt(QCursor.pos()))
        if section is not None and section.key != self.active_section:
            self.active_section = section.key
            self.section_changed.emit(section.key)
        for each in self.sections.values():
            active = each is section
            if bool(each.property("active")) != active:
                each.setProperty("active", active)
                each.style().unpolish(each)
                each.style().polish(each)

    def _section_of(self, widget: QWidget | None) -> Section | None:
        while widget is not None:
            if isinstance(widget, Section):
                return widget
            widget = widget.parentWidget()
        return None

    def jump_to(self, key: str) -> None:
        section = self.sections[key]
        self.scroller.glide_to(section.y() - 14)
        button = self._rail_buttons.get(key)
        if button is not None:
            button.setChecked(True)

    def _follow_scroll(self, value: int) -> None:
        """The rail marks the last section whose top has scrolled past."""
        if not self._rail_buttons:
            return
        bar = self.column.verticalScrollBar()
        # At the very bottom the last card may never reach the top.
        keys = list(self._rail_buttons)
        current = keys[-1] if value >= bar.maximum() > 0 else keys[0]
        if value < bar.maximum() or bar.maximum() == 0:
            for key in keys:
                if self.sections[key].y() - 30 <= value:
                    current = key
        self._rail_buttons[current].setChecked(True)

    def set_rail_warning(self, key: str, on: bool) -> None:
        button = self._rail_buttons.get(key)
        if button is not None and bool(button.property("warn")) != on:
            button.setProperty("warn", on)
            button.setIcon(dot_icon(WARN_COLOR if on else self._rail_colors[key]))
            button.style().unpolish(button)
            button.style().polish(button)


DIALOG_STYLE = """
QFrame#sheetHead { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1d2431, stop:1 #191f29);
    border: 0; border-bottom: 1px solid #303947; }
QLabel#sheetTitle { font-size: 15px; font-weight: 700; background: transparent; }
QLabel#sheetSub { color: #7d8794; font-size: 12px; background: transparent; }
QLabel#sheetChip { color: #ff9dcc; background: #2a1f2a; border: 1px solid #6b3a55; border-radius: 10px;
    padding: 3px 9px; font-size: 10.5px; font-weight: 700; }
QFrame#sheetFoot { background: #171d26; border: 0; border-top: 1px solid #303947; }
QLabel#sheetSummary { color: #aeb8c5; font-size: 12.5px; background: transparent; }
QLabel#sheetSummary[bad="true"] { color: #ffb347; }
"""


class SheetDialog(QDialog):
    """Header, a `ConfigSheet`, and a footer with one pink action.

    Restore defaults and Cancel are ghost buttons: pink marks the action.
    `summary` is a line in the footer for what the action will do.
    """

    def __init__(self, title: str, subtitle: str = "", chip: str = "", action: str = "OK",
                 rail: bool = True, explain: bool = True, restore: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setStyleSheet(DIALOG_STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QFrame()
        head.setObjectName("sheetHead")
        # Kept, so a dialog can put a control at the header's right.
        self.head_row = head_row = QHBoxLayout(head)
        head_row.setContentsMargins(16, 12, 16, 12)
        words = QVBoxLayout()
        words.setSpacing(1)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("sheetTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("sheetSub")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))
        words.addWidget(self.title_label)
        words.addWidget(self.subtitle_label)
        head_row.addLayout(words, 1)
        if chip:
            badge = QLabel(chip)
            badge.setObjectName("sheetChip")
            head_row.addWidget(badge, 0, Qt.AlignTop)
        root.addWidget(head)

        self.sheet = ConfigSheet(rail=rail, explain=explain)
        root.addWidget(self.sheet, 1)

        foot = QFrame()
        foot.setObjectName("sheetFoot")
        row = QHBoxLayout(foot)
        row.setContentsMargins(16, 10, 16, 10)
        row.setSpacing(8)
        self.restore_button = QPushButton(tr("MainWindow", "Restore defaults"))
        self.restore_button.setProperty("role", "ghost")
        self.restore_button.setVisible(restore)
        row.addWidget(self.restore_button)
        self.summary = QLabel()
        self.summary.setObjectName("sheetSummary")
        self.summary.setWordWrap(True)
        row.addWidget(self.summary, 1)
        self.cancel_button = QPushButton(tr("MainWindow", "Cancel"))
        self.cancel_button.setProperty("role", "ghost")
        self.cancel_button.setStyleSheet("QPushButton { border: 1px solid #3a4554; }")
        self.cancel_button.clicked.connect(self.reject)
        row.addWidget(self.cancel_button)
        self.action_button = QPushButton(action)
        self.action_button.clicked.connect(self.accept)
        row.addWidget(self.action_button)
        root.addWidget(foot)
        # Only once the button is inside the dialog: set while the footer had
        # no parent, QDialog never heard of it and made the first auto-default
        # button its default instead -- "Restore defaults", so Enter reset the
        # sheet, or did nothing where that button is hidden.
        self.action_button.setDefault(True)

    def set_summary(self, text: str, bad: bool = False) -> None:
        self.summary.setText(text)
        if bool(self.summary.property("bad")) != bad:
            self.summary.setProperty("bad", bad)
            self.summary.style().unpolish(self.summary)
            self.summary.style().polish(self.summary)


class Diagram(QWidget):
    """A painted picture for the explain panel, redrawn on `refresh()`.

    `painter_fn(painter, rect)` does the drawing; the dialog passes one that
    reads its own `config()`, so the picture is what gets written.
    """

    changed = Signal()

    def __init__(self, painter_fn, height: int = 150, parent=None) -> None:
        super().__init__(parent)
        self.painter_fn = painter_fn
        self.setFixedHeight(height)
        self.setStyleSheet("background: transparent;")

    def refresh(self) -> None:
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        try:
            self.painter_fn(painter, QRect(0, 0, self.width(), self.height()))
        finally:
            painter.end()
