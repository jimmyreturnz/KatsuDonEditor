"""The editor's dialogs: views, gimmick configs and functions, timing lines, SV functions."""
from __future__ import annotations
from dataclasses import replace
from pathlib import Path
from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QDialog,
    QDialogButtonBox,
    QSizePolicy,
)
from ui.smooth_scroll import smooth
from ui.config_sheet import (
    WARN_COLOR,
    Diagram,
    SheetDialog,
    TrimmedDoubleSpinBox,
    bind_segments,
    set_field_visible,
    bind_tiles,
    own_or_custom,
    set_warning,
)
from app.i18n import tr
from ui import theme
from osu_io.timing import EFFECT_KIAI, EFFECT_OMIT_FIRST_BARLINE, TimingPoint, active_uninherited_at
from chart import sv_curves
from chart.sv_curves import sv_ease
from chart.time_axis import SNAP_DIVISORS, osu_snap_ms, snap_time
from chart.gimmick_session import (
    DEFAULT_RED_LINE_BPM,
    DEFAULT_SHINY_COUNT,
    FAKE_SLIDER_MAX_LENGTH,
    GimmickConfig,
    GimmickConfigError,
    base_bpm_at,
    gimmick_version_for,
    oscillating_series,
    shiny_collides,
    spacing_collides,
)
from ui.common import (
    DON_COLOR,
    DRUMROLL_COLOR,
    KAT_COLOR,
    application_icon,
    equalize_button_widths,
    format_time,
)


def set_row_visible(layout: QFormLayout | None, widget: QWidget, visible: bool) -> None:
    """Show or hide `widget`'s row: its config sheet field, label and all, or
    its QFormLayout row in a dialog not yet on a sheet."""
    field = widget.property("sheetField")
    if field is not None:
        set_field_visible(field, visible)
    elif layout is not None:
        layout.setRowVisible(widget, visible)


# Decimals an SV value can be *typed* to. A green line's multiplier is
# -100/beat_length, so under a 60000 BPM red line the SV that moves a note a
# visible distance differs from its neighbour in the seventh decimal. The graph
# still labels at 2dp: it is read at a glance, and 8 decimals on every point is
# a smear -- the spin boxes are where a value is actually tuned.
SV_DECIMALS = 8


class AddViewDialog(QDialog):
    """Choose a view type and difficulty for a new view.

    The two editors offer their own lists. The layer-restricted types -- one
    gimmick layer's objects, as a chart or as a preview -- only answer a
    question the gimmick page asks, and on the Editor page they were eight
    entries between the five that page is actually for. The plain views of a
    whole chart are offered in both, since reading the real chart is as useful
    beside a gimmick layer as it is on its own.
    """

    def __init__(
        self, difficulties: list[tuple[str, Path]], parent=None,
        current: Path | None = None, gimmick: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("MainWindow", "Add view"))
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        layout = QFormLayout(self)

        self.type_combo = QComboBox()
        # Literal tr() calls, one per type: the i18n coverage gate statically
        # scans for tr("context", "constant") pairs and would silently miss a
        # dynamic VIEW_TYPE_LABELS[key] lookup here.
        self.type_combo.addItem(tr("MainWindow", "Chart"), "chart")
        if gimmick:
            self.type_combo.addItem(tr("MainWindow", "Regular Chart Only"), "chart_regular")
            self.type_combo.addItem(tr("MainWindow", "Fake Sliders Only"), "chart_fake_slider")
            self.type_combo.addItem(tr("MainWindow", "Barlines Only"), "chart_barline")
        self.type_combo.addItem(tr("MainWindow", "SV Editor"), "sv")
        self.type_combo.addItem(tr("MainWindow", "Kiai and Sound Volume"), "kiai_sound")
        self.type_combo.addItem(tr("MainWindow", "Gameplay Viewer"), "gameplay")
        if gimmick:
            self.type_combo.addItem(tr("MainWindow", "Gameplay: Regular Chart Only"), "gameplay_regular")
            self.type_combo.addItem(tr("MainWindow", "Gameplay: Fake Sliders Only"), "gameplay_fake_slider")
            self.type_combo.addItem(tr("MainWindow", "Gameplay: Barlines Only"), "gameplay_barline")
        self.type_combo.addItem(tr("MainWindow", "Density"), "density")
        layout.addRow(tr("MainWindow", "View type"), self.type_combo)

        # The gimmick page edits exactly one difficulty, so it keeps the plain
        # combo it always had -- one entry, disabled by the caller. Only the
        # Editor page gets the list below.
        self.difficulty_combo: QComboBox | None = None
        self.difficulty_list: QListWidget | None = None
        self.all_difficulties_check: QCheckBox | None = None
        if gimmick:
            self.difficulty_combo = QComboBox()
            for label, path in difficulties:
                self.difficulty_combo.addItem(label, str(path))
            found = (
                self.difficulty_combo.findData(str(current))
                if current is not None else -1
            )
            if found >= 0:
                self.difficulty_combo.setCurrentIndex(found)
            layout.addRow(tr("MainWindow", "Difficulty"), self.difficulty_combo)
            self._add_buttons(layout)
            return

        # A checkable list rather than a combo: a mapset is several
        # difficulties of one song, and opening a chart for each of them meant
        # walking this dialog once per difficulty. Ticking is also how "all of
        # them" is expressed, so there is no separate All entry competing with
        # the real ones for a slot in the list.
        self.difficulty_list = QListWidget()
        self.difficulty_list.setSelectionMode(QAbstractItemView.NoSelection)
        for label, path in difficulties:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, str(path))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.difficulty_list.addItem(item)
        # The difficulty being edited, not whichever one sorts first: adding a
        # view is nearly always about the one already open. Falls back to the
        # first, so the dialog never opens with nothing to add.
        self._check_default(str(current) if current is not None else None)
        self._size_difficulty_list()
        self.difficulty_list.itemChanged.connect(self._difficulty_checked)
        smooth(self.difficulty_list)

        self.all_difficulties_check = QCheckBox(
            tr("MainWindow", "All taiko difficulties"))
        self.all_difficulties_check.toggled.connect(self._toggle_all_difficulties)

        difficulty_box = QWidget()
        difficulty_rows = QVBoxLayout(difficulty_box)
        difficulty_rows.setContentsMargins(0, 0, 0, 0)
        difficulty_rows.setSpacing(4)
        difficulty_rows.addWidget(self.all_difficulties_check)
        difficulty_rows.addWidget(self.difficulty_list)
        layout.addRow(tr("MainWindow", "Difficulty"), difficulty_box)
        self._add_buttons(layout)

    def _add_buttons(self, layout: QFormLayout) -> None:
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _size_difficulty_list(self) -> None:
        """Tall enough to show every difficulty, up to most of the screen.

        Derived from the rows rather than a typed height: a typed 140px showed
        four difficulties and made a mapset of twelve a scrolling exercise in a
        dialog whose whole job is ticking them. Bounded by the screen the
        dialog opens on, so a set of forty still leaves the OK button on it.
        """
        rows = self.difficulty_list.count()
        row_height = max(
            self.difficulty_list.sizeHintForRow(0) if rows else 0,
            self.fontMetrics().height() + 6,
        )
        frame = 2 * self.difficulty_list.frameWidth() + 4
        wanted = rows * row_height + frame
        screen = self.screen() or QApplication.primaryScreen()
        ceiling = (
            int(screen.availableGeometry().height() * 0.6) if screen is not None
            else wanted
        )
        self.difficulty_list.setMinimumHeight(min(wanted, ceiling))

    def _items(self):
        if self.difficulty_list is None:
            return []
        return [
            self.difficulty_list.item(index)
            for index in range(self.difficulty_list.count())
        ]

    def _check_default(self, wanted: str | None) -> None:
        """Tick the difficulty being edited, or the first one."""
        items = self._items()
        if not items:
            return
        match = next(
            (item for item in items if item.data(Qt.UserRole) == wanted), items[0])
        match.setCheckState(Qt.Checked)

    def _toggle_all_difficulties(self, checked: bool) -> None:
        # Blocked because itemChanged fires per row and would otherwise untick
        # this box again on the first one it sets.
        self.difficulty_list.blockSignals(True)
        for item in self._items():
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.difficulty_list.blockSignals(False)

    def _difficulty_checked(self, _item) -> None:
        """Unticking one row is no longer "all of them"."""
        every = all(item.checkState() == Qt.Checked for item in self._items())
        if every != self.all_difficulties_check.isChecked():
            self.all_difficulties_check.blockSignals(True)
            self.all_difficulties_check.setChecked(every)
            self.all_difficulties_check.blockSignals(False)

    def selected_view_type(self) -> str:
        return str(self.type_combo.currentData())

    def selected_difficulty_paths(self) -> list[Path]:
        if self.difficulty_combo is not None:
            return [Path(self.difficulty_combo.currentData())]
        return [
            Path(item.data(Qt.UserRole))
            for item in self._items()
            if item.checkState() == Qt.Checked
        ]

    def selected_difficulty_path(self) -> Path:
        """The first ticked difficulty.

        Kept for the callers that only ever want one -- the gimmick page, which
        offers a single entry, and the tests that pin which difficulty the
        dialog opens on.
        """
        paths = self.selected_difficulty_paths()
        return paths[0] if paths else Path(self._items()[0].data(Qt.UserRole))


class GimmickEntryDialog(QDialog):
    """Asked once per difficulty, the first time the Gimmick tab is opened.

    Gimmick editing fills a map with 60000 BPM lines and objects that cannot be
    hit. That is not something to do to the only copy of a chart by accident, so
    the default answer creates a separate difficulty and the dialog says plainly
    what each button does before anything is written.

    The answer is remembered permanently (gimmick_index.json), so this is asked
    once per difficulty rather than once per visit.
    """

    CREATE = "create"
    USE_CURRENT = "use_current"
    CANCEL = "cancel"

    def __init__(self, version: str, parent=None, references: list[tuple[str, Path]] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("MainWindow", "Gimmick editor"))
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self._action = self.CANCEL

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        headline = QLabel(tr("MainWindow", "Edit gimmicks on a new difficulty?"))
        headline.setStyleSheet("font-weight: 600;")
        layout.addWidget(headline)

        body = QLabel(
            tr(
                "MainWindow",
                "Yes creates a new difficulty called \"{0}\" in this song folder, "
                "copied from the one you have open, and edits that.\n\n"
                "Use This One edits the difficulty you already have open, without "
                "making a copy.\n\n"
                "Either way, keep an un-gimmicked version of the chart: gimmick "
                "timing cannot be cleanly undone once the file is saved.",
            ).format(gimmick_version_for(version))
        )
        body.setWordWrap(True)
        layout.addWidget(body)

        # Which difficulty the grid comes from. Asked here rather than
        # discovered later because the answer has to be settled before the first
        # placement writes a 60000 BPM line into whatever would otherwise have
        # been the source of it.
        layout.addWidget(QLabel(tr("MainWindow", "Timing reference")))
        self.reference_combo = QComboBox()
        # Difficulty names are long and the combo sized itself to the shortest
        # one, so the entry it was showing was usually elided into
        # unreadability -- the one control on this dialog whose whole job is to
        # say which file you are choosing.
        self.reference_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.reference_combo.setMinimumWidth(360)
        for label, path in references or ():
            self.reference_combo.addItem(label, str(path))
        note = QLabel()
        note.setWordWrap(True)
        note.setStyleSheet("color:#ffb347;border:0;")
        if self.reference_combo.count():
            note.setText(tr(
                "MainWindow",
                "Snaps, scrolling and the BPM overlay follow this difficulty's timing "
                "for the life of the pairing, so the barlines you place here never move "
                "the grid you place them against.",
            ))
        else:
            self.reference_combo.addItem(
                tr("MainWindow", "This difficulty (no other one exists)"), ""
            )
            note.setText(tr(
                "MainWindow",
                "There is no other difficulty in this song folder to take timing from. "
                "The one you have open will be used, which works until it has gimmicks "
                "of its own -- making a plain, correctly-timed difficulty first is worth "
                "the minute it costs.",
            ))
        layout.addWidget(self.reference_combo)
        layout.addWidget(note)

        buttons = QDialogButtonBox()
        # Explicit roles rather than Yes/No: all three are affirmative answers
        # to different questions, and only Cancel actually leaves.
        create = buttons.addButton(tr("MainWindow", "Yes"), QDialogButtonBox.AcceptRole)
        use_current = buttons.addButton(
            tr("MainWindow", "Use This One"), QDialogButtonBox.ActionRole
        )
        buttons.addButton(tr("MainWindow", "No"), QDialogButtonBox.RejectRole)
        create.clicked.connect(lambda: self._finish(self.CREATE))
        use_current.clicked.connect(lambda: self._finish(self.USE_CURRENT))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        equalize_button_widths((create, use_current))

    def _finish(self, action: str) -> None:
        self._action = action
        self.accept()

    def selected_action(self) -> str:
        """CREATE, USE_CURRENT, or CANCEL if the dialog was dismissed."""
        return self._action

    def selected_reference(self) -> Path | None:
        """The difficulty to take timing from, or None for the open one."""
        data = self.reference_combo.currentData()
        return Path(data) if data else None


# Read as each diagram is drawn, so a theme switch reaches them.
def _diagram_ink() -> QColor:
    return theme.color("#e8edf3")


def _diagram_muted() -> QColor:
    return theme.color("#7d8794")


_DIAGRAM_RULE = QColor(255, 255, 255, 20)


# Off while a diagram is drawn as a Convert Notes tile: at that size its
# labels are a smudge, and the tile's own caption names the structure.
_diagram_labels = True


def _diagram_text(painter: QPainter, x: float, y: float, text: str, color: QColor | None = None,
                  align=Qt.AlignLeft, bold: bool = False) -> None:
    if not _diagram_labels:
        return
    if color is None:
        color = _diagram_muted()
    font = painter.font()
    font.setPixelSize(10)
    font.setBold(bold)
    painter.setFont(font)
    painter.setPen(color)
    width = painter.fontMetrics().horizontalAdvance(text)
    left = x - width / 2 if align == Qt.AlignHCenter else x - width if align == Qt.AlignRight else x
    painter.drawText(QPointF(left, y), text)


