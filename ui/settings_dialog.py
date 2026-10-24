from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QKeySequence

from library.skin import available_skins, skins_root
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QKeySequenceEdit,
    QLineEdit,
)

from ui.config_sheet import bind_segments, bind_tiles, set_warning, ui_asset

from ui import theme
from app import updater
from audio.audio_engine import DEFAULT_HITSOUND_OFFSET_MS
from app.settings import (
    NOTE_OPACITY_DEFAULT_PERCENT,
    NOTE_OPACITY_MIN_PERCENT,
    SettingsManager,
    ShortcutRegistry,
    duplicate_shortcuts,
    normalize_sequence,
)
from ui.settings_widgets import (
    _Group,
    _NoteOpacityPreview,
    _Page,
    _control_width,
    _nav_tile,
    _theme_swatch,
)


# Each page's tile in the sidebar, the way System Settings tells its panes
# apart at a glance. Audio and the two greys are theme keys, so they follow
# the theme; the other three are fixed, like a chart colour, because a tile
# that changes colour with the theme stops being the thing you look for.
NAV_TINTS = {
    "general": "#7d8794",
    "audio": "#ff66aa",
    "skin": "#5aa7c7",
    "language": "#f0b34a",
    "shortcuts": "#a6e22e",
    "advanced": "#7d8794",
}

SETTINGS_STYLE = """
QDialog#settingsDialog { background: #191f29; }
QFrame#settingsSide { background: #151b24; border: 0; }
QLineEdit#settingsSearch, QLineEdit#shortcutSearch {
    background: #11151c; border: 1px solid #222a36; border-radius: 8px; padding: 6px 10px;
}
QLineEdit#settingsSearch:focus, QLineEdit#shortcutSearch:focus { border-color: #ff66aa; }
QListWidget#settingsNav { background: transparent; border: 0; outline: 0; font-size: 13px; font-weight: 600; }
QListWidget#settingsNav::item { color: #aeb8c5; padding: 5px 6px; border-radius: 8px; margin: 1px 0; }
QListWidget#settingsNav::item:hover { background: #1d2431; color: #e8edf3; }
QListWidget#settingsNav::item:selected { background: #2a3341; color: #e8edf3; }
QLabel#sideNote { color: #7d8794; font-size: 12px; background: transparent; }
QScrollArea#settingsPage, QWidget#settingsPageBody { background: transparent; border: 0; }
QLabel#pageTitle { font-size: 22px; font-weight: 700; background: transparent; }
QLabel#groupTitle { color: #aeb8c5; font-size: 12.5px; font-weight: 600; background: transparent; }
QFrame#settingsCard { background: #222a36; border: 0; border-radius: 12px; }
QFrame#settingsRow { background: transparent; border: 0; }
QFrame#settingsRow[divider="true"] { border-top: 1px solid #2a3341; }
QLabel#rowLabel { font-size: 13px; font-weight: 600; background: transparent; }
QLabel#rowLabel:disabled { color: #4d5664; }
QLabel#rowNote { color: #aeb8c5; font-size: 12px; background: transparent; }
QLabel#rowNote:disabled { color: #4d5664; }
QFrame#settingsFoot { background: #191f29; border: 0; border-top: 1px solid #222a36; }
QFrame#settingsRow QCheckBox { background: transparent; spacing: 0; }
QWidget#settingsControl, QWidget#settingsControl QLabel { background: transparent; }
QSlider { background: transparent; min-height: 20px; }
QSlider::groove:horizontal { height: 4px; border-radius: 2px; background: #3a4554; }
QSlider::sub-page:horizontal { border-radius: 2px; background: #ff66aa; }
QSlider::handle:horizontal { width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; background: #e8edf3; }
QSlider::sub-page:horizontal:disabled { background: #3a4554; }
QSlider::handle:horizontal:disabled { background: #4d5664; }
QCheckBox::indicator { width: 40px; height: 24px; }
QKeySequenceEdit QLineEdit {
    background: #151b24; border: 1px solid #3a4554; border-radius: 6px; padding: 4px 8px; font-weight: 600;
}
QKeySequenceEdit QLineEdit:hover { border-color: #4a5668; }
QKeySequenceEdit QLineEdit:focus { border-color: #ff66aa; }
QKeySequenceEdit QLineEdit[warn="true"] { border-color: #ffb347; color: #ffb347; }
QToolButton#shortcutReset { background: transparent; border: 0; border-radius: 6px; padding: 4px; }
QToolButton#shortcutReset:hover { background: #2a3341; }
"""