def paint_barline_notes(painter: QPainter, rect: QRect, config: GimmickConfig) -> None:
    """A Don and a Kat drawn out of bars: the squash on the note, the
    restores at each spacing (mirrored or trailing), on a ms axis."""
    mid = rect.center().x()
    kats = (config.kat_spacing1_ms, config.kat_spacing2_ms, config.kat_spacing3_ms)
    reach = max(10, config.spacing_ms + 1, max(kats) + 1)
    px_per_ms = (rect.width() / 2 - 18) / reach
    rows = (
        ("DON", 0.30, (config.spacing_ms,), config.mirror_don_lines, QColor(*DON_COLOR)),
        ("KAT", 0.66, kats, config.mirror_kat_lines, QColor(*KAT_COLOR)),
    )
    for name, fraction, spacings, mirrored, color in rows:
        y = rect.top() + rect.height() * fraction
        _diagram_text(painter, rect.left() + 8, y - 20, name, bold=True)
        painter.setPen(QPen(_DIAGRAM_RULE, 1))
        painter.drawLine(QPointF(rect.left() + 8, y), QPointF(rect.right() - 8, y))
        offsets = [-value for value in spacings] + list(spacings) if mirrored else list(spacings)
        painter.setPen(QPen(_diagram_ink(), 2.5))
        for offset in offsets:
            x = mid + offset * px_per_ms
            painter.drawLine(QPointF(x, y - 15), QPointF(x, y + 15))
        painter.setPen(QPen(color, 2, Qt.DashLine))
        painter.drawLine(QPointF(mid, y - 15), QPointF(mid, y + 15))
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QPointF(mid, y), 4, 4)
    step = 5 if reach <= 20 else 10 if reach <= 50 else 50
    tick = -(reach // step) * step
    while tick <= reach:
        label = f"+{tick}" if tick > 0 else str(tick)
        _diagram_text(painter, mid + tick * px_per_ms, rect.bottom() - 6, label, align=Qt.AlignHCenter)
        tick += step
    _diagram_text(painter, rect.right() - 8, rect.top() + 14, "ms", align=Qt.AlignRight)


def paint_anti_barline(painter: QPainter, rect: QRect, config: GimmickConfig) -> None:
    """The wall with a Don slit and a Kat slit, widths in wall ticks."""
    gap = max(1.6, 115.0 / max(1, config.anti_lines_per_beat))
    top, bottom = rect.top() + 18, rect.bottom() - 40
    don = (rect.left() + rect.width() * 0.30, config.anti_don_ticks)
    kat = (rect.left() + rect.width() * 0.66, config.anti_kat_ticks)
    painter.setPen(QPen(_diagram_ink(), 1.2))
    x = rect.left() + 8.0
    while x < rect.right() - 8:
        in_slit = any(start < x < start + ticks * gap + 0.01 for start, ticks in (don, kat))
        if not in_slit:
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
        x += gap
    for (start, ticks), name, color in ((don, "Don", QColor(*DON_COLOR)), (kat, "Kat", QColor(*KAT_COLOR))):
        _diagram_text(painter, start + ticks * gap / 2, bottom + 16, f"{name} {ticks}",
                      color, Qt.AlignHCenter, bold=True)
    _diagram_text(painter, rect.left() + 8, rect.bottom() - 6,
                  tr("MainWindow", "{count} lines per beat").format(count=config.anti_lines_per_beat))


def paint_shiny_stack(painter: QPainter, rect: QRect, config: GimmickConfig) -> None:
    """Two notes at T: one with its shiny stack from the shiny offset, one
    with a plain fake slider at the fake slider offset. On the same axis, so
    the two offsets meeting -- amber -- is visible as the same column."""
    left = rect.left() + 34
    px_per_ms = (rect.width() - 60) / 6
    clash = config.shiny_offset_ms == config.fake_slider_offset_ms
    rows = ((tr("MainWindow", "Shiny"), rect.top() + rect.height() * 0.28),
            (tr("MainWindow", "Fake slider"), rect.top() + rect.height() * 0.62))
    for name, y in rows:
        _diagram_text(painter, rect.left() + 8, y - 17, name.upper(), bold=True)
        painter.setPen(QPen(_DIAGRAM_RULE, 1))
        painter.drawLine(QPointF(rect.left() + 8, y), QPointF(rect.right() - 8, y))
    shiny_y, fake_y = rows[0][1], rows[1][1]
    for index in reversed(range(config.shiny_count)):
        x = left + (config.shiny_offset_ms + index) * px_per_ms
        if x > rect.right():
            continue
        colour = QColor(*DRUMROLL_COLOR)
        painter.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
        painter.setBrush(colour)
        painter.drawEllipse(QPointF(x, shiny_y), 10, 10)
    fake_x = left + config.fake_slider_offset_ms * px_per_ms
    painter.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
    painter.setBrush(QColor(*DRUMROLL_COLOR))
    painter.drawEllipse(QPointF(fake_x, fake_y), 10, 10)
    for y in (shiny_y, fake_y):
        painter.setPen(QPen(Qt.white, 2))
        painter.setBrush(QColor(*DON_COLOR))
        painter.drawEllipse(QPointF(left, y), 11, 11)
    if clash:
        painter.setPen(QPen(QColor(WARN_COLOR), 1.5, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(fake_x - 14, shiny_y - 15, 28, fake_y - shiny_y + 30))
    for ms in range(7):
        x = left + ms * px_per_ms
        _diagram_text(painter, x, rect.bottom() - 8, f"+{ms}" if ms else "T", align=Qt.AlignHCenter)


def paint_hidden_anti_barline(painter: QPainter, rect: QRect, config: GimmickConfig) -> None:
    """One sheet of bars, and a slit that narrows as it nears the hit
    position: the raised SV pushes bars ahead in proportion to how far away
    they still are, so the gap is a wedge that closes at the target."""
    top, bottom = rect.top() + 14, rect.bottom() - 26
    target = rect.left() + 24
    slit_right = rect.right() - rect.width() * 0.18
    excess = max(0.0, config.hidden_don_sv / max(config.hidden_base_sv, 1e-9) - 1.0)
    painter.setPen(QPen(_diagram_ink(), 1))
    x = rect.left() + 8.0
    gap = 3.2
    while x < rect.right() - 8:
        distance = max(0.0, x - target) / max(1.0, slit_right - target)
        half = min(28.0, 4 + 900 * excess * distance) if x < slit_right else 0
        centre = target + (slit_right - target) * 0.75
        if not (half and abs(x - centre) < half):
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
        x += gap
    painter.setPen(QPen(theme.color("#ff66aa"), 2))
    painter.drawLine(QPointF(target, top - 4), QPointF(target, bottom + 4))
    _diagram_text(painter, target + 4, rect.bottom() - 8, tr("MainWindow", "hit position"), theme.color("#f3a6bd"))


def paint_fake_slider_structure(painter: QPainter, rect: QRect, config: GimmickConfig) -> None:
    """A note at T -- faint when hidden -- and its fake slider at the offset."""
    left = rect.left() + 40
    px_per_ms = (rect.width() - 70) / max(4, config.fake_slider_offset_ms + 1)
    y = rect.center().y() - 6
    painter.setPen(QPen(_DIAGRAM_RULE, 1))
    painter.drawLine(QPointF(rect.left() + 8, y), QPointF(rect.right() - 8, y))
    note = QColor(*DON_COLOR)
    note.setAlphaF(0.3 if config.hide_note else 1.0)
    painter.setPen(QPen(QColor(255, 255, 255, 90 if config.hide_note else 230), 2))
    painter.setBrush(note)
    painter.drawEllipse(QPointF(left, y), 14, 14)
    fake_x = left + config.fake_slider_offset_ms * px_per_ms
    painter.setPen(QPen(QColor(255, 255, 255, 220), 2))
    painter.setBrush(QColor(*DRUMROLL_COLOR))
    painter.drawEllipse(QPointF(fake_x, y), 14, 14)
    _diagram_text(painter, left, rect.bottom() - 10, "T", align=Qt.AlignHCenter)
    _diagram_text(painter, fake_x, rect.bottom() - 10, f"+{config.fake_slider_offset_ms} ms", align=Qt.AlignHCenter)


def _tile_picture(painter_fn, config: GimmickConfig) -> QPixmap:
    """A structure's diagram, small and unlabelled, for a Convert Notes tile."""
    global _diagram_labels
    pixmap = QPixmap(140, 56)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setOpacity(0.85)
    _diagram_labels = False
    try:
        painter_fn(painter, QRect(0, 0, 140, 56), config)
    finally:
        _diagram_labels = True
        painter.end()
    return pixmap


class GimmickConfigDialog(SheetDialog):
    """One layer's toolbox settings, as a config sheet.

    Two layers, two configs: the barline layer's red line spacing and the fake
    slider layer's offset are separate numbers that must not agree, so each
    layer opens its own dialog and both show the caution naming the other's
    value. `other` is that other config -- read only, for the caution.

    The barline layer's Config is three tools' settings (barline notes, the
    Red Line tool, anti-barline), so each gets its own section; one unbroken
    list of 13 numbers put "Redline BPM" and "Anti-barline barline BPM" ten
    rows apart with nothing saying which tool either belonged to.
    """

    def __init__(self, config: GimmickConfig, layer_id: str, other: GimmickConfig, parent=None) -> None:
        sv = layer_id.startswith("sv_")
        barline = layer_id == "barline"
        if sv:
            title = tr("MainWindow", "SV layer settings")
            subtitle = tr("MainWindow", "Saved with this layer. Used by its Green Line and Function tools.")
        elif barline:
            title = tr("MainWindow", "Barline settings")
            subtitle = tr("MainWindow", "Saved with this layer. Used by the Don, Kat, Red Line and Convert Notes tools.")
        else:
            title = tr("MainWindow", "Fake slider settings")
            subtitle = tr("MainWindow", "Saved with this layer. Used by the Fake Slider, Don, Kat, Shiny and Multiple tools.")
        super().__init__(title, subtitle, action=tr("MainWindow", "Save"), rail=not sv, explain=not sv, parent=parent)
        self.layer_id = layer_id
        self._other = other
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.bpm_spin = QDoubleSpinBox()
        self.bpm_spin.setRange(1.0, 1000000.0)
        self.bpm_spin.setDecimals(0)
        self.bpm_spin.setValue(config.gimmick_bpm)

        # The other half of the pair the owner asked to separate: the BPM
        # above is how invisible the note is, this one is how fast the chart
        # runs once the structure has given it back. Unchecked is the
        # chart's own BPM at that millisecond, which is what every structure
        # written before this dial existed used. Same shape as the two BPM
        # boxes below it.
        self.restore_bpm_check = QCheckBox(tr("MainWindow", "Custom"))
        self.restore_bpm_check.setChecked(config.restore_bpm is not None)
        self.restore_bpm_spin = QDoubleSpinBox()
        self.restore_bpm_spin.setRange(0.001, 1000000.0)
        self.restore_bpm_spin.setDecimals(3)
        self.restore_bpm_spin.setValue(
            config.restore_bpm if config.restore_bpm is not None
            else DEFAULT_RED_LINE_BPM
        )
        self.restore_bpm_spin.setEnabled(self.restore_bpm_check.isChecked())
        self.restore_bpm_check.toggled.connect(self.restore_bpm_spin.setEnabled)

        self.sv_offset_spin = QSpinBox()
        self.sv_offset_spin.setRange(-5000, 5000)
        self.sv_offset_spin.setValue(config.sv_offset_ms)

        self.spacing_spin = QSpinBox()
        self.spacing_spin.setRange(1, 1000)
        self.spacing_spin.setValue(config.spacing_ms)
        self.spacing_spin.valueChanged.connect(self._update_caution)

        # Kat's three mirrored pairs, independently configurable -- see the
        # comment above `gimmick_session.DEFAULT_SPACING_MS`. Each feeds the
        # same caution as the Don spacing above: any one of the four landing
        # on the fake slider's own offset is a collision (`spacing_collides`
        # now checks all four), so every spin box here re-runs it.
        self.kat_spacing1_spin = QSpinBox()
        self.kat_spacing1_spin.setRange(1, 1000)
        self.kat_spacing1_spin.setValue(config.kat_spacing1_ms)
        self.kat_spacing1_spin.valueChanged.connect(self._update_caution)

        self.kat_spacing2_spin = QSpinBox()
        self.kat_spacing2_spin.setRange(1, 1000)
        self.kat_spacing2_spin.setValue(config.kat_spacing2_ms)
        self.kat_spacing2_spin.valueChanged.connect(self._update_caution)

        self.kat_spacing3_spin = QSpinBox()
        self.kat_spacing3_spin.setRange(1, 1000)
        self.kat_spacing3_spin.setValue(config.kat_spacing3_ms)
        self.kat_spacing3_spin.valueChanged.connect(self._update_caution)

        self.fake_offset_spin = QSpinBox()
        self.fake_offset_spin.setRange(1, 1000)
        self.fake_offset_spin.setValue(config.fake_slider_offset_ms)
        self.fake_offset_spin.valueChanged.connect(self._update_caution)

        self.length_spin = QDoubleSpinBox()
        # Short enough to derive no duration: anything longer is a real,
        # hittable drumroll. The upper bound is the config's own, so the box
        # cannot ask for a length `GimmickConfig` would reject -- and it
        # reaches the positive near-zero form (`0.001`) as well as the
        # canonical negative one.
        self.length_spin.setRange(-100000.0, FAKE_SLIDER_MAX_LENGTH)
        self.length_spin.setDecimals(4)
        self.length_spin.setValue(config.fake_slider_length)

        self.fake_sv_spin = TrimmedDoubleSpinBox()
        self.fake_sv_spin.setRange(0.01, 100.0)
        self.fake_sv_spin.setDecimals(SV_DECIMALS)
        self.fake_sv_spin.setValue(config.fake_slider_sv)

        self.place_notes_check = QCheckBox(
            tr("MainWindow", "Also place the note itself")
        )
        self.place_notes_check.setChecked(config.place_notes)

        # Plain fake sliders only -- Don/Kat need their squash and a shiny is
        # identified by its line. Off is the bare drawn object, which is what
        # the Multiple tool wants: on, it writes one line per slider in a run,
        # each restating a BPM already in force.
        self.fake_red_line_check = QCheckBox(
            tr("MainWindow", "Write a red line for plain fake sliders")
        )
        self.fake_red_line_check.setChecked(config.fake_slider_red_line)

        # Mirrored reads as one object centred on the note; unticked, the bars
        # all trail it -- a squash on the note and its restores after it, which
        # is the style several hand-made maps are written in and which nothing
        # here could ask for before. One box per kind, beside that kind's own
        # spacings: Don and Kat are separate structures layered on one region,
        # and a single box made a centred Don force a centred Kat.
        self.mirror_don_check = QCheckBox(
            tr("MainWindow", "Mirror Don bars on both sides")
        )
        self.mirror_don_check.setChecked(config.mirror_don_lines)
        self.mirror_kat_check = QCheckBox(
            tr("MainWindow", "Mirror Kat bars on both sides")
        )
        self.mirror_kat_check.setChecked(config.mirror_kat_lines)

        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(-1000, 1000)
        self.offset_spin.setValue(config.red_line_offset_ms)

        # An uninherited point's BPM is also its scroll speed, so the plain red
        # line tool is a speed change with no green line involved -- but only
        # when it carries a BPM of its own. Unchecked it writes the chart's own
        # BPM, which is the line that changes nothing but where the bars fall.
        self.red_bpm_check = QCheckBox(tr("MainWindow", "Custom"))
        self.red_bpm_check.setChecked(config.red_line_bpm is not None)
        self.red_bpm_spin = QDoubleSpinBox()
        self.red_bpm_spin.setRange(0.001, 1000000.0)
        self.red_bpm_spin.setDecimals(3)
        self.red_bpm_spin.setValue(
            config.red_line_bpm if config.red_line_bpm is not None
            else DEFAULT_RED_LINE_BPM
        )
        self.red_bpm_spin.setEnabled(self.red_bpm_check.isChecked())
        self.red_bpm_check.toggled.connect(self.red_bpm_spin.setEnabled)

        # Beats per bar for that same line. osu! reads meter from the
        # uninherited point in force, so a standalone red line re-bars
        # everything after it -- at 4/4 unless it is told otherwise. The
        # ceiling matches TimingLineDialog's, where anti-barline's 999 lives.
        self.red_meter_spin = QSpinBox()
        self.red_meter_spin.setRange(1, 10000)
        self.red_meter_spin.setValue(config.red_line_meter)

        # Bit 3 of the effects field. A fake slider's 60000 BPM line draws a bar
        # nobody asked for; the barline layer's whole output is bars, so it is
        # offered here only.
        self.omit_barline_check = QCheckBox()
        self.omit_barline_check.setChecked(config.omit_barline)

        self.shiny_offset_spin = QSpinBox()
        self.shiny_offset_spin.setRange(1, 1000)
        self.shiny_offset_spin.setValue(config.shiny_offset_ms)
        self.shiny_offset_spin.valueChanged.connect(self._update_caution)

        self.shiny_count_spin = QSpinBox()
        self.shiny_count_spin.setRange(1, 50)
        self.shiny_count_spin.setValue(config.shiny_count)

        self.shiny_bpm_spin = QDoubleSpinBox()
        self.shiny_bpm_spin.setRange(0.001, 1000.0)
        self.shiny_bpm_spin.setDecimals(3)
        self.shiny_bpm_spin.setValue(config.shiny_bpm_multiplier)

        # Same trade as fake_red_line_check, for a shiny's own line. Free to
        # turn off when the shiny decorates a note already in the chart --
        # the note alone still identifies it. A shiny with no note of its own
        # loses the only thing that told it apart from a plain fake slider.
        # The other half of that line: without a restore it governs the rest
        # of the map, and the second line a mapper would place by hand is
        # exactly the one the Red Line tool refuses, since the slider's own
        # line already owns that millisecond.
        self.fake_restore_after_check = QCheckBox(
            tr("MainWindow", "Restore the chart BPM 1 ms later"))
        self.fake_restore_after_check.setChecked(config.fake_slider_restore_after)

        self.shiny_red_line_check = QCheckBox(
            tr("MainWindow", "Write a red line for shiny notes")
        )
        self.shiny_red_line_check.setChecked(config.shiny_red_line)

        self.fake_slider_bpm_spin = QDoubleSpinBox()
        self.fake_slider_bpm_spin.setRange(0.001, 1000.0)
        self.fake_slider_bpm_spin.setDecimals(3)
        self.fake_slider_bpm_spin.setValue(config.fake_slider_bpm_multiplier)

        # The anti-barline converter's four numbers. All four are ratios and
        # counts rather than milliseconds, so they hold at any BPM: the wall is
        # a fraction of a beat, the slowdown a factor of it, and the two slit
        # widths a count of wall ticks.
        self.anti_density_spin = QSpinBox()
        self.anti_density_spin.setRange(1, 10000)
        self.anti_density_spin.setValue(config.anti_lines_per_beat)
        # Same shape as the plain red-line BPM above, and for the same reason:
        # unchecked writes the chart's own BPM at each line, which only moves
        # where the bars fall; a typed value is the scroll effect, since an
        # uninherited point's BPM is its scroll speed. DEFAULT_RED_LINE_BPM is
        # only the number the box opens on -- it is never written unless
        # Custom is ticked.
        self.anti_bpm_check = QCheckBox(tr("MainWindow", "Custom"))
        self.anti_bpm_check.setChecked(config.anti_wall_bpm is not None)
        self.anti_bpm_spin = QDoubleSpinBox()
        self.anti_bpm_spin.setRange(0.001, 1000000.0)
        self.anti_bpm_spin.setDecimals(3)
        self.anti_bpm_spin.setValue(
            config.anti_wall_bpm if config.anti_wall_bpm is not None
            else DEFAULT_RED_LINE_BPM
        )
        self.anti_bpm_spin.setEnabled(self.anti_bpm_check.isChecked())
        self.anti_bpm_check.toggled.connect(self.anti_bpm_spin.setEnabled)
        self.anti_don_spin = QSpinBox()
        self.anti_don_spin.setRange(1, 1000)
        self.anti_don_spin.setValue(config.anti_don_ticks)
        self.anti_kat_spin = QSpinBox()
        self.anti_kat_spin.setRange(1, 1000)
        self.anti_kat_spin.setValue(config.anti_kat_ticks)

        # Units live in the box, so the labels lose their "(ms)".
        for spin in (self.bpm_spin, self.restore_bpm_spin, self.red_bpm_spin, self.anti_bpm_spin):
            spin.setSuffix(" BPM")
        for spin in (self.spacing_spin, self.kat_spacing1_spin, self.kat_spacing2_spin,
                     self.kat_spacing3_spin, self.fake_offset_spin, self.offset_spin,
                     self.shiny_offset_spin, self.sv_offset_spin):
            spin.setSuffix(" ms")
        for spin in (self.fake_sv_spin, self.fake_slider_bpm_spin, self.shiny_bpm_spin):
            spin.setSuffix("\u00d7")
        self.anti_don_spin.setSuffix(" " + tr("MainWindow", "ticks"))
        self.anti_kat_spin.setSuffix(" " + tr("MainWindow", "ticks"))
        self.shiny_count_spin.setSuffix(" " + tr("MainWindow", "sliders"))
        own, custom = tr("MainWindow", "Chart's own"), tr("MainWindow", "Custom")
        self.restore_bpm_widget = own_or_custom(self.restore_bpm_check, self.restore_bpm_spin, own, custom)
        self.red_bpm_widget = own_or_custom(self.red_bpm_check, self.red_bpm_spin, own, custom)
        self.anti_bpm_widget = own_or_custom(self.anti_bpm_check, self.anti_bpm_spin, own, custom)
        self.omit_barline_check.setText(tr("MainWindow", "Omit barline"))
        self.mirror_don_check.setText(tr("MainWindow", "Mirror on both sides"))
        self.mirror_kat_check.setText(tr("MainWindow", "Mirror on both sides"))
        self.place_notes_check.setText(tr("MainWindow", "Also place the note"))

        # Each layer is shown the numbers it actually uses. The rest are still
        # carried through `config()` untouched, so opening one layer's dialog
        # cannot silently reset the other's structure.
        sheet = self.sheet
        self.caution = QLabel()
        self.shiny_caution = QLabel()
        self.kat_caution = QLabel()
        if sv:
            section = sheet.add_section("sv", tr("MainWindow", "Green lines"))
            section.field(tr("MainWindow", "Position offset"), self.sv_offset_spin, span=2)
            hint = QLabel(tr(
                "MainWindow",
                "How far ahead of its object this layer's green lines sit. An SV point "
                "governs what comes after it, so a note needs its line slightly early; a "
                "timing line needs it on the same millisecond, which is why the two "
                "gimmick SV layers default to 0.",
            ))
            hint.setObjectName("fieldNote")
            hint.setWordWrap(True)
            section.add(hint, 2)
            self.resize(460, 300)
        else:
            every = sheet.add_section(
                "every", tr("MainWindow", "Every structure"),
                tr("MainWindow", "The hidden note, and the chart after it"))
            every.field(tr("MainWindow", "Invisible note BPM"), self.bpm_spin, tr(
                "MainWindow",
                "The red line on the note's own millisecond. At 60000 BPM the note "
                "scrolls 392 px per ms, so it is never on screen and the structure "
                "around it is the whole object."), default="60000 BPM")
            every.field(tr("MainWindow", "Red line BPM after the structure"), self.restore_bpm_widget, tr(
                "MainWindow",
                "How fast the chart runs once the structure hands it back. Chart's own "
                "reads the BPM in force at that millisecond, which is what every "
                "structure written before this setting used."),
                span=2, default=own, scrub=self.restore_bpm_spin)
        if barline:
            don = sheet.add_section("don", tr("MainWindow", "Barline Don"),
                                    tr("MainWindow", "One pair of bars"), QColor(*DON_COLOR).name())
            don.field(tr("MainWindow", "Spacing"), self.spacing_spin, tr(
                "MainWindow",
                "How far from the note each bar sits. 1 ms is the tightest the file "
                "format allows, and reads as one thin object."), default="1 ms")
            don.break_row()
            don.switch(self.mirror_don_check, tr(
                "MainWindow", "Off: one bar after the note, as most hand-made maps do."),
                default=tr("MainWindow", "off"))
            kat = sheet.add_section("kat", tr("MainWindow", "Barline Kat"),
                                    tr("MainWindow", "Three pairs, so a Kat reads wider than a Don"), QColor(*KAT_COLOR).name())
            kat_help = tr(
                "MainWindow",
                "Kat's three bars, each set on its own so several structures can be "
                "layered on one region. The three must all differ.")
            spacings = QWidget()
            spacing_row = QHBoxLayout(spacings)
            spacing_row.setContentsMargins(0, 0, 0, 0)
            spacing_row.setSpacing(12)
            for label, spin in ((tr("MainWindow", "Spacing 1"), self.kat_spacing1_spin),
                                (tr("MainWindow", "Spacing 2"), self.kat_spacing2_spin),
                                (tr("MainWindow", "Spacing 3"), self.kat_spacing3_spin)):
                spacing_row.addWidget(sheet.field(label, spin, kat_help), 1)
            kat.add(spacings, 2)
            kat.switch(self.mirror_kat_check, tr(
                "MainWindow", "On: bars either side, centred on the note."), default=tr("MainWindow", "on"))
            self.kat_caution = kat.caution(tr(
                "MainWindow",
                "The three Kat spacings must all be different: two bars on one "
                "millisecond are one red line."))
            red = sheet.add_section("red", tr("MainWindow", "Red Line tool"),
                                    tr("MainWindow", "A plain red line where you click"), "#ff5a5a")
            red.field(tr("MainWindow", "Offset"), self.offset_spin, tr(
                "MainWindow", "Moves each placed red line off the snap by this much."), default="0 ms")
            red.field(tr("MainWindow", "Meter"), self.red_meter_spin, tr(
                "MainWindow",
                "Beats per bar for the line. osu! reads meter from the red line in "
                "force, so this re-bars everything after it."), default="4")
            red.field(tr("MainWindow", "BPM"), self.red_bpm_widget, tr(
                "MainWindow",
                "A red line's BPM is also its scroll speed. Chart's own changes nothing "
                "but where the bars fall; a number is a speed change with no green line."),
                span=2, default=own, scrub=self.red_bpm_spin)
            red.switch(self.place_notes_check, tr(
                "MainWindow", "A hittable note on the snap, so the bars are something the player plays."),
                default=tr("MainWindow", "on"))
            anti = sheet.add_section("anti", tr("MainWindow", "Anti-barline"),
                                     tr("MainWindow", "A wall of bars with a slit where each note is"), "#e8edf3")
            anti.field(tr("MainWindow", "Lines per beat"), self.anti_density_spin, tr(
                "MainWindow", "How tight the wall is. The reference section (mekurume) uses 72."), default="36")
            anti.field(tr("MainWindow", "Don slit"), self.anti_don_spin, tr(
                "MainWindow",
                "Wall lines left out for a Don. The slit width is what tells Don from "
                "Kat, since the notes themselves are invisible."), default="1")
            anti.field(tr("MainWindow", "Wall BPM"), self.anti_bpm_widget, tr(
                "MainWindow",
                "Below the chart's own BPM the wall packs tighter: a red line's BPM is "
                "also its scroll speed, so a third of the BPM draws the same lines a "
                "third as far apart."), span=2, default=own, scrub=self.anti_bpm_spin)
            anti.field(tr("MainWindow", "Kat slit"), self.anti_kat_spin, tr(
                "MainWindow", "Wall lines left out for a Kat. Wider than Don's, or the two cannot be told apart."),
                default="2")
            self.caution = anti.caution()
            self.resize(980, 660)
        elif not sv:
            fake = sheet.add_section("slider", tr("MainWindow", "Fake slider"),
                                     tr("MainWindow", "A drumroll too short to hit: only its head is drawn"),
                                     QColor(*DRUMROLL_COLOR).name())
            fake.field(tr("MainWindow", "Offset"), self.fake_offset_spin, tr(
                "MainWindow",
                "How far after the snap the fake slider sits. Must differ from the "
                "shiny offset and from every barline spacing."), default="2 ms")
            fake.field(tr("MainWindow", "Length"), self.length_spin, tr(
                "MainWindow",
                "Anything longer than 0.001 is a real, hittable drumroll. -0.0001 is "
                "the canonical fake slider; long negative lengths draw a track to the "
                "right of the head."), default="-0.001")
            fake.field(tr("MainWindow", "Red line BPM"), self.fake_slider_bpm_spin, tr(
                "MainWindow", "The slider's own red line as a multiple of the chart's BPM. 1\u00d7 restates the BPM in force."),
                default="1\u00d7")
            fake.break_row()
            fake.switch(self.fake_red_line_check, tr(
                "MainWindow", "Off leaves the bare object, which is what the Multiple tool wants."),
                default=tr("MainWindow", "on"))
            fake.switch(self.fake_restore_after_check, tr(
                "MainWindow", "Without it the slider's line governs the rest of the map."),
                default=tr("MainWindow", "off"))
            fake.switch(self.omit_barline_check, tr(
                "MainWindow", "The slider's 60000 BPM line would otherwise stamp a bar nobody asked for."),
                default=tr("MainWindow", "on"))
            self.caution = fake.caution()
            donkat = sheet.add_section("donkat", tr("MainWindow", "Don / Kat"),
                                       tr("MainWindow", "A note that becomes a fake slider in flight"))
            donkat.field(tr("MainWindow", "Gimmick SV"), self.fake_sv_spin, tr(
                "MainWindow",
                "The green line that takes the already squashed note the rest of the "
                "way off screen."), default="10\u00d7")
            shiny = sheet.add_section("shiny", tr("MainWindow", "Shiny"),
                                      tr("MainWindow", "A stack of fake sliders under one note"), "#ffffff")
            shiny.field(tr("MainWindow", "Offset"), self.shiny_offset_spin, tr(
                "MainWindow",
                "The first fake slider of the stack sits this long after the note, the "
                "rest one ms apart."), default="1 ms")
            shiny.field(tr("MainWindow", "Stack size"), self.shiny_count_spin, tr(
                "MainWindow",
                "How many fake sliders sit under the note. In a kiai section each one "
                "adds its own flash, so a deeper stack shines brighter."), default="3")
            shiny.field(tr("MainWindow", "Red line BPM"), self.shiny_bpm_spin, tr(
                "MainWindow", "The shiny's own red line as a multiple of the chart's BPM."), default="1\u00d7")
            shiny.break_row()
            shiny.switch(self.shiny_red_line_check, tr(
                "MainWindow", "Off is fine when the shiny decorates a note already in the chart."),
                default=tr("MainWindow", "on"))
            self.shiny_caution = shiny.caution(tr(
                "MainWindow",
                "The shiny offset equals the fake slider offset. Both land on the same "
                "millisecond, and a stack of fake sliders on one millisecond is what a "
                "shiny is, so the two can no longer be told apart."))
            self.resize(980, 640)
        if not sv:
            sheet.set_help_default(
                tr("MainWindow", "Hover or focus a setting"),
                tr("MainWindow", "This panel says what it does and its default. The picture above follows your numbers."))
            self.diagram = Diagram(self._paint_diagram)
            sheet.set_diagram(self.diagram, tr("MainWindow", "Preview"))
            sheet.section_changed.connect(lambda _key: self.diagram.refresh())
            for spin in self.findChildren(QAbstractSpinBox):
                spin.valueChanged.connect(self.diagram.refresh)
            for check in self.findChildren(QCheckBox):
                check.toggled.connect(self.diagram.refresh)
        self.restore_button.clicked.connect(self._restore_defaults)
        self._last_good = config
        self._update_caution()

    def _update_caution(self) -> None:
        """Warn, don't refuse: the two values only actually clash where a
        barline note and a fake slider land on the same snap.

        `config()` can raise while the three Kat spin boxes are mid-edit --
        keyboard tracking fires on every keystroke, so typing a second Kat
        spacing through a value that briefly matches another is a live,
        reachable state, not just a theoretical one. Caught here rather than
        avoided: the cautions simply hold their last picture until the typing
        settles on something constructible again. `accept()` is the actual
        gate against leaving the dialog with a broken combination.
        """
        kats = (self.kat_spacing1_spin, self.kat_spacing2_spin, self.kat_spacing3_spin)
        kat_clash = len({spin.value() for spin in kats}) < 3
        if self.layer_id == "barline":
            self.kat_caution.setVisible(kat_clash)
            for spin in kats:
                set_warning(spin, kat_clash)
            self.sheet.set_rail_warning("kat", kat_clash)
        try:
            edited = self.config()
        except GimmickConfigError:
            return
        self._last_good = edited
        shiny = shiny_collides(edited)
        self.shiny_caution.setVisible(shiny)
        spacing = spacing_collides(*self._configs_for(edited))
        self.caution.setVisible(spacing)
        self.caution.setText(tr(
            "MainWindow",
            "Caution: one of the barline spacings (Don or Kat) is the same "
            "as the fake slider offset. A fake slider writes 60000 BPM at "
            "its own millisecond and the chart's BPM one offset later, so a "
            "barline note placed on the same snap will fight it for that "
            "line.",
        ))
        # The fields that clash turn amber, not just the paragraph.
        _barline_config, fake_config = self._configs_for(edited)
        offset = fake_config.fake_slider_offset_ms
        if self.layer_id == "barline":
            for spin in (self.spacing_spin, *kats):
                set_warning(spin, (spacing and spin.value() == offset) or (spin in kats and kat_clash))
            self.sheet.set_rail_warning("anti", spacing)
        elif self.layer_id == "fake_slider":
            set_warning(self.fake_offset_spin, shiny or spacing)
            set_warning(self.shiny_offset_spin, shiny)
            self.sheet.set_rail_warning("slider", shiny or spacing)
            self.sheet.set_rail_warning("shiny", shiny)

    def _configs_for(self, edited: GimmickConfig) -> tuple[GimmickConfig, GimmickConfig]:
        """(barline config, fake slider config), whichever this dialog edits."""
        return (edited, self._other) if self.layer_id == "barline" else (self._other, edited)

    def _restore_defaults(self) -> None:
        """Every field this layer shows back to `GimmickConfig()`'s value.

        Only the fields on screen: the rest belong to the other layer's
        structures, and `config()` carries them through untouched.
        """
        default = GimmickConfig()
        values = {
            self.bpm_spin: default.gimmick_bpm, self.spacing_spin: default.spacing_ms,
            self.kat_spacing1_spin: default.kat_spacing1_ms, self.kat_spacing2_spin: default.kat_spacing2_ms,
            self.kat_spacing3_spin: default.kat_spacing3_ms, self.offset_spin: default.red_line_offset_ms,
            self.red_meter_spin: default.red_line_meter, self.anti_density_spin: default.anti_lines_per_beat,
            self.anti_don_spin: default.anti_don_ticks, self.anti_kat_spin: default.anti_kat_ticks,
            self.fake_offset_spin: default.fake_slider_offset_ms, self.length_spin: default.fake_slider_length,
            self.fake_sv_spin: default.fake_slider_sv, self.fake_slider_bpm_spin: default.fake_slider_bpm_multiplier,
            self.shiny_offset_spin: default.shiny_offset_ms, self.shiny_count_spin: default.shiny_count,
            self.shiny_bpm_spin: default.shiny_bpm_multiplier, self.sv_offset_spin: default.sv_offset_ms,
        }
        checks = {
            self.mirror_don_check: default.mirror_don_lines, self.mirror_kat_check: default.mirror_kat_lines,
            self.place_notes_check: default.place_notes, self.omit_barline_check: default.omit_barline,
            self.fake_red_line_check: default.fake_slider_red_line,
            self.fake_restore_after_check: default.fake_slider_restore_after,
            self.shiny_red_line_check: default.shiny_red_line,
            self.restore_bpm_check: default.restore_bpm is not None,
            self.red_bpm_check: default.red_line_bpm is not None,
            self.anti_bpm_check: default.anti_wall_bpm is not None,
        }
        # A widget this layer never placed has no window of its own.
        for widget, value in values.items():
            if widget.window() is self:
                widget.setValue(value)
        for widget, value in checks.items():
            if widget.window() is self:
                widget.setChecked(value)

    def _paint_diagram(self, painter: QPainter, rect: QRect) -> None:
        """The structure the numbers draw, from the last valid `config()`."""
        config = self._last_good
        if self.layer_id == "barline":
            if self.sheet.active_section == "anti":
                paint_anti_barline(painter, rect, config)
            else:
                paint_barline_notes(painter, rect, config)
        else:
            paint_shiny_stack(painter, rect, config)

    def accept(self) -> None:
        # The three Kat spin boxes have no shared range to enforce
        # distinctness the way a single min/max keeps every other field here
        # constructible -- so unlike them, this one combination has to be
        # checked by hand before the dialog is allowed to close, or an
        # invalid GimmickConfig would reach `_open_gimmick_config` and raise
        # there instead, with no field left focused to point at the mistake.
        try:
            self.config()
        except GimmickConfigError as error:
            QMessageBox.warning(self, tr("MainWindow", "Cannot save this"), str(error))
            return
        super().accept()

    def config(self) -> GimmickConfig:
        return GimmickConfig(
            gimmick_bpm=self.bpm_spin.value(),
            spacing_ms=self.spacing_spin.value(),
            kat_spacing1_ms=self.kat_spacing1_spin.value(),
            kat_spacing2_ms=self.kat_spacing2_spin.value(),
            kat_spacing3_ms=self.kat_spacing3_spin.value(),
            fake_slider_length=self.length_spin.value(),
            red_line_offset_ms=self.offset_spin.value(),
            fake_slider_offset_ms=self.fake_offset_spin.value(),
            fake_slider_sv=self.fake_sv_spin.value(),
            fake_slider_red_line=self.fake_red_line_check.isChecked(),
            fake_slider_restore_after=self.fake_restore_after_check.isChecked(),
            anti_lines_per_beat=self.anti_density_spin.value(),
            anti_wall_bpm=(
                self.anti_bpm_spin.value() if self.anti_bpm_check.isChecked() else None
            ),
            anti_don_ticks=self.anti_don_spin.value(),
            anti_kat_ticks=self.anti_kat_spin.value(),
            place_notes=self.place_notes_check.isChecked(),
            mirror_don_lines=self.mirror_don_check.isChecked(),
            mirror_kat_lines=self.mirror_kat_check.isChecked(),
            sv_offset_ms=self.sv_offset_spin.value(),
            red_line_bpm=(
                self.red_bpm_spin.value() if self.red_bpm_check.isChecked() else None
            ),
            red_line_meter=self.red_meter_spin.value(),
            omit_barline=self.omit_barline_check.isChecked(),
            shiny_offset_ms=self.shiny_offset_spin.value(),
            shiny_count=self.shiny_count_spin.value(),
            shiny_bpm_multiplier=self.shiny_bpm_spin.value(),
            shiny_red_line=self.shiny_red_line_check.isChecked(),
            fake_slider_bpm_multiplier=self.fake_slider_bpm_spin.value(),
            restore_bpm=(
                self.restore_bpm_spin.value()
                if self.restore_bpm_check.isChecked() else None
            ),
        )


class ConvertNotesDialog(SheetDialog):
    """Which structure Convert Notes draws over a dragged range, and its numbers.

    Both gimmick layers ask. The fake slider layer has one structure to draw and
    so asks only for its numbers; the barline layer has three -- bars drawn
    around each note, bars taken away where each note is, or a sheet of bars
    whose slits are cut with SV -- and asks which first.

    The numbers are **per call**. They open on the layer's saved Config so the
    common case is one click, and what is typed here is used for this drag and
    forgotten -- a section whose slits want to be wider than the last one's is
    the normal case, not a reason to re-save the layer.
    """

    # This layer's own Don/Kat structure, whichever layer that is.
    STRUCTURE = "structure"
    ANTI = "anti"
    HIDDEN = "hidden"

    def __init__(self, config: GimmickConfig, layer_id: str = "barline", parent=None,
                 start_ms: float | None = None, end_ms: float | None = None,
                 sv_inputs=None, experimental: bool = False) -> None:
        subtitle = tr("MainWindow", "These numbers are for this conversion only; the layer's Config is not changed.")
        if start_ms is not None and end_ms is not None:
            subtitle = tr("MainWindow", "Range: {0} to {1}").format(
                format_time(round(start_ms)), format_time(round(end_ms))) + ". " + subtitle
        title = tr("MainWindow", "Experimental Conversion") if experimental else tr("MainWindow", "Convert notes")
        super().__init__(title, subtitle, action=tr("MainWindow", "Convert"), rail=False, parent=parent)
        # Keep SV (owner, 2026-10-09): (timing points, notes, start, end) to
        # read the chart's own SV from. `experimental` is the whole-map
        # converter: Keep SV always on, and no hidden anti-barline.
        self._sv_inputs = sv_inputs
        self.experimental = experimental
        self._runs: dict[str, list] = {}
        self._table_mode: str | None = None
        self._base = config
        self.layer_id = layer_id
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.restore_button.hide()

        barline = layer_id == "barline"
        sheet = self.sheet
        self.mode_combo = QComboBox()
        if barline:
            # Literal tr() calls, one per option -- see AddViewDialog on the gate.
            self.mode_combo.addItem(tr("MainWindow", "Barline notes"), self.STRUCTURE)
            self.mode_combo.addItem(tr("MainWindow", "Anti-barline"), self.ANTI)
            tiles = [
                (tr("MainWindow", "Barline notes") + "\n" + tr("MainWindow", "Each note drawn out of bars."),
                 _tile_picture(paint_barline_notes, config)),
                (tr("MainWindow", "Anti-barline") + "\n" + tr("MainWindow", "Each note is a slit."),
                 _tile_picture(paint_anti_barline, config)),
            ]
            if not experimental:
                self.mode_combo.addItem(tr("MainWindow", "Hidden anti-barline"), self.HIDDEN)
                tiles.append(
                    (tr("MainWindow", "Hidden anti-barline") + "\n" + tr("MainWindow", "SV opens the slits."),
                     _tile_picture(paint_hidden_anti_barline, config)))
            picker = sheet.field(tr("MainWindow", "Convert to"), bind_tiles(self.mode_combo, tiles))
            sheet.add_widget(picker)
            self._barline_page(config)
            self._anti_page(config)
            if not experimental:
                self._hidden_page(config)
            if sv_inputs is not None:
                self._curves_page()
        else:
            self.mode_combo.addItem(tr("MainWindow", "Fake sliders"), self.STRUCTURE)
            self._fake_slider_page(config)
        self.mode_combo.currentIndexChanged.connect(self._show_mode)

        sheet.set_help_default(
            tr("MainWindow", "What it writes, per note"),
            tr("MainWindow", "Every plain note in the range becomes this structure, on its own millisecond. "
                             "Finishers, drumrolls and fake sliders are left as they are."))
        self._last_good = config
        self.diagram = Diagram(self._paint_diagram)
        sheet.set_diagram(self.diagram, tr("MainWindow", "Preview"))
        for spin in self.findChildren(QAbstractSpinBox):
            spin.valueChanged.connect(self._changed)
        for check in self.findChildren(QCheckBox):
            check.toggled.connect(self._changed)
        self._show_mode()
        self.resize(1000 if barline else 860, 640 if barline else 560)

    # -- pages ---------------------------------------------------------------

    def _show_mode(self, _index: int = 0) -> None:
        keys = {self.mode()}
        if self._curves_wanted():
            keys.add("curves")
            self._fill_curves()
        self.sheet.show_only(keys)
        self._changed()

    # -- Keep SV -------------------------------------------------------------

    def _curve_choices(self) -> list[tuple[str, str]]:
        """What a run can be told it is: Generate SV's curves, by its names,
        and the chart's own step."""
        return [
            (sv_curves.STEP, tr("MainWindow", "Step")),
            ("linear", tr("MainWindow", "Linear")),
            ("sin_in", tr("MainWindow", "Sin In")),
            ("sin_out", tr("MainWindow", "Sin Out")),
            ("exp1.3", tr("MainWindow", "Exp 1.3")),
            ("exp1.6", tr("MainWindow", "Exp 1.6")),
            ("true_exp", tr("MainWindow", "True Exp")),
            ("sin", tr("MainWindow", "Sin")),
        ]

    def _curves_page(self) -> None:
        section = self.sheet.add_section("curves", tr("MainWindow", "Detected SV curves"), tr(
            "MainWindow", "Each run of green lines and the curve it follows. Change one if the guess is wrong."))
        self.curve_table = QTableWidget(0, 4)
        self.curve_table.setHorizontalHeaderLabels([
            tr("MainWindow", "Time"), tr("MainWindow", "SV"), tr("MainWindow", "Curve"), tr("MainWindow", "Notes")])
        self.curve_table.verticalHeader().hide()
        self.curve_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.curve_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.curve_table.horizontalHeader().setStretchLastSection(True)
        self.curve_table.setMinimumHeight(240)
        smooth(self.curve_table)
        section.add(self.curve_table, 2)

    def _curves_wanted(self) -> bool:
        if self._sv_inputs is None or self.mode() == self.HIDDEN:
            return False
        if self.experimental:
            return True
        return self.mode() == self.STRUCTURE and self.keep_sv_check.isChecked()

    def _sv_mode(self) -> str:
        """Barline notes keep a lone jump a step; the anti-barline wall ramps
        every gap the chart changed speed in (owner, 2026-10-09)."""
        return sv_curves.BARLINE if self.mode() == self.STRUCTURE else sv_curves.ANTI

    def runs(self) -> list:
        """The detected runs for the current mode, with any curve the mapper
        corrected. Read once per mode: a whole map takes seconds."""
        mode = self._sv_mode()
        if mode not in self._runs:
            points, notes, start_ms, end_ms = self._sv_inputs
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._runs[mode] = sv_curves.detect_runs(
                    sv_curves.anchors(points, notes, start_ms, end_ms), mode)
            finally:
                QApplication.restoreOverrideCursor()
        return self._runs[mode]

    def _fill_curves(self) -> None:
        mode = self._sv_mode()
        if self._table_mode == mode:
            return
        self._table_mode = mode
        runs = self.runs()
        choices = self._curve_choices()
        table = self.curve_table
        table.setRowCount(len(runs))
        for row, run in enumerate(runs):
            table.setItem(row, 0, QTableWidgetItem(
                f"{format_time(round(run.start.time))} \u2013 {format_time(round(run.end.time))}"))
            table.setItem(row, 1, QTableWidgetItem(
                f"{run.start.value:.2f}\u00d7 \u2192 {run.end.value:.2f}\u00d7"))
            combo = QComboBox()
            for function, label in choices:
                combo.addItem(label, function)
            if combo.findData(run.function) < 0:
                # A fitted "Exp x": its own exponent, as Generate SV writes it.
                combo.addItem(f"Exp {run.function[3:]}", run.function)
            combo.setCurrentIndex(combo.findData(run.function))
            combo.currentIndexChanged.connect(
                lambda _index, run=run, combo=combo: setattr(run, "function", combo.currentData()))
            table.setCellWidget(row, 2, combo)
            table.setItem(row, 3, QTableWidgetItem(str(run.count)))
        table.resizeColumnsToContents()

    def sv_model(self):
        """`sv_curves.SvModel` for this conversion, or None without Keep SV."""
        if not self._curves_wanted():
            return None
        points, notes, _start, _end = self._sv_inputs
        return sv_curves.SvModel(self.runs(), points, notes)

    def _changed(self, *_args) -> None:
        try:
            self._last_good = self.config()
        except GimmickConfigError:
            pass
        self.diagram.refresh()

    def _paint_diagram(self, painter: QPainter, rect: QRect) -> None:
        mode = self.mode()
        if mode == self.ANTI:
            paint_anti_barline(painter, rect, self._last_good)
        elif mode == self.HIDDEN:
            paint_hidden_anti_barline(painter, rect, self._last_good)
        elif self.layer_id == "barline":
            paint_barline_notes(painter, rect, self._last_good)
        else:
            paint_fake_slider_structure(painter, rect, self._last_good)

    def _hide_note_check(self, config: GimmickConfig) -> QCheckBox:
        """The one option both structures share.

        A Don or Kat structure hides the note it is drawn around by putting a
        60000 BPM line on the note's own millisecond -- the note then scrolls
        392px/ms, which is to say it is never on screen, and the bars or the
        fake slider beside it are all the player sees. Unticked, that line
        carries the chart's *own* BPM instead and omits its barline, so it
        changes nothing except restarting measure counting: the note stays
        visible and the structure is drawn around a note you can see.
        """
        check = QCheckBox(tr("MainWindow", "Hide the note itself"))
        check.setChecked(config.hide_note)
        return check

    def _hide_note_help(self) -> str:
        return tr(
            "MainWindow",
            "On, the note is squashed to invisibility by a gimmick-BPM line and only "
            "the structure is seen. Off, that line carries the chart's own BPM with "
            "its barline detached, so the note stays visible inside the structure.",
        )

    def _slit_note_hint(self) -> str:
        """The extra half of the story on a converter whose structure is a hole.

        Everywhere else the option is "structure with or without a visible note
        in it". Here the invisible note is what makes the gap read as an object
        at all, so showing it changes what the section says rather than only how
        it looks -- worth one line, not worth refusing the option over.
        """
        return tr(
            "MainWindow",
            "Showing the note changes what this gimmick reads as: the slit is a hole "
            "in a sheet of bars, and an invisible note is what makes that hole the "
            "object. Shown, the note travels through its own slit at the chart's "
            "normal speed while the sheet crawls at the wall speed.",
        )

    def _barline_page(self, config: GimmickConfig) -> None:
        section = self.sheet.add_section(self.STRUCTURE, tr("MainWindow", "Barline notes"),
                                         tr("MainWindow", "Each note drawn out of bars"))
        self.bpm_spin = QDoubleSpinBox()
        self.bpm_spin.setRange(1.0, 1000000.0)
        self.bpm_spin.setDecimals(0)
        self.bpm_spin.setSuffix(" BPM")
        self.bpm_spin.setValue(config.gimmick_bpm)
        section.field(tr("MainWindow", "Invisible note BPM"), self.bpm_spin, tr(
            "MainWindow", "The red line on each note's own millisecond, which carries the note off screen."))

        self.spacing_spin = QSpinBox()
        self.spacing_spin.setRange(1, 1000)
        self.spacing_spin.setSuffix(" ms")
        self.spacing_spin.setValue(config.spacing_ms)
        section.field(tr("MainWindow", "Don spacing"), self.spacing_spin, tr(
            "MainWindow", "How far from the note each Don bar sits."))

        # Three boxes, three labels, one loop -- but the labels are separate
        # tr() literals for the same reason every other option list here is:
        # the catalog is scanned for literals, not built at runtime.
        self.kat_spins = []
        labels = (tr("MainWindow", "Kat spacing 1"), tr("MainWindow", "Kat spacing 2"),
                  tr("MainWindow", "Kat spacing 3"))
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(12)
        for label, value in zip(labels, (
            config.kat_spacing1_ms, config.kat_spacing2_ms, config.kat_spacing3_ms,
        )):
            spin = QSpinBox()
            spin.setRange(1, 1000)
            spin.setSuffix(" ms")
            spin.setValue(value)
            self.kat_spins.append(spin)
            row_layout.addWidget(self.sheet.field(label, spin, tr(
                "MainWindow", "Kat's three bars. The three must all differ.")), 1)
        section.add(row, 2)
        self.mirror_don_check = QCheckBox(tr("MainWindow", "Mirror Don bars"))
        self.mirror_don_check.setChecked(config.mirror_don_lines)
        section.switch(self.mirror_don_check, tr("MainWindow", "On both sides of the note."), span=1)
        self.mirror_kat_check = QCheckBox(tr("MainWindow", "Mirror Kat bars"))
        self.mirror_kat_check.setChecked(config.mirror_kat_lines)
        section.switch(self.mirror_kat_check, tr("MainWindow", "On both sides of the note."), span=1)

        self.hide_note_check = self._hide_note_check(config)
        section.switch(self.hide_note_check, tr("MainWindow", "Off keeps the note visible inside its bars."),
                       self._hide_note_help(), span=1)
        self.omit_note_barline_check = QCheckBox(tr("MainWindow", "Omit barline at each note"))
        self.omit_note_barline_check.setChecked(config.omit_note_barline)
        # A shown note's line already omits its barline, so the box only
        # means something while the note is hidden.
        self.omit_note_barline_check.setEnabled(self.hide_note_check.isChecked())
        self.hide_note_check.toggled.connect(self.omit_note_barline_check.setEnabled)
        section.switch(self.omit_note_barline_check, tr("MainWindow", "The note's own line draws no bar."), span=1)
        # Per selection, Barline notes only (owner, 2026-10-09). The whole-map
        # converter keeps SV always, so it shows no switch for it.
        self.keep_sv_check = QCheckBox(tr("MainWindow", "Keep SV"))
        if self._sv_inputs is not None and not self.experimental:
            section.switch(self.keep_sv_check, tr(
                "MainWindow", "Every line the bars add gets the chart's own SV back, following its curves."), span=1)
            self.keep_sv_check.toggled.connect(self._show_mode)

    def _fake_slider_page(self, config: GimmickConfig) -> None:
        section = self.sheet.add_section(self.STRUCTURE, tr("MainWindow", "Fake sliders"),
                                         tr("MainWindow", "Each note becomes a fake slider in flight"))
        self.bpm_spin = QDoubleSpinBox()
        self.bpm_spin.setRange(1.0, 1000000.0)
        self.bpm_spin.setDecimals(0)
        self.bpm_spin.setSuffix(" BPM")
        self.bpm_spin.setValue(config.gimmick_bpm)
        section.field(tr("MainWindow", "Invisible note BPM"), self.bpm_spin, tr(
            "MainWindow", "The red line on each note's own millisecond, which carries the note off screen."))

        self.fake_offset_spin = QSpinBox()
        self.fake_offset_spin.setRange(1, 1000)
        self.fake_offset_spin.setSuffix(" ms")
        self.fake_offset_spin.setValue(config.fake_slider_offset_ms)
        section.field(tr("MainWindow", "Fake slider offset"), self.fake_offset_spin, tr(
            "MainWindow", "How far after the note the fake slider sits."))

        self.length_spin = QDoubleSpinBox()
        # The config's own bound, so the box cannot ask for a length
        # GimmickConfig would reject -- see GimmickConfigDialog.
        self.length_spin.setRange(-100000.0, FAKE_SLIDER_MAX_LENGTH)
        self.length_spin.setDecimals(4)
        self.length_spin.setValue(config.fake_slider_length)
        section.field(tr("MainWindow", "Fake slider length"), self.length_spin, tr(
            "MainWindow", "Anything longer than 0.001 is a real, hittable drumroll."))

        self.fake_sv_spin = TrimmedDoubleSpinBox()
        self.fake_sv_spin.setRange(0.01, 100.0)
        self.fake_sv_spin.setDecimals(SV_DECIMALS)
        self.fake_sv_spin.setSuffix("\u00d7")
        self.fake_sv_spin.setValue(config.fake_slider_sv)
        section.field(tr("MainWindow", "Gimmick SV"), self.fake_sv_spin, tr(
            "MainWindow",
            "With the note hidden the gimmick SV above is what takes the already "
            "squashed note the rest of the way off screen. Showing the note drops it "
            "and restates the chart's own speed instead, or the note would be flung "
            "off the screen the line was just told to keep it on.",
        ))

        self.fake_slider_bpm_spin = QDoubleSpinBox()
        self.fake_slider_bpm_spin.setRange(0.001, 1000.0)
        self.fake_slider_bpm_spin.setDecimals(3)
        self.fake_slider_bpm_spin.setSuffix("\u00d7")
        self.fake_slider_bpm_spin.setValue(config.fake_slider_bpm_multiplier)
        section.field(tr("MainWindow", "Red line BPM"), self.fake_slider_bpm_spin, tr(
            "MainWindow", "The slider's own red line as a multiple of the chart's BPM. 1\u00d7 restates the BPM in force."))

        self.omit_barline_check = QCheckBox(tr("MainWindow", "Omit barline"))
        self.omit_barline_check.setChecked(config.omit_barline)
        section.switch(self.omit_barline_check, tr(
            "MainWindow", "The slider's 60000 BPM line would otherwise stamp a bar nobody asked for."), span=1)
        self.hide_note_check = self._hide_note_check(config)
        section.switch(self.hide_note_check, tr("MainWindow", "Off keeps the note visible beside its slider."),
                       self._hide_note_help(), span=1)

    def _anti_page(self, config: GimmickConfig) -> None:
        section = self.sheet.add_section(self.ANTI, tr("MainWindow", "Anti-barline"), tr(
            "MainWindow", "Only plain circles convert; finishers, drumrolls and spinners are left as they are"))
        self.anti_density_spin = QSpinBox()
        self.anti_density_spin.setRange(1, 10000)
        self.anti_density_spin.setValue(config.anti_lines_per_beat)
        section.field(tr("MainWindow", "Lines per beat"), self.anti_density_spin, tr(
            "MainWindow", "How tight the wall is. The reference section (mekurume) uses 72."))

        self.anti_don_spin = QSpinBox()
        self.anti_don_spin.setRange(1, 1000)
        self.anti_don_spin.setSuffix(" " + tr("MainWindow", "ticks"))
        self.anti_don_spin.setValue(config.anti_don_ticks)
        section.field(tr("MainWindow", "Don slit"), self.anti_don_spin, tr(
            "MainWindow",
            "Wall lines left out for a Don. The slit width is what tells Don from "
            "Kat, since the notes themselves are invisible."))

        # Same shape as the Config dialog's: the chart's own BPM at each wall
        # line only moves where the bars fall.
        self.anti_bpm_check = QCheckBox(tr("MainWindow", "Custom"))
        self.anti_bpm_check.setChecked(config.anti_wall_bpm is not None)
        self.anti_bpm_spin = QDoubleSpinBox()
        self.anti_bpm_spin.setRange(0.001, 1000000.0)
        self.anti_bpm_spin.setDecimals(3)
        self.anti_bpm_spin.setSuffix(" BPM")
        self.anti_bpm_spin.setValue(
            config.anti_wall_bpm if config.anti_wall_bpm is not None else DEFAULT_RED_LINE_BPM
        )
        section.field(tr("MainWindow", "Wall BPM"), own_or_custom(
            self.anti_bpm_check, self.anti_bpm_spin, tr("MainWindow", "Chart's own"), tr("MainWindow", "Custom")),
            tr("MainWindow",
               "Below the chart's own BPM the wall packs tighter: a red line's BPM is "
               "also its scroll speed, so a third of the BPM draws the same lines a "
               "third as far apart."), span=2, scrub=self.anti_bpm_spin)

        self.anti_kat_spin = QSpinBox()
        self.anti_kat_spin.setRange(1, 1000)
        self.anti_kat_spin.setSuffix(" " + tr("MainWindow", "ticks"))
        self.anti_kat_spin.setValue(config.anti_kat_ticks)
        section.field(tr("MainWindow", "Kat slit"), self.anti_kat_spin, tr(
            "MainWindow", "Wall lines left out for a Kat. Wider than Don's, or the two cannot be told apart."))
        self.anti_hide_note_check = self._hide_note_check(config)
        section.switch(self.anti_hide_note_check, tr("MainWindow", "Off shows the note inside its slit."),
                       self._slit_note_hint(), span=1)

    def _hidden_page(self, config: GimmickConfig) -> None:
        section = self.sheet.add_section(self.HIDDEN, tr("MainWindow", "Hidden anti-barline"), tr(
            "MainWindow", "The slit is a percentage of speed: wide far away, closed at the hit position"))
        self.hidden_hide_bpm_spin = QDoubleSpinBox()
        self.hidden_hide_bpm_spin.setRange(1.0, 1000000.0)
        self.hidden_hide_bpm_spin.setDecimals(3)
        self.hidden_hide_bpm_spin.setSuffix(" BPM")
        self.hidden_hide_bpm_spin.setValue(config.hidden_hide_bpm)
        section.field(tr("MainWindow", "Hide BPM"), self.hidden_hide_bpm_spin, tr(
            "MainWindow", "The red line that carries each note off screen."))

        self.hidden_wall_bpm_spin = QDoubleSpinBox()
        self.hidden_wall_bpm_spin.setRange(1.0, 1000000.0)
        self.hidden_wall_bpm_spin.setDecimals(3)
        self.hidden_wall_bpm_spin.setSuffix(" BPM")
        self.hidden_wall_bpm_spin.setValue(config.hidden_wall_bpm)
        section.field(tr("MainWindow", "Wall BPM"), self.hidden_wall_bpm_spin, tr(
            "MainWindow", "The meter-1 red line that makes osu! draw the whole sheet."))

        # The floor is not the SV editor's 0.01: this whole gimmick lives at
        # about that value and the differences carrying the colour are several
        # decimals below it. SV_DECIMALS is what makes them typeable at all.
        sv_help = tr(
            "MainWindow",
            "Caution: the three SV values are the gimmick, and they are meant to sit "
            "within about a percent of each other. Wall SV decides how tight the sheet "
            "is; Don and Kat only read as a colour while both are above it and Kat is "
            "the larger. Ordinary-looking values will not read as this gimmick at all.",
        )
        self.hidden_base_sv_spin = self._sv_spin(config.hidden_base_sv)
        section.field(tr("MainWindow", "Wall SV"), self.hidden_base_sv_spin, sv_help)
        self.hidden_window_spin = QSpinBox()
        self.hidden_window_spin.setRange(1, 64)
        self.hidden_window_spin.setPrefix("1/")
        self.hidden_window_spin.setValue(config.hidden_window_divisor)
        section.field(tr("MainWindow", "Slit length"), self.hidden_window_spin, tr(
            "MainWindow",
            "One wall line makes osu! draw the whole sheet, and each note's slit is "
            "opened by raising SV for a fraction of a beat after it. Taiko places an "
            "object at its distance times the speed at its own time, so the raised "
            "bars sit ahead of the rest by that excess times how far away they still "
            "are -- the slit opens with distance and closes at the hit position.",
        ))
        self.hidden_don_sv_spin = self._sv_spin(config.hidden_don_sv)
        section.field(tr("MainWindow", "Don SV"), self.hidden_don_sv_spin, sv_help)
        self.hidden_kat_sv_spin = self._sv_spin(config.hidden_kat_sv)
        section.field(tr("MainWindow", "Kat SV"), self.hidden_kat_sv_spin, sv_help)
        self.hidden_hide_note_check = self._hide_note_check(config)
        section.switch(self.hidden_hide_note_check, tr("MainWindow", "Off shows the note inside its slit."),
                       self._slit_note_hint(), span=1)

    @staticmethod
    def _sv_spin(value: float) -> QDoubleSpinBox:
        spin = TrimmedDoubleSpinBox()
        spin.setRange(10.0 ** -SV_DECIMALS, 100.0)
        spin.setDecimals(SV_DECIMALS)
        spin.setSingleStep(0.0001)
        spin.setSuffix("\u00d7")
        spin.setValue(value)
        return spin

    # -- result --------------------------------------------------------------

    def mode(self) -> str:
        return self.mode_combo.currentData()

    def config(self) -> GimmickConfig:
        """The layer's config with this call's edits on it.

        Only the active page's fields are applied. The rest ride through
        untouched, so choosing a converter cannot quietly rewrite the numbers
        another one uses.
        """
        mode = self.mode()
        if mode == self.ANTI:
            return replace(
                self._base,
                anti_lines_per_beat=self.anti_density_spin.value(),
                anti_wall_bpm=(
                    self.anti_bpm_spin.value() if self.anti_bpm_check.isChecked() else None
                ),
                anti_don_ticks=self.anti_don_spin.value(),
                anti_kat_ticks=self.anti_kat_spin.value(),
                hide_note=self.anti_hide_note_check.isChecked(),
            )
        if mode == self.HIDDEN:
            return replace(
                self._base,
                hidden_hide_bpm=self.hidden_hide_bpm_spin.value(),
                hidden_wall_bpm=self.hidden_wall_bpm_spin.value(),
                hidden_base_sv=self.hidden_base_sv_spin.value(),
                hidden_don_sv=self.hidden_don_sv_spin.value(),
                hidden_kat_sv=self.hidden_kat_sv_spin.value(),
                hidden_window_divisor=self.hidden_window_spin.value(),
                hide_note=self.hidden_hide_note_check.isChecked(),
            )
        if self.layer_id == "barline":
            return replace(
                self._base,
                gimmick_bpm=self.bpm_spin.value(),
                spacing_ms=self.spacing_spin.value(),
                kat_spacing1_ms=self.kat_spins[0].value(),
                kat_spacing2_ms=self.kat_spins[1].value(),
                kat_spacing3_ms=self.kat_spins[2].value(),
                mirror_don_lines=self.mirror_don_check.isChecked(),
                mirror_kat_lines=self.mirror_kat_check.isChecked(),
                hide_note=self.hide_note_check.isChecked(),
                omit_note_barline=self.omit_note_barline_check.isChecked(),
            )
        return replace(
            self._base,
            gimmick_bpm=self.bpm_spin.value(),
            fake_slider_offset_ms=self.fake_offset_spin.value(),
            fake_slider_length=self.length_spin.value(),
            fake_slider_sv=self.fake_sv_spin.value(),
            fake_slider_bpm_multiplier=self.fake_slider_bpm_spin.value(),
            omit_barline=self.omit_barline_check.isChecked(),
            hide_note=self.hide_note_check.isChecked(),
        )

    def accept(self) -> None:
        # The three Kat spacings have no shared range to keep them distinct, so
        # like the Config dialog this one combination is checked before the
        # dialog closes rather than raising in the caller with nothing focused.
        try:
            self.config()
        except GimmickConfigError as error:
            QMessageBox.warning(self, tr("MainWindow", "Cannot convert with this"), str(error))
            return
        super().accept()


class BarlineFunctionDialog(SheetDialog):
    """Fill a dragged range with red lines, every `n` ms or every `n` snaps.

    The barline layer's answer to the SV editor's function tool. Two rhythms,
    because a run of barlines is one of two different things:

    * **Every n (ms)** -- scenery. n = 1 is the default because a barline
      gimmick's whole vocabulary is lines packed as tightly as the format
      allows, and coarser runs are what you tune it up to, not down from.
    * **Every n snaps** -- rhythm. Walks the chart's own beat grid at the chosen
      divisor, so the bars land where the music does rather than on a
      millisecond count that drifts against it.

    The snap walk uses the base timing snapshot, not the document: the document
    is the file being filled with 60000 BPM lines, and stepping along *those*
    would collapse the run into the first few milliseconds.
    """

    def __init__(
        self, start_ms: float, end_ms: float, parent=None,
        base_timing: list[TimingPoint] | None = None, snap_divisor: int = 4,
        note_times: set[int] | None = None, current_sv: float = 1.0, meter: int = 4,
    ) -> None:
        super().__init__(
            self._title(),
            tr("MainWindow", "Range: {0} to {1}").format(format_time(round(start_ms)), format_time(round(end_ms))),
            action=tr("MainWindow", "Generate"), rail=False, restore=False, parent=parent)
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.base_timing = base_timing or []
        self.note_times = note_times or set()
        sheet = self.sheet
        # Rows are sheet fields; set_row_visible finds them by property.
        self._form_layout = None

        self.mode_combo = QComboBox()
        # Literal tr() calls, one per option -- see AddViewDialog on why a dict
        # lookup here would escape the i18n coverage gate.
        self.mode_combo.addItem(tr("MainWindow", "Every n (ms)"), "ms")
        self.mode_combo.addItem(tr("MainWindow", "Every n snaps"), "snaps")
        for text, data in self._extra_modes():
            self.mode_combo.addItem(text, data)
        where = sheet.add_section("where", tr("MainWindow", "Where"))
        pills = bind_segments(self.mode_combo)
        where.field(tr("MainWindow", "Generate at"), pills, tr(
            "MainWindow",
            "Every n ms is scenery: a barline gimmick's vocabulary is lines packed as tight as "
            "the format allows. Every n snaps walks the chart's own beat grid, so the lines land "
            "where the music does."), span=2)

        self.spacing_spin = QSpinBox()
        self.spacing_spin.setRange(1, 10000)
        self.spacing_spin.setSuffix(" ms")
        self.spacing_spin.setValue(1)
        where.field(tr("MainWindow", "Every"), self.spacing_spin, tr(
            "MainWindow", "One line every n milliseconds across the range."))

        self.snap_count_spin = QSpinBox()
        self.snap_count_spin.setRange(1, 64)
        self.snap_count_spin.setSuffix(" " + tr("MainWindow", "snaps"))
        self.snap_count_spin.setValue(1)
        where.field(tr("MainWindow", "Every"), self.snap_count_spin, tr(
            "MainWindow", "One line every n gridlines of the snap."))

        self.snap_combo = QComboBox()
        for divisor in SNAP_DIVISORS:
            self.snap_combo.addItem(f"1/{divisor}", divisor)
        found = self.snap_combo.findData(int(snap_divisor))
        self.snap_combo.setCurrentIndex(found if found >= 0 else self.snap_combo.findData(4))
        where.field(tr("MainWindow", "Snap"), self.snap_combo, tr(
            "MainWindow", "The gridline spacing the snaps are counted in."))

        # Shifts where the walk itself begins -- the first "every n (ms)" line,
        # or (in snap mode) the anchor `_snap_times` snaps to before it starts
        # counting divisions. That anchor is what "every n snaps" counts from,
        # so this is a phase shift of *which* grid lines get chosen, not a
        # shift of where the chosen ones land.
        self.start_offset_spin = QSpinBox()
        self.start_offset_spin.setRange(-10000, 10000)
        self.start_offset_spin.setSuffix(" ms")
        self.start_offset_spin.setValue(0)
        where.field(tr("MainWindow", "Starting offset"), self.start_offset_spin, tr(
            "MainWindow",
            "Moves where the walk starts counting from. In snap mode this changes which "
            "gridlines are picked, not where the picked ones land."))

        # Shifts every already-selected line by a fixed amount, after the walk
        # (or the note filter) has picked which milliseconds those are. A
        # snap-mode run wants this far more often than a starting offset: it
        # is what puts a red line consistently early or late of the beat
        # itself rather than of wherever the range happened to start.
        self.snap_offset_spin = QSpinBox()
        self.snap_offset_spin.setRange(-10000, 10000)
        self.snap_offset_spin.setSuffix(" ms")
        self.snap_offset_spin.setValue(0)
        where.field(tr("MainWindow", "Snap offset"), self.snap_offset_spin, tr(
            "MainWindow",
            "Moves every picked line by the same amount: consistently early or late of the beat."))

        # Off by default: a red line landing on a note's own millisecond resets
        # SV to 1.0x for that note and restarts measure counting under it, which
        # is a gimmick in its own right rather than scenery. Available, because
        # doing it deliberately is a real technique.
        self.allow_on_notes_check = QCheckBox(tr("MainWindow", "Allow on existing notes"))
        self.allow_on_notes_check.setChecked(False)
        self.allow_on_notes_check.toggled.connect(self._update_count)
        where.switch(self.allow_on_notes_check, tr(
            "MainWindow", "Off skips any millisecond that already has a note."))

        # A run of red lines at the chart's own BPM is scenery standing still.
        # Ramping it across the range is what makes the bars accelerate, and the
        # growth functions are the SV generator's own (`sv_ease`), so a barline
        # sweep and an SV sweep shaped "Exp 1.6" mean the same curve.
        # Literal tr() calls, one per option -- see AddViewDialog on why a dict
        # lookup here would escape the i18n coverage gate.
        bpm = sheet.add_section("bpm", tr("MainWindow", "BPM"))
        self._bpm_section = bpm
        self.bpm_curve_combo = QComboBox()
        self.bpm_curve_combo.addItem(tr("MainWindow", "Base timing BPM"), "none")
        self.bpm_curve_combo.addItem(tr("MainWindow", "Linear"), "linear")
        self.bpm_curve_combo.addItem(tr("MainWindow", "Exp 1.3"), "exp1.3")
        self.bpm_curve_combo.addItem(tr("MainWindow", "Exp 1.6"), "exp1.6")
        self.bpm_curve_combo.addItem(tr("MainWindow", "True Exp"), "true_exp")
        self.bpm_curve_combo.currentIndexChanged.connect(self._update_mode)
        curve_pills = bind_segments(self.bpm_curve_combo)
        bpm.field(tr("MainWindow", "BPM growth"), curve_pills, tr(
            "MainWindow",
            "Base timing BPM writes the chart's own BPM: the lines change where the bars fall "
            "and nothing else. A curve ramps the BPM from Start to End across the range, which "
            "is what makes the bars accelerate."), span=2)
        self.bpm_curve_combo.setProperty("sheetField", curve_pills.property("sheetField"))

        start_bpm = base_bpm_at(self.base_timing, start_ms) if self.base_timing else 120.0
        self.start_bpm_spin = QDoubleSpinBox()
        self.start_bpm_spin.setRange(1.0, 1000000.0)
        self.start_bpm_spin.setDecimals(4)
        self.start_bpm_spin.setSuffix(" BPM")
        self.start_bpm_spin.setValue(start_bpm)
        bpm.field(tr("MainWindow", "Start BPM"), self.start_bpm_spin, tr(
            "MainWindow", "The first line's BPM."))

        self.end_bpm_spin = QDoubleSpinBox()
        self.end_bpm_spin.setRange(1.0, 1000000.0)
        self.end_bpm_spin.setDecimals(4)
        self.end_bpm_spin.setSuffix(" BPM")
        self.end_bpm_spin.setValue(start_bpm * 2)
        bpm.field(tr("MainWindow", "End BPM"), self.end_bpm_spin, tr(
            "MainWindow", "The last line's BPM."))

        # Same reason as the Red Line tool's Config row: osu! takes meter from
        # the uninherited point in force, so every line in the run re-bars what
        # follows it. Seeded from that Config so the two tools agree.
        self.meter_spin = QSpinBox()
        self.meter_spin.setRange(1, 10000)
        self.meter_spin.setValue(int(meter))
        bpm.field(tr("MainWindow", "Meter"), self.meter_spin, tr(
            "MainWindow",
            "Beats per bar for each line. osu! reads meter from the red line in force, so every "
            "line in the run re-bars what follows it."))

        # Every uninherited point resets SV to 1.0x, so a run of red lines
        # silently flattens the chart's scroll speed for its whole length
        # unless a green line goes down with each one. "Current speed" writes
        # exactly the SV already in force at each millisecond -- the run then
        # changes where the bars fall and nothing else, which is what you
        # almost always want and why it is the default. A typed value drives
        # the whole run at one speed instead.
        # Literal tr() calls, one per option -- see AddViewDialog on the gate.
        speed = sheet.add_section("speed", tr("MainWindow", "Speed"))
        self.sv_mode_combo = QComboBox()
        self.sv_mode_combo.addItem(tr("MainWindow", "Current speed"), "current")
        self.sv_mode_combo.addItem(tr("MainWindow", "Custom"), "custom")
        self.sv_mode_combo.currentIndexChanged.connect(self._update_mode)
        speed.field(tr("MainWindow", "SV"), bind_segments(self.sv_mode_combo), tr(
            "MainWindow",
            "Every red line resets SV to 1\u00d7, so each gets a green line too. Current speed "
            "writes the SV already in force there; Custom drives the whole run at one speed."))

        self.sv_spin = TrimmedDoubleSpinBox()
        self.sv_spin.setRange(0.01, 100.0)
        self.sv_spin.setDecimals(SV_DECIMALS)
        self.sv_spin.setSingleStep(0.05)
        self.sv_spin.setSuffix("\u00d7")
        self.sv_spin.setValue(float(current_sv))
        speed.field(tr("MainWindow", "SV multiplier"), self.sv_spin, tr(
            "MainWindow", "The speed every line in the run carries."))

        # The count Generate will write, in the footer where the action is.
        self.count_label = self.summary
        sheet.set_help_default(
            tr("MainWindow", "What Generate writes"),
            tr("MainWindow", "Each tick above is one line in the range, drawn over the notes already there."))
        self.run_diagram = Diagram(self._paint_run, 110)
        sheet.set_diagram(self.run_diagram, tr("MainWindow", "Preview"))
        for widget in (
            self.spacing_spin, self.snap_count_spin,
            self.start_offset_spin, self.snap_offset_spin,
        ):
            widget.valueChanged.connect(self._update_count)
        self.snap_combo.currentIndexChanged.connect(self._update_count)
        self.mode_combo.currentIndexChanged.connect(self._update_mode)
        self._update_mode()
        self.resize(900, 600)

    # The preview's tick colour: what one line of the run is.
    RUN_COLOR = "#ff5a5a"

    def _title(self) -> str:
        return tr("MainWindow", "Generate red lines")

    def _extra_modes(self) -> list[tuple[str, str]]:
        """Rhythms a subclass adds to Generate at, before its pills are drawn."""
        return []

    def _paint_run(self, painter: QPainter, rect: QRect) -> None:
        """Where the run lands across the range, over the notes in it."""
        span = max(1.0, self.end_ms - self.start_ms)
        left, right = rect.left() + 10, rect.right() - 10

        def x_of(at: float) -> float:
            return left + (at - self.start_ms) / span * (right - left)

        base = rect.top() + rect.height() * 0.62
        painter.setPen(QPen(_DIAGRAM_RULE, 1))
        painter.drawLine(QPointF(left, base), QPointF(right, base))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*DON_COLOR))
        for at in self.note_times:
            if self.start_ms <= at <= self.end_ms:
                painter.drawEllipse(QPointF(x_of(at), base), 3.5, 3.5)
        times = self.times()
        painter.setPen(QPen(QColor(self.RUN_COLOR), 1))
        for at in times[:4000]:
            x = x_of(at)
            painter.drawLine(QPointF(x, rect.top() + 12), QPointF(x, base - 8))
        _diagram_text(painter, left, rect.bottom() - 6, format_time(round(self.start_ms)))
        _diagram_text(painter, right, rect.bottom() - 6, format_time(round(self.end_ms)), align=Qt.AlignRight)

    def _update_mode(self) -> None:
        """Show only the rhythm the chosen mode actually uses.

        setRowVisible collapses the whole form row; hiding the widget alone
        would leave its label and blank space behind.
        """
        mode = str(self.mode_combo.currentData())
        snaps = mode == "snaps"
        set_row_visible(self._form_layout, self.spacing_spin, mode == "ms")
        set_row_visible(self._form_layout, self.snap_count_spin, snaps)
        set_row_visible(self._form_layout, self.snap_combo, snaps)
        # Starting offset shifts an anchor "notes" mode does not have -- the
        # chart's own notes are picked by their fixed positions in range, not
        # walked from one. Snap offset shifts each already-picked line, which
        # a flat "every n (ms)" run has no separate use for: sliding every
        # line by the same amount there is just a different starting offset.
        set_row_visible(self._form_layout, self.start_offset_spin, mode != "notes")
        set_row_visible(self._form_layout, self.snap_offset_spin, mode != "ms")
        ramping = str(self.bpm_curve_combo.currentData()) != "none"
        set_row_visible(self._form_layout, self.start_bpm_spin, ramping)
        set_row_visible(self._form_layout, self.end_bpm_spin, ramping)
        set_row_visible(
            self._form_layout, self.sv_spin, self.sv_multiplier() is not None
        )
        self._update_count()

    def sv_multiplier(self) -> float | None:
        """The SV every generated line carries, or None for "current speed".

        None rather than a number, because "current" is a different value at
        every millisecond in the run once the chart has SV of its own -- the
        caller reads it per line.
        """
        if str(self.sv_mode_combo.currentData()) == "custom":
            return float(self.sv_spin.value())
        return None

    def _update_count(self) -> None:
        """Say how many lines Ok will write. At n = 1 a careless drag is
        thousands of them, and the number is the only warning worth giving."""
        count = len(self.times())
        self.set_summary(tr("MainWindow", "{0} red lines").format(count), bad=count == 0)
        self.action_button.setEnabled(count > 0)
        self.run_diagram.refresh()

    def times(self) -> list[int]:
        """Every millisecond a line lands on, in order.

        Bounded by the dragged range rather than by a typed cap: how many lines
        a gimmick wants is the mapper's call, and the selection is where they
        already said how far it goes.

        Two offsets, not one, because they answer different questions.
        Starting offset moves the anchor the walk counts *from* -- in snap
        mode that is a phase shift, changing which grid lines "every n snaps"
        picks, not just where the picked ones land. Snap offset moves each
        already-picked line by a fixed amount afterward, which is what puts a
        run consistently early or late of the beat rather than of wherever
        the range started.
        """
        start_offset = self.start_offset_spin.value()
        snap_offset = self.snap_offset_spin.value()
        last = round(self.end_ms)
        mode = str(self.mode_combo.currentData())
        if mode == "notes":
            # The chart's own notes inside the range. Offered only where a
            # subclass adds the option (FakeSliderFunctionDialog): a run of red
            # lines has no reason to land on notes, and refuses to by default.
            # Fixed positions, not a walk -- there is no anchor for a starting
            # offset to shift, so only the snap offset reaches this mode.
            return sorted(
                at + snap_offset for at in self.note_times
                if round(self.start_ms) <= at <= last
            )
        if mode == "snaps":
            times = [
                at + snap_offset for at in self._snap_times(start_offset)
                if at + snap_offset <= last
            ]
        else:
            first = osu_snap_ms(self.start_ms) + start_offset
            times = list(range(first, last + 1, self.spacing_spin.value())) if first <= last else []
        if self.allow_on_notes_check.isChecked() or not self.note_times:
            return times
        return [at for at in times if at not in self.note_times]

    def bpms(self, times: list[int]) -> list[float]:
        """The BPM each line in `times` carries, in the same order.

        The base timing's own BPM by default -- a line that changes nothing but
        where the bars fall. With a growth function chosen, a ramp from Start to
        End BPM eased across the range instead, which is what makes a run of
        bars accelerate rather than just repeat.

        Progress is measured in *time*, not in line index, so the curve is the
        same shape whether the run is packed every millisecond or spread over
        the beat grid.
        """
        function_id = str(self.bpm_curve_combo.currentData())
        if function_id == "none" or not times:
            return [base_bpm_at(self.base_timing, at) for at in times]
        start_bpm = self.start_bpm_spin.value()
        end_bpm = self.end_bpm_spin.value()
        span = times[-1] - times[0]
        return [
            start_bpm + (end_bpm - start_bpm) * sv_ease(
                function_id, 0.0 if span <= 0 else (at - times[0]) / span,
                start_bpm, end_bpm,
            )
            for at in times
        ]

    def _snap_times(self, start_offset: int = 0) -> list[int]:
        """The beat grid across the range, every `n` divisions.

        Stepped one division at a time and then taken every `n`th rather than
        multiplied out, because beat length can change mid-range: a BPM section
        starting inside the selection has to re-anchor the walk, which a single
        multiplication cannot express.

        `start_offset` moves the anchor the walk snaps to before counting
        divisions -- a phase shift of which grid lines "every n" lands on,
        not a shift of the range itself. `end_ms` stays put regardless: the
        dragged range's own end is still the hard bound.
        """
        if not self.base_timing:
            return []
        divisor = int(self.snap_combo.currentData())
        every = self.snap_count_spin.value()
        anchor = self.start_ms + start_offset
        cursor = snap_time(self.base_timing, anchor, divisor)
        if cursor < anchor - 0.001:
            cursor += active_uninherited_at(self.base_timing, cursor).beat_length / divisor
        times: list[int] = []
        index = 0
        # A pathological beat length would otherwise walk forever; the SV
        # generator caps its own walk at the same order of magnitude.
        while cursor <= self.end_ms + 0.001 and len(times) < 20001:
            if index % every == 0:
                # osu_snap_ms, not round: a beat position commits to a whole
                # millisecond by truncating, same as every other snapped
                # gridline in the app (time_axis.osu_snap_ms). round()'s
                # banker's rounding sent adjacent grid lines up or down
                # depending on which side of a half-millisecond they happened
                # to fall on -- invisible on an exact-dividing beat length,
                # but on a real one it made a fixed snap offset land 1ms off
                # depending on which snap it was relative to.
                times.append(osu_snap_ms(cursor))
            step = active_uninherited_at(self.base_timing, cursor + 0.001).beat_length / divisor
            if step <= 0:
                break
            cursor += step
            index += 1
        return times