class SettingsDialog(QDialog):
    """The app-wide settings, in the window's own theme."""

    def __init__(self, settings: SettingsManager, shortcuts: ShortcutRegistry, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.shortcuts = shortcuts
        self.setObjectName("settingsDialog")
        self.setWindowTitle(self.tr("Settings"))
        self.resize(960, 660)
        # Small enough to fit a short laptop screen once the pages scroll.
        self.setMinimumSize(640, 380)
        self._shortcut_editors: dict[str, QKeySequenceEdit] = {}
        self._update_check: object | None = None
        self._build_ui()
        self._load_current_values()
        # A switch loaded off emits nothing, so its rows are set here.
        for row in self._hitsound_rows:
            row.setEnabled(self.hitsounds_enabled.isChecked())
        self._fit_to_screen()

    def _fit_to_screen(self) -> None:
        """Never open taller or wider than the screen it opens on.

        The size above is a preference, not a promise: pages grow as settings
        are added, and a laptop's work area is smaller than the desktop the
        number was picked on. Clamped to the *available* geometry, so a taskbar
        or a dock is already discounted -- and the pages scroll, so clamping
        hides nothing.
        """
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.resize(
            min(self.width(), int(available.width() * 0.9)),
            min(max(self.height(), self.sizeHint().height()),
                int(available.height() * 0.9)),
        )

    def _build_ui(self) -> None:
        """A sidebar of pages with a search over them, the page, and a footer.

        Laid out like System Settings: a darker sidebar for structure, pages
        of rounded cards whose rows put the label on the left and the control
        on the right. The page list stays a list (not one long scroll) because
        the Shortcuts table scrolls on its own and a table inside a scrolling
        column is two scroll bars fighting.
        """
        # First, before any widget exists: set after the build it restyles
        # all ~560 of them a second time, 180ms -> 285ms to open (measured
        # 2026-10-11).
        self.setStyleSheet(SETTINGS_STYLE)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        side = QFrame()
        side.setObjectName("settingsSide")
        side.setFixedWidth(232)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(10, 16, 10, 14)
        side_layout.setSpacing(10)
        self.page_search = QLineEdit()
        self.page_search.setObjectName("settingsSearch")
        self.page_search.setPlaceholderText(self.tr("Search settings"))
        self.page_search.addAction(QIcon(ui_asset("settings-search.svg")), QLineEdit.LeadingPosition)
        self.page_search.setClearButtonEnabled(True)
        self.page_search.textChanged.connect(self._filter_pages)
        side_layout.addWidget(self.page_search)
        self.nav = QListWidget()
        self.nav.setObjectName("settingsNav")
        self.nav.setIconSize(QSize(26, 26))
        self.nav.setFocusPolicy(Qt.NoFocus)
        side_layout.addWidget(self.nav, 1)
        side_note = QLabel(self.tr("For the whole app. Saved on this computer."))
        side_note.setObjectName("sideNote")
        side_note.setWordWrap(True)
        side_note.setContentsMargins(6, 0, 6, 0)
        side_layout.addWidget(side_note)

        self.pages = QStackedWidget()
        self._page_ids: list[str] = []
        for page_id, build in (
            ("general", self._general_page),
            ("audio", self._audio_page),
            ("skin", self._skin_page),
            ("language", self._language_page),
            ("shortcuts", self._shortcuts_page),
            ("advanced", self._advanced_page),
        ):
            title_text = self._page_title(page_id)
            self.nav.addItem(QListWidgetItem(title_text))
            self.pages.addWidget(build(title_text))
            self._page_ids.append(page_id)
        self._paint_nav_icons()
        # The tiles carry theme colours, and the theme is picked live on the
        # Appearance page; repainted once the window has switched to it.
        self.theme_combo.currentIndexChanged.connect(lambda _index: QTimer.singleShot(0, self._paint_nav_icons))
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self._page_words = [self._words_of(self.pages.widget(i), self.nav.item(i).text())
                            for i in range(self.pages.count())]
        body.addWidget(side)
        body.addWidget(self.pages, 1)
        root.addLayout(body, 1)

        foot = QFrame()
        foot.setObjectName("settingsFoot")
        footer = QHBoxLayout(foot)
        footer.setContentsMargins(16, 10, 16, 10)
        footer.setSpacing(8)
        self.restore_button = QPushButton(self.tr("Restore Defaults"))
        self.restore_button.setProperty("role", "ghost")
        self.restore_button.clicked.connect(self._restore_current_page_defaults)
        footer.addWidget(self.restore_button)
        footer.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Apply | QDialogButtonBox.Ok)
        for kind in (QDialogButtonBox.Cancel, QDialogButtonBox.Apply):
            button = self.buttons.button(kind)
            button.setProperty("role", "ghost")
            button.setStyleSheet("QPushButton { border: 1px solid #3a4554; }")
        self.buttons.button(QDialogButtonBox.Apply).clicked.connect(self.apply)
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        footer.addWidget(self.buttons)
        root.addWidget(foot)

    def _page_title(self, page_id: str) -> str:
        return {
            "general": self.tr("General"),
            "audio": self.tr("Audio"),
            # Theme, skin and how the timeline is drawn: more than the skin.
            "skin": self.tr("Appearance"),
            "language": self.tr("Language"),
            "shortcuts": self.tr("Shortcuts"),
            "advanced": self.tr("Advanced"),
        }[page_id]

    def _paint_nav_icons(self) -> None:
        for row, page_id in enumerate(self._page_ids):
            tint = theme.color(NAV_TINTS[page_id])
            icon = QIcon()
            icon.addPixmap(_nav_tile(page_id, tint, False), QIcon.Normal)
            icon.addPixmap(_nav_tile(page_id, tint, True), QIcon.Selected)
            self.nav.item(row).setIcon(icon)

    @staticmethod
    def _words_of(page: QWidget, title: str) -> str:
        """Every word a page shows, for the sidebar search: titles, labels,
        notes, switch names and the choices in its menus."""
        words = [title]
        for label in page.findChildren(QLabel):
            words.append(label.text())
        for button in page.findChildren(QAbstractButton):
            words.extend((button.text(), button.accessibleName()))
        for combo in page.findChildren(QComboBox):
            words.extend(combo.itemText(i) for i in range(combo.count()))
        return "\n".join(words).lower()

    def _filter_pages(self, text: str) -> None:
        """Leave in the sidebar only the pages that mention `text`, and show
        the first of them if the one showing is not among them."""
        wanted = text.strip().lower()
        for row, words in enumerate(self._page_words):
            self.nav.item(row).setHidden(bool(wanted) and wanted not in words)
        current = self.nav.currentItem()
        if current is None or current.isHidden():
            for row in range(self.nav.count()):
                if not self.nav.item(row).isHidden():
                    self.nav.setCurrentRow(row)
                    break

    def _volume_row(self, spin: QSpinBox) -> QWidget:
        """A slider for a volume, with its box beside it.

        The box stays the value (apply() and Restore read it); the slider is
        how "a bit quieter" is asked for, which is what a volume is.
        """
        slider = QSlider(Qt.Horizontal)
        slider.setRange(spin.minimum(), spin.maximum())
        slider.setValue(spin.value())
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        spin.setFixedWidth(84)
        return self._beside(slider, spin)

    @staticmethod
    def _beside(main: QWidget, side: QWidget, width: int = 280) -> QWidget:
        row = QWidget()
        # Named so it is not painted by the window's `QWidget { background }`.
        row.setObjectName("settingsControl")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(main, 1)
        layout.addWidget(side)
        return _control_width(row, width)

    def _percent_slider(self, low: int, high: int, page_step: int = 10) -> tuple[QSlider, QLabel, QWidget]:
        """A slider with its value as a readout, for a look rather than a
        number: nobody knows they want 62%, they want it a bit fainter."""
        slider = QSlider(Qt.Horizontal)
        slider.setRange(low, high)
        slider.setSingleStep(1)
        slider.setPageStep(page_step)
        value = QLabel()
        value.setMinimumWidth(44)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider.valueChanged.connect(lambda percent: value.setText(f"{percent} %"))
        return slider, value, self._beside(slider, value)

    @staticmethod
    def _ghost(button: QPushButton) -> QPushButton:
        button.setProperty("role", "ghost")
        button.setStyleSheet("QPushButton { border: 1px solid #3a4554; }")
        return button

    def _general_page(self, title: str) -> QWidget:
        page = _Page(title)
        saving = page.group(self.tr("Saving"))
        self.confirm_overwrite = QCheckBox(self.tr("Confirm before overwriting original beatmap"))
        saving.switch(self.confirm_overwrite, self.tr("Asks once per save when the file on disk is the one you opened."))
        updates = page.group(self.tr("Updates"))
        self.check_updates_on_startup = QCheckBox(self.tr("Check for updates on startup"))
        updates.switch(self.check_updates_on_startup, self.tr("One request to GitHub when the app opens."))
        self.check_updates_button = self._ghost(QPushButton(self.tr("Check for updates now")))
        self.check_updates_button.clicked.connect(self._check_for_updates)
        updates.wide(self.check_updates_button, own_width=True)
        return page

    def _check_for_updates(self) -> None:
        # Held so the worker is not garbage collected while it is running.
        # The main window owns the restart: it asks about unsaved work and quits.
        restart = getattr(self.parent(), "restart_into_update", None)
        self._update_check = updater.check_now(self, self.settings, restart)

    def _audio_page(self, title: str) -> QWidget:
        page = _Page(title)
        hits = page.group(self.tr("Hitsounds"))
        self.hitsounds_enabled = QCheckBox(self.tr("Hitsounds"))
        hits.switch(self.hitsounds_enabled, self.tr("The skin's own samples when a skin is chosen."))
        self.hitsound_volume = QSpinBox()
        self.hitsound_volume.setRange(0, 100)
        self.hitsound_volume.setSuffix("%")
        volume_row = hits.row(self.tr("Hitsound volume"), self._volume_row(self.hitsound_volume),
                              scrub=self.hitsound_volume)
        self.hitsound_offset_ms = QSpinBox()
        # A trim against the music, not a latency any more. The notes are mixed
        # into the music stream (`audio_engine.HitsoundMixer`), so they already
        # carry exactly the device latency the music does and Music offset
        # below covers both. Kept, and kept negative-capable, because this is
        # the physical world and somebody will want to nudge it.
        self.hitsound_offset_ms.setRange(-500, 500)
        self.hitsound_offset_ms.setSuffix(" ms")
        offset_row = hits.row(self.tr("Hitsound offset (ms)"), _control_width(self.hitsound_offset_ms, 120),
                              self.tr(
                                  "Nudge hitsounds earlier or later against the music. Your device's "
                                  "latency is already covered by Music offset below, because the "
                                  "notes leave through the same output as the song."
                              ))
        # Nothing to set for sounds that are off; still saved as they stand.
        self._hitsound_rows = (volume_row, offset_row)
        for row in self._hitsound_rows:
            self.hitsounds_enabled.toggled.connect(row.setEnabled)

        music = page.group(self.tr("Music"))
        self.music_volume = QSpinBox()
        self.music_volume.setRange(0, 100)
        self.music_volume.setSuffix("%")
        music.row(self.tr("Music volume"), self._volume_row(self.music_volume), scrub=self.music_volume)
        # **This** is the device latency, and now the only setting that is.
        # Both the music and the notes leave through one sink, so one number
        # covers both -- the hitsound offset above is a trim between them
        # rather than a second latency. Ships at zero: the right value is a
        # property of the user's device, not something to guess, which is what
        # the Calibrate button is for.
        self.output_offset_ms = QSpinBox()
        self.output_offset_ms.setRange(-500, 500)
        self.output_offset_ms.setSuffix(" ms")
        self.calibrate_button = self._ghost(QPushButton(self.tr("Calibrate…")))
        self.calibrate_button.clicked.connect(self._calibrate_offset)
        music.row(self.tr("Music offset (ms)"), self._beside(self.output_offset_ms, self.calibrate_button),
                  self.tr(
                      "Shift the playhead to match when the music actually reaches your "
                      "ears. Raise it if the notes look early against what you hear. "
                      "Measured in real time, so one value stays correct at every "
                      "playback speed."
                  ), scrub=self.output_offset_ms)

        interface = page.group(self.tr("Interface"))
        # Some players hate it (owner, 2026-10-23).
        self.page_click_enabled = QCheckBox(self.tr("Tab switch sound"))
        interface.switch(self.page_click_enabled, self.tr("The click when you change page."))
        return page

    def _skin_page(self, title: str) -> QWidget:
        """Theme, skin and the timeline's look. Its own page rather than rows
        on the Audio one: nothing here is about sound."""
        page = _Page(title)
        look = page.group(self.tr("App theme"))
        self.theme_combo = QComboBox()
        names = [("osu", self.tr("osu!")), ("taiko", self.tr("Taiko")), ("lantern", self.tr("Lantern Rite")),
                 ("gold", self.tr("Gold")), ("monokai", self.tr("Monokai")),
                 ("nso", self.tr("NSO"))]
        for name, label in names:
            self.theme_combo.addItem(label, name)
        swatches = []
        for name, label in names:
            primary, _ink, focus, _accent, steps = theme.THEMES[name]
            swatches.append((label, _theme_swatch(steps, primary, focus)))
        look.wide(bind_tiles(self.theme_combo, swatches))
        theme_note = QLabel(self.tr(
            "The colours of the window around the chart. Notes, snap ticks and "
            "the SV graph keep osu!'s own."
        ))
        theme_note.setObjectName("rowNote")
        theme_note.setWordWrap(True)
        look.wide(theme_note)

        skin = page.group(self.tr("Gameplay preview"))
        # Skins live beside the songs folder the user already chose, so this
        # asks for nothing new. Only folders carrying taiko note art are
        # offered -- an osu! install collects dozens of skins for other modes,
        # and listing those would be a menu of choices that change nothing.
        self.skin_combo = QComboBox()
        self.skin_combo.addItem(self.tr("Built-in"), "")
        root = skins_root(self.settings.string_value("library/songs_folder", ""))
        for name in available_skins(root):
            self.skin_combo.addItem(name, name)
        skin.row(self.tr("Gameplay skin"), _control_width(self.skin_combo), self.tr(
            "Uses the note, drumroll and hit-explosion art from one of your "
            "osu! skins in the gameplay preview. Anything a skin does not "
            "provide falls back to the built-in drawing."
        ))
        # The song list's preview, under the art. Live, so the size can be
        # picked by eye against the pane it is in.
        self.song_select_preview, self.song_select_preview_value, preview_row = self._percent_slider(50, 75, 5)
        skin.row(self.tr("Song select preview size"), preview_row)

        # The notes under the slider show what the number looks like.
        timeline = page.group(self.tr("Editor timeline"))
        self.note_opacity, self.note_opacity_value, opacity_row = self._percent_slider(NOTE_OPACITY_MIN_PERCENT, 100)
        timeline.row(self.tr("Note opacity"), opacity_row, self.tr(
            "How solid notes are drawn in the editor timeline layers. Lower "
            "leaves the snap grid and the lines behind them easier to read "
            "through a dense section; 100% draws them opaque. The gameplay "
            "preview is unaffected."
        ))
        self.note_opacity_preview = _NoteOpacityPreview(self.note_opacity, self.skin_combo)
        timeline.wide(self.note_opacity_preview)

        # MainWindow listens to these while they move, so the Editor and
        # Gimmick pages show what the number looks like and cancelling puts
        # the saved value back.
        backdrop = page.group(self.tr("Editor background"))
        self.background_opacity, self.background_opacity_value, backdrop_row = self._percent_slider(0, 100)
        backdrop.row(self.tr("Background opacity"), backdrop_row, self.tr(
            "How strongly the map's own background shows behind the Editor and "
            "Gimmick pages. 0% leaves them plain."
        ))
        self.view_opacity, self.view_opacity_value, view_row = self._percent_slider(0, 100)
        backdrop.row(self.tr("View opacity"), view_row, self.tr(
            "How solid the views are. Under 100% the background shows through "
            "them as well as around them."
        ))

        fancy = page.group(self.tr("Fancy Arranger"))
        self.transform_animation = QCheckBox(self.tr("Animate transforms"))
        fancy.switch(self.transform_animation, self.tr(
            "Notes glide to their new places when a transform changes. Off "
            "jumps straight there, which is lighter on a dense map."))
        return page

    def _calibrate_offset(self) -> None:
        """Tap along to a click track and take the number it settles on.

        Only ever writes the spin box above, which is the app-wide music
        offset. No beatmap's own offset is read or written by any of this --
        the value being measured belongs to the sound card, not to a chart.
        """
        from audio.offset_calibration import OffsetCalibrationDialog

        dialog = OffsetCalibrationDialog(self)
        if dialog.exec() == QDialog.Accepted and dialog.result_offset is not None:
            self.output_offset_ms.setValue(dialog.result_offset)

    def _language_page(self, title: str) -> QWidget:
        page = _Page(title)
        section = page.group(self.tr("Language"))
        self.language_combo = QComboBox()
        self.language_combo.addItem("English", "en")
        self.language_combo.addItem("日本語", "ja")
        section.row(self.tr("Language"), bind_segments(self.language_combo),
                    self.tr("Restart KatsuDon Editor to apply the interface language."))
        return page

    def _shortcuts_page(self, title: str) -> QWidget:
        """One card per category, a key chip at the end of each row.

        The title, the search and the conflict line stay put while the cards
        scroll under them: with a tool set per gimmick layer the list is long,
        and a clash reported somewhere off screen is not reported.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        top = QVBoxLayout()
        top.setContentsMargins(28, 18, 28, 6)
        top.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        top.addWidget(heading)
        # Forty-odd actions across the tool rows of every layer: finding one
        # by name (or finding what Ctrl+R does) beats scrolling for it.
        self.shortcut_search = QLineEdit()
        self.shortcut_search.setObjectName("shortcutSearch")
        self.shortcut_search.setPlaceholderText(self.tr("Find an action or a key"))
        self.shortcut_search.addAction(QIcon(ui_asset("settings-search.svg")), QLineEdit.LeadingPosition)
        self.shortcut_search.setClearButtonEnabled(True)
        self.shortcut_search.textChanged.connect(self._filter_shortcuts)
        top.addWidget(self.shortcut_search)
        layout.addLayout(top)

        cards = _Page("")
        self._shortcut_syncs: list = []
        # (group, [(row, label, editor)]) per category, for the filter.
        self._shortcut_groups: list[tuple[_Group, list[tuple[QFrame, str, QKeySequenceEdit]]]] = []
        for definition in self.shortcuts.definitions.values():
            if not self._shortcut_groups or self._shortcut_groups[-1][0].category != definition.category:
                group = cards.group(self.tr(definition.category))
                group.category = definition.category
                self._shortcut_groups.append((group, []))
            group, rows = self._shortcut_groups[-1]
            editor = QKeySequenceEdit()
            editor.setProperty("action_id", definition.action_id)
            # One chord per action, as osu! binds them; a second key press
            # replaces the first instead of starting a two-step sequence.
            editor.setMaximumSequenceLength(1)
            editor.setClearButtonEnabled(True)
            editor.setFixedWidth(170)
            reset = QToolButton()
            reset.setObjectName("shortcutReset")
            reset.setIcon(QIcon(ui_asset("settings-reset.svg")))
            reset.setIconSize(QSize(14, 14))
            default = QKeySequence(definition.default).toString(QKeySequence.NativeText)
            reset.setToolTip(self.tr("Back to the default: {sequence}").format(sequence=default or "—"))
            reset.setAccessibleName(reset.toolTip())
            reset.clicked.connect(
                lambda _checked=False, e=editor, d=definition.default: e.setKeySequence(QKeySequence(d)))
            # Space held while hidden, so the chips stay in one column.
            policy = reset.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            reset.setSizePolicy(policy)
            def sync_reset(_sequence=None, e=editor, r=reset, d=definition.default) -> None:
                r.setVisible(normalize_sequence(e.keySequence()) != normalize_sequence(d))
            editor.keySequenceChanged.connect(sync_reset)
            # Loading an empty binding over an empty editor emits nothing.
            self._shortcut_syncs.append(sync_reset)
            editor.keySequenceChanged.connect(lambda _sequence: self._validate())
            label = self.tr(definition.label)
            row = group.row(label, self._beside(editor, reset, width=200))
            self._shortcut_editors[definition.action_id] = editor
            rows.append((row, label, editor))
        layout.addWidget(cards, 1)
        self.validation_label = QLabel("")
        self.validation_label.setStyleSheet("color: #ffb347;")
        self.validation_label.setWordWrap(True)
        self.validation_label.setContentsMargins(28, 0, 28, 10)
        self.validation_label.hide()
        layout.addWidget(self.validation_label)
        return page

    def _filter_shortcuts(self, text: str) -> None:
        """Hide actions whose name and keys do not contain `text`; a category
        stays while any action in it does."""
        wanted = text.strip().lower()
        for group, rows in self._shortcut_groups:
            any_shown = False
            for row, label, editor in rows:
                keys = editor.keySequence().toString(QKeySequence.NativeText).lower()
                visible = not wanted or wanted in label.lower() or wanted in keys
                row.setVisible(visible)
                any_shown = any_shown or visible
            group.set_visible(any_shown)

    def _advanced_page(self, title: str) -> QWidget:
        page = _Page(title)
        section = page.group(self.tr("Settings storage location:").rstrip(":"))
        self.storage_value = QLabel(self.settings.storage_name())
        self.storage_value.setObjectName("rowNote")
        self.storage_value.setWordWrap(True)
        self.storage_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        section.wide(self.storage_value)
        self.reset_all_button = QPushButton(self.tr("Reset All Settings"))
        self.reset_all_button.setProperty("role", "ghost")
        self.reset_all_button.setStyleSheet(
            "QPushButton { border: 1px solid #6b3a3a; color: #ff8a8a; }"
            "QPushButton:hover { background: #2a1f24; color: #ffb0b0; }")
        self.reset_all_button.clicked.connect(self._reset_all_settings)
        section.wide(self.reset_all_button, own_width=True)
        return page

    def _load_current_values(self) -> None:
        self.confirm_overwrite.setChecked(self.settings.bool_value("general/confirm_overwrite", True))
        self.transform_animation.setChecked(self.settings.bool_value("appearance/transform_animation", True))
        self.check_updates_on_startup.setChecked(
            self.settings.bool_value(updater.SETTING_CHECK_ON_STARTUP, True)
        )
        self.hitsounds_enabled.setChecked(self.settings.bool_value("audio/hitsounds_enabled", True))
        self.page_click_enabled.setChecked(self.settings.bool_value("audio/page_click", True))
        self.hitsound_volume.setValue(self.settings.int_value("audio/hitsound_volume", 70))
        self.hitsound_offset_ms.setValue(self.settings.int_value(
            "audio/hitsound_offset_ms", DEFAULT_HITSOUND_OFFSET_MS))
        self.music_volume.setValue(self.settings.int_value("audio/music_volume", 65))
        self.output_offset_ms.setValue(self.settings.int_value("audio/output_offset_ms", 0))
        self.note_opacity.setValue(self.settings.int_value(
            "appearance/note_opacity", NOTE_OPACITY_DEFAULT_PERCENT))
        self.background_opacity.setValue(self.settings.int_value("appearance/background_opacity", 25))
        self.view_opacity.setValue(self.settings.int_value("appearance/view_opacity", 100))
        self.song_select_preview.setValue(self.settings.int_value("song_select/preview_percent", 67))
        chosen_theme = self.settings.string_value(theme.SETTING, theme.DEFAULT)
        chosen_theme = theme.RENAMED.get(chosen_theme, chosen_theme)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(chosen_theme)))
        chosen = self.settings.string_value("appearance/skin", "")
        self.skin_combo.setCurrentIndex(max(0, self.skin_combo.findData(chosen)))
        language = self.settings.string_value("language/current", "en")
        index = self.language_combo.findData(language if language in {"en", "ja"} else "en")
        self.language_combo.setCurrentIndex(max(0, index))
        self._set_shortcut_sequences(self.shortcuts.sequence)

    def _set_shortcut_sequences(self, sequence_for) -> None:
        """Fill every chip, then reset-buttons and clash check once each.

        Signals off while filling: each chip's change re-ran the clash check
        over all 48, which is O(n^2) and was 41ms of opening Settings
        (measured 2026-10-11). The end state is the same, since both only
        read the final values.
        """
        for action_id, editor in self._shortcut_editors.items():
            editor.blockSignals(True)
            editor.setKeySequence(QKeySequence(sequence_for(action_id)))
            editor.blockSignals(False)
        for sync in self._shortcut_syncs:
            sync()
        self._validate()

    def _restore_current_page_defaults(self) -> None:
        """Reset whichever page is showing.

        Dispatched on the page's own identifier rather than its position:
        adding the Skin page between Audio and Language silently shifted every
        index below it, so Restore Defaults on Language reset the shortcuts and
        the last page pointed past the end.
        """
        page = self._page_ids[self.pages.currentIndex()]
        if page == "general":
            self.confirm_overwrite.setChecked(True)
            self.transform_animation.setChecked(True)
            self.check_updates_on_startup.setChecked(True)
        elif page == "audio":
            self.hitsounds_enabled.setChecked(True)
            self.page_click_enabled.setChecked(True)
            self.hitsound_volume.setValue(70)
            self.hitsound_offset_ms.setValue(DEFAULT_HITSOUND_OFFSET_MS)
            self.output_offset_ms.setValue(0)
            self.music_volume.setValue(65)
        elif page == "skin":
            self.theme_combo.setCurrentIndex(0)
            self.skin_combo.setCurrentIndex(0)
            self.note_opacity.setValue(NOTE_OPACITY_DEFAULT_PERCENT)
            self.background_opacity.setValue(25)
            self.view_opacity.setValue(100)
            self.song_select_preview.setValue(67)
        elif page == "language":
            self.language_combo.setCurrentIndex(self.language_combo.findData("en"))
        elif page == "shortcuts":
            self._set_shortcut_sequences(self.shortcuts.default_sequence)
        elif page == "advanced":
            QMessageBox.information(self, self.tr("Settings"), self.tr("Use Reset All Settings to clear every saved setting."))

    def _reset_all_settings(self) -> None:
        if QMessageBox.question(self, self.tr("Reset all settings"), self.tr("Clear every saved setting and restore defaults?")) != QMessageBox.Yes:
            return
        self.settings.clear_all()
        self._load_current_values()

    def _shortcut_values(self) -> dict[str, str]:
        return {action_id: normalize_sequence(editor.keySequence()) for action_id, editor in self._shortcut_editors.items()}

    def _validate(self) -> bool:
        """Mark every chip in a clash, and name the first clash under the list."""
        duplicates = duplicate_shortcuts(self._shortcut_values())
        clashing = {action for _sequence, first, second in duplicates for action in (first, second)}
        for action_id, editor in self._shortcut_editors.items():
            line = editor.findChild(QLineEdit)
            if line is not None:
                set_warning(line, action_id in clashing)
        if duplicates:
            sequence, first, second = duplicates[0]
            first_label = self.shortcuts.definitions[first].label
            second_label = self.shortcuts.definitions[second].label
            self.validation_label.setText(self.tr("Shortcut conflict: {sequence} is assigned to {first} and {second}.").format(sequence=sequence, first=first_label, second=second_label))
            self.validation_label.show()
            return False
        self.validation_label.setText("")
        self.validation_label.hide()
        return True

    def apply(self) -> bool:
        if not self._validate():
            # The clash is named on the Shortcuts page, so show it.
            self.nav.setCurrentRow(self._page_ids.index("shortcuts"))
            return False
        previous_language = self.settings.string_value("language/current", "en")
        selected_language = str(self.language_combo.currentData())
        # Already showing: the main window switches it live as it is picked.
        selected_theme = str(self.theme_combo.currentData())
        self.settings.set_value(theme.SETTING, selected_theme)
        self.settings.set_value("general/confirm_overwrite", self.confirm_overwrite.isChecked())
        self.settings.set_value("appearance/transform_animation", self.transform_animation.isChecked())
        self.settings.set_value(
            updater.SETTING_CHECK_ON_STARTUP, self.check_updates_on_startup.isChecked()
        )
        self.settings.set_value("audio/hitsounds_enabled", self.hitsounds_enabled.isChecked())
        self.settings.set_value("audio/page_click", self.page_click_enabled.isChecked())
        self.settings.set_value("audio/hitsound_volume", self.hitsound_volume.value())
        self.settings.set_value("audio/hitsound_offset_ms", self.hitsound_offset_ms.value())
        self.settings.set_value("audio/music_volume", self.music_volume.value())
        self.settings.set_value("audio/output_offset_ms", self.output_offset_ms.value())
        self.settings.set_value("appearance/skin", str(self.skin_combo.currentData()))
        self.settings.set_value("appearance/note_opacity", self.note_opacity.value())
        self.settings.set_value("appearance/background_opacity", self.background_opacity.value())
        self.settings.set_value("appearance/view_opacity", self.view_opacity.value())
        self.settings.set_value("song_select/preview_percent", self.song_select_preview.value())
        self.settings.set_value("language/current", selected_language)
        for action_id, sequence in self._shortcut_values().items():
            self.shortcuts.set_sequence(action_id, sequence)
        self.settings.sync()
        parent = self.parent()
        if parent is not None and hasattr(parent, "_reload_shortcuts"):
            parent._reload_shortcuts()
        if selected_language != previous_language:
            self._prompt_language_restart()
        return True

    def _prompt_language_restart(self) -> None:
        """Ask to restart, in the language just chosen -- not the old one.

        Qt does not retranslate widgets that already exist, so switching the
        catalog here leaves the rest of the running UI in the previous
        language; that is exactly what the restart is for. But the prompt
        itself is built *after* the switch, so someone who has just picked
        Japanese is asked in Japanese rather than in the language they were
        moving away from.
        """
        from app import i18n

        i18n.install_translator(QApplication.instance(), self.settings)
        self._prompt_restart(self.tr("Please restart KatsuDon Editor to apply the language change."))

    def _prompt_restart(self, text: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(self.tr("Restart required"))
        box.setText(text)
        restart_button = box.addButton(self.tr("Restart Now"), QMessageBox.AcceptRole)
        box.addButton(self.tr("Restart Later"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is restart_button:
            self._restart_application()

    def _restart_application(self) -> None:
        """Relaunch this program and quit the current instance.

        startDetached before quit, so the new process is not a child of one
        that is about to exit. A frozen build is its own executable and takes
        only the original arguments; a source run needs the interpreter in
        front of them.
        """
        if getattr(sys, "frozen", False):
            program, arguments = sys.executable, sys.argv[1:]
        else:
            program, arguments = sys.executable, sys.argv
        if not QProcess.startDetached(program, arguments, str(Path.cwd())):
            QMessageBox.warning(
                self,
                self.tr("Restart required"),
                self.tr("Could not restart automatically. Please close and reopen the program."),
            )
            return
        QApplication.quit()

    def _ok(self) -> None:
        if self.apply():
            self.accept()