class FakeSliderFunctionDialog(BarlineFunctionDialog):
    """Fill a dragged range with fake sliders, or with shiny notes.

    A subclass rather than a second dialog: the two rhythms ("every n ms" for
    scenery, "every n snaps" for something the music can be read against), the
    offset, and the walk along the base timing grid are the same question the
    barline generator already asks, and answering it twice in two places is how
    the two drifted apart last time. What differs is what lands on each
    millisecond, which is the two rows this adds -- and the BPM ramp, which
    belongs to red lines and is hidden.
    """

    def __init__(
        self, start_ms: float, end_ms: float, parent=None,
        base_timing: list[TimingPoint] | None = None, snap_divisor: int = 4,
        shiny_count: int = DEFAULT_SHINY_COUNT,
        shiny_bpm_multiplier: float = 1.0, fake_slider_bpm_multiplier: float = 1.0,
        note_times: set[int] | None = None,
    ) -> None:
        self._subclass_ready = False
        super().__init__(
            start_ms, end_ms, parent, base_timing=base_timing, snap_divisor=snap_divisor,
            note_times=note_times,
        )

        # Each object kind keeps its own remembered multiplier -- switching
        # the combo below is a change of *which* object this run builds, not
        # an edit to either number, so it must not clobber the other kind's
        # setting the way a single shared value would.
        self._kind_multipliers = {
            "shiny": float(shiny_bpm_multiplier), "regular": float(fake_slider_bpm_multiplier),
        }

        # Literal tr() calls, one per option -- see AddViewDialog on why a dict
        # lookup here would escape the i18n coverage gate.
        self.object_combo = QComboBox()
        self.object_combo.addItem(tr("MainWindow", "Fake slider"), "regular")
        self.object_combo.addItem(tr("MainWindow", "Shiny"), "shiny")
        self.object_combo.currentIndexChanged.connect(self._object_kind_changed)

        self.shiny_count_spin = QSpinBox()
        self.shiny_count_spin.setRange(1, 50)
        self.shiny_count_spin.setValue(int(shiny_count))

        # A red line's BPM is also its scroll speed, so retiming a structure's
        # own governing line is how it gets room to move. For a shiny the SV
        # written alongside it divides by the same number, so the note travels
        # at the speed it did before; a fake slider has no such green line to
        # pay it back with (see the hint below), so this is a plain speed
        # change there. The named multiples are the ones this is actually used
        # at; Custom is there because the list can never be complete.
        # Literal tr() calls, one per option -- see AddViewDialog on the gate.
        self.bpm_multiplier_combo = QComboBox()
        self.bpm_multiplier_combo.addItem(tr("MainWindow", "Current BPM"), 1.0)
        self.bpm_multiplier_combo.addItem(tr("MainWindow", "Half"), 0.5)
        self.bpm_multiplier_combo.addItem(tr("MainWindow", "2x"), 2.0)
        self.bpm_multiplier_combo.addItem(tr("MainWindow", "3x"), 3.0)
        self.bpm_multiplier_combo.addItem(tr("MainWindow", "4x"), 4.0)
        self.bpm_multiplier_combo.addItem(tr("MainWindow", "8x"), 8.0)
        self.bpm_multiplier_combo.addItem(tr("MainWindow", "Custom"), None)
        self.bpm_multiplier_combo.currentIndexChanged.connect(self._update_object_mode)

        self.bpm_multiplier_spin = QDoubleSpinBox()
        self.bpm_multiplier_spin.setRange(0.001, 1000.0)
        self.bpm_multiplier_spin.setDecimals(3)

        self.bpm_multiplier_spin.setSuffix("\u00d7")

        self.multiplier_hint = QLabel()
        self.multiplier_hint.setObjectName("fieldNote")
        self.multiplier_hint.setWordWrap(True)
        self.multiplier_hint.setProperty("sheetField", self.multiplier_hint)

        what = self.sheet.add_section("what", tr("MainWindow", "What"))
        self.sheet.body_layout.removeWidget(what)
        self.sheet.body_layout.insertWidget(1, what)
        object_pills = bind_segments(self.object_combo)
        what.field(tr("MainWindow", "Object"), object_pills, tr(
            "MainWindow", "A shiny is a stack of fake sliders under a note."), span=2)
        self.shiny_count_spin.setSuffix(" " + tr("MainWindow", "sliders"))
        what.field(tr("MainWindow", "Stack size"), self.shiny_count_spin, tr(
            "MainWindow", "How many fake sliders sit under each shiny note."))
        what.break_row()
        multiplier_pills = bind_segments(self.bpm_multiplier_combo)
        what.field(tr("MainWindow", "Red line BPM"), multiplier_pills, tr(
            "MainWindow",
            "A red line's BPM is also its scroll speed, so retiming a structure's own line is "
            "how it gets room to move. The named multiples are the ones this is used at."), span=2)
        what.field(tr("MainWindow", "Custom multiplier"), self.bpm_multiplier_spin)
        what.add(self.multiplier_hint, 2)

        # The BPM ramp writes a growing BPM onto each red line; here the red
        # lines are a structure's own and their BPM is not the mapper's to bend.
        self._bpm_section.hide()
        self._subclass_ready = True
        # Snaps, not milliseconds. A barline run wants every millisecond it can
        # get; a run of drawn objects at that density is thousands of sliders
        # nobody can see past.
        snaps = self.mode_combo.findData("snaps")
        if snaps >= 0:
            self.mode_combo.setCurrentIndex(snaps)
        self._load_kind_multiplier()
        self._update_object_mode()

    RUN_COLOR = "#fbb706"

    def _title(self) -> str:
        return tr("MainWindow", "Generate fake sliders")

    def _extra_modes(self) -> list[tuple[str, str]]:
        # A third rhythm the barline generator has no use for: the chart's own
        # notes. Turning a run of notes into shiny ones is the same generation
        # with the beat grid swapped for where the notes actually are.
        return [(tr("MainWindow", "At each note"), "notes")]

    def _object_kind_changed(self) -> None:
        self._load_kind_multiplier()
        self._update_object_mode()

    def _load_kind_multiplier(self) -> None:
        """Show the selected object's own remembered multiplier."""
        value = self._kind_multipliers[self.object_kind()]
        found = self.bpm_multiplier_combo.findData(value)
        self.bpm_multiplier_combo.setCurrentIndex(
            found if found >= 0 else self.bpm_multiplier_combo.findData(None)
        )
        self.bpm_multiplier_spin.setValue(value)

    def _update_object_mode(self) -> None:
        shiny = self.object_kind() == "shiny"
        # The combo itself is shown for both kinds now -- a fake slider's own
        # restore line is retimed the same way a shiny's is. The hint below it
        # stays shiny-only: it explains the SV compensation that pays the
        # retime back, and a fake slider has no green line to do that with.
        custom = self.bpm_multiplier_combo.currentData() is None
        set_row_visible(self._form_layout, self.bpm_multiplier_spin, custom)
        set_row_visible(self._form_layout, self.shiny_count_spin, shiny)
        multiplier = self.bpm_multiplier()
        show_hint = shiny and multiplier != 1.0
        self.multiplier_hint.setText(
            tr(
                "MainWindow",
                "Shiny only: the red line is written at {0}x the chart's BPM, "
                "with a green line right after it dividing SV by the same "
                "amount, so the shiny travels at the speed it already did. A "
                "fake slider's restore line has no such green line, so retiming "
                "it there is a plain speed change with nothing to compensate.",
            ).format(f"{multiplier:g}") if show_hint else ""
        )
        set_row_visible(self._form_layout, self.multiplier_hint, show_hint)
        self._update_count()

    def bpm_multiplier(self) -> float:
        """The chosen multiple of the chart's own BPM, named or typed."""
        chosen = self.bpm_multiplier_combo.currentData()
        return float(self.bpm_multiplier_spin.value() if chosen is None else chosen)

    def _update_count(self) -> None:
        # Overrides the barline generator's "N red lines", which is not what
        # this one writes.
        if not getattr(self, "_subclass_ready", False):
            return  # still inside the base __init__
        count = len(self.times())
        self.set_summary(tr("MainWindow", "{0} objects").format(count), bad=count == 0)
        self.action_button.setEnabled(count > 0)
        self.run_diagram.refresh()

    def object_kind(self) -> str:
        return str(self.object_combo.currentData())

    def shiny_count(self) -> int:
        return int(self.shiny_count_spin.value())


class MultiFakeSliderDialog(SheetDialog):
    """The Multiple Fake Slider tool's config: a run's shape, chosen once.

    Opened by `_set_gimmick_tool` when the tool is *selected*, not on every
    click -- a run's start, end and spacing describe the tool, not one
    placement, so asking on each click would be the same question asked a
    dozen times over a dozen sliders. Start and end locate the run's
    *structures*; each structure's own slider then lands its usual
    `fake_slider_offset_ms` further on, exactly where one click of the plain
    Fake Slider tool would put it -- see `MainWindow._multi_fake_slider_commands`.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(
            tr("MainWindow", "Multiple fake slider"),
            tr("MainWindow", "The run each click of the tool places, around the snap you click."),
            action=tr("MainWindow", "OK"), rail=False, explain=False, restore=False, parent=parent)
        self.action_button.clicked.disconnect()
        self.action_button.clicked.connect(self._accept)
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        section = self.sheet.add_section("run", tr("MainWindow", "Run"))

        self.start_spin = QSpinBox()
        # Negative allowed and asymmetric with End on purpose: a run is
        # routinely wanted leading into the click as well as following it.
        self.start_spin.setRange(-10000, 10000)
        self.start_spin.setSuffix(" ms")
        self.start_spin.setValue(0)
        section.field(tr("MainWindow", "Start offset"), self.start_spin)

        self.end_spin = QSpinBox()
        self.end_spin.setRange(-10000, 10000)
        self.end_spin.setSuffix(" ms")
        self.end_spin.setValue(16)
        section.field(tr("MainWindow", "End offset"), self.end_spin)

        self.distance_spin = QSpinBox()
        self.distance_spin.setRange(1, 10000)
        self.distance_spin.setSuffix(" ms")
        self.distance_spin.setValue(2)
        section.field(tr("MainWindow", "Distance"), self.distance_spin)
        self.resize(460, 300)

    def _accept(self) -> None:
        # Clamped up rather than refused: End below Start is "I want just the
        # one structure at Start", not a mistake to bounce back to the user.
        if self.end_spin.value() < self.start_spin.value():
            self.end_spin.setValue(self.start_spin.value())
        self.accept()

    def parameters(self) -> tuple[int, int, int]:
        """(start, end, distance), all in ms."""
        return (self.start_spin.value(), self.end_spin.value(), self.distance_spin.value())


class TimingLineDialog(SheetDialog):
    """Retype one timing line: its value, and the millisecond it sits on.

    One dialog for both kinds, because the only difference is what the value
    means -- BPM on a red line, SV multiplier on a green one. Both carry a time,
    and moving a line is the edit that has no other keyboard route: dragging
    retimes it only as far as the snap grid allows.
    """

    def __init__(self, point: TimingPoint, parent=None) -> None:
        self.uninherited = point.uninherited
        super().__init__(
            tr("MainWindow", "Red line") if self.uninherited else tr("MainWindow", "Green line"),
            format_time(round(point.time)),
            action=tr("MainWindow", "OK"), rail=False, explain=False, restore=False, parent=parent)
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        section = self.sheet.add_section("line", tr("MainWindow", "Line"))

        self.value_spin = TrimmedDoubleSpinBox()
        if self.uninherited:
            self.value_spin.setRange(0.001, 1000000.0)
            self.value_spin.setDecimals(3)
            self.value_spin.setSuffix(" BPM")
            self.value_spin.setValue(point.bpm or 120.0)
            section.field(tr("MainWindow", "BPM"), self.value_spin)
        else:
            self.value_spin.setRange(0.01, 100.0)
            self.value_spin.setDecimals(SV_DECIMALS)
            self.value_spin.setSuffix("\u00d7")
            self.value_spin.setValue(point.sv_multiplier)
            section.field(tr("MainWindow", "SV multiplier"), self.value_spin)

        self.time_spin = QSpinBox()
        self.time_spin.setRange(0, 100000000)
        self.time_spin.setSuffix(" ms")
        self.time_spin.setValue(round(point.time))
        section.field(tr("MainWindow", "Time"), self.time_spin)

        # Beats per bar, which is where this line's barlines fall. Red lines
        # only: osu! reads meter from the uninherited point in force, so on a
        # green line the field is written but never counts. The ceiling is
        # generous on purpose -- anti-barline writes 999 so a line never stamps
        # a second bar, and clamping that on open would silently rewrite it.
        self.meter_spin = None
        if self.uninherited:
            self.meter_spin = QSpinBox()
            self.meter_spin.setRange(1, 10000)
            self.meter_spin.setValue(max(1, point.meter))
            section.field(tr("MainWindow", "Meter"), self.meter_spin)

        # The two effects bits, editable where the line itself is. Both are
        # things a gimmick has to fix line by line -- a stray bar to hide, a
        # kiai section a generated line switched off -- and until now the only
        # way to reach either was a text editor.
        section.break_row()
        self.kiai_check = QCheckBox(tr("MainWindow", "Kiai"))
        self.kiai_check.setChecked(point.kiai)
        section.switch(self.kiai_check, span=1)

        self.omit_barline_check = QCheckBox(tr("MainWindow", "Omit barline"))
        self.omit_barline_check.setChecked(point.omit_first_barline)
        section.switch(self.omit_barline_check, span=1)
        self.resize(460, 320)

    def effects(self, point: TimingPoint) -> int:
        """`point.effects` with only the two checkboxes' bits rewritten.

        Masked rather than rebuilt: the field carries bits this dialog does not
        offer, and dropping them would be a silent edit.
        """
        effects = point.effects
        for bit, checked in (
            (EFFECT_KIAI, self.kiai_check.isChecked()),
            (EFFECT_OMIT_FIRST_BARLINE, self.omit_barline_check.isChecked()),
        ):
            effects = (effects | bit) if checked else (effects & ~bit)
        return effects

    def changes(self, point: TimingPoint) -> dict:
        """`EditTimingPoint` changes, holding only the fields that moved.

        Every field in one command, so retiming a line, changing its value and
        toggling kiai together is one Ctrl+Z rather than three.
        """
        changes = {}
        time_ms = float(self.time_spin.value())
        if time_ms != point.time:
            changes["time"] = (point.time, time_ms)
        value = self.value_spin.value()
        beat_length = 60000.0 / value if self.uninherited else -100.0 / value
        if beat_length != point.beat_length:
            changes["beat_length"] = (point.beat_length, beat_length)
        if self.meter_spin is not None and self.meter_spin.value() != point.meter:
            changes["meter"] = (point.meter, self.meter_spin.value())
        effects = self.effects(point)
        if effects != point.effects:
            changes["effects"] = (point.effects, effects)
        return changes


class SVFunctionPreview(QWidget):
    """20 dots showing what Generate would actually produce.

    Plots the real rate at each step (initial -> final through `sv_ease`), not
    a normalised 0..1 curve, so a 1.10x -> 0.90x sweep is drawn *descending*.
    The old version always rose left-to-right whatever the rates were, which
    made it a picture of the easing function rather than of the result -- the
    one case where the preview mattered most (did I get the direction right?)
    was the case it could not show.
    """

    def __init__(self, width: int = 190, height: int = 130, *, labels: bool = True) -> None:
        super().__init__()
        self.function_id = "linear"
        self.initial_rate = 1.0
        self.final_rate = 1.0
        # "", "point" or "pair" -- see SVFunctionDialog's mode combo. Oscillation
        # is the same easing function read as an *amplitude* rather than as a
        # position, so it belongs here rather than in a second preview widget:
        # the curve drawn is still exactly what Generate writes.
        self.oscillate = ""
        # The dialog renders one of these per function button, at button size
        # and without the endpoint labels -- there is no room for them there,
        # and the same two numbers on all seven tiles say nothing anyway.
        self.labels = labels
        self.setFixedSize(width, height)

    def set_function(self, function_id: str) -> None:
        self.function_id = function_id
        self.update()

    def set_range(self, initial_rate: float, final_rate: float) -> None:
        self.initial_rate = float(initial_rate)
        self.final_rate = float(final_rate)
        self.update()

    def set_oscillate(self, mode: str) -> None:
        self.oscillate = mode
        self.update()

    def rate_at(self, t: float) -> float:
        return self.initial_rate + (self.final_rate - self.initial_rate) * sv_ease(
            self.function_id, t, self.initial_rate, self.final_rate,
        )

    def rates(self, count: int) -> list[float]:
        """The `count` values Generate would write, in order.

        One function for the preview and for `_generate_sv`'s own loop would be
        better still, but that loop also applies the position offset and the
        BPM compensation, neither of which the preview knows about. This is the
        shape; that is the shape with the map's own numbers folded in.
        """
        if self.oscillate:
            return oscillating_series(
                self.initial_rate,
                abs(self.final_rate - self.initial_rate),
                count,
                lambda t: sv_ease(self.function_id, t, self.initial_rate, self.final_rate),
                per_pair=self.oscillate == "pair",
            )
        return [self.rate_at(index / max(1, count - 1)) for index in range(count)]

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), theme.color("#151b24"))
        painter.setPen(QPen(theme.color("#3a4554"), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

        margin = 22.0 if self.labels else 10.0
        width = self.width() - margin * 2
        height = self.height() - margin * 2

        rates = self.rates(20)
        low, high = min(rates), max(rates)
        span = high - low

        dots = []
        for i, rate in enumerate(rates):
            t = i / 19
            # A flat sweep has no span to normalise against; draw it down the
            # middle rather than dividing by zero or pinning it to an edge.
            ratio = 0.5 if span < 1e-9 else (rate - low) / span
            dots.append(QPointF(margin + t * width, margin + (1 - ratio) * height))

        painter.setPen(QPen(theme.rgba(243, 166, 189, 110), 1))
        painter.drawPolyline(QPolygonF(dots))
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.color("#f3a6bd"))
        radius = 3 if self.labels else 2
        for dot in dots:
            painter.drawEllipse(dot, radius, radius)

        if not self.labels:
            return

        painter.setPen(QPen(QColor(225, 230, 240, 220), 1))
        painter.drawText(QPointF(4, dots[0].y() - 6), f"{rates[0]:.2f}x")
        final_text = f"{rates[-1]:.2f}x"
        text_width = painter.fontMetrics().horizontalAdvance(final_text)
        painter.drawText(QPointF(self.width() - text_width - 4, dots[-1].y() - 6), final_text)


class SVFunctionDialog(SheetDialog):
    """M4 function mode: drag-select a range, then generate an eased SV sweep
    across it.

    A config sheet: the plot of every rate Generate will write across the
    top, the curves as a row of chips under it, then the numbers in sections.
    The curves used to be eight 144px tiles, each plotting its own preview of
    the typed range -- which at the default 1.00x -> 1.00x made all eight the
    same flat line. A chip draws the *shape* (rising or falling with the
    range) and the one large plot draws the result.

    "Generate at" decides the point positions:

    * **Each note** (default) -- one SV point per hit object inside the
      selected range, and nothing between them. This is what an SV sweep is
      usually for: the scroll speed only has to be right where a note is.
    * **Every snap** -- one point per beat subdivision of the chosen divisor,
      for sweeps that must move continuously rather than note by note.

    Position offset defaults to -5ms: an SV point has to take effect
    *slightly before* the note it governs, or that note is still drawn at the
    previous speed.
    """

    DEFAULT_POSITION_OFFSET_MS = -5
    # Square tile, with its graph above the name. The tile *is* the preview now,
    # so it is worth the room: at the old 78px there was nothing to read.
    TILE_SIZE = 144
    TILE_GRAPH_SIZE = (TILE_SIZE - 16, TILE_SIZE - 34)

    # SV ceiling. Matches TimingLineDialog's own range: a barline gimmick is
    # routinely driven at speeds an ordinary chart never asks for, and there is
    # no reason for the two places that type an SV to disagree about the limit.
    MAX_RATE = 100.0

    def __init__(
        self, start_ms: float, end_ms: float, parent=None, snap_divisor: int = 4,
        initial_rate: float = 1.0, final_rate: float = 1.0,
        gimmick_layer: str | bool | None = False,
        position_offset: int | None = None,
        base_timing: list[TimingPoint] | None = None,
        volume: bool = False,
    ) -> None:
        title = tr("MainWindow", "Generate Volume") if volume else tr("MainWindow", "Generate SV")
        super().__init__(
            title,
            tr("MainWindow", "Range: {0} to {1}").format(format_time(round(start_ms)), format_time(round(end_ms))),
            action=tr("MainWindow", "Generate"), rail=False, restore=False, parent=parent)
        self.action_button.clicked.disconnect()
        self.action_button.clicked.connect(self._accept)
        self.start_ms = start_ms
        self.end_ms = end_ms
        # Only for the BPM filter's default, and only layer 6 asks for it. The
        # *base* snapshot rather than the document's own timing for the same
        # reason BarlineFunctionDialog uses it: by the second placement the
        # gimmick difficulty is full of 60000 BPM lines, so its own timing is
        # the worst possible answer to "what BPM are we at".
        self.base_timing = base_timing or []
        # The Kiai and Sound Volume layer's Volume tool is this same generator
        # pointed at TimingPoint.volume: same functions, same modes, same
        # placement. Only the quantity differs -- whole percent in 0..100
        # instead of a scroll rate -- so it is a flag rather than a second
        # dialog with three hundred copied lines behind it.
        self.volume = volume
        # A gimmick SV layer owns exactly one structure's green lines, matched
        # by millisecond (MainWindow._sv_layer_times). A point generated
        # anywhere else is invisible in the layer that made it *and* silently
        # wiped by the next uninherited line, so the two controls that can move
        # a point off its object are taken away rather than merely defaulted.
        self.gimmick_layer = gimmick_layer
        icon = application_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        sheet = self.sheet
        # Rows are fields on the sheet now; set_row_visible finds them there.
        self._form_layout = None

        # Volume is a whole percent in 0..100 (what the .osu field holds and
        # what the writer validates), so the same two spin boxes are told to
        # carry integers rather than a second pair being built beside them.
        low, high = (0.0, 100.0) if volume else (0.01, self.MAX_RATE)
        # SV_DECIMALS for a rate: -100/beat_length routinely lands on a repeating
        # decimal (e.g. 1.4286x), and 2 was rounding the prefilled "nearest
        # green line" value before Generate ever saw it -- so it looked like
        # the generator wasn't reading the existing line at all.
        step, decimals = (1.0, 0) if volume else (0.05, SV_DECIMALS)
        suffix = "%" if volume else "\u00d7"

        # The result, large, at the top: every value Generate will write.
        self.preview = SVFunctionPreview(420, 170)
        self.preview.setMinimumWidth(0)
        self.preview.setMaximumWidth(16777215)
        self.preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        sheet.add_widget(self.preview)

        # Literal tr() calls, one per function -- see AddViewDialog's note on
        # why a dynamic lookup here would silently escape the i18n gate.
        functions = (
            ("linear", tr("MainWindow", "Linear")),
            ("sin_in", tr("MainWindow", "Sin In")),
            ("sin_out", tr("MainWindow", "Sin Out")),
            ("exp1.3", tr("MainWindow", "Exp 1.3")),
            ("exp1.6", tr("MainWindow", "Exp 1.6")),
            ("true_exp", tr("MainWindow", "True Exp")),
            ("sin", tr("MainWindow", "Sin")),
            ("exp_x", tr("MainWindow", "Exp x")),
        )
        self.function_buttons: dict[str, QToolButton] = {}
        self._function_group = QButtonGroup(self)
        self._function_group.setExclusive(True)
        chips = QWidget()
        chips.setStyleSheet(
            "QToolButton { background: #222a36; color: #aeb8c5; border: 1px solid #303947;"
            " border-radius: 6px; padding: 4px 9px 4px 5px; font-size: 12px; font-weight: 600; }"
            "QToolButton:hover { background: #2a3341; color: #e8edf3; }"
            "QToolButton:checked { background: #2a2230; color: #ffffff; border: 1px solid #ff66aa; }")
        chip_rows = QGridLayout(chips)
        chip_rows.setContentsMargins(0, 0, 0, 0)
        chip_rows.setSpacing(6)
        for index, (function_id, label) in enumerate(functions):
            button = QToolButton()
            button.setText(label)
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setIconSize(QSize(*self.CHIP_ICON_SIZE))
            button.setCheckable(True)
            button.setFocusPolicy(Qt.NoFocus)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setChecked(function_id == "linear")
            button.toggled.connect(
                lambda checked, f=function_id: self._update_preview() if checked else None
            )
            self._function_group.addButton(button)
            self.function_buttons[function_id] = button
            chip_rows.addWidget(button, index // 4, index % 4)
        curve_help = tr(
            "MainWindow",
            "How the value moves from Initial to Final. Sin In is fast at the start and slow at "
            "the end, following TaikoEditor's naming (the reverse of easings.net). True Exp bends "
            "by the range itself, so 1\u00d7 to 10\u00d7 is a hard curve and 1\u00d7 to 1.1\u00d7 "
            "is nearly straight.")
        sheet.add_widget(sheet.field(tr("MainWindow", "Curve"), chips, curve_help))

        rates = sheet.add_section("rates", tr("MainWindow", "Volume") if volume else tr("MainWindow", "Rates"))
        self.initial_rate_spin = TrimmedDoubleSpinBox()
        self.initial_rate_spin.setRange(low, high)
        self.initial_rate_spin.setSingleStep(step)
        self.initial_rate_spin.setDecimals(decimals)
        self.initial_rate_spin.setSuffix(suffix)
        self.initial_rate_spin.setValue(round(float(initial_rate), decimals))
        self.initial_rate_spin.valueChanged.connect(self._update_preview)
        rates.field(
            tr("MainWindow", "Initial volume") if volume else tr("MainWindow", "Initial rate"),
            self.initial_rate_spin,
            tr("MainWindow", "The value at the first point."))

        self.final_rate_spin = TrimmedDoubleSpinBox()
        self.final_rate_spin.setRange(low, high)
        self.final_rate_spin.setSingleStep(step)
        self.final_rate_spin.setDecimals(decimals)
        self.final_rate_spin.setSuffix(suffix)
        self.final_rate_spin.setValue(round(float(final_rate), decimals))
        self.final_rate_spin.valueChanged.connect(self._update_preview)
        rates.field(
            tr("MainWindow", "Final volume") if volume else tr("MainWindow", "Final rate"),
            self.final_rate_spin,
            tr("MainWindow", "The value at the last point."))

        # The exponent behind the "Exp x" chip. The two fixed exp curves are
        # the shapes that get used most and stay as one click each; this is the
        # same curve with the number exposed, for a sweep that wants to bend
        # harder than 1.6 or softer than 1.3. Floored at 1, because below it
        # the curve bends the other way and the chip would stop being an exp
        # curve.
        self.exp_spin = QDoubleSpinBox()
        self.exp_spin.setRange(1.0, 10.0)
        self.exp_spin.setDecimals(2)
        self.exp_spin.setSingleStep(0.1)
        self.exp_spin.setValue(2.0)
        self.exp_spin.valueChanged.connect(self._update_preview)
        self._exp_field = rates.field(tr("MainWindow", "Exponent"), self.exp_spin, tr(
            "MainWindow", "The x in t^x for the Exp x curve. Floored at 1: below it the curve bends the other way."))

        # Sweep or oscillation. The same growth function drives both: a sweep
        # eases the rate from Initial to Final, an oscillation eases the
        # *amplitude* it fans out to either side of Initial, reaching Final at
        # the last point. Per point opens the fan on every step (the two sides
        # peak at different widths); per pair opens it once per up/down pair,
        # so every pair is symmetric about the base.
        # Literal tr() calls, one per option -- see AddViewDialog's note on why
        # a dict lookup here would silently escape the i18n coverage gate.
        where = sheet.add_section("where", tr("MainWindow", "Where the points go"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("MainWindow", "Sweep"), "")
        self.mode_combo.addItem(tr("MainWindow", "Oscillate (per point)"), "point")
        self.mode_combo.addItem(tr("MainWindow", "Oscillate (per pair)"), "pair")
        self.mode_combo.currentIndexChanged.connect(self._update_preview)
        where.field(tr("MainWindow", "Mode"), bind_segments(self.mode_combo), tr(
            "MainWindow",
            "Sweep eases from Initial to Final. Oscillate fans out either side of Initial, "
            "reaching Final at the last point: per point on every step, per pair once per "
            "up/down pair."), span=2)
        self.mode_combo.setProperty("sheetField", self.mode_combo.parentWidget().property("sheetField"))

        self.placement_combo = QComboBox()
        # Literal tr() calls, one per option -- see AddViewDialog's note on why
        # a dict lookup here would silently escape the i18n coverage gate.
        self.placement_combo.addItem(tr("MainWindow", "Each note"), "notes")
        self.placement_combo.addItem(tr("MainWindow", "Every snap"), "snaps")
        self.placement_combo.currentIndexChanged.connect(self._update_placement_controls)
        placement = bind_segments(self.placement_combo)
        where.field(tr("MainWindow", "Generate at"), placement, tr(
            "MainWindow",
            "Each note puts a line just before every note in the range. Every snap puts one on "
            "every gridline of the snap below."))
        self.placement_combo.setProperty("sheetField", placement.property("sheetField"))

        self.snap_combo = QComboBox()
        for divisor in SNAP_DIVISORS:
            self.snap_combo.addItem(f"1/{divisor}", divisor)
        found = self.snap_combo.findData(int(snap_divisor))
        self.snap_combo.setCurrentIndex(found if found >= 0 else self.snap_combo.findData(4))
        self.snap_label = QLabel(tr("MainWindow", "Snap"))
        where.field(tr("MainWindow", "Snap"), self.snap_combo, tr(
            "MainWindow", "The gridline spacing for Every snap."))

        self.position_offset_spin = QSpinBox()
        self.position_offset_spin.setRange(-5000, 5000)
        self.position_offset_spin.setSuffix(" ms")
        # An SV point governs what comes after it, so it has to land slightly
        # before the note it is meant to affect. In a gimmick layer the value
        # comes from that layer's config, which is also what decides which
        # milliseconds the layer recognises as its own.
        self.position_offset_spin.setValue(
            self.DEFAULT_POSITION_OFFSET_MS if position_offset is None else int(position_offset)
        )
        where.field(tr("MainWindow", "Position offset"), self.position_offset_spin, tr(
            "MainWindow",
            "How far ahead of its note each line sits. An SV point governs what comes after "
            "it, so a note needs its line slightly early."))

        self.omit_barline_check = QCheckBox(tr("MainWindow", "Omit barline"))
        self.omit_barline_check.setChecked(False)
        where.switch(self.omit_barline_check, tr(
            "MainWindow", "Only matters where a generated point lands on a red line."), span=1)

        self.relative_to_final_bpm_check = QCheckBox(tr("MainWindow", "Relative to final BPM"))
        # Off in a gimmick layer. It multiplies every generated rate by
        # local_bpm / start_bpm, and a gimmick's local BPM is 60000 against a
        # chart's 180 -- so a "1.0x to 2.0x" sweep came out in the hundreds and
        # neither end landed on the number that was typed.
        self.relative_to_final_bpm_check.setChecked(not gimmick_layer and not volume)
        where.switch(self.relative_to_final_bpm_check, tr(
            "MainWindow", "Scales each rate so the scroll speed holds across BPM changes in the range."), span=1)
        if volume:
            # Position offset and Omit barline describe an *inserted* point;
            # the Volume tool edits the timing points already in the range.
            # Relative to final BPM is a statement about scroll speed, and a
            # hitsound is as loud as it is whatever the BPM is doing.
            for widget in (self.position_offset_spin, self.omit_barline_check,
                           self.relative_to_final_bpm_check):
                set_row_visible(None, widget, False)

        self.include_shiny_check = QCheckBox(tr("MainWindow", "Include shiny notes"))
        self.include_shiny_check.setChecked(False)
        # Layer 6 owns *every* red line -- the chart's own timing, a barline
        # gimmick's 60000 BPM run, hand-placed lines -- and a sweep across a
        # dragged range hits all of them indiscriminately. This narrows it to
        # one BPM range, which is how you drive a single gimmick run without
        # disturbing the ordinary timing lines interleaved with it.
        self.bpm_filter_check = QCheckBox(tr("MainWindow", "Only red lines at this BPM"))
        self.bpm_filter_check.setChecked(False)
        self.bpm_filter_spin = QDoubleSpinBox()
        self.bpm_filter_spin.setRange(1.0, 1000000.0)
        self.bpm_filter_spin.setDecimals(4)
        self.bpm_filter_spin.setSuffix(" BPM")
        # The BPM in force where the drag started -- the run you are looking at
        # is almost always the one you want, so the default costs no typing.
        self.bpm_filter_spin.setValue(
            base_bpm_at(self.base_timing, start_ms) if self.base_timing else 120.0
        )
        # An inclusive range rather than one exact BPM: an exact match is just
        # the degenerate case where the two ends are equal.
        self.bpm_filter_max_spin = QDoubleSpinBox()
        self.bpm_filter_max_spin.setRange(1.0, 1000000.0)
        self.bpm_filter_max_spin.setDecimals(4)
        self.bpm_filter_max_spin.setSuffix(" BPM")
        self.bpm_filter_max_spin.setValue(self.bpm_filter_spin.value())
        # Layer 5's counterpart: two fake sliders can sit on the same
        # millisecond and differ only in `length` (-0.001 vs -0.0011), which is
        # how a mapper encodes "these get one SV, those get another".
        self.length_filter_check = QCheckBox(tr("MainWindow", "Only fake sliders with this length"))
        self.length_filter_check.setChecked(False)
        self.length_filter_spin = QDoubleSpinBox()
        self.length_filter_spin.setRange(-100000.0, FAKE_SLIDER_MAX_LENGTH)
        self.length_filter_spin.setDecimals(4)
        self.length_filter_spin.setValue(-0.001)
        self.length_filter_max_spin = QDoubleSpinBox()
        self.length_filter_max_spin.setRange(-100000.0, FAKE_SLIDER_MAX_LENGTH)
        self.length_filter_max_spin.setDecimals(4)
        self.length_filter_max_spin.setValue(-0.001)
        if gimmick_layer in ("sv_barline", "sv_fake_slider"):
            filters = sheet.add_section("filters", tr("MainWindow", "Which lines"))
            if gimmick_layer == "sv_fake_slider":
                filters.switch(self.include_shiny_check, tr(
                    "MainWindow",
                    "This layer holds the speed of plain fake sliders. Shiny notes "
                    "are left out by default -- their gimmick line sits on the note, "
                    "so the normal chart SV layer already moves the note and its "
                    "shine together -- but this sweep can add them in as well.",
                ))
                filters.switch(self.length_filter_check, tr(
                    "MainWindow", "Sweep one length family of fake sliders and leave the rest."))
                filters.field(tr("MainWindow", "Length from"), self.length_filter_spin)
                filters.field(tr("MainWindow", "Length to"), self.length_filter_max_spin)
                self.length_filter_check.toggled.connect(self._update_length_filter_row)
            else:
                filters.switch(self.bpm_filter_check, tr(
                    "MainWindow", "Drive one gimmick run without touching the chart's own timing lines."))
                filters.field(tr("MainWindow", "BPM from"), self.bpm_filter_spin)
                filters.field(tr("MainWindow", "BPM to"), self.bpm_filter_max_spin)
                self.bpm_filter_check.toggled.connect(self._update_bpm_filter_row)

        sheet.set_help_default(
            tr("MainWindow", "Curve"),
            tr("MainWindow", "The plot shows every value Generate will write, in order. This picture is the curve alone, first point to last."))
        self.curve = Diagram(self._paint_curve, 130)
        sheet.set_diagram(self.curve, tr("MainWindow", "Curve"))
        self.resize(980, 700)

        self._update_preview()
        self._update_placement_controls()
        self._update_bpm_filter_row()
        self._update_length_filter_row()

    CHIP_ICON_SIZE = (30, 20)

    def _curve_pixmap(self, function_id: str, size: tuple[int, int], width: float) -> QPixmap:
        """`function_id`'s shape, rising or falling with the typed range.

        Normalised, so a 1.00x -> 1.00x range still shows the curve rather
        than eight flat lines -- but True Exp bends by the range itself, so it
        is read with the real endpoints whenever they differ.
        """
        initial, final = self.initial_rate_spin.value(), self.final_rate_spin.value()
        falling = final < initial
        a, b = (initial, final) if initial != final else (1.0, 4.0)
        w, h = size
        pixmap = QPixmap(w * 2, h * 2)
        pixmap.setDevicePixelRatio(2.0)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        points = []
        for index in range(33):
            t = index / 32
            eased = sv_ease(function_id, t, a, b)
            y = eased if not falling else 1 - eased
            points.append(QPointF(2 + t * (w - 4), h - 2 - y * (h - 4)))
        painter.setPen(QPen(theme.color("#f3a6bd"), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolyline(QPolygonF(points))
        painter.end()
        return pixmap

    def _paint_curve(self, painter: QPainter, rect: QRect) -> None:
        inner = rect.adjusted(16, 12, -16, -22)
        painter.setPen(QPen(theme.color("#3a4554"), 1))
        painter.drawLine(inner.bottomLeft(), inner.bottomRight())
        painter.drawLine(inner.bottomLeft(), inner.topLeft())
        pixmap = self._curve_pixmap(self.selected_function(), (inner.width(), inner.height()), 2.2)
        painter.drawPixmap(inner.topLeft(), pixmap)
        _diagram_text(painter, inner.left(), rect.bottom() - 6, tr("MainWindow", "first point"))
        _diagram_text(painter, inner.right(), rect.bottom() - 6, tr("MainWindow", "last point"), align=Qt.AlignRight)

    def _resolved_function(self, key: str) -> str:
        """The curve id a tile stands for.

        Every tile but one is its own id. "Exp x" is the exponent box's current
        value spelled as an id (`sv_ease` parses it back out), which is what
        lets one tile be any exponent without a second argument on every call
        that takes a function.
        """
        if key == "exp_x":
            return f"exp{self.exp_spin.value():g}"
        return key

    def selected_function(self) -> str:
        for key, button in self.function_buttons.items():
            if button.isChecked():
                return self._resolved_function(key)
        return "linear"

    def set_selected_function(self, function_id: str) -> None:
        button = self.function_buttons.get(function_id)
        if button is None and function_id.startswith("exp") and function_id != "true_exp":
            # A typed exponent comes back as its own id; put the number back in
            # the box and light the tile it belongs to.
            try:
                self.exp_spin.setValue(float(function_id[3:]))
            except ValueError:
                return
            button = self.function_buttons.get("exp_x")
        if button is not None:
            button.setChecked(True)

    def _update_preview(self) -> None:
        """Re-plot the result and every chip's curve against the current rates.

        The chips follow the range's direction -- a 1.10x -> 0.90x sweep shows
        every curve falling -- and "Exp x" draws the exponent in its box.
        """
        self.preview.set_range(self.initial_rate_spin.value(), self.final_rate_spin.value())
        self.preview.set_oscillate(str(self.mode_combo.currentData()))
        for key, button in self.function_buttons.items():
            button.setIcon(QIcon(self._curve_pixmap(self._resolved_function(key), self.CHIP_ICON_SIZE, 1.6)))
        self.preview.set_function(self.selected_function())
        self._exp_field.setVisible(self.function_buttons["exp_x"].isChecked())
        self.curve.refresh()

    def _update_placement_controls(self) -> None:
        """The snap divisor only means anything in "Every snap" mode.

        setRowVisible collapses the whole form row; hiding the two widgets
        individually would leave the row's blank space behind.

        A gimmick layer has no "where" to offer: its points go on its own
        objects or they do not exist. The position offset stays -- it is how far
        ahead of those objects they sit, which is a real choice and the one the
        normal chart layer needs.

        Volume mode has no "where" either, for a different reason: it edits
        the timing points already in the range rather than placing new ones,
        so there is no position to offer a choice about.
        """
        if self.gimmick_layer or self.volume:
            set_row_visible(self._form_layout, self.snap_combo, False)
            set_row_visible(self._form_layout, self.placement_combo, False)
            return
        every_snap = str(self.placement_combo.currentData()) == "snaps"
        set_row_visible(self._form_layout, self.snap_combo, every_snap)

    def _update_bpm_filter_row(self) -> None:
        """The BPM only means anything while the filter is on.

        Hidden rather than disabled, same as the snap divisor above: a greyed
        control still takes a row of an already tall form and still invites a
        click that does nothing.
        """
        if self.gimmick_layer != "sv_barline":
            return
        visible = self.bpm_filter_check.isChecked()
        set_row_visible(self._form_layout, self.bpm_filter_spin, visible)
        set_row_visible(self._form_layout, self.bpm_filter_max_spin, visible)

    def _update_length_filter_row(self) -> None:
        """The length range only means anything while the filter is on --
        same reasoning as `_update_bpm_filter_row`."""
        if self.gimmick_layer != "sv_fake_slider":
            return
        visible = self.length_filter_check.isChecked()
        set_row_visible(self._form_layout, self.length_filter_spin, visible)
        set_row_visible(self._form_layout, self.length_filter_max_spin, visible)

    def _accept(self) -> None:
        # Clamped up rather than refused, same reasoning as
        # MultiFakeSliderDialog._accept: "to" below "from" is "I only meant
        # the one BPM at from", not a mistake worth bouncing back to the user.
        if self.bpm_filter_max_spin.value() < self.bpm_filter_spin.value():
            self.bpm_filter_max_spin.setValue(self.bpm_filter_spin.value())
        if self.length_filter_max_spin.value() < self.length_filter_spin.value():
            self.length_filter_max_spin.setValue(self.length_filter_spin.value())
        self.accept()

    def parameters(self) -> dict:
        return {
            "initial_rate": self.initial_rate_spin.value(),
            "final_rate": self.final_rate_spin.value(),
            "placement": str(self.placement_combo.currentData()),
            "snap_divisor": int(self.snap_combo.currentData()),
            # Zero in volume mode, not merely hidden: the Volume tool edits
            # the timing points already in the range, so there is nothing to
            # offset and no path may act as though there were.
            "position_offset": 0 if self.volume else self.position_offset_spin.value(),
            "omit_barline": self.omit_barline_check.isChecked(),
            "relative_to_final_bpm": self.relative_to_final_bpm_check.isChecked(),
            "function": self.selected_function(),
            "oscillate": str(self.mode_combo.currentData()),
            "include_shiny": self.include_shiny_check.isChecked(),
            # One key rather than a flag and a value: None *is* "no filter",
            # so no consumer can read the BPM interval without first having
            # checked whether it applies. The value is a (low, high) tuple,
            # inclusive at both ends -- an exact single BPM is just low == high.
            "only_red_line_bpm": (
                (self.bpm_filter_spin.value(), self.bpm_filter_max_spin.value())
                if self.bpm_filter_check.isChecked() else None
            ),
            "only_fake_slider_length": (
                (self.length_filter_spin.value(), self.length_filter_max_spin.value())
                if self.length_filter_check.isChecked() else None
            ),
        }
