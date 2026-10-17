from __future__ import annotations

import gc
import math
import os
import random
import csv
import shutil
import sys
from collections import deque
from contextlib import contextmanager
from itertools import count as count_from
from pathlib import Path
import json
from types import SimpleNamespace
from typing import Any

from PySide6.QtCore import (
    QByteArray, QElapsedTimer, QEvent, QObject, QVariantAnimation, QPoint, QPointF, QPropertyAnimation,
    QRectF, QSize, QStandardPaths, Qt, QEasingCurve, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QActionGroup, QColor, QFont, QFontDatabase, QFontMetrics, QKeySequence, QPainter,
    QPainterPath, QPen, QPixmap, QShortcut, QIcon,
)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractSlider, QAbstractSpinBox, QApplication, QButtonGroup, QComboBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QBoxLayout, QGraphicsOpacityEffect, QGridLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QTextEdit, QSlider, QSpinBox, QStackedWidget, QTabWidget,
    QToolButton, QVBoxLayout, QWidget, QDockWidget, QMenu, QDialog, QDialogButtonBox,
    QFontComboBox, QSizePolicy, QStyle, QStyleOptionFrame,
)

from ui.motion import (
    SEGMENT_GLIDE_STYLE, HoverSheen, PageSlide, PlayPauseGlyph, PressRipple, ReorderDrag,
    SegmentGlide,
)
from ui.smooth_scroll import reduced_motion, smooth
from ui.config_sheet import SEGMENT_STYLE, scrub_label, control_stylesheet, segmented
from audio.audio_engine import DEFAULT_HITSOUND_OFFSET_MS, WHEEL_SEEK_HOLD_MS, TrackPlayer
from library.skin import TaikoSkin, skins_root
from chart.parameters import PARAMETERS
from app.i18n import install_translator, tr
from ui.image_trace_dialog import ImageTraceDialog
from model.commands import (
    CompositeCommand, EditTimingPoint, InsertHitObjects, InsertTimingPoints,
    MoveNotes, RemoveHitObjects, RemoveTimingPoints, SetNoteFields, SetNotePositions,
)
from model.editor_state import DifficultyState
from model.hit_object import HITSOUND_CLAP, HITSOUND_FINISH, HitObject, TYPE_CIRCLE, TYPE_NEW_COMBO, TYPE_SLIDER, TYPE_SPINNER
from app.settings import (
    NOTE_OPACITY_DEFAULT_PERCENT,
    NOTE_OPACITY_MIN_PERCENT,
    APPLICATION_NAME, ORGANIZATION_NAME, SettingsManager, ShortcutDefinition, ShortcutRegistry,
    register_shortcut_definitions, should_ignore_shortcut_focus,
)
from ui.settings_dialog import SettingsDialog
from ui import theme, windose
from app import updater
from osu_io.parser import parse_osu, read_version
from osu_io.timing import (
    EFFECT_KIAI, TimingPoint, active_point_at, active_uninherited_at, kiai_spans,
    sorted_by_time, sv_at, uninherited_points,
)
from osu_io.writer import write_osu
from library.song_library import read_header
from chart import autoplay
from chart.sv_curves import sv_ease
from chart.time_axis import (
    KIAI_OPEN_END_MS, SNAP_DIVISORS, TimeAxisMixin, osu_snap_ms, resnap_time, snap_time,
)
from chart.gimmick_session import (
    FAKE_SLIDER_MAX_LENGTH, FAKE_SLIDER_RESTORE_OFFSET_MS, GimmickConfig, GimmickConfigError,
    GimmickPairing, base_bpm_at, sv_restore_point, gimmick_path_for, gimmick_version_for,
    index_key, difficulty_setting, load_index, looks_gimmicked, oscillating_series,
    carry_active_state, save_index, snapshot_timing,
    anti_barline as gimmick_anti_barline,
    barline_note as gimmick_barline_note,
    fake_slider as gimmick_fake_slider,
    format_length as gimmick_format_length,
    hidden_anti_barline as gimmick_hidden_anti_barline,
    keep_sv as gimmick_keep_sv,
    red_line as gimmick_red_line,
)
from chart.transformer import available_transformations, transform, transform_groups
from ui.common import (
    ACCENT_PINK, DON_COLOR, DRUMROLL_COLOR, KAT_COLOR, SCROLL_MODS, SLIDER_MULTIPLIER_ASSUMED,
    SONG_SELECT_PREVIEW_DEFAULT_PERCENT, application_icon, button_text_width,
    display_frame_interval_ns, draw_note_sprite, drumroll_tick_times, equalize_button_widths,
    format_time, next_frame_due, resource_roots, set_view_ground_alpha, spinner_pixmap, tabular,
    view_ground_is_opaque,
)
from ui.dialogs import (
    AddViewDialog, GimmickEntryDialog, GimmickConfigDialog, ConvertNotesDialog,
    BarlineFunctionDialog, FakeSliderFunctionDialog, MultiFakeSliderDialog, TimingLineDialog,
    SVFunctionDialog,
)
from ui.gameplay_view import GameplayViewerView
from ui.song_select import LibraryPageController
from ui.editor_keys import EditorKeys

from ui.chart_views import (
    DensityOverview, EditorViewFrame, TimelineGameplay, TimingOverviewBar,
    editor_timeline_metadata, extract_timing_points, is_fake_slider, timeline_bar_data,
)

from ui.sv_editor import (
    SVEditorView,
)

PLAYFIELD_WIDTH = 512
PLAYFIELD_HEIGHT = 384

# Ported from osu!(lazer)'s InterpolatingFramedClock.ProcessFrame: every
# *rendered frame* (not every sparse backend report) the displayed clock is
# nudged 1/8 of the way toward the source clock, or snapped to it outright if
# the two have drifted more than AllowableErrorMilliseconds apart. Blending
# every frame instead of only at report time is what makes the correction
# invisible; osu!'s own numbers for both constants are kept rather than
# re-tuned, since they were already chosen for this exact smoothing problem.
#
# The blend is an exponential decay toward the source rather than a fixed
# fraction per frame, matching what upstream does now:
#
#     currentTime = Interpolation.DampContinuously(
#         currentTime, framedSourceClock.CurrentTime,
#         DriftRecoveryHalfLife, realtimeClock.ElapsedFrameTime);
#
# A fixed 1/8 per frame (which this port used first, from an older revision)
# makes the correction speed depend on the frame rate -- the same drift is
# absorbed twice as fast at 120fps as at 60fps. Halving the remaining error
# every DRIFT_RECOVERY_HALF_LIFE_MS of *real* time does not care.
#
# The tolerance is multiplied by the playback rate at the point of use, which
# the first version of this port left out. Upstream does it -- `withinAllowable
# Error = Math.Abs(...) <= AllowableErrorMilliseconds * Rate` -- and the reason
# is in its own doc comment: the constant is a budget in *real* time, so it has
# to be converted into the song time the comparison is made in. Unscaled, 33ms
# of song is 33ms of real drift at 1.0x but 133ms of it at 0.25x, so the one
# speed that most needs a tight playhead got the loosest tolerance of all.
DRIFT_RECOVERY_HALF_LIFE_MS = 80.0
POSITION_ALLOWABLE_ERROR_MS = 1000.0 / 60.0 * 2.0

# The song-folder scan runs on the UI thread, sliced by time rather than by a
# file count: one frame's worth of work per timer tick, so a folder with a few
# fast files and one with thousands of slow ones both stay responsive.
# Placeholder the timeline info line is split on, so the unchanging halves
# either side of the playhead position can be built once and reused. A NUL
# cannot occur in any translated label, which is the whole requirement.
_INFO_POSITION_SLOT = chr(0)

# Tool buttons are a dense row of many small targets, not the app's ordinary
# call-to-action buttons, so they do not take the app-wide 8px/16px padding.
# The vertical half of that was doing nothing but fighting TOOL_BUTTON_HEIGHT,
# which is already fixed, while the horizontal half widened every button in a
# row that has to fit ten of them plus a caption. The Editor and SV rows were
# silently keeping the app-wide value while the gimmick rows overrode it, so
# the three never matched.
#
# Only the properties named here are overridden: a leaf widget's own
# QPushButton{} rule leaves the inherited pink background, hover and checked
# states alone, which is why this is set per button rather than by restyling.
# Measured, not chosen: at the app-wide 8px/16px an Editor tool row is 32px
# tall and 477px wide; these bring it to 26x429 without the labels getting
# cramped. The gimmick rows stay strictly smaller than the Editor row -- they
# carry up to ten tools plus Config, and one shared size would either overflow
# that row or bloat this one.
TOOL_BUTTON_STYLE = "QPushButton { padding: 5px 12px; font-size: 12px; }"
GIMMICK_TOOL_BUTTON_STYLE = "QPushButton { padding: 3px 9px; font-size: 11px; }"

# page_stack / page_button_group indices.
PAGE_LIBRARY, PAGE_EDITOR, PAGE_GIMMICK, PAGE_FANCY = 0, 1, 2, 3

# Points added to the system UI font. Applied to the QApplication font so it
# reaches dialogs and honours DPI scaling, unlike a stylesheet pixel size.
UI_FONT_POINT_BOOST = 2.0


def resolve_song_asset(song_folder: Path, raw_name: str, allowed_suffixes: set[str]) -> Path:
    """Resolve a beatmap asset without allowing absolute paths or .. traversal."""
    name=raw_name.strip().strip('"')
    if not name:
        raise ValueError("Beatmap asset name is empty")
    relative=Path(name)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise ValueError(f"Unsafe beatmap asset path: {raw_name}")
    candidate=(song_folder.resolve()/relative).resolve()
    try:
        candidate.relative_to(song_folder.resolve())
    except ValueError as error:
        raise ValueError(f"Beatmap asset escapes the song folder: {raw_name}") from error
    if candidate.suffix.lower() not in allowed_suffixes:
        raise ValueError(f"Unsupported beatmap asset type: {candidate.suffix}")
    return candidate


# The UI face, from the mockups the owner approved: Exo 2, the face osu! grew
# up with. Bundled (SIL OFL) rather than asked of the system, which usually does
# not have it. It has no kana, so Japanese falls to the system's own faces.
UI_FONT_FILE = Path("assets/fonts/Exo2-VariableFont_wght.ttf")
UI_FONT_FAMILY = "Exo 2"
UI_FONT_FALLBACKS = ("Yu Gothic UI", "Meiryo", "Segoe UI")


_registered_fonts: dict[Path, str | None] = {}


def ui_font_family(font_file: Path = UI_FONT_FILE) -> str | None:
    """Register a bundled face once and return its family, or None when the
    file is missing (a source checkout without assets) -- the system font then
    stays, which is what the app used before."""
    if font_file in _registered_fonts:
        return _registered_fonts[font_file]
    family = None
    for root in resource_roots():
        candidate = root / font_file
        if candidate.is_file():
            font_id = QFontDatabase.addApplicationFont(str(candidate))
            families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
            if families:
                QFont.insertSubstitutions(families[0], list(UI_FONT_FALLBACKS))
                family = families[0]
                break
    _registered_fonts[font_file] = family
    return family


def apply_ui_font() -> None:
    """The application font: the active theme's face (theme.FONTS) or Exo 2,
    at the system font's size two points up. Run at startup and again on a
    theme switch, since a face is part of a theme.

    Two points up because the default reads small on the dense editor pages,
    and raising the application font (rather than a stylesheet px size) keeps
    every dialog and DPI scaling in step. Without the bundled file, the
    system face.
    """
    base_font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    themed = theme.FONTS.get(theme.active())
    if themed:
        # Its bold too, or Qt fakes one wider than the measured labels.
        ui_font_family(Path("assets/fonts") / themed.replace("-Regular", "-Bold"))
    family = ui_font_family(Path("assets/fonts") / themed) if themed else ui_font_family()
    if family:
        base_font.setFamily(family)
        # Exo 2 carries no hinting instructions, and Windows' default is
        # full (GDI-style) hinting, which snaps every glyph to whole pixels:
        # the letters came out unevenly spaced ("m a pp e d"). Vertical
        # hinting keeps baselines crisp and places glyphs fractionally.
        base_font.setHintingPreference(QFont.PreferVerticalHinting)
        # Grayscale, as osu! draws it. Only the FreeType engine honours
        # this -- see main() for why that is the engine.
        base_font.setStyleStrategy(QFont.NoSubpixelAntialias)
    if base_font.pointSizeF() > 0:
        base_font.setPointSizeF(base_font.pointSizeF() + UI_FONT_POINT_BOOST)
    elif base_font.pixelSize() > 0:
        # A pixel-defined system font reports pointSizeF() as -1; adding to
        # that would set a 1pt font instead of a slightly larger one.
        base_font.setPixelSize(base_font.pixelSize() + 3)
    QApplication.instance().setFont(base_font)


def slider_length_for_duration(
    duration_ms: float, beat_length: float, sv: float,
    slider_multiplier: float = SLIDER_MULTIPLIER_ASSUMED,
) -> float:
    """osu!px length that plays for `duration_ms` at the given beat length/SV."""
    if beat_length <= 0 or sv <= 0 or slider_multiplier <= 0:
        return 1.0
    return max(1.0, duration_ms * slider_multiplier * 100.0 * sv / beat_length)


PARAMETERS.setdefault("drawn_path", [{"key":"chunk_size","label":"Notes per Drawing","type":"int","min":2,"max":4096,"default":256},{"key":"reverse","label":"Direction","type":"choice","choices":[("Left to Right",False),("Right to Left",True)],"default":False}])
if not any(item.get("key")=="font_family" for item in PARAMETERS.get("text",[])):
    text_parameters=PARAMETERS.setdefault("text",[])
    insert_at=next((index+1 for index,item in enumerate(text_parameters) if item.get("key")=="text"),0)
    text_parameters.insert(insert_at,{"key":"font_family","label":"Font","type":"font","default":"Segoe UI"})
if not any(item.get("key")=="reverse" for item in PARAMETERS.get("text",[])):
    PARAMETERS["text"].append({"key":"reverse","label":"Direction","type":"choice","choices":[("Left to Right",False),("Right to Left",True)],"default":False})
GUI_TRANSFORMATIONS=[name for name in ("text","drawn_path","equation") if name in PARAMETERS]+[name for name in available_transformations() if name in PARAMETERS and name not in {"text","drawn_path","equation"}]


# The compact +/- pair. The Fancy Arranger's controls build one by hand
# (`ParameterControl` twice, `DifficultyValueControl`) and they agree on
# how it looks: the window stylesheet's 8px/16px is far too much padding for
# a single glyph, and overriding only `padding` and `font-size` leaves the
# inherited pink background, hover and disabled states alone -- a leaf
# widget's own QPushButton{} rule overrides just the properties it names.


def fit_button_width(button: QPushButton) -> None:
    """Pin `button` to the width its own label needs. See button_text_width."""
    button.setFixedWidth(button_text_width(button))


def is_row_visible(layout: QFormLayout | None, widget: QWidget) -> bool:
    field = widget.property("sheetField")
    if field is not None:
        return not field.isHidden()
    # A widget a sheet never placed has no row to show.
    return layout is not None and layout.isRowVisible(widget)


# -- hitsounds -------------------------------------------------------------
#
# Which sample a note asks for. **Circles only.** A drumroll -- which is what
# every fake slider and every shiny note is -- a spinner, and a timing point
# are all silent as objects. A *real* drumroll's ticks are not (owner,
# 2026-10-10): `hitsound_schedule` voices each one as a don, the way the
# autoplay hits it; a fake slider has no duration and so no ticks. That single rule is the whole of "play them wherever normal
# chart notes exist, never for fake sliders or barlines", and it needs no
# special case for the gimmick layers: the hittable note a Don/Kat fake slider
# or a barline note writes *is* an ordinary circle in the chart, and the player
# really does hit it, so silencing it would make playback lie about the map.
HITSOUND_SAMPLES = {
    "normal": "taiko-normal-hitnormal.wav",   # don
    "clap": "taiko-normal-hitclap.wav",       # kat
    "finish": "taiko-normal-hitfinish.wav",   # big don
    "whistle": "taiko-normal-hitwhistle.wav", # big kat
}

def extras_at(note, new_time: float) -> tuple[str, ...]:
    """`note.extras` for the same object moved to `new_time`.

    A spinner keeps its *end* as an absolute millisecond in its extras, so
    retiming only `time` kept the old end: a dragged or pasted spinner came out
    longer or shorter by however far it moved (owner, 2026-10-08: "its length
    is not being copied"). Everything else's extras are relative already.
    """
    end = note.end_time if note.is_spinner else None
    if end is None:
        return tuple(note.extras)
    return (str(round(new_time + (end - note.time))), *note.extras[1:])


def hitsound_keys(note) -> tuple[str, ...]:
    """The samples `note` plays, together; empty when it is silent.

    A finisher is its small note's sound with the big one over it, as in game
    (owner, 2026-10-08): a big don is hitnormal + hitfinish, a big kat hitclap
    + hitwhistle. Alone, the finish was a cymbal with no drum under it.
    """
    if not note.is_circle:
        return ()
    small = "clap" if note.is_kat else "normal"
    if note.is_finisher:
        return (small, "whistle" if note.is_kat else "finish")
    return (small,)


def hitsound_schedule(
    hit_objects, timing_points=(), slider_multiplier: float | None = None,
) -> tuple[list[float], list[str], list[float]]:
    """(times, sample keys, volume fractions) for every audible note, in order.

    Built once per edit and binary-searched per frame. Scanning the hit objects
    on every frame instead would be a full pass over thousands of notes at
    120Hz, which is the cost this file avoids everywhere else it looks
    something up by time.

    The volume is what the Kiai and Sound Volume layer edits, made audible: a
    timing point's `volume` is osu!'s hitsound volume for the section it opens,
    so 100 plays the sample at full and 0 silences it. Resolved by one forward
    merge over the sorted points rather than a lookup per note, because
    `active_point_at` walks backwards a point at a time and a gimmick
    difficulty has thousands of them between any two notes.
    """
    scheduled = [
        (float(note.time), key)
        for note in hit_objects
        for key in hitsound_keys(note)
    ]
    # Drumroll ticks, given the map's SliderMultiplier to derive their end.
    if slider_multiplier is not None:
        ordered_points = sorted_by_time(list(timing_points))
        beat_points = uninherited_points(ordered_points)
        if beat_points:
            for note in hit_objects:
                if note.is_slider:
                    scheduled.extend(
                        (float(time_ms), "normal") for time_ms in drumroll_tick_times(
                            note, beat_points, ordered_points, slider_multiplier))
    scheduled.sort()
    times = [time_ms for time_ms, _key in scheduled]
    keys = [key for _time, key in scheduled]
    ordered = sorted_by_time(list(timing_points))
    volumes = []
    index = 0
    # Before the first timing point there is no section to read a volume from,
    # so a note there plays at full rather than silently.
    current = 1.0
    for time_ms in times:
        while index < len(ordered) and ordered[index].time <= time_ms:
            current = max(0, min(100, int(ordered[index].volume))) / 100.0
            index += 1
        volumes.append(current)
    return times, keys, volumes


class HitsoundPlayer:
    """The chart half of hitsounds: which note wants which sample, how loud,
    and where its file is. The sound itself is mixed into the music stream by
    `audio_engine.HitsoundMixer`.

    **It used to make the sound too**, out of a pool of `QSoundEffect` per
    sample, played when the interpolated playhead crossed a note in
    `_render_gameplay_frame`. Three errors stacked in that: placement quantised
    to a rendered frame, `QSoundEffect`'s own device latency (which is why
    there is a second offset setting at all), and -- largest at slow rates --
    the whole residual of `grain_offset_ms`, because the playhead is a linear
    map over audio that arrives one grain at a time. Mixed into the music
    instead, a note sits on its own millisecond of the song to the sample at
    every playback rate, and shares the music's latency because it is in the
    music's buffer.

    What went with the pools:

    - **The pools themselves.** Four samples times eight effects was 32
      QObjects per window, built lazily because building them eagerly made each
      MainWindow cost more than the last and turned the test suite quadratic.
    - **`pending`/`advance` and their window.** There is no window now: a grain
      voices the notes inside its own 20ms of source and nothing else.
    - **`HITSOUND_MAX_WINDOW_MS`.** The scroll-burst guard -- the one failure
      mode in this program that could injure somebody, a seek firing thirty
      samples on one instant. It is not guarded now, it is impossible:
      `HitsoundMixer.mix` cannot reach outside the grain it was handed.
    - **`playback_rate`.** It existed to scale a wall-time offset into song
      time. The offset is source time now, so the rate has nothing to do.
    """

    def __init__(self, player) -> None:
        self._player = player
        self._enabled = True
        self._offset_ms = 0
        # Remembered so an offset change can re-send without the caller having
        # to know the offset is baked into the schedule -- it is, because the
        # schedule is integer source frames and that is where it belongs.
        self._objects = ()
        self._points = ()
        self._skin_sounds: dict[str, object] = {}
        self._sent_paths: dict | None = None
        self._resolve_samples()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value) -> None:
        self._enabled = bool(value)
        self._player.set_hitsounds_enabled(self._enabled)

    @property
    def offset_ms(self) -> int:
        return self._offset_ms

    @offset_ms.setter
    def offset_ms(self, value) -> None:
        self._offset_ms = int(value)
        self._player.set_hitsound_offset_ms(self._offset_ms)
        # The offset shifts the schedule, so the schedule has to be re-sent.
        self._send_schedule()

    def set_volume(self, fraction: float) -> None:
        self._player.set_hitsound_volume(max(0.0, min(1.0, float(fraction))))

    def set_skin_sounds(self, sounds) -> None:
        if dict(sounds) == self._skin_sounds:
            return
        self._skin_sounds = dict(sounds)
        self._resolve_samples()

    def _resolve_samples(self) -> None:
        """Which file each key plays, resolved per key rather than per skin: a
        skin shipping only a don keeps the built-in kat rather than falling
        silent."""
        paths = {}
        for key, filename in HITSOUND_SAMPLES.items():
            path = self._skin_sounds.get(key)
            if path is None:
                path = next(
                    (
                        candidate for candidate in
                        (root / "assets" / "se" / filename
                         for root in resource_roots())
                        if candidate.is_file()
                    ),
                    None,
                )
            if path is not None:
                paths[key] = str(path)
        if paths == self._sent_paths:
            return
        self._sent_paths = paths
        self._player.set_hitsound_samples(paths)

    def set_schedule(self, hit_objects, timing_points=(), slider_multiplier=None) -> None:
        self._objects = hit_objects
        self._points = timing_points
        self._slider_multiplier = slider_multiplier
        self._send_schedule()

    @property
    def schedule(self):
        """(times, keys, volumes) exactly as the last send computed them.

        Recomputed rather than stored: it is only read to check that an edit
        reached the samples (`test_kiai_sound_layer`), and a stored copy would
        be a second few-thousand-entry list per refresh on a gimmick
        difficulty for the benefit of nothing that plays."""
        return hitsound_schedule(
            self._objects, self._points, getattr(self, "_slider_multiplier", None))

    def _send_schedule(self) -> None:
        self._player.set_hitsound_schedule(*self.schedule)


def in_kiai(timing_points: list[TimingPoint], time_ms: float) -> bool:
    """Whether `time_ms` sits in a kiai section.

    Needs the *full* point list, inherited ones included: kiai is normally
    switched on by a green line.
    """
    point = active_point_at(timing_points, time_ms)
    return point is not None and point.kiai


def paint_note_preview(painter: QPainter, rect, skin: TaikoSkin, percent: float) -> None:
    """Don, kat, finisher, drumroll head and spinner as the editor timeline
    draws them, for the Settings opacity preview.

    The fills carry the same alpha `TimelineGameplay.set_note_opacity` gives
    them (180 at the default, scaled by percent), and the notes go through
    `draw_note_sprite` and `spinner_pixmap`, so a skin or an opacity that
    reads one way here reads that way on the timeline. Radii are the timeline's
    full-height ones (17 / 25).
    """
    alpha = max(1, min(255, round(180 * max(NOTE_OPACITY_MIN_PERCENT, percent) / NOTE_OPACITY_DEFAULT_PERCENT)))
    pen = QPen(QColor(255, 255, 255, 220), 2)
    y = rect.height() / 2
    for index, (colour, radius, big) in enumerate((
        (DON_COLOR, 17, False), (KAT_COLOR, 17, False),
        (DON_COLOR, 25, True), (DRUMROLL_COLOR, 17, False),
    )):
        draw_note_sprite(painter, QColor(*colour, alpha), pen, 40 + index * 62, y, radius, skin, big=big)
    # Spinner: grey band, edge caps, then the shared pixmap, as in the timeline.
    x, end_x, radius = 40 + 4 * 62, 40 + 4 * 62 + 90, 25
    painter.setBrush(QColor(190, 195, 205, max(1, min(255, round(70 * alpha / 180)))))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(QRectF(x, y - radius * 0.5, end_x - x, radius), radius * 0.25, radius * 0.25)
    painter.setPen(QPen(QColor(220, 226, 236, 170), 2))
    for edge_x in (x, end_x):
        painter.drawLine(QPointF(edge_x, y - radius * 0.7), QPointF(edge_x, y + radius * 0.7))
    pixmap = spinner_pixmap()
    if not pixmap.isNull():
        painter.drawPixmap(QRectF(x - radius, y - radius, radius * 2, radius * 2), pixmap, QRectF(pixmap.rect()))


# Visible transformation labels, keyed by the stable internal transformation ID.
# The IDs are what get stored and compared; only the values are ever shown or
# translated. Adding a transformation without an entry here still works and
# falls back to a titled form of its ID.
TRANSFORMATION_LABELS = {
    "text": "Text",
    "drawn_path": "Drawing",
    "equation": "Equation",
    "pinwheel": "Pinwheel",
    "horizontal": "Horizontal",
    "vertical": "Vertical",
    "taiko": "Taiko",
    "vertical_taiko": "Vertical Taiko",
    "dvd_bouncing": "DVD Bouncing",
    "circle": "Circle",
    "ellipse": "Ellipse",
    "square": "Square",
    "triangle": "Triangle",
    "diamond": "Diamond",
    "infinity": "Infinity",
    "star": "Star",
    "spiral": "Spiral",
    "arc": "Arc",
    "straight_line": "Straight Line",
    "polyline": "Polyline",
    "bezier": "Bézier Path",
    "wave": "Wave",
    "zigzag": "Zigzag",
    "random_walk": "Random Walk",
    "random": "Random",
}


# osu!'s gameplay frame in osu!pixels -- the storyboard's space, which the
# background is drawn in -- and the 8px the playfield sits lower than centre
# in it (`OsuPlayfieldAdjustmentContainer`, AlignWithStoryboard).
OSU_FRAME_WIDTH = 640.0
OSU_FRAME_HEIGHT = 480.0
OSU_PLAYFIELD_SHIFT_Y = 8.0


def osu_circle_radius(circle_size: float) -> float:
    """osu!'s hit circle radius in osu!pixels at `circle_size`:
    `54.4 - 4.48 * CS` (32 at CS 5), the value both clients use."""
    return 54.4 - 4.48 * float(circle_size)


def display_name(name: str) -> str:
    label = TRANSFORMATION_LABELS.get(name) or name.replace("_", " ").title()
    return tr("Transformations", label)


def extract_background_filename(document) -> str:
    section = ""
    for line in document.lines:
        content = line.rstrip("\r\n")
        stripped = content.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section != "Events" or not stripped or stripped.startswith("//"):
            continue
        try:
            fields = next(csv.reader([content], skipinitialspace=True))
        except (csv.Error, StopIteration):
            continue
        if len(fields) >= 3 and fields[0].strip() == "0":
            return fields[2].strip().strip('"')
    return ""


def set_document_background(document, filename: str) -> None:
    section = ""
    events_header = None
    for index, line in enumerate(document.lines):
        content = line.rstrip("\r\n")
        stripped = content.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            if section == "Events":
                events_header = index
            continue
        if section != "Events" or not stripped or stripped.startswith("//"):
            continue
        try:
            fields = next(csv.reader([content], skipinitialspace=True))
        except (csv.Error, StopIteration):
            continue
        if len(fields) >= 3 and fields[0].strip() == "0":
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            document.lines[index] = f'0,0,"{filename}",0,0{ending}'
            return
    if events_header is None:
        ending = "\n"
        document.lines.extend([f"{ending}[Events]{ending}", f'0,0,"{filename}",0,0{ending}'])
    else:
        ending = "\r\n" if document.lines[events_header].endswith("\r\n") else "\n"
        document.lines.insert(events_header + 1, f'0,0,"{filename}",0,0{ending}')
        for note in document.hit_objects:
            if note.source_line_index > events_header:
                note.source_line_index += 1


PARAM_STYLE = """
QLabel#paramLabel { color: #aeb8c5; font-size: 12px; font-weight: 600; background: transparent; }
"""


class ParameterControl(QWidget):
    """One transformation parameter, as the approved Fancy Arranger mockup
    draws it: its name and its number on one line, the slider under them.

    With no `label` it is the compact form (slider, then number) for the
    sub-toolbar and anywhere else a caption already sits beside it. The
    number box carries its own step arrows (the window stylesheet's); the
    pink +/- pair beside it is gone, as it is from every other number.
    """

    changed = Signal()

    def __init__(self, definition: dict[str, Any], label: str | None = None) -> None:
        super().__init__(); self.definition = definition
        self.setStyleSheet(PARAM_STYLE)
        self.choice = None; self.slider = None; self.spin = None
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(4)
        head = QHBoxLayout(); head.setContentsMargins(0, 0, 0, 0); head.setSpacing(8)
        self.caption = None
        if label is not None:
            self.caption = QLabel(label); self.caption.setObjectName("paramLabel")
            head.addWidget(self.caption)
            head.addStretch(1)
            outer.addLayout(head)
        kind = definition["type"]
        if kind == "font":
            self.font_combo = QFontComboBox(); self.font_combo.setCurrentFont(QFont(str(definition.get("default", "Segoe UI"))))
            self.font_combo.currentFontChanged.connect(lambda _font: self.changed.emit())
            outer.addWidget(self.font_combo)
            return
        if kind == "text":
            self.text_input = QLineEdit(str(definition.get("default", "")))
            self.text_input.setPlaceholderText(tr("Parameters", "Enter text, for example 67, 日本, or ภาษาไทย"))
            self.text_input.textChanged.connect(self.changed)
            outer.addWidget(self.text_input)
            return
        if kind == "choice":
            self.choice_group = QButtonGroup(self); self.choice_group.setExclusive(True); self.choice_buttons = {}
            buttons = []
            for choice_label, value in definition["choices"]:
                button = QPushButton(tr("Parameters", str(choice_label))); button.setCheckable(True)
                button.setProperty("choice_value", value)
                self.choice_group.addButton(button); self.choice_buttons[value] = button; buttons.append(button)
            self.choice_buttons[definition["default"]].setChecked(True)
            self.choice_group.buttonClicked.connect(self.changed)
            self.pills = pills = segmented(buttons)
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(pills); row.addStretch(1)
            outer.addLayout(row)
            return
        if definition["key"] == "seed" or definition["key"].endswith("_seed"):
            self.scale = 1
            self.spin = QSpinBox()
            self.spin.setRange(int(definition["min"]), int(definition["max"]))
            self.spin.setValue(int(definition.get("default", 0)))
            self.spin.setMinimumWidth(110)
            random_button = QPushButton(tr("Parameters", "Random Seed"))
            random_button.setProperty("role", "ghost")
            random_button.setStyleSheet("QPushButton { border: 1px solid #3a4554; }")
            random_button.clicked.connect(self.randomize)
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(8)
            row.addWidget(self.spin, 1); row.addWidget(random_button)
            outer.addLayout(row)
            self.spin.valueChanged.connect(lambda _value: self.changed.emit())
            if self.caption is not None:
                scrub_label(self.caption, self.spin)
            return
        step = float(definition.get("step", 1 if kind == "int" else .1))
        self.scale = 1 if kind == "int" or step >= 1 else 10 if step >= .1 else 100
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(round(float(definition["min"]) * self.scale), round(float(definition["max"]) * self.scale))
        if kind == "int":
            self.spin = QSpinBox(); self.spin.setRange(int(definition["min"]), int(definition["max"])); self.spin.setSingleStep(int(definition.get("step", 1)))
        else:
            self.spin = QDoubleSpinBox(); self.spin.setRange(float(definition["min"]), float(definition["max"])); self.spin.setSingleStep(step); self.spin.setDecimals(2)
        self.spin.setFixedWidth(96)
        if self.caption is not None:
            # The number sits on the name's line, right-aligned; the slider
            # gets the full width under it, which is what makes it draggable.
            head.addWidget(self.spin)
            outer.addWidget(self.slider)
            scrub_label(self.caption, self.spin)
        else:
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(8)
            row.addWidget(self.slider, 1); row.addWidget(self.spin)
            outer.addLayout(row)
        self.slider.valueChanged.connect(self._from_slider); self.spin.valueChanged.connect(self._from_spin); self.set_value(definition["default"])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        pills = getattr(self, "pills", None)
        if pills is None:
            return
        # A pill row wider than the column squeezed its buttons over one
        # another (Drawing's Direction, 450px of labels in 324px). Stacked,
        # each choice keeps its whole label; tools/check_overlaps.py.
        row = pills.layout()
        side_by_side = row.direction() == QBoxLayout.LeftToRight
        wide = sum(button.minimumWidth() or button.sizeHint().width() for button in self.choice_group.buttons())
        wide += row.spacing() * (len(self.choice_group.buttons()) - 1) + 6
        fits = wide <= event.size().width()
        if fits != side_by_side:
            row.setDirection(QBoxLayout.LeftToRight if fits else QBoxLayout.TopToBottom)

    def _from_slider(self, raw):
        self.spin.blockSignals(True); self.spin.setValue(raw / self.scale); self.spin.blockSignals(False); self.changed.emit()

    def _from_spin(self, value):
        self.slider.blockSignals(True); self.slider.setValue(round(float(value) * self.scale)); self.slider.blockSignals(False); self.changed.emit()

    def randomize(self):
        self.set_value(random.SystemRandom().randint(int(self.definition["min"]), int(self.definition["max"]))); self.changed.emit()

    def set_value(self, value):
        if self.definition["type"] == "font":
            self.font_combo.blockSignals(True); self.font_combo.setCurrentFont(QFont(str(value))); self.font_combo.blockSignals(False); return
        if self.definition["type"] == "text":
            self.text_input.blockSignals(True)
            self.text_input.setText(str(value))
            self.text_input.blockSignals(False)
            return

        if self.definition["type"] == "choice":
            for button in self.choice_group.buttons():
                if button.property("choice_value") == value:
                    button.blockSignals(True)
                    button.setChecked(True)
                    button.blockSignals(False)
                    return
            return

        # Seed controls deliberately have slider=None. Numeric restoration must
        # therefore treat the spin box as mandatory and the slider as optional.
        self.spin.blockSignals(True)
        self.spin.setValue(value)

        if self.slider is not None:
            self.slider.blockSignals(True)
            self.slider.setValue(round(float(value) * self.scale))
            self.slider.blockSignals(False)

        self.spin.blockSignals(False)

    def value(self):
        if self.definition["type"] == "font": return self.font_combo.currentFont().family()
        return self.text_input.text() if self.definition["type"] == "text" else self.choice_group.checkedButton().property("choice_value") if self.definition["type"] == "choice" else self.spin.value()


class DifficultyValueControl(QWidget):
    """AR or CS for the export: its short name, a slider and a number box.

    Was a caption, a 200px slider, a box and a pink +/- pair; the row ran off
    a 1920 screen, and the slider went with the +/- pair. It is back, short,
    because a CS is chosen by watching the circles grow and shrink on the
    canvas rather than by typing a number. The box stays for the exact value
    written to the file; the slider moves in the same 0.01 steps it holds.
    """

    SLIDER_STEPS_PER_UNIT = 100

    changed = Signal(float)

    def __init__(self, label: str, default: float, tooltip: str) -> None:
        super().__init__()
        self.setToolTip(tooltip)
        self.setObjectName("stepper")
        self.setStyleSheet(
            "QWidget#stepper { background: #252d39; border: 1px solid #3a4554; border-radius: 6px; }"
            "QWidget#stepper QLabel { color: #7d8794; font-size: 11px; font-weight: 700; background: transparent;"
            " padding-left: 8px; }"
            "QWidget#stepper QDoubleSpinBox { border: 0; background: transparent; }")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(2)
        caption = QLabel(label); caption.setToolTip(tooltip); layout.addWidget(caption)
        self.value_box = QDoubleSpinBox(); self.value_box.setRange(0.0, 10.0); self.value_box.setDecimals(2)
        self.value_box.setSingleStep(0.1); self.value_box.setFixedWidth(84); self.value_box.setToolTip(tooltip)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, int(10 * self.SLIDER_STEPS_PER_UNIT))
        self.slider.setPageStep(self.SLIDER_STEPS_PER_UNIT)
        # 110 where there is room, and it gives way first on a narrow window:
        # fixed at 110, the two of them pushed Export off a 1366 screen.
        self.slider.setMinimumWidth(56)
        self.slider.setMaximumWidth(110)
        self.slider.setToolTip(tooltip)
        layout.addWidget(self.slider)
        layout.addWidget(self.value_box)
        scrub_label(caption, self.value_box)
        # The box is the one source of truth and the only emitter: the slider
        # writes into it, so a drag fires `changed` once per step, not twice.
        self.slider.valueChanged.connect(
            lambda position: self.value_box.setValue(position / self.SLIDER_STEPS_PER_UNIT))
        self.value_box.valueChanged.connect(self._box_changed); self.set_value(default)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.fit_value_box()

    def fit_value_box(self) -> None:
        """The box as wide as "10.00" needs in the font in force, and never
        narrower than the 84 it was typed at.

        Measured down the chain that clipped it: the box keeps the arrows and
        its border, the sheet's padding comes off the edit inside it, and
        QLineEdit keeps 2px a side and a pixel of cursor -- 36px of a 49px
        edit for NSO's 40px "10.00", which drew as "10.0C" (Exo 2's narrower
        digits fitted). Run once laid out, and again on a theme switch.
        """
        box, edit = self.value_box, self.value_box.lineEdit()
        if edit.width() <= 0:
            return
        option = QStyleOptionFrame()
        edit.initStyleOption(option)
        contents = edit.style().subElementRect(QStyle.SE_LineEditContents, option, edit).width()
        room = contents - 2 * 2 - 1
        need = edit.fontMetrics().horizontalAdvance("10.00")
        box.setFixedWidth(max(84, box.width() + max(0, need - room)))

    def _box_changed(self, value: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(round(value * self.SLIDER_STEPS_PER_UNIT))
        self.slider.blockSignals(False)
        self.changed.emit(float(value))

    def value(self) -> float: return float(self.value_box.value())

    def set_value(self, value: float) -> None:
        self.value_box.setValue(max(0.0, min(10.0, float(value))))


class DrawingSurface(QWidget):
    def __init__(self):
        super().__init__();self.strokes=[];self.active=None;self.redo_strokes=[];self.setMinimumSize(700,450);self.setFocusPolicy(Qt.StrongFocus)
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:
            self.setFocus();self.active=[event.position()];self.strokes.append(self.active);self.redo_strokes.clear();event.accept();self.update()
    def mouseMoveEvent(self,event):
        if self.active is not None and event.buttons() & Qt.LeftButton:
            point=event.position()
            if (point-self.active[-1]).manhattanLength()>=1:self.active.append(point);self.update()
            event.accept()
    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton:
            point=event.position()
            if self.active is not None and (point-self.active[-1]).manhattanLength()>=1:self.active.append(point)
            self.active=None;event.accept();self.update()
    def paintEvent(self,event):
        painter=QPainter(self);painter.fillRect(self.rect(),theme.screen("#11151c"));painter.setRenderHint(QPainter.Antialiasing,True);painter.setPen(QPen(theme.screen("#f3a6bd"),4,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
        for stroke in self.strokes:
            if len(stroke)==1:painter.drawPoint(stroke[0])
            for a,b in zip(stroke,stroke[1:]):painter.drawLine(a,b)
    def sampled_points(self):
        points=[]
        for stroke in self.strokes:
            for point in stroke:
                points.append((max(0.0,min(512.0,point.x()/max(1,self.width())*512.0)),max(0.0,min(384.0,point.y()/max(1,self.height())*384.0))))
        return points
    def clear(self):
        if self.strokes:self.redo_strokes.extend(reversed(self.strokes));self.strokes.clear();self.active=None;self.update()
    def undo(self):
        self.active=None
        if self.strokes:self.redo_strokes.append(self.strokes.pop());self.update()
    def redo(self):
        if self.redo_strokes:self.strokes.append(self.redo_strokes.pop());self.update()


class DrawingDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent);self.accepted_points=[];self.setWindowTitle(tr("DrawingDialog", "Drawing"));self.resize(780,560)
        icon=application_icon()
        if not icon.isNull():self.setWindowIcon(icon)
        layout=QVBoxLayout(self);label=QLabel(tr("DrawingDialog", "Draw one or more strokes. Notes are placed top-to-bottom, then horizontally within each row. Ctrl+Z: undo, Ctrl+Y: redo."));label.setWordWrap(True);layout.addWidget(label)
        self.surface=DrawingSurface();layout.addWidget(self.surface,1)
        row=QHBoxLayout();undo_button=QPushButton(tr("DrawingDialog", "Undo"));redo_button=QPushButton(tr("DrawingDialog", "Redo"));clear_button=QPushButton(tr("DrawingDialog", "Clear"));undo_button.clicked.connect(self.surface.undo);redo_button.clicked.connect(self.surface.redo);clear_button.clicked.connect(self.surface.clear);row.addWidget(undo_button);row.addWidget(redo_button);row.addWidget(clear_button)
        import_button=QPushButton(tr("DrawingDialog", "Import Image..."));import_button.clicked.connect(self.import_image);row.addWidget(import_button)
        layout.addLayout(row)
        self.undo_shortcut=QShortcut(QKeySequence("Ctrl+Z"),self);self.undo_shortcut.setContext(Qt.WidgetWithChildrenShortcut);self.undo_shortcut.activated.connect(self.surface.undo)
        self.redo_shortcut=QShortcut(QKeySequence("Ctrl+Y"),self);self.redo_shortcut.setContext(Qt.WidgetWithChildrenShortcut);self.redo_shortcut.activated.connect(self.surface.redo)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel);buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
    def import_image(self):
        """Trace a local image and add the result as Drawing strokes.

        trace_image already fits its output to the 512x384 playfield, so the
        only conversion needed is playfield -> surface widget coordinates,
        which is the exact inverse of DrawingSurface.sampled_points().

        Strokes are appended rather than replacing the canvas, so an import
        can never silently destroy hand-drawn work.
        """
        dialog=ImageTraceDialog(self)
        # Applied from here rather than inside the dialog: gui imports it, so
        # it cannot import back.
        if dialog.exec()!=QDialog.Accepted:
            return
        strokes=[stroke for stroke in dialog.accepted_strokes if len(stroke)>=2]
        if not strokes:
            return
        width=max(1,self.surface.width());height=max(1,self.surface.height())
        for stroke in strokes:
            self.surface.strokes.append([
                QPointF(x/PLAYFIELD_WIDTH*width, y/PLAYFIELD_HEIGHT*height)
                for x,y in stroke
            ])
        self.surface.active=None;self.surface.redo_strokes.clear();self.surface.update()

    def accept(self):
        points=self.surface.sampled_points()
        if len(points)<2:
            QMessageBox.information(self,tr("DrawingDialog", "Drawing"),tr("DrawingDialog", "Draw at least one stroke before pressing OK."))
            return
        self.accepted_points=points
        super().accept()


def shape_icon(name: str) -> QIcon:
    """A small line drawing of transformation `name`, for its tile.

    Two pixmaps: grey for the tile at rest, pink for the chosen one (QIcon's
    On state, which a checked QToolButton draws). Drawn rather than shipped
    as files so a new transformation needs one branch here, not an asset.
    """
    icon = QIcon()
    for colour, state in (("#aeb8c5", QIcon.Off), ("#f3a6bd", QIcon.On)):
        colour = theme.color(colour).name()
        pixmap = QPixmap(64, 44)
        pixmap.setDevicePixelRatio(2.0)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(colour), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        _draw_shape_glyph(painter, name, colour)
        painter.end()
        icon.addPixmap(pixmap, QIcon.Normal, state)
    return icon


def _draw_shape_glyph(painter: QPainter, name: str, colour: str) -> None:
    """Draw `name` in a 32x22 box (the icon at 1x)."""
    cx, cy = 16.0, 11.0

    def poly(points, closed=True):
        path = QPainterPath(QPointF(*points[0]))
        for point in points[1:]:
            path.lineTo(QPointF(*point))
        if closed:
            path.closeSubpath()
        painter.drawPath(path)

    def curve(fn, steps=48):
        poly([fn(i / steps) for i in range(steps + 1)], closed=False)

    def dot(x, y, r=1.4):
        painter.save()
        painter.setBrush(QColor(colour))
        painter.drawEllipse(QPointF(x, y), r, r)
        painter.restore()

    if name == "circle":
        painter.drawEllipse(QPointF(cx, cy), 8, 8)
    elif name in ("ellipse", "arc"):
        if name == "ellipse":
            painter.drawEllipse(QPointF(cx, cy), 12, 7)
        else:
            curve(lambda t: (cx + 12 * math.cos(math.pi * (1 + t)), cy + 4 + 12 * math.sin(math.pi * (1 + t)) * 0.8))
    elif name == "square":
        painter.drawRect(QRectF(cx - 8, cy - 8, 16, 16))
    elif name == "triangle":
        poly([(cx, 3), (cx + 9, 19), (cx - 9, 19)])
    elif name == "diamond":
        poly([(cx, 2), (cx + 8, cy), (cx, 20), (cx - 8, cy)])
    elif name == "infinity":
        curve(lambda t: (cx + 12 * math.cos(2 * math.pi * t) / (1 + math.sin(2 * math.pi * t) ** 2),
                         cy + 12 * math.sin(2 * math.pi * t) * math.cos(2 * math.pi * t) / (1 + math.sin(2 * math.pi * t) ** 2)))
    elif name == "star":
        points = []
        for k in range(10):
            radius = 9 if k % 2 == 0 else 3.8
            angle = -math.pi / 2 + k * math.pi / 5
            points.append((cx + radius * math.cos(angle), cy + 1 + radius * math.sin(angle)))
        poly(points)
    elif name == "spiral":
        curve(lambda t: (cx + 9 * t * math.cos(6 * math.pi * t), cy + 9 * t * math.sin(6 * math.pi * t)), 80)
    elif name == "straight_line":
        poly([(4, 18), (28, 4)], closed=False)
    elif name == "wave":
        curve(lambda t: (3 + 26 * t, cy - 7 * math.sin(4 * math.pi * t)))
    elif name == "zigzag":
        poly([(3, 16), (9, 6), (15, 16), (21, 6), (27, 16)], closed=False)
    elif name in ("horizontal", "vertical"):
        for row in range(3):
            for column in range(5):
                x, y = 6 + column * 5, 5 + row * 6
                dot(x, y) if name == "horizontal" else dot(10 + row * 6, 2 + column * 4.5)
    elif name in ("taiko", "vertical_taiko"):
        for index in range(3):
            if name == "taiko":
                poly([(4, 5 + index * 6), (28, 5 + index * 6)], closed=False)
            else:
                poly([(9 + index * 7, 2), (9 + index * 7, 20)], closed=False)
        dot(12, 11, 2.2) if name == "taiko" else dot(16, 9, 2.2)
    elif name == "pinwheel":
        for blade in range(4):
            base = blade * math.pi / 2
            curve(lambda t, b=base: (cx + 9 * t * math.cos(b + 1.3 * t), cy + 9 * t * math.sin(b + 1.3 * t)), 16)
    elif name == "dvd_bouncing":
        painter.drawRect(QRectF(3, 2, 26, 18))
        poly([(6, 17), (14, 5), (22, 17), (26, 11)], closed=False)
    elif name == "random_walk":
        poly([(4, 14), (8, 9), (11, 13), (15, 6), (19, 11), (23, 8), (28, 15)], closed=False)
    elif name == "random":
        for x, y in ((6, 6), (13, 15), (19, 5), (25, 13), (9, 18), (22, 19), (28, 6)):
            dot(x, y)
    elif name == "text":
        font = QFont("Segoe UI", 11)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, 32, 22), Qt.AlignCenter, "Aa")
    elif name == "drawn_path":
        path = QPainterPath(QPointF(4, 16))
        path.cubicTo(QPointF(9, 2), QPointF(16, 22), QPointF(28, 5))
        painter.drawPath(path)
    elif name == "equation":
        font = QFont("Segoe UI", 10)
        font.setItalic(True)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, 32, 22), Qt.AlignCenter, "f(x)")
    else:  # none
        painter.drawEllipse(QPointF(cx, cy), 7, 7)
        poly([(cx - 5, cy + 5), (cx + 5, cy - 5)], closed=False)


TILE_GRID_STYLE = """
QToolButton#shapeTile {
    background: #222a36; color: #aeb8c5; border: 1px solid #303947; border-radius: 8px;
    padding: 5px 2px 4px; font-size: 11px; font-weight: 600;
}
QToolButton#shapeTile:hover { background: #2a3341; color: #e8edf3; }
QToolButton#shapeTile:checked { background: #2a2230; color: #ffffff; border: 1px solid #ff66aa; }
QLabel#sectionCaption { color: #7d8794; font-size: 10.5px; font-weight: 700; letter-spacing: 1px; background: transparent; }
QFrame#inspectorCard { background: #1d2431; border: 0; border-radius: 12px; }
QLabel#inspectorTitle { color: #aeb8c5; font-size: 12.5px; font-weight: 600; background: transparent; }
QLabel#shapeNote { color: #aeb8c5; font-size: 12px; background: transparent; }
QLabel#kbd { background: #252d39; border: 1px solid #3a4554; border-bottom: 2px solid #3a4554; border-radius: 3px;
    color: #aeb8c5; font-size: 10.5px; font-weight: 600; padding: 0 4px; }
QLabel#kbdText { color: #7d8794; font-size: 11.5px; background: transparent; }
"""


class TileLabelFitter(QObject):
    """Shape tile labels that fit their tiles: one wider than its tile breaks
    at the space nearest its middle instead of eliding, and every tile takes
    the tallest one's height so the grid stays even.

    "DVD Bouncing" was elided by a pixel in the default theme, and NSO's wider
    pixel face elided four ("Vert...aiko"). Run on a tile's resize: they all
    share one width, and that is the moment the room is known.
    """

    def __init__(self, tiles: list[QToolButton]) -> None:
        super().__init__(tiles[0])
        self.tiles = tiles
        for tile in tiles:
            tile.setProperty("fullText", tile.text())
        tiles[0].installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (QEvent.Resize, QEvent.FontChange, QEvent.StyleChange):
            self.fit()
        return False

    def fit(self) -> None:
        for tile in self.tiles:
            full = str(tile.property("fullText"))
            text = full
            room = tile.contentsRect().width() - 8
            if tile.fontMetrics().horizontalAdvance(full) > room and " " in full:
                spaces = [i for i, ch in enumerate(full) if ch == " "]
                cut = min(spaces, key=lambda i: abs(i - len(full) / 2))
                text = full[:cut] + "\n" + full[cut + 1:]
            if tile.text() != text:
                tile.setText(text)
        height = max(tile.sizeHint().height() for tile in self.tiles)
        for tile in self.tiles:
            if tile.height() != height:
                tile.setFixedHeight(height)


def inspector_card(layout: QVBoxLayout, title: str) -> QVBoxLayout:
    """A titled rounded card, the Settings window's and the trace dialog's:
    a sentence-case heading above, the controls grouped inside."""
    heading = QLabel(title)
    heading.setObjectName("inspectorTitle")
    heading.setContentsMargins(2, 4, 2, 0)
    card = QFrame()
    card.setObjectName("inspectorCard")
    inner = QVBoxLayout(card)
    inner.setContentsMargins(12, 12, 12, 12)
    inner.setSpacing(10)
    layout.addWidget(heading)
    layout.addWidget(card)
    return inner


def shape_description(name: str) -> str:
    """What a transformation does to the notes, in a sentence, under the
    tiles: an icon says which shape, not what happens to a chunk or a seed."""
    descriptions = {
        "": tr("MainWindow", "Notes keep the positions they have now."),
        "text": tr("MainWindow", "Spells your text out in notes. Each chunk of notes writes the text once."),
        "drawn_path": tr("MainWindow", "Places notes along a shape you draw, or trace from a picture, in the Drawing window."),
        "equation": tr("MainWindow", "Places notes along the graph of an equation you type, such as y = sin(x)."),
        "pinwheel": tr("MainWindow", "Curled blades spinning out from a centre, with an optional ring of notes inside."),
        "horizontal": tr("MainWindow", "Rows across the playfield, each filled before the next one starts."),
        "vertical": tr("MainWindow", "Columns down the playfield, each filled before the next one starts."),
        "taiko": tr("MainWindow", "Rows of a fixed number of beats, notes spaced by their real timing, like a taiko lane."),
        "vertical_taiko": tr("MainWindow", "The Taiko layout on its side: columns of fixed beats, spaced by timing."),
        "dvd_bouncing": tr("MainWindow", "Travels in straight lines and bounces off the playfield edges, like the DVD logo."),
        "circle": tr("MainWindow", "Notes evenly spaced around a circle. Each chunk of notes draws one circle."),
        "ellipse": tr("MainWindow", "Notes evenly spaced around an ellipse. Each chunk of notes draws one ellipse."),
        "square": tr("MainWindow", "Notes evenly spaced around a square. Each chunk of notes draws one square."),
        "triangle": tr("MainWindow", "Notes evenly spaced around a triangle. Each chunk of notes draws one triangle."),
        "diamond": tr("MainWindow", "Notes evenly spaced around a diamond. Each chunk of notes draws one diamond."),
        "infinity": tr("MainWindow", "Notes around a figure eight. Each chunk of notes draws one loop of it."),
        "star": tr("MainWindow", "Notes around a star, in and out between its points. Each chunk draws one star."),
        "spiral": tr("MainWindow", "Notes winding outward from the start radius to the end radius."),
        "arc": tr("MainWindow", "Notes along part of an ellipse, from the start angle through the sweep."),
        "straight_line": tr("MainWindow", "Notes evenly spaced along one straight line."),
        "wave": tr("MainWindow", "Notes along a sine wave: width, height and number of cycles are yours."),
        "zigzag": tr("MainWindow", "Notes along a zigzag of sharp turns."),
        "random_walk": tr("MainWindow", "Each note steps on from the last in a slowly turning direction, bouncing off the edges."),
        "random": tr("MainWindow", "Every note at a random spot. The same seed brings the same layout back."),
    }
    return descriptions.get(name, "")


def section_caption(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("sectionCaption")
    return label


def key_hint(keys: list[str], text: str) -> QWidget:
    """"[Shift] + [Wheel] 1 beat": key caps, so a hint reads as the keys."""
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(3)
    for index, key in enumerate(keys):
        if index:
            plus = QLabel("+")
            plus.setObjectName("kbdText")
            row.addWidget(plus)
        cap = QLabel(key)
        cap.setObjectName("kbd")
        cap.setFixedHeight(18)
        cap.setAlignment(Qt.AlignCenter)
        row.addWidget(cap, 0, Qt.AlignVCenter)
    words = QLabel(text)
    words.setObjectName("kbdText")
    row.addSpacing(3)
    row.addWidget(words, 0, Qt.AlignVCenter)
    box.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return box


class TransformCanvas(QWidget):
    background_dropped = Signal(str)
    drag_offset_requested = Signal(object, float, float)
    def __init__(self) -> None:
        super().__init__()

        self.notes = []
        self.positions: dict[int, tuple[int, int]] = {}
        self.selected: set[int] = set()
        self.drag_last_position = None
        self.drag_group = None
        self.view_scale = 1.0
        self.view_offset_x = 0.0
        self.view_offset_y = 0.0
        self.render_rect = QRectF()
        self.playfield_rect = QRectF()

        self.background_pixmap = QPixmap()
        self.background_opacity = 0.55
        # Where each note is drawn while it glides from its old place to
        # `positions` (the target, which is what hit tests and callers read).
        # Seeing where each note goes is the point of the page.
        self._glide_from: dict[int, tuple[float, float]] = {}
        self._glide_t = 1.0
        self._glide = QVariantAnimation(self)
        self._glide.setStartValue(0.0)
        self._glide.setEndValue(1.0)
        self._glide.setDuration(260)
        self._glide.setEasingCurve(QEasingCurve.OutCubic)
        self._glide.valueChanged.connect(self._glide_step)
        # The export's CS, which sizes the circles (osu_circle_radius).
        self.circle_size = 7.0
        # The Settings switch; reduced_motion() is the OS's, this is the user's.
        self.animate_transforms = True
        self.setAcceptDrops(True)
        # Small enough for a half-screen window; the docks around it take what
        # is left, and the 16:9 frame letterboxes inside whatever this gets.
        self.setMinimumHeight(160)

    def set_state(
        self,
        notes,
        positions: dict[int, tuple[int, int]],
        selected: set[int],
    ) -> None:
        # A new target while the old one is still being reached starts from
        # where the notes are drawn now, so a slider dragged across its range
        # never makes them jump. Not while a note is being dragged by hand:
        # there the note follows the pointer, and easing it would lag behind.
        moved = positions is not self.positions and positions != self.positions
        if (moved and self.drag_last_position is None and self.animate_transforms
                and not reduced_motion() and self.isVisible()):
            self._glide_from = {
                note.original_index: self._drawn_position(note) for note in self.notes
            }
            self._glide_t = 0.0
            self._glide.stop()
            self._glide.start()
        self.notes = notes
        self.positions = positions
        self.selected = set(selected)
        self.update()

    def _glide_step(self, value) -> None:
        self._glide_t = float(value)
        self.update()

    def _drawn_position(self, note) -> tuple[float, float]:
        target = self.positions.get(note.original_index, (note.x, note.y))
        if self._glide_t >= 1.0:
            return target
        start = self._glide_from.get(note.original_index, target)
        t = self._glide_t
        return (start[0] + (target[0] - start[0]) * t, start[1] + (target[1] - start[1]) * t)

    def set_background(self, image_path: str | None) -> None:
        if image_path and Path(image_path).is_file():
            self.background_pixmap = QPixmap(image_path)
        else:
            self.background_pixmap = QPixmap()
        self.update()

    def set_background_opacity(self, value: int) -> None:
        self.background_opacity = max(0.0, min(1.0, value / 100.0))
        self.update()

    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if any(Path(url.toLocalFile()).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"} for url in urls):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            candidate = Path(url.toLocalFile())
            if candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"} and candidate.is_file():
                self.background_dropped.emit(str(candidate))
                event.acceptProposedAction()
                return

    def _update_view_geometry(self) -> None:
        # Fixed 16:9 preview, letterboxed inside the widget.
        available = self.rect().adjusted(12, 12, -12, -12)
        render_width = float(max(1, available.width()))
        render_height = render_width * 9.0 / 16.0
        if render_height > available.height():
            render_height = float(max(1, available.height()))
            render_width = render_height * 16.0 / 9.0
        self.render_rect = QRectF(
            available.center().x() - render_width / 2.0,
            available.center().y() - render_height / 2.0,
            render_width,
            render_height,
        )

        # osu!'s own placement, not the 4:3 playfield stretched to the full
        # height as it was -- which drew every pattern 25% larger than osu!
        # shows it over the same background. `OsuPlayfieldAdjustmentContainer`:
        # the 512x384 playfield is 80% of a 4:3 area (640x480 in osu!pixels,
        # the storyboard's own space), centred, and shifted down 8 osu!pixels
        # to line up with the storyboard -- osu!stable's (64, 56).
        frame_height = self.render_rect.height()
        frame_width = frame_height * 4.0 / 3.0
        if frame_width > self.render_rect.width():
            frame_width = self.render_rect.width()
            frame_height = frame_width * 3.0 / 4.0
        scale = frame_height / OSU_FRAME_HEIGHT
        frame_left = self.render_rect.center().x() - frame_width / 2.0
        frame_top = self.render_rect.center().y() - frame_height / 2.0
        self.playfield_rect = QRectF(
            frame_left + (OSU_FRAME_WIDTH - PLAYFIELD_WIDTH) / 2.0 * scale,
            frame_top + ((OSU_FRAME_HEIGHT - PLAYFIELD_HEIGHT) / 2.0 + OSU_PLAYFIELD_SHIFT_Y) * scale,
            PLAYFIELD_WIDTH * scale,
            PLAYFIELD_HEIGHT * scale,
        )

        self.view_scale = self.playfield_rect.width() / PLAYFIELD_WIDTH
        self.view_offset_x = self.playfield_rect.left()
        self.view_offset_y = self.playfield_rect.top()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_view_geometry()
        self.update()

    def paintEvent(self, event) -> None:
        # First, not after the border: the border and the background read the
        # geometry too, and drew with the previous size's until the notes
        # asked for it again.
        self._update_view_geometry()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), theme.screen("#11151c"))
        backdrop = self._scaled_background()
        if backdrop is not None:
            painter.drawPixmap(self.render_rect.topLeft(), backdrop)
        painter.setPen(QPen(theme.screen("#465164"), 1))
        painter.drawRect(
            self.playfield_rect
        )

        radius = self._drawn_radius()
        view_scale = self.view_scale
        offset_x = self.view_offset_x
        offset_y = self.view_offset_y
        # A note is one of eight looks (kat/don x selected x finisher), so each
        # is rasterised once per radius and stamped. Anti-aliased ellipses cost
        # ~50us apiece and a dense map is thousands of them per glide frame.
        # Stamped in note order rather than batched by colour: overlapping
        # notes of different colours would swap which one is on top.
        selected = self.selected
        half = self._sprite_size(radius) / 2.0
        # Off-canvas notes (a transform can fling them anywhere) cost nothing.
        visible = self.rect().adjusted(-int(half), -int(half), int(half), int(half))
        for note in self.notes:
            position = self._drawn_position(note)
            x = offset_x + position[0] * view_scale
            y = offset_y + position[1] * view_scale
            if not visible.contains(int(x), int(y)):
                continue
            sprite = self._note_sprite(note.is_kat, note.original_index in selected, note.is_finisher, radius)
            painter.drawPixmap(QPointF(x - half, y - half), sprite)

    _KAT_BRUSH = QColor("#4aa3ff")
    _DON_BRUSH = QColor("#ff4f5e")
    _NOTE_PEN_WIDTH = 2
    _SELECTED_PEN = QPen(QColor("#ffd166"), _NOTE_PEN_WIDTH)
    _NORMAL_PEN = QPen(QColor("#f4f7fb"), _NOTE_PEN_WIDTH)

    def _sprite_size(self, radius: float) -> int:
        # The circle plus half the pen on each side, and a pixel of
        # anti-aliasing margin.
        return int(2 * radius + self._NOTE_PEN_WIDTH) + 2

    def _note_sprite(self, is_kat: bool, is_selected: bool, is_finisher: bool, radius: float):
        ratio = self.devicePixelRatioF()
        key = (round(radius, 2), ratio)
        if getattr(self, "_sprite_key", None) != key:
            self._sprite_key = key
            self._sprites: dict[tuple[bool, bool, bool], QPixmap] = {}
        look = (is_kat, is_selected, is_finisher)
        sprite = self._sprites.get(look)
        if sprite is None:
            size = self._sprite_size(radius)
            sprite = QPixmap(int(size * ratio), int(size * ratio))
            sprite.setDevicePixelRatio(ratio)
            sprite.fill(Qt.transparent)
            stamper = QPainter(sprite)
            stamper.setRenderHint(QPainter.Antialiasing, True)
            stamper.setBrush(self._KAT_BRUSH if is_kat else self._DON_BRUSH)
            stamper.setPen(self._SELECTED_PEN if is_selected else self._NORMAL_PEN)
            centre = QPointF(size / 2.0, size / 2.0)
            stamper.drawEllipse(centre, radius, radius)
            if is_finisher:
                stamper.setBrush(Qt.NoBrush)
                stamper.drawEllipse(centre, radius * 0.72, radius * 0.72)
            stamper.end()
            self._sprites[look] = sprite
        return sprite

    def _drawn_radius(self) -> float:
        # osu!'s circle at the export's CS, to scale with the playfield. Floored
        # at the pen width and a half: CS 10 is 9.6 osu!px, which on the 160px
        # minimum canvas is a radius the 2px outline alone would swallow, and a
        # note that is all outline is not visibly a note.
        return max(osu_circle_radius(self.circle_size) * self.view_scale, self._NOTE_PEN_WIDTH * 1.5)

    def _scaled_background(self):
        """The cropped, scaled, faded background, or None without one.

        Cached because the glide repaints 60 times a second and each paint was
        cropping and scaling a full-size image with opacity applied. Rebuilt
        only when the size, the image or the opacity changes.
        """
        if self.background_pixmap.isNull():
            return None
        size = self.render_rect.size().toSize()
        if size.isEmpty():
            return None
        ratio = self.devicePixelRatioF()
        key = (size.width(), size.height(), ratio, self.background_pixmap.cacheKey(), self.background_opacity)
        if getattr(self, "_background_cache_key", None) != key:
            source_width = self.background_pixmap.width()
            source_height = self.background_pixmap.height()
            source_ratio = source_width / max(1, source_height)
            if source_ratio > 16 / 9:
                crop_width = source_height * 16 / 9
                source = QRectF((source_width - crop_width) / 2, 0, crop_width, source_height)
            else:
                crop_height = source_width * 9 / 16
                source = QRectF(0, (source_height - crop_height) / 2, source_width, crop_height)
            baked = QPixmap(size * ratio)
            baked.setDevicePixelRatio(ratio)
            baked.fill(Qt.transparent)
            baker = QPainter(baked)
            baker.setRenderHint(QPainter.SmoothPixmapTransform, True)
            baker.setOpacity(self.background_opacity)
            baker.drawPixmap(QRectF(0, 0, size.width(), size.height()), self.background_pixmap, source)
            baker.end()
            self._background_cache = baked
            self._background_cache_key = key
        return self._background_cache

    def _note_at_canvas_position(self, position):
        self._update_view_geometry()
        if self.view_scale <= 0:
            return None
        playfield_x = (position.x() - self.view_offset_x) / self.view_scale
        playfield_y = (position.y() - self.view_offset_y) / self.view_scale
        best_note = None
        best_distance = float("inf")
        for note in self.notes:
            if note.original_index not in self.selected:
                continue
            x, y = self.positions.get(note.original_index, (note.x, note.y))
            distance = (x - playfield_x) ** 2 + (y - playfield_y) ** 2
            if distance < best_distance:
                best_distance = distance
                best_note = note
        # The circle as drawn, in playfield units, so what is grabbable is
        # what is seen (a fixed 60 was bigger than any CS but 0).
        hit_radius = self._drawn_radius() / self.view_scale
        return best_note if best_note is not None and best_distance <= hit_radius ** 2 else None

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        note = self._note_at_canvas_position(event.position())
        if note is None:
            return
        self.drag_group = "kat" if note.is_kat else "don"
        self.drag_last_position = event.position()
        self.setCursor(Qt.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_last_position is None or self.view_scale <= 0:
            return
        delta = event.position() - self.drag_last_position
        self.drag_last_position = event.position()
        self.drag_offset_requested.emit(
            self.drag_group,
            delta.x() / self.view_scale,
            delta.y() / self.view_scale,
        )
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self.drag_last_position is None:
            return
        self.drag_last_position = None
        self.drag_group = None
        self.unsetCursor()
        event.accept()


# A bound is pushed outward once a value gets within this ratio of it, rather
# than only once it crosses -- a line drawn hard against the edge of the graph
# is already unreadable.
SV_BOUND_NEAR = 1.05
# ...and pulled back in only once the visible range fits this much inside the
# current bound. Without the gap, scrolling across a section that straddles a
# ladder step would retune the axis on alternate frames.
SV_BOUND_SLACK = 1.3


# --- Mods, from ppy/osu's own TaikoMod* (read 2026-09-24) ------------------
# Every string that lists mods uses this order, which is osu!'s: HD, the rate
# mod, HR/EZ, FL -- "HDNCHR", never "NCHDHR".
MOD_ORDER = ("HD", "NC", "DT", "DC", "HT", "HR", "EZ", "FL")
# Rate mods: (song rate, whether the song's pitch moves with it). DT/HT are
# osu!'s AdjustableProperty.Tempo (pitch kept, the same WSOLA the speed buttons
# use); NC/DC are Frequency (resampled, the song's pitch follows the rate). The
# hitsounds keep their own pitch under all four -- the owner's call.
RATE_MODS = {"NC": (1.5, True), "DT": (1.5, False), "DC": (0.75, True), "HT": (0.75, False)}
# The pill row's layout, and only that: scroll, visibility, rate.
MOD_PILL_GROUPS = (("HR", "EZ"), ("HD", "FL"), ("DT", "NC", "HT", "DC"))
# Hard-coded, like the note colours: a mod is the same colour in every theme,
# and none of these is a pink-palette key `theme` would move. Red and green
# are osu!'s own mod-select colours (DifficultyIncrease / Reduction).
MOD_COLORS = {
    "HD": "#ffc801", "FL": "#85858f", "HR": "#fe6465",
    "DT": "#8c65ff", "NC": "#5b5fe0",
    "EZ": "#b0ff65", "HT": "#b0ff65", "DC": "#b0ff65",
}
# A mod switched on fills with its colour, except where white on that colour
# cannot be read (owner, 2026-10-08): osu!'s green is 1.2:1 against white, so
# a lit EZ/HT/DC fills with the same green darkened to 4.6:1. Off, the pill
# keeps osu!'s own green tint.
MOD_FILLS = {"EZ": "#4a8413", "HT": "#4a8413", "DC": "#4a8413"}
# FL was #2c2c30, which against the ground is no colour at all: it is light
# gray now, and that old gray is what a hover shows (owner, 2026-10-08).
MOD_HOVERS = {"FL": "#2c2c30"}


def mod_pill_style(mod: str) -> str:
    """One mod pill: tinted with its colour while off, filled with it while on.
    White text both ways (owner's call). Set on the button itself, so it wins
    over the segment's shared pink `:checked`."""
    colour = MOD_COLORS[mod]
    # The segment's own ground, as the active theme draws it, so the tint
    # sits on the same colour in every theme.
    base = theme.color("#252d39")
    own = QColor(colour)
    def mix(amount: float) -> str:
        return QColor(
            round(base.red() + (own.red() - base.red()) * amount),
            round(base.green() + (own.green() - base.green()) * amount),
            round(base.blue() + (own.blue() - base.blue()) * amount),
        ).name()
    # White on a light theme's faint tint was unreadable: there an unlit
    # pill takes the theme's text, and only the lit one stays white.
    text = "#ffffff" if base.lightness() < 128 else theme.color("#e8edf3").name()
    return (
        f"QPushButton {{ background: {mix(0.16)}; color: {text};"
        f" border-bottom: 2px solid {mix(0.7)}; }}"
        f"QPushButton:hover {{ background: {MOD_HOVERS.get(mod) or mix(0.32)}; color: {text}; }}"
        f"QPushButton:checked {{ background: {MOD_FILLS.get(mod, colour)}; color: #ffffff; font-weight: 700;"
        " border-bottom: 2px solid #ffffff; }"
    )


def mod_string(mods) -> str:
    """`mods` as osu! writes them: "HDDT", in MOD_ORDER, "" for none."""
    return "".join(mod for mod in MOD_ORDER if mod in mods)


def freeze_preview_value(value):
    """Convert nested preview parameters into stable, hashable cache data."""
    if isinstance(value, dict):
        return tuple(sorted((str(key), freeze_preview_value(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze_preview_value(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(freeze_preview_value(item) for item in value))
    return value

class SnapSlider(QWidget):
    """Beat snap as a slider over SNAP_DIVISORS: "Snap", the track, "1/4".

    It stands in for the QComboBox each editor page had, so it answers the
    handful of calls those combos got -- currentData, findData,
    setCurrentIndex, currentIndexChanged -- and none of their callers change.
    A combo hid 22 divisors behind a click; the slider shows where the current
    one sits among them, with the ones people use labelled underneath.

    The wheel is the slider's own (`_page_wheel_target` leaves every
    QAbstractSlider alone), which is what the combo's wheel did too.
    """

    currentIndexChanged = Signal(int)

    # The divisors labelled under the track, and clickable. Not 2, 3 and 6:
    # 1-16 are consecutive steps about 10px apart, so their labels ran into
    # one another as "1234". Every divisor is still a drag or a notch away.
    TICKS = (1, 4, 8, 12, 16, 32, 64)
    HANDLE = 14
    STYLE = f"""
        QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: #3a4554; }}
        QSlider::sub-page:horizontal {{ border-radius: 2px; background: #ff66aa; }}
        QSlider::handle:horizontal {{
            width: {HANDLE}px; height: {HANDLE}px; margin: -5px 0; border-radius: 7px;
            background: {ACCENT_PINK}; border: 1px solid #ff9dcc;
        }}
    """

    def __init__(self, divisor: int = 4, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.divisors = tuple(SNAP_DIVISORS)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(0)
        caption = QLabel(tr("MainWindow", "Snap"))
        caption.setStyleSheet("color: #7d8794; font-size: 11px; font-weight: 700;")
        layout.addWidget(caption, 0, 0, 2, 1)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, len(self.divisors) - 1)
        self.slider.setPageStep(1)
        self.slider.setFixedWidth(220)
        self.slider.setStyleSheet(self.STYLE)
        self.slider.setFocusPolicy(Qt.NoFocus)
        layout.addWidget(self.slider, 0, 1)
        self.ticks = _SnapTicks(self)
        layout.addWidget(self.ticks, 1, 1)
        self.value_label = QLabel()
        self.value_label.setStyleSheet(f"color: {ACCENT_PINK}; font-weight: 700;")
        # Wide enough for "1/64", so the track does not shift as it changes.
        self.value_label.setMinimumWidth(self.value_label.fontMetrics().horizontalAdvance("1/64") + 6)
        layout.addWidget(self.value_label, 0, 2, 2, 1)
        self.slider.valueChanged.connect(self._slider_moved)
        self.slider.valueChanged.connect(self.ticks.update)
        self.setCurrentIndex(max(0, self.findData(divisor)))

    def _slider_moved(self, index: int) -> None:
        self.value_label.setText(f"1/{self.divisors[index]}")
        self.slider.setToolTip(tr("MainWindow", "Beat snap") + f" 1/{self.divisors[index]}")
        self.currentIndexChanged.emit(index)

    def wheelEvent(self, event) -> None:
        # Over the caption, the ticks or the value, as over the track: one
        # notch is one divisor, the way the combo's wheel was.
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            self.slider.setValue(self.slider.value() + (1 if delta > 0 else -1))
        event.accept()

    def currentIndex(self) -> int:
        return self.slider.value()

    def currentData(self) -> int:
        return self.divisors[self.slider.value()]

    def count(self) -> int:
        return len(self.divisors)

    def findData(self, divisor) -> int:
        try:
            return self.divisors.index(int(divisor))
        except (ValueError, TypeError):
            return -1

    def setCurrentIndex(self, index: int) -> None:
        # blockSignals on this widget still silences a caller's "quiet" set,
        # as it did on the combo: the slider moves, but currentIndexChanged is
        # emitted by this widget, and that is the signal that is blocked.
        self.slider.setValue(index)
        # The label has to exist on the first call too, when nothing moved.
        self.value_label.setText(f"1/{self.divisors[index]}")

    def tick_x(self, index: int) -> float:
        """Where the handle's centre sits for `index`, in slider coordinates."""
        span = self.slider.width() - self.HANDLE
        return self.HANDLE / 2 + span * index / max(1, len(self.divisors) - 1)


class _SnapTicks(QWidget):
    """The labelled divisors under a SnapSlider's track. Click one to pick it."""

    def __init__(self, owner: SnapSlider) -> None:
        super().__init__(owner)
        self.owner = owner
        font = QFont(self.font())
        font.setPixelSize(10)
        self.setFont(font)
        self.setFixedHeight(QFontMetrics(font).height())
        self.setCursor(Qt.PointingHandCursor)

    def _positions(self):
        for divisor in SnapSlider.TICKS:
            index = self.owner.findData(divisor)
            if index >= 0:
                yield divisor, index, self.owner.tick_x(index)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setPen(theme.color("#7d8794"))
        metrics = painter.fontMetrics()
        current = self.owner.currentData()
        for divisor, _index, x in self._positions():
            text = str(divisor)
            if divisor == current:
                painter.setPen(theme.color("#e8edf3"))
            painter.drawText(QPointF(x - metrics.horizontalAdvance(text) / 2, metrics.ascent()), text)
            if divisor == current:
                painter.setPen(theme.color("#7d8794"))

    def mousePressEvent(self, event) -> None:
        x = event.position().x()
        nearest = min(self._positions(), key=lambda item: abs(item[2] - x), default=None)
        if nearest is not None:
            self.owner.slider.setValue(nearest[1])
            self.update()


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)

        self._full_text = text
        # Plain, never auto-detected rich text: what this shows comes from
        # .osu files (names, difficulty, artist), and a QLabel on AutoText
        # renders a difficulty called "<img src=...>" as markup.
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        self.setMinimumWidth(0)
        self.setToolTip(text)

    def setText(self, text: str) -> None:
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._update_elided_text()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_elided_text()

    def _update_elided_text(self) -> None:
        available_width = max(0, self.width() - 4)

        elided = self.fontMetrics().elidedText(
            self._full_text,
            Qt.ElideRight,
            available_width,
        )

        super().setText(elided)


BACKDROP_OPACITY_DEFAULT_PERCENT = 25

# Settings > View opacity: how much of each editor view's own ground covers
# the page's map backdrop. 100 is the old look and the cheap path -- the lanes
# stay WA_OpaquePaintEvent, so the backdrop is never repainted under them.
VIEW_OPACITY_DEFAULT_PERCENT = 100
def set_view_opacity(percent: int, widgets) -> None:
    """Apply View opacity to `widgets` (lanes and frames) and to every one
    built after this. Live: Settings calls it while its slider moves."""
    set_view_ground_alpha(percent)
    for widget in widgets:
        if not getattr(widget, "follows_view_opacity", True):
            continue
        if isinstance(widget, EditorViewFrame):
            widget.refresh_ground()
        else:
            widget.setAttribute(Qt.WA_OpaquePaintEvent, view_ground_is_opaque())
            widget.update()


class MapBackdrop(QWidget):
    """A page's floor: the window's navy with the active map's background
    faded into it, cropped to fill.

    One per page rather than one per lane, so ten open difficulties cost
    nothing extra. The faded image is baked into a single cached pixmap and
    rebuilt only when the size, the picture or the opacity changes, so a paint
    is one blit -- and the lanes that sit on it are opaque and say so
    (WA_OpaquePaintEvent), which keeps their per-frame repaints from reaching
    it at all.
    """

    @staticmethod
    def ground() -> QColor:
        # Read as it paints, so a theme switch reaches it (theme.switch).
        # Through `screen`: NSO's editor floor is its pastel pink, and the
        # sticker wallpaper is the Songs page's alone (owner, 2026-10-12).
        return theme.screen("#191f29")

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("mapBackdrop")
        self._path: Path | None = None
        self._source: QPixmap | None = None
        self._percent = BACKDROP_OPACITY_DEFAULT_PERCENT
        self._baked: QPixmap | None = None

    def set_background(self, path: Path | None) -> None:
        if path == self._path:
            return
        self._path = path
        source = QPixmap(str(path)) if path is not None else None
        self._source = source if source is not None and not source.isNull() else None
        self._baked = None
        self.update()

    def set_opacity(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        if percent != self._percent:
            self._percent = percent
            self._baked = None
            self.update()

    def _bake(self) -> QPixmap:
        size = self.size()
        baked = QPixmap(size)
        baked.fill(self.ground())
        painter = QPainter(baked)
        if self._source is not None and self._percent > 0:
            cover = self._source.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            painter.setOpacity(self._percent / 100.0)
            painter.drawPixmap((size.width() - cover.width()) // 2, (size.height() - cover.height()) // 2, cover)
        painter.end()
        return baked

    def paintEvent(self, event) -> None:
        if self._source is None or self._percent == 0:
            QPainter(self).fillRect(event.rect(), self.ground())
            return
        if self._baked is None or self._baked.size() != self.size():
            self._baked = self._bake()
        QPainter(self).drawPixmap(event.rect(), self._baked, event.rect())


def see_through(*widgets: QWidget) -> None:
    """Let a page's backdrop show through these chrome containers.

    The window's `QWidget { background }` rule paints every plain widget
    opaque, so the ones between a page and its lanes opt out by property (see
    the `seeThrough` rule in MainWindow's stylesheet) rather than the rule
    being loosened for everything. A scroll area's container is also filled by
    QScrollArea itself, which the stylesheet does not reach.
    """
    for widget in widgets:
        widget.setProperty("seeThrough", True)
        widget.setAutoFillBackground(False)


def opaque_strip(strip: QHBoxLayout) -> QWidget:
    """The page's snap / time / timing bar / transport / mods row, on the app's
    own ground rather than the map's backdrop.

    As a bare layout it sat directly on the backdrop, and the backdrop rule
    makes every label on the page transparent, so the art showed between and
    behind the controls -- a strip of tools reading as part of the picture.
    """
    holder = QWidget()
    holder.setObjectName("controlStrip")
    holder.setAttribute(Qt.WA_StyledBackground, True)
    strip.setContentsMargins(4, 3, 4, 3)
    holder.setLayout(strip)
    return holder


def see_through_row(row: QWidget) -> QWidget:
    """`row` and the plain containers inside it. Buttons, boxes and other
    controls keep their own fills: only a bare QWidget is chrome."""
    see_through(row, *(w for w in row.findChildren(QWidget) if type(w) is QWidget))
    return row


def add_view_row(on_click) -> QPushButton:
    """"+ Add view" as a dashed row under a page's stack of views.

    It was a "+" in the playback strip, beside the view-size stepper's own
    +/-, and three "+" in a row said nothing about which one opened a view.
    Under the stack is where the new view appears. Not inside the stack's
    layout: that ends in its stretch, and group reordering and insertion both
    count on it.
    """
    button = QPushButton("+  " + tr("MainWindow", "Add view"))
    button.setToolTip(tr("MainWindow", "open new view"))
    button.setFocusPolicy(Qt.NoFocus)
    button.setStyleSheet(
        "QPushButton { background: transparent; color: #7d8794; border: 1px dashed #3a4554;"
        " border-radius: 6px; padding: 5px; font-weight: 600; }"
        "QPushButton:hover { color: #f3a6bd; border-color: #b0587f; background: rgba(255,102,170,13); }"
    )
    button.clicked.connect(on_click)
    return button


def is_text_entry(widget) -> bool:
    """A box the keyboard types into: what Esc leaves before anything else."""
    return isinstance(widget, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)) or (
        isinstance(widget, QComboBox) and widget.isEditable())


def add_view_on_empty_right_click(container: QWidget, on_click) -> None:
    """Right click on the bare space under a page's views is "Add view" too.

    A context menu request a child leaves unhandled travels up to its parent,
    so a right click on a view arrives here as well. "No child under the
    point" was the test, and it held in the 10px gaps *between* views too --
    a right click meant for a view's edge opened the dialog (owner's report).
    So: below the last view, and nowhere else.
    """
    def below_the_views(pos) -> bool:
        # Never mid-drag (owner, 2026-10-08): a selection box dragged down
        # past the last view puts the pointer here with the left button held,
        # and a right click then is part of that gesture, not a request.
        if QApplication.mouseButtons() & Qt.LeftButton:
            return False
        children = container.findChildren(QWidget, options=Qt.FindDirectChildrenOnly)
        bottom = max((c.geometry().bottom() for c in children if c.isVisibleTo(container)), default=-1)
        return pos.y() > bottom

    container.setContextMenuPolicy(Qt.CustomContextMenu)
    container.customContextMenuRequested.connect(
        lambda pos: on_click() if below_the_views(pos) else None)


# -- config sheet diagrams ---------------------------------------------------
#
# The explain panel's pictures. Each is drawn from a GimmickConfig with the
# same offsets gimmick_session writes, so the picture is the structure and not
# an illustration of it.

# sv_ease and _true_exp_ease live in sv_curves now, beside the detector that
# reads their curves back out of a chart (experimental conversion).


# Relative, not absolute, because the BPMs compared here span five orders of
# magnitude: a chart line at 176.9 and a gimmick line at 60000 cannot share one
# millisecond-scale epsilon. A red line's BPM is never stored -- it is
# 60000/beat_length, recomputed from a beat_length that has been through a text
# file with a handful of decimals -- so an authored 60000 and a computed one
# differ in the last few digits and `==` says no. One part in a million is far
# tighter than any two red lines a human would ever want to tell apart, and far
# looser than the round trip's error.
BPM_MATCH_RELATIVE_EPSILON = 1e-6


def bpm_matches(bpm: float | None, wanted: float) -> bool:
    """Is `bpm` the same red-line BPM as `wanted`, allowing for float drift?"""
    if bpm is None:
        return False
    return abs(bpm - wanted) <= BPM_MATCH_RELATIVE_EPSILON * max(abs(bpm), abs(wanted), 1.0)


def bpm_in_range(bpm: float | None, low: float, high: float) -> bool:
    """Is `bpm` within [low, high], inclusive, allowing for the same float
    drift bpm_matches guards against?

    The tolerance is applied per end rather than as one shared absolute
    slop, for the same reason bpm_matches scales it by the value being
    compared: a red line's BPM is recomputed as 60000/beat_length, so an
    authored 500.0 can come back as 499.99999...  and a fixed epsilon would
    be wrong by orders of magnitude between a 60 BPM line and a 60000 BPM
    barline gimmick one.
    """
    if bpm is None:
        return False
    low_tolerance = BPM_MATCH_RELATIVE_EPSILON * max(abs(bpm), abs(low), 1.0)
    high_tolerance = BPM_MATCH_RELATIVE_EPSILON * max(abs(bpm), abs(high), 1.0)
    return bpm >= low - low_tolerance and bpm <= high + high_tolerance


class LanguageDialog(QDialog):
    """First-start language choice, shown before anything else is built.

    Deliberately not translated: it is the one screen that cannot assume which
    language the reader wants, so both options are written in their own
    language and there is nothing else on it to translate.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Language / 言語")
        self.language = "en"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        heading = QLabel("Choose your language\n言語を選んでください")
        heading.setAlignment(Qt.AlignCenter)
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(heading)
        for code, label in (("en", "English"), ("ja", "日本語")):
            button = QPushButton(label)
            button.setMinimumHeight(44)
            button.clicked.connect(lambda _checked=False, value=code: self._choose(value))
            layout.addWidget(button)
        self.setStyleSheet(
            """
            QDialog { background: #191f29; }
            QLabel { color: #e8edf3; }
            QPushButton { background: #f3a6bd; color: #17191f; border: 0; border-radius: 6px;
                          padding: 10px 18px; font-weight: 600; font-size: 14px; }
            QPushButton:hover { background: #f7bfd0; }
            """
        )

    def _choose(self, code: str) -> None:
        self.language = code
        self.accept()


def keep_width_under_scrollbar(area: QScrollArea, layout) -> None:
    """Hold `area`'s content at one width whether or not its vertical
    scrollbar is showing. The bar takes its width out of the viewport when it
    appears, so everything inside reflowed -- tiles jumped, labels rewrapped
    -- the moment a panel grew past the window (owner, 2026-10-09). While the
    bar is hidden, `layout` carries that width as extra right margin."""
    left, top, right, bottom = layout.getContentsMargins()
    bar = area.verticalScrollBar()

    def fit(_minimum=0, maximum=0) -> None:
        reserve = 0 if maximum > 0 else bar.sizeHint().width()
        layout.setContentsMargins(left, top, right + reserve, bottom)

    bar.rangeChanged.connect(fit)
    fit(bar.minimum(), bar.maximum())


# The widest the playhead readouts ever get: format_time's mm.ss.mmm, three
# spaces, and a percentage.
READOUT_WIDEST = "00.00.000   100.0%"


def steady_readout(label: QLabel, widest: str = READOUT_WIDEST) -> None:
    """Stop a ticking number from moving what is beside it.

    Exo 2's default figures are proportional, so "01.58.111" is narrower than
    "01.58.000", and a label sized to its own text changed width every few
    frames during playback -- which, beside a stretching timing bar, shook the
    bar left and right. Tabular figures make every digit one width, and the
    floor at the widest text the readout can show keeps it from shrinking.
    """
    label.ensurePolished()
    label.setFont(tabular(label.font()))
    label.setMinimumWidth(label.fontMetrics().horizontalAdvance(widest) + 6)


# The song list's wheel and arrow scrolling glide over this long.


class ToolStateController:
    """The two global note/SV tool rows, and which view they act on.

    "Global" means global to the program, not per-view (per user feedback
    after trying the per-frame version): a row applies to whichever chart/SV
    view last had keyboard focus, tracked through QApplication.focusChanged.
    MainWindow keeps read/write properties for active_chart_view,
    active_sv_view and last_focused_editor_view -- clipboard, editor-view
    management and playback code elsewhere in MainWindow still read and
    write them directly, and moving all of that as one unit was more change
    than this pass wants. Gimmick-page state (GIMMICK_TOOLSETS,
    gimmick_tool_buttons, _active_gimmick_layer, _show_gimmick_row_for) is
    reached through self.window rather than owned here, since it belongs to
    the gimmick page, not this cluster.
    """

    def __init__(self, window: "MainWindow") -> None:
        self.window = window
        self.active_chart_view: TimelineGameplay | None = None
        self.active_sv_view: SVEditorView | None = None
        self.last_focused_editor_view: TimelineGameplay | SVEditorView | None = None
        self.tool_shortcuts: dict[str, QShortcut] = {}
        self._tool_shortcuts_by_scope: dict[str, list[QShortcut]] = {}
        # Set before any toolbox exists, so the first refresh has an answer.
        self._tool_shortcuts_allowed = True

    def build_global_tool_row(self) -> QWidget:
        """M3 note-editing tools: 1 select, 2 don, 3 kat, 4 slider, 5 spinner,
        6 new combo. Also driven by the number keys themselves (see
        build_tool_shortcuts) since these are meant to be pressed, not just
        clicked.
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(QLabel(tr("MainWindow", "Note tools:")))

        self.tool_buttons: dict[str, QPushButton] = {}
        tool_group = QButtonGroup(row)
        tool_group.setExclusive(True)
        tools = (
            ("1", "select", tr("MainWindow", "1. Select")),
            ("2", "don", tr("MainWindow", "2. Don")),
            ("3", "kat", tr("MainWindow", "3. Kat")),
            # Labels only -- "slider"/"spinner" have been the stable internal
            # tool ids since M3 and are what the .osu type bits mean.
            ("4", "slider", tr("MainWindow", "4. Slider")),
            ("5", "spinner", tr("MainWindow", "5. Spinner")),
        )
        for number, tool_id, label in tools:
            button = QPushButton(label)
            button.setStyleSheet(TOOL_BUTTON_STYLE)
            button.setCheckable(True)
            button.setFixedHeight(self.window.TOOL_BUTTON_HEIGHT)
            button.setFocusPolicy(Qt.NoFocus)
            button.setChecked(tool_id == "select")
            button.toggled.connect(lambda checked, t=tool_id: self.set_active_tool(t) if checked else None)
            tool_group.addButton(button)
            self.tool_buttons[tool_id] = button
            layout.addWidget(button)

        self.new_combo_button = QPushButton(tr("MainWindow", "6. New Combo"))
        self.new_combo_button.setStyleSheet(TOOL_BUTTON_STYLE)
        self.new_combo_button.setCheckable(True)
        self.new_combo_button.setFixedHeight(self.window.TOOL_BUTTON_HEIGHT)
        self.new_combo_button.setFocusPolicy(Qt.NoFocus)
        self.new_combo_button.toggled.connect(self.set_active_new_combo)
        layout.addWidget(self.new_combo_button)

        # An action, not a tool: it does its work and leaves the tool alone.
        self.resnap_button = QPushButton(tr("MainWindow", "Resnap"))
        self.resnap_button.setStyleSheet(TOOL_BUTTON_STYLE)
        self.resnap_button.setFixedHeight(self.window.TOOL_BUTTON_HEIGHT)
        self.resnap_button.setFocusPolicy(Qt.NoFocus)
        self.resnap_button.setToolTip(tr(
            "MainWindow",
            "Move the selected objects (or all of them, with nothing selected) onto the "
            "nearest line of their own snap. Only objects a millisecond or two off move.",
        ))
        self.resnap_button.clicked.connect(self.window.resnap_selection)
        layout.addWidget(self.resnap_button)

        layout.addStretch(1)
        self.global_tool_row = row
        self.global_tool_row.setEnabled(False)
        return row

    def build_global_sv_tool_row(self) -> QWidget:
        """M4 SV-editing tools: 1 select, 2 green line, 3 function. Same
        pattern and rationale as the note tool row above; applies to
        whichever SV view last had focus (active_sv_view). Only one of this
        row and the note tool row is visible at a time, switched by
        editor_view_focus_changed based on which view type gained focus.
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(QLabel(tr("MainWindow", "SV tools:")))

        self.sv_tool_buttons: dict[str, QPushButton] = {}
        tool_group = QButtonGroup(row)
        tool_group.setExclusive(True)
        # Two sets sharing slot 1: an ordinary SV view builds green lines and
        # sweeps, the Kiai and Sound Volume view sets kiai and note volume over
        # the same drag. Both are built here and sync_global_sv_tool_row shows
        # whichever the focused view wants, so the digits keep meaning the
        # button in the same position -- the rule the gimmick rows already use.
        tools = (
            ("1", "select", tr("MainWindow", "1. Select")),
            ("2", "green_line", tr("MainWindow", "2. Green Line")),
            ("3", "function", tr("MainWindow", "3. Function")),
            ("2", "kiai", tr("MainWindow", "2. Kiai")),
            ("3", "volume", tr("MainWindow", "3. Volume")),
        )
        for number, tool_id, label in tools:
            button = QPushButton(label)
            button.setStyleSheet(TOOL_BUTTON_STYLE)
            button.setCheckable(True)
            button.setFixedHeight(self.window.TOOL_BUTTON_HEIGHT)
            button.setFocusPolicy(Qt.NoFocus)
            button.setChecked(tool_id == "select")
            button.toggled.connect(lambda checked, t=tool_id: self.set_active_sv_tool(t) if checked else None)
            tool_group.addButton(button)
            self.sv_tool_buttons[tool_id] = button
            layout.addWidget(button)

        self.SV_TOOLS_DEFAULT = ("select", "green_line", "function")
        self.SV_TOOLS_KIAI_SOUND = ("select", "kiai", "volume")
        for tool_id in self.SV_TOOLS_KIAI_SOUND[1:]:
            self.sv_tool_buttons[tool_id].setVisible(False)

        layout.addStretch(1)
        self.global_sv_tool_row = row
        self.global_sv_tool_row.setEnabled(False)
        self.global_sv_tool_row.setVisible(False)
        return row

    def build_tool_shortcuts(self) -> None:
        """Number keys 1-6 as global tool shortcuts, not just clickable buttons.

        ApplicationShortcut context fires regardless of which widget has
        focus -- including window.timeline, the shared Fancy Arranger
        timeline -- but the handler only ever calls set_tool on
        active_chart_view / active_sv_view, both of which are exclusively
        Editor-page views (see editor_view_focus_changed), so this can't
        resurrect the contamination risk a per-widget keyPressEvent override
        would have. Routes to whichever of the two tool rows is currently
        visible, since both use keys 1-3 for different tools and only one is
        ever relevant at a time.

        The bound key comes from the shortcut registry so Settings can rebind
        it; the digit passed to the handler stays the logical tool number
        regardless of which key ends up on it.

        One shortcut per *scoped* definition, not a fixed six: the Editor
        page's toolbox is `tool_1`...`tool_6`, and each gimmick layer has its
        own set derived from its own toolbox (see the registration at the foot
        of this module), because the layers do not agree on what tool 2 is or
        even on how many tools there are.
        """
        self.tool_shortcuts = {}
        self._tool_shortcuts_by_scope = {}
        for definition in self.window.shortcuts.definitions.values():
            # A scope is what marks a definition as a toolbox binding; the
            # global actions each have their own QShortcut already.
            if not definition.scope:
                continue
            digit = definition.action_id.rsplit("_", 1)[-1]
            shortcut = QShortcut(QKeySequence(self.window.shortcuts.sequence(definition.action_id)), self.window)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.activated.connect(lambda d=digit: self.activate_tool_digit(d))
            self.tool_shortcuts[definition.action_id] = shortcut
            self._tool_shortcuts_by_scope.setdefault(definition.scope, []).append(shortcut)
        self.refresh_tool_shortcut_scope()

    def set_tool_shortcuts_enabled(self, enabled: bool) -> None:
        self._tool_shortcuts_allowed = enabled
        self.refresh_tool_shortcut_scope()

    def refresh_tool_shortcut_scope(self) -> None:
        """Leave only the reachable toolbox's keys enabled.

        Every toolbox numbers its tools from 1, so seven of them want the key
        "1" -- which is correct, since only one toolbox is reachable at a
        time. Qt does not see it that way: two enabled ApplicationShortcuts on
        one key fire `activatedAmbiguously` and *neither* acts, so the digit
        has to belong to exactly one toolbox at any moment. The focused
        gimmick layer's while that page is up, the Editor page's otherwise,
        and none of them while a text widget has focus (see
        `editor_view_focus_changed`: an ApplicationShortcut consumes its key,
        so the only way a digit reaches a search box is for no tool shortcut
        to be enabled).

        getattr throughout: focusChanged and _show_page both run before the
        shortcuts and the pages exist.
        """
        by_scope = getattr(self, "_tool_shortcuts_by_scope", None)
        if not by_scope:
            return
        stack = getattr(self.window, "page_stack", None)
        if not self._tool_shortcuts_allowed:
            active = ""
        elif stack is not None and stack.currentIndex() == PAGE_GIMMICK:
            active = getattr(self.window, "_active_gimmick_layer", None) or ""
        else:
            active = "editor"
        for scope, shortcuts in by_scope.items():
            for shortcut in shortcuts:
                shortcut.setEnabled(scope == active)

    def activate_tool_digit(self, digit: str) -> None:
        if should_ignore_shortcut_focus(QApplication.focusWidget()):
            return
        note_tools = {"1": "select", "2": "don", "3": "kat", "4": "slider", "5": "spinner"}
        sv_tools = dict(zip("123", self.sv_tools_for(self.active_sv_view)))
        # The gimmick page has its own row and its own meanings for 1-4, and it
        # is checked first: its row is always visible while that page is up,
        # where the Editor page's two rows are only visible on theirs.
        if self.window.page_stack.currentIndex() == PAGE_GIMMICK:
            # The digit is the button's position in the focused layer's own
            # toolbox, so 2 is a fake slider in one layer and a barline note in
            # the next -- the same relationship the buttons themselves have.
            layer = getattr(self.window, "_active_gimmick_layer", None)
            tools = self.window.GIMMICK_TOOLSETS.get(layer or "", ())
            index = int(digit) - 1
            if 0 <= index < len(tools):
                self.window.gimmick_tool_buttons[layer][tools[index][0]].setChecked(True)
            return
        if self.global_tool_row.isVisible():
            if digit in note_tools:
                self.tool_buttons[note_tools[digit]].setChecked(True)
            elif digit == "6":
                self.new_combo_button.toggle()
        elif self.global_sv_tool_row.isVisible() and digit in sv_tools:
            self.sv_tool_buttons[sv_tools[digit]].setChecked(True)

    def set_active_tool(self, tool_id: str) -> None:
        if self.active_chart_view is None:
            return
        self.active_chart_view.set_tool(tool_id)
        # New Combo applies until another tool is chosen -- the rule the
        # gimmick row has followed since its own version of this was fixed
        # (see `_set_gimmick_tool`), and the one this row was missing. Off the
        # tool group, the button latched: pressed once, every note placed
        # afterwards carried the flag, through any number of tool changes.
        #
        # Not cleared after a single placement: a run of notes that all start
        # a combo is a real thing to want, and one press per note is not.
        self.set_new_combo_checked(False)

    def set_new_combo_checked(self, value: bool) -> None:
        """Set the flag and the button together, without re-entering."""
        if self.active_chart_view is not None:
            self.active_chart_view.set_new_combo(value)
        self.new_combo_button.blockSignals(True)
        self.new_combo_button.setChecked(value)
        self.new_combo_button.blockSignals(False)

    def set_active_new_combo(self, value: bool) -> None:
        if self.active_chart_view is not None:
            self.active_chart_view.set_new_combo(value)

    def editor_view_focus_changed(self, _old, new) -> None:
        # An ApplicationShortcut *consumes* its key, so the plain digits 1-6
        # would eat characters typed into the library's search box or any spin
        # box -- should_ignore_shortcut_focus can decline to act, but cannot
        # hand the key back. Disabling the shortcuts outright while a text
        # widget has focus is the only way the key reaches it.
        self.set_tool_shortcuts_enabled(not should_ignore_shortcut_focus(new))

        # Only Editor-page views (chart: symmetric; any SVEditorView) can
        # become a tool target; window.timeline (the shared Fancy Arranger
        # timeline) never does, so clicking it can't silently steal either
        # global tool row. Whichever type gains focus shows its own row and
        # hides the other, since only one is ever relevant at a time.
        # A gimmick layer owns its own toolbox rather than the Editor page's:
        # the same button means a different structure there.
        # Moving to a *different* view always arrives on Select. A tool is a
        # mode, and carrying one across a view boundary meant the first click in
        # the view you just moved to placed something instead of selecting it --
        # in the gimmick page, a whole barline structure. Same view again (after
        # a modal dialog, say) is not a move, so an active tool survives that.
        moved = new is not self.last_focused_editor_view

        if (getattr(new, "gimmick_layer", None) is not None or isinstance(new, SVEditorView)
                or (isinstance(new, TimelineGameplay) and new.editor_view)):
            self.window._mark_focused_view(new)

        if getattr(new, "gimmick_layer", None) is not None:
            if isinstance(new, SVEditorView):
                self.active_sv_view = new
            else:
                self.active_chart_view = new
            self.window._show_gimmick_row_for(new)
            if moved:
                self.last_focused_editor_view = new
                self.window.gimmick_tool_buttons[new.gimmick_layer]["select"].setChecked(True)
            return

        if isinstance(new, TimelineGameplay) and new.editor_view:
            self.active_chart_view = new
            if moved:
                new.set_tool("select")
                new.set_new_combo(False)
            self.last_focused_editor_view = new
            self.sync_global_tool_row()
            self.global_tool_row.setVisible(True)
            self.global_sv_tool_row.setVisible(False)
        elif isinstance(new, SVEditorView):
            self.active_sv_view = new
            if moved:
                new.set_tool("select")
            self.last_focused_editor_view = new
            self.sync_global_sv_tool_row()
            self.global_tool_row.setVisible(False)
            self.global_sv_tool_row.setVisible(True)

    def sync_global_tool_row(self) -> None:
        view = self.active_chart_view
        enabled = view is not None and view.isEnabled()
        self.global_tool_row.setEnabled(enabled)
        if view is None:
            return
        button = self.tool_buttons.get(view.tool)
        if button is not None:
            button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(False)
        self.new_combo_button.blockSignals(True)
        self.new_combo_button.setChecked(view.new_combo)
        self.new_combo_button.blockSignals(False)

    def set_active_sv_tool(self, tool_id: str) -> None:
        if self.active_sv_view is not None:
            self.active_sv_view.set_tool(tool_id)

    def sv_tools_for(self, view) -> tuple[str, ...]:
        """Which of the two SV tool sets `view` is driven by."""
        if getattr(view, "kiai_sound", False):
            return self.SV_TOOLS_KIAI_SOUND
        return self.SV_TOOLS_DEFAULT

    def sync_global_sv_tool_row(self) -> None:
        view = self.active_sv_view
        enabled = view is not None and view.isEnabled()
        self.global_sv_tool_row.setEnabled(enabled)
        if view is None:
            return
        wanted = self.sv_tools_for(view)
        for tool_id, button in self.sv_tool_buttons.items():
            button.setVisible(tool_id in wanted)
        button = self.sv_tool_buttons.get(view.tool)
        if button is not None:
            button.blockSignals(True)
            button.setChecked(True)
            button.blockSignals(False)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        # Set only while song select opens a chart (_ensure_default_editor_views).
        self._gameplay_with_defaults = False
        self.app_name="Taiko Fancy Arranger";self.setWindowTitle(self.app_name)
        icon=application_icon()
        if not icon.isNull():self.setWindowIcon(icon)
        apply_ui_font()
        self.resize(1360, 900)

        self.settings = SettingsManager()
        self.shortcuts = ShortcutRegistry(self.settings)
        # One DifficultyState per source path visited this session, so
        # switching difficulty and back preserves edits, selection, undo
        # history and cursor position instead of discarding them.
        self._states: dict[Path, DifficultyState] = {}
        # source_path -> history.revision at the moment the user chose
        # "Continue Without Saving" on it. _confirm_leaving_editor stays
        # quiet about a state whose revision hasn't moved since, so
        # answering the prompt once does not immediately re-ask on the very
        # next page/view change; a fresh edit moves the revision on and the
        # prompt is legitimate again. Cleared once the state is actually
        # saved (save_all_states).
        self._discard_acknowledged: dict[Path, int] = {}
        self.state: DifficultyState | None = None
        self.song_difficulties: list[Path] = []
        # Every TimelineGameplay currently on screen: the shared player-deck
        # timeline plus any Editor-page chart views. _render_gameplay_frame
        # broadcasts the clock to all of them instead of just self.timeline.
        self._chart_views: list[TimelineGameplay] = []
        # Subset of _chart_views, plus every open SV editor view: everything
        # Editor-page-added that has its own snap_divisor (not self.timeline).
        # Alt+wheel on the Editor page and its own snap combo drive every
        # entry here together, regardless of whether it's a chart or SV view.
        self._editor_snap_views: list[TimelineGameplay | SVEditorView | GameplayViewerView] = []
        # Every open SV editor view. Broadcast the clock to these alongside
        # _chart_views so the SV editor scrolls with the chart during
        # playback instead of standing still.
        self._sv_views: list[SVEditorView] = []
        # Every open gameplay viewer. On the same clock, but not in
        # _editor_snap_views' zoom family: it scrolls by distance, not by a
        # shared time window.
        self._gameplay_views: list[GameplayViewerView] = []
        # Zoom (window_ms) is per difficulty, shared by every view of that
        # difficulty: a chart and an SV editor showing different time spans
        # cannot stay visually aligned while the playhead moves.
        # One zoom for the whole Editor page. The difficulties open there
        # are the same song out of the same folder on the same timing, so
        # two of them on different scales put the same x at two different
        # times -- which is the thing the per-difficulty dict this replaces
        # was already preventing *within* one difficulty.
        self._editor_zoom_ms = self.DEFAULT_EDITOR_WINDOW_MS
        # Which difficulty's hit objects are sounded. None follows the
        # active one, which is what this did before the choice existed.
        # Non-None only inside `_refresh_cycle`; see `_cached`.
        self._doc_cache: dict | None = None
        # Ctrl+C / Ctrl+V payloads, kept as plain data (time offset + the
        # fields needed to rebuild) rather than live objects, so a paste
        # always creates fresh identities and pasting twice is two objects.
        self._note_clipboard: list[dict] = []
        # Red lines copied alongside the notes -- a gimmick object is both.
        self._timing_clipboard: list[dict] = []
        # The snap divisor the clipboard was copied at. A paste rebuilds
        # its objects on that grid rather than on whatever divisor happens
        # to be selected now, so changing the divisor between Ctrl+C and
        # Ctrl+V cannot re-snap a pattern onto a coarser one.
        self._clipboard_divisor = 0
        self._sv_clipboard: list[dict] = []
        # Every open Editor-page density view, broadcast separately since
        # DensityOverview.set_time has a different signature than
        # TimelineGameplay.set_time (no `force` kwarg). self.fancy_density
        # (Fancy Arranger's own, always showing the active difficulty) is
        # appended here too once built, for the same broadcast.
        self._density_views: list[DensityOverview] = []
        # self.timing_bar (Editor page) + self.fancy_timing_bar (Fancy
        # Arranger page): two instances of the same widget type -- a
        # TimingOverviewBar can only live in one layout at a time -- kept in
        # sync wherever the active difficulty's timing info changes.
        self._timing_bars: list[TimingOverviewBar] = []
        self._tool_state = ToolStateController(self)
        QApplication.instance().focusChanged.connect(self._editor_view_focus_changed)

        self._library = LibraryPageController(self)
        # Which difficulty each gimmick session edits, and the timing snapshot
        # its snaps and scroll are anchored to. Read once, kept for the session.
        self._gimmick_pairings: dict[str, GimmickPairing] = load_index(self._gimmick_index_path())
        self._gimmick_pairing: GimmickPairing | None = None
        # One config per structure-building layer. Sharing a single one meant
        # the barline spacing and the fake slider offset were the same number,
        # which is exactly the pair that must not agree (spacing_collides).
        # The three SV layers get their own too, for `sv_offset_ms`: how far
        # from its object each layer's green lines sit. All three start on the
        # object itself. The Editor page's SV view keeps its -5ms lead-in
        # (SVFunctionDialog.DEFAULT_POSITION_OFFSET_MS); in the gimmick editor
        # the chart's SV is placed against gimmick structures on exact
        # milliseconds, and a lead-in there has to be asked for.
        self.gimmick_configs = {
            "fake_slider": GimmickConfig(),
            "barline": GimmickConfig(),
            "sv_chart": GimmickConfig(),
            "sv_fake_slider": GimmickConfig(),
            "sv_barline": GimmickConfig(),
        }
        # The Multiple Fake Slider tool's last-chosen run shape (start, end,
        # distance), reused until the tool is reselected and reconfigured --
        # see `_set_gimmick_tool` and `_multi_fake_slider_commands`.
        self._multi_fake_slider_params: tuple[int, int, int] = (0, 16, 2)

        self.position_controls: dict[str, dict[str, ParameterControl]] = {}
        self.drawing_points={"all":[],"don":[],"kat":[]};self.last_drawing_points=[];self.drawing_dialog_active=False
        self.controls: dict[
            str,
            dict[str, ParameterControl],
        ] = {
            "all": {},
            "don": {},
            "kat": {},
        }

        # Not QMediaPlayer: see audio_engine's module docstring. The short of
        # it is that a sample-accurate clock, a pitch-preserving slow speed and
        # a rate change that does not stall all live below the level Qt's
        # player exposes.
        self.player = TrackPlayer(self)
        self.hitsounds = HitsoundPlayer(self.player)
        # Music volume was hardcoded here until it had a settings page; both it
        # and the hitsound values are read in one place so the Settings dialog
        # can re-apply them without a restart.
        self._apply_audio_settings()
        self.player.errorOccurred.connect(self._audio_failed)
        self.player.positionChanged.connect(
            self._player_position_changed
        )
        self.player.durationChanged.connect(
            lambda _duration: self._update_timeline_info()
        )
        # The interpolated clock (osu!'s framedClock): the value actually
        # displayed. Advanced and blended toward the source clock once per
        # rendered frame in _advance_interpolated_clock -- never stepped
        # directly from a backend report, and never allowed to move backwards
        # except on an explicit reset (seek_audio, toggle_playback).
        self.audio_anchor_position = 0.0
        # Restarted every time audio_anchor_position is set (per frame, or on
        # a reset) so _predicted_audio_position can extrapolate the small gap
        # since then for callers between rendered frames.
        self.audio_anchor_clock = QElapsedTimer()
        self.audio_anchor_clock.start()
        # The last raw report from QMediaPlayer.positionChanged, and the
        # instant it arrived. osu!'s framedSourceClock is exactly this: a
        # sparse report extrapolated forward by elapsed real time * rate, so
        # there is something to blend toward every frame instead of only at
        # the tens-of-milliseconds-apart instants a report actually lands.
        self.latest_audio_position = 0.0
        self._source_report_clock = QElapsedTimer()
        self._source_report_clock.start()
        # Seeks sent to the engine whose report has not come back yet, oldest
        # first. The engine reports every seek with its exact target, in order,
        # so a report matching the oldest is that seek's echo -- and stale if a
        # newer seek is still waiting (see _player_position_changed). Bounded
        # so a report that never comes back cannot grow it without limit.
        self._unechoed_seeks: deque[int] = deque(maxlen=64)
        # Monotonic floor for _predicted_audio_position's own sub-frame
        # extrapolation -- reset alongside the clocks above so a legitimate
        # backwards seek is never clamped away.
        self._last_predicted_position = 0.0
        # Set while a setPlaybackRate is still working its way through the
        # backend; see _advance_interpolated_clock for what it suppresses.
        self._awaiting_rate_report = False

        # Never restarted after this; _render_gameplay_frame schedules off a
        # fixed cadence (self._next_frame_due_ns) measured against it instead
        # of resetting to "now" every tick, which is what kept turning a
        # slightly-late timer wakeup into a permanently shifted, uneven frame
        # pace -- the main source of visible stutter in the old scheme.
        self.gameplay_frame_clock = QElapsedTimer()
        self.gameplay_frame_clock.start()
        # The display's own refresh, re-read when the window changes screen
        # (_follow_screen); see display_frame_interval_ns.
        self.gameplay_frame_interval_ns = display_frame_interval_ns(QApplication.primaryScreen())
        self._next_frame_due_ns = self.gameplay_frame_interval_ns
        # The real gap between rendered frames drives the interpolated
        # clock's own per-frame advance (_advance_interpolated_clock), rather
        # than the fixed interval above -- a render that fires a little late
        # advances the playhead by how late it actually was.
        self._last_frame_ns = self.gameplay_frame_clock.nsecsElapsed()
        # Last position handed to the views, so a paused playhead stops costing
        # a full repaint of every open view per frame.
        self._last_broadcast_position: float | None = None

        self.gameplay_render_timer = QTimer(self)
        self.gameplay_render_timer.setTimerType(Qt.PreciseTimer)
        self.gameplay_render_timer.setInterval(4)
        self.gameplay_render_timer.timeout.connect(self._render_gameplay_frame)
        self.gameplay_render_timer.start()

        self.info_timer = QTimer(self)
        self.info_timer.setInterval(16)
        self.info_timer.timeout.connect(self._update_timeline_info)
        self.info_timer.start()

        self.play_shortcut = QShortcut(QKeySequence(self.shortcuts.sequence("play_pause")), self)
        self.play_shortcut.setContext(Qt.ApplicationShortcut)
        self.play_shortcut.activated.connect(self._toggle_playback_from_shortcut)

        self.undo_shortcut=QShortcut(QKeySequence(self.shortcuts.sequence("undo")),self); self.undo_shortcut.setContext(Qt.ApplicationShortcut); self.undo_shortcut.activated.connect(self.undo)
        self.redo_shortcut=QShortcut(QKeySequence(self.shortcuts.sequence("redo")),self); self.redo_shortcut.setContext(Qt.ApplicationShortcut); self.redo_shortcut.activated.connect(self.redo)
        self.save_shortcut = QShortcut(QKeySequence(self.shortcuts.sequence("save_all")), self)
        self.save_shortcut.setContext(Qt.ApplicationShortcut)
        self.save_shortcut.activated.connect(self._save_from_shortcut)
        self.hitsound_earlier_shortcut = QShortcut(QKeySequence(self.shortcuts.sequence("hitsound_offset_earlier")), self)
        self.hitsound_earlier_shortcut.setContext(Qt.ApplicationShortcut)
        self.hitsound_earlier_shortcut.activated.connect(lambda: self._nudge_hitsound_offset(-1))
        self.hitsound_later_shortcut = QShortcut(QKeySequence(self.shortcuts.sequence("hitsound_offset_later")), self)
        self.hitsound_later_shortcut.setContext(Qt.ApplicationShortcut)
        self.hitsound_later_shortcut.activated.connect(lambda: self._nudge_hitsound_offset(1))
        self._tool_state.build_tool_shortcuts()
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(70)
        self.preview_timer.timeout.connect(self.update_preview)

        self._build_ui()
        # After _build_ui: these are scoped to self.editor_page, which does
        # not exist until the pages are built.
        self._build_clipboard_shortcuts()
        self._editor_keys = EditorKeys(self, self.editor_page, self.gimmick_page)
        self._rebuild_control_tabs()
        self._restore_mods()
        self._sync_mod_controls()
        self._restore_autoplay()
        QApplication.instance().installEventFilter(self)

    # -- DifficultyState forwarding -----------------------------------------
    #
    # These keep every existing call site (self.applied_positions[...], etc.)
    # working unchanged while the underlying storage moves to self.state, one
    # DifficultyState per open difficulty. Getters must tolerate self.state
    # being None: eventFilter reads self.selected on every application click,
    # before any map is open.

    @property
    def document(self):
        return self.state.document if self.state else None

    @property
    def source_path(self) -> Path | None:
        return self.state.source_path if self.state else None

    @property
    def current_background_path(self) -> Path | None:
        return self.state.background_path if self.state else None

    @current_background_path.setter
    def current_background_path(self, value) -> None:
        if self.state is not None:
            self.state.background_path = value

    @property
    def original_positions(self) -> dict[int, tuple[int, int]]:
        return self.state.original_positions if self.state else {}

    @property
    def applied_positions(self) -> dict[int, tuple[int, int]]:
        return self.state.applied_positions if self.state else {}

    @applied_positions.setter
    def applied_positions(self, value) -> None:
        if self.state is not None:
            self.state.applied_positions = value

    @property
    def preview_positions(self) -> dict[int, tuple[int, int]]:
        return self.state.preview_positions if self.state else {}

    @preview_positions.setter
    def preview_positions(self, value) -> None:
        if self.state is not None:
            self.state.preview_positions = value

    @property
    def selected(self) -> set[int]:
        return self.state.selected if self.state else set()

    @selected.setter
    def selected(self, value) -> None:
        if self.state is not None:
            self.state.selected = value

    @property
    def preview_offsets(self) -> dict[str, list[float]]:
        if self.state is None:
            return {"all": [0.0, 0.0], "don": [0.0, 0.0], "kat": [0.0, 0.0]}
        return self.state.preview_offsets

    @preview_offsets.setter
    def preview_offsets(self, value) -> None:
        if self.state is not None:
            self.state.preview_offsets = value

    @property
    def preview_cache(self) -> dict:
        return self.state.preview_cache if self.state else {}

    @preview_cache.setter
    def preview_cache(self, value) -> None:
        if self.state is not None:
            self.state.preview_cache = value

    @property
    def commit_revision(self) -> int:
        return self.state.history.revision if self.state else 0

    @staticmethod
    def _is_descendant(widget, ancestor) -> bool:
        while widget is not None:
            if widget is ancestor: return True
            widget=widget.parentWidget()
        return False
    def _page_wheel_target(self, watched):
        """Which view a wheel event on the gimmick or Editor page belongs to,
        or None.

        The Editor page joined on 2026-10-07 (owner: "global inside editor
        and gimmick editor"): a wheel over a header, the space below the views
        or the strip scrolled the page or did nothing. One difference there --
        Alt+wheel is left to the page's own branch below, which moves every
        chart view's snap together rather than one view's.

        **Every wheel on these pages seeks, except on the scrollbar.** Seeking is
        what the wheel means here wherever the pointer is, so the pointer does
        not have to be inside a band for it to work.

        The scrollbar is the one exception, and the reason it is the *only* one
        is that it is the only surface on the page that does not move. Letting
        the gaps between bands scroll as well looked reasonable and was not:
        scrolling slides the bands under a stationary cursor, so one continuous
        gesture lands alternately on a gap and on a band and does half a scroll
        and half a seek -- reported as "scrolling horizontal and vertical
        simultaneously", and at a high BPM (where a seek barely moves) as the
        view sliding somewhere else entirely. Measured: ten notches on a band
        seek 971 -> 2250; ten alternating seek half as far and scroll the rest.

        A combo box or spin box under the pointer keeps its own wheel too:
        those read a wheel as a value.

        This also has to run *before* the Alt+wheel branch below, which was
        catching every Alt+wheel in the program and handing it to the Fancy
        Arranger timeline: on the gimmick page that consumed the event and the
        layer under the pointer never saw it at all.

        A combo box, spin box or scrollbar under the pointer keeps its own
        wheel whatever the modifiers: those read a wheel as a value, or as the
        scroll they exist for.
        """
        stack = getattr(self, "page_stack", None)
        page = stack.currentIndex() if stack is not None else None
        if page == PAGE_EDITOR:
            return self._editor_wheel_target(watched)
        if page != PAGE_GIMMICK:
            return None
        if not self._gimmick_views or isinstance(
            watched, (QComboBox, QAbstractSpinBox, QAbstractSlider, SnapSlider)
        ) or isinstance(watched.parentWidget() if isinstance(watched, QWidget) else None, SnapSlider):
            # A SnapSlider's caption, ticks and value label read the wheel as
            # the snap too, not only its track.
            return None
        views = [
            getattr(frame, "chart_view", None) or getattr(frame, "sv_view", None)
            for frame in self._gimmick_views
        ]
        views = [view for view in views if view is not None]
        if not views:
            return None
        # The band under the pointer -- so Ctrl+zoom and the snap grid read as
        # belonging to what is being looked at -- or its chrome, which is part
        # of the same band as far as the pointer is concerned. Anywhere else on
        # the page, the first band: the gesture means the same thing wherever
        # it happens.
        for view in views:
            if watched is view or self._is_descendant(watched, view):
                return view
        return views[0]

    def _editor_wheel_target(self, watched):
        """`_page_wheel_target` on the Editor page: the view under the
        pointer, else the one last clicked into, else the first on screen."""
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.AltModifier or isinstance(
            watched, (QComboBox, QAbstractSpinBox, QAbstractSlider, SnapSlider)
        ) or isinstance(watched.parentWidget() if isinstance(watched, QWidget) else None, SnapSlider):
            return None
        if isinstance(watched, QWidget) and watched.window() is not self:
            return None
        views = [
            frame.content for frame in self._editor_views
            if isinstance(frame.content, (TimelineGameplay, SVEditorView, GameplayViewerView))
            and frame.content.isVisible()
        ]
        if not views:
            return None
        for view in views:
            if watched is view or self._is_descendant(watched, view):
                return view
        focused = getattr(self, "_last_focused_editor_view", None)
        return focused if focused in views else views[0]

    # The only event types the application filter below acts on. Everything
    # else -- paints, timers, layout requests, every mouse move -- is handed
    # straight back, which matters because this filter is installed on the
    # QApplication and therefore sees *every* event in the process: profiling
    # a playback frame caught it running 31 times per frame, four
    # QEvent.type() conversions deep each time, for events it could never act
    # on.
    _FILTERED_EVENT_TYPES = frozenset({
        QEvent.KeyPress, QEvent.KeyRelease, QEvent.Wheel, QEvent.MouseButtonPress,
    })

    def eventFilter(self,watched,event)->bool:
        kind=event.type()
        if kind not in self._FILTERED_EVENT_TYPES:
            return super().eventFilter(watched,event)
        # Esc in a text box leaves the box, and does nothing else (owner,
        # 2026-10-08): the next Esc closes the dialog or leaves the chart as
        # it always did. Before the window check below, because a dialog's
        # boxes are the ones this is most for. Only on the box itself, which
        # sees the key first, so it happens once.
        if (kind == QEvent.KeyPress and event.key() == Qt.Key_Escape
                and not event.modifiers() and watched is QApplication.focusWidget()
                and is_text_entry(watched)):
            watched.clearFocus()
            return True
        # Nothing outside this window is ours. The filter is installed on the
        # *application*, so every event in the process runs through it --
        # including the ones inside a dialog, whose own top-level window this
        # is not. Without this, scrolling the Settings dialog seeked the
        # gimmick layer behind it (measured: 1000ms -> 1125ms on one notch),
        # and its combo boxes and sliders were fighting the editor for the
        # wheel. A combo box popup is its own top-level too, so this hands the
        # popup its own scrolling back at the same time.
        window = watched.window() if isinstance(watched, QWidget) else None
        # A floating Fancy Arranger dock is a top-level of its own, but still
        # this window's: its parent chain reaches here, which a dialog's does
        # not. Without this, Alt+wheel snap and click-to-clear stopped at the
        # dock's edge the moment it was floated.
        if window is not None and window is not self and not (
            isinstance(window, QDockWidget) and self._is_descendant(window, self)
        ):
            return super().eventFilter(watched,event)
        # Shift toggles the placement preview between its normal and finisher
        # size (_draw_placement_ghost reads it fresh on every paint), but a
        # chart/SV view only repaints on mouse *move* -- so holding Shift over
        # a stationary cursor changed nothing on screen until the mouse
        # twitched. Caught here rather than in the views' own keyPressEvent
        # because a view usually does not have keyboard focus (the window
        # does), so it would never see the key at all. Filtered to views the
        # cursor is actually over, not every open view, so this stays a
        # handful of repaints rather than a whole-app one on every keystroke.
        if (
            kind in (QEvent.KeyPress, QEvent.KeyRelease)
            and event.key() == Qt.Key_Shift
            and not event.isAutoRepeat()
        ):
            for view in [*self.findChildren(TimelineGameplay), *self.findChildren(SVEditorView)]:
                if view.isVisible() and view.underMouse():
                    view.update()
                    # Shift also widens a split layer's band to both rows,
                    # which should not wait for the mouse to move either.
                    if (
                        isinstance(view, TimelineGameplay)
                        and view.drag_anchor_time is not None and view._drag_y is not None
                    ):
                        view._update_drag_selection()
            # Falls through rather than returning True: nothing else treats
            # Shift as consumed, and eating it here would be a silent change
            # to every other Shift-modified gesture (Shift+click, Shift+drag).
        if self.drawing_dialog_active and kind==QEvent.MouseButtonPress:return super().eventFilter(watched,event)
        if kind==QEvent.Wheel:
            target = self._page_wheel_target(watched)
            if target is not None:
                target.wheelEvent(event)
                return True
        if kind==QEvent.Wheel and hasattr(self,"timeline"):
            modifiers=event.modifiers() | QApplication.keyboardModifiers()
            alt_down=bool(modifiers & Qt.AltModifier)
            if alt_down:
                angle=event.angleDelta();pixel=event.pixelDelta()
                delta=angle.y() or angle.x() or pixel.y() or pixel.x()
                if delta:
                    # Snap is global per page: on the Editor page every open
                    # chart view moves together; on Fancy Arranger it's just
                    # self.timeline, as before.
                    on_editor_page = hasattr(self,"page_stack") and self.page_stack.currentWidget() is self.editor_page
                    if on_editor_page and hasattr(self,"editor_snap_combo"):
                        self._editor_change_snap_from_wheel(delta)
                    else:
                        self.timeline._change_snap_from_wheel(delta)
                event.accept()
                return True
        # Fancy-Arranger-only: this selection belongs to the transform
        # workflow on that page, so a click elsewhere (including anywhere on
        # the Editor page) must not clear it just because it landed outside
        # a whitelist of widgets. The whitelist missed the dock scrollbar and
        # the sub-toolbar's sliders, so dragging Background Opacity or CS
        # dropped the selection the slider was about to act on. The one
        # deliberate deselect here is a press on the canvas that misses every
        # note; the timeline clears its own on an empty click, and Esc too.
        # A press on the empty space under the views lets go of the focused
        # one (owner, 2026-10-08), as Esc does.
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton and watched in (
                getattr(self, "editor_views_layout", None) and self.editor_views_layout.parentWidget(),
                getattr(self, "gimmick_views_layout", None) and self.gimmick_views_layout.parentWidget()):
            self._unfocus_views()
        on_fancy_arranger_page = hasattr(self, "page_stack") and self.page_stack.currentWidget() is self.fancy_arranger_page
        if kind==QEvent.MouseButtonPress and on_fancy_arranger_page and hasattr(self,"timeline") and self.selected:
            global_pos = event.globalPosition().toPoint()
            if self._is_descendant(QApplication.widgetAt(global_pos), self.canvas):
                if self.canvas._note_at_canvas_position(QPointF(self.canvas.mapFromGlobal(global_pos))) is None:
                    self.clear_transform_selection()
        return super().eventFilter(watched,event)
    def clear_transform_selection(self)->None:
        self.selected.clear(); self.timeline.selected.clear(); self.preview_positions=dict(self.applied_positions); self.refresh_canvas()

    def _toggle_playback_from_shortcut(self) -> None:
        if should_ignore_shortcut_focus(QApplication.focusWidget()):
            return
        # The song list has its own player. This shortcut is application-wide,
        # so it reaches the song list before `LibraryKeys` sees the KeyPress, and
        # once a difficulty had been opened `self.document` stayed set: Space
        # on the song list restarted the *editor's* song from the editor's
        # playhead.
        if self.page_stack.currentIndex() == PAGE_LIBRARY:
            self._library.toggle_preview()
            return
        if self.document is not None:
            self.toggle_playback()

    def _save_from_shortcut(self) -> None:
        if not should_ignore_shortcut_focus(QApplication.focusWidget()) and self.state is not None:
            self.save_all_states()

    def _reload_shortcuts(self) -> None:
        """Re-read every registry-driven binding after Settings applies.

        `back_to_songs` needs nothing here: keyPressEvent compares against the
        stored sequence each time rather than holding a QShortcut.
        """
        for action_id, shortcut in (
            ("play_pause", self.play_shortcut),
            ("undo", self.undo_shortcut),
            ("redo", self.redo_shortcut),
            ("save_all", self.save_shortcut),
            ("copy", self.copy_shortcut),
            ("paste", self.paste_shortcut),
            # The Gimmick page's pair was missing here, so a rebound copy or
            # paste key reached the Editor page and not the Gimmick page.
            ("copy", self.gimmick_copy_shortcut),
            ("paste", self.gimmick_paste_shortcut),
            ("resnap", self.resnap_shortcut),
            ("hitsound_offset_earlier", self.hitsound_earlier_shortcut),
            ("hitsound_offset_later", self.hitsound_later_shortcut),
            *self.tool_shortcuts.items(),
        ):
            shortcut.setKey(QKeySequence(self.shortcuts.sequence(action_id)))
        self._editor_keys.reload()

    def _nudge_hitsound_offset(self, step: int) -> None:
        """Calibrate the note-vs-music trim by ear, live.

        Tapping cannot measure this one: the notes and the music leave through
        the same buffer, so the only instrument is hearing whether they fuse.
        The setter re-sends the schedule and the mixer rebases its watermark,
        so a nudge mid-playback is heard on the next note. Slow the chart down
        first -- at 0.25x every millisecond of error is four of wall time.
        """
        value = max(-500, min(500, self.hitsounds.offset_ms + step))
        self.hitsounds.offset_ms = value
        self.settings.set_value("audio/hitsound_offset_ms", value)
        self.show_toast(tr("MainWindow", "Hitsound offset: %1 ms").replace("%1", f"{value:+d}"))

    def _apply_audio_settings(self) -> None:
        """Re-read the Audio page and apply it live.

        Called at startup and again after the Settings dialog closes, so a
        changed volume or latency offset takes effect immediately rather than
        at next launch. Reading unconditionally after the dialog is simpler
        than reacting to accept/reject and costs nothing: a rejected dialog
        wrote nothing, so this reads back exactly what was already in force.
        """
        self._set_volumes(self.settings.int_value("audio/music_volume", 65),
                          self.settings.int_value("audio/hitsound_volume", 70))
        self.hitsounds.enabled = self.settings.bool_value("audio/hitsounds_enabled", True)
        self.hitsounds.offset_ms = self.settings.int_value(
            "audio/hitsound_offset_ms", DEFAULT_HITSOUND_OFFSET_MS)
        # Deliberately not defaulted to anything but zero. The lag between the
        # position the backend reports and the sound reaching the speakers is a
        # property of this machine's device and drivers, so the only honest
        # shipped value is "none", with the user free to dial in their own.
        self.audio_output_offset_ms = self.settings.int_value("audio/output_offset_ms", 0)
        self._apply_appearance_settings()

    def _set_volumes(self, music_percent: int, hitsound_percent: int) -> None:
        """Every player a volume slider governs: the editor's song, its
        hitsounds, and song select's preview, which is a separate player."""
        self.player.setVolume(music_percent / 100.0)
        self.hitsounds.set_volume(hitsound_percent / 100.0)
        self._library.set_preview_volume(music_percent / 100.0)

    def _register_chart_view(self, view) -> None:
        """Take a chart view into the playhead broadcast, skin and all.

        Three places build chart views -- the player deck, the Editor page's
        bands and the gimmick page's layers -- and each used to append to
        `_chart_views` itself. Adding the skin to the Editor path therefore
        reached two of the three, and the gimmick layers stayed unskinned until
        Settings was applied again *after* they existed, which is why the same
        chart looked different on the two pages. Going through one place is
        what stops the next per-view property doing the same thing.
        """
        view.skin = getattr(self, "skin", None)
        view.set_note_opacity(
            getattr(self, "note_opacity_percent", NOTE_OPACITY_DEFAULT_PERCENT))
        self._chart_views.append(view)

    def _apply_appearance_settings(self) -> None:
        """Load the chosen skin once and hand it, and the note opacity, to
        every view that draws a note.

        Loaded here rather than per view: the pixmaps and their scaled cache
        are the same for all of them, and a gimmick page can hold six.
        """
        self.note_opacity_percent = self.settings.int_value(
            "appearance/note_opacity", NOTE_OPACITY_DEFAULT_PERCENT)
        name = self.settings.string_value("appearance/skin", "")
        folder = None
        if name:
            root = skins_root(self.settings.string_value("library/songs_folder", ""))
            if root is not None and (root / name).is_dir():
                folder = root / name
        self.skin = TaikoSkin(folder)
        # Every view that draws a note, not just the gameplay preview: the
        # chart layers draw the same don and kat, and a skin that changed one
        # and not the other would read as a bug.
        for view in getattr(self, "_gameplay_views", ()):
            view.set_skin(self.skin)
        for view in getattr(self, "_chart_views", ()):
            view.skin = self.skin
            view.set_note_opacity(self.note_opacity_percent)
        if hasattr(self, "timeline"):
            self.timeline.skin = self.skin
            self.timeline.set_note_opacity(self.note_opacity_percent)
        self.hitsounds.set_skin_sounds(self.skin.sounds)
        if hasattr(self, "canvas"):
            self.canvas.animate_transforms = self.settings.bool_value("appearance/transform_animation", True)
        self._sync_backdrops()
        self._apply_view_opacity()
        # getattr: this first runs before the song list's page is built.
        if getattr(getattr(self, "_library", None), "chart_preview", None) is not None:
            self._library.chart_preview.set_skin(self.skin)
            self._library.set_chart_preview_share(self.settings.int_value(
                "song_select/preview_percent", SONG_SELECT_PREVIEW_DEFAULT_PERCENT))

    def maybe_check_for_updates(self) -> None:
        """Startup update check, on a worker thread so the window never waits.

        Skipped versions are filtered here rather than in updater.check_now:
        an explicit "Check for updates now" should still report the release the
        user once skipped.
        """
        if not self.settings.bool_value(updater.SETTING_CHECK_ON_STARTUP, True):
            return
        skipped = self.settings.string_value(updater.SETTING_SKIPPED_TAG, "")

        def finished(release: object, _reachable: bool) -> None:
            if release is not None and getattr(release, "tag", "") != skipped:
                updater.present_update(release, self.settings, self, self.restart_into_update)

        # Held on self so Qt does not destroy the thread while it is running.
        self._update_check = updater.CheckThread(self)
        self._update_check.result.connect(finished)
        self._update_check.start()

    def restart_into_update(self, staging) -> bool:
        """Quit into the staged build. False when the user stays for unsaved work.

        The save prompt is asked here because close() from code is not
        spontaneous, and closeEvent only asks for a quit the user started.
        The helper is launched before closing so a failure to start it leaves
        the window up rather than the app gone and nothing installed.
        """
        if not self._confirm_leaving_editor():
            return False
        try:
            updater.launch_apply(staging)
        except OSError as error:
            QMessageBox.warning(
                self,
                tr("MainWindow", "Update failed"),
                tr("MainWindow", "The update could not be installed. Your current copy was not changed.")
                + "\n\n" + str(error),
            )
            return False
        self.close()
        QApplication.quit()
        return True

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self.shortcuts, self)
        # Applied from here because settings_dialog cannot import gui -- gui
        # imports it. Same route ImageTraceDialog's result takes.
        # The volume sliders are heard while they move; nothing is written
        # until OK, and the re-apply below puts a cancelled change back.
        def live_volumes(_value=None):
            self._set_volumes(dialog.music_volume.value(), dialog.hitsound_volume.value())
        dialog.music_volume.valueChanged.connect(live_volumes)
        dialog.hitsound_volume.valueChanged.connect(live_volumes)
        # Same for the backdrop's opacity; after exec the saved value is read
        # back, which is the restore on Cancel and a no-op after OK.
        dialog.background_opacity.valueChanged.connect(self._sync_backdrops)
        dialog.view_opacity.valueChanged.connect(self._apply_view_opacity)
        dialog.song_select_preview.valueChanged.connect(self._library.set_chart_preview_share)
        # The theme too, live as it is picked (owner, 2026-10-08: no restart);
        # Cancel puts back the one the dialog opened on.
        opened_on = theme.active()

        def preview_theme(_index=None):
            name = str(dialog.theme_combo.currentData())
            if name != theme.active():
                theme.switch(name)
                self.retheme()
        dialog.theme_combo.currentIndexChanged.connect(preview_theme)
        # The opacity preview draws with the timeline's own note code, in
        # whichever skin the combo shows. Loaded once per name so dragging the
        # slider repaints without touching disk.
        previews: dict[str, TaikoSkin] = {}

        def paint_notes(painter, rect, name, percent):
            if name not in previews:
                root = skins_root(self.settings.string_value("library/songs_folder", ""))
                folder = root / name if name and root is not None and (root / name).is_dir() else None
                previews[name] = TaikoSkin(folder)
            paint_note_preview(painter, rect, previews[name], percent)
        dialog.note_opacity_preview.paint_notes = paint_notes
        accepted = dialog.exec()
        if not accepted and theme.active() != opened_on:
            theme.switch(opened_on)
            self.retheme()
        self._apply_audio_settings()
        self._sync_backdrops()
        self._apply_view_opacity()
        self._library.set_chart_preview_share(self.settings.int_value(
            "song_select/preview_percent", SONG_SELECT_PREVIEW_DEFAULT_PERCENT))

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(7)
        self.setCentralWidget(central)

        # Global header, shown on both pages: file/session actions that make
        # sense regardless of which page you're on, plus the page tabs
        # themselves (top-right). Difficulty/Play/Reset/AR/CS and the old
        # file-dialog export are Fancy-Arranger-specific and live on that
        # page instead -- see _build_fancy_arranger_page.
        # In a frame of its own, which NSO draws as a window on its wallpaper
        # (windose.app_chrome); every other theme sees the plain ground.
        self.app_header = QFrame()
        self.app_header.setObjectName("appHeader")
        header = QHBoxLayout(self.app_header)
        header.setContentsMargins(0, 0, 0, 0)

        open_button = QPushButton(tr("MainWindow", "Open .osu"))
        open_button.clicked.connect(self.open_map)
        open_button.setFocusPolicy(Qt.NoFocus)
        header.addWidget(open_button)

        self.undo_button = QPushButton(tr("MainWindow", "Undo"))
        self.undo_button.clicked.connect(self.undo)
        self.undo_button.setFocusPolicy(Qt.NoFocus)
        self.undo_button.setEnabled(False)
        header.addWidget(self.undo_button)

        self.redo_button = QPushButton(tr("MainWindow", "Redo"))
        self.redo_button.clicked.connect(self.redo)
        self.redo_button.setFocusPolicy(Qt.NoFocus)
        self.redo_button.setEnabled(False)
        header.addWidget(self.redo_button)

        self.save_all_button = QPushButton(tr("MainWindow", "Save"))
        self.save_all_button.setToolTip(tr("MainWindow", "Save every changed difficulty"))
        self.save_all_button.clicked.connect(self.save_all_states)
        self.save_all_button.setFocusPolicy(Qt.NoFocus)
        self.save_all_button.setEnabled(False)
        header.addWidget(self.save_all_button)

        self.export_new_difficulty_button = QPushButton(tr("MainWindow", "Export"))
        self.export_new_difficulty_button.setToolTip(tr("MainWindow", "Export as a new difficulty in the same folder"))
        self.export_new_difficulty_button.clicked.connect(self.export_new_difficulty)
        self.export_new_difficulty_button.setFocusPolicy(Qt.NoFocus)
        self.export_new_difficulty_button.setEnabled(False)
        header.addWidget(self.export_new_difficulty_button)

        # Quiet, not pink: a filled pink button is the page's one action (Save)
        # or a choice that is on. With every header button filled, 49 controls
        # on the Gimmick page were pink and none of them stood out.
        for button in (open_button, self.undo_button, self.redo_button,
                       self.export_new_difficulty_button):
            button.setProperty("role", "ghost")

        # Which chart this is, in the header: the difficulty in pink (it is
        # what tells two charts of one song apart), then artist - title and
        # mapper, which elide first when the header runs out of room. The
        # status message that used to sit here moved to the status bar.
        self.chart_version_label = QLabel()
        # The difficulty name is the mapper's text: plain, not markup.
        self.chart_version_label.setTextFormat(Qt.PlainText)
        self.chart_version_label.setStyleSheet(f"color: {ACCENT_PINK}; font-weight: 700; padding-left: 10px;")
        header.addWidget(self.chart_version_label)
        self.chart_title_label = ElidedLabel()
        self.chart_title_label.setStyleSheet("color: #aeb8c5;")
        header.addWidget(self.chart_title_label, 1)

        # Status/log line: the status bar, where the eye goes after acting.
        # ElidedLabel keeps the full text as its tooltip when it does not fit.
        self.status = ElidedLabel(tr("MainWindow", "Open a map to begin."))
        self.status.setAlignment(
            Qt.AlignLeft | Qt.AlignVCenter
        )
        # The mods in force, in osu!'s order, visible from every page -- the
        # strip that sets them is on two of the four.
        self.mods_chip = QLabel()
        # Outlined, not filled: a filled pink pill in this header is a button,
        # and this one does nothing when clicked.
        self.mods_chip.setStyleSheet(
            "background: rgba(255, 102, 170, 34); color: #ff9dcc; border: 1px solid #ff66aa;"
            " border-radius: 4px; padding: 1px 6px; font-weight: 700;")
        self.mods_chip.setVisible(False)
        header.addWidget(self.mods_chip)

        self.settings_button = QPushButton(f"⚙ {tr('SettingsDialog', 'Settings')}")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setToolTip(tr("MainWindow", "Open application settings"))
        self.settings_button.setAccessibleName("Settings")
        self.settings_button.setProperty("role", "ghost")
        self.settings_button.setFocusPolicy(Qt.NoFocus)
        self.settings_button.clicked.connect(self.open_settings)
        header.addWidget(self.settings_button)

        # Songs (the library browser you start on and Esc back to), then
        # Editor, then Fancy Arranger. Tabs sit rightmost in the global header.
        self.library_page_button = QPushButton(tr("MainWindow", "Songs"))
        self.editor_page_button = QPushButton(tr("MainWindow", "Editor"))
        self.gimmick_page_button = QPushButton(tr("MainWindow", "Gimmick"))
        self.fancy_arranger_page_button = QPushButton(tr("MainWindow", "Fancy Arranger"))
        page_buttons = (
            self.library_page_button, self.editor_page_button,
            self.gimmick_page_button, self.fancy_arranger_page_button,
        )
        for button in page_buttons:
            button.setCheckable(True)
        self.page_button_group = QButtonGroup(self)
        self.page_button_group.setExclusive(True)
        self.page_button_group.addButton(self.library_page_button, PAGE_LIBRARY)
        self.page_button_group.addButton(self.editor_page_button, PAGE_EDITOR)
        self.page_button_group.addButton(self.gimmick_page_button, PAGE_GIMMICK)
        self.page_button_group.addButton(self.fancy_arranger_page_button, PAGE_FANCY)
        self.library_page_button.setChecked(True)
        self.page_button_group.idClicked.connect(self._switch_page)
        # A sheen crosses every page tab on hover: the app's own pages in the
        # approved mockup (2026-10-07), all four since 2026-10-08 (owner).
        self._tab_sheens = [HoverSheen(button) for button in page_buttons]
        # One segmented control, the same pill group as the speed and mod
        # strips: four solid pink tabs read as four more actions. Wider
        # padding than a segment's, since these are the app's navigation.
        # Widths are pinned in showEvent, after the frame's sheet applies.
        self.page_tabs = segmented(page_buttons)
        self.page_tabs.setStyleSheet(
            SEGMENT_STYLE
            + "QFrame#segment QPushButton { padding: 5px 18px; font-size: 14px; }"
            + SEGMENT_GLIDE_STYLE
        )
        # The checked pill slides to the new tab rather than jumping.
        self._tab_glide = SegmentGlide(self.page_tabs, page_buttons)
        header.addWidget(self.page_tabs)

        root.addWidget(self.app_header)
        self._style_app_root()

        self.page_stack = QStackedWidget()
        self.library_page = self._library.build_page()
        self.page_stack.addWidget(self.library_page)
        self.editor_page = self._build_editor_page()
        self.page_stack.addWidget(self.editor_page)
        self.gimmick_page = self._build_gimmick_page()
        self.page_stack.addWidget(self.gimmick_page)
        self.fancy_arranger_page = self._build_fancy_arranger_page()
        self.page_stack.addWidget(self.fancy_arranger_page)
        self.page_stack.setCurrentIndex(PAGE_LIBRARY)
        root.addWidget(self.page_stack, 1)
        # The page left slides away and the new one arrives (_show_page).
        self._page_slide = PageSlide(self.page_stack, "#191f29")
        self._build_status_bar()

        # A transient toast for a refusal (see show_toast). Parented to
        # page_stack rather than added through it, so it floats above
        # whichever page is current as an ordinary un-managed child without
        # joining the QStackedLayout -- one instance serves every page.
        self._build_toast()
        # A ripple under the pointer on every button the window has built.
        # Lazy: a button never pressed costs an event filter, no widget.
        self._ripples = [PressRipple(button) for button in self.findChildren(QPushButton)]

        # Both pages' playback-speed button sets exist now; sync them once.
        self._change_playback_speed(1.0)
        # And again now that the views exist: the first call happens with the
        # audio settings, long before any view is built, so on startup nothing
        # was there to hand the skin to.
        self._apply_appearance_settings()

        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #191f29;
                color: #e8edf3;
            }
            /* The Editor/Gimmick pages' map backdrop: the chrome between it
               and the (opaque) lanes lets it through, and nothing else does. */
            QWidget[seeThrough="true"], QWidget#mapBackdrop, QWidget#mapBackdrop QLabel { background: transparent; }
            /* Under a stylesheet a menu item with no padding of its own gets
               no gap before its shortcut column: "Reload this beatmap\tF5"
               drew the F5 over the label's last letters. */
            QMenu::item { padding: 4px 12px; }
            QMenu::item:selected { background: #2a3341; }
            QPushButton {
                background: #f3a6bd; color: #17191f;
                border: 0;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 600;
            }
            QPushButton:hover { background: #f7bfd0; }
            QPushButton:disabled {
                background: #39414d;
                color: #7d8794;
            }
            /* Everything that is neither the page's action nor a choice
               that is on: quiet until hovered. */
            QPushButton[role="ghost"] {
                background: transparent; color: #aeb8c5;
                border: 1px solid transparent; padding: 5px 10px;
            }
            QPushButton[role="ghost"]:hover { background: #2a3341; color: #e8edf3; }
            QPushButton[role="ghost"]:disabled { background: transparent; color: #4d5664; }
            /* The window's own navy, so the widgets in it need no rule of
               their own: a status bar a shade darker than its combo box's
               parent painted every plain QWidget in it as a lighter block. */
            QStatusBar { border-top: 1px solid #303947; color: #aeb8c5; }
            QStatusBar::item { border: 0; }
            """ + control_stylesheet() + """
            QTabWidget::pane { border: 1px solid #303947; }
            QTabBar::tab {
                /* 8px/18px made the tab strip taller than the controls under
                   it and each tab wider than its own label needed. */
                background: #222a36;
                padding: 4px 11px;
            }
            QTabBar::tab:selected { background: #f3a6bd; color: #17191f; }
            """
        )

    def _build_status_bar(self) -> None:
        """Status message on the left, the current page's session settings
        on the right. Built after the pages, which own those widgets."""
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        # The page's own 8px gutter, or the message sits on the window edge.
        bar.setContentsMargins(8, 0, 8, 0)
        bar.addWidget(self.status, 1)
        bar.addPermanentWidget(self.gimmick_status_box)
        bar.addPermanentWidget(self.editor_status_box)
        self._sync_status_bar(self.page_stack.currentIndex())

    def _sync_status_bar(self, index: int) -> None:
        self.editor_status_box.setVisible(index == PAGE_EDITOR)
        self.gimmick_status_box.setVisible(index == PAGE_GIMMICK)

    # -- toast ---------------------------------------------------------------

    def _build_toast(self) -> None:
        """Build the one reusable overlay `show_toast` shows and hides.

        Click-through (`WA_TransparentForMouseEvents`) so a refusal can never
        eat the click that follows it -- "nothing to act on" should not also
        mean "and now the next click goes nowhere". Sized to its own text via
        `adjustSize` in `show_toast`, not a fixed box, so a short refusal does
        not sit inside acres of empty black.
        """
        self._toast = QLabel(self.page_stack)
        self._toast.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._toast.setWordWrap(True)
        self._toast.setAlignment(Qt.AlignCenter)
        self._toast.setStyleSheet(
            """
            QLabel {
                background: rgba(0, 0, 0, 179);
                color: #ffffff;
                border-radius: 10px;
                padding: 12px 20px;
                font-size: 14px;
            }
            """
        )
        # Opacity lives on a graphics effect rather than the stylesheet alpha:
        # QPropertyAnimation needs a Qt property to animate, and "opacity" on
        # QGraphicsOpacityEffect fades the label (background, text, border)
        # together instead of requiring the background and text alpha to be
        # animated in step.
        self._toast_opacity = QGraphicsOpacityEffect(self._toast)
        self._toast_opacity.setOpacity(1.0)
        self._toast.setGraphicsEffect(self._toast_opacity)
        self._toast.hide()

        self._toast_hide_timer = QTimer(self)
        self._toast_hide_timer.setSingleShot(True)
        self._toast_hide_timer.timeout.connect(self._start_toast_fade)

        # Out faster than in (mockup, 2026-10-09): 160ms, was 400.
        self._toast_fade = QPropertyAnimation(self._toast_opacity, b"opacity", self)
        self._toast_fade.setDuration(160)
        self._toast_fade.setEndValue(0.0)
        self._toast_fade.finished.connect(self._toast.hide)
        # In: it rises 16px with a slight overshoot as it fades up.
        self._toast_rise = QPropertyAnimation(self._toast, b"pos", self)
        self._toast_rise.setDuration(340)
        self._toast_rise.setEasingCurve(QEasingCurve.OutBack)
        self._toast_appear = QPropertyAnimation(self._toast_opacity, b"opacity", self)
        self._toast_appear.setDuration(200)
        self._toast_appear.setStartValue(0.0)
        self._toast_appear.setEndValue(1.0)

    def show_toast(self, text: str) -> None:
        """Flash `text` for 3 seconds, then fade it out over ~400ms.

        For refusals -- "nothing in the selected range" and the like -- that
        used to only ever reach the status line. The status line reads as a
        log a mapper checks later rather than an answer to the click they just
        made, so a refusal that produced no dialog and no visible change was
        easy to read as the app doing nothing. A second call while one is
        already showing replaces the text and restarts the 3 seconds rather
        than queuing a second toast behind it.
        """
        arriving = not self._toast.isVisible()
        self._toast_fade.stop()
        self._toast_opacity.setOpacity(1.0)
        self._toast.setText(text)
        self._toast.adjustSize()
        self._position_toast()
        self._toast.show()
        self._toast.raise_()
        if arriving and not reduced_motion():
            # A replacement swaps its text in place; only a new toast rises.
            rest = self._toast.pos()
            self._toast_rise.stop()
            self._toast_rise.setStartValue(rest + QPoint(0, 16))
            self._toast_rise.setEndValue(rest)
            self._toast_rise.start()
            self._toast_appear.start()
        self._toast_hide_timer.start(3000)

    def _start_toast_fade(self) -> None:
        self._toast_appear.stop()
        self._toast_fade.stop()
        self._toast_fade.setStartValue(self._toast_opacity.opacity())
        self._toast_fade.start()

    def _position_toast(self) -> None:
        """Centre the toast over `page_stack`, whichever page is current."""
        size = self._toast.sizeHint()
        container = self._toast.parentWidget()
        x = max(0, (container.width() - size.width()) // 2)
        y = max(0, (container.height() - size.height()) // 2)
        self._toast.setGeometry(x, y, size.width(), size.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Guarded: resize events can fire (from the initial self.resize() /
        # setCentralWidget()) before _build_ui has reached _build_toast.
        if hasattr(self, "_toast"):
            self._position_toast()

    def _build_fancy_arranger_page(self) -> QWidget:
        """Everything specific to the transform-and-preview workflow: its own
        sub-toolbar (Difficulty/Play/Reset/AR/CS/old file-dialog export), the
        canvas + transform controls, and the big note-scrolling timeline with
        its own snap combo and playback row.
        """
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(7)

        # The row is ~1800px of controls, and as a plain layout that was the
        # whole window's minimum width: anything narrower ran off the screen.
        # In a scroll area it keeps its one line and scrolls sideways instead.
        sub_toolbar_row = QWidget()
        sub_toolbar = QHBoxLayout(sub_toolbar_row)
        sub_toolbar.setContentsMargins(0, 0, 0, 0)
        sub_toolbar_scroll = QScrollArea()
        sub_toolbar_scroll.setWidget(sub_toolbar_row)
        sub_toolbar_scroll.setWidgetResizable(True)
        sub_toolbar_scroll.setFrameShape(QFrame.NoFrame)
        sub_toolbar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sub_toolbar_scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        sub_toolbar_scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:horizontal { background: transparent; height: 6px; }"
            "QScrollBar::handle:horizontal { background: #3a4554; border-radius: 3px; min-width: 32px; }"
            "QScrollBar::handle:horizontal:hover { background: #ff66aa; }"
            "QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page { width: 0; background: none; }")
        self.fancy_sub_toolbar = sub_toolbar_scroll

        self.play_button = QPushButton(tr("MainWindow", "Play"))
        self.play_button.clicked.connect(self.toggle_playback)
        self.play_button.setFocusPolicy(Qt.NoFocus)

        self.reset_button = QPushButton(tr("MainWindow", "Reset Applied Transforms"))
        self.reset_button.clicked.connect(self.reset_applied)
        self.reset_button.setFocusPolicy(Qt.NoFocus)
        # Quiet: the row has one action, Export. Reset is a way back, not the
        # thing the page is for.
        self.reset_button.setProperty("role", "ghost")
        self.reset_button.setStyleSheet("QPushButton { border: 1px solid #3a4554; }")

        # What it does: writes the transformed notes as a new .osu beside this
        # one. Saving over this difficulty is the header's Save (Ctrl+S).
        self.export_button = QPushButton(tr("MainWindow", "Export as new difficulty…"))
        self.export_button.setToolTip(tr("MainWindow", "Write the transformed notes to a new .osu difficulty next to this one"))
        self.export_button.clicked.connect(self.export_map)
        self.export_button.setFocusPolicy(Qt.NoFocus)

        for button in (
            self.play_button,
            self.reset_button,
            self.export_button,
        ):
            button.setEnabled(False)

        self.difficulty_combo = QComboBox()
        self.difficulty_combo.setMinimumWidth(260)
        self.difficulty_combo.setEnabled(False)
        self.difficulty_combo.currentIndexChanged.connect(self._difficulty_changed)
        self.difficulty_combo.setToolTip(tr("MainWindow", "Difficulty"))
        sub_toolbar.addWidget(self.difficulty_combo)
        # Play lives in the Timeline dock's transport, as on the other pages;
        # the button stays (Space and the tests drive it) but not in this row.
        self.play_button.hide()
        # Reset and Export are added once, at the far end (after Layout): a
        # second addWidget of a widget already in the row makes Qt free its old
        # item behind PySide's back, and the wrapper PySide keeps for it (made
        # when this row was attached) then claims freed memory. The next Qt
        # object allocated there reached eventFilter as a QWidgetItem -- an
        # access violation inside show(), about one test_gimmick_editor run in six.
        self.approach_rate_control=DifficultyValueControl("AR",10.0,"Approach Rate: 0 is slowest, 10 is fastest. Export default is 10.00.")
        self.circle_size_control=DifficultyValueControl("CS",7.0,"Circle Size: 0 is biggest, 10 is smallest. Starts at the map's own CS.")
        sub_toolbar.addWidget(self.approach_rate_control)
        sub_toolbar.addWidget(self.circle_size_control)
        page_layout.addWidget(sub_toolbar_scroll)

        # Every box but the canvas is a dock: moved to any edge, floated, closed
        # and brought back from the Layout menu, with the arrangement kept
        # between sessions (fancy/dock_state). Qt's own QDockWidget, inside a
        # QMainWindow of the page's own -- no dependency, and dragging,
        # floating and snapping come with it.
        docks = QMainWindow(page)
        docks.setWindowFlags(Qt.Widget)
        docks.setDockOptions(QMainWindow.AnimatedDocks | QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks)
        # The Transform dock takes the full height beside the canvas, as in
        # the mockup, rather than stopping at the timeline.
        docks.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)
        self.fancy_docks = docks
        self._style_fancy_docks()
        page_layout.addWidget(docks, 1)

        self.canvas = TransformCanvas()
        self.canvas.circle_size = self.circle_size_control.value()
        self.circle_size_control.changed.connect(self._canvas_circle_size_changed)
        self.canvas.background_dropped.connect(self._background_dropped)
        self.canvas.drag_offset_requested.connect(self._canvas_dragged)
        docks.setCentralWidget(self.canvas)

        right = QWidget()
        self.transform_controls_panel = right
        # The panel keeps the width its widest label needs; the dock around it
        # scrolls instead, so a small window scrolls rather than clipping.
        right.setMinimumWidth(340)
        right.setStyleSheet(TILE_GRID_STYLE)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(10)

        self.background_opacity_control = ParameterControl(
            {"key": "background_opacity", "label": "Background Opacity", "type": "int", "min": 0, "max": 100, "default": 55}
        )
        self.background_opacity_control.changed.connect(
            lambda: self.canvas.set_background_opacity(int(self.background_opacity_control.value()))
        )
        background_caption = QLabel(tr("MainWindow", "Background"))
        background_caption.setStyleSheet("color: #7d8794; font-size: 11px; font-weight: 700; padding-left: 6px;")
        background_caption.setToolTip(tr("MainWindow", "Background Opacity"))
        sub_toolbar.addWidget(background_caption)
        self.background_opacity_control.setToolTip(tr("MainWindow", "Background Opacity"))
        self.background_opacity_control.setMaximumWidth(220)
        sub_toolbar.addWidget(self.background_opacity_control, 1)
        sub_toolbar.addStretch(1)
        self.fancy_layout_button = QToolButton()
        self.fancy_layout_button.setText(tr("MainWindow", "Layout"))
        self.fancy_layout_button.setPopupMode(QToolButton.InstantPopup)
        self.fancy_layout_button.setFocusPolicy(Qt.NoFocus)
        self.fancy_layout_button.setStyleSheet(
            "QToolButton { background: transparent; color: #aeb8c5; border: 1px solid #3a4554;"
            " border-radius: 6px; padding: 5px 10px; font-weight: 600; }"
            "QToolButton:hover { background: #2a3341; color: #e8edf3; }")
        sub_toolbar.addWidget(self.fancy_layout_button)
        sub_toolbar.addWidget(self.reset_button)
        sub_toolbar.addWidget(self.export_button)
        # Its own height, plus the scroll bar's when the row is too wide.
        sub_toolbar_scroll.setFixedHeight(sub_toolbar_row.sizeHint().height() + 8)

        self.mode_combo = QComboBox()
        # Item data is the stable mode identifier. The display text is localized,
        # so mode logic reads currentData() and never currentText().
        self.mode_combo.addItem(tr("MainWindow", "All Notes"), "all")
        self.mode_combo.addItem(tr("MainWindow", "Split Don / Kat"), "split")
        self.mode_combo.currentIndexChanged.connect(
            self._rebuild_control_tabs
        )
        self.mode_combo.currentIndexChanged.connect(
            lambda _index: self.swap_don_kat_button.setVisible(self._is_split_mode())
        )
        right_layout.addWidget(self.mode_combo)
        self.mode_combo.setVisible(False)
        self.mode_buttons: dict[str, QPushButton] = {}
        for mode_id, label in (("all", tr("MainWindow", "All Notes")), ("split", tr("MainWindow", "Split Don / Kat"))):
            button = QPushButton(label)
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, m=mode_id: self.mode_combo.setCurrentIndex(self.mode_combo.findData(m)))
            self.mode_buttons[mode_id] = button
        mode_row = QHBoxLayout()
        mode_row.addWidget(segmented(list(self.mode_buttons.values())))
        self.mode_combo.currentIndexChanged.connect(self._sync_mode_buttons)
        self._sync_mode_buttons()
        # Beside the Don/Kat pills, and only in Split: it is what Split is for.
        # On the mode row it pushed the row wider than the dock and overlapped.
        self.swap_don_kat_button = QPushButton(tr("MainWindow", "Swap Don ↔ Kat"))
        self.swap_don_kat_button.setVisible(False)
        self.swap_don_kat_button.setProperty("role", "ghost")
        self.swap_don_kat_button.setStyleSheet("QPushButton { border: 1px solid #3a4554; }")
        self.swap_don_kat_button.setToolTip(tr("MainWindow", "Swap transformation, parameters, and position between Don and Kat"))
        self.swap_don_kat_button.clicked.connect(self._swap_don_kat_transformations)
        mode_row.addStretch(1)
        right_layout.addLayout(mode_row)

        # Split's "which half am I editing" as sub-pills under the mode. The
        # tab widget stays the model (one page per group, as _spec reads it);
        # its own tab bar is hidden and these drive it.
        self.split_side_buttons = [QPushButton(tr("MainWindow", "Don")), QPushButton(tr("MainWindow", "Kat"))]
        for index, button in enumerate(self.split_side_buttons):
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, i=index: self.control_tabs.setCurrentIndex(i))
        self.split_side_pills = segmented(self.split_side_buttons)
        side_row = QHBoxLayout()
        side_row.addWidget(self.split_side_pills)
        # The pill group's height, so the two read as one row of buttons
        # rather than a short ghost button beside a taller pill.
        self.swap_don_kat_button.setFixedHeight(self.split_side_pills.sizeHint().height())
        side_row.addWidget(self.swap_don_kat_button)
        side_row.addStretch(1)
        right_layout.addLayout(side_row)

        self.control_tabs = QTabWidget()
        self.control_tabs.tabBar().hide()
        self.control_tabs.setDocumentMode(True)
        self.control_tabs.setStyleSheet("QTabWidget::pane { border: 0; }")
        self.control_tabs.currentChanged.connect(self._sync_split_side_buttons)
        right_layout.addWidget(self.control_tabs, 1)
        self.mode_combo.currentIndexChanged.connect(self._sync_split_side_buttons)
        self._sync_split_side_buttons()

        self.apply_button = QPushButton(tr("MainWindow", "Transform Selected Notes"))
        self.apply_button.clicked.connect(self.apply_selection)
        self.apply_button.setFocusPolicy(Qt.NoFocus)
        self.apply_button.setEnabled(False)
        right_layout.addWidget(self.apply_button)
        # "Apply All Changes to Original File" is gone: a transform is one undo
        # step, which marks the difficulty changed, and the header's Save writes
        # it to this file with the same backup -- the button was Save twice.
        save_hint = QLabel(tr("MainWindow", "Transforming is one undo step. Save (Ctrl+S) writes it to this difficulty's file, with a backup."))
        save_hint.setWordWrap(True)
        save_hint.setStyleSheet("color: #7d8794; font-size: 11px;")
        right_layout.addWidget(save_hint)

        transform_scroll = QScrollArea()
        transform_scroll.setWidgetResizable(True)
        transform_scroll.setFrameShape(QFrame.NoFrame)
        transform_scroll.setWidget(right)
        smooth(transform_scroll)
        keep_width_under_scrollbar(transform_scroll, right_layout)
        self.fancy_transform_dock = QDockWidget(tr("MainWindow", "Transform"), docks)
        self.fancy_transform_dock.setObjectName("fancy_transform_dock")
        self.fancy_transform_dock.setWidget(transform_scroll)
        # Never narrower than the panel it holds: a saved layout from when the
        # panel was sized differently restored the dock at ~190px, and the
        # shape tiles and every number box were cut off at its edge.
        self.fancy_transform_dock.setMinimumWidth(
            right.minimumWidth() + transform_scroll.verticalScrollBar().sizeHint().width() + 4)
        docks.addDockWidget(Qt.RightDockWidgetArea, self.fancy_transform_dock)

        timeline_box = QWidget()
        layout = QVBoxLayout(timeline_box)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        timeline_controls = QHBoxLayout()
        # The same snap slider as the Editor and Gimmick pages, with its own
        # "Snap" caption; each page keeps its own divisor, as before.
        self.snap_combo = SnapSlider(4)
        self.snap_combo.currentIndexChanged.connect(self._snap_changed)
        timeline_controls.addWidget(self.snap_combo)

        self.timeline_info = QLabel(self._timeline_info_text("--", "--", "1/4"))
        self.timeline_info.setStyleSheet("color: #aeb8c5; padding-left: 10px;")
        timeline_controls.addWidget(self.timeline_info, 1)
        # The wheel's three steps as key caps, not a sentence at the end of
        # the info line.
        hints = QWidget()
        hints.setStyleSheet(TILE_GRID_STYLE)
        hint_row = QHBoxLayout(hints)
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_row.setSpacing(14)
        wheel = tr("MainWindow", "Wheel")
        hint_row.addWidget(key_hint([wheel], tr("MainWindow", "1 snap")))
        hint_row.addWidget(key_hint(["Shift", wheel], tr("MainWindow", "1 beat")))
        hint_row.addWidget(key_hint(["Ctrl", wheel], tr("MainWindow", "zoom")))
        timeline_controls.addWidget(hints)
        layout.addLayout(timeline_controls)

        self.timeline = TimelineGameplay()
        # Drawn as the Editor's chart views are -- notes on a centred row, the
        # ticks either side (owner, 2026-10-08): it was the one chart view
        # still on the old bottom baseline.
        self.timeline.set_symmetric(True, editor_view=False)
        self.timeline.selection_changed.connect(self._selection_changed)
        self.timeline.selection_finalized.connect(self._selection_finalized)
        self.timeline.seek_requested.connect(self.seek_audio)
        self.timeline.snap_changed_by_wheel.connect(self._snap_changed_by_wheel)
        self._register_chart_view(self.timeline)
        self._share_kiai_bands(self.timeline, sv=False)
        layout.addWidget(self.timeline, 0)
        # Time+percentage, the kiai/bookmark bar, and playback controls all
        # in one row -- matches the Editor page's own strip layout, per
        # owner feedback that this row previously had the timing bar split
        # out on its own and was missing the percentage the Editor strip has.
        timeline_row=QHBoxLayout();timeline_row.setSpacing(5)
        time_area=QWidget()
        time_area_layout=QHBoxLayout(time_area);time_area_layout.setContentsMargins(0,0,0,0);time_area_layout.addStretch()
        self.timeline_time=QLabel("00:00:000   0.0%");self.timeline_time.setAlignment(Qt.AlignCenter);self.timeline_time.setStyleSheet("font-size:18px;font-weight:700;");time_area_layout.addWidget(self.timeline_time)
        steady_readout(self.timeline_time)
        # Wide enough for the longest readout this label ever shows, measured
        # in its own font rather than assumed in pixels. Polished first, or the
        # measurement uses the inherited font instead of the 18px one above.
        self.timeline_time.ensurePolished()
        time_area.setMinimumWidth(self.timeline_time.fontMetrics().horizontalAdvance("00:00:000   100.0%") + 24)
        time_area_layout.addStretch();timeline_row.addWidget(time_area)

        # Same widget type as the Editor page's timing bar (TimingOverviewBar
        # can only live in one layout at a time, so this is a second
        # instance, kept in sync wherever self.timing_bar is updated -- see
        # self._timing_bars).
        self.fancy_timing_bar = TimingOverviewBar()
        self.fancy_timing_bar.seek_requested.connect(self.seek_audio)
        timeline_row.addWidget(self.fancy_timing_bar, 1)
        self._timing_bars.append(self.fancy_timing_bar)
        # Right-click-scrub target for a live drag-selection in self.timeline
        # -- see TimeAxisMixin._press_is_on_timing_bar. Set here, once
        # fancy_timing_bar exists, rather than at self.timeline's own
        # construction above.
        self.timeline.timing_bar = self.fancy_timing_bar

        self.timeline_play_button=QPushButton("▶");self.timeline_play_button.clicked.connect(self.toggle_playback);timeline_row.addWidget(self.timeline_play_button)
        # Song select's morphing play/pause (owner, 2026-10-09). The text
        # still flips between ▶ and ❚❚ -- it is what the row's widths are
        # measured from -- but drawn invisible, under PlayPauseGlyph.
        self.timeline_play_button.setStyleSheet("QPushButton { color: transparent; }")
        self.fancy_play_glyph = PlayPauseGlyph(self.timeline_play_button, "#17191f", "#17191f")
        self.playback_speed_buttons=[]
        for label,rate in (("25%",.25),("50%",.5),("75%",.75),("100%",1.0)):
            button=QPushButton(label);button.setCheckable(True);button.setProperty("playbackRate",rate)
            button.clicked.connect(lambda checked=False,r=rate:self._choose_speed(r));self.playback_speed_buttons.append(button)
        timeline_row.addWidget(segmented(self.playback_speed_buttons))
        layout.addLayout(timeline_row)

        self.fancy_density = DensityOverview()
        self.fancy_density.seek_requested.connect(self.seek_audio)
        layout.addWidget(self.fancy_density)
        self._density_views.append(self.fancy_density)

        # "&&": a single & in a dock title is taken as a mnemonic and vanishes.
        self.fancy_timeline_dock = QDockWidget(tr("MainWindow", "Timeline & playback").replace("&", "&&"), docks)
        self.fancy_timeline_dock.setObjectName("fancy_timeline_dock")
        self.fancy_timeline_dock.setWidget(timeline_box)
        self.fancy_timeline_dock.setMinimumHeight(120)
        docks.addDockWidget(Qt.BottomDockWidgetArea, self.fancy_timeline_dock)

        windose.dress_docks((self.fancy_transform_dock, self.fancy_timeline_dock))
        self._fancy_default_dock_state = docks.saveState()
        saved = self.settings.value("fancy/dock_state")
        if isinstance(saved, (QByteArray, bytes)) and saved:
            docks.restoreState(QByteArray(saved))
        else:
            # Sizes only mean anything once the page is laid out, so the first
            # open gets them on the next pass of the event loop.
            QTimer.singleShot(0, self._size_fancy_docks)
        layout_menu = QMenu(self.fancy_layout_button)
        layout_menu.aboutToShow.connect(lambda: self._fill_fancy_layout_menu(layout_menu))
        self.fancy_layout_button.setMenu(layout_menu)

        return page

    def _fill_fancy_layout_menu(self, menu: QMenu) -> None:
        """Each dock's own show/hide toggle -- how a closed one comes back --
        and a reset to the arrangement the page opens with."""
        menu.clear()
        for dock in (self.fancy_transform_dock, self.fancy_timeline_dock):
            menu.addAction(dock.toggleViewAction())
        menu.addSeparator()
        menu.addAction(tr("MainWindow", "Reset layout"), self._reset_fancy_layout)

    def _reset_fancy_layout(self) -> None:
        for dock in (self.fancy_transform_dock, self.fancy_timeline_dock):
            dock.setFloating(False)
            dock.show()
        self.fancy_docks.restoreState(self._fancy_default_dock_state)
        self._size_fancy_docks()

    def _size_fancy_docks(self) -> None:
        """The opening sizes: the Transform panel at the width its widest label
        needs (plus a scrollbar), the timeline at what its four rows need."""
        panel = self.transform_controls_panel.minimumWidth() + 24
        self.fancy_docks.resizeDocks([self.fancy_transform_dock], [panel], Qt.Horizontal)
        self.fancy_docks.resizeDocks([self.fancy_timeline_dock], [280], Qt.Vertical)

    def _sync_mode_buttons(self, _index: int = 0) -> None:
        current = self.mode_combo.currentData()
        for mode_id, button in self.mode_buttons.items():
            button.setChecked(mode_id == current)

    def _sync_split_side_buttons(self, _index: int = 0) -> None:
        if not hasattr(self, "split_side_pills"):
            return
        split = self._is_split_mode()
        self.split_side_pills.setVisible(split)
        current = self.control_tabs.currentIndex() if hasattr(self, "control_tabs") else 0
        for index, button in enumerate(self.split_side_buttons):
            button.setChecked(split and index == current)

    def _switch_page(self, index: int) -> None:
        current = self.page_stack.currentIndex()
        # Leaving the editor for the song list is the same door Esc uses, so
        # it asks the same question and tears down the same views.
        if index == PAGE_LIBRARY and current != PAGE_LIBRARY and not self._leave_editor():
            self._show_page(current)
            return
        # "No" in the gimmick entry dialog means stay where you were.
        if index == PAGE_GIMMICK and not self._enter_gimmick_page():
            self._show_page(current)
            return
        # Clicking the Editor tab directly (not "Edit This Difficulty")
        # skips _activate_state, so a difficulty whose views were torn down
        # by an earlier _leave_editor never gets them back on its own.
        if index == PAGE_EDITOR and self.state is not None:
            self._ensure_default_editor_views(self.state)
        if index != current:
            self._click_page_change()
        self._show_page(index)

    def _click_page_change(self) -> None:
        """The skin's difficulty click, for a page the user changed -- a tab,
        or Esc back to the songs -- at full level rather than the effects
        volume the difficulty list plays it at (owner, 2026-10-09). Not in
        _show_page: loading a map changes page too, and that is not a click."""
        self._library.play_ui_sound("select_difficulty", volume=1.0)

    def _reload_timing_bars(self, state, force: bool = False) -> None:
        """Rebuild every kiai/bookmark/marker bar from `state`.

        Every bar, always -- an earlier version of this skipped the ones whose
        page was hidden, which was faster and wrong: it left them stale until
        you switched pages, and it quietly turned an existing "deleting a green
        line clears its marker" test into one that passed because the bar had
        never been updated at all.

        The saving is taken without touching that guarantee. There is a bar on
        each of three pages, all showing the same document, and the derived
        data -- kiai spans, one marker per timing point, bookmarks, the preview
        time -- is identical for all of them. It is computed once here and
        handed to each bar, instead of each bar walking every timing point in
        the map for itself.
        """
        data = self._cached(
            "bar_data",
            lambda: timeline_bar_data(state.document, state.duration_hint),
        )
        for bar in self._timing_bars:
            bar.apply_document_data(data)

    def _show_page(self, index: int) -> None:
        """Switch page and keep the header tab that owns it checked."""
        leaving = self.page_stack.currentIndex()
        sliding = index != leaving and self.page_stack.isVisible() and not reduced_motion()
        before = self.page_stack.currentWidget().grab() if sliding else None
        self.page_stack.setCurrentIndex(index)
        if sliding:
            # Laid out before it is photographed: a page whose views were
            # just built still has its layout requests queued.
            for _ in range(2):
                QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
            self._page_slide.play(before, self.page_stack.currentWidget().grab(), 1 if index > leaving else -1)
        button = self.page_button_group.button(index)
        if button is not None:
            button.setChecked(True)
        self._sync_status_bar(index)
        self._sync_mod_controls()  # the mods chip is off song select
        # Which toolbox owns the number keys is a property of the page.
        self._tool_state.refresh_tool_shortcut_scope()
        # So is which difficulty is heard: the Play choice is the Editor
        # page's, and `_hitsound_state` reads the page to decide.
        if getattr(self, "state", None) is not None:
            self._apply_hitsound_source()
        if index == PAGE_LIBRARY and self.player.playbackState() == QMediaPlayer.PlayingState:
            self.toggle_playback()
        if index == PAGE_LIBRARY:
            # Arrows step through the songs straight away, no click first.
            self._library.song_list.setFocus()
            state = getattr(self, "state", None)
            if leaving != PAGE_LIBRARY and state is not None and state.source_path is not None:
                # The list back on the chart being edited: a song picked here
                # and then left by the Editor tab stayed selected, under the
                # edited song's music.
                self._library.select_chart(state.source_path)
            if leaving != PAGE_LIBRARY and state is not None and state.audio_path is not None:
                # The song goes on from the playhead, on the library's own
                # player: 1.00x and the track's own pitch whatever speed or
                # rate mod the editor was at.
                self._library.continue_song(
                    state.audio_path, round(self.timeline.current_time),
                    editor_timeline_metadata(state.document)[1])
        else:
            self._library.stop_preview()

    def keyPressEvent(self, event) -> None:
        """Esc goes back a step -- to the song list from anywhere else, and
        out of the app from the song list itself, since that is as far back
        as "back" goes.

        It only arrives here when the focused view did not want it: both
        editor views consume Esc to clear their own selection first, and pass
        it up untouched when they have nothing selected.
        """
        back = QKeySequence(self.shortcuts.sequence("back_to_songs"))
        if not back.isEmpty() and QKeySequence(event.keyCombination()) == back:
            # A focused view is let go of first, and the next Esc goes back
            # (owner, 2026-10-08) -- the same one-step-at-a-time order as a
            # selection, which the view itself clears before this.
            if self._unfocus_views():
                event.accept()
                return
            if self.page_stack.currentIndex() != PAGE_LIBRARY:
                self._back_to_library()
            else:
                self._confirm_exit_application()
            event.accept()
            return
        super().keyPressEvent(event)

    def _back_to_library(self) -> None:
        if self._leave_editor():
            if self.page_stack.currentIndex() != PAGE_LIBRARY:
                self._click_page_change()
            self._show_page(PAGE_LIBRARY)

    def _confirm_exit_application(self) -> None:
        """The song list is the front door, so "back" from here means "out".

        A plain yes/no gate, separate from `_confirm_leaving_editor`'s save
        prompt below: that one only fires when a difficulty actually has
        unwritten edits, so a stray Esc with nothing at stake would close the
        window with no way to undo it. This one asks unconditionally, and the
        unsaved-work check still runs afterward -- confirming twice on a dirty
        session is one click more than leaving it unguarded is worth.

        Built the same way as `_confirm_leaving_editor`'s box, by role rather
        than the static `QMessageBox.question` convenience -- so the same
        `_fake_message_box` test double answers both.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(tr("MainWindow", "Exit"))
        box.setText(tr("MainWindow", "Exit Taiko Fancy Arranger?"))
        yes_button = box.addButton(tr("MainWindow", "Yes"), QMessageBox.AcceptRole)
        box.addButton(tr("MainWindow", "No"), QMessageBox.RejectRole)
        box.setDefaultButton(yes_button)
        box.exec()
        if box.clickedButton() is not yes_button:
            return
        if self._confirm_leaving_editor():
            self.close()

    def _leave_editor(self) -> bool:
        """Confirm, then tear the open views down. False means "stay"."""
        if not self._confirm_leaving_editor():
            return False
        self._close_all_editor_views()
        return True

    def _close_all_editor_views(self) -> None:
        """Going back to the song list ends the page's views -- after writing
        down what they were, so coming back to the same difficulty restores
        them (`_ensure_default_editor_views`). The editor still does not
        accumulate every view of every song visited: a difficulty opened
        for the first time gets the default pair.
        """
        if getattr(self, "state", None) is not None:
            remembered = self._editor_view_layout()
            if remembered:
                self._session_views[self.state.source_path] = remembered
        self._remember_gimmick_order(closing=True)
        for frame in list(self._editor_views):
            self._close_editor_view(frame)

    def _editor_view_layout(self) -> list[tuple[str, Path, bool]]:
        """Every Editor-page view as it stands, top to bottom: (type as
        added, difficulty, locked). Gimmick-page bands are not in a
        difficulty group and are not listed."""
        views = []
        for i in range(self.editor_views_layout.count()):
            group = self.editor_views_layout.itemAt(i).widget()
            if group is None or group.layout() is None:
                continue
            for j in range(group.layout().count()):
                frame = group.layout().itemAt(j).widget()
                if isinstance(frame, EditorViewFrame):
                    views.append((frame.view_type, frame.difficulty_path, frame.locked))
        return views

    def _gimmick_band_order(self) -> list[tuple[str, object]]:
        """The gimmick page's bands, top to bottom: ("layer", layer_id) for
        the six fixed ones, ("view", frame) for one added with "+"."""
        order = []
        for i in range(self.gimmick_views_layout.count()):
            frame = self.gimmick_views_layout.itemAt(i).widget()
            if not isinstance(frame, EditorViewFrame):
                continue
            layer = getattr(frame, "gimmick_layer", None)
            order.append(("layer", layer) if layer is not None else ("view", frame))
        return order

    def _remember_gimmick_order(self, closing: bool = False) -> None:
        """Note the band order under the difficulty the layers were built
        for. `closing`: the added bands are about to be deleted, so they are
        written down by type and rebuilt next time."""
        built_for = getattr(self, "_gimmick_built_for", None)
        order = self._gimmick_band_order()
        if built_for is None or not any(kind == "layer" for kind, _ in order):
            return
        if closing:
            order = [(kind, value.view_type if kind == "view" else value) for kind, value in order]
            # The layers are rebuilt on the next visit, and noting the order
            # again then would overwrite this with one the closed bands are
            # already missing from.
            self._gimmick_built_for = None
        self._gimmick_orders[built_for] = order

    def _restore_gimmick_order(self, target: Path, layers: dict[str, EditorViewFrame]) -> None:
        """Put the bands back in the order last seen for `target`.

        The six layers are rebuilt on every visit and placed at the top, so
        without this a layer moved down came back up each time the tab was
        left, and a band added with "+" was lost with the song list. Layers
        the remembered order does not name (none, today) stay where the
        rebuild put them, after the ones it does."""
        order = self._gimmick_orders.get(target)
        if not order:
            return
        placed: list[EditorViewFrame] = []
        for kind, value in order:
            if kind == "layer":
                frame = layers.get(value)
            elif isinstance(value, EditorViewFrame):
                frame = value if value in self._editor_views else None
            else:
                count = len(self._editor_views)
                self._add_editor_view(value, target, container=self.gimmick_views_layout)
                frame = self._editor_views[-1] if len(self._editor_views) > count else None
            if frame is not None and frame not in placed:
                placed.append(frame)
        for index, frame in enumerate(placed):
            self.gimmick_views_layout.removeWidget(frame)
            self.gimmick_views_layout.insertWidget(index, frame)
        # "First" is what the tool row and copy/paste follow; keep the list
        # in the order the screen shows.
        self._gimmick_views.sort(key=self.gimmick_views_layout.indexOf)

    def _confirm_leaving_editor(self) -> bool:
        """Ask about unsaved difficulties. False means "stay where you are".

        A state the user already dismissed with "Continue Without Saving" is
        left out as long as nothing has changed on it since: `history.revision`
        bumps on every edit, so a match against the revision recorded at that
        click is exactly "still the same unsaved edits the user already said
        to ignore" -- and a mismatch means a real new edit, which is legitimate
        to ask about again.
        """
        dirty = [
            state for state in self._states.values()
            if state.history.dirty
            and self._discard_acknowledged.get(state.source_path) != state.history.revision
        ]
        if not dirty:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(tr("MainWindow", "Unsaved changes"))
        box.setText(tr("MainWindow", "Save your changes first?"))
        box.setInformativeText(
            tr("MainWindow", "These difficulties have edits that are not written to disk:")
            + "\n\n"
            + "\n".join(f"• {state.source_path.name}" for state in dirty)
        )
        save_button = box.addButton(tr("MainWindow", "Save"), QMessageBox.AcceptRole)
        leave_button = box.addButton(tr("MainWindow", "Continue Without Saving"), QMessageBox.DestructiveRole)
        box.addButton(tr("MainWindow", "Cancel"), QMessageBox.RejectRole)
        box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            self.save_all_states()
            return True
        if clicked is leave_button:
            for state in dirty:
                self._discard_acknowledged[state.source_path] = state.history.revision
            return True
        return False

    def closeEvent(self, event) -> None:
        # Same guard as Esc: quitting is the other way to walk away from
        # unwritten edits, and it is the one that cannot be undone.
        #
        # spontaneous() gates it: True only when the close came from the
        # window manager (the X button, Alt+F4) -- someone quitting. A
        # programmatic close() is code tidying up, including every test's
        # teardown, and a modal prompt there has nobody to answer it.
        if not event.spontaneous() or self._confirm_leaving_editor():
            # The render loop is a 4ms PreciseTimer, which Windows serves from
            # WINMM's multimedia-timer thread posting into the main queue. A
            # closed window whose object has not been collected yet kept
            # rendering, and a few dozen of them starved the main thread of
            # its own event-queue lock: test_gimmick_editor hung forever in
            # ScrollBarWheelTests.setUp, the 33rd window of the module.
            self.gameplay_render_timer.stop()
            self.info_timer.stop()
            self.settings.set_value("fancy/dock_state", self.fancy_docks.saveState())
            self._release_application_hooks()
            self._release_audio_file()
            self._library.stop_preview()
            self._library.jobs.shutdown()
            event.accept()
        else:
            event.ignore()

    def _release_application_hooks(self) -> None:
        """Unhook this window from QApplication once it is really closing.

        Two registrations in __init__ are on the *application*, which outlives
        every window: an event filter and a focusChanged slot. Neither is
        undone by close() or by the window being garbage collected, and Qt
        dispatches **every application event through every installed filter**
        -- so a second live registration doubles the per-event Python work, a
        third triples it, and so on.

        One window per run never noticed. The test suite builds one per test
        and noticed badly: twelve windows took 171ms to build the first and
        976ms the twelfth (5.7x), and the whole suite degraded quadratically
        past four hours. Releasing here makes that flat -- 164ms to 154ms over
        the same twelve -- with the same number of objects still alive, which
        is the proof that the cost was the fan-out and not the memory.

        Idempotent: removeEventFilter on an unregistered filter is a no-op, and
        the disconnect is guarded, so a second close (or a close after a
        failed one) is harmless.
        """
        app = QApplication.instance()
        if app is None:
            return
        app.removeEventFilter(self)
        try:
            app.focusChanged.disconnect(self._editor_view_focus_changed)
        except (RuntimeError, TypeError):
            # Already disconnected, or the C++ side is gone.
            pass

    def _audio_failed(self, _error, message: str) -> None:
        """Say so when a song will not decode.

        There is no longer a second decoder to fall back to, and that is the
        point: the engine decodes through Qt's FFmpeg decoder whichever
        platform backend is installed, so the Ogg files that used to need a
        fallback simply open.
        """
        self.show_toast(tr("MainWindow", "This song's audio could not be decoded."))

    def _release_audio_file(self) -> None:
        """Let go of the song file on close.

        Qt's media backend holds the decoded source open until it is cleared,
        which on Windows is a live file lock -- the song cannot be moved,
        renamed, or deleted while a closed-but-not-cleared window still names
        it. The test suite hit the same lock from the other side: tearDown's
        TemporaryDirectory cleanup raced the backend and failed intermittently
        with NotADirectoryError on the fixture's audio.mp3.
        """
        self.player.stop()
        self.player.setSource(QUrl())
        self.player.shutdown()

    # -- Song library page --------------------------------------------------

    def _library_cache_path(self) -> Path:
        """Where the song index lives: <AppData>/jimmyreturnz/TaikoFancyArranger.

        main() sets the organization and application names, without which
        AppDataLocation is a folder shared with every other app run by the
        same Python.

        Stays a real method (not delegated) because tests monkeypatch it
        per-instance to point a scan at a temp file; LibraryPageController
        calls back through self.window._library_cache_path() rather than a
        private copy so that patch is still seen.
        """
        root = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        return Path(root or str(Path.home())) / "song_index.json"

    # Read/write pass-throughs for the library widgets and scan state: the
    # test suite (and main()) reach for these directly on the window, so
    # LibraryPageController keeps owning them while these forward the name.
    @property
    def song_list(self): return self._library.song_list
    @property
    def difficulty_list(self): return self._library.difficulty_list
    @property
    def library_search(self): return self._library.library_search
    @property
    def scan_timer(self): return self._library.scan_timer
    @property
    def _library_songs(self): return self._library.library_songs
    @_library_songs.setter
    def _library_songs(self, value): self._library.library_songs = value
    @property
    def _library_cache(self): return self._library.library_cache
    @_library_cache.setter
    def _library_cache(self, value): self._library.library_cache = value
    @property
    def _listed_paths(self): return self._library.listed_paths
    @_listed_paths.setter
    def _listed_paths(self, value): self._library.listed_paths = value
    @property
    def _scan_iterator(self): return self._library._scan_iterator
    @property
    def _scan_files(self): return self._library._scan_files
    @property
    def library_group_combo(self): return self._library.library_group_combo
    @property
    def library_sort_combo(self): return self._library.library_sort_combo
    @property
    def original_metadata_check(self): return self._library.original_metadata_check

    # -- Editor page: page switcher + multi-difficulty view stacking --------

    def _new_backdrop_page(self) -> MapBackdrop:
        """A page floored with the active map's faded background; the Editor
        and Gimmick pages are the two that have one."""
        page = MapBackdrop()
        if not hasattr(self, "_backdrops"):
            self._backdrops: list[MapBackdrop] = []
        self._backdrops.append(page)
        return page

    def _apply_view_opacity(self, percent: int | None = None) -> None:
        """Every editor view's ground at View opacity: the setting, or
        `percent` while Settings is previewing one."""
        if percent is None:
            percent = self.settings.int_value("appearance/view_opacity", VIEW_OPACITY_DEFAULT_PERCENT)
        set_view_opacity(percent, [
            widget
            for kind in (EditorViewFrame, TimelineGameplay, SVEditorView, GameplayViewerView)
            for widget in self.findChildren(kind)
        ])

    def _style_fancy_docks(self) -> None:
        """The Fancy page's dock area, with NSO's window outline under a
        theme that draws window chrome (re-run on a theme switch)."""
        self.fancy_docks.setStyleSheet(
            "QDockWidget { color: #7d8794; font-weight: 700; }"
            "QDockWidget::title { background: #1e2530; padding: 4px 8px; border-bottom: 1px solid #303947; }"
            "QMainWindow::separator { background: transparent; width: 8px; height: 8px; }"
            "QMainWindow::separator:hover { background: #ff66aa; }"
            + (windose.DOCK_CHROME if windose.active() else "")
        )

    def _style_app_root(self) -> None:
        """NSO's desktop behind the whole window and its colours on the
        window's own title bar; nothing under any other theme."""
        self.centralWidget().setStyleSheet(windose.app_chrome() if windose.active() else "")
        windose.dress_title_bar(self)

    def retheme(self) -> None:
        """Redo what a theme switch does not reach on its own (theme.switch
        re-translates every stylesheet; paint code reads theme.color as it
        paints): sheets built from a computed colour, and cached layers."""
        apply_ui_font()
        self._style_app_root()
        for buttons in getattr(self, "_mod_buttons", ()):
            for mod, button in buttons.items():
                button.setStyleSheet(mod_pill_style(mod))
        self._apply_view_opacity()  # the frames' grounds are computed colours
        for bar in (*self.findChildren(TimingOverviewBar), *self.findChildren(DensityOverview)):
            bar.static_layer_dirty = True
        for backdrop in getattr(self, "_backdrops", ()):
            backdrop._baked = None
        for control in self.findChildren(DifficultyValueControl):
            control.fit_value_box()
        # Pens the views build once, some of them in screen colours.
        for view in (*self.findChildren(TimelineGameplay), *self.findChildren(SVEditorView),
                     *self.findChildren(GameplayViewerView)):
            view.restyle_pens()
        self._library.restyle()
        if hasattr(self, "fancy_docks"):
            self._style_fancy_docks()
            windose.dress_docks((self.fancy_transform_dock, self.fancy_timeline_dock))
        # Drawn once into pixmaps, in the colours of the theme they were made in.
        for tile in self.findChildren(QToolButton, "shapeTile"):
            tile.setIcon(shape_icon(tile.property("shapeName")))
        for widget in QApplication.allWidgets():
            widget.update()

    def _sync_backdrops(self, percent: int | None = None) -> None:
        """Hand every page's backdrop the active map's picture and the
        opacity (the setting, or `percent` while Settings is previewing one).
        Cheap when neither changed: they compare first."""
        if not hasattr(self, "_backdrops"):
            return  # the first appearance pass runs before any page exists
        path = self.current_background_path
        if percent is None:
            percent = self.settings.int_value("appearance/background_opacity", BACKDROP_OPACITY_DEFAULT_PERCENT)
        for page in getattr(self, "_backdrops", ()):
            page.set_background(path)
            page.set_opacity(percent)

    def _build_editor_page(self) -> QWidget:
        page = self._new_backdrop_page()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Editor page's own viewing-controls strip, left to right: beat-snap
        # (drives every open chart view together, not just one), merged
        # time+percentage, the timing bar (kiai/bookmarks/SV/BPM -- relocated
        # here from the old shared player deck, not rebuilt), play + speed,
        # then "open new view".
        strip = QHBoxLayout()
        strip.setSpacing(6)

        self.editor_snap_combo = SnapSlider(4)
        self.editor_snap_combo.currentIndexChanged.connect(self._editor_snap_changed)
        strip.addWidget(self.editor_snap_combo)

        self.editor_timeline_strip = QLabel()
        steady_readout(self.editor_timeline_strip)
        strip.addWidget(self.editor_timeline_strip)

        self.timing_bar = TimingOverviewBar()
        self.timing_bar.seek_requested.connect(self.seek_audio)
        strip.addWidget(self.timing_bar, 1)
        self._timing_bars.append(self.timing_bar)

        # Tool-row padding, not the window sheet's 8px/16px: this strip now
        # carries a snap combo, the readout, the timing bar, play, four speeds,
        # the Play difficulty and the view height, and the speed buttons were
        # the widest things on it for the least information.
        self.editor_play_button = QPushButton("▶")
        self.editor_play_button.setFocusPolicy(Qt.NoFocus)
        # Morphs like the Fancy Arranger's; the typed text stays for width.
        self.editor_play_button.setStyleSheet(TOOL_BUTTON_STYLE + " QPushButton { color: transparent; }")
        self.editor_play_glyph = PlayPauseGlyph(self.editor_play_button, "#17191f", "#17191f")
        self.editor_play_button.clicked.connect(self.toggle_playback)
        strip.addWidget(self.editor_play_button)

        self.editor_playback_speed_buttons = []
        for label, rate in (("25%", .25), ("50%", .5), ("75%", .75), ("100%", 1.0)):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("playbackRate", rate)
            button.clicked.connect(lambda checked=False, r=rate: self._choose_speed(r))
            self.editor_playback_speed_buttons.append(button)
        strip.addWidget(segmented(self.editor_playback_speed_buttons))
        self._build_mod_strip(strip)
        # Widths for this row are equalized in showEvent (see there): derived
        # from the polished labels instead of hard-coded pixels, which stopped
        # fitting the moment the UI font grew.

        # Every open view at once rather than a grip on each frame: the page
        # stacks views of one song and reading them together only works while
        # they share a scale, which is the same argument that makes zoom
        # page-level (see _zoom_changed). Fixed rather than minimum, so a view
        # cannot quietly grow past the size that was asked for.
        self.editor_view_height_spin = QSpinBox()
        self.editor_view_height_spin.setRange(TimelineGameplay.MIN_HEIGHT, 600)
        self.editor_view_height_spin.setSingleStep(10)
        self.editor_view_height_spin.setToolTip(tr("MainWindow", "View height"))
        self.editor_view_height_spin.setValue(self.settings.int_value(
            "editor/view_height", TimelineGameplay.DEFAULT_VIEW_HEIGHT))
        self.editor_view_height_spin.valueChanged.connect(self._editor_view_height_changed)
        # The status bar (see _build_status_bar) shows this only while this
        # page is. It held a "Hitsounds from" choice as well until 2026-10-07;
        # the owner dropped it, and hitsounds follow the view being edited.
        self.editor_status_box = QWidget()
        status_row = QHBoxLayout(self.editor_status_box)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(4)

        # The label is here rather than in the tooltip because a bare number
        # box says nothing about what it is a number of.
        height_box = QWidget()
        height_row = QHBoxLayout(height_box)
        height_row.setContentsMargins(0, 0, 0, 0)
        height_row.setSpacing(4)
        height_row.addWidget(QLabel(tr("MainWindow", "View size")))
        height_row.addWidget(self.editor_view_height_spin)
        status_row.addWidget(height_box)
        # A stepper nobody reaches for often: quiet, like the rest of the bar.
        for button in height_box.findChildren(QPushButton):
            button.setProperty("role", "ghost")

        layout.addWidget(opaque_strip(strip))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        views_container = QWidget()
        self.editor_views_layout = QVBoxLayout(views_container)
        # Edge to edge: see EditorViewFrame.FRAME_STYLE.
        self.editor_views_layout.setContentsMargins(0, 0, 0, 0)
        self.editor_views_layout.setSpacing(0)
        self.editor_views_layout.addStretch(1)
        scroll.setWidget(views_container)
        see_through(scroll, scroll.viewport(), views_container)
        add_view_on_empty_right_click(views_container, self._open_add_view_dialog)
        smooth(scroll)
        layout.addWidget(scroll, 1)
        layout.addWidget(add_view_row(self._open_add_view_dialog))

        # Global, not per-view: applies to whichever view has focus. Only
        # one of these two rows is visible at a time (chart vs SV).
        layout.addWidget(see_through_row(self._tool_state.build_global_tool_row()))
        layout.addWidget(see_through_row(self._tool_state.build_global_sv_tool_row()))

        # Difficulty (source path) -> that group's QVBoxLayout. Views append
        # to the bottom of their difficulty's group and are never movable;
        # new difficulties get a new group appended at the bottom of the stack.
        self._editor_view_groups: dict[Path, QVBoxLayout] = {}
        self._editor_views: list[EditorViewFrame] = []
        # What was open when the song list was last visited, per difficulty
        # opened, for this session only: (type, difficulty, locked), top to
        # bottom. Going back to the song list ends the page's views, and
        # coming back used to rebuild a bare chart + SV pair -- every view
        # opened and ordered by hand was gone.
        self._session_views: dict[Path, list[tuple[str, Path, bool]]] = {}
        # The gimmick page's band order per paired difficulty, likewise:
        # ("layer", layer_id) or ("view", frame-or-type) -- see
        # `_gimmick_band_order`.
        self._gimmick_orders: dict[Path, list[tuple[str, object]]] = {}
        return page

    # -- Gimmick page ---------------------------------------------------------

    def _build_gimmick_page(self) -> QWidget:
        """The gimmick editor's six layers, in the Editor page's own stack.

        Empty until a session is opened: which difficulty this page edits is
        decided by GimmickEntryDialog on first entry, not by whatever happened
        to be open.
        """
        page = self._new_backdrop_page()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Same viewing-controls strip the Editor page has, with its own widget
        # instances: a QWidget lives in exactly one layout, so these cannot be
        # the Editor page's own (see self._timing_bars on the same point).
        strip = QHBoxLayout()
        strip.setSpacing(6)

        self.gimmick_snap_combo = SnapSlider(4)
        self.gimmick_snap_combo.currentIndexChanged.connect(self._gimmick_snap_changed)
        strip.addWidget(self.gimmick_snap_combo)

        self.gimmick_timeline_strip = QLabel()
        steady_readout(self.gimmick_timeline_strip)
        strip.addWidget(self.gimmick_timeline_strip)

        self.gimmick_timing_bar = TimingOverviewBar()
        self.gimmick_timing_bar.seek_requested.connect(self.seek_audio)
        strip.addWidget(self.gimmick_timing_bar, 1)
        self._timing_bars.append(self.gimmick_timing_bar)


        self.gimmick_play_button = QPushButton("▶")
        self.gimmick_play_button.setFocusPolicy(Qt.NoFocus)
        self.gimmick_play_button.clicked.connect(self.toggle_playback)
        self.gimmick_play_button.setStyleSheet("QPushButton { color: transparent; }")
        self.gimmick_play_glyph = PlayPauseGlyph(self.gimmick_play_button, "#17191f", "#17191f")
        strip.addWidget(self.gimmick_play_button)

        self.gimmick_playback_speed_buttons = []
        for label, rate in (("25%", .25), ("50%", .5), ("75%", .75), ("100%", 1.0)):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setProperty("playbackRate", rate)
            button.clicked.connect(lambda checked=False, r=rate: self._choose_speed(r))
            self.gimmick_playback_speed_buttons.append(button)
        strip.addWidget(segmented(self.gimmick_playback_speed_buttons))
        self._build_mod_strip(strip)

        layout.addWidget(opaque_strip(strip))

        # Which file is edited and whose timing it follows: the status bar,
        # shown only on this page (see _build_status_bar). It was a row of
        # its own between the strip and the layers, and every pixel of this
        # page's height is a layer's.
        self.gimmick_status_box = QWidget()
        status_row = QHBoxLayout(self.gimmick_status_box)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        self.gimmick_status = QLabel()
        self.gimmick_status.setTextFormat(Qt.PlainText)  # file names, not markup
        status_row.addWidget(self.gimmick_status)
        # The entry question is asked once and then remembered forever, so
        # without this there was no way to correct the answer -- including the
        # common one of pairing before the properly-timed difficulty existed.
        self.gimmick_reference_button = QPushButton(tr("MainWindow", "Timing Reference..."))
        self.gimmick_reference_button.setFocusPolicy(Qt.NoFocus)
        self.gimmick_reference_button.clicked.connect(self._change_gimmick_reference)
        self.gimmick_reference_button.setProperty("role", "ghost")
        status_row.addWidget(self.gimmick_reference_button)
        # The Editor page's View size, for the layer bands (owner, 2026-10-09):
        # every band at once, for the same reason as there -- the six layers
        # are read together, so they share a height. Its own setting, since a
        # band and a full Editor view want different sizes.
        self.gimmick_view_height_spin = QSpinBox()
        self.gimmick_view_height_spin.setRange(TimelineGameplay.MIN_HEIGHT, 600)
        self.gimmick_view_height_spin.setSingleStep(10)
        self.gimmick_view_height_spin.setToolTip(tr("MainWindow", "View height"))
        self.gimmick_view_height_spin.setValue(self.settings.int_value(
            "gimmick/view_height", self.GIMMICK_LAYER_HEIGHT))
        self.gimmick_view_height_spin.valueChanged.connect(self._gimmick_view_height_changed)
        status_row.addWidget(QLabel(tr("MainWindow", "View size")))
        status_row.addWidget(self.gimmick_view_height_spin)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        # Kept, because every visit to this page rebuilds all six layers and a
        # rebuilt layout starts scrolled to the top -- see `_open_gimmick_layers`.
        self.gimmick_scroll = scroll
        views_container = QWidget()
        self.gimmick_views_layout = QVBoxLayout(views_container)
        self.gimmick_views_layout.setContentsMargins(0, 0, 0, 0)
        self.gimmick_views_layout.setSpacing(0)
        self.gimmick_views_layout.addStretch(1)
        scroll.setWidget(views_container)
        see_through(scroll, scroll.viewport(), views_container)
        add_view_on_empty_right_click(views_container, self._open_gimmick_add_view_dialog)
        smooth(scroll)
        layout.addWidget(scroll, 1)
        # Adds to the layer stack rather than to a difficulty group: the
        # gimmick page edits one difficulty, so there is one place it can go.
        self.gimmick_add_view_button = add_view_row(self._open_gimmick_add_view_dialog)
        layout.addWidget(self.gimmick_add_view_button)

        # One row per layer, only ever one visible: clicking a layer brings up
        # that layer's own toolbox. Six rows and not three shared ones because
        # the same word means a different structure per layer -- Don is a fake
        # slider in layer 2 and a barline note in layer 3 -- and because the
        # two gimmick SV layers carry tools the plain SV layer does not.
        self.gimmick_tool_rows: dict[str, QWidget] = {}
        self.gimmick_tool_buttons: dict[str, dict[str, QPushButton]] = {}
        self._gimmick_row_buttons: list[QPushButton] = []
        for layer_id, _view_type, _label in self.GIMMICK_LAYERS:
            row = self._build_gimmick_row(layer_id)
            self.gimmick_tool_rows[layer_id] = see_through_row(row)
            row.setVisible(False)
            layout.addWidget(row)

        self._gimmick_views: list[EditorViewFrame] = []
        return page

    # Per layer: (tool id, label). The digit is the button's position, so 1-N
    # line up with the shortcut keys whatever the layer.
    GIMMICK_TOOLSETS = {
        "chart": (
            ("select", "Select"), ("don", "Don"), ("kat", "Kat"),
            ("slider", "Slider"), ("spinner", "Spinner"), ("new_combo", "New Combo"),
        ),
        "fake_slider": (
            ("select", "Select"), ("regular", "Fake Slider"),
            ("don", "Don"), ("kat", "Kat"), ("shiny", "Shiny"),
            ("multi", "Multiple Fake Slider"),
            ("function", "Function"), ("convert", "Convert Notes"),
        ),
        "barline": (
            ("select", "Select"), ("don", "Don"), ("kat", "Kat"),
            ("red_line", "Red Line"), ("function", "Function"),
            ("convert", "Convert Notes"),
        ),
        "sv_chart": (
            ("select", "Select"), ("green_line", "Green Line"), ("function", "Function"),
        ),
        "sv_fake_slider": (
            ("select", "Select"), ("green_line", "Green Line"), ("function", "Function"),
        ),
        "sv_barline": (
            ("select", "Select"), ("green_line", "Green Line"), ("function", "Function"),
        ),
        # Both tools act on a dragged range rather than on a click: Kiai sets
        # the flag across it, Volume sweeps hitsound volume over it. Kiai used
        # to live in the fake-slider layer, which was never where it belonged --
        # a kiai section is read against every layer at once.
        "kiai_sound": (
            ("select", "Select"), ("kiai", "Kiai"), ("volume", "Volume"),
        ),
    }

    def _build_gimmick_row(self, layer_id: str) -> QWidget:
        """One layer's toolbox: numbered buttons plus its Config button.

        Built to the same shape as the Editor page's own tool rows -- the same
        leading caption, the same TOOL_BUTTON_HEIGHT, and the same equalized
        widths (applied in showEvent, since padding only lands after polish).
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        # Literal tr() calls, one per caption -- see AddViewDialog on why a
        # dict lookup here would escape the i18n coverage gate. The two gimmick
        # object layers say what their tools build: the same "Don" writes a fake
        # slider in one and a run of red lines in the other.
        if layer_id.startswith("sv_"):
            caption = tr("MainWindow", "SV tools:")
        elif layer_id == "fake_slider":
            caption = tr("MainWindow", "Fake Slider Note Tool:")
        elif layer_id == "barline":
            caption = tr("MainWindow", "Barline Note Tool:")
        elif layer_id == "kiai_sound":
            caption = tr("MainWindow", "Kiai / Volume tools:")
        else:
            caption = tr("MainWindow", "Note tools:")
        layout.addWidget(QLabel(caption))

        # Nine tools in the fake-slider layer plus Config, and every layer's
        # row is equalized to one shared set (see the note where
        # _gimmick_row_buttons is extended below) -- so the window stylesheet's
        # 8px/16px button padding, applied at full size, made "Multiple Fake
        # Slider" alone push the assembled row past a monitor's width. Same
        # fix EditorViewFrame's chrome buttons and the pink +/- spin buttons
        # already needed: override padding and font-size directly on the
        # button, which -- like those two -- leaves the inherited pink
        # background/hover/checked look alone, since a leaf widget's own
        # QPushButton{} rule only overrides the properties it names.
        # Widths are no longer equalized across layers (see
        # equalize_button_widths), which is what actually bought the room.
        gimmick_button_style = GIMMICK_TOOL_BUTTON_STYLE

        buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(row)
        group.setExclusive(True)
        for index, (tool_id, label) in enumerate(self.GIMMICK_TOOLSETS[layer_id], start=1):
            button = QPushButton(f"{index}. {tr('MainWindow', label)}")
            button.setCheckable(True)
            button.setFixedHeight(self.TOOL_BUTTON_HEIGHT)
            button.setStyleSheet(gimmick_button_style)
            button.setFocusPolicy(Qt.NoFocus)
            button.setChecked(tool_id == "select")
            button.toggled.connect(
                lambda checked, t=tool_id, lid=layer_id:
                    self._set_gimmick_tool(lid, t) if checked else None
            )
            group.addButton(button)
            buttons[tool_id] = button
            layout.addWidget(button)

        config_button = QPushButton(tr("MainWindow", "Config"))
        config_button.setFixedHeight(self.TOOL_BUTTON_HEIGHT)
        config_button.setStyleSheet(gimmick_button_style)
        config_button.setFocusPolicy(Qt.NoFocus)
        config_button.clicked.connect(lambda _=False, lid=layer_id: self._open_gimmick_config(lid))
        layout.addWidget(config_button)
        if layer_id == "barline":
            # Experimental Conversion (owner, 2026-10-09): a menu, not a tool,
            # since what is in it acts on the whole map and needs no drag.
            experimental = QPushButton(tr("MainWindow", "Experimental Conversion"))
            experimental.setFixedHeight(self.TOOL_BUTTON_HEIGHT)
            experimental.setStyleSheet(gimmick_button_style)
            experimental.setFocusPolicy(Qt.NoFocus)
            menu = QMenu(experimental)
            menu.addAction(tr("MainWindow", "Convert Whole Map, Keep SV…")).triggered.connect(
                lambda _=False: self._gimmick_pairing is not None
                and self._experimental_conversion(self._gimmick_pairing.target))
            # A drop-up (owner, 2026-10-10).
            experimental.clicked.connect(lambda _=False, b=experimental, m=menu: m.exec(
                b.mapToGlobal(QPoint(0, -m.sizeHint().height()))))
            self.gimmick_experimental_button = experimental
            layout.addWidget(experimental)

        layout.addStretch(1)
        self.gimmick_tool_buttons[layer_id] = buttons
        # Every gimmick row swaps into the same spot, so they are sized as one
        # set -- Config included, or it alone stays narrower than the tools.
        self._gimmick_row_buttons.extend([*buttons.values(), config_button])
        return row

    def _unfocus_views(self) -> bool:
        """Let go of the focused view on the current page: no rim, nothing
        faded, keyboard focus back on the window. Whether there was one."""
        frames = [frame for frame in (*self._editor_views, *self._gimmick_views)
                  if frame.focused and frame.isVisible()]
        if not frames:
            return False
        for frame in (*self._editor_views, *self._gimmick_views):
            frame.set_focused(False)
            frame.set_dimmed(False)
        focus = QApplication.focusWidget()
        if focus is not None and any(frame.isAncestorOf(focus) for frame in frames):
            self.setFocus(Qt.OtherFocusReason)
        return True

    def _mark_focused_view(self, view) -> None:
        """Rim the frame holding `view`, the one the tool row now acts on."""
        frames = (*self._editor_views, *self._gimmick_views)
        for frame in frames:
            frame.set_focused(frame.isAncestorOf(view))
        # The rest fade, on the focused view's own page only -- the other
        # page's views are not the ones being looked at.
        focused = next((f for f in frames if f.focused), None)
        page = next((column.parentWidget() for column in (self.editor_views_layout, self.gimmick_views_layout)
                     if focused is not None and column.parentWidget().isAncestorOf(focused)), None)
        for frame in frames:
            frame.set_dimmed(not frame.focused and page is not None and page.isAncestorOf(frame))

    def _show_gimmick_row_for(self, view) -> None:
        """Bring up the toolbox belonging to the layer that just got focus."""
        layer = getattr(view, "gimmick_layer", None)
        if layer is None:
            return
        for layer_id, row in self.gimmick_tool_rows.items():
            row.setVisible(layer_id == layer)
        self._active_gimmick_layer = layer
        self._tool_state.refresh_tool_shortcut_scope()

    def _set_gimmick_tool(self, layer_id: str, tool_id: str) -> None:
        """Set a tool on the one layer that owns it.

        Per layer, not globally: the layers have different toolsets, and a tool
        chosen in the barline layer means nothing in the fake-slider one.

        "Multiple Fake Slider" needs a run shape (start, end, distance) before
        it can place anything, so selecting it opens that config here -- once,
        on selection, not on every click a placement tool would otherwise take
        silently. Cancelling leaves nothing configured, so the button falls
        back to Select the same way a moved-away-and-back focus change does
        (see the `moved` branch in `_editor_view_focus_changed`) rather than
        sitting armed with a run nobody chose.
        """
        if tool_id == "multi":
            dialog = MultiFakeSliderDialog(self)
            if dialog.exec() != QDialog.Accepted:
                self.gimmick_tool_buttons[layer_id]["select"].setChecked(True)
                return
            self._multi_fake_slider_params = dialog.parameters()
        for frame in self._gimmick_views:
            if getattr(frame, "gimmick_layer", None) != layer_id:
                continue
            view = getattr(frame, "chart_view", None) or getattr(frame, "sv_view", None)
            if view is None:
                continue
            # Set every time, not only when New Combo is the button pressed:
            # it shares the row's exclusive button group, so the only signal
            # that it has been turned *off* is another tool being turned on.
            # Without this it latched -- pressed once, every note placed in the
            # layer afterwards carried the flag, with the button no longer even
            # highlighted.
            #
            # Chart views only: an SV view has no combo, and calling it there
            # raised before `set_tool` was ever reached -- which is why Green
            # Line and Function in layers 4, 5 and 6 did nothing at all.
            if hasattr(view, "set_new_combo"):
                view.set_new_combo(tool_id == "new_combo")
            # The Multiple tool's ghost has to draw the whole run, so the view
            # needs the run shape as well as the tool. Pushed here rather than
            # read back through the window, so the ghost is drawn from the same
            # three numbers `_multi_fake_slider_commands` will place from.
            if hasattr(view, "multi_run"):
                view.multi_run = (
                    (*self._multi_fake_slider_params,
                     self._gimmick_config("fake_slider").fake_slider_offset_ms)
                    if tool_id == "multi" else None
                )
            if tool_id != "new_combo":
                view.set_tool(tool_id)

    def _open_gimmick_add_view_dialog(self) -> None:
        """The gimmick page's "+": another band under the six fixed layers.

        The difficulty is not asked for -- the page edits the paired one, and a
        band showing a different map next to the six that don't would only be
        confusing. Everything else is the Editor page's own add-view path, so an
        added view is fully wired for editing rather than a read-only copy.
        """
        pairing = self._gimmick_pairing
        if pairing is None:
            QMessageBox.information(
                self,
                tr("MainWindow", "Open a map first"),
                tr("MainWindow", "Open a beatmap before adding a view."),
            )
            return
        label = tr("MainWindow", "Gimmick") + f": {pairing.target.name}"
        dialog = AddViewDialog(
            [(label, pairing.target)], self, pairing.target, gimmick=True)
        dialog.difficulty_combo.setEnabled(False)
        if dialog.exec() != QDialog.Accepted:
            return
        self._add_editor_view(
            dialog.selected_view_type(), pairing.target,
            container=self.gimmick_views_layout,
        )

    def _gimmick_config(self, layer_id: str) -> GimmickConfig:
        """The config the layer's tools build with. SV layers borrow the
        barline one, which is the only place they read a gimmick BPM from."""
        return self.gimmick_configs.get(layer_id, self.gimmick_configs["barline"])

    def _open_gimmick_config(self, layer_id: str) -> None:
        if layer_id not in self.gimmick_configs:
            layer_id = "barline"
        # The caution compares the two structure layers' spacing against each
        # other; an SV layer has no part in it, so it is shown the barline
        # config and the caution stays silent (its own spacing is the default).
        other = "fake_slider" if layer_id == "barline" else "barline"
        dialog = GimmickConfigDialog(
            self.gimmick_configs[layer_id], layer_id, self.gimmick_configs[other], self
        )
        if dialog.exec() == QDialog.Accepted:
            self.gimmick_configs[layer_id] = dialog.config()
            # Which lines each layer owns, and which of them are structure
            # centres, are read out of these configs on every refresh -- so a
            # changed BPM or offset only reaches the layers when one happens.
            self._refresh_gimmick_views()

    def _gimmick_index_path(self) -> Path:
        """Beside song_index.json -- see _library_cache_path on the location."""
        root = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        return Path(root or str(Path.home())) / "gimmick_index.json"

    def _enter_gimmick_page(self) -> bool:
        """Resolve which difficulty the gimmick editor edits. False = stay put.

        The pairing is asked once and then remembered permanently, so this is a
        no-op on every visit after the first.
        """
        if self.state is None:
            QMessageBox.information(
                self,
                tr("MainWindow", "Open a map first"),
                tr("MainWindow", "Open a beatmap before using the gimmick editor."),
            )
            return False

        key = index_key(self.state.source_path)
        pairing = self._gimmick_pairings.get(key)
        if pairing is not None and not pairing.target.is_file():
            # The remembered file is gone -- deleted by hand, or while testing.
            # A stale pairing would open a difficulty that no longer exists, so
            # forget it and ask again rather than failing on the way in.
            QMessageBox.information(
                self,
                tr("MainWindow", "Gimmick difficulty is missing"),
                tr("MainWindow", "{0} is no longer in the song folder, so its pairing was forgotten.")
                .format(pairing.target.name),
            )
            self._states.pop(pairing.target, None)
            del self._gimmick_pairings[key]
            save_index(self._gimmick_index_path(), self._gimmick_pairings)
            pairing = None
        if pairing is None:
            pairing = self._create_gimmick_pairing()
            if pairing is None:
                return False
            self._gimmick_pairings[key] = pairing
            save_index(self._gimmick_index_path(), self._gimmick_pairings)

        self._gimmick_pairing = pairing
        self._update_gimmick_status()
        self._open_gimmick_layers()
        return True

    def _update_gimmick_status(self) -> None:
        pairing = self._gimmick_pairing
        if pairing is None:
            return
        status = tr("MainWindow", "Editing: {0}").format(pairing.target.name)
        reference = pairing.reference or pairing.target
        status += "   " + tr("MainWindow", "Timing from: {0}").format(reference.name)
        self.gimmick_status.setText(status)

    def _timing_snapshot_of(self, path: Path) -> list[TimingPoint]:
        """Base timing read from `path`, preferring an open copy of it.

        A difficulty already open this session may carry unsaved edits, and the
        timing on screen is the one the answer is about -- re-reading the file
        would quietly snapshot a different grid than the one being pointed at.
        """
        state = self._states.get(path)
        document = state.document if state is not None else parse_osu(path)
        return snapshot_timing(document.timing_points)

    def _change_gimmick_reference(self) -> None:
        """Re-pick the difficulty the grid comes from, after the fact.

        The difficulty being edited is on the list too. It is the wrong answer
        once it has gimmicks of its own -- which is why the entry dialog leaves
        it out -- but it is the right one for a session that has not written any
        yet, or one deliberately built on its own timing.
        """
        pairing = self._gimmick_pairing
        if pairing is None:
            return
        choices = [
            Path(self.difficulty_combo.itemData(index))
            for index in range(self.difficulty_combo.count())
        ]
        if pairing.target not in choices:
            choices.append(pairing.target)
        # File names, not versions: two difficulties can share a version name,
        # and a list where two rows read the same cannot be chosen between.
        names = [path.name for path in choices]
        current = (pairing.reference or pairing.target).name
        name, accepted = QInputDialog.getItem(
            self,
            tr("MainWindow", "Timing reference"),
            tr("MainWindow", "Take snaps, scrolling and the BPM overlay from:"),
            names,
            names.index(current) if current in names else 0,
            False,
        )
        if not accepted or not name:
            return
        chosen = choices[names.index(name)]
        try:
            base_timing = self._timing_snapshot_of(chosen)
        except Exception as error:
            QMessageBox.warning(
                self,
                tr("MainWindow", "Could not read the timing reference"),
                tr("MainWindow", "{0} could not be read, so this difficulty's own timing is used instead.")
                .format(chosen.name) + f"\n\n{error}",
            )
            return
        if looks_gimmicked(base_timing):
            QMessageBox.warning(
                self,
                tr("MainWindow", "This difficulty already has gimmicks"),
                tr(
                    "MainWindow",
                    "Its timing is being used as the snap and scroll reference, so "
                    "both may behave erratically. Pairing a clean difficulty instead "
                    "avoids this.",
                ),
            )
        pairing.base_timing = base_timing
        pairing.reference = chosen
        save_index(self._gimmick_index_path(), self._gimmick_pairings)
        # Handed to the open layers rather than rebuilding them: the grid is all
        # that changed, and a rebuild would throw the cursor back to the top of
        # every band.
        for frame in self._gimmick_views:
            view = getattr(frame, "chart_view", None) or getattr(frame, "sv_view", None)
            if view is not None:
                view.set_base_timing(base_timing)
        self._update_gimmick_status()

    def _create_gimmick_pairing(self) -> GimmickPairing | None:
        """Ask the entry question, then create or adopt the target difficulty."""
        document = self.state.document
        source = self.state.source_path
        version = document.version or source.stem

        dialog = GimmickEntryDialog(version, self, self._timing_reference_choices(source))
        dialog.exec()
        action = dialog.selected_action()
        reference = dialog.selected_reference()
        dialog.deleteLater()
        if action == GimmickEntryDialog.CANCEL:
            return None

        # The base timing is snapshotted once, here, and drives snaps, scroll
        # and the BPM overlay for the life of the pairing. Taken from the chosen
        # reference difficulty when there is one, so the grid keeps describing
        # the *song* however many 60000 BPM lines land in the file being edited.
        base_timing, reference = self._reference_timing(document, reference)

        if looks_gimmicked(base_timing):
            QMessageBox.warning(
                self,
                tr("MainWindow", "This difficulty already has gimmicks"),
                tr(
                    "MainWindow",
                    "Its timing is being used as the snap and scroll reference, so "
                    "both may behave erratically. Pairing a clean difficulty instead "
                    "avoids this.",
                ),
            )

        if action == GimmickEntryDialog.USE_CURRENT:
            # Editing in place: the difficulty being edited *is* the timing
            # reference unless another was picked, so the pairing records it
            # rather than None. `_reference_timing` returns None when it fell
            # back to the open document, which is the same file -- naming it
            # is what lets the status line say where the grid comes from.
            return GimmickPairing(source, base_timing, reference or source)

        target = gimmick_path_for(source)
        if target.exists():
            QMessageBox.information(
                self,
                tr("MainWindow", "Gimmick difficulty already exists"),
                tr("MainWindow", "Opening the existing file instead of creating a second one:")
                + f"\n{target.name}",
            )
            return GimmickPairing(target, base_timing, reference)

        try:
            write_osu(
                document, target, gimmick_version_for(version),
                # The source's own values, not the export defaults: this is a
                # copy of a difficulty, not a new one.
                force_ar=difficulty_setting(document, "ApproachRate", 5.0),
                force_cs=difficulty_setting(document, "CircleSize", 5.0),
            )
        except Exception as error:
            QMessageBox.critical(self, tr("MainWindow", "Could not create the difficulty"), str(error))
            return None
        return GimmickPairing(target, base_timing, reference)

    def _timing_reference_choices(self, source: Path) -> list[tuple[str, Path]]:
        """Every difficulty in this song, for the timing reference combo.

        The one being entered from is included, and listed **first** so it is
        what the combo shows before the list is ever opened. It used to be
        excluded on the grounds that a difficulty is a poor reference for its
        own gimmicks -- true once the file fills with 60000 BPM lines, but the
        snapshot is taken *before* any of that, so its timing is still the
        song's at the moment it is read. Excluding it meant the common case --
        "this map is already timed, just use it" -- had no answer at all, and
        "Use This One" had to silently pick something else.
        """
        source_resolved = source.resolve()
        choices = [
            (self.difficulty_combo.itemText(i), Path(self.difficulty_combo.itemData(i)))
            for i in range(self.difficulty_combo.count())
        ]
        own = [c for c in choices if c[1].resolve() == source_resolved]
        others = [c for c in choices if c[1].resolve() != source_resolved]
        return own + others

    def _reference_timing(self, document, reference: Path | None):
        """(base timing snapshot, the file it came from or None).

        A reference that will not parse falls back to the open document rather
        than refusing to open the editor -- a missing grid is worth a warning,
        not a locked door.
        """
        if reference is not None:
            try:
                return snapshot_timing(parse_osu(reference).timing_points), reference
            except Exception as error:
                QMessageBox.warning(
                    self,
                    tr("MainWindow", "Could not read the timing reference"),
                    tr("MainWindow", "{0} could not be read, so this difficulty's own timing is used instead.")
                    .format(reference.name) + f"\n\n{error}",
                )
        return snapshot_timing(document.timing_points), None

    # Every layer has to share one screen, so each is a band rather than a
    # full-height view. Tall enough for a note and its snap ticks; anything
    # less and the SV graph has no room to say anything. This is the *view's*
    # height -- its frame adds a chrome row on top -- and the views scale their
    # drawing to it against TimelineGameplay.DESIGN_HEIGHT.
    GIMMICK_LAYER_HEIGHT = 88

    # The layers, top to bottom: the three object layers, an SV layer for each,
    # and last the one that spans them all. (id, view type, label).
    #
    # "kiai_sound" is an SV view with no `point_times` filter, so it draws every
    # timing point in the map -- red, green, and yellow where both share a
    # millisecond. That whole-map picture is the point of it: kiai and hitsound
    # volume are properties of the active timing point, so they are read against
    # every layer at once rather than against one structure's lines.
    GIMMICK_LAYERS = (
        ("chart", "chart", "Normal chart"),
        ("fake_slider", "chart", "Fake sliders"),
        ("barline", "chart", "Barline gimmicks"),
        ("sv_chart", "sv", "SV (normal chart)"),
        ("sv_fake_slider", "sv", "SV (fake sliders)"),
        ("sv_barline", "sv", "SV (barlines)"),
        ("kiai_sound", "sv", "Kiai and Sound Volume"),
    )

    # The predicate itself is module-level (TimelineGameplay is defined long
    # before this class and needs it to decide whether to cap a roll), and this
    # is the name the rest of the window and the tests reach it by.
    is_fake_slider = staticmethod(is_fake_slider)

    # AddViewDialog's chart-only types, and the gimmick layer each one is.
    # Adding "Barlines Only" from the dialog gets the same view the gimmick
    # page's third layer is, rather than a second implementation of it.
    CHART_ONLY_VIEWS = {
        "chart_regular": "chart",
        "chart_fake_slider": "fake_slider",
        "chart_barline": "barline",
    }
    # The same three layers as a scrolling gameplay preview. Only the object
    # filter carries over: the gameplay viewer has no tools, no red lines of its
    # own and no ghost, so the rest of _apply_chart_layer means nothing here.
    GAMEPLAY_ONLY_VIEWS = {
        "gameplay_regular": "chart",
        "gameplay_fake_slider": "fake_slider",
        "gameplay_barline": "barline",
    }

    def _shiny_times(self, document) -> set[int]:
        """Milliseconds the fake slider layer draws as a shiny note.

        A structure hangs off something: the note it decorates, or -- when
        there is no note -- its own red line. `shiny_offset_ms` after that
        thing is a shiny; `fake_slider_offset_ms` after it is a fake slider.

        **A note outranks a line, and is checked at both distances first.**
        That order is the whole subtlety, and it is what a real map looks
        like::

            87124  note            + 66666 BPM squash
            87125  red line        <- the don's barline, one forward
            87126  fake slider     + its own SV

        The object at 87126 is two milliseconds after its *note*, so it is a
        fake slider -- but one millisecond after the barline that happens to
        sit between them. Reading the nearest anchor of any kind called it a
        shiny. Asking the note first gets it right, and asking the line only
        when no note is in range still covers a structure standing on its own
        (our own tools write one that way when you place onto empty space).

        Position alone decides -- kiai has no say, and neither does whether
        the object has a line of its own. `+1` is a shiny and `+2` is a fake
        slider, everywhere, always.
        """
        return self._cached("shiny", lambda: self._compute_shiny_times(document))

    def _compute_shiny_times(self, document) -> set[int]:
        config = self._gimmick_config("fake_slider")
        shiny_offset = config.shiny_offset_ms
        slider_offset = config.fake_slider_offset_ms
        notes = {
            round(note.time) for note in document.hit_objects
            if not self.is_fake_slider(note)
        }
        reds = self._red_line_times(document)
        shiny = set()
        for at in self._fake_slider_times(document):
            if at - shiny_offset in notes:
                shiny.add(at)
            elif at - slider_offset in notes:
                continue
            elif at - shiny_offset in reds:
                shiny.add(at)
        return shiny

    def _fake_slider_lines(self, document) -> tuple[set[int], set[int]]:
        return self._cached("fs_lines", lambda: self._compute_fake_slider_lines(document))

    def _compute_fake_slider_lines(self, document) -> tuple[set[int], set[int]]:
        """(head times, every structure-line time) for every fake-slider-layer object.

        A shiny's head is its own red line, `shiny_offset_ms` before the
        object. A fake slider's is its own millisecond -- rule 1 in
        `_shiny_times` is exactly "a fake slider has its own line" -- except a
        Don/Kat, whose object sits on the *restore* line and whose head is the
        squash line `fake_slider_offset_ms` earlier, on the note it hides.
        Read off the gimmick BPM rather than guessed from distance alone: a
        plain fake slider is routinely placed that same distance from an
        unrelated note (see the owner's file in `HandAuthoredShinyTests`), and
        only the BPM says which of the two this is.

        Layer 2 shows only the head per structure -- see
        `_apply_chart_layer`'s `timing_line_times_for`. Layers 1 and 3 need
        every line a structure owns, head and (for a Don/Kat) its restore
        too, so neither mistakes one for barline material.
        """
        config = self._gimmick_config("fake_slider")
        reds_by_time: dict[int, list] = {}
        for point in document.timing_points:
            if point.uninherited:
                reds_by_time.setdefault(round(point.time), []).append(point)
        shiny_ats = self._shiny_times(document)
        fake_slider_ats = self._fake_slider_times(document)

        heads: set[int] = set()
        all_lines: set[int] = set()
        for at in shiny_ats:
            line = at - config.shiny_offset_ms
            heads.add(line)
            all_lines.add(line)
        for at in fake_slider_ats - shiny_ats:
            squash_at = at - config.fake_slider_offset_ms
            squashed = any(
                point.bpm == config.gimmick_bpm for point in reds_by_time.get(squash_at, ())
            )
            head = squash_at if squashed else at
            heads.add(head)
            all_lines.add(head)
            all_lines.add(at)
            # ...and the restore a millisecond later, where the layer writes
            # one. Gated on the dial rather than claimed unconditionally: at
            # +1ms an unrelated line is entirely possible, and taking it for
            # this structure would hand another layer's material to this one.
            if config.fake_slider_restore_after:
                all_lines.add(at + FAKE_SLIDER_RESTORE_OFFSET_MS)
        return heads, all_lines

    def _gimmick_bpms(self) -> set[float]:
        """Every BPM that means "this red line is a structure's centre".

        Both object layers, because they hold separate configs and either may
        be retuned: keying the structure walk on the barline layer's number
        alone left a fake slider's own line unrecognised the moment the two
        differed, and deleting one then left its note behind.
        """
        return {
            self._gimmick_config(layer).gimmick_bpm
            for layer in ("barline", "fake_slider")
        }

    def _fake_slider_line_times(self, document) -> set[int]:
        """Every millisecond carrying a fake slider's own red lines."""
        gimmick, restores = self._fake_slider_lines(document)
        return gimmick | restores

    def _gimmick_line_times(self, document) -> set[int]:
        return self._cached("gimmick_lines", lambda: self._compute_gimmick_line_times(document))

    def _compute_gimmick_line_times(self, document) -> set[int]:
        """Milliseconds carrying a structure's own gimmick line."""
        bpms = self._gimmick_bpms()
        return {
            round(point.time) for point in document.timing_points
            if point.uninherited and point.bpm in bpms
        }

    def _layer_object_filter(self, layer_id: str):
        """Which hit objects one layer shows. Shared by both view kinds."""
        if layer_id == "fake_slider":
            return self.is_fake_slider
        if layer_id == "barline":
            # A barline gimmick is red lines; its notes belong to layer 1.
            return lambda note: False
        return lambda note: not self.is_fake_slider(note)

    def _apply_chart_layer(self, view: TimelineGameplay, layer_id: str) -> None:
        """Restrict a chart view to one kind of object, with its red lines.

        The three object layers, in one place because the gimmick page and
        AddViewDialog both build them:

        * **chart** -- the real chart. Fake sliders are excluded: they are drawn
          objects, not notes, and layer 2 owns them. Red lines are shown for
          reference, and are not editable here.
        * **fake_slider** -- fake sliders alone. Red lines are shown here too:
          a fake slider is only fake because of the gimmick line squashing it,
          so the line is the object's other half. Double-clicking one opens its
          BPM, as in layer 3; everything else about it stays view-only, since
          only the object knows how to move the structure it belongs to.
        * **barline** -- no hit objects at all, because a barline gimmick is
          made of red lines. Every red line in the map is drawn; the ones that
          do not sit on an object are this layer's own, and double-clicking one
          opens its BPM.
        """
        view.object_filter = self._layer_object_filter(layer_id)
        view.show_timing_lines = True
        # Which red lines are structure centres, by exact BPM out of the layer
        # configs -- what a drag lands on the grid and what a right click
        # resolves to. See TimelineGameplay.gimmick_times.
        view.gimmick_times_for = self._gimmick_line_times
        # A fake slider's two lines are layer 2's; every other red line in the
        # map -- the chart's own timing, a barline gimmick's, one placed by
        # hand -- is the barline layer's to edit. Set on layer 1 as well so the
        # two draw the same line in the same colour.
        if layer_id in ("chart", "barline"):
            view.foreign_times_for = self._fake_slider_line_times
        if layer_id == "fake_slider":
            # Only the gimmick line each fake slider is squashed by, which for a
            # Don or Kat sits on the note rather than on the slider. Every other
            # red line in the map is another layer's business and just clutters
            # this one.
            view.timing_line_times_for = lambda document: self._fake_slider_lines(document)[0]
            # Right click on the line removes that line alone; on the slider
            # it still removes both.
            view.timing_delete_enabled = True
            # Dragging one moves the whole structure with it. This was off on
            # the grounds that a line pulled out from under its slider
            # dismantles the gimmick -- but a drag goes through `_expand_move`,
            # which grows whatever was grabbed into the structure around it, so
            # the slider travels with its line. Off, a red line that landed on
            # a fake slider was adopted by this layer and then could not be
            # moved at all, which is the reported dead end.
            view.timing_edit_enabled = True
            # ...and double-clicking that line opens it, the same dialog and
            # the same one-step EditTimingPoint the barline layer's lines get.
            # Its BPM is what squashes the object beside it, so it is as much
            # this layer's material as the object is -- but only for the
            # dialog: see TimelineGameplay.timing_dialog_enabled.
            view.timing_dialog_enabled = True
            # Double-clicking the *object* types its length instead. The
            # toolbox's Length applies to what it places next; this is how an
            # already-placed one is changed.
            view.note_length_dialog_enabled = True
            view.ghost_style = "fake_slider"
            # This layer draws only its own structures' lines, and their BPM is
            # the thing being tuned -- a shiny's retimed line especially, where
            # the number *is* the effect. Layer 1 stays unlabelled: it shows
            # every red line in the map for reference, and a number beside each
            # of them is noise on top of a chart you are reading for notes.
            view.show_bpm_labels = True
            # Two rows: fake sliders above, shiny notes below, snap ticks down
            # the middle. The two structures sit a millisecond apart, which at
            # any readable zoom is the same pixel column.
            view.split_rows = True
            view.shiny_times_for = self._shiny_times
        elif layer_id == "barline":
            view.timing_edit_enabled = True
            view.timing_dialog_enabled = True
            view.ghost_style = "barline"
            view.show_bpm_labels = True

    def _expand_move(self, state: DifficultyState, note_uids: set[int], point_uids: set[int]):
        """Grow what a drag grabbed into the whole structure around it.

        A gimmick object is never one thing. A fake slider is a slider plus the
        60000 BPM line squashing it and the restore line after it; a barline
        note is a gimmick line, its mirrored restore lines and (usually) a real
        note on the same millisecond. Dragging any of those and leaving the rest
        behind does not move the gimmick, it dismantles it.

        Membership is read off the layer configs' own offsets rather than
        guessed from proximity, so a structure placed with different settings
        than the ones currently in the toolbox moves only the part that still
        matches -- visibly wrong, rather than quietly dragging a neighbour's
        lines along.
        """
        document = state.document
        notes = {note.uid: note for note in document.hit_objects if note.uid in note_uids}
        points = {p.uid: p for p in document.timing_points if p.uid in point_uids}

        # Indexed once. A selection of a thousand barlines dragged at once would
        # otherwise scan the whole document a thousand times over.
        red_at: dict[int, list[TimingPoint]] = {}
        for point in document.timing_points:
            if point.uninherited:
                red_at.setdefault(round(point.time), []).append(point)
        notes_at: dict[int, list] = {}
        for note in document.hit_objects:
            notes_at.setdefault(round(note.time), []).append(note)

        fake_config = self._gimmick_config("fake_slider")
        barline_config = self._gimmick_config("barline")
        # Every spacing a Don or Kat could actually have written a line at:
        # Don's own (`spacing_ms`) and Kat's three independent pairs. No
        # longer a single formula's n/n+2/n+4 -- see `barline_note` -- so the
        # four values are read straight off the config instead of derived.
        gimmick_spacings = (
            barline_config.spacing_ms,
            barline_config.kat_spacing1_ms,
            barline_config.kat_spacing2_ms,
            barline_config.kat_spacing3_ms,
        )
        gimmick_bpms = self._gimmick_bpms()

        # A fake slider's own line(s) -- see `_shiny_times` and
        # `_fake_slider_lines` for the same classification, done once for a
        # whole layer rather than per selected note here.
        shiny_ats = self._shiny_times(document)
        for note in list(notes.values()):
            if not self.is_fake_slider(note):
                continue
            at_time = round(note.time)
            if at_time in shiny_ats:
                head_times = (at_time - fake_config.shiny_offset_ms,)
            else:
                squash_at = at_time - fake_config.fake_slider_offset_ms
                squashed = any(
                    point.bpm == fake_config.gimmick_bpm for point in red_at.get(squash_at, ())
                )
                # A Don/Kat also has the squash line hiding its note; a plain
                # fake slider has only its own line, which is `at_time` itself.
                head_times = (squash_at, at_time) if squashed else (at_time,)
                if fake_config.fake_slider_restore_after:
                    # The restore line is part of the structure, so it travels
                    # with it -- left behind, it retimes whatever the slider
                    # moved away from. See `fake_slider_restore_after`.
                    head_times += (at_time + FAKE_SLIDER_RESTORE_OFFSET_MS,)
            for at in head_times:
                for point in red_at.get(at, ()):
                    points.setdefault(point.uid, point)
            # Every sibling on the same millisecond, not only the one that was
            # actually grabbed: a shiny or a stacked fake slider is several
            # hit objects on one timestamp, and a shiny's own red line is
            # never a gimmick-BPM line for the loop below to find them through
            # -- it carries the chart's own BPM (possibly retimed), like every
            # other fake-slider-layer line now does.
            for sibling in notes_at.get(at_time, ()):
                if self.is_fake_slider(sibling):
                    notes.setdefault(sibling.uid, sibling)

        # A real note standing on a gimmick line is that structure's note --
        # both layers write one, and it is what layer 1 shows of them. Without
        # this, nudging or deleting it there left the bars behind at the old
        # time: an invisible section of chart with nothing on screen to explain
        # it. The loop below then grows the rest of the structure from the line.
        for note in list(notes.values()):
            if self.is_fake_slider(note):
                continue
            for point in red_at.get(round(note.time), ()):
                if point.bpm in gimmick_bpms:
                    points.setdefault(point.uid, point)

        # Which milliseconds carry a fake-slider-layer line, whatever BPM they
        # carry. A plain fake slider's own line is the chart's own BPM, so the
        # gimmick-BPM test below never recognised it and a drag on it moved the
        # line alone -- which is why editing was disabled in that layer instead
        # of fixed. The layer already answers this question for its own drawing.
        fake_slider_line_times = self._fake_slider_lines(document)[1]

        for point in list(points.values()):
            if not point.uninherited:
                continue
            at = round(point.time)
            if point.bpm not in gimmick_bpms and at not in fake_slider_line_times:
                continue
            # Both shapes at once: Don writes +/-`spacing_ms` and Kat writes
            # +/- each of its three independent spacings, all tried here
            # because there is no longer a formula relating one to the
            # others -- and only the offsets that actually carry a line
            # match, so trying every spacing for every gimmick line costs
            # nothing beyond a handful of extra dict misses.
            for value in gimmick_spacings:
                for offset in (-value, value):
                    for neighbour in red_at.get(at + offset, ()):
                        points.setdefault(neighbour.uid, neighbour)
            for note in notes_at.get(at, ()):
                notes.setdefault(note.uid, note)
            # ...and the drawn object one offset later, which is where a fake
            # slider or a shiny sits when the line was grabbed rather than the
            # object. Either offset: the two structures use their own, and only
            # the one that actually has sliders on it matches.
            for offset in (fake_config.fake_slider_offset_ms, fake_config.shiny_offset_ms):
                for note in notes_at.get(at + offset, ()):
                    if self.is_fake_slider(note):
                        notes.setdefault(note.uid, note)
            # The restore line and the slider it belongs to, in both
            # directions: grabbing the slider's line has to bring the restore
            # after it, and grabbing the restore has to bring the slider one
            # millisecond before it. See `fake_slider_restore_after`.
            if fake_config.fake_slider_restore_after:
                for neighbour in red_at.get(at + FAKE_SLIDER_RESTORE_OFFSET_MS, ()):
                    points.setdefault(neighbour.uid, neighbour)
                before = at - FAKE_SLIDER_RESTORE_OFFSET_MS
                if any(self.is_fake_slider(note) for note in notes_at.get(before, ())):
                    for neighbour in red_at.get(before, ()):
                        points.setdefault(neighbour.uid, neighbour)
                    for note in notes_at.get(before, ()):
                        if self.is_fake_slider(note):
                            notes.setdefault(note.uid, note)

        # Green lines ride along with the red line they are stacked on, or the
        # SV layers would be left pointing at a millisecond the structure has
        # moved off -- which is the same as losing them.
        moved_times = {round(point.time) for point in points.values() if point.uninherited}
        for point in document.timing_points:
            if not point.uninherited and round(point.time) in moved_times:
                points.setdefault(point.uid, point)

        return list(notes.values()), list(points.values())

    def _move_objects(self, difficulty_path: Path, note_uids, point_uids, delta_ms: float) -> None:
        """Apply a select-tool drag: retime what was grabbed, as one undo step."""
        state = self._states.get(difficulty_path)
        if state is None:
            return
        delta = round(delta_ms)
        if not delta:
            return
        notes, points = self._expand_move(state, set(note_uids), set(point_uids))
        commands = [
            SetNoteFields(note.uid, {
                "time": (note.time, int(note.time) + delta),
                "extras": (note.extras, extras_at(note, int(note.time) + delta)),
            })
            for note in notes
        ] + [
            EditTimingPoint(point.uid, {"time": (point.time, point.time + delta)})
            for point in points
        ]
        if not commands:
            return
        # Landing on an occupied millisecond replaces what is there, the same
        # as placing on one does. Without this a note dragged onto its
        # neighbour left two hit objects stacked on one timestamp -- one of
        # them permanently invisible, and both of them in the exported map.
        moving = {note.uid for note in notes}
        destinations = {int(note.time) + delta for note in notes if not self.is_fake_slider(note)}
        displaced = [
            note for note in state.document.hit_objects
            if note.uid not in moving
            and round(note.time) in destinations
            and not note.is_spinner
            and not self.is_fake_slider(note)
        ]
        if displaced:
            commands.insert(0, RemoveHitObjects(displaced))
        # And the same for lines, per kind: a red and a green legitimately
        # share a millisecond, two reds or two greens never do (osu! reads
        # the first red and the last green, and the other is clutter the
        # layers go on drawing and hit-testing).
        moving_points = {point.uid for point in points}
        landing = {(round(point.time) + delta, point.uninherited) for point in points}
        displaced_points = [
            point for point in state.document.timing_points
            if point.uid not in moving_points
            and (round(point.time), point.uninherited) in landing
        ]
        if displaced_points:
            commands.insert(0, RemoveTimingPoints(displaced_points))
        state.history.push(CompositeCommand(commands, "move_objects"), state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(difficulty_path)
            self._refresh_difficulty_sv_views(difficulty_path)

    def resnap_selection(self) -> None:
        """Put each selected object -- every object, with nothing selected --
        on the nearest line of its *own* grid, as one undo step.

        Its own grid, not the current divisor: a 1/3 note stays 1/3 with 1/4
        selected (`time_axis.own_divisor`). Only objects within
        `RESNAP_TOLERANCE_MS` move, which is the millisecond another editor's
        rounding leaves; anything farther off was put there on purpose.

        Fake sliders never move -- a gimmick lives on millisecond offsets from
        the grid -- and nothing is moved onto a millisecond another object
        holds, since two objects on one millisecond is one the player never sees.
        """
        if should_ignore_shortcut_focus(QApplication.focusWidget()):
            return
        view = self._active_chart_view
        if view is None:
            return
        path = self._difficulty_for_view(view)
        state = self._states.get(path)
        if state is None:
            return
        notes = view.selected_notes() or list(state.document.hit_objects)
        occupied = {round(note.time) for note in state.document.hit_objects}
        commands = []
        for note in notes:
            if self.is_fake_slider(note):
                continue
            target = resnap_time(view.snap_points, note.time)
            if target is None or int(target) == note.time or int(target) in occupied:
                continue
            occupied.discard(round(note.time))
            occupied.add(int(target))
            commands.append(SetNoteFields(note.uid, {"time": (note.time, int(target))}))
        if not commands:
            self.show_toast(tr("MainWindow", "Everything is already on its snap."))
            return
        state.history.push(CompositeCommand(commands, "resnap"), state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(path)
            self._refresh_difficulty_sv_views(path)
        self.status.setText(tr("MainWindow", "Resnapped {count} objects.").format(count=len(commands)))

    def _delete_gimmick_objects(self, difficulty_path: Path, note_uids=(), point_uids=()) -> None:
        """Delete a gimmick object with the structure around it, as one step.

        The same expansion a drag uses (`_expand_move`), for the same reason:
        removing the slider and leaving the 60000 BPM line squashing it does not
        delete the gimmick, it leaves the wreckage of one behind -- an invisible
        section of chart that nothing on screen explains any more. Right click
        and Delete both come through here, so the two agree.

        Green lines are the exception, and deliberately: an SV layer's line
        describes the chart's scroll speed at a millisecond rather than
        belonging to the object that happened to be there, and rebuilding a
        sweep because one note was retyped is worse than a line left behind.
        Red lines go.

        **The chart's own notes are the other exception, and this is where a
        delete stops matching a drag.** `_expand_move` pulls in the real note
        standing on a gimmick line because a structure and the note it
        decorates have to travel together -- but a gimmick is drawn *around*
        a note that was already there (`_without_redundant_note`), so deleting
        the gimmick and taking the note with it deletes a beat of the map
        nobody asked to lose. Removing a barline Don took the don with it;
        removing an anti-barline wall line took whatever note its squash line
        was hiding. Only what was actually selected, plus the gimmick's own
        drawn objects -- fake sliders, which exist for no other reason -- are
        taken. A structure that had to write its own note leaves that note
        behind, which is one Delete in layer 1 rather than lost music.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        notes, points = self._expand_move(state, set(note_uids), set(point_uids))
        wanted = set(note_uids)
        notes = [
            note for note in notes
            if note.uid in wanted or self.is_fake_slider(note)
        ]
        points = [point for point in points if point.uninherited]
        if not notes and not points:
            return
        commands = []
        if notes:
            commands.append(RemoveHitObjects(notes))
        if points:
            commands.append(RemoveTimingPoints(points))
        state.history.push(CompositeCommand(commands, "delete_gimmick"), state)
        # Note selection only: refresh_notes prunes the red-line selection
        # against the document by itself, but `selected` is original_index and
        # survives its notes, so it would keep drawing them as selected.
        gimmick_views = (
            getattr(frame, "chart_view", None) for frame in self._gimmick_views
        )
        for view in (*self._editor_views_for(difficulty_path), *gimmick_views):
            selection = getattr(view, "selected", None)
            if isinstance(selection, set):
                selection.clear()
        with self._refresh_cycle():
            self._refresh_difficulty_views(difficulty_path)
            self._refresh_difficulty_sv_views(difficulty_path)

    def _delete_timing_lines(self, difficulty_path: Path, uids) -> None:
        """Remove red lines, from a right click or a Delete on a selection.

        One command whatever the count, so clearing a generated run of a
        thousand lines is one Ctrl+Z rather than a thousand.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        wanted = set(uids)
        victims = [point for point in state.document.timing_points if point.uid in wanted]
        if not victims:
            return
        state.history.push(RemoveTimingPoints(victims), state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(difficulty_path)
            self._refresh_difficulty_sv_views(difficulty_path)

    def _generate_barlines(self, difficulty_path: Path, start_ms: float, end_ms: float) -> None:
        """The barline layer's function tool: a run of red lines over a range.

        The range's start is snapped the same way a placement is, so a generated
        run begins where a hand-placed line would have.
        """
        pairing = self._gimmick_pairing
        state = self._states.get(difficulty_path)
        if state is None or pairing is None:
            return
        # osu_snap_ms, not the bare fractional snap_time: everything committed
        # to a beat position goes through it (time_axis.osu_snap_ms), and
        # leaving this fractional was what let a downstream `round()` -- on
        # this start, or on a grid line derived from it -- round some but not
        # all positions up by a millisecond depending on parity.
        start_ms = osu_snap_ms(snap_time(pairing.base_timing, start_ms, self._gimmick_snap_divisor()))
        dialog = BarlineFunctionDialog(
            start_ms, end_ms, self,
            base_timing=pairing.base_timing, snap_divisor=self._gimmick_snap_divisor(),
            # Fake sliders are excluded: they are drawn objects rather than
            # notes, and a red line on one is the structure that makes it fake.
            note_times={
                round(note.time) for note in state.document.hit_objects
                if not self.is_fake_slider(note)
            },
            current_sv=sv_at(sorted_by_time(state.document.timing_points), start_ms),
            meter=self._gimmick_config("barline").red_line_meter,
        )
        accepted = dialog.exec() == QDialog.Accepted
        times = dialog.times() if accepted else []
        # Read before the dialog goes: the growth curve lives on its widgets.
        bpms = dialog.bpms(times) if accepted else []
        sv = dialog.sv_multiplier() if accepted else None
        meter = dialog.meter_spin.value() if accepted else 4
        dialog.deleteLater()
        if not times:
            return
        # Never a second uninherited point on a millisecond that already has
        # one: only the first is meaningful, and the rest are invisible clutter
        # that the layer would then draw and hit-test forever.
        taken = {round(p.time) for p in state.document.timing_points if p.uninherited}
        ordered = sorted_by_time(state.document.timing_points)
        points = []
        for at, bpm in zip(times, bpms):
            if at in taken:
                continue
            # Per point, not once for the range: hitsound volume is carried by
            # whichever timing point is in force, and a run long enough to be
            # worth generating routinely crosses a volume change. Taking the
            # range's opening volume would flatten every later change back to
            # it -- and taking no template at all would reset the whole run to
            # 100%, which is what this used to do.
            template = active_point_at(ordered, at)
            points.append(TimingPoint.uninherited_at(at, bpm, meter=meter, template=template))
            # Straight after its red line, never before: osu! resolves a shared
            # timestamp by file order, and the red one has just reset SV to
            # 1.0x, so the other way round the reset would win and the run
            # would flatten the chart's speed exactly as it did before this
            # existed. `sv_at` is read against the document as it was, not as
            # the run leaves it, so "current" means the speed being replaced.
            points.append(TimingPoint.inherited_at(
                at, sv if sv is not None else sv_at(ordered, at), template=template,
            ))
        if not points:
            return
        # Same reason as _place_gimmick: a thousand generated lines with kiai
        # off is a thousand ways to kill the section they were drawn across.
        carry_active_state(points, state.document.timing_points)
        state.history.push(self._insert_points_command(state.document, points), state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(difficulty_path)
            self._refresh_difficulty_sv_views(difficulty_path)

    def _edit_timing_line(self, difficulty_path: Path, uid: int) -> None:
        """Retype one timing line, from a double click on it.

        Red or green: the dialog reads which from the point itself, so a BPM
        line and an SV line are edited the same way and both can be moved to a
        different millisecond while they are open.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        point = next((p for p in state.document.timing_points if p.uid == uid), None)
        if point is None:
            return
        dialog = TimingLineDialog(point, self)
        accepted = dialog.exec() == QDialog.Accepted
        changes = dialog.changes(point) if accepted else {}
        dialog.deleteLater()
        if not changes:
            return
        state.history.push(EditTimingPoint(uid, changes), state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(difficulty_path)
            self._refresh_difficulty_sv_views(difficulty_path)

    def _sv_layer_times(self, layer_id: str, document) -> set[int] | None:
        """Milliseconds an SV layer owns, or None for a layer that owns them all.

        Its objects' own times, *and* those times shifted by the layer's
        `sv_offset_ms`. The shift is part of ownership rather than something
        applied afterwards: a layer recognises its green lines by exact
        millisecond, so if the offset moved a generated point without moving
        what the layer looks for, the point would vanish from the layer that
        made it. One number, read here and by the Generate dialog both.

        Both, not just the shifted set, because a map arrives with SV already on
        it. Owning only `note - 5` would have hidden every green line a mapper
        had ever put on a note, in the layer whose whole job is to show them.
        """
        return self._cached(
            f"sv_layer_times:{layer_id}", lambda: self._compute_sv_layer_times(layer_id, document),
        )

    def _compute_sv_layer_times(self, layer_id: str, document) -> set[int] | None:
        times = self._sv_layer_owned_times(layer_id, document)
        if times is None or layer_id != "sv_barline":
            return times
        # ...plus every green line that belongs to nothing at all: no note, no
        # fake slider, no red line. Owned by no layer, it was drawn in none of
        # them, while the Kiai and Sound Volume layer -- which owns every
        # millisecond -- listed it happily, so the map plainly had SV the SV
        # views denied. Unowned SV is barline SV; this layer already holds the
        # chart's own timing.
        #
        # Here and not in `_sv_layer_object_times`, which is what Generate
        # sweeps: an orphan is a line, not an object, and a sweep across the
        # barline layer targets red lines. This is only what the layer shows
        # and what a paste can land on.
        claimed = set()
        for other in ("sv_chart", "sv_fake_slider", "sv_barline"):
            claimed |= self._sv_layer_owned_times(other, document) or set()
        return times | {
            round(point.time) for point in document.timing_points
            if point.inherited and round(point.time) not in claimed
        }

    def _sv_layer_paste_times(self, layer_id: str, document) -> set[int] | None:
        """Milliseconds a paste in this layer lands on: one per object.

        Ownership deliberately covers an object's own millisecond *and* the one
        `sv_offset_ms` away, because a map arrives with SV already written on
        its notes and the layer has to show that too. Pasting onto the owned
        set therefore had two targets for every object -- with layer 4's
        default -5ms offset, four copied lines filled two notes, one line on
        each note and one 5ms before it, and half of them sat where the layer's
        own Generate never writes.

        A paste lands where Generate lands: the object's millisecond shifted by
        the layer's offset. Whatever else a layer owns and did not derive from
        an object -- the barline layer's orphan green lines -- is a line rather
        than an object and has no offset of its own, so it stays a target as it
        is.

        A shiny's own green line (`_shiny_green_line_times`) is the one thing
        shown in `sv_fake_slider` that must NOT stay a target: it sits between
        two of the layer's real fake sliders, and an index-mapped paste that
        landed on it ate a clipboard slot there and shifted every fake slider
        after the shiny by one -- four SV values copied off four fake sliders
        landed on the first three plus the shiny, not the four fake sliders in
        the destination range.
        """
        times = self._sv_layer_times(layer_id, document)
        if times is not None and layer_id == "sv_fake_slider":
            times = times - self._shiny_green_line_times(document)
        offset = self._gimmick_config(layer_id).sv_offset_ms
        if times is None or not offset:
            return times
        objects = self._sv_layer_object_times(layer_id, document) or set()
        return (times - objects) | {at + offset for at in objects}

    def _sv_layer_owned_times(self, layer_id: str, document) -> set[int] | None:
        """A layer's objects, and those times shifted by its `sv_offset_ms`."""
        return self._cached(
            f"sv_layer_owned_times:{layer_id}",
            lambda: self._compute_sv_layer_owned_times(layer_id, document),
        )

    def _compute_sv_layer_owned_times(self, layer_id: str, document) -> set[int] | None:
        times = self._sv_layer_object_times(layer_id, document)
        if times is None:
            return None
        offset = self._gimmick_config(layer_id).sv_offset_ms
        owned = times | {at + offset for at in times} if offset else times
        if layer_id == "sv_fake_slider":
            # A shiny's own millisecond, once a green line already sits there --
            # display-only, added after the offset shift so it stays put rather
            # than moving with sv_offset_ms. Not part of `_sv_layer_object_times`
            # itself: that set is what Generate sweeps and what a paste maps its
            # clipboard onto by index, and this millisecond is not a fake
            # slider's own -- folding it in there inserted an extra slot between
            # two real fake sliders, so a Generate run touched it uninvited and a
            # paste across a shiny shifted every value after it by one. See
            # `_shiny_green_line_times`.
            owned = owned | self._shiny_green_line_times(document)
        return owned

    def _shiny_green_line_times(self, document) -> set[int]:
        """A shiny's own millisecond, where something already put a green line.

        Not a fake slider object -- a shiny's speed is layer 4's -- but a green
        line already sitting there has to stay visible and editable rather than
        lost from every layer, the same reasoning `sv_barline`'s orphan lines
        get. Kept out of `_sv_layer_object_times` and added to ownership
        instead, so it can be seen and dragged without becoming a Generate
        target or an index-mapped paste slot.

        Measured on a placement into a gimmick map's dense timing (tens of
        thousands of points): a set intersection here, not `any(... for p in
        document.timing_points)` re-scanned per shiny. The scan is O(shiny
        groups x timing points) and every placement calls this once per SV
        layer refresh, so on a large map it dominated a single fake slider
        placement -- multiple seconds, almost all of it inside this one
        expression -- against a plain set build-and-intersect, which is O(shiny
        groups + timing points).

        `_cached`: called from `_sv_layer_owned_times` ("sv_fake_slider"),
        again from `_sv_layer_times`'s "sv_barline" branch (which asks every
        layer's owned times to find what nothing claims), and again from
        `_sv_layer_paste_times` -- several times over per refresh, same as
        `_shiny_times` below it.
        """
        return self._cached("shiny_green_lines", lambda: self._compute_shiny_green_line_times(document))

    def _compute_shiny_green_line_times(self, document) -> set[int]:
        shiny_ats = self._shiny_times(document)
        if not shiny_ats:
            return set()
        inherited_ats = {round(p.time) for p in document.timing_points if p.inherited}
        return shiny_ats & inherited_ats

    def _sv_layer_object_times(self, layer_id: str, document) -> set[int] | None:
        """Where an SV layer's objects actually are, before its offset.

        Each SV layer owns a disjoint set of milliseconds, matched exactly so
        the three of them stop showing each other's green lines:

        * `sv_chart` -- every real (non-fake-slider) hit object's millisecond,
          plus each shiny's red-line millisecond (`shiny_offset_ms` before its
          object). A shiny's line sits on the note it decorates and stays
          visible -- there is no squash to hide -- so its SV is the chart's to
          hold, same as the note beside it. Nothing else: a plain fake
          slider's own line has never been chart SV, which is the bug this
          replaced -- it used to add *every* fake slider's gimmick line, so a
          plain fake slider's SV leaked into the normal-chart layer even where
          no note exists.
        * `sv_fake_slider` -- every fake slider's own millisecond (which is
          now its object's, not a separate restore line). A shiny's
          millisecond is excluded -- its speed is set from the chart layer
          above. If it already carries an inherited point it still shows, via
          `_shiny_green_line_times` in `_sv_layer_owned_times` rather than
          here -- a line, not an object, the same distinction `sv_barline`'s
          orphans get below.
        * `sv_barline` -- every other uninherited point's millisecond: the
          chart's own timing, barline gimmicks, hand-placed red lines. Minus
          whatever the two layers above own.

        Nothing here is stored: the sets are recomputed from the document on
        every refresh, so a line placed in a layer shows up in it immediately.
        """
        return self._cached(
            f"sv_layer_object_times:{layer_id}",
            lambda: self._compute_sv_layer_object_times(layer_id, document),
        )

    def _compute_sv_layer_object_times(self, layer_id: str, document) -> set[int] | None:
        config = self._gimmick_config("fake_slider")
        fake_slider_ats = self._fake_slider_times(document)
        shiny_ats = self._shiny_times(document)
        plain_ats = fake_slider_ats - shiny_ats
        shiny_lines = {at - config.shiny_offset_ms for at in shiny_ats}

        if layer_id == "sv_chart":
            return {
                round(n.time) for n in document.hit_objects if not self.is_fake_slider(n)
            } | shiny_lines
        if layer_id == "sv_fake_slider":
            return plain_ats
        if layer_id == "sv_barline":
            return self._red_line_times(document) - shiny_lines - plain_ats
        return None

    def _fake_slider_times(self, document) -> set[int]:
        """Every fake slider's own millisecond. Shared, `_cached`: rebuilt by
        `_shiny_times`, `_fake_slider_lines` and this layer family alike, each
        of them a full pass over `document.hit_objects`."""
        return self._cached(
            "fake_slider_ats",
            lambda: {round(n.time) for n in document.hit_objects if self.is_fake_slider(n)},
        )

    def _red_line_times(self, document) -> set[int]:
        """Every uninherited point's millisecond. Shared, `_cached`, same
        reasoning as `_fake_slider_times`."""
        return self._cached(
            "red_line_ats",
            lambda: {round(p.time) for p in document.timing_points if p.uninherited},
        )

    def _open_gimmick_layers(self) -> None:
        """(Re)build the gimmick layers for the current pairing.

        Every layer shows the same document -- the gimmick editor edits exactly
        one difficulty -- and differs only in which objects it shows and which
        toolbox drives it. They all snap against the base timing snapshot rather
        than the document's own, which the gimmick lines make unusable.
        """
        pairing = self._gimmick_pairing
        if pairing is None:
            return

        # Where the user was looking, across the teardown below. Six layers plus
        # anything added with "+" is taller than the page, so leaving for
        # another tab and coming back landed you at the top every time -- the
        # views themselves keep their playhead, but the *page* forgot which of
        # them you had scrolled down to.
        scrolled_to = self.gimmick_scroll.verticalScrollBar().value()
        self._remember_gimmick_order()

        for frame in list(self._gimmick_views):
            # Unregister before deleting, or the playhead broadcast keeps
            # calling set_time on a deleted C++ object.
            for attribute, registry in (
                ("chart_view", self._chart_views), ("sv_view", self._sv_views),
            ):
                view = getattr(frame, attribute, None)
                if view is not None:
                    if view in registry:
                        registry.remove(view)
                    if view in self._editor_snap_views:
                        self._editor_snap_views.remove(view)
                    # ...and drop it as the focused view, the same way
                    # _close_editor_view does. A rebuild happens on every visit
                    # to the page, and a stale reference here is not a crash --
                    # PySide6 keeps the Python object alive -- it is copy and
                    # paste silently acting on a layer that is no longer there.
                    for focus in ("_active_chart_view", "_active_sv_view",
                                  "_last_focused_editor_view"):
                        if getattr(self, focus, None) is view:
                            setattr(self, focus, None)
            self.gimmick_views_layout.removeWidget(frame)
            frame.deleteLater()
        self._gimmick_views.clear()

        try:
            state = self._ensure_state(pairing.target)
        except Exception as error:
            QMessageBox.critical(self, tr("MainWindow", "Open failed"), str(error))
            return

        divisor = self._gimmick_snap_divisor()
        for layer_id, view_type, label in self.GIMMICK_LAYERS:
            frame = EditorViewFrame(view_type, label, compact=True)
            frame.difficulty_path = pairing.target
            frame.gimmick_layer = layer_id

            if view_type == "chart":
                view = TimelineGameplay()
                view.set_symmetric(True)
                # A gimmick object is placed by hand and then nudged; the layers
                # show one kind of thing each, so a press that lands on
                # something is unambiguous about what it means to move.
                view.move_enabled = True
                self._apply_chart_layer(view, layer_id)
                view.timing_line_edit_requested.connect(
                    lambda uid, dp=pairing.target: self._edit_timing_line(dp, uid)
                )
                view.load_document(state.document)
            else:
                view = SVEditorView()
                view.point_times_for = (
                    lambda document, lid=layer_id: self._sv_layer_times(lid, document)
                )
                # The BPM belongs beside the lines themselves, which is layer 3.
                view.show_bpm_labels = layer_id != "sv_barline"
                # Layer 7 graphs note volume on a 0-100% axis, not scroll
                # speed. Set here as well as in _add_editor_view because the
                # gimmick page builds its bands itself rather than going
                # through that path -- and the band *is* where this layer
                # normally lives, so missing it here left the curve reading
                # SV in the one place anybody looks at it.
                view.volume_mode = layer_id == "kiai_sound"
                view.timing_line_edit_requested.connect(
                    lambda uid, dp=pairing.target: self._edit_timing_line(dp, uid)
                )
                # The same editing signals an Editor-page SV view gets. Without
                # these the SV layers were read-only: green-line placement,
                # dragging, deletion and Generate all reached nothing.
                # `lid` routes Generate to the layer's own objects, so a sweep
                # in layer 5 lands on fake sliders and nothing else.
                view.function_range_requested.connect(
                    lambda start_ms, end_ms, dp=pairing.target, v=view, lid=layer_id:
                        self._sv_range_action(dp, v.tool, start_ms, end_ms, layer_id=lid)
                )
                view.point_add_requested.connect(
                    lambda time_ms, sv, dp=pairing.target: self._add_sv_point(dp, time_ms, sv)
                )
                view.point_time_edit_requested.connect(
                    lambda uid, time_ms, dp=pairing.target: self._edit_sv_point_time(dp, uid, time_ms)
                )
                view.point_sv_edit_requested.connect(
                    lambda uid, sv, dp=pairing.target: self._edit_sv_point(dp, uid, sv)
                )
                view.point_volume_edit_requested.connect(
                    lambda uid, volume, dp=pairing.target:
                        self._edit_volume_point(dp, uid, volume)
                )
                view.point_volume_dialog_requested.connect(
                    lambda uid, dp=pairing.target: self._type_volume_point(dp, uid)
                )
                view.point_delete_requested.connect(
                    lambda uid, dp=pairing.target: self._delete_sv_point(dp, uid)
                )
                view.points_delete_requested.connect(
                    lambda uids, dp=pairing.target: self._delete_sv_points(dp, uids)
                )
                view.load_document(state.document)

            view.set_base_timing(pairing.base_timing)
            # None of the six layers set their own zoom otherwise, so a chart
            # band opened at TimelineGameplay's 2000ms default and an SV band
            # at SVEditorView's 4000ms -- two different scales for views of
            # the same document that are supposed to line up.
            view.window_ms = self.DEFAULT_EDITOR_WINDOW_MS
            # Right-click-scrub target (see TimeAxisMixin._press_is_on_timing_bar):
            # every other view gets this, and the six layers were the one
            # place left without it.
            view.timing_bar = self.gimmick_timing_bar
            view.set_snap_divisor(divisor)
            view.current_time = state.playhead_ms
            view.seek_requested.connect(self.seek_audio)
            # Alt+wheel. Without this the view that caught the wheel changed its
            # own divisor and nothing else -- the combo went stale and the other
            # five layers kept the old grid, so which snap a placement used
            # depended on which band the pointer happened to be over.
            view.snap_changed_by_wheel.connect(self._gimmick_snap_changed_by_wheel)
            if view_type == "chart" and layer_id != "chart":
                # The tool carries the shape, the layer carries the kind.
                view.note_place_requested.connect(
                    lambda kind, time_ms, _combo, big, lid=layer_id:
                        self._place_gimmick(lid, kind, time_ms, big)
                )
            elif view_type == "chart":
                # Layer 1 is an ordinary chart, so its tools are the ordinary
                # ones -- and they were connected to nothing at all, which left
                # its whole toolbox (Don, Kat, Slider, Spinner, New Combo)
                # inert. Routed to the same handlers an Editor-page chart view
                # uses, so this stays a normal chart rather than being hijacked
                # into placing gimmick structures.
                view.note_place_requested.connect(
                    lambda kind, time_ms, combo, big, dp=pairing.target:
                        self._place_note(dp, kind, time_ms, combo, big)
                )
                view.note_place_with_duration_requested.connect(
                    lambda kind, start_ms, end_ms, combo, big, dp=pairing.target:
                        self._place_note_with_duration(dp, kind, start_ms, end_ms, combo, big)
                )
                view.note_duration_edit_requested.connect(
                    lambda uid, end_ms, dp=pairing.target: self._edit_note_duration(dp, uid, end_ms)
                )
            if view_type == "chart":
                view.note_length_edit_requested.connect(
                    lambda uid, dp=pairing.target: self._type_note_length(dp, uid)
                )
            if view_type == "chart":
                # Right click and Delete both remove what the layer shows -- a
                # fake slider in layer 2, a barline note in layer 3, a normal
                # note in layer 1 -- together with the structure around it. The
                # view only ever offers objects that passed its own filter, so
                # this cannot reach past the layer into the rest of the chart.
                view.note_delete_requested.connect(
                    lambda uid, dp=pairing.target: self._delete_gimmick_objects(dp, [uid])
                )
                view.notes_delete_requested.connect(
                    lambda uids, dp=pairing.target: self._delete_gimmick_objects(dp, uids)
                )
                if layer_id == "fake_slider":
                    # A line removed on its own, without `_expand_move` pulling
                    # in the slider beside it -- that is the whole request.
                    view.timing_lines_delete_requested.connect(
                        lambda uids, dp=pairing.target: self._delete_timing_lines(dp, uids)
                    )
                else:
                    view.timing_lines_delete_requested.connect(
                        lambda uids, dp=pairing.target:
                            self._delete_gimmick_objects(dp, (), uids)
                    )
                view.objects_move_requested.connect(
                    lambda notes, points, delta, dp=pairing.target:
                        self._move_objects(dp, notes, points, delta)
                )
                # One range signal, two tools: Function fills the range with
                # red lines, Convert rebuilds the notes already in it as this
                # layer's structures. The view says which is active rather than
                # the signal carrying it -- the gesture is identical.
                view.timing_function_range_requested.connect(
                    lambda start_ms, end_ms, dp=pairing.target, v=view, lid=layer_id:
                        self._gimmick_range_action(dp, lid, v.tool, start_ms, end_ms)
                )
            # Zoom is a property of the page, not of one band: the six layers
            # describe the same moment, so reading them together only works
            # while they are all on one scale.
            view.gimmick_layer = layer_id
            view.zoom_changed.connect(self._gimmick_zoom_changed)
            # Every layer on one screen: each gets a slice of the window rather
            # than the Editor page's full-height view, so the whole stack is
            # visible without scrolling between related layers.
            # The band is set on the *view*, not on the frame: a maximum on the
            # frame fought the view's own DESIGN_HEIGHT minimum, and the layout
            # resolved that by clipping -- every layer showed its top half and
            # nothing else. A fixed height on the view leaves nothing to
            # resolve, and the frame sizes itself around it and its chrome. It
            # has to be fixed rather than merely allowed: a bare QWidget has no
            # height of its own to fall back on, so a minimum of zero is what
            # the layout gives it.
            view.setFixedHeight(self.gimmick_view_height_spin.value())
            frame.set_content(view)
            setattr(frame, "chart_view" if view_type == "chart" else "sv_view", view)

            # Registered for the playhead broadcast, which is what makes the
            # layers scroll with the song during playback.
            if view_type == "chart":
                self._register_chart_view(view)
            else:
                self._sv_views.append(view)
            self._share_kiai_bands(view, sv=view_type != "chart")
            self._editor_snap_views.append(view)

            frame.closed.connect(self._close_gimmick_view)
            frame.move_requested.connect(self._move_view)
            frame.drag_started.connect(self._begin_view_drag)
            # Above anything added with "+", not merely above the trailing
            # stretch: a band added to this page outlives the rebuild that
            # happens on every visit, so appending put the six layers *under*
            # it and a gameplay preview crept to the top of the page each time
            # you left the tab and came back. `_gimmick_views` was cleared
            # above, so its length is how many layers are already placed.
            self.gimmick_views_layout.insertWidget(len(self._gimmick_views), frame)
            self._gimmick_views.append(frame)

        self._gimmick_built_for = pairing.target
        self._restore_gimmick_order(
            pairing.target, {frame.gimmick_layer: frame for frame in self._gimmick_views})

        # Something has to be showing before the first click lands -- and be
        # what copy, paste and the tool digits act on, or they would all be
        # aimed at nothing until a layer was clicked.
        first = self._gimmick_views[0]
        self._active_chart_view = first.chart_view
        self._show_gimmick_row_for(first.chart_view)

        # Deferred: the scrollbar's range is only recomputed once the rebuilt
        # layout has been laid out, and setting a value against a stale range
        # is silently clamped to whatever it was before.
        QTimer.singleShot(
            0, lambda: self.gimmick_scroll.verticalScrollBar().setValue(scrolled_to)
        )

    def _close_gimmick_view(self, frame: EditorViewFrame) -> None:
        """Close one of the six layers. They come back on the next visit.

        Six bands on one screen is the right default -- a gimmick is read
        across all of them -- but it is a lot of screen for work that is often
        happening in two, so the ✕ has to do something here as well. Nothing
        remembers which were closed: re-entering the tab rebuilds the stack
        (`_open_gimmick_layers`), which is the way back and needs no second
        control to say so.
        """
        if frame in self._gimmick_views:
            self._gimmick_views.remove(frame)
        self.gimmick_views_layout.removeWidget(frame)
        self._close_editor_view(frame)
        # The toolbox belongs to a layer; with that layer gone there is nothing
        # for it to act on, so it goes with it.
        if getattr(frame, "gimmick_layer", None) == getattr(self, "_active_gimmick_layer", None):
            for row in self.gimmick_tool_rows.values():
                row.setVisible(False)
            self._active_gimmick_layer = None
            self._tool_state.refresh_tool_shortcut_scope()
        # ...and something has to be the focused layer afterwards, or the tool
        # digits, copy and paste are all aimed at nothing -- the same reason
        # _open_gimmick_layers picks one on the way in.
        for remaining in self._gimmick_views:
            view = getattr(remaining, "chart_view", None) or getattr(remaining, "sv_view", None)
            if view is None:
                continue
            if self._active_chart_view is None and hasattr(remaining, "chart_view"):
                self._active_chart_view = remaining.chart_view
            self._show_gimmick_row_for(view)
            break

    def _place_gimmick(self, layer_id: str, kind: str, time_ms: float, big: bool = False) -> None:
        """Place one gimmick object: its lines, its object, and its SV restore.

        Everything a single placement writes is one CompositeCommand, so a fake
        slider and the three or seven timing points that shape it undo together
        rather than one line at a time.

        `big` is Shift held at the click, the same flag Don/Kat placement takes
        -- it reaches the drawn object only where that object has a size of its
        own to give away; see `gimmick_session.fake_slider`.
        """
        pairing = self._gimmick_pairing
        if pairing is None:
            return
        state = self._states.get(pairing.target)
        if state is None:
            return
        # osu_snap_ms, not round: a snapped position goes *down* (see
        # time_axis.osu_snap_ms), so rounding put a structure one millisecond
        # above the chart's own notes on the same snap, on every beat whose
        # fractional part happened to be at least .5.
        time_ms = osu_snap_ms(snap_time(pairing.base_timing, time_ms, self._gimmick_snap_divisor()))
        if kind == "multi":
            commands = self._multi_fake_slider_commands(state, pairing, time_ms, big)
        else:
            commands = self._gimmick_commands(
                state, pairing, layer_id, kind, time_ms, count_from(self._next_original_index(state)),
                big=big,
            )
        if not commands:
            return
        state.history.push(CompositeCommand(commands, "place_gimmick"), state)

        # Both refreshes reach the layers themselves now, so a placement needs
        # no third call of its own.
        with self._refresh_cycle():
            self._refresh_difficulty_views(pairing.target)
            self._refresh_difficulty_sv_views(pairing.target)

    def _multi_fake_slider_commands(self, state, pairing, click_ms: int, big: bool = False) -> list:
        """The Multiple Fake Slider tool: N plain fake sliders in one run.

        `click_ms` is where the click snapped to; the configured start/end/
        distance (`_multi_fake_slider_params`) locate the *structures*, one
        per position from `click_ms+start` to `click_ms+end` stepped by
        `distance` -- not their sliders. Each structure's own slider then
        lands `fake_slider_offset_ms` after its position the same way one
        click of the plain Fake Slider tool would place it, which is also why
        this can never write a shiny by accident: every object is at its
        structure's `+fake_slider_offset_ms`, never at the shiny's `+1`.
        Built with `_gimmick_commands(..., kind="regular", ...)` per position
        rather than a second structure builder.

        The run writes **no timing points at all**, and needs no special case
        to manage it: a plain fake slider structure carries none either way now
        (see `gimmick_session.fake_slider`), so N positions produce N sliders
        and nothing else. This tool is where that showed up worst -- N dead
        lines for one run -- but the rule is the structure's, not the tool's.

        One counter shared across the whole run, the same reason
        `_generate_fake_sliders` and `_convert_notes_to_gimmick` share theirs:
        `_next_original_index` reads the document, which this batch has not
        written to yet, so asking it once per position would hand every note
        the same key.
        """
        start, end, distance = self._multi_fake_slider_params
        indices = count_from(self._next_original_index(state))
        commands: list = []
        position = click_ms + start
        limit = click_ms + end
        while position <= limit:
            commands.extend(self._gimmick_commands(
                state, pairing, "fake_slider", "regular", position, indices, big=big,
            ))
            position += distance
        return commands

    def _gimmick_commands(
        self, state, pairing, layer_id: str, kind: str, time_ms: int, indices,
        copies: int | None = None, big: bool = False,
        config: GimmickConfig | None = None, sv_model=None,
    ) -> list:
        """Everything one gimmick structure writes, as commands. [] for a no-op.

        Split out of `_place_gimmick` so the convert tool can build a structure
        per note across a whole selection and push all of them as one undo step.
        `indices` is a counter for `original_index`, shared across a batch --
        `_next_original_index` reads the document, which a batch has not written
        to yet, so asking it once per note would give every one the same key.

        `time_ms` arrives already snapped: the batch path snaps to the notes it
        is converting, not to the grid.

        `config` overrides the layer's saved one for this call. Convert Notes
        asks for its numbers per drag (see `ConvertNotesDialog`), and those must
        not be written back to the layer.

        `sv_model` (`sv_curves.SvModel`) is Keep SV: every red line the
        structure writes gets the chart's own speed back on its millisecond,
        instead of the one restore handle at the end of the cluster.
        """
        if kind in ("don", "kat"):
            time_ms = self._note_centre_near(state.document, time_ms)
        if config is None:
            config = self._gimmick_config(layer_id)
        # Shiny is a fake slider with its own offset and several sliders on it,
        # so the builder is the same one with a flag -- see gimmick_session.
        shiny = kind == "shiny"
        if shiny:
            kind = "regular"

        # One gimmick structure per millisecond, in every layer. osu! honours
        # only the first uninherited point on a timestamp, so a second structure
        # on one costs a pile of dead lines -- and a duplicated hit object,
        # which is invisible in the editor and a double note in game -- while
        # changing nothing on screen. One accidental double click away, since
        # the time is snapped before it gets here.
        #
        # The fake slider layer is the exception, and deliberately: its two
        # structures are told apart by *where* their object sits, so a fake
        # slider and a shiny on the same snap are two readable objects a
        # millisecond apart sharing one gimmick line -- which is what the
        # layer's two rows exist to show. There, the thing that must not be
        # duplicated is the object's own millisecond, not the line's.
        if layer_id == "fake_slider":
            at = time_ms + (config.shiny_offset_ms if shiny else config.fake_slider_offset_ms)
            if any(
                self.is_fake_slider(note) and round(note.time) == at
                for note in state.document.hit_objects
            ):
                self.show_toast(tr("MainWindow", "There is already a gimmick here."))
                return []
        else:
            line_at = time_ms + config.red_line_offset_ms if kind == "red_line" else time_ms
            occupied = [
                point for point in state.document.timing_points
                if point.uninherited and round(point.time) == line_at
            ]
            # A plain red line duplicating *any* line there is equally
            # meaningless; a structure is refused only by another structure, so
            # one can still be drawn on top of the chart's own timing.
            #
            # The Red Line tool is refused by *any* red line, a fake slider's
            # or shiny's own included, and says so by name: two uninherited
            # points on one millisecond are never placed.
            if kind == "red_line" and occupied:
                self.show_toast(tr("MainWindow", "There is already a red line here."))
                return []
            if any(point.bpm == config.gimmick_bpm for point in occupied):
                self.show_toast(tr("MainWindow", "There is already a gimmick here."))
                return []
        try:
            if layer_id == "fake_slider":
                points, notes = gimmick_fake_slider(
                    time_ms, pairing.base_timing, config, kind=kind, shiny=shiny,
                    copies=copies, big=big,
                )
            elif kind == "red_line":
                bpm = base_bpm_at(pairing.base_timing, time_ms)
                points, notes = gimmick_red_line(time_ms, bpm, config)
            else:
                points, notes = gimmick_barline_note(time_ms, pairing.base_timing, config, kind=kind)
        except GimmickConfigError as error:
            QMessageBox.warning(self, tr("MainWindow", "Cannot place this"), str(error))
            return []

        notes = self._without_redundant_note(state.document, notes, kind, time_ms)

        # The gimmick's own lines reset SV to 1.0x. Hand the chart's SV back at
        # the end of the cluster -- and for a structure, write that line even
        # when there was nothing to hand back. An SV layer matches its green
        # lines by exact millisecond, so with none there layers 5 and 6 had
        # nothing to show, drag or generate a sweep from: a 1.0x line agrees
        # with the timing either way and is the handle the layer is made of.
        #
        # The plain red-line tool is one line rather than a structure, and layer
        # 6 already owns its millisecond, so it keeps the old rule -- otherwise
        # every hand-placed line would come with a green one stacked on it.
        #
        # Appended last so it sorts *after* the red line sharing its
        # millisecond: osu! resolves a tie by file order, and the green one has
        # to win. InsertTimingPoints sorts stably on time alone.
        #
        # A shiny's own points are [red@T, green@T], so this handle's
        # millisecond -- the latest point's -- is T, and the builder has
        # already written the green line there. Appending a second one would
        # stack two inherited points on the same millisecond; skipped
        # whenever the cluster already carries an inherited point at the
        # handle's time, which is only ever the shiny case, since every other
        # structure's last point is the uninherited restore line.
        # ...except in the fake slider layer, which now takes no SV from the
        # chart at all -- not the builder's own green lines and not this
        # handle. A fake slider is decoration, and handing it back the speed of
        # whatever section it was dropped in made identical structures scroll
        # differently. The cost is accepted and real: layer 5 matches its green
        # lines by exact millisecond, so these structures no longer arrive with
        # a line for it to show, drag or sweep from.
        if sv_model is not None and points:
            points = gimmick_keep_sv(points, sv_model, at_note=time_ms)
        elif layer_id != "fake_slider" and points:
            restore_at = max(point.time for point in points)
            already_handled = any(
                not point.uninherited and round(point.time) == round(restore_at)
                for point in points
            )
            if not already_handled:
                restore = sv_restore_point(state.document.timing_points, time_ms - 1, restore_at)
                if kind != "red_line" or restore.sv_multiplier != 1.0:
                    points = [*points, restore]

        # Kiai is a property of the active timing point, so a gimmick line
        # written without it ends the section it lands in for everything after
        # it -- a chorus that stops being a chorus the moment a fake slider is
        # placed in it. Applied to the whole cluster, red and green alike.
        carry_active_state(points, state.document.timing_points)

        # A fake slider and a shiny on one snap share a gimmick line, so the
        # second of them must not write a duplicate: osu! honours only the first
        # uninherited point on a timestamp, and a second identical one is dead
        # weight the structure walk would then have to reason about.
        existing_red = {
            (round(point.time), point.beat_length)
            for point in state.document.timing_points if point.uninherited
        }
        points = [
            point for point in points
            if not point.uninherited
            or (round(point.time), point.beat_length) not in existing_red
        ]
        if not points and not notes:
            return []

        for note in notes:
            note.original_index = next(indices)

        commands = (
            [self._insert_points_command(state.document, points)] if points else []
        )
        if notes:
            # Same "one object per millisecond" rule every other placement path
            # follows (_insert_notes). This path used to skip it, so a Kat
            # placed over an existing Don stacked two hit objects on the same
            # millisecond instead of replacing one -- invisible in the editor,
            # and a double note in game.
            #
            # Fake sliders are outside the rule in both directions: one is
            # decoration drawn beside a note rather than a note competing for
            # its millisecond, so placing one must not delete the chart's note
            # and placing a note must not delete one.
            displaced = [
                victim
                for note in notes
                if not self.is_fake_slider(note)
                for victim in self._notes_blocking(state.document, note.time, note.is_spinner)
                if not self.is_fake_slider(victim)
            ]
            if displaced:
                commands.append(RemoveHitObjects(displaced))
            commands.append(InsertHitObjects(notes))
        return commands

    def _gimmick_range_action(
        self, difficulty_path: Path, layer_id: str, tool: str, start_ms: float, end_ms: float,
    ) -> None:
        """Route a dragged range to whichever range tool the layer is holding.

        One gesture, one signal: Function and Convert are both "drag a range,
        then do something to it", and which of the two it was is a property of
        the view rather than of the drag.

        Anti-barline used to be a fourth button here. It is one of the things
        Convert Notes can draw now (`ConvertNotesDialog`), and a second way in
        that could only ever use the layer's saved numbers was the worse one.
        """
        if tool == "kiai":
            self._set_kiai_range(difficulty_path, start_ms, end_ms)
        elif tool == "convert":
            self._convert_notes_to_gimmick(difficulty_path, layer_id, start_ms, end_ms)
        elif layer_id == "fake_slider":
            self._generate_fake_sliders(difficulty_path, start_ms, end_ms)
        else:
            self._generate_barlines(difficulty_path, start_ms, end_ms)

    def _sv_range_action(
        self, difficulty_path: Path, tool: str, start_ms: float, end_ms: float,
        layer_id: str | None = None,
    ) -> None:
        """The same routing for an SV view's dragged range.

        One signal, three tools: the SV layers' Function, and the Kiai and
        Sound Volume layer's Kiai and Volume. Which one it was is a property of
        the view rather than of the drag -- see _gimmick_range_action.
        """
        if tool == "kiai":
            # The Kiai and Sound Volume layer's Kiai tool never creates a
            # green line (task: "you shouldn't be able to drag them at all"
            # where there is no timing point) -- so a range with nothing in
            # it is refused outright rather than silently inserting edges.
            state = self._states.get(difficulty_path)
            if state is not None and not any(
                round(start_ms) <= round(point.time) <= round(end_ms)
                for point in state.document.timing_points
            ):
                self.show_toast(tr("MainWindow", "No timing point in the selected range."))
                return
            self._set_kiai_range(difficulty_path, start_ms, end_ms)
        elif tool == "volume":
            self._open_volume_function_dialog(difficulty_path, start_ms, end_ms)
        else:
            self._open_sv_function_dialog(difficulty_path, start_ms, end_ms, layer_id=layer_id)

    def _set_kiai_range(
        self, difficulty_path: Path, start_ms: float, end_ms: float,
    ) -> None:
        """Turn a dragged range into one kiai section, across every layer.

        Kiai is a bit on a timing point rather than a span, so a section is
        really "every point from here on says kiai until one says it does not".
        A gimmick fills the range with hundreds of points -- a fake slider's two
        lines, a barline note's seven -- and each of them is a place the section
        can end by accident. Setting them all is the only way to say it once.

        Nothing is ever *created* here. The only toolbox with a Kiai tool is
        the Kiai and Sound Volume layer, and that layer must never write a
        green line -- so an edge with no timing point on it is left alone and
        the section starts (or ends) at the nearest existing point instead.
        This used to invent an inherited point at each edge restating the SV
        already in force, which is a scroll-speed edit made on the user's
        behalf in a layer that has nothing to do with scroll speed. If the
        range holds no timing point at all there is nothing to flag, so the
        caller toasts and does not call this (see _sv_range_action).

        The range arrives already snapped: the view that dragged it owns the
        grid (and the Ctrl override that trades it for whole milliseconds), so
        snapping a second time here would quietly undo that override.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        start, end = round(start_ms), round(end_ms)
        if end <= start:
            return

        ordered = sorted_by_time(state.document.timing_points)
        commands: list = []
        for point in ordered:
            at = round(point.time)
            if start <= at < end:
                wanted = point.effects | EFFECT_KIAI
            elif at == end:
                # The first point of what comes after: it is what ends the
                # section, so it has to say so.
                wanted = point.effects & ~EFFECT_KIAI
            else:
                continue
            if wanted != point.effects:
                commands.append(
                    EditTimingPoint(point.uid, {"effects": (point.effects, wanted)})
                )

        if not commands:
            return
        state.history.push(CompositeCommand(commands, "set_kiai"), state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(difficulty_path)
            self._refresh_difficulty_sv_views(difficulty_path)

    def _generate_fake_sliders(
        self, difficulty_path: Path, start_ms: float, end_ms: float,
    ) -> None:
        """The fake slider layer's function tool: fill a range with objects.

        Fake sliders or shiny notes, on the same two rhythms the barline
        generator offers -- every n milliseconds for scenery, every n snaps for
        something the music can be read against.
        """
        pairing = self._gimmick_pairing
        state = self._states.get(difficulty_path)
        if state is None or pairing is None:
            return
        config = self._gimmick_config("fake_slider")
        # See the identical comment in _generate_barlines.
        start_ms = osu_snap_ms(snap_time(pairing.base_timing, start_ms, self._gimmick_snap_divisor()))
        dialog = FakeSliderFunctionDialog(
            start_ms, end_ms, self,
            base_timing=pairing.base_timing, snap_divisor=self._gimmick_snap_divisor(),
            shiny_count=config.shiny_count,
            shiny_bpm_multiplier=config.shiny_bpm_multiplier,
            fake_slider_bpm_multiplier=config.fake_slider_bpm_multiplier,
            # "At each note" turns a run of the chart's own notes into shiny
            # ones, which is what the fake slider layer's structures decorate.
            note_times={
                round(note.time) for note in state.document.hit_objects
                if note.is_circle
            },
        )
        accepted = dialog.exec() == QDialog.Accepted
        times = dialog.times() if accepted else []
        kind = dialog.object_kind() if accepted else "regular"
        count = dialog.shiny_count() if accepted else config.shiny_count
        multiplier = dialog.bpm_multiplier() if accepted else (
            config.shiny_bpm_multiplier if kind == "shiny" else config.fake_slider_bpm_multiplier
        )
        dialog.deleteLater()
        if not times:
            return
        # Written back before generating: the builder reads the multiplier off
        # the config, and the Shiny/Fake Slider tools and the detector have to
        # agree with what this run wrote. Same reason the SV dialog writes its
        # offset back. Which field depends on which object this run built --
        # a fake slider has no compensating green line, so its multiplier is
        # its own setting, never the shiny's.
        if kind == "shiny":
            config.shiny_count = count
            config.shiny_bpm_multiplier = multiplier
        else:
            config.fake_slider_bpm_multiplier = multiplier
        indices = count_from(self._next_original_index(state))
        commands: list = []
        for at in times:
            commands.extend(self._gimmick_commands(
                state, pairing, "fake_slider", kind, at, indices, copies=count,
            ))
        if not commands:
            return
        state.history.push(CompositeCommand(commands, "place_gimmick"), state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(difficulty_path)
            self._refresh_difficulty_sv_views(difficulty_path)

    def _convert_to_anti_barline(
        self, difficulty_path: Path, start_ms: float, end_ms: float,
        config: GimmickConfig | None = None, sv_model=None,
    ) -> None:
        """Turn a dragged range into the anti-barline gimmick.

        Reached from Convert Notes, which is where all three converters live.

        The whole structure is one undo step, because it is one thing: a wall
        of red lines with a slit taken out of it per note, and half of it
        without the other half is either a solid white lane with nothing in it
        or a chart of invisible notes.

        Only plain circles are converted -- see `gimmick_session.anti_barline`.
        A finisher, a drumroll and a spinner in the range are left alone, so
        the six downbeats of a section like the reference one stay ordinary
        notes to be marked however the mapper wants.

        A wall tick landing on a millisecond that already carries an
        uninherited point is dropped rather than stacked: osu! honours only the
        first of two on a timestamp, so the second is a line the layer would
        draw and hit-test forever while changing nothing.
        """
        pairing = self._gimmick_pairing
        state = self._states.get(difficulty_path)
        if state is None or pairing is None:
            return
        if config is None:
            config = self._gimmick_config("barline")
        try:
            points = gimmick_anti_barline(
                [
                    note for note in state.document.hit_objects
                    if start_ms <= note.time <= end_ms
                ],
                start_ms, end_ms, pairing.base_timing, config, model=sv_model,
            )
        except GimmickConfigError as error:
            QMessageBox.warning(self, tr("MainWindow", "Cannot place this"), str(error))
            return
        if not points:
            self.show_toast(tr("MainWindow", "No notes in the selected range to convert."))
            return
        replaced = self._greens_under_wall(state.document, points, sv_model) if sv_model is not None else []
        # Only the red lines are filtered against what is already there. The
        # green line beside each one is this layer's handle on it and has to
        # survive even where the map's own timing supplied the red line, or
        # the wall would have gaps in the SV layer exactly where the chart has
        # timing of its own.
        taken = {round(p.time) for p in state.document.timing_points if p.uninherited}
        points = [
            point for point in points
            if point.inherited or round(point.time) not in taken
        ]
        if not any(point.uninherited for point in points):
            self.show_toast(tr("MainWindow", "There is already a gimmick here."))
            return
        # Same reason as every other generator: a thousand lines with kiai off
        # is a thousand ways to kill the section they were drawn across. Read
        # before the chart's own green lines go (Keep SV), from the chart as
        # it was.
        carry_active_state(points, state.document.timing_points)
        # Displacement is worked out on the timeline as it will be, without
        # the lines already being removed: removed twice, an undo would put
        # each of them back twice.
        gone = {point.uid for command in replaced if isinstance(command, RemoveTimingPoints)
                for point in command.points}
        remaining = SimpleNamespace(
            timing_points=[point for point in state.document.timing_points if point.uid not in gone])
        command = self._insert_points_command(remaining, points)
        if replaced:
            command = CompositeCommand([*replaced, command], "insert_timing_points")
        state.history.push(command, state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(pairing.target)
            self._refresh_difficulty_sv_views(pairing.target)

    def _greens_under_wall(self, document, points: list, model) -> list:
        """Keep SV's anti-barline: what becomes of the chart's own green lines
        under the wall.

        Left in place, each would hold its step value until the next wall
        tick, cutting the interpolated sheet back into the staircase Keep SV
        exists to remove -- so they go. Except a line that changes **kiai or
        volume**: that is a section edge, kept on its exact millisecond with
        its SV rewritten to the curve's value there.
        """
        reds = [point.time for point in points if point.uninherited]
        if not reds:
            return []
        first, last = min(reds), max(reds)
        ordered = sorted_by_time(document.timing_points)
        removed, commands = [], []
        previous = None
        for point in ordered:
            if not point.uninherited and first < point.time < last:
                edge = previous is not None and (
                    bool(point.kiai) != bool(previous.kiai) or point.volume != previous.volume)
                if edge:
                    value = model.value_at(point.time)
                    commands.append(EditTimingPoint(
                        point.uid, {"beat_length": (point.beat_length, -100.0 / value)}))
                else:
                    removed.append(point)
            previous = point
        if removed:
            commands.insert(0, RemoveTimingPoints(removed))
        return commands

    def _convert_to_hidden_anti_barline(
        self, difficulty_path: Path, start_ms: float, end_ms: float,
        config: GimmickConfig,
    ) -> None:
        """Turn a dragged range into the hidden anti-barline gimmick.

        One undo step, for the same reason the classic converter is one: the
        wall and the slits are halves of one picture, and either alone is a
        white lane or a chart of invisible notes.

        Unlike the classic converter this writes four lines per note and two for
        the whole range, so nothing here is dense enough to want the
        already-taken filter that one needs -- but a note whose millisecond
        already carries a red line would still lose its hide line to it (osu!
        honours the first on a timestamp), so those are dropped and reported
        rather than written into a structure that would silently not work.
        """
        pairing = self._gimmick_pairing
        state = self._states.get(difficulty_path)
        if state is None or pairing is None:
            return
        try:
            points = gimmick_hidden_anti_barline(
                [
                    note for note in state.document.hit_objects
                    if start_ms <= note.time <= end_ms
                ],
                start_ms, end_ms, pairing.base_timing, config,
            )
        except GimmickConfigError as error:
            QMessageBox.warning(self, tr("MainWindow", "Cannot place this"), str(error))
            return
        if not points:
            self.show_toast(tr("MainWindow", "No notes in the selected range to convert."))
            return
        taken = {round(p.time) for p in state.document.timing_points if p.uninherited}
        points = [
            point for point in points
            if point.inherited or round(point.time) not in taken
        ]
        if not any(point.uninherited for point in points):
            self.show_toast(tr("MainWindow", "There is already a gimmick here."))
            return
        carry_active_state(points, state.document.timing_points)
        state.history.push(self._insert_points_command(state.document, points), state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(pairing.target)
            self._refresh_difficulty_sv_views(pairing.target)

    def _convert_notes_to_gimmick(
        self, difficulty_path: Path, layer_id: str, start_ms: float, end_ms: float,
    ) -> None:
        """Turn every plain note in a dragged range into this layer's structure.

        A gimmick is normally drawn around notes that already exist, and doing
        that by hand meant clicking each one -- so this is the same Don/Kat
        placement the tools make, aimed at the chart's own notes: a don becomes
        a Don structure and a kat a Kat one, on the note's own millisecond
        rather than on the grid, so nothing is retimed by being converted.

        Fake sliders and drumrolls are skipped: they are what the tool *writes*,
        and converting its own output is how a range gets gimmicked twice.

        Both layers ask first, because both have numbers a section wants tuned
        for it -- and the barline layer also has three different structures the
        same drag could draw. Which one, and its numbers, are properties of the
        section rather than of the layer: `ConvertNotesDialog` opens on the
        layer's Config and the edits live for this call only.
        """
        pairing = self._gimmick_pairing
        state = self._states.get(difficulty_path)
        if state is None or pairing is None:
            return
        self._run_conversion(difficulty_path, layer_id, start_ms, end_ms, experimental=False)

    def _experimental_conversion(self, difficulty_path: Path) -> None:
        """The barline layer's Experimental Conversion: the whole map into
        barline notes or anti-barline, with the chart's SV kept (owner,
        2026-10-09). Every note, first to last; same window, no hidden
        anti-barline, Keep SV always on."""
        state = self._states.get(difficulty_path)
        if state is None or self._gimmick_pairing is None or not state.document.hit_objects:
            self.show_toast(tr("MainWindow", "No notes in the selected range to convert."))
            return
        times = [note.time for note in state.document.hit_objects]
        self._run_conversion(difficulty_path, "barline", min(times), max(times), experimental=True)

    def _run_conversion(
        self, difficulty_path: Path, layer_id: str, start_ms: float, end_ms: float, experimental: bool,
    ) -> None:
        pairing = self._gimmick_pairing
        state = self._states.get(difficulty_path)
        # The chart's own SV, read from this difficulty (owner: the gimmick
        # difficulty is still the original chart wherever it has not been
        # converted). A snapshot, so the window's reading does not move under
        # it; the barline layer only.
        sv_inputs = (
            (list(state.document.timing_points), list(state.document.hit_objects), start_ms, end_ms)
            if layer_id == "barline" else None
        )
        dialog = ConvertNotesDialog(self._gimmick_config(layer_id), layer_id, self, start_ms, end_ms,
                                    sv_inputs=sv_inputs, experimental=experimental)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        mode, config, sv_model = dialog.mode(), dialog.config(), dialog.sv_model() if accepted else None
        dialog.deleteLater()
        if not accepted:
            return
        if mode == ConvertNotesDialog.ANTI:
            self._convert_to_anti_barline(difficulty_path, start_ms, end_ms, config, sv_model=sv_model)
            return
        if mode == ConvertNotesDialog.HIDDEN:
            self._convert_to_hidden_anti_barline(difficulty_path, start_ms, end_ms, config)
            return
        indices = count_from(self._next_original_index(state))
        commands: list = []
        for note in sorted(state.document.hit_objects, key=lambda n: n.time):
            if not note.is_circle or not (start_ms <= note.time <= end_ms):
                continue
            commands.extend(self._gimmick_commands(
                state, pairing, layer_id, "kat" if note.is_kat else "don",
                round(note.time), indices, config=config, sv_model=sv_model,
            ))
        if not commands:
            self.show_toast(tr("MainWindow", "No notes in the selected range to convert."))
            return
        # One insert for the whole batch, where the first one was: each sorts
        # the whole timing list as it applies, and a whole map was 5,310
        # sorts (measured, Darling Game Over Love). A stable sort of the
        # concatenation leaves every tie in the order the separate ones did.
        inserts = [command for command in commands if isinstance(command, InsertTimingPoints)]
        if len(inserts) > 1:
            merged = InsertTimingPoints([point for command in inserts for point in command.points])
            first = commands.index(inserts[0])
            commands = [command for command in commands if not isinstance(command, InsertTimingPoints)]
            commands.insert(first, merged)
        state.history.push(CompositeCommand(commands, "place_gimmick"), state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(pairing.target)
            self._refresh_difficulty_sv_views(pairing.target)

    # How far off its own snap a chart's note is allowed to be and still count
    # as the note this structure is being drawn around. A snap is rarely a whole
    # millisecond, and the millisecond the map itself wrote for it need not be
    # the one round() picks -- osu! rounds a .5 up, Python rounds it to even.
    NOTE_CENTRE_TOLERANCE_MS = 2

    def _note_centre_near(self, document, time_ms: int) -> int:
        """The millisecond a Don/Kat structure should be centred on.

        The chart's own note when one is standing within a millisecond or two,
        its own time exactly; the snap otherwise.

        A barline or fake slider Don is the note plus the structure decorating
        it, and the two only read as one object while they share a millisecond.
        Built on the bare snap instead, a placement one millisecond off the note
        already there wrote a *second* hit object next to it -- the pair a
        millisecond apart that `_without_redundant_note` exists to prevent, which
        it could not see because it matches on the exact millisecond.
        """
        nearest = min(
            (
                note for note in document.hit_objects
                if note.is_circle and abs(note.time - time_ms) <= self.NOTE_CENTRE_TOLERANCE_MS
            ),
            key=lambda note: abs(note.time - time_ms),
            default=None,
        )
        return time_ms if nearest is None else round(nearest.time)

    def _without_redundant_note(self, document, notes: list, kind: str, time_ms: int) -> list:
        """Drop a placement's real note when the chart already has that note.

        Don and Kat in the fake slider and barline layers write a hittable note
        *and* the structure decorating it. When the normal chart already carries
        the same kind of note on that millisecond, that note is the one the
        gimmick is being drawn around: writing a second one only replaces it
        (a placement displaces what it lands on), losing whatever hitsounds,
        finisher bit and new-combo flag the mapper put there. A note of the
        *other* kind is left to be replaced, since swapping a don for a kat is a
        real edit rather than an accident.

        A circle, specifically. A drumroll's hit sound is 0, which reads as a
        don, so a Don placed on one used to write its bars and then drop its own
        note as redundant -- leaving a gimmick with nothing to hit.
        """
        if kind not in ("don", "kat"):
            return notes
        existing = next(
            (
                note for note in document.hit_objects
                if round(note.time) == time_ms and note.is_circle
            ),
            None,
        )
        if existing is None:
            return notes
        # osu!'s own rule, whistle *or* clap: on the clap bit alone every
        # whistle kat read as a don, so converting a map replaced each one with
        # a fresh clap kat at the playfield centre -- its hitsound and its
        # Fancy Arranger position gone (owner, 2026-10-09: 713 notes of
        # Darling Game Over Love's Ura Oni).
        if existing.is_kat != (kind == "kat"):
            return notes
        return [note for note in notes if self.is_fake_slider(note) or round(note.time) != time_ms]

    def _gimmick_zoom_changed(self, window_ms: float) -> None:
        """Ctrl+wheel on any layer zooms all of them, within one shared range.

        The layers describe the same moment from different angles, so reading
        them against each other only works while they share a scale. The
        gameplay viewer is excluded on purpose: its x axis is pixels-per-beat,
        not a time window, and it renders at its own size.

        The range is the *narrowest* the participating layers agree on, because
        they do not all have the same one: `SVEditorView` allows a 120s window
        and the chart views 60s, deliberately -- an SV sweep is read across a
        whole section and a note pattern is not. Broadcasting unclamped let the
        Kiai/Volume layer (an SV view) push every chart view to 120s, past a
        limit those views enforce for themselves; the next notch of the wheel
        on one of them clamped straight back to 60s, so zooming stepped by a
        factor of two in one direction and not the other.

        ponytail: the shared range is the intersection, so on this page the SV
        and Kiai/Volume layers reach 60s rather than their own 120s. Widening
        the chart views instead would be the other way to make the page
        consistent, and wants a look at whether a 120s window of notes is
        legible at the layer height before it is worth doing.
        """
        views = [
            view for view in (
                getattr(frame, "chart_view", None) or getattr(frame, "sv_view", None)
                for frame in self._gimmick_views
            )
            if view is not None and not isinstance(view, GameplayViewerView)
        ]
        if not views:
            return
        shared = max(
            max(view.MIN_WINDOW_MS for view in views),
            min(window_ms, min(view.MAX_WINDOW_MS for view in views)),
        )
        for view in views:
            if view.window_ms != shared:
                view.window_ms = shared
                view.update()

    def _gimmick_snap_divisor(self) -> int:
        return int(self.gimmick_snap_combo.currentData())

    def _gimmick_snap_changed_by_wheel(self, divisor: int) -> None:
        """Alt+wheel in one layer moves the whole page's snap.

        The combo is the single source of truth for all six layers, so the view
        that caught the wheel hands the divisor to it and lets the combo push it
        back out -- to the other five, and harmlessly to the sender, which has
        already set it on itself.
        """
        index = self.gimmick_snap_combo.findData(divisor)
        if index >= 0:
            self.gimmick_snap_combo.setCurrentIndex(index)

    def _gimmick_snap_changed(self) -> None:
        divisor = self._gimmick_snap_divisor()
        for frame in self._gimmick_views:
            view = getattr(frame, "chart_view", None) or getattr(frame, "sv_view", None)
            if view is not None:
                view.set_snap_divisor(divisor)

    def _share_kiai_bands(self, view, sv: bool) -> None:
        """Let `view` take its kiai bands, sorted points and beat-only points
        from the shared per-refresh cache.

        Without this every view runs `kiai_spans` and `sorted_by_time` (and,
        for the beat-only list, `uninherited_points` on top) itself -- six
        layers each sorting the whole timing point list, twice over, on every
        placement was a measurable part of the 162ms it used to take to place
        one note. A chart view is handed the document and an SV view its point
        list, matching what each already passes to `set_kiai_from`.
        """
        if sv:
            view.kiai_bands_for = lambda points: self._kiai_bands(points)
            view.sorted_points_for = lambda points: self._sorted_points(points)
        else:
            view.kiai_bands_for = lambda document: self._kiai_bands(document.timing_points)
            view.sorted_points_for = lambda document: self._sorted_points(document.timing_points)
        view.uninherited_points_for = lambda ordered: self._uninherited_points_cached(ordered)

    def _sorted_points(self, points) -> list:
        """`sorted_by_time(points)`, shared for the length of one refresh
        cycle. See `_share_kiai_bands`."""
        return self._cached("sorted_points", lambda: sorted_by_time(points))

    def _uninherited_points_cached(self, ordered) -> list:
        """`uninherited_points(ordered)`, shared the same way. `ordered` must
        already be the cycle's shared sorted list -- passing anything else
        would seem to work (the cache key ignores the argument) but silently
        serve the wrong list back."""
        return self._cached("uninherited_points", lambda: uninherited_points(ordered))

    def _cached(self, key: str, compute):
        """Memoize a document-derived set for the length of one refresh cycle.

        A placement refreshes six layers, and each of them asks for the same
        derived sets -- which fake slider is a shiny, which milliseconds carry
        a structure's lines, where the kiai sections are. Every one of those is
        a full pass over the document, and on a gimmick map that is tens of
        thousands of timing points, six times over, twice per placement. It
        measured at 162ms to place one note, and the whole test suite runs on
        placements.

        Scoped to a refresh cycle rather than cached against the document,
        deliberately: nothing mutates the document *during* a refresh, so the
        answer cannot go stale inside the window where it is reused, and there
        is no invalidation to get wrong when an edit lands. Outside a cycle
        (`_doc_cache is None`) every call computes as it always did.
        """
        if self._doc_cache is None:
            return compute()
        if key not in self._doc_cache:
            self._doc_cache[key] = compute()
        return self._doc_cache[key]

    @contextmanager
    def _refresh_cycle(self):
        """Open the `_cached` window, re-entrantly.

        The refresh entry points call each other -- a placement runs both the
        note and the SV refresh, and the gimmick page's own refresh runs inside
        those -- so only the outermost opens and clears the cache.
        """
        if self._doc_cache is not None:
            yield
            return
        self._doc_cache = {}
        try:
            yield
        finally:
            self._doc_cache = None

    def _kiai_bands(self, document_points) -> list[tuple[int, int]]:
        return self._cached("kiai", lambda: kiai_spans(document_points, KIAI_OPEN_END_MS))

    def _refresh_view_frame(self, frame) -> None:
        """Re-read the document into one view, on demand.

        One place that knows what each kind of view is called and which of its
        refresh methods to use, so a caller cannot refresh a frame's chart and
        silently leave the gameplay preview beside it holding the old document.

        Falls back to the gimmick pairing's target: a view added to the gimmick
        page with "+" is showing that difficulty whatever it was opened against.
        """
        difficulty_path = getattr(frame, "difficulty_path", None)
        state = self._states.get(difficulty_path) if difficulty_path is not None else None
        if state is None and self._gimmick_pairing is not None:
            state = self._states.get(self._gimmick_pairing.target)
        if state is None:
            return
        # refresh_*, not load_document, for the same reason every other refresh
        # path uses them: this must not throw the view back to the first object.
        for attribute, method in (
            ("chart_view", "refresh_notes"),
            ("gameplay_view", "refresh_notes"),
            ("sv_view", "refresh_points"),
        ):
            view = getattr(frame, attribute, None)
            if view is not None:
                getattr(view, method)(state.document)

    def _refresh_gimmick_views(self) -> None:
        # Once per refresh cycle, not once per caller. An edit runs both
        # `_refresh_difficulty_views` and `_refresh_difficulty_sv_views` and
        # both end up here, so every placement rebuilt all six layers twice
        # over -- the same document, to the same result.
        if self._doc_cache is not None:
            if self._doc_cache.get("gimmick_refreshed"):
                return
            self._doc_cache["gimmick_refreshed"] = True
        with self._refresh_cycle():
            pairing = self._gimmick_pairing
            state = self._states.get(pairing.target) if pairing else None
            if state is None:
                return
            for frame in self._gimmick_views:
                # refresh_*, not load_document: a placement must not throw the
                # cursor back to the first object in all six layers.
                # Every view the frame holds, not the first one found: a
                # `continue` after the chart meant a frame carrying a second
                # view was refreshed for one of them and left stale for the
                # other.
                self._refresh_view_frame(frame)

    def _editor_snap_changed(self) -> None:
        divisor = int(self.editor_snap_combo.currentData())
        for view in self._editor_snap_views:
            view.set_snap_divisor(divisor)

    def _editor_change_snap_from_wheel(self, delta: int) -> None:
        """Alt+wheel on the Editor page: every open chart view moves together."""
        if not delta:
            return
        values = SNAP_DIVISORS
        current = int(self.editor_snap_combo.currentData())
        try:
            index = values.index(current)
        except ValueError:
            index = values.index(4)
        direction = 1 if delta > 0 else -1
        new_index = max(0, min(len(values) - 1, index + direction))
        new_divisor = values[new_index]
        if new_divisor == current:
            return
        found = self.editor_snap_combo.findData(new_divisor)
        if found >= 0:
            self.editor_snap_combo.blockSignals(True)
            self.editor_snap_combo.setCurrentIndex(found)
            self.editor_snap_combo.blockSignals(False)
        for view in self._editor_snap_views:
            view.set_snap_divisor(new_divisor)

    # -- per-difficulty zoom ---------------------------------------------------

    DEFAULT_EDITOR_WINDOW_MS = 2000.0

    def _editor_window_ms(self) -> float:
        """The time span every view on the Editor page shows.

        A new view adopts the page's current zoom, so opening an SV editor --
        or another difficulty of the same song -- next to a zoomed-in chart
        does not put the two on different scales.
        """
        return self._editor_zoom_ms

    def _gimmick_view_height_changed(self, height: int) -> None:
        """Resize every gimmick band (the six layers and any "+" view), and
        remember the size."""
        self.settings.set_value("gimmick/view_height", int(height))
        # The layers live in _gimmick_views; a "+" band in _editor_views,
        # told apart from the Editor's own views by its compact chrome.
        for frame in [*self._gimmick_views, *self._editor_views]:
            if not getattr(frame, "compact", False):
                continue
            content = getattr(frame, "content", None)
            if isinstance(content, (TimelineGameplay, SVEditorView)):
                content.setFixedHeight(int(height))

    def _editor_view_height_changed(self, height: int) -> None:
        """Resize every open Editor-page view, and remember the size."""
        self.settings.set_value("editor/view_height", int(height))
        self._apply_editor_view_height()

    def _apply_editor_view_height(self) -> None:
        """Both view kinds, at one height.

        Gameplay and density views are left out on purpose: a density strip is
        a 58px bar by design and the gameplay preview has a floor of its own,
        so folding them in would mean either clipping them or letting the
        smallest of them decide the number.
        """
        height = self.editor_view_height_spin.value()
        for view in self._all_editor_views():
            view.setFixedHeight(height)

    def _all_editor_views(self):
        """Every chart/SV view on the Editor page, whatever difficulty.

        `_editor_views` also holds the bands the gimmick page's "+" adds --
        they go through the same wiring, which is what makes them editable
        rather than read-only copies. A band is a fixed GIMMICK_LAYER_HEIGHT
        slice of a six-layer stack, so neither the page zoom nor the height
        control has any business reaching one; `compact` is the chrome those
        frames already carry and is what tells them apart.
        """
        for frame in self._editor_views:
            if getattr(frame, "compact", False):
                continue
            for attribute in ("chart_view", "sv_view"):
                view = getattr(frame, attribute, None)
                if view is not None:
                    yield view

    def _editor_views_for(self, difficulty_path: Path):
        """Every chart/SV view currently open for one difficulty."""
        for frame in self._editor_views:
            if getattr(frame, "difficulty_path", None) != difficulty_path:
                continue
            for attribute in ("chart_view", "sv_view"):
                view = getattr(frame, attribute, None)
                if view is not None:
                    yield view

    def _zoom_changed(self, difficulty_path: Path, window_ms: float, origin=None) -> None:
        """Ctrl+wheel on one view zooms every view on the Editor page.

        Page-level rather than view-level because the SV editor has to stay
        aligned with the chart it annotates -- if one is showing 2 seconds and
        the other 4, the same x position means two different times and
        following the shared playhead together stops meaning anything. Every
        difficulty open here is the same song from the same folder on the same
        timing, so that argument does not stop at a difficulty boundary and the
        audio file they each name has nothing to do with it.

        `difficulty_path` is still taken: it is what the per-view lambda has to
        hand, and dropping it would mean touching every connection for nothing.
        """
        del difficulty_path
        self._editor_zoom_ms = window_ms
        for view in self._all_editor_views():
            if view is origin or abs(view.window_ms - window_ms) < 0.01:
                continue
            view.window_ms = window_ms
            view.update()

    def _difficulty_group_layout(self, source_path: Path, label: str) -> QVBoxLayout:
        existing = self._editor_view_groups.get(source_path)
        if existing is not None:
            return existing

        # No group header label: the difficulty name is shown once per view, at
        # the right of each EditorViewFrame's chrome row. A header here as well
        # is what made the name appear on both the left and the right of every
        # difficulty's views. `label` is still taken so callers keep one
        # signature whether or not the group already exists.
        del label
        group = QWidget()
        # Chrome, not a view: without this the window's QWidget rule painted
        # every difficulty's group navy, so View opacity uncovered the group
        # instead of the map behind it (owner, 2026-10-08).
        see_through(group)
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(0)

        # Insert before the trailing stretch that keeps groups pinned to the top.
        self.editor_views_layout.insertWidget(self.editor_views_layout.count() - 1, group)
        self._editor_view_groups[source_path] = group_layout
        return group_layout

    def _taiko_difficulty_entries(self) -> list[tuple[str, Path]]:
        """(label, path) for every taiko difficulty of the open song.

        `song_difficulties` is a bare `*.osu` glob of the folder, so a mapset
        that also ships osu!standard or mania difficulties would offer charts of
        files whose hit objects are not taiko notes at all. `read_header` is the
        song library's own mode check and stops before `[Difficulty]`, so this
        costs a few hundred bytes per file and only on opening a dialog.

        The difficulty currently open is kept whatever its mode says: a file
        opened by hand must never vanish from its own dialog.
        """
        current = self.difficulty_combo.currentData()
        entries = []
        for index in range(self.difficulty_combo.count()):
            path = Path(self.difficulty_combo.itemData(index))
            header = read_header(path)
            taiko = header is not None and header.get("mode") == "1"
            if taiko or str(path) == str(current):
                entries.append((self.difficulty_combo.itemText(index), path))
        return entries

    def _open_add_view_dialog(self) -> None:
        if not self.song_difficulties:
            QMessageBox.information(
                self,
                tr("MainWindow", "Open a map first"),
                tr("MainWindow", "Open a beatmap before adding a view."),
            )
            return
        entries = self._taiko_difficulty_entries()
        current = self.difficulty_combo.currentData()
        dialog = AddViewDialog(entries, self, Path(current) if current else None)
        if dialog.exec() != QDialog.Accepted:
            return
        view_type = dialog.selected_view_type()
        for difficulty_path in dialog.selected_difficulty_paths():
            # Idempotent: ticking every difficulty a second time must not stack
            # a second chart on each of them. A view type is asked for once per
            # difficulty; a second one is what the type combo is for.
            if any(
                getattr(frame, "difficulty_path", None) == difficulty_path
                and frame.view_type == view_type
                for frame in self._editor_views
            ):
                continue
            self._add_editor_view(view_type, difficulty_path)

    def _add_editor_view(
        self, view_type: str, difficulty_path: Path, container: QVBoxLayout | None = None,
    ) -> None:
        """Open one view. `container` overrides where the frame is placed.

        The gimmick page passes its own layer stack, so its "+" reuses this
        wiring rather than growing a second, read-only copy of it. Frames added
        there are bands like the six fixed layers, with the same compact chrome.
        """
        try:
            state = self._ensure_state(difficulty_path)
        except Exception as error:
            QMessageBox.critical(self, tr("MainWindow", "Open failed"), str(error))
            return

        band = container is not None
        label = state.document.version or difficulty_path.stem
        frame = EditorViewFrame(view_type, label, compact=band)
        frame.difficulty_path = difficulty_path
        frame.closed.connect(self._close_editor_view)

        # The layer-restricted types are ordinary chart/gameplay views with one
        # layer's filter on them, so they fall through to everything the plain
        # type already sets up rather than growing a branch each.
        chart_layer = self.CHART_ONLY_VIEWS.get(view_type)
        gameplay_layer = self.GAMEPLAY_ONLY_VIEWS.get(view_type)
        if chart_layer is not None:
            view_type = "chart"
        elif gameplay_layer is not None:
            view_type = "gameplay"
        # ...and so is the Kiai and Sound Volume layer: an unfiltered SV view,
        # differing only in which range tools its drag can be holding, which
        # every SV view now routes through _sv_range_action anyway.
        kiai_sound = view_type == "kiai_sound"
        if kiai_sound:
            view_type = "sv"

        if view_type == "chart":
            view = TimelineGameplay()
            view.set_symmetric(True)
            # A note is placed by hand and then nudged, same as a gimmick
            # object (see the direct TimelineGameplay() construction below
            # for the gimmick page's own layers) -- without this, a select
            # drag could box notes but a press on one just started a new
            # rubber-band instead of moving them.
            view.move_enabled = True
            # A layer-restricted band (chart_layer) or a gimmick-page band
            # (band) behaves like the gimmick page's own fixed layers --
            # grab and nudge without selecting first. A plain Editor-page
            # chart view is the "select first, then drag it" one.
            view.move_requires_selection = chart_layer is None and not band
            # Right-click-scrub target for a live drag-selection (see
            # TimeAxisMixin._press_is_on_timing_bar): the gimmick page's own
            # bar for a band added there, the Editor page's otherwise.
            view.timing_bar = self.gimmick_timing_bar if band else self.timing_bar
            if chart_layer is not None:
                self._apply_chart_layer(view, chart_layer)
                view.timing_line_edit_requested.connect(
                    lambda uid, dp=difficulty_path: self._edit_timing_line(dp, uid)
                )
                # _apply_chart_layer hands the barline layer its red lines to
                # edit, so this band offers right click and Delete on them the
                # same way the page's own layer does. Connected only here: a
                # plain chart view shows no red lines to act on.
                view.timing_lines_delete_requested.connect(
                    lambda uids, dp=difficulty_path:
                        self._delete_gimmick_objects(dp, (), uids)
                )
            view.load_document(state.document)
            view.set_snap_divisor(int(self.editor_snap_combo.currentData()))
            # The shared playhead, not this difficulty's stored one: every
            # open view rides one clock, so a new view starting at its own
            # difficulty's first note opened misaligned with all of them.
            view.current_time = self.timeline.current_time
            view.window_ms = self._editor_window_ms()
            view.zoom_changed.connect(
                lambda window_ms, dp=difficulty_path, v=view: self._zoom_changed(dp, window_ms, v)
            )
            # Scrubbing any chart view moves the one shared playhead, so every
            # open view -- this one, the player deck, and any others -- stays
            # in sync, matching "chart viewing" rather than independent tabs.
            view.seek_requested.connect(self.seek_audio)
            view.note_place_requested.connect(
                lambda note_kind, time_ms, new_combo, big, dp=difficulty_path: self._place_note(dp, note_kind, time_ms, new_combo, big)
            )
            view.note_place_with_duration_requested.connect(
                lambda note_kind, start_ms, end_ms, new_combo, big, dp=difficulty_path:
                    self._place_note_with_duration(dp, note_kind, start_ms, end_ms, new_combo, big)
            )
            view.note_duration_edit_requested.connect(
                lambda uid, new_end_ms, dp=difficulty_path: self._edit_note_duration(dp, uid, new_end_ms)
            )
            view.note_length_edit_requested.connect(
                lambda uid, dp=difficulty_path: self._type_note_length(dp, uid)
            )
            # Same handler the gimmick page's own layers use: _move_objects'
            # _expand_move only grows a drag into the structure around it when
            # there actually is one (a fake slider's squash line, a barline
            # note's restores), so a plain don/kat here just retimes itself.
            view.objects_move_requested.connect(
                lambda notes, points, delta, dp=difficulty_path: self._move_objects(dp, notes, points, delta)
            )
            # A layer band deletes the whole structure, the same as the gimmick
            # page's own layers -- right-clicking a fake slider in one and
            # leaving its 60000 BPM line behind is the wreckage
            # _delete_gimmick_objects exists to prevent. It also clears the
            # red-line selection, which _delete_notes does not: without that,
            # one Delete keypress produced two undo steps here.
            if chart_layer is not None:
                view.note_delete_requested.connect(
                    lambda uid, dp=difficulty_path: self._delete_gimmick_objects(dp, [uid])
                )
                view.notes_delete_requested.connect(
                    lambda uids, dp=difficulty_path: self._delete_gimmick_objects(dp, uids)
                )
            else:
                view.note_delete_requested.connect(
                    lambda uid, dp=difficulty_path: self._delete_note(dp, uid)
                )
                view.notes_delete_requested.connect(
                    lambda uids, dp=difficulty_path: self._delete_notes(dp, uids)
                )
            frame.set_content(view)
            frame.chart_view = view
            if not band:
                view.setFixedHeight(self.editor_view_height_spin.value())
            self._register_chart_view(view)
            self._share_kiai_bands(view, sv=False)
            self._editor_snap_views.append(view)
            # Locking/unlocking the currently-active view must re-enable or
            # disable the global tool row, since it can't watch every
            # frame's lock state itself.
            frame.lock_button.toggled.connect(
                lambda _checked, v=view: self._tool_state.sync_global_tool_row() if v is self._active_chart_view else None
            )
            # Newly-added view becomes the global tool row's target. Set
            # directly rather than relying solely on setFocus() -> Qt's
            # focusChanged: that requires a truly active top-level window,
            # which doesn't hold under every platform plugin (offscreen
            # tests included), so this guarantees the invariant either way.
            # focusChanged still applies when a user later clicks into a
            # different already-open view.
            view.setFocus(Qt.FocusReason.OtherFocusReason)
            self._active_chart_view = view
            self._last_focused_editor_view = view
            self._tool_state.sync_global_tool_row()
            self.global_tool_row.setVisible(True)
            self.global_sv_tool_row.setVisible(False)
        elif view_type == "sv":
            view = SVEditorView()
            # Marks the Kiai and Sound Volume view for the global SV tool row,
            # which is the one place an unfiltered SV view still has to be told
            # apart from an ordinary one (see _sv_tools_for).
            view.kiai_sound = kiai_sound
            # The Kiai and Sound Volume layer has nothing to do with SV: its
            # axis, curve and drags all read/write note volume instead.
            view.volume_mode = kiai_sound
            view.timing_bar = self.gimmick_timing_bar if band else self.timing_bar
            view.load_document(state.document)
            view.set_snap_divisor(int(self.editor_snap_combo.currentData()))
            # The shared playhead, not this difficulty's stored one: every
            # open view rides one clock, so a new view starting at its own
            # difficulty's first note opened misaligned with all of them.
            view.current_time = self.timeline.current_time
            view.window_ms = self._editor_window_ms()
            view.zoom_changed.connect(
                lambda window_ms, dp=difficulty_path, v=view: self._zoom_changed(dp, window_ms, v)
            )
            view.seek_requested.connect(self.seek_audio)
            view.function_range_requested.connect(
                lambda start_ms, end_ms, dp=difficulty_path, v=view:
                    self._sv_range_action(dp, v.tool, start_ms, end_ms)
            )
            view.point_add_requested.connect(
                lambda time_ms, sv, dp=difficulty_path: self._add_sv_point(dp, time_ms, sv)
            )
            view.point_time_edit_requested.connect(
                lambda uid, time_ms, dp=difficulty_path: self._edit_sv_point_time(dp, uid, time_ms)
            )
            view.point_sv_edit_requested.connect(
                lambda uid, sv, dp=difficulty_path: self._edit_sv_point(dp, uid, sv)
            )
            view.point_volume_edit_requested.connect(
                lambda uid, volume, dp=difficulty_path:
                    self._edit_volume_point(dp, uid, volume)
            )
            view.point_volume_dialog_requested.connect(
                lambda uid, dp=difficulty_path: self._type_volume_point(dp, uid)
            )
            view.point_delete_requested.connect(
                lambda uid, dp=difficulty_path: self._delete_sv_point(dp, uid)
            )
            view.points_delete_requested.connect(
                lambda uids, dp=difficulty_path: self._delete_sv_points(dp, uids)
            )
            view.timing_line_edit_requested.connect(
                lambda uid, dp=difficulty_path: self._edit_timing_line(dp, uid)
            )
            frame.set_content(view)
            frame.sv_view = view
            if not band:
                view.setFixedHeight(self.editor_view_height_spin.value())
            self._sv_views.append(view)
            self._share_kiai_bands(view, sv=True)
            self._editor_snap_views.append(view)
            frame.lock_button.toggled.connect(
                lambda _checked, v=view: self._tool_state.sync_global_sv_tool_row() if v is self._active_sv_view else None
            )
            view.setFocus(Qt.FocusReason.OtherFocusReason)
            self._active_sv_view = view
            self._last_focused_editor_view = view
            self._tool_state.sync_global_sv_tool_row()
            self.global_tool_row.setVisible(False)
            self.global_sv_tool_row.setVisible(True)
        elif view_type == "gameplay":
            view = GameplayViewerView()
            if gameplay_layer is not None:
                view.object_filter = self._layer_object_filter(gameplay_layer)
            self._share_kiai_bands(view, sv=False)
            view.load_document(state.document)
            view.set_snap_divisor(int(self.editor_snap_combo.currentData()))
            view.current_time = state.playhead_ms
            view.seek_requested.connect(self.seek_audio)
            frame.set_content(view)
            frame.gameplay_view = view
            skin = getattr(self, "skin", None)
            if skin is not None:
                view.set_skin(skin)
            view.set_mods(self._mods)
            view.set_autoplay(self._autoplay)
            self._gameplay_views.append(view)
            self._editor_snap_views.append(view)
        elif view_type == "density":
            view = DensityOverview()
            view.load_document(state.document, state.duration_hint)
            view.seek_requested.connect(self.seek_audio)
            frame.set_content(view)
            frame.density_view = view
            self._density_views.append(view)

        frame.move_requested.connect(self._move_view)
        frame.drag_started.connect(self._begin_view_drag)
        self._editor_views.append(frame)
        if container is not None:
            content = frame.content
            if isinstance(content, (TimelineGameplay, SVEditorView)):
                content.setFixedHeight(self.gimmick_view_height_spin.value())
                # Same base snapshot and snap the six layers use: a view added
                # here that snapped against the live document would collapse its
                # grid onto the gimmick's 60000 BPM lines.
                if self._gimmick_pairing is not None:
                    content.set_base_timing(self._gimmick_pairing.base_timing)
                content.set_snap_divisor(self._gimmick_snap_divisor())
            # Above the trailing stretch, so added bands land under the layers.
            container.insertWidget(container.count() - 1, frame)
            return
        group_layout = self._difficulty_group_layout(difficulty_path, label)
        self._insert_into_group(group_layout, frame)

    def _frame_layout(self, frame: EditorViewFrame):
        """The box layout that directly holds `frame`, or None.

        Found rather than remembered: the Editor page puts frames in a layout
        per difficulty group and the gimmick page in one shared column, and
        `indexOf` is the only thing that knows which. Holding a reference per
        frame would be a second copy of what the layout already says, and one
        that a rebuild could leave pointing at a deleted layout.
        """
        parent = frame.parentWidget()
        if parent is None:
            return None
        candidates = list(parent.findChildren(QBoxLayout))
        if parent.layout() is not None:
            candidates.append(parent.layout())
        for layout in candidates:
            if layout.indexOf(frame) >= 0:
                return layout
        return None

    def _move_view(self, frame: EditorViewFrame, delta: int) -> None:
        """Swap `frame` with the next view above (-1) or below (+1) it.

        Swapped with the next *frame* rather than the next layout item, because
        both columns carry things that are not views -- a trailing stretch, a
        group header -- and stepping onto one would file the view under the
        wrong difficulty or below the stretch that holds the column up.

        At either end this does nothing rather than wrapping around: a view at
        the top asked to go up is already where it was asked to go.
        """
        layout = self._frame_layout(frame)
        if layout is None:
            return
        index = layout.indexOf(frame)
        target = index + delta
        while 0 <= target < layout.count():
            if isinstance(layout.itemAt(target).widget(), EditorViewFrame):
                break
            target += delta
        else:
            # Nothing further this way inside the group. On the Editor page
            # that is the difficulty group's edge, and the move goes to the
            # group as a whole -- see `_move_difficulty_group`.
            self._move_difficulty_group(layout, delta)
            return
        if not 0 <= target < layout.count():
            return
        layout.removeWidget(frame)
        # Removing shifts everything after `index` down one, so `target` is
        # already the slot on the far side of the neighbour when moving down,
        # and is untouched when moving up.
        layout.insertWidget(target, frame)
        self._sync_view_lists(layout, frame)

    def _sync_view_lists(self, layout, frame: EditorViewFrame) -> None:
        """Put the view lists back in the order `layout` now shows.

        The lists decide which layer the tool row and copy/paste follow
        (`_gimmick_views[0]`), so leaving them in the old order would make
        "first" mean something the screen disagrees with. Only the slots this
        layout owns are rewritten -- the Editor page interleaves several
        difficulty groups in one list, and the others have not moved.
        """
        for views in (self._editor_views, self._gimmick_views):
            if frame not in views:
                continue
            # Only this list's own frames: the gimmick page's stack holds the
            # six layers (`_gimmick_views`) *and* bands added with "+"
            # (`_editor_views`), and taking every frame wrote a layer into the
            # band's slot -- the band then dropped out of the list, and closing
            # the Editor's views closed the layer instead of it.
            ordered = [
                layout.itemAt(i).widget() for i in range(layout.count())
                if isinstance(layout.itemAt(i).widget(), EditorViewFrame)
                and layout.itemAt(i).widget() in views
            ]
            slots = [i for i, existing in enumerate(views) if existing in ordered]
            for slot, moved in zip(slots, ordered):
                views[slot] = moved
            break

    def _view_drag_items(self, frame: EditorViewFrame):
        """(layout, the widgets a drag of `frame` reorders, the one it moves).

        A view moves among the views of its own stack -- its difficulty's on
        the Editor page, the one column on the gimmick page -- never into
        another difficulty's, as for the arrows. A difficulty with nothing to
        reorder it against (one view, which "tick all difficulties" makes)
        moves as a whole group among the others, as `_move_difficulty_group`
        does at the edge.
        """
        layout = self._frame_layout(frame)
        if layout is None:
            return None
        frames = [
            widget for widget in (layout.itemAt(i).widget() for i in range(layout.count()))
            if isinstance(widget, EditorViewFrame)
        ]
        if len(frames) > 1:
            return layout, frames, frame
        group = layout.parentWidget()
        page = self.editor_views_layout
        if layout is self.gimmick_views_layout or group is None or page.indexOf(group) < 0:
            return None
        groups = [page.itemAt(i).widget() for i in range(page.count()) if page.itemAt(i).widget() is not None]
        return (page, groups, group) if len(groups) > 1 else None

    def _begin_view_drag(self, frame: EditorViewFrame, global_pos) -> None:
        """Lift `frame` (or its difficulty's group) and let the pointer carry
        it; the layout changes once, on drop. See motion.ReorderDrag."""
        found = self._view_drag_items(frame)
        if found is None:
            return
        layout, items, moving = found
        # The page's own column, even for a difficulty group's views: the
        # overlay is drawn there so it is not clipped to the group.
        page = self.gimmick_views_layout if layout is self.gimmick_views_layout else self.editor_views_layout
        container = page.parentWidget()

        def drop(new_index: int) -> None:
            if new_index == items.index(moving):
                return  # put back where it was
            others = [item for item in items if item is not moving]
            layout.removeWidget(moving)
            if new_index < len(others):
                layout.insertWidget(layout.indexOf(others[new_index]), moving)
            else:
                layout.insertWidget(layout.indexOf(others[-1]) + 1, moving)
            if isinstance(moving, EditorViewFrame):
                self._sync_view_lists(layout, moving)

        self._view_drag = ReorderDrag(container, items, moving, global_pos, drop)

    def _move_difficulty_group(self, group_layout, delta: int) -> None:
        """Move a whole difficulty group past its neighbour on the Editor page.

        The Editor page keeps each difficulty's views in a group of their own,
        and a move never crosses into another group -- that would file a view
        under the wrong difficulty. But stopping dead at the group's edge left
        a difficulty with a single view (one chart per difficulty is exactly
        what "tick all difficulties" produces) with two buttons that did
        nothing. At the edge, the group moves instead: the view goes where it
        was asked to, and it stays filed under its own difficulty.

        A no-op on the gimmick page, whose frames share one column with no
        groups, and at either end of the page.
        """
        group = group_layout.parentWidget()
        page = self.editor_views_layout
        if group is None or page.indexOf(group) < 0:
            return
        index = page.indexOf(group)
        target = index + delta
        # Skip anything that is not a group -- the trailing stretch that pins
        # the column to the top is a layout item with no widget.
        while 0 <= target < page.count() and page.itemAt(target).widget() is None:
            target += delta
        if not 0 <= target < page.count():
            return
        page.removeWidget(group)
        page.insertWidget(target, group)

    def _insert_into_group(self, group_layout: QVBoxLayout, frame: EditorViewFrame) -> None:
        """Place `frame` in its difficulty group, keeping density bottommost.

        Density stays last in its group regardless of add order: a density
        view added first still ends up below a chart view added afterward.
        """
        if frame.view_type == "density":
            group_layout.addWidget(frame)
            return
        insert_index = group_layout.count()
        for i in range(group_layout.count()):
            widget = group_layout.itemAt(i).widget()
            if isinstance(widget, EditorViewFrame) and widget.view_type == "density":
                insert_index = i
                break
        group_layout.insertWidget(insert_index, frame)

    # Floor for every tool button in the program: the Editor page's two rows and
    # the gimmick page's six all sit in the same spot below the views, so a
    # shorter button there reads as a different, smaller control. showEvent
    # raises them all to the tallest polished hint, which is what stops a
    # descender being clipped once the window stylesheet's padding lands.
    # What TOOL_BUTTON_STYLE's labels actually measure once polished. Note
    # that `equalize_button_widths(heights=True)` in showEvent *replaces* this
    # with the measured hint rather than treating it as a floor, so a value
    # above the hint is silently dropped -- it is a build-time size, not a
    # minimum, whatever the call site's comment says.
    TOOL_BUTTON_HEIGHT = 24

    def _editor_view_focus_changed(self, _old, new) -> None:
        self._tool_state.editor_view_focus_changed(_old, new)
        # Re-sent only when the heard difficulty actually changes: focus moves
        # on every click into a spin box, and a gimmick map's schedule is a
        # sort of thousands of objects.
        if (
            getattr(self, "state", None) is not None
            and self._hitsound_state() is not getattr(self, "_sounded_state", None)
        ):
            self._apply_hitsound_source()

    # Read/write pass-throughs: clipboard, editor-view management and
    # playback code elsewhere in MainWindow still read and write these
    # directly, and the test suite reaches for several of them too.
    @property
    def _active_chart_view(self): return self._tool_state.active_chart_view
    @_active_chart_view.setter
    def _active_chart_view(self, value): self._tool_state.active_chart_view = value
    @property
    def _active_sv_view(self): return self._tool_state.active_sv_view
    @_active_sv_view.setter
    def _active_sv_view(self, value): self._tool_state.active_sv_view = value
    @property
    def _last_focused_editor_view(self): return self._tool_state.last_focused_editor_view
    @_last_focused_editor_view.setter
    def _last_focused_editor_view(self, value): self._tool_state.last_focused_editor_view = value
    @property
    def tool_buttons(self): return self._tool_state.tool_buttons
    @property
    def new_combo_button(self): return self._tool_state.new_combo_button
    @property
    def resnap_button(self): return self._tool_state.resnap_button
    @property
    def sv_tool_buttons(self): return self._tool_state.sv_tool_buttons
    @property
    def global_tool_row(self): return self._tool_state.global_tool_row
    @property
    def global_sv_tool_row(self): return self._tool_state.global_sv_tool_row
    @property
    def SV_TOOLS_DEFAULT(self): return self._tool_state.SV_TOOLS_DEFAULT
    @property
    def SV_TOOLS_KIAI_SOUND(self): return self._tool_state.SV_TOOLS_KIAI_SOUND
    @property
    def tool_shortcuts(self): return self._tool_state.tool_shortcuts
    @property
    def _tool_shortcuts_by_scope(self): return self._tool_state._tool_shortcuts_by_scope
    @property
    def _tool_shortcuts_allowed(self): return self._tool_state._tool_shortcuts_allowed

    def _difficulty_for_view(self, view) -> Path | None:
        """Which difficulty a chart/SV view belongs to.

        The gimmick page's six layers are searched too. They were not, which is
        the whole reason paste did nothing there: the lookup returned None, the
        state lookup that follows returned None, and the paste handler bailed
        without a word.
        """
        for frame in [*self._editor_views, *self._gimmick_views]:
            if getattr(frame, "chart_view", None) is view or getattr(frame, "sv_view", None) is view:
                return getattr(frame, "difficulty_path", None)
        return None

    # -- clipboard -------------------------------------------------------------

    def _build_clipboard_shortcuts(self) -> None:
        """Ctrl+C / Ctrl+V, routed the same way the tool digits are: to
        whichever of the two tool rows is visible, so copying in a chart view
        copies notes and copying in an SV view copies green lines. Fixed
        sequences rather than ShortcutRegistry entries -- these are the
        platform's own copy/paste keys, not app-specific bindings.

        Scoped to the Editor page (WidgetWithChildrenShortcut on
        self.editor_page) rather than application-wide on purpose: an
        application-context shortcut *consumes* the key press, so a global
        Ctrl+C would silently break copying text out of any line edit or spin
        box elsewhere in the app, and the should_ignore_shortcut_focus guard
        can only decline to act, not hand the key back.

        Sequences come from the registry (defaulting to the platform's own
        Ctrl+C / Ctrl+V) so Settings lists them with everything else.
        """
        # One pair per page that edits. WidgetWithChildrenShortcut only fires
        # for descendants of the widget it is installed on, and the gimmick page
        # is a sibling of the Editor page rather than a child -- so its six
        # layers never saw the copy/paste keys at all.
        self.copy_shortcut = QShortcut(QKeySequence(self.shortcuts.sequence("copy")), self.editor_page)
        self.copy_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.copy_shortcut.activated.connect(self.copy_selection)
        self.paste_shortcut = QShortcut(QKeySequence(self.shortcuts.sequence("paste")), self.editor_page)
        self.paste_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.paste_shortcut.activated.connect(self.paste_clipboard)
        self.resnap_shortcut = QShortcut(QKeySequence(self.shortcuts.sequence("resnap")), self.editor_page)
        self.resnap_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.resnap_shortcut.activated.connect(self.resnap_selection)
        self.gimmick_copy_shortcut = QShortcut(QKeySequence(self.shortcuts.sequence("copy")), self.gimmick_page)
        self.gimmick_copy_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.gimmick_copy_shortcut.activated.connect(self.copy_selection)
        self.gimmick_paste_shortcut = QShortcut(QKeySequence(self.shortcuts.sequence("paste")), self.gimmick_page)
        self.gimmick_paste_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.gimmick_paste_shortcut.activated.connect(self.paste_clipboard)

    def _clipboard_target(self) -> str:
        """"sv" or "chart" -- which editor the copy/paste keys act on.

        Neither Editor-page tool row is visible on the gimmick page, so asking
        them there always answered "chart" -- which is why Ctrl+C in one of the
        three SV layers copied notes. There the focused layer says which it is.
        """
        if self.page_stack.currentIndex() == PAGE_GIMMICK:
            layer = getattr(self, "_active_gimmick_layer", "") or ""
            return "sv" if layer.startswith("sv_") else "chart"
        if self.global_sv_tool_row.isVisible() and self._active_sv_view is not None:
            return "sv"
        return "chart"

    def copy_selection(self) -> None:
        if should_ignore_shortcut_focus(QApplication.focusWidget()):
            return
        if self._clipboard_target() == "sv":
            self._copy_sv_points()
        else:
            self._copy_notes()

    def paste_clipboard(self) -> None:
        if should_ignore_shortcut_focus(QApplication.focusWidget()):
            return
        if self._clipboard_target() == "sv":
            self._paste_sv_points()
        else:
            self._paste_notes()

    def _copy_notes(self) -> None:
        """Copy a chart view's selection: its notes *and* the lines it owns.

        Both, because in the gimmick page they are one thing. A fake slider
        without its 60000 BPM line is an ordinary drumroll, and the barline
        layer has no notes at all -- its selection is nothing but red lines, so
        a notes-only clipboard made Ctrl+C there a no-op.
        """
        view = self._active_chart_view
        if view is None:
            return
        notes = view.selected_notes()
        lines = [
            point for point in view.timing_points
            if point.uid in view.selected_timing_uids
        ]
        if not notes and not lines:
            return
        # Grown into the whole structure first, the same way a drag and a delete
        # are. The fake slider layer never selects red lines -- it does not own
        # them -- so without this its Ctrl+C copied the slider and left the
        # 60000 BPM line that makes it fake behind.
        state = self._states.get(self._difficulty_for_view(view))
        if state is not None and getattr(view, "gimmick_layer", None) is not None:
            notes, lines = self._expand_move(
                state, {note.uid for note in notes}, {point.uid for point in lines},
            )
        # Green lines are copied too, and sorted behind the red line sharing
        # their millisecond. A gimmick's SV is part of the structure, not a
        # description of it: a Don/Kat fake slider's `fake_slider_sv` is what
        # takes the squashed note off screen, and a barline structure's handle
        # line is written by the same generator as its bars -- copied without
        # them, a pasted structure was a different object from the one copied.
        # The order matters because osu! resolves a shared timestamp by file
        # order and an uninherited point resets SV to 1.0x, so a green line
        # ahead of its red one is silently cancelled.
        lines = sorted(lines, key=lambda point: (point.time, 0 if point.uninherited else 1))
        base_times = [note.time for note in notes] + [point.time for point in lines]
        # A fake slider sits its offset *after* the snap it was placed on, and
        # paste lands the clipboard's base on the snapped playhead. A plain fake
        # slider's own line is on the slider's millisecond rather than the snap,
        # so nothing copied was on the snap: the base was the slider itself and
        # the paste dropped it straight onto the playhead, offset gone. Each one
        # counts its own snap as a candidate base, so a paste lands exactly where
        # placing it there would.
        if state is not None and getattr(view, "gimmick_layer", None) is not None:
            config = self._gimmick_config("fake_slider")
            shiny = self._shiny_times(state.document)
            base_times += [
                note.time - (
                    config.shiny_offset_ms if round(note.time) in shiny
                    else config.fake_slider_offset_ms
                )
                for note in notes if self.is_fake_slider(note)
            ]
        base = min(base_times)
        # Plain data, not the HitObjects themselves: a paste has to produce
        # new identities, and holding live objects would let a later edit (or
        # an undo) mutate what the clipboard "contains".
        # How far each object sits off its own snap, measured here rather than
        # recomputed on paste: a gimmick object is deliberately 1-2ms off the
        # grid (a fake slider's line, a barline restore) and that offset is the
        # structure, while the *snap* it is measured from has to be the
        # destination's. Storing both is what lets a paste land on the snap
        # without flattening the structure onto it. See `_paste_notes`.
        divisor = int(view.snap_divisor)
        self._clipboard_divisor = divisor

        def _off_grid(time_ms: float) -> int:
            return int(time_ms) - osu_snap_ms(
                snap_time(view.snap_points, float(time_ms), divisor))

        # Relative to the anchor's own, because the anchor is pasted *onto* the
        # destination snap: its offset from the source grid is spent by the
        # paste itself, and counting it again moved the whole clipboard by it.
        base_off_grid = _off_grid(base)

        def residual(time_ms: float) -> int:
            return _off_grid(time_ms) - base_off_grid

        self._note_clipboard = [
            {
                "offset": note.time - base,
                "residual": residual(note.time),
                "x": note.x, "y": note.y, "type": note.type, "hit_sound": note.hit_sound,
                "extras": tuple(note.extras), "hit_sample": note.hit_sample,
                # A spinner's length, taken now: its extras hold an absolute
                # end, which the paste moves with it (extras_at).
                "length": (note.end_time - note.time) if note.is_spinner and note.end_time is not None else None,
            }
            for note in notes
        ]
        self._timing_clipboard = [
            {
                "offset": point.time - base,
                "residual": residual(point.time),
                "beat_length": point.beat_length, "meter": point.meter,
                "sample_set": point.sample_set, "sample_index": point.sample_index,
                "volume": point.volume, "effects": point.effects,
                "uninherited_flag": 1 if point.uninherited else 0,
            }
            for point in lines
        ]
        self.status.setText(
            tr("MainWindow", "Copied {count} notes.").format(count=len(notes) + len(lines))
        )

    def _paste_notes(self, at_time: float | None = None) -> None:
        """Paste at the playhead, or at `at_time` (Ctrl+D's clone)."""
        view = self._active_chart_view
        if view is None or not (self._note_clipboard or self._timing_clipboard):
            return
        difficulty_path = self._difficulty_for_view(view)
        state = self._states.get(difficulty_path) if difficulty_path is not None else None
        if state is None:
            return
        # Snap the paste anchor to the grid. The playhead is wherever audio
        # happens to be, which is almost never exactly on a division, so
        # pasting at the raw position landed a copied pattern a few
        # milliseconds off every snap it was built on. `snap_points` rather
        # than the document's own timing: in a gimmick layer that is the base
        # snapshot, and the document's 60000 BPM lines have no usable grid.
        # `osu_snap_ms`, the same truncation the playhead itself lands on
        # (`TimeAxisMixin.snap_ms`) -- rounding to nearest here put the paste a
        # millisecond *ahead* of the cursor on every beat whose fractional part
        # was at least .5, which is why a pattern pasted a few beats along kept
        # ending up one millisecond off the grid it was copied from.
        anchor = view.current_time if at_time is None else at_time
        base = osu_snap_ms(max(0.0, snap_time(view.snap_points, anchor, view.snap_divisor)))

        divisor = getattr(self, "_clipboard_divisor", 0) or int(view.snap_divisor)

        def landing(entry) -> int:
            return self._paste_landing(view, base, divisor, entry)

        next_index = self._next_original_index(state)
        pasted = []
        for entry in self._note_clipboard:
            time_ms = landing(entry)
            pasted.append(HitObject(
                x=entry["x"], y=entry["y"], time=time_ms,
                type=entry["type"], hit_sound=entry["hit_sound"],
                extras=(entry["extras"] if entry["length"] is None
                        else (str(round(time_ms + entry["length"])), *entry["extras"][1:])),
                hit_sample=entry["hit_sample"],
                original_index=next_index,
            ))
            next_index += 1
        # Never a second point of the *same kind* on a millisecond that already
        # has one -- the same rule the barline generator follows, for the same
        # reason: of two red lines on a millisecond only the first is ever read,
        # and a second green line there just overrides the one already in it.
        taken = {
            kind: {
                round(p.time) for p in state.document.timing_points
                if bool(p.uninherited) is kind
            }
            for kind in (True, False)
        }
        lines = [
            TimingPoint(
                time=float(landing(entry)), beat_length=entry["beat_length"],
                meter=entry["meter"], sample_set=entry["sample_set"],
                sample_index=entry["sample_index"], volume=entry["volume"],
                uninherited_flag=entry["uninherited_flag"], effects=entry["effects"],
            )
            for entry in self._timing_clipboard
            if landing(entry) not in taken[bool(entry["uninherited_flag"])]
        ]
        commands = []
        if pasted:
            displaced = [
                victim
                for note in pasted
                for victim in self._notes_blocking(state.document, note.time, note.is_spinner)
            ]
            if displaced:
                commands.append(RemoveHitObjects(displaced))
            commands.append(InsertHitObjects(pasted))
        if lines:
            commands.append(InsertTimingPoints(lines))
        if not commands:
            return
        # One undo step for the whole paste, structures included.
        state.history.push(CompositeCommand(commands, "paste_notes"), state)
        with self._refresh_cycle():
            self._refresh_difficulty_views(difficulty_path)
            self._refresh_difficulty_sv_views(difficulty_path)
        self.status.setText(
            tr("MainWindow", "Pasted {count} notes.").format(count=len(pasted) + len(lines))
        )

    def _paste_landing(self, view, base: int, divisor: int, entry) -> int:
        """Where one clipboard entry goes, on the destination's grid.

        `base + offset` alone is off by a millisecond or two, and always
        was: both ends truncate their own beat positions to whole
        milliseconds (`osu_snap_ms`), so shifting a pattern by an integer
        carries the *source's* truncation to a destination whose fractional
        beat positions are different. That is the "pasted +-1 or 2 ms off
        the snap" report.

        So: strip the object's deliberate offset from its snap, snap what
        is left to the grid actually in force there, and put the offset
        back. Nearest rather than down, because the error being corrected
        goes both ways. Across a BPM change this lands on that section's
        real snap, which is the grid a mapper pasting there is looking at.

        Bailing out when the residual is half a division or more: at an
        absurd BPM (a gimmick wall's 12345) a division is shorter than the
        1-2ms a structure is offset by, so snapping would pull the object
        onto the wrong division entirely. There the raw shift is right,
        since such a section has no grid a mapper is aligning to.
        """
        raw = base + int(entry["offset"])
        residual = int(entry.get("residual", 0))
        timing = active_uninherited_at(view.snap_points, float(raw))
        if timing is not None and abs(residual) * 2 >= timing.beat_length / divisor:
            return raw
        return osu_snap_ms(
            snap_time(view.snap_points, float(raw - residual), divisor)) + residual

    def _copy_sv_points(self) -> None:
        view = self._active_sv_view
        if view is None:
            return
        points = view.selected_points()
        if not points:
            return
        base = points[0].time
        # Each point's distance off its own snap, as the note copy records it
        # (see `_copy_notes`), so the delta paste lands on the destination's
        # grid rather than carrying this one's truncation along.
        divisor = int(view.snap_divisor)
        self._sv_clipboard_divisor = divisor

        def off_grid(time_ms: float) -> int:
            return int(time_ms) - osu_snap_ms(snap_time(view.snap_points, float(time_ms), divisor))

        base_off_grid = off_grid(base)
        # `offset` is what the millisecond-delta paste (the Editor page's own
        # SV view) uses; the list's own order is what the index-mapped paste
        # (a gimmick SV layer) walks instead, since `selected_points` is
        # already sorted by time -- see `_paste_sv_points` on why the two
        # views need different targets and both need the same clipboard.
        self._sv_clipboard = [
            {
                "offset": point.time - base,
                "residual": off_grid(point.time) - base_off_grid,
                "beat_length": point.beat_length, "meter": point.meter,
                "sample_set": point.sample_set, "sample_index": point.sample_index,
                "volume": point.volume, "effects": point.effects,
            }
            for point in points
        ]
        self.status.setText(tr("MainWindow", "Copied {count} SV points.").format(count=len(points)))

    def _paste_sv_points(self) -> None:
        """Paste the SV clipboard, by millisecond delta or by object index.

        A gimmick SV layer owns a scattered set of milliseconds -- its
        structures' own, not a beat grid (`_sv_layer_times`) -- so a paste
        built from millisecond deltas off a snapped base routinely lands where
        the layer cannot see it: four green lines copied off four fake
        sliders have nowhere near four fake sliders exactly `offset` apart to
        land on. Mapped by index there instead: walk the layer's own owned
        milliseconds forward from the cursor and drop the clipboard values
        onto them in the order they were copied.

        The Editor page's standalone SV view owns every millisecond there is,
        so it keeps the old delta behaviour -- there is nothing to map onto
        that isn't already reachable by offset.
        """
        view = self._active_sv_view
        if view is None or not self._sv_clipboard:
            return
        difficulty_path = self._difficulty_for_view(view)
        state = self._states.get(difficulty_path) if difficulty_path is not None else None
        if state is None:
            return

        # None from `_sv_layer_times` means "owns every millisecond", which is
        # the *delta* case, not the empty one -- the Kiai and Sound Volume layer
        # is a gimmick layer that owns them all. Collapsed with `or ()` it came
        # out as owning none, so every paste there found no targets, toasted and
        # did nothing. Only a layer with its own scattered set maps by index.
        layer_id = getattr(view, "gimmick_layer", None)
        owned_times = (
            self._sv_layer_paste_times(layer_id, state.document) if layer_id is not None else None
        )
        if owned_times is not None:
            owned = sorted(owned_times)
            targets = [at for at in owned if at >= view.current_time - 0.001]
            if not targets:
                self.show_toast(tr(
                    "MainWindow", "No milliseconds after the cursor in this layer to paste onto.",
                ))
                return
            count = min(len(targets), len(self._sv_clipboard))
            pasted = [
                TimingPoint(
                    time=float(targets[index]), beat_length=entry["beat_length"],
                    meter=entry["meter"], sample_set=entry["sample_set"],
                    sample_index=entry["sample_index"], volume=entry["volume"],
                    uninherited_flag=0, effects=entry["effects"],
                )
                for index, entry in enumerate(self._sv_clipboard[:count])
            ]
            self._insert_sv_points(state, pasted, "paste_sv")
            self._refresh_difficulty_sv_views(difficulty_path)
            if count < len(self._sv_clipboard):
                self.status.setText(tr(
                    "MainWindow",
                    "Pasted {count} of {total} SV points -- this layer had no more "
                    "milliseconds of its own after the cursor.",
                ).format(count=count, total=len(self._sv_clipboard)))
            else:
                self.status.setText(
                    tr("MainWindow", "Pasted {count} SV points.").format(count=count)
                )
            return

        # Same grid snap as the note paste -- see _paste_notes.
        base = osu_snap_ms(max(0.0, snap_time(
            extract_timing_points(state.document), view.current_time, view.snap_divisor,
        )))
        divisor = getattr(self, "_sv_clipboard_divisor", 0) or int(view.snap_divisor)
        pasted = [
            TimingPoint(
                time=float(self._paste_landing(view, base, divisor, entry)),
                beat_length=entry["beat_length"],
                meter=entry["meter"], sample_set=entry["sample_set"],
                sample_index=entry["sample_index"], volume=entry["volume"],
                uninherited_flag=0, effects=entry["effects"],
            )
            for entry in self._sv_clipboard
        ]
        self._insert_sv_points(state, pasted, "paste_sv")
        self._refresh_difficulty_sv_views(difficulty_path)
        self.status.setText(tr("MainWindow", "Pasted {count} SV points.").format(count=len(pasted)))

    # -- "one object per millisecond" -----------------------------------------

    @staticmethod
    def _next_original_index(state: DifficultyState) -> int:
        """A fresh applied_positions key for an inserted note.

        Inserted notes used to keep HitObject's default original_index of 0,
        so every one of them aliased the map's very first note in
        applied_positions and in a chart view's selection set.
        """
        return 1 + max(
            [note.original_index for note in state.document.hit_objects]
            + list(state.applied_positions)
            + [-1]
        )

    @staticmethod
    def _notes_blocking(document, time_ms: int, placing_spinner: bool) -> list:
        """Existing hit objects a new one at `time_ms` would have to replace.

        Only one object may sit on any given millisecond, with one exception:
        a spinner (denden) may share a timestamp with anything, in both
        directions -- its start and end routinely land on top of notes and
        neither displaces the other.
        """
        if placing_spinner:
            return []
        return [
            note for note in document.hit_objects
            if note.time == time_ms and not note.is_spinner
        ]

    def _insert_notes(self, state: DifficultyState, notes: list, label_id: str) -> None:
        """Insert notes, replacing anything they would stack on, as one undo step."""
        displaced = []
        seen: set[int] = set()
        for note in notes:
            for victim in self._notes_blocking(state.document, note.time, note.is_spinner):
                if id(victim) not in seen:
                    seen.add(id(victim))
                    displaced.append(victim)
        if displaced:
            state.history.push(
                CompositeCommand([RemoveHitObjects(displaced), InsertHitObjects(notes)], label_id),
                state,
            )
        else:
            state.history.push(InsertHitObjects(notes), state)

    def _insert_points_command(self, document, points: list, label_id: str = "insert_timing_points"):
        """One command inserting `points`, with at most one green line per ms.

        osu! resolves a shared timestamp by file order and the **last** point
        wins (`active_point_at` walks to the last point at or before the time),
        so a green line stacked on another is not wrong in game -- it is simply
        a line that does nothing, which the editor still draws, hit-tests,
        drags and sweeps from. A pile of those is what "the SV looks weird
        where there are many gimmick objects" is.

        Every generator had its own idea of the rule: the SV editor replaced
        what it landed on, the red-line generator dropped duplicate *red* lines
        and stacked the green ones beside them, and both anti-barline
        converters did the same. One place now, so a new generator cannot
        reintroduce it. Keeping the last of a tie and removing what it lands on
        leaves exactly the speed the stack had, written once.

        Replacing rather than skipping, which is what placing a note or
        dragging one onto an occupied millisecond already does -- the newer
        line is the one the mapper just asked for.

        Uninherited points are deliberately untouched here: a red and a green
        legitimately share a millisecond (the case the SV editor paints
        yellow), and which red lines a generator may write against the chart's
        own timing is that generator's decision, not this one's.
        """
        # Last of a tie, so `kept` carries the value that was in force. Walked
        # backwards for that reason and re-reversed, rather than letting the
        # first one win and silently changing the speed a batch produces.
        kept: list = []
        seen: set[int] = set()
        for point in reversed(points):
            if point.uninherited:
                kept.append(point)
                continue
            at = round(point.time)
            if at in seen:
                continue
            seen.add(at)
            kept.append(point)
        kept.reverse()
        displaced = [
            point for point in document.timing_points
            if not point.uninherited and round(point.time) in seen
        ]
        insert = InsertTimingPoints(kept)
        if not displaced:
            return insert
        return CompositeCommand([RemoveTimingPoints(displaced), insert], label_id)

    def _insert_sv_points(self, state: DifficultyState, points: list, label_id: str) -> None:
        """Insert inherited points, replacing any that share a millisecond."""
        state.history.push(
            self._insert_points_command(state.document, points, label_id), state)

    def _build_hit_object(self, note_kind: str, time_ms: int, new_combo: bool, big: bool = False) -> HitObject:
        """don/kat only. Slider/spinner carry a duration this signature has
        no room for -- see _build_extendable_hit_object.

        Big (finisher) is the same HITSOUND_FINISH bit sliders already place
        with Shift held; kat additionally keeps its clap bit, since a big kat
        is a finisher clap, not a finisher replacing the clap.
        """
        # Taiko gameplay ignores hit-object x/y entirely; center is the
        # convention used elsewhere in this codebase (e.g. the fake-slider
        # gimmick literal).
        x, y = PLAYFIELD_WIDTH // 2, PLAYFIELD_HEIGHT // 2
        combo_bit = TYPE_NEW_COMBO if new_combo else 0
        finisher_bit = HITSOUND_FINISH if big else 0
        if note_kind == "don":
            return HitObject(x=x, y=y, time=time_ms, type=TYPE_CIRCLE | combo_bit, hit_sound=finisher_bit)
        if note_kind == "kat":
            return HitObject(x=x, y=y, time=time_ms, type=TYPE_CIRCLE | combo_bit, hit_sound=HITSOUND_CLAP | finisher_bit)
        raise ValueError(f"Unknown note kind: {note_kind}")

    def _place_note(self, difficulty_path: Path, note_kind: str, time_ms: float, new_combo: bool, big: bool = False) -> None:
        state = self._states.get(difficulty_path)
        if state is None:
            return
        note = self._build_hit_object(note_kind, round(time_ms), new_combo, big)
        note.original_index = self._next_original_index(state)
        self._insert_notes(state, [note], "place_note")
        self._refresh_difficulty_views(difficulty_path)

    def _build_extendable_hit_object(
        self, note_kind: str, start_ms: int, end_ms: int, new_combo: bool, big: bool, timing_points: list,
        slider_multiplier: float = SLIDER_MULTIPLIER_ASSUMED,
    ) -> HitObject:
        """Slider/spinner, sized from a requested time duration (start_ms to
        end_ms) rather than a fixed default. A bare click (start == end)
        still gets a short, visible object instead of a zero-length one.
        """
        x, y = PLAYFIELD_WIDTH // 2, PLAYFIELD_HEIGHT // 2
        combo_bit = TYPE_NEW_COMBO if new_combo else 0
        duration = max(20.0, end_ms - start_ms)
        if note_kind == "slider":
            timing = active_uninherited_at(timing_points, start_ms)
            sv = sv_at(timing_points, start_ms)
            length = slider_length_for_duration(
                duration, timing.beat_length, sv, slider_multiplier,
            )
            hit_sound = HITSOUND_FINISH if big else 0
            return HitObject(
                x=x, y=y, time=start_ms, type=TYPE_SLIDER | combo_bit, hit_sound=hit_sound,
                extras=(f"L|{x + 80}:{y}", "1", f"{length:.3f}"),
            )
        if note_kind == "spinner":
            return HitObject(
                x=x, y=y, time=start_ms, type=TYPE_SPINNER | combo_bit, hit_sound=0,
                extras=(str(round(start_ms + duration)),),
            )
        raise ValueError(f"Unknown extendable note kind: {note_kind}")

    def _place_note_with_duration(
        self, difficulty_path: Path, note_kind: str, start_ms: float, end_ms: float, new_combo: bool, big: bool,
    ) -> None:
        state = self._states.get(difficulty_path)
        if state is None:
            return
        note = self._build_extendable_hit_object(
            note_kind, round(start_ms), round(end_ms), new_combo, big, state.document.timing_points,
            state.document.slider_multiplier,
        )
        note.original_index = self._next_original_index(state)
        self._insert_notes(state, [note], "place_note")
        self._refresh_difficulty_views(difficulty_path)

    def _edit_note_duration(self, difficulty_path: Path, uid: int, new_end_ms: float) -> None:
        """Extend/shorten an already-placed slider or spinner by dragging its
        right edge. Committed once on release (see TimelineGameplay), not
        per drag tick, so this doesn't need SetNoteFields to support merging.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        note = next((n for n in state.document.hit_objects if n.uid == uid), None)
        if note is None:
            return
        duration = max(20.0, new_end_ms - note.time)
        if note.is_spinner:
            new_extras = (str(round(note.time + duration)),)
        elif note.is_slider:
            timing = active_uninherited_at(state.document.timing_points, note.time)
            sv = sv_at(state.document.timing_points, note.time)
            length = slider_length_for_duration(
                duration, timing.beat_length, sv, state.document.slider_multiplier,
            )
            curve = note.extras[0] if note.extras else f"L|{note.x + 80}:{note.y}"
            new_extras = (curve, "1", f"{length:.3f}")
        else:
            return
        state.history.push(SetNoteFields(note.uid, {"extras": (note.extras, new_extras)}), state)
        self._refresh_difficulty_views(difficulty_path)

    def _type_note_length(self, difficulty_path: Path, uid: int) -> None:
        """Double click a fake slider: type its `length`.

        The one number that decides how far a fake slider draws, and the one
        the drag handles cannot reach -- its end precedes its start, so it has
        no right edge to pull. The toolbox's Length is what the *next* one is
        placed with; this is how one already in the map is changed.

        Bounded above by `FAKE_SLIDER_MAX_LENGTH` for the same reason
        `GimmickConfig` is: past it the object stops being a fake slider and
        becomes a real, scoreable drumroll, which is not an edit to this
        object but a different object.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        note = next((n for n in state.document.hit_objects if n.uid == uid), None)
        if note is None or note.length is None or not self.is_fake_slider(note):
            return
        length, accepted = QInputDialog.getDouble(
            self, tr("MainWindow", "Fake slider length"),
            tr("MainWindow", "Length (osu! pixels, no duration is the point)"),
            float(note.length), -100000.0, FAKE_SLIDER_MAX_LENGTH, 4,
        )
        if not accepted or length == note.length:
            return
        curve = note.extras[0] if note.extras else f"L|{note.x + 100}:{note.y}"
        slides = note.extras[1] if len(note.extras) > 1 else "1"
        new_extras = (curve, slides, gimmick_format_length(length))
        state.history.push(
            SetNoteFields(note.uid, {"extras": (note.extras, new_extras)}), state)
        self._refresh_difficulty_views(difficulty_path)

    def _delete_note(self, difficulty_path: Path, uid: int) -> None:
        state = self._states.get(difficulty_path)
        if state is None:
            return
        victim = next((note for note in state.document.hit_objects if note.uid == uid), None)
        if victim is None:
            return
        state.history.push(RemoveHitObjects([victim]), state)
        self._refresh_difficulty_views(difficulty_path)

    def _delete_notes(self, difficulty_path: Path, uids) -> None:
        """Delete/Backspace on a chart view's whole selection, as one undo step."""
        state = self._states.get(difficulty_path)
        if state is None:
            return
        wanted = set(uids)
        victims = [note for note in state.document.hit_objects if note.uid in wanted]
        if not victims:
            return
        state.history.push(RemoveHitObjects(victims), state)
        for view in self._editor_views_for(difficulty_path):
            selected = getattr(view, "selected", None)
            if isinstance(selected, set):
                selected.clear()
        self._refresh_difficulty_views(difficulty_path)

    def _hitsound_state(self):
        """The difficulty whose hit objects are sounded.

        Not always `self.state`: on the Editor page it is the difficulty of
        the view last clicked into. Everything else -- the timing bars,
        the kiai bands, the volume graph, both other pages -- stays on the
        active difficulty deliberately: those describe what is being *edited*,
        and following two different difficulties at once is how an overlay
        starts disagreeing with the chart under it.
        """
        # The Editor page only. That page is where several
        # difficulties are open side by side; the gimmick page edits exactly
        # one, and sounding some other difficulty's notes over it would be
        # hearing a chart that is not on screen. `_show_page` re-applies on
        # the way back.
        if (
            hasattr(self, "page_stack")
            and self.page_stack.currentIndex() != PAGE_EDITOR
        ):
            return self.state
        # "Active" here means the view last clicked into, not the window's
        # active difficulty: with several difficulties side by side, the one
        # being edited is the one worth hearing. Editor-page frames only -- a
        # gimmick layer stays the last focused view after leaving that page.
        # getattr: focus events start arriving before either exists.
        focused = getattr(self, "_tool_state", None) and self._last_focused_editor_view
        for frame in getattr(self, "_editor_views", ()):
            if focused is not None and focused in (
                getattr(frame, "chart_view", None), getattr(frame, "sv_view", None)
            ):
                state = self._states.get(getattr(frame, "difficulty_path", None))
                if state is not None:
                    return state
                break
        return self.state

    def _apply_hitsound_source(self) -> None:
        """Point the mixer at whichever difficulty is meant to be heard.

        One place, because `_activate_state` used to call `set_schedule`
        directly -- so switching the active difficulty silently threw away a
        pinned choice.
        """
        state = self._hitsound_state()
        self._sounded_state = state
        if state is None:
            return
        self.hitsounds.set_schedule(
            state.document.hit_objects, state.document.timing_points,
            state.document.slider_multiplier)

    def _reschedule_hitsounds(self, state) -> None:
        """`self.hitsounds.set_schedule`, once per refresh cycle.

        A placement runs both `_refresh_difficulty_views` and
        `_refresh_difficulty_sv_views` inside one `_refresh_cycle()`, and each
        calls this with identical arguments when the playing difficulty is the
        one being edited -- `hitsound_schedule` sorts both the hit object and
        timing point lists, so a gimmick map paid for that twice per edit.
        Guarded the same way `_cached` scopes its own memoization: keyed in
        `_doc_cache` when a cycle is open, unconditional outside one.
        """
        if self._doc_cache is not None:
            if self._doc_cache.get("hitsounds_scheduled"):
                return
            self._doc_cache["hitsounds_scheduled"] = True
        self.hitsounds.set_schedule(
            state.document.hit_objects, state.document.timing_points,
            state.document.slider_multiplier)

    def _refresh_difficulty_views(self, difficulty_path: Path) -> None:
        """Re-read hit objects into every open view of this difficulty after an edit.

        Deliberately does not touch self._timing_bars: a note place/delete
        never changes [TimingPoints] or [Editor] bookmarks, so reloading
        them here was pure wasted work -- and a visible one, since
        TimingOverviewBar.load_document forces an immediate repaint outside
        the normal ~120fps broadcast cadence, which read as a stutter/flicker
        every time a note was placed. Only _refresh_difficulty_sv_views
        (timing-point edits) needs to reload the timing bars.
        """
        with self._refresh_cycle():
            state = self._states.get(difficulty_path)
            if state is None:
                return
            # Placing or deleting a note must make it audible without reopening the
            # map, but only the difficulty actually being played is scheduled --
            # editing a sibling in another view changes nothing you can hear.
            if self._hitsound_state() is state:
                self._reschedule_hitsounds(state)
            for frame in self._editor_views:
                if getattr(frame, "difficulty_path", None) != difficulty_path:
                    continue
                chart_view = getattr(frame, "chart_view", None)
                if chart_view is not None:
                    chart_view.refresh_notes(state.document)
                gameplay_view = getattr(frame, "gameplay_view", None)
                if gameplay_view is not None:
                    gameplay_view.refresh_notes(state.document)
            # The gimmick layers show a difficulty like any other view does, and
            # every path that edits it -- undo, a right-click delete, a BPM retype --
            # arrives here. Refreshing them anywhere but here left one of those
            # paths behind each time.
            pairing = self._gimmick_pairing
            if pairing is not None and pairing.target == difficulty_path:
                self._refresh_gimmick_views()
            if self.state is state:
                self.timeline.refresh_notes(state.document)
                self.refresh_canvas()

        # -- M4: SV point editing -------------------------------------------------

    def _add_sv_point(self, difficulty_path: Path, time_ms: float, sv: float) -> None:
        state = self._states.get(difficulty_path)
        if state is None:
            return
        ordered = sorted_by_time(state.document.timing_points)
        template = active_point_at(ordered, time_ms)
        point = TimingPoint.inherited_at(
            round(time_ms), max(0.01, sv),
            # Kiai is a property of the active point, so a new SV point that
            # doesn't carry it forward silently ends the kiai section it was
            # dropped into.
            kiai=bool(template.kiai) if template is not None else False,
            template=template,
        )
        # _insert_sv_points enforces one inherited point per millisecond, so
        # clicking on top of an existing green line replaces it instead of
        # quietly stacking a second one behind it.
        self._insert_sv_points(state, [point], "add_sv_point")
        self._refresh_difficulty_sv_views(difficulty_path)

    def _edit_sv_point_time(self, difficulty_path: Path, uid: int, new_time: float) -> None:
        """Horizontal drag of a line (green or red): retime only, no SV
        change. Not inherited-only -- either kind's own grid position depends
        on its time, so this also refreshes chart views, not just the SV
        graph.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        point = next((p for p in state.document.timing_points if p.uid == uid), None)
        if point is None:
            return
        state.history.push(
            EditTimingPoint(uid, {"time": (point.time, new_time)}),
            state, allow_merge=True,
        )
        state.document.timing_points.sort(key=lambda p: p.time)
        with self._refresh_cycle():
            self._refresh_difficulty_views(difficulty_path)
            self._refresh_difficulty_sv_views(difficulty_path)

    def _edit_sv_point(self, difficulty_path: Path, uid: int, new_sv: float) -> None:
        """Vertical drag of a green line's own value dot: SV only, no retime."""
        state = self._states.get(difficulty_path)
        if state is None:
            return
        point = next((p for p in state.document.timing_points if p.uid == uid), None)
        if point is None or point.uninherited:
            return
        new_beat_length = -100.0 / max(0.01, new_sv)
        state.history.push(
            EditTimingPoint(uid, {"beat_length": (point.beat_length, new_beat_length)}),
            state, allow_merge=True,
        )
        self._refresh_difficulty_sv_views(difficulty_path)

    def _edit_volume_point(self, difficulty_path: Path, uid: int, new_volume: int) -> None:
        """Vertical drag in the Kiai and Sound Volume layer: hitsound volume.

        Any line, red or green: osu! reads the sample volume off whichever
        timing point is in force, and a red line is a timing point. Restricting
        this to green ones would have left the volume at a BPM change
        unreachable in the layer whose whole job is volume.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        point = next((p for p in state.document.timing_points if p.uid == uid), None)
        if point is None:
            return
        volume = max(0, min(100, int(new_volume)))
        if volume == point.volume:
            return
        state.history.push(
            EditTimingPoint(uid, {"volume": (point.volume, volume)}),
            state, allow_merge=True,
        )
        self._refresh_difficulty_sv_views(difficulty_path)

    def _type_volume_point(self, difficulty_path: Path, uid: int) -> None:
        """Double click in that layer: type the volume instead of dragging it."""
        state = self._states.get(difficulty_path)
        if state is None:
            return
        point = next((p for p in state.document.timing_points if p.uid == uid), None)
        if point is None:
            return
        volume, accepted = QInputDialog.getInt(
            self, tr("MainWindow", "Sound volume"),
            tr("MainWindow", "Hitsound volume (%)"), int(point.volume), 0, 100)
        if accepted:
            self._edit_volume_point(difficulty_path, uid, volume)

    def _delete_sv_point(self, difficulty_path: Path, uid: int) -> None:
        state = self._states.get(difficulty_path)
        if state is None:
            return
        # Inherited only: an uninherited point is a BPM section, not SV
        # editor scope, and deleting the last one would leave the map
        # without any timing at all.
        victim = next((p for p in state.document.timing_points if p.uid == uid and not p.uninherited), None)
        if victim is None:
            return
        state.history.push(RemoveTimingPoints([victim]), state)
        self._refresh_difficulty_sv_views(difficulty_path)

    def _delete_sv_points(self, difficulty_path: Path, uids) -> None:
        """Delete/Backspace on an SV view's selection, as one undo step.

        Same inherited-only rule as the single-point delete: an uninherited
        (BPM) point in the selection is skipped rather than removed.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        wanted = set(uids)
        victims = [
            point for point in state.document.timing_points
            if point.uid in wanted and not point.uninherited
        ]
        if not victims:
            return
        state.history.push(RemoveTimingPoints(victims), state)
        self._refresh_difficulty_sv_views(difficulty_path)

    def _open_sv_function_dialog(
        self, difficulty_path: Path, start_ms: float, end_ms: float, layer_id: str | None = None,
    ) -> None:
        state = self._states.get(difficulty_path)
        points = sorted_by_time(state.document.timing_points) if state is not None else []
        divisor = (
            self._gimmick_snap_divisor() if layer_id is not None
            else int(self.editor_snap_combo.currentData())
        )
        # Prefill with the SV the first and last objects of the sweep play at,
        # so "Generate" with nothing changed is a no-op rather than a jump to
        # some arbitrary ramp. It read the SV in force at the drag's edges
        # before, and a drag starting a little ahead of a run's first note
        # read the line before the run (2.70 -> 2.00 opened as 2.00 -> 2.00).
        # The edges stay the fallback for a range with nothing in it.
        targets = (
            self._sv_generation_times(state, start_ms, end_ms, {"placement": "notes"}, layer_id)
            if state is not None else []
        )
        dialog = SVFunctionDialog(
            start_ms, end_ms, self, snap_divisor=divisor,
            initial_rate=sv_at(points, targets[0] if targets else start_ms),
            final_rate=sv_at(points, targets[-1] if targets else end_ms),
            gimmick_layer=layer_id,
            settings=self.settings,
            position_offset=(
                self._gimmick_config(layer_id).sv_offset_ms if layer_id is not None else None
            ),
            # Layer 6's BPM filter defaults to the BPM in force where the drag
            # started, which only the base snapshot can answer honestly.
            base_timing=(
                self._gimmick_pairing.base_timing
                if layer_id == "sv_barline" and self._gimmick_pairing is not None else None
            ),
        )
        if dialog.exec() != QDialog.Accepted:
            return
        params = dialog.parameters()
        if layer_id is not None:
            # Written back before generating, because the layer decides which
            # milliseconds it owns from this number -- generate first and the
            # sweep would land where the layer is not yet looking.
            self._gimmick_config(layer_id).sv_offset_ms = params["position_offset"]
        self._generate_sv(difficulty_path, start_ms, end_ms, params, layer_id=layer_id)

    def _open_volume_function_dialog(
        self, difficulty_path: Path, start_ms: float, end_ms: float,
    ) -> None:
        """The Kiai and Sound Volume layer's Volume tool: the SV generator's
        easing curves aimed at hitsound volume, editing existing timing
        points rather than placing new ones (see _generate_volume).

        No `layer_id` is passed on -- there is no note/snap placement mode to
        decide between any more, since nothing is placed. SVFunctionDialog
        hides "Generate at" and the other placement-only rows in volume mode.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        ordered = sorted_by_time(state.document.timing_points)
        # Prefilled with the volume already in force at each end, so Generate
        # with nothing changed is a no-op -- same reasoning as the SV dialog.
        initial = active_point_at(ordered, start_ms)
        final = active_point_at(ordered, end_ms)
        dialog = SVFunctionDialog(
            start_ms, end_ms, self, snap_divisor=self._gimmick_snap_divisor(),
            initial_rate=initial.volume if initial else 100,
            final_rate=final.volume if final else 100,
            volume=True,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        self._generate_sv(
            difficulty_path, start_ms, end_ms, dialog.parameters(), volume=True,
        )

    def _sv_generation_times(
        self, state: DifficultyState, start_ms: float, end_ms: float, params: dict,
        layer_id: str | None = None,
    ) -> list[float]:
        """Where the generated SV points go, before the position offset.

        "Each note" (the default) puts one point on each hit object inside the
        selected range and nowhere else -- an SV sweep only has to be correct
        where a note actually is, and a point every 20ms between them was
        hundreds of timing lines nothing could see. "Every snap" walks the
        beat grid at the chosen divisor instead, for sweeps that have to move
        continuously.

        `layer_id` overrides both modes with the layer's own objects -- real
        notes for layer 4, fake sliders for 5, red lines for 6. Checked before
        the placement mode, not after: "every snap" walks the beat grid, which
        lands between a gimmick's objects, and a point there is both invisible
        in the layer that made it and reset to 1.0x by the next uninherited
        line. A gimmick SV layer only has one honest answer to "where", so it
        is not asked (SVFunctionDialog hides the choice).

        `params["include_shiny"]` adds shiny milliseconds to layer 5's targets
        for this one sweep only -- the layer's *ownership* is unchanged
        (`_sv_layer_object_times` still excludes them, and a hand-placed or
        generated shiny line outside a sweep is still layer 4's to show), this
        just lets one Generate run reach both at once instead of two runs.

        `params["only_red_line_bpm"]` (layer 6 only, None when off) is an
        inclusive (low, high) BPM interval; every position whose red line
        falls outside it is dropped, so a sweep can drive one 60000 BPM
        barline run -- or a whole family of them, "500-1000 BPM" -- and leave
        the chart's own timing lines, which share the layer, untouched. An
        exact single BPM is simply low == high.

        `params["only_fake_slider_length"]` (layer 5 only, None when off) is
        the same shape for `length`: a mapper who wants two structures reading
        the same millisecond in the editor to take different SV encodes the
        difference in a `length` too small to render differently (`-0.001` vs
        `-0.0011`), and this is how a sweep tells them apart. Defaults to
        `(-0.001, -0.001)`, the canonical fake slider length.
        """
        if layer_id is not None:
            # The objects themselves, unshifted: `_generate_sv` applies the
            # layer's offset as the position offset, and applying it here too
            # would double it.
            times = set(self._sv_layer_object_times(layer_id, state.document) or ())
            if layer_id == "sv_fake_slider" and params.get("include_shiny"):
                times |= self._shiny_times(state.document)
            selected = sorted(
                float(time_ms) for time_ms in times
                if start_ms - 0.001 <= time_ms <= end_ms + 0.001
            )
            wanted_bpm = params.get("only_red_line_bpm")
            if wanted_bpm is not None:
                low_bpm, high_bpm = wanted_bpm
                # Layer 6's BPM filter. Keyed on the exact millisecond first: these
                # times *are* red-line milliseconds, rounded, and a line at 1234.6
                # rounds to 1235, where active_uninherited_at would answer with the
                # line before it instead. The walk-back is the fallback for a
                # position that is not itself a red line, where the governing one
                # is the only sensible answer.
                reds = {
                    round(point.time): point.bpm
                    for point in sorted_by_time(state.document.timing_points)
                    if point.uninherited
                }
                selected = [
                    at for at in selected
                    if bpm_in_range(
                        reds[round(at)] if round(at) in reds
                        else active_uninherited_at(state.document.timing_points, at).bpm,
                        low_bpm, high_bpm,
                    )
                ]
            wanted_length = params.get("only_fake_slider_length")
            if wanted_length is not None:
                low_length, high_length = wanted_length
                # Layer 5's length filter: two fake sliders can occupy the exact
                # same millisecond (a Don/Kat's squash, a plain slider offset the
                # same amount) and look identical in the editor while encoding two
                # different things through `length` alone -- see
                # gimmick_session.FAKE_SLIDER_MAX_LENGTH on why the sign and
                # magnitude of a near-zero length is meaningful rather than noise.
                # Keyed on the millisecond the same way the BPM filter is; unlike a
                # red line's BPM a fake slider's length has no formula to drift
                # from, so the range check itself needs no tolerance.
                lengths = {
                    round(note.time): note.length
                    for note in state.document.hit_objects
                    if self.is_fake_slider(note) and note.length is not None
                }
                selected = [
                    at for at in selected
                    if round(at) in lengths and low_length <= lengths[round(at)] <= high_length
                ]
            return selected
        placement = params.get("placement", "notes")
        if placement == "snaps":
            divisor = max(1, int(params.get("snap_divisor", 4)))
            timing_points = extract_timing_points(state.document)
            times: list[float] = []
            # snap_time rounds to the *nearest* division, which can land just
            # before the range; step forward once when it does.
            cursor = snap_time(timing_points, start_ms, divisor)
            if cursor < start_ms - 0.001:
                cursor += active_uninherited_at(timing_points, cursor).beat_length / divisor
            # Guard against a pathological beat length producing an endless
            # walk; 500 matches the old generator's own cap.
            while cursor <= end_ms + 0.001 and len(times) < 501:
                times.append(cursor)
                step = active_uninherited_at(timing_points, cursor + 0.001).beat_length / divisor
                if step <= 0:
                    break
                cursor += step
            return times
        return sorted({
            float(note.time)
            for note in state.document.hit_objects
            if start_ms - 0.001 <= note.time <= end_ms + 0.001
        })

    def _generate_sv(
        self, difficulty_path: Path, start_ms: float, end_ms: float, params: dict,
        layer_id: str | None = None, volume: bool = False,
    ) -> None:
        """Fill a range with generated inherited points.

        `volume` no longer reuses this machinery -- see _generate_volume. The
        Kiai and Sound Volume layer's Volume tool edits existing timing
        points' own volume in place; it must never insert a green line, which
        is exactly what this path does. This function is SV-only now.
        """
        state = self._states.get(difficulty_path)
        if state is None:
            return
        if volume:
            self._generate_volume(state, difficulty_path, start_ms, end_ms, params)
            return
        ordered = sorted_by_time(state.document.timing_points)
        start_bpm = active_uninherited_at(state.document.timing_points, start_ms).bpm or 1.0

        times = self._sv_generation_times(state, start_ms, end_ms, params, layer_id)
        if not times:
            self.show_toast(tr("MainWindow", "No notes in the selected range to generate SV on."))
            return

        # Progress runs across the points that are actually generated, not
        # across the dragged range. With note placement the last note is
        # usually well before where the drag ended, so normalising by the
        # dragged duration meant the final point never reached the requested
        # final rate -- the one value a user picking "1.0x to 2.5x" cares most
        # about getting exactly.
        span = times[-1] - times[0]
        offset = params.get("position_offset", SVFunctionDialog.DEFAULT_POSITION_OFFSET_MS)
        # A green line already sitting on a generation time, in the regular
        # editor, keeps that time rather than being shoved to the offset
        # position: the offset exists to land a *new* point slightly before
        # the note it governs, and applying it here instead left the old line
        # untouched (its ms didn't match the shifted one _insert_sv_points
        # replaces by) and added a second, offset line right beside it. A
        # gimmick layer's points never sit on the object's own ms to begin
        # with, so its offset always applies.
        existing_times = (
            {
                round(point.time) for point in state.document.timing_points
                if not point.uninherited and start_ms - 0.5 <= point.time <= end_ms + 0.5
            }
            if layer_id is None and not params.get("replace_existing") else set()
        )
        # Oscillation is indexed, not timed: it alternates on every *point*, so
        # unlike a sweep its shape cannot be read off elapsed time. Built up
        # front for the whole run, then indexed alongside it.
        oscillate = params.get("oscillate", "")
        wobble = oscillating_series(
            params["initial_rate"],
            abs(params["final_rate"] - params["initial_rate"]),
            len(times),
            lambda t: sv_ease(
                params["function"], t, params["initial_rate"], params["final_rate"],
            ),
            per_pair=oscillate == "pair",
        ) if oscillate else []
        points = []
        for index, source_time in enumerate(times):
            # Measured from where the point *belongs*, not from where the
            # offset moves it, so a -5ms lead-in doesn't skew the curve.
            t = 0.0 if span <= 0 else (source_time - times[0]) / span
            unoffset_ms = round(source_time)
            time_ms = unoffset_ms if unoffset_ms in existing_times else round(source_time + offset)
            if wobble:
                rate = wobble[index]
            else:
                eased = sv_ease(
                    params["function"], t, params["initial_rate"], params["final_rate"],
                )
                rate = params["initial_rate"] + (params["final_rate"] - params["initial_rate"]) * eased
            if params["relative_to_final_bpm"] and start_bpm:
                # Compensate for a BPM change across the range so the
                # perceived scroll speed matches `rate` regardless of where
                # the local BPM lands, not just the raw multiplier.
                #
                # Scroll distance is SV / beat_length, so it goes as SV * BPM
                # -- to hold it steady as BPM rises, SV has to come *down*.
                # This read `local / start`, which compounded the BPM change
                # instead of cancelling it: at 180 -> 360 BPM a requested 1.5x
                # wrote 3.0 and scrolled four times the intended speed, where
                # TaikoEditor (`SVFunctionTool` lines 488, 510) writes 0.75.
                # The regression test only covered a range with no BPM change
                # in it, where the ratio is 1.0 either way and the direction
                # cannot be seen.
                local_bpm = active_uninherited_at(state.document.timing_points, time_ms).bpm or start_bpm
                if local_bpm:
                    rate = rate * (start_bpm / local_bpm)
            # Kiai belongs to the active point, so a generated sweep that
            # doesn't carry it forward switches kiai off for the whole range
            # it covers. Read per point, not once: a range can cross a kiai
            # boundary.
            active = active_point_at(ordered, source_time)
            points.append(TimingPoint.inherited_at(
                time_ms, max(0.01, rate),
                kiai=bool(active.kiai) if active is not None else False,
                omit_first_barline=(params["omit_barline"] and index == 0),
                # The same point kiai comes from. Sweeping across a volume
                # change used to copy the range's opening volume onto every
                # generated line, silently undoing the change.
                template=active,
            ))

        # One undo step regardless of point count, and regardless of how many
        # existing green lines the sweep replaces.
        command = self._insert_points_command(state.document, points, "generate_sv")
        if params.get("replace_existing"):
            # Every green line in the range goes, not only those a new one
            # lands on (owner, 2026-10-17) -- the range widened by the offset,
            # which is where the new lines go. On a gimmick layer only the
            # layer's own: another structure's line is not this sweep's.
            owned = self._sv_layer_times(layer_id, state.document) if layer_id is not None else None
            low, high = start_ms + min(offset, 0) - 0.5, end_ms + max(offset, 0) + 0.5
            landing = {round(point.time) for point in points}
            old = [
                point for point in state.document.timing_points
                if not point.uninherited and low <= point.time <= high
                and round(point.time) not in landing  # _insert_points_command's own
                and (owned is None or round(point.time) in owned)
            ]
            if old:
                command = CompositeCommand([RemoveTimingPoints(old), command], "generate_sv")
        state.history.push(command, state)
        self._refresh_difficulty_sv_views(difficulty_path)

    def _generate_volume(
        self, state: DifficultyState, difficulty_path: Path, start_ms: float, end_ms: float,
        params: dict,
    ) -> None:
        """The Kiai and Sound Volume layer's Volume tool.

        Edits the volume already on every existing timing point (red and
        green alike) inside the range -- it never inserts one. Position
        offset, "omit first barline", "relative to final BPM" and kiai
        preservation all describe a point being *created*, so none of them
        apply here; SVFunctionDialog hides those rows in volume mode.
        """
        ordered = sorted_by_time(state.document.timing_points)
        targets = [
            point for point in ordered
            if round(start_ms) <= round(point.time) <= round(end_ms)
        ]
        if not targets:
            self.show_toast(tr("MainWindow", "No timing point in the selected range."))
            return

        times = [point.time for point in targets]
        span = times[-1] - times[0]
        oscillate = params.get("oscillate", "")
        wobble = oscillating_series(
            params["initial_rate"],
            abs(params["final_rate"] - params["initial_rate"]),
            len(times),
            lambda t: sv_ease(
                params["function"], t, params["initial_rate"], params["final_rate"],
            ),
            per_pair=oscillate == "pair",
        ) if oscillate else []

        commands = []
        for index, point in enumerate(targets):
            t = 0.0 if span <= 0 else (point.time - times[0]) / span
            if wobble:
                rate = wobble[index]
            else:
                eased = sv_ease(
                    params["function"], t, params["initial_rate"], params["final_rate"],
                )
                rate = params["initial_rate"] + (params["final_rate"] - params["initial_rate"]) * eased
            new_volume = max(0, min(100, round(rate)))
            if new_volume == point.volume:
                continue
            commands.append(EditTimingPoint(point.uid, {"volume": (point.volume, new_volume)}))

        if not commands:
            return
        state.history.push(CompositeCommand(commands, "generate_volume"), state)
        self._refresh_difficulty_sv_views(difficulty_path)

    def _refresh_difficulty_sv_views(self, difficulty_path: Path) -> None:
        """Re-read timing points into every open SV view of this difficulty after an edit."""
        with self._refresh_cycle():
            state = self._states.get(difficulty_path)
            if state is None:
                return
            for frame in self._editor_views:
                if getattr(frame, "difficulty_path", None) != difficulty_path:
                    continue
                sv_view = getattr(frame, "sv_view", None)
                if sv_view is not None:
                    sv_view.refresh_points(state.document)
                # An SV or BPM edit changes where the gameplay viewer puts every
                # note after it -- that is the point of the view -- so it belongs on
                # the timing-point refresh path as well as the note one.
                gameplay_view = getattr(frame, "gameplay_view", None)
                if gameplay_view is not None:
                    gameplay_view.refresh_points(state.document)
            # The gimmick layers again -- see _refresh_difficulty_views. A timing
            # edit is the one they care about most: red lines *are* the barline
            # layer's content, and the SV layers are nothing else.
            pairing = self._gimmick_pairing
            if pairing is not None and pairing.target == difficulty_path:
                self._refresh_gimmick_views()
            # Two different difficulties now: what is heard and what the bars
            # describe. A volume edit changes no note, so the note-refresh path
            # never runs -- but it is exactly the edit that has to become
            # audible at once, since hearing it is how you tell you have set it
            # right.
            if self._hitsound_state() is state:
                self._reschedule_hitsounds(state)
            if self.state is state:
                self._reload_timing_bars(state)

    def _close_editor_view(self, frame: EditorViewFrame) -> None:
        if frame in self._editor_views:
            self._editor_views.remove(frame)
        chart_view = getattr(frame, "chart_view", None)
        if chart_view is not None and chart_view in self._chart_views:
            self._chart_views.remove(chart_view)
        if chart_view is not None and chart_view in self._editor_snap_views:
            self._editor_snap_views.remove(chart_view)
        if chart_view is not None and chart_view is self._active_chart_view:
            self._active_chart_view = None
            self._tool_state.sync_global_tool_row()
        sv_view = getattr(frame, "sv_view", None)
        if sv_view is not None and sv_view in self._sv_views:
            self._sv_views.remove(sv_view)
        if sv_view is not None and sv_view in self._editor_snap_views:
            self._editor_snap_views.remove(sv_view)
        if sv_view is not None and sv_view is self._active_sv_view:
            self._active_sv_view = None
            self._tool_state.sync_global_sv_tool_row()
        if self._last_focused_editor_view in (chart_view, sv_view):
            self._last_focused_editor_view = None
        gameplay_view = getattr(frame, "gameplay_view", None)
        if gameplay_view is not None and gameplay_view in self._gameplay_views:
            self._gameplay_views.remove(gameplay_view)
        if gameplay_view is not None and gameplay_view in self._editor_snap_views:
            self._editor_snap_views.remove(gameplay_view)
        density_view = getattr(frame, "density_view", None)
        if density_view is not None and density_view in self._density_views:
            self._density_views.remove(density_view)

        difficulty_path = getattr(frame, "difficulty_path", None)
        # A gimmick layer lives in the gimmick page's own stack, not in an
        # Editor-page difficulty group -- and the two can name the same
        # difficulty, so looking one up by path would prune a group this frame
        # was never in.
        group_layout = (
            None if getattr(frame, "gimmick_layer", None) is not None
            else self._editor_view_groups.get(difficulty_path) if difficulty_path is not None
            else None
        )
        if group_layout is not None:
            group_layout.removeWidget(frame)
        frame.setParent(None)
        frame.deleteLater()
        if group_layout is not None:
            self._prune_empty_difficulty_group(difficulty_path, group_layout)

    def _prune_empty_difficulty_group(self, difficulty_path: Path, group_layout: QVBoxLayout) -> None:
        """Remove a difficulty's group (header included) once its last view closes."""
        still_has_views = any(
            isinstance(group_layout.itemAt(i).widget(), EditorViewFrame)
            for i in range(group_layout.count())
        )
        if still_has_views:
            return
        group_widget = group_layout.parentWidget()
        self.editor_views_layout.removeWidget(group_widget)
        group_widget.setParent(None)
        group_widget.deleteLater()
        del self._editor_view_groups[difficulty_path]

    def _snap_changed(self) -> None:
        divisor = int(self.snap_combo.currentData())
        self.timeline.set_snap_divisor(divisor)
        self._update_timeline_info()

    def _snap_changed_by_wheel(self,divisor:int)->None:
        index=self.snap_combo.findData(divisor)
        if index>=0:
            self.snap_combo.blockSignals(True); self.snap_combo.setCurrentIndex(index); self.snap_combo.blockSignals(False)
        self.timeline.snap_divisor=divisor; self._update_timeline_info()

    def _update_equation_control_visibility(self, group: str) -> None:
        controls = self.controls.get(group, {})
        mode_control = controls.get("equation_mode")
        if mode_control is None:
            return
        mode = mode_control.value()
        for key, control in controls.items():
            label = control.parentWidget()
            visible = True
            if key in {"x_expression", "y_expression", "t_min", "t_max"}:
                visible = mode == "parametric"
            elif key == "equation":
                visible = mode != "parametric"
            elif key in {"y_min", "y_max"}:
                visible = mode != "parametric"
            control.setVisible(visible)

    def _insert_equation_token(self, group: str, token: str) -> None:
        control = self.controls.get(group, {}).get("equation")
        if not control or not hasattr(control, "text_input"):
            return
        line = control.text_input
        line.insert(token)

    def _create_equation_keyboard(self, group: str) -> QWidget:
        keyboard = QWidget()
        grid = QVBoxLayout(keyboard)
        grid.setContentsMargins(0, 4, 0, 4)
        rows = [
            ["x", "y", "t", "=", "(", ")", "{", "}"],
            ["+", "-", "*", "/", "^", "abs(", "sqrt("],
            ["sin(", "cos(", "tan(", "asin(", "acos(", "atan("],
            ["pi", "e", "exp(", "ln(", "log(", "floor(", "ceil("],
        ]
        for tokens in rows:
            row = QHBoxLayout()
            for token in tokens:
                button = QPushButton(token)
                button.setFocusPolicy(Qt.NoFocus)
                button.clicked.connect(
                    lambda checked=False, active=group, value=token:
                    self._insert_equation_token(active, value)
                )
                row.addWidget(button)
            grid.addLayout(row)
        keyboard.setVisible(False)
        return keyboard

    def _apply_selected_toggle_style(self) -> None:
        if getattr(self, "_selected_toggle_style_applied", False):
            return
        self._selected_toggle_style_applied = True
        # Appended to the sheet as written, not to styleSheet(): that is the
        # active theme's translation, and kept as the source it pinned the
        # window's navy to whichever theme the app started in.
        self.setStyleSheet(
            (self.property(theme.SOURCE_PROPERTY) or self.styleSheet())
            + """
            QPushButton:checked {
                background-color: #ff66aa;
                color: #ffffff;
                border: 1px solid #ff9dcc;
                font-weight: 700;
            }
            """
        )

    def _follow_screen(self, screen=None) -> None:
        """Pace frames to the screen the window is on, and keep doing so when
        it is dragged to another one (a 60Hz laptop panel beside a 144Hz
        monitor is the usual pair)."""
        handle = self.windowHandle()
        if handle is not None and not getattr(self, "_screen_followed", False):
            handle.screenChanged.connect(self._follow_screen)
            self._screen_followed = True
        self.gameplay_frame_interval_ns = display_frame_interval_ns(screen or self.screen())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._follow_screen()
        self._apply_selected_toggle_style()
        # Checked whenever one plays, so it is sized for its bold label.
        for button, _actions in getattr(self, "_autoplay_menus", ()):
            fit_button_width(button)
        # One size for every tool button, across both rows: they swap in and out
        # of the same spot, so per-label widths made the row re-flow on every
        # switch. Here rather than at build time because the toggle style above
        # is the last thing to change how wide a label wants to be -- and
        # measured rather than hard-coded, or the longest translation is exactly
        # the one that clips.
        # heights=True: TOOL_BUTTON_HEIGHT is a floor, and the polished hint is
        # what a label actually needs -- typed at 32 the descenders were clipped.
        equalize_button_widths(
            [*self.tool_buttons.values(), self.new_combo_button, *self.sv_tool_buttons.values()],
            heights=True,
        )
        # The gimmick rows are sized as their own set rather than folded into
        # the call above: "2. Fake Slider" is the widest label in the program,
        # and sharing one width would stretch the Editor page's row to match it.
        # Heights only -- see equalize_button_widths on why this row cannot
        # afford a uniform width any more.
        equalize_button_widths(self._gimmick_row_buttons, heights=True, widths=False)
        # Resnap is an action beside the tools, not one of them: its own width,
        # measured after the stylesheet the same as theirs.
        equalize_button_widths([self.resnap_button], heights=True, widths=False)
        # Same reasoning for the two playback rows, and the same reason it
        # cannot happen at build time: the window's stylesheet (padding
        # included) is applied after the pages are built, so a button measured
        # there reports a width its padded self does not fit in.
        equalize_button_widths([self.editor_play_button, *self.editor_playback_speed_buttons])
        equalize_button_widths([self.timeline_play_button, *self.playback_speed_buttons])
        # Three playback rows, not two. The gimmick page's was left out of this
        # and so kept its build-time hint: unpadded, and measured before the
        # checked rule could make "100%" heavier -- five pixels short of its own
        # label, on the page this program is mostly used on.
        equalize_button_widths([self.gimmick_play_button, *self.gimmick_playback_speed_buttons])
        # Page tabs: each pinned to what its label costs *checked* (bold), per
        # button so the four keep their ragged widths. Measured here, under
        # the segment's sheet -- at build time the frame's padding had not
        # applied, and the selected page clipped the last letter of its name.
        # Timing Reference... too: a permanent status bar widget is squeezed
        # to whatever the message leaves it unless it has a width of its own.
        for button in (*self.page_button_group.buttons(), self.gimmick_reference_button):
            button.ensurePolished()
            fit_button_width(button)

    def _snapshot_transform_group(self, group: str):
        ref = self._transform_page_refs[group]
        combo = ref["combo"]
        values = {
            key: control.value()
            for key, control in self.controls.get(group, {}).items()
        }
        return {
            # Store the actual index as the primary identity. currentData() may
            # be empty or duplicated in older combo configurations, which made
            # both sides eventually fall back to the same transformation.
            "index": combo.currentIndex(),
            "text": combo.currentText(),
            "data": combo.currentData(),
            "values": values,
            "offset": tuple(self.preview_offsets.get(group, [0.0, 0.0])),
        }

    def _restore_transform_group(self, group: str, snapshot) -> None:
        ref = self._transform_page_refs[group]
        combo = ref["combo"]

        target_index = int(snapshot["index"])
        if not 0 <= target_index < combo.count():
            target_index = combo.findText(
                str(snapshot["text"]),
                Qt.MatchExactly,
            )
        if target_index < 0 and snapshot["data"] is not None:
            target_index = combo.findData(snapshot["data"])
        if target_index < 0:
            raise RuntimeError(
                f"Could not restore {group} transformation: "
                f"{snapshot['text']}"
            )

        combo.blockSignals(True)
        combo.setCurrentIndex(target_index)
        combo.blockSignals(False)

        # Rebuild this side exactly once. The copied snapshot remains immutable
        # while Don and Kat are restored, so neither side can overwrite the
        # other side's source state during repeated swaps.
        ref["rebuild"]()

        for key, value in snapshot["values"].items():
            control = self.controls.get(group, {}).get(key)
            if control is not None:
                control.set_value(value)

        offset_x, offset_y = snapshot["offset"]
        self.preview_offsets[group] = [float(offset_x), float(offset_y)]
        self._sync_position_controls(group)

    def _swap_don_kat_transformations(self) -> None:
        if not self._is_split_mode():
            return
        if not all(
            group in getattr(self, "_transform_page_refs", {})
            for group in ("don", "kat")
        ):
            return

        # Capture both complete states before changing either UI page.
        snapshots = {
            "don": self._snapshot_transform_group("don"),
            "kat": self._snapshot_transform_group("kat"),
        }

        self.preview_timer.stop()
        self._restore_transform_group("don", snapshots["kat"])
        self._restore_transform_group("kat", snapshots["don"])
        self.preview_cache.clear()
        self.schedule_preview()
        self.status.setText(tr("MainWindow", "Swapped Don and Kat transformations."))

    def _control_page(self, group: str) -> QWidget:
        """One group's shape and parameters (the mockup's Transform dock body).

        The shape is picked from tiles with a line drawing each: two dozen
        transformations are recognised faster than they are read in a list.
        The combo stays, hidden, as the model `_spec` and the tests read.
        """
        page = QWidget()
        page.setStyleSheet(TILE_GRID_STYLE)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        combo = QComboBox(page)
        combo.hide()
        combo.addItem(tr("MainWindow", "None"), "")
        for name in GUI_TRANSFORMATIONS: combo.addItem(display_name(name), name)

        shape_card = inspector_card(layout, tr("MainWindow", "Shape"))
        # Tight sides: every pixel of the card's padding comes off four tiles,
        # and at 12px "Vertical Taiko" and "Random Walk" were elided.
        shape_card.setContentsMargins(4, 10, 4, 12)
        tiles = QWidget()
        grid = QGridLayout(tiles)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        tile_group = QButtonGroup(page)
        tile_group.setExclusive(True)
        columns = 4
        for index in range(combo.count()):
            tile = QToolButton()
            tile.setObjectName("shapeTile")
            tile.setCheckable(True)
            tile.setFocusPolicy(Qt.NoFocus)
            tile.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            tile.setProperty("shapeName", str(combo.itemData(index) or "none"))
            tile.setIcon(shape_icon(tile.property("shapeName")))
            tile.setIconSize(QSize(32, 22))
            tile.setText(combo.itemText(index))
            tile.setToolTip(combo.itemText(index))
            tile.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            tile.setMinimumWidth(0)
            tile_group.addButton(tile, index)
            grid.addWidget(tile, index // columns, index % columns)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        TileLabelFitter(tile_group.buttons())
        tile_group.idClicked.connect(combo.setCurrentIndex)
        combo.currentIndexChanged.connect(lambda index: tile_group.button(index).setChecked(True))
        tile_group.button(0).setChecked(True)
        shape_card.addWidget(tiles)
        shape_note = QLabel(shape_description(""))
        shape_note.setObjectName("shapeNote")
        shape_note.setWordWrap(True)
        shape_note.setContentsMargins(6, 0, 6, 0)
        combo.currentIndexChanged.connect(lambda _index: shape_note.setText(shape_description(str(combo.currentData() or ""))))
        shape_card.addWidget(shape_note)

        parameter_card = inspector_card(layout, tr("MainWindow", "Parameters"))
        form_widget = QWidget()
        form = QVBoxLayout(form_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(12)
        parameter_card.addWidget(form_widget)
        equation_keyboard_button = QPushButton("⌨")
        equation_keyboard_button.setToolTip(tr("MainWindow", "Show or hide equation keyboard"))
        equation_keyboard_button.setCheckable(True)
        equation_keyboard_button.setProperty("role", "ghost")
        equation_keyboard_button.setStyleSheet("QPushButton { border: 1px solid #3a4554; }")
        # No typed width. 38px was narrower than the window stylesheet's own
        # 16px of padding either side left room for, so the button covered its
        # keyboard glyph; AlignLeft below already keeps it to its own hint,
        # which is recomputed once that stylesheet lands.
        equation_keyboard_button.setVisible(False)
        equation_keyboard = self._create_equation_keyboard(group)
        equation_keyboard_button.toggled.connect(equation_keyboard.setVisible)
        layout.addWidget(equation_keyboard_button, 0, Qt.AlignLeft)
        layout.addWidget(equation_keyboard)
        layout.addStretch(1)

        self.controls[group] = {}

        def rebuild() -> None:
            while form.count():
                item = form.takeAt(0)
                if item.widget() is not None:
                    # Hidden now: deleteLater waits for the event loop, and
                    # until then the old shape's rows drew over the new ones.
                    item.widget().hide()
                    item.widget().deleteLater()

            self.controls[group] = {}

            transformation_name = combo.currentData()
            equation_mode = transformation_name == "equation"
            equation_keyboard_button.setVisible(equation_mode)
            if not equation_mode:
                equation_keyboard_button.setChecked(False)

            for definition in PARAMETERS.get(transformation_name, []):
                control = ParameterControl(definition, tr("Parameters", str(definition["label"])))
                control.changed.connect(self.schedule_preview)
                if definition["key"] == "equation_mode":
                    control.changed.connect(lambda active_group=group: self._update_equation_control_visibility(active_group))

                self.controls[group][definition["key"]] = control
                form.addWidget(control)

            if transformation_name=="drawn_path":
                draw_button=QPushButton(tr("MainWindow", "Open Drawing Window"))
                def open_drawing(active_group=group):
                    self.drawing_dialog_active=True
                    try:
                        dialog=DrawingDialog(self)
                        if dialog.exec()==QDialog.Accepted:
                            points=list(dialog.accepted_points)
                            self.last_drawing_points=points
                            self.drawing_points[active_group]=points
                            # Share the strokes with every other group that is also
                            # set to Drawing, so Split Don/Kat previews both halves.
                            for group_name,refs in self._transform_page_refs.items():
                                if str(refs["combo"].currentData() or "")=="drawn_path":self.drawing_points[group_name]=points
                            self.preview_cache.clear();self.schedule_preview()
                    finally:self.drawing_dialog_active=False
                draw_button.setProperty("role","ghost");draw_button.setStyleSheet("QPushButton { border: 1px solid #3a4554; }")
                draw_button.clicked.connect(open_drawing);form.addWidget(draw_button)
            self.position_controls[group] = {}
            for axis, label, minimum, maximum in (
                ("x", "Position X", -512, 512),
                ("y", "Position Y", -384, 384),
            ):
                position_control = ParameterControl(
                    {
                        "key": f"position_{axis}",
                        "label": label,
                        "type": "int",
                        "min": minimum,
                        "max": maximum,
                        "default": round(self.preview_offsets[group][0 if axis == "x" else 1]),
                    },
                    tr("Parameters", label),
                )
                position_control.changed.connect(
                    lambda active_group=group: self._position_slider_changed(active_group)
                )
                self.position_controls[group][axis] = position_control
                form.addWidget(position_control)

            self.schedule_preview()

        combo.currentTextChanged.connect(rebuild)
        page.combo = combo
        rebuild()

        if not hasattr(self, "_transform_page_refs"):
            self._transform_page_refs = {}
        self._transform_page_refs[group] = {
            "page": page,
            "combo": combo,
            "rebuild": rebuild,
        }
        return page

    def _transformation_mode(self) -> str:
        """Return the stable mode identifier, "all" or "split".

        Reads combo item data rather than the visible label so that mode logic
        is unaffected by localization.
        """
        return str(self.mode_combo.currentData() or "all")

    def _is_split_mode(self) -> bool:
        return self._transformation_mode() == "split"

    def _rebuild_control_tabs(self) -> None:
        self.control_tabs.clear()

        if not self._is_split_mode():
            self.control_tabs.addTab(
                self._control_page("all"),
                tr("MainWindow", "All"),
            )
        else:
            self.control_tabs.addTab(
                self._control_page("don"),
                tr("MainWindow", "Don"),
            )
            self.control_tabs.addTab(
                self._control_page("kat"),
                tr("MainWindow", "Kat"),
            )

        self.schedule_preview()

    def _spec(
        self,
        tab_index: int,
        group: str,
    ) -> tuple[str, dict[str, Any]]:
        page = self.control_tabs.widget(tab_index)

        name=str(page.combo.currentData() or "");params={key:control.value() for key,control in self.controls[group].items()}
        if name=="drawn_path":params["points"]=list(self.drawing_points.get(group) or self.last_drawing_points)
        return name,params

    def _ensure_state(self, source_path: Path) -> DifficultyState:
        """Return the DifficultyState for source_path, parsing it once.

        A difficulty that has already been visited this session keeps its
        edits, selection and undo history in self._states rather than being
        re-parsed from disk and losing them.
        """
        cached = self._states.get(source_path)
        if cached is not None:
            return cached

        document = parse_osu(source_path)
        audio_path = resolve_song_asset(
            source_path.parent,
            document.audio_filename,
            {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".opus"},
        )
        if not audio_path.is_file():
            raise FileNotFoundError(f"Audio file not found: {audio_path.name}")

        background_name = extract_background_filename(document)
        background_path = None
        if background_name:
            try:
                candidate = resolve_song_asset(source_path.parent, background_name, {".jpg", ".jpeg", ".png", ".webp", ".bmp"})
                background_path = candidate if candidate.is_file() else None
            except ValueError:
                background_path = None

        state = DifficultyState.from_document(
            document,
            source_path,
            audio_path=audio_path,
            background_path=background_path,
        )
        # Matches the cursor TimelineGameplay.load_document starts at, so
        # activating a freshly-parsed state doesn't jump the audio to 0:00.
        # A difficulty with no notes yet is the file a mapper is about to
        # map into, so it opens at 0 rather than being refused.
        state.playhead_ms = float(
            document.hit_objects[0].time if document.hit_objects else 0.0
        )
        self._states[source_path] = state
        return state

    def _activate_state(self, state: DifficultyState) -> None:
        """Make `state` the active difficulty.

        Only touches what actually changed: the audio source is left alone
        when the new difficulty shares the previous one's audio file, which
        is the common case within one song folder, so playback is not reset
        to 0:00 on every switch.
        """
        previous = self.state
        if previous is not None:
            previous.playhead_ms = self.timeline.current_time

        self.state = state
        self.setWindowTitle(f"{state.source_path.name} - {self.app_name}")

        if state.audio_path is not None and (previous is None or previous.audio_path != state.audio_path):
            self.player.setSource(QUrl.fromLocalFile(str(state.audio_path)))

        self.timeline.load_document(state.document)
        # Not `set_schedule` directly: a pinned playback difficulty outlives a
        # change of active difficulty. See `_hitsound_state`.
        self._apply_hitsound_source()
        self.timeline.set_snap_divisor(int(self.snap_combo.currentData()))
        self._reload_timing_bars(state)
        # Fancy Arranger's own density chart always reflects the active
        # difficulty (unlike Editor-page density views, which are
        # per-chosen-difficulty and don't reload on activation).
        self.fancy_density.load_document(state.document, state.duration_hint)

        self.canvas.set_background(str(state.background_path) if state.background_path else None)
        self._sync_backdrops()
        # The box is what Ctrl+S / Export write, so left at its 7.0 default it
        # overwrote every map's own CircleSize. Only the canvas listens to
        # `changed`, so this redraws and marks nothing dirty.
        self.circle_size_control.set_value(difficulty_setting(state.document, "CircleSize", 5.0))

        for button in (
            self.play_button, self.apply_button, self.reset_button, self.export_button,
            self.undo_button, self.redo_button, self.save_all_button, self.export_new_difficulty_button,
        ):
            button.setEnabled(True)
        self.refresh_canvas()
        self.seek_audio(state.playhead_ms)

        self._ensure_default_editor_views(state)

        self.status.setText(f"{state.source_path.name} | drag notes, preview, then Apply to selection")
        self._show_chart_identity(state)

    def _show_chart_identity(self, state: DifficultyState) -> None:
        """The header's "which chart": difficulty, then artist - title and mapper."""
        header = read_header(state.source_path) or {}
        self.chart_version_label.setText(state.document.version or state.source_path.stem)
        title = " - ".join(part for part in (header.get("artist"), header.get("title")) if part)
        creator = header.get("creator")
        mapper = tr("MainWindow", "mapped by {name}").format(name=creator) if creator else ""
        self.chart_title_label.setText("  ·  ".join(part for part in (title, mapper) if part))

    def _ensure_default_editor_views(self, state: DifficultyState) -> None:
        """Give `state` the views it had this session, or a chart + SV pair.

        Covers both "first time this difficulty is activated" and "views
        were torn down by leaving the editor, then the Editor tab was
        clicked directly" -- the latter doesn't go through _activate_state,
        so it needs the same check on entry to PAGE_EDITOR (see
        _switch_page). Once the user closes them, _editor_view_groups drops
        the entry and they come back next time either path runs -- treated
        as "not set up yet" rather than "user doesn't want any views."
        """
        if state.source_path in self._editor_view_groups:
            return
        remembered = self._session_views.get(state.source_path)
        # Only onto an empty page: switching difficulty with views already
        # open adds that difficulty's pair, as it always did.
        if remembered and not self._editor_view_groups:
            for view_type, path, locked in remembered:
                if not path.is_file():
                    continue  # deleted since; nothing to show
                count = len(self._editor_views)
                self._add_editor_view(view_type, path)
                if locked and len(self._editor_views) > count:
                    self._editor_views[-1].lock_button.setChecked(True)
            if state.source_path in self._editor_view_groups:
                return
        self._add_editor_view("chart", state.source_path)
        self._add_editor_view("sv", state.source_path)
        # Opened from song select, the pair comes with a gameplay preview
        # under it (owner, 2026-10-09); another difficulty opened from inside
        # the editor gets the pair alone.
        if self._gameplay_with_defaults:
            self._add_editor_view("gameplay", state.source_path)

    def open_map(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, tr("MainWindow", "Open osu! beatmap"), "", tr("MainWindow", "osu! beatmaps (*.osu)")
        )
        if filename:
            self._load_map_path(Path(filename).resolve(), refresh_difficulties=True)

    def _load_map_path(self, source_path: Path, refresh_difficulties: bool = False) -> None:
        try:
            state = self._ensure_state(source_path)
        except Exception as error:
            QMessageBox.critical(self, tr("MainWindow", "Open failed"), str(error))
            return

        # A different difficulty picked on song select starts without mods
        # (owner, 2026-10-17); coming back to the same one keeps them.
        previous = getattr(self, "state", None)
        if (self.page_stack.currentIndex() == PAGE_LIBRARY and previous is not None
                and previous.source_path != state.source_path and self._mods):
            self._set_mods(())

        self._activate_state(state)

        # Opening a map from the song list (or the header's Open .osu) means
        # you want to edit it, so follow it to the Editor. Switching
        # difficulty while already on a working page leaves you there.
        if self.page_stack.currentIndex() == PAGE_LIBRARY:
            self._show_page(PAGE_EDITOR)

        if refresh_difficulties:
            self.song_difficulties = sorted(source_path.parent.glob("*.osu"), key=lambda item: item.name.lower())
            self.difficulty_combo.blockSignals(True)
            self.difficulty_combo.clear()
            for difficulty_path in self.song_difficulties:
                try:
                    label = read_version(difficulty_path) or difficulty_path.stem
                except Exception:
                    label = difficulty_path.stem
                self.difficulty_combo.addItem(label, str(difficulty_path))
            selected_index = next((i for i, item in enumerate(self.song_difficulties) if item.resolve() == source_path), 0)
            self.difficulty_combo.setCurrentIndex(selected_index)
            self.difficulty_combo.setEnabled(bool(self.song_difficulties))
            self.difficulty_combo.blockSignals(False)

    def _difficulty_changed(self, index: int) -> None:
        if index < 0:
            return
        value = self.difficulty_combo.itemData(index)
        if value:
            candidate = Path(value).resolve()
            if candidate != self.source_path:
                self._load_map_path(candidate, refresh_difficulties=False)

    def _background_dropped(self, dropped_path: str) -> None:
        if self.document is None or self.source_path is None:
            QMessageBox.information(self, tr("MainWindow", "Open a map first"), tr("MainWindow", "Open a beatmap before adding a background."))
            return
        source = Path(dropped_path).resolve()
        destination = self.source_path.parent / source.name
        if source != destination:
            if destination.exists():
                stem, suffix = source.stem, source.suffix
                number = 2
                while destination.exists():
                    destination = self.source_path.parent / f"{stem}_{number}{suffix}"
                    number += 1
            try:
                shutil.copy2(source, destination)
            except Exception as error:
                QMessageBox.critical(self, tr("MainWindow", "Background copy failed"), str(error))
                return
        set_document_background(self.document, destination.name)
        self.current_background_path = destination
        # Neither line above goes through a Command, so history.dirty stayed
        # False and Ctrl+S ("Nothing to save.") silently skipped writing the
        # new reference -- this was the only way a background actually
        # reached disk before Export or Apply to Original.
        self.state.history.touch()
        self.canvas.set_background(str(destination))
        self._sync_backdrops()
        self.status.setText(f"Background set to {destination.name}. Ctrl+S (or Export/Apply to Original) writes it to the .osu.")

    def _selection_changed(self, selected) -> None:
        new_selection = set(selected)
        if new_selection != self.selected:
            self.preview_offsets = {"all": [0.0, 0.0], "don": [0.0, 0.0], "kat": [0.0, 0.0]}
        self.selected=new_selection; self.preview_timer.stop(); self.preview_positions=dict(self.applied_positions); self.refresh_canvas()
        self._update_apply_label()
    def _update_apply_label(self) -> None:
        """Says what the click will do: how many notes it transforms."""
        count = len(self.selected)
        self.apply_button.setText(
            tr("MainWindow", "Transform {count} notes").format(count=count) if count
            else tr("MainWindow", "Transform Selected Notes"))
    def _selection_finalized(self, selected) -> None:
        self.selected=set(selected)
        if any(self._spec(i, group)[0] for i,group in enumerate(("all",) if not self._is_split_mode() else ("don","kat"))): self.schedule_preview()

    def schedule_preview(self) -> None:
        if self.document is not None:
            self.preview_timer.start()

    def _calculate_selected_transform(
        self,
    ) -> dict[int, tuple[int, int]]:
        # Time order, not key order (R11). sorted(self.selected) was
        # time-ordered only by the accident that original_index followed file
        # order -- which stopped being true once notes could be inserted and
        # pasted, since those get the next free key regardless of when they
        # sit. Every chunked transformation depends on this ordering.
        indexes = self.state.notes_in_time_order() if self.state is not None else []

        if not indexes:
            return {}

        if not self._is_split_mode():
            transformation_name, params = self._spec(
                0,
                "all",
            )

            if not transformation_name: return {}
            self._undo_center_drag(params, "all")
            if transformation_name in {"taiko","vertical_taiko"}:
                params.update({"note_times":{n.original_index:n.time for n in self.document.hit_objects},"timing_points":self.document.timing_points,"timing_mode":"filtered","anchor_mode":"selection_start"})
            return transform(transformation_name,indexes,params)

        note_by_index = {
            note.original_index: note
            for note in self.document.hit_objects
        }

        don_indexes = [
            index
            for index in indexes
            if not note_by_index[index].is_kat
        ]

        kat_indexes = [
            index
            for index in indexes
            if note_by_index[index].is_kat
        ]

        don_name, don_params = self._spec(0,"don"); kat_name, kat_params = self._spec(1,"kat")
        self._undo_center_drag(don_params, "don"); self._undo_center_drag(kat_params, "kat")
        for name,params in ((don_name,don_params),(kat_name,kat_params)):
            if name in {"taiko","vertical_taiko"}: params.update({"note_times":{n.original_index:n.time for n in self.document.hit_objects},"timing_points":self.document.timing_points,"timing_mode":"filtered","anchor_mode":"selection_start"})

        groups={}
        if don_name: groups["don"]={"transformation_name":don_name,"selected_note_indexes":don_indexes,"params":don_params}
        if kat_name: groups["kat"]={"transformation_name":kat_name,"selected_note_indexes":kat_indexes,"params":kat_params}
        return transform_groups(groups)


    def _undo_center_drag(self, params: dict, offset_key: str) -> None:
        """Compute the shape from where its centre was before the drag.

        A canvas drag moves the notes by `preview_offsets` *and* nudges Center
        X/Y along with them so the boxes read true (see
        `_sync_center_controls_after_drag`). Both then reached the result: the
        shape was rebuilt around the moved centre and the offset added on top,
        so Apply committed a centred transformation twice as far as the canvas
        had shown it. The offset is the translation; the centre is its label.

        ponytail: a centre clamped at the playfield edge moved less than the
        offset, so undoing the full offset there overshoots by the clamped part.
        """
        dx, dy = self.preview_offsets[offset_key]
        if "center_x" in params: params["center_x"] = float(params["center_x"]) - dx
        if "center_y" in params: params["center_y"] = float(params["center_y"]) - dy

    def _indices_for_drag_group(self, clicked_group: str) -> set[int]:
        if not self._is_split_mode():
            return set(self.selected)
        return {
            note.original_index
            for note in self.document.hit_objects
            if note.original_index in self.selected
            and ((clicked_group == "kat" and note.is_kat) or (clicked_group == "don" and not note.is_kat))
        }

    def _apply_preview_offsets(self, positions: dict[int, tuple[int, int]]) -> dict[int, tuple[int, int]]:
        """The Position X/Y offset on top of each group's shape, kept on the
        osu! editing canvas.

        The offset is clamped when the slider or a canvas drag sets it, but
        against the shape *of that moment*; it is then stored and re-added to
        whatever shape later parameters build. A circle offset right and then
        given Radius 250 committed a note at x=552 -- off the playfield the
        editor can show or reach. So the translation is fitted here, where
        preview and Apply both pass, and a shape bigger than the playfield
        is clamped point by point as the last word.

        ponytail: an oversized shape is clamped flat against the edge, not
        scaled to fit; scale it in the transformer if that ever matters.
        """
        output = dict(positions)
        note_by_index = {note.original_index: note for note in self.document.hit_objects}
        groups: dict[str, list[int]] = {}
        for index in self.selected:
            if index in output:
                group = "kat" if note_by_index[index].is_kat else "don"
                groups.setdefault("all" if not self._is_split_mode() else group, []).append(index)
        for offset_key, indexes in groups.items():
            dx, dy = self.preview_offsets[offset_key]
            xs = [output[index][0] for index in indexes]
            ys = [output[index][1] for index in indexes]
            dx = max(-min(xs), min(PLAYFIELD_WIDTH - max(xs), dx))
            dy = max(-min(ys), min(PLAYFIELD_HEIGHT - max(ys), dy))
            for index in indexes:
                x, y = output[index]
                output[index] = (
                    min(max(round(x + dx), 0), round(PLAYFIELD_WIDTH)),
                    min(max(round(y + dy), 0), round(PLAYFIELD_HEIGHT)),
                )
        return output

    def _position_slider_changed(self, group: str) -> None:
        controls = self.position_controls.get(group)
        if not controls:
            return
        new_x = float(controls["x"].value())
        new_y = float(controls["y"].value())
        old_x, old_y = self.preview_offsets[group]
        requested_dx = new_x - old_x
        requested_dy = new_y - old_y
        clicked_group = group
        if group == "all":
            # All Notes uses either note kind only as a drag anchor. The group
            # selection method returns the whole selection in this mode.
            clicked_group = "don"
        self._canvas_dragged(
            clicked_group,
            requested_dx,
            requested_dy,
            sync_controls=False,
        )

    def _sync_position_controls(self, group: str) -> None:
        controls = self.position_controls.get(group)
        if not controls:
            return
        x, y = self.preview_offsets[group]
        controls["x"].set_value(round(x))
        controls["y"].set_value(round(y))

    def _sync_center_controls_after_drag(self, offset_key: str, dx: float, dy: float) -> None:
        # Position X/Y are always synchronized by the existing drag code.
        # Center X/Y are transformation parameters and should visually track
        # the same translation without triggering a transformation rebuild.
        groups = ("all",) if not self._is_split_mode() else (offset_key,)
        for group in groups:
            controls = self.controls.get(group, {})
            center_x = controls.get("center_x")
            center_y = controls.get("center_y")
            if center_x is not None:
                center_x.set_value(max(0, min(PLAYFIELD_WIDTH, float(center_x.value()) + dx)))
            if center_y is not None:
                center_y.set_value(max(0, min(PLAYFIELD_HEIGHT, float(center_y.value()) + dy)))

    def _canvas_circle_size_changed(self, value: float) -> None:
        self.canvas.circle_size = float(value)
        self.canvas.update()

    def _canvas_dragged(self, clicked_group: str, requested_dx: float, requested_dy: float, sync_controls: bool = True) -> None:
        if self.document is None or not self.selected:
            return
        target_indices = self._indices_for_drag_group(clicked_group)
        if not target_indices:
            return
        offset_key = "all" if not self._is_split_mode() else clicked_group
        current_positions = {
            index: self.preview_positions.get(index, self.applied_positions[index])
            for index in target_indices
        }
        min_x = min(x for x, _ in current_positions.values())
        max_x = max(x for x, _ in current_positions.values())
        min_y = min(y for _, y in current_positions.values())
        max_y = max(y for _, y in current_positions.values())
        dx = max(-min_x, min(PLAYFIELD_WIDTH - max_x, requested_dx))
        dy = max(-min_y, min(PLAYFIELD_HEIGHT - max_y, requested_dy))
        self.preview_offsets[offset_key][0] += dx
        self.preview_offsets[offset_key][1] += dy
        for index in target_indices:
            x, y = current_positions[index]
            self.preview_positions[index] = (round(x + dx), round(y + dy))
        if sync_controls:
            self._sync_position_controls(offset_key)
        elif dx != requested_dx or dy != requested_dy:
            # A slider request reached a playfield edge. Keep the displayed
            # value aligned with the actual clamped translation.
            self._sync_position_controls(offset_key)
        self._sync_center_controls_after_drag(offset_key, dx, dy)
        self.refresh_canvas()

    def _preview_specs(self) -> list:
        if not self._is_split_mode(): return [self._spec(0,"all")]
        return [self._spec(0,"don"),self._spec(1,"kat")]

    def _preview_key(self, specs) -> tuple:
        return (self.commit_revision,tuple(sorted(self.selected)),tuple((name,freeze_preview_value(params)) for name,params in specs))

    def _with_preview_offsets(self, result: dict[int, tuple[int, int]]) -> dict[int, tuple[int, int]]:
        """Every selected note's position: the transformation's, else where it
        already is -- then the drag offset on top.

        Offsetting only what the transformation returned dropped a drag on any
        note it did not move, and with no transformation at all it returned
        nothing: Apply committed none of a canvas drag.
        """
        base = {index: self.applied_positions[index] for index in self.selected if index in self.applied_positions}
        base.update(result)
        return self._apply_preview_offsets(base)

    def update_preview(self) -> None:
        try:
            self.preview_positions=dict(self.applied_positions)
            specs=self._preview_specs()
            if any(name=="drawn_path" and len(params.get("points",[]))<2 for name,params in specs):
                self.preview_positions=dict(self.applied_positions);self.refresh_canvas();self.status.setText(tr("MainWindow", "Open Drawing Window and draw a shape to preview."));return
            key=self._preview_key(specs)
            result=self.preview_cache.get(key)
            if result is None:
                result=self._calculate_selected_transform(); self.preview_cache={key:result}
            self.preview_positions.update(self._with_preview_offsets(result))
        except Exception as error:
            self.status.setText(
                f"Preview error: {error}"
            )
            return

        self.refresh_canvas()

        self.status.setText(
            f"Preview only: {len(self.selected)} selected notes. "
            "Click Apply to selection to commit."
        )

    def refresh_canvas(self) -> None:
        if self.document is None:
            return

        visible_notes=[n for n in self.document.hit_objects if not self.selected or n.original_index in self.selected]
        self.canvas.set_state(visible_notes,self.preview_positions,self.selected)

        self.timeline.selected = set(self.selected)
        self.timeline.update()

    def apply_selection(self) -> None:
        if not self.selected:
            self.status.setText(
                tr("MainWindow", "Select notes in the bottom timeline first.")
            )
            return

        try:
            changed_positions = self._with_preview_offsets(
                self._calculate_selected_transform()
            )
        except Exception as error:
            QMessageBox.warning(
                self,
                tr("MainWindow", "Apply failed"),
                str(error),
            )
            return

        changes = {
            key: (self.applied_positions[key], value)
            for key, value in changed_positions.items()
        }
        self.preview_cache.clear()
        self.state.history.push(MoveNotes(changes), self.state)
        self.preview_offsets = {"all": [0.0, 0.0], "don": [0.0, 0.0], "kat": [0.0, 0.0]}
        for group in self.position_controls:
            self._sync_position_controls(group)
        # The transformation is still selected on the same notes, so the next
        # preview -- a click on the canvas or the timeline, any control --
        # recomputed it from the positions it had just been applied to, with the
        # drag offset gone: the notes snapped back to the transformation's
        # pre-drag layout on screen while the committed ones sat elsewhere.
        # What was just applied *is* the preview for this selection and these
        # settings, until either changes or the history moves (the revision is
        # in the key, so an undo recomputes as before).
        self.preview_cache = {self._preview_key(self._preview_specs()): {}}
        self.state.sync_preview_to_applied()
        self.refresh_canvas()

        self.status.setText(
            f"Applied transformation to "
            f"{len(changed_positions)} notes."
        )

    def reset_applied(self) -> None:
        self.preview_cache.clear()
        self.state.history.push(
            SetNotePositions(dict(self.applied_positions), dict(self.original_positions)),
            self.state,
        )
        self.state.sync_preview_to_applied()
        self.refresh_canvas()

        self.status.setText(
            tr("MainWindow", "All applied transformations reset.")
        )

    def _history_target(self) -> DifficultyState | None:
        """The difficulty Ctrl+Z / Ctrl+Y should act on.

        Whichever Editor-page view last had focus, since that is what the
        user was just editing -- an Editor view can be showing a difficulty
        other than the Fancy Arranger page's active one. Falls back to the
        active difficulty when no editor view is focused (Fancy Arranger's
        own transform undo).

        The gimmick page comes first and does not consult focus at all: it edits
        exactly one difficulty, its layers deliberately never become the
        Editor page's focused view (they own their own toolbox), and without
        this Ctrl+Z there undid whatever the Editor page had last touched.
        """
        if self.page_stack.currentIndex() == PAGE_GIMMICK and self._gimmick_pairing is not None:
            gimmick_state = self._states.get(self._gimmick_pairing.target)
            if gimmick_state is not None:
                return gimmick_state

        view = self._last_focused_editor_view
        if view is not None:
            path = self._difficulty_for_view(view)
            state = self._states.get(path) if path is not None else None
            if state is not None:
                return state
        return self.state

    def _after_history_change(self, state: DifficultyState) -> None:
        """Re-read the reverted/reapplied document into every view showing it.

        Undo used to refresh only the transform canvas, so undoing a note
        placement or an SV sweep left every Editor-page view still painting
        the old document -- which read as "the undo shortcut does nothing".
        """
        state.preview_cache.clear()
        state.sync_preview_to_applied()
        with self._refresh_cycle():
            self._refresh_difficulty_views(state.source_path)
            self._refresh_difficulty_sv_views(state.source_path)
        self.refresh_canvas()

    def undo(self)->None:
        state = self._history_target()
        if state is None or not state.history.can_undo(): return
        state.history.undo(state); self._after_history_change(state)
    def redo(self)->None:
        state = self._history_target()
        if state is None or not state.history.can_redo(): return
        state.history.redo(state); self._after_history_change(state)

    # -- mods ------------------------------------------------------------

    _mods: frozenset = frozenset()

    def _build_mod_strip(self, strip) -> None:
        """HR EZ | HD FL | DT NC HT DC, each in its own colour. The row is
        grouped by what the mods do (owner's call, 2026-10-07); every mod
        *string* still reads in osu!'s order (MOD_ORDER)."""
        if not hasattr(self, "_mod_buttons"):
            self._mod_buttons: list[dict[str, QPushButton]] = []
        tips = {
            "HD": tr("MainWindow", "Hidden: notes fade out as they come in, gone after 37.5% of the scroll"),
            "NC": tr("MainWindow", "Nightcore: 1.5x speed, pitch raised with it"),
            "DT": tr("MainWindow", "Double Time: 1.5x speed, pitch kept"),
            "DC": tr("MainWindow", "Daycore: 0.75x speed, pitch lowered with it"),
            "HT": tr("MainWindow", "Half Time: 0.75x speed, pitch kept"),
            "HR": tr("MainWindow", "Hard Rock: notes scroll faster (SliderMultiplier x1.87)"),
            "EZ": tr("MainWindow", "Easy: notes scroll slower (SliderMultiplier x0.8)"),
            "FL": tr("MainWindow", "Flashlight: only a circle around the hit target is visible, smaller as combo grows"),
        }
        buttons: dict[str, QPushButton] = {}
        for mod in MOD_ORDER:
            button = QPushButton(mod)
            button.setCheckable(True)
            button.setToolTip(tips[mod])
            button.clicked.connect(lambda checked=False, m=mod: self._toggle_mod(m))
            buttons[mod] = button
        for group in MOD_PILL_GROUPS:
            strip.addWidget(segmented([buttons[mod] for mod in group]))
            for mod in group:
                buttons[mod].setStyleSheet(mod_pill_style(mod))
        self._mod_buttons.append(buttons)
        self._build_autoplay_button(strip)

    _autoplay: tuple[str, str, bool] | None = None
    _autoplay_choice: tuple = (None, autoplay.BINDINGS[0], False)

    def _build_autoplay_button(self, strip) -> None:
        """Autoplay (owner, 2026-10-10): the preview's input drum played the
        way a player would, in a chosen style. One menu: style, binding,
        dominant hand. Checked while one plays."""
        if not hasattr(self, "_autoplay_menus"):
            self._autoplay_menus: list[tuple[QPushButton, dict]] = []
        button = QPushButton(tr("MainWindow", "Autoplay"))
        button.setCheckable(True)
        button.setFocusPolicy(Qt.NoFocus)
        button.setToolTip(tr("MainWindow", "Play the gameplay preview's drum the way a player would"))
        menu = QMenu(button)
        actions: dict = {}
        sections = (
            ("style", tr("MainWindow", "Style"), (
                (None, tr("MainWindow", "Off")),
                (autoplay.FULL_ALT, tr("MainWindow", "kddk full alternate")),
                (autoplay.SEMI_ALT, tr("MainWindow", "kddk semi alternate")),
                (autoplay.SINGLE_TAP, tr("MainWindow", "kddk single tap")),
                (autoplay.ROLL, tr("MainWindow", "kddk roll")),
                (autoplay.DDKK, "ddkk"),
            )),
            ("binding", tr("MainWindow", "Binding (kddk)"), tuple((b, b) for b in autoplay.BINDINGS)),
            ("left", tr("MainWindow", "Dominant hand"), (
                (False, tr("MainWindow", "Right")),
                (True, tr("MainWindow", "Left")),
            )),
        )
        for key, title, choices in sections:
            menu.addSection(title)
            group = QActionGroup(menu)
            for value, label in choices:
                action = menu.addAction(label)
                action.setCheckable(True)
                group.addAction(action)
                action.triggered.connect(lambda _=False, k=key, v=value: self._choose_autoplay(k, v))
                actions[(key, value)] = action
        button.setMenu(menu)
        strip.addWidget(button)
        self._autoplay_menus.append((button, actions))

    def _choose_autoplay(self, key: str, value) -> None:
        style, binding, left = self._autoplay_choice
        if key == "style":
            style = value
        elif key == "binding":
            binding = value
        else:
            left = value
        self._set_autoplay_choice(style, binding, left)

    def _set_autoplay_choice(self, style, binding: str, left: bool) -> None:
        # The binding and hand are kept while Off, so turning it back on
        # plays the way it last did.
        self._autoplay_choice = (style, binding, left)
        self._autoplay = (style, binding, left) if style is not None else None
        self.settings.set_value("playback/autoplay", f"{style or ''},{binding},{int(left)}")
        for view in self._gameplay_views:
            view.set_autoplay(self._autoplay)
        for button, actions in getattr(self, "_autoplay_menus", []):
            button.setChecked(style is not None)
            for (key, value), action in actions.items():
                action.setChecked(value == {"style": style, "binding": binding, "left": left}[key])

    def _restore_autoplay(self) -> None:
        saved = self.settings.string_value("playback/autoplay", "")
        # kddk full alt until the user picks otherwise (owner, 2026-10-10).
        # Off is saved as an empty style, so it survives a restart.
        style, binding, left = ((saved or autoplay.FULL_ALT) + ",,").split(",")[:3]
        self._set_autoplay_choice(
            style if style in autoplay.STYLES else None,
            binding if binding in autoplay.BINDINGS else autoplay.BINDINGS[0],
            left == "1")

    def _rate_mod(self) -> str | None:
        return next((mod for mod in MOD_ORDER if mod in self._mods and mod in RATE_MODS), None)

    def _choose_speed(self, rate: float) -> None:
        """A speed button. It replaces any rate mod: the song plays at one rate."""
        if self._rate_mod():
            self._mods = frozenset(mod for mod in self._mods if mod not in RATE_MODS)
            self._store_mods()
        self.player.set_pitch_shift(False)
        self._change_playback_speed(rate)

    def _toggle_mod(self, mod: str) -> None:
        """One click on a mod pill. The rate mods are one choice among
        themselves, and so are HR/EZ; the rest combine freely (HDHR, HDDT)."""
        mods = set(self._mods)
        if mod in mods:
            mods.discard(mod)
        else:
            for group in (tuple(RATE_MODS), SCROLL_MODS):
                if mod in group:
                    mods.difference_update(group)
            mods.add(mod)
        self._set_mods(mods)

    def _set_mods(self, mods) -> None:
        previous_rate_mod = self._rate_mod()
        self._mods = frozenset(mod for mod in mods if mod in MOD_ORDER)
        self._store_mods()
        rate_mod = self._rate_mod()
        if rate_mod != previous_rate_mod:
            rate, pitch = RATE_MODS[rate_mod] if rate_mod else (1.0, False)
            # Pitch first: the engine reads both on its next grain, in order.
            self.player.set_pitch_shift(pitch)
            self._change_playback_speed(rate)
        for view in self._gameplay_views:
            view.set_mods(self._mods)
        self._sync_mod_controls()

    def _store_mods(self) -> None:
        self.settings.set_value("playback/mods", mod_string(self._mods))

    def _restore_mods(self) -> None:
        text = self.settings.string_value("playback/mods", "")
        mods = {text[i:i + 2] for i in range(0, len(text), 2)} & set(MOD_ORDER)
        if mods:
            self._set_mods(mods)

    def _sync_mod_controls(self) -> None:
        for buttons in getattr(self, "_mod_buttons", []):
            for mod, button in buttons.items():
                button.blockSignals(True)
                button.setChecked(mod in self._mods)
                button.blockSignals(False)
        chip = getattr(self, "mods_chip", None)
        if chip is not None:
            chip.setText(mod_string(self._mods))
            # Not on song select (owner, 2026-10-17): the mods are the
            # editor's, and its preview plays without them.
            chip.setVisible(bool(self._mods) and self.page_stack.currentIndex() != PAGE_LIBRARY)

    def _change_playback_speed(self,rate:float)->None:
        # Re-anchor from *our own* playhead, sampled before the rate changes --
        # never from self.player.position(). That value is whole milliseconds
        # (seek_audio deliberately keeps the real one fractional), it is stale
        # by up to one backend report, it is 0 whenever no media loaded or the
        # backend has not reported yet, and reading it *after* setPlaybackRate
        # means whatever the backend extrapolates into it is computed with the
        # new rate against playback still running at the old one. Every one of
        # those fired on the speed-button click, which is why 25/50/75% looked
        # wrong while 100% -- the startup default nobody ever clicks -- did not.
        # The clock restarts immediately after the sample and before the rate
        # is applied, so the handover is continuous: position up to here at the
        # old rate, everything after it at the new one.
        # The *source* clock needs the same handover, and for the same reason.
        # _source_clock_position extrapolates the last backend report forward
        # by elapsed real time * playbackRate(), so leaving it alone here would
        # replay the interval since that report at the new rate. Reports land
        # tens of milliseconds apart, so at 0.25x that mis-extrapolation is
        # comfortably past POSITION_ALLOWABLE_ERROR_MS -- the interpolated
        # clock would then *snap* to a wrong source on the very next frame,
        # which is the speed-button jump this scheme exists to remove. Rebase
        # it on the old rate first, then let the new one run from there.
        rate=float(rate)
        self.latest_audio_position=self._source_clock_position()
        self._source_report_clock.restart()
        self.audio_anchor_position=self._predicted_audio_position();self.audio_anchor_clock.restart()
        self._last_predicted_position=self.audio_anchor_position
        self.player.setPlaybackRate(rate)
        # See _advance_interpolated_clock: the backend goes quiet for around a
        # tenth of a second here, and the playhead waits it out rather than
        # running on into the silence.
        self._awaiting_rate_report = self.player.playbackState() == QMediaPlayer.PlayingState
        for button in (
            getattr(self,"playback_speed_buttons",[])
            + getattr(self,"editor_playback_speed_buttons",[])
            + getattr(self,"gimmick_playback_speed_buttons",[])
        ):
            # The speed the song is at, whoever set it: HT/DC light 75% too
            # (owner's call), and DT/NC light nothing, since there is no 150%.
            active=abs(float(button.property("playbackRate"))-rate)<0.0001
            button.blockSignals(True);button.setChecked(active);button.blockSignals(False)
        self._sync_mod_controls()

    def _set_views_playing(self, playing: bool) -> None:
        """Tell every time-axis view whether the song is running.

        The wheel steps a whole beat while playing (`TimeAxisMixin.wheelEvent`),
        so this cannot be the main timeline alone the way the playhead flag once
        was -- the Editor and gimmick pages scroll their own views.
        """
        self.timeline.is_playing = playing
        for view in (self._chart_views + self._sv_views + self._gameplay_views):
            view.is_playing = playing

    def toggle_playback(self) -> None:
        if (
            self.player.playbackState()
            == QMediaPlayer.PlayingState
        ):
            self.player.pause()
            self._set_views_playing(False)
            self.play_button.setText(tr("MainWindow", "Play"))
            # The glyph buttons keep their typed "▶" for good: it is only
            # there for width, and swapping in the wider "❚❚" grew the button
            # under the glyph on every Play.
            if hasattr(self,"fancy_play_glyph"):self.fancy_play_glyph.set_playing(False)
            if hasattr(self,"editor_play_glyph"):self.editor_play_glyph.set_playing(False)
            if hasattr(self,"gimmick_play_glyph"):self.gimmick_play_glyph.set_playing(False)
        else:
            # The anchor is already the playhead, to the fraction of a
            # millisecond seek_audio kept there on purpose; self.player.position()
            # would round it off, or hand back 0 before the backend has ever
            # reported. So: leave it alone, and restart the clock *after*
            # play() rather than before it, because the backend produces no
            # audio while play() is still setting itself up and counting that
            # setup as elapsed song time is what put the playhead ahead of the
            # music for the first second of every resume.
            self._set_views_playing(True)
            self.player.play()
            self.audio_anchor_clock.restart()
            self._source_report_clock.restart()
            self._last_predicted_position=self.audio_anchor_position
            self._awaiting_rate_report=False
            self.play_button.setText(tr("MainWindow", "Pause"))
            if hasattr(self,"fancy_play_glyph"):self.fancy_play_glyph.set_playing(True)
            if hasattr(self,"editor_play_glyph"):self.editor_play_glyph.set_playing(True)
            if hasattr(self,"gimmick_play_glyph"):self.gimmick_play_glyph.set_playing(True)

    def seek_audio(self, position: float) -> None:
        """Move the shared playhead. `position` is a float on purpose.

        A snap is rarely a whole millisecond, and the audio backend only takes
        whole ones -- so the exact position is kept here and broadcast to the
        views, and only the player is handed the rounded one. Rounding it for
        everybody put every view up to half a millisecond off its own grid,
        which is a third of the screen at the 20ms zoom floor.
        """
        position=max(0.0,float(position)); self.audio_anchor_position=position; self.latest_audio_position=position
        self.audio_anchor_clock.restart(); self._source_report_clock.restart(); self._last_predicted_position=position
        self._awaiting_rate_report=False
        # No hitsound cursor to move: `TimeStretcher.reset` drops the mixer's
        # voices and watermark with the overlap tail, so the seek below is the
        # whole of it.
        #
        # A wheel notch during playback can land with a short pause, audio and
        # playhead together (`playback/wheel_seek_hold_ms`, off by default).
        self._seek_hold_until_ns = 0
        if TimeAxisMixin.wheel_seek_in_progress and \
                self.player.playbackState() == QMediaPlayer.PlayingState:
            hold_ms = self.settings.int_value("playback/wheel_seek_hold_ms", WHEEL_SEEK_HOLD_MS)
            if hold_ms > 0:
                self.player.set_seek_hold_ms(hold_ms)
                self._seek_hold_until_ns = (
                    self.gameplay_frame_clock.nsecsElapsed() + hold_ms * 1_000_000)
        self._unechoed_seeks.append(round(position))
        self.player.setPosition(round(position))
        # Playing, the frame loop carries the new position to every view on its
        # next frame -- it reads the anchor reset above -- so a broadcast here
        # painted every view a second time per notch. On the gimmick page's six
        # bands that was most of a fast scroll's cost:
        # tools/measure_wheel_seek.py --gimmick, Nbt-Hwt, a notch every 10ms,
        # measured 86 of 305 frames late with it.
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            return
        # Every chart view, not just the shared deck timeline: on the gimmick
        # page the six layers are chart views, and a seek that reached only
        # some of them left them disagreeing about where "now" is until the
        # next frame -- which at deep zoom is a visible jump between bands.
        for view in self._chart_views:
            view.set_time(position,force=True)
        for view in self._sv_views:
            view.set_time(position,force=True)
        for view in self._gameplay_views:
            view.set_time(position,force=True)
        for bar in self._timing_bars:
            bar.set_time(position)
        for view in self._density_views:
            view.set_time(position)

    _info_prefix = ""
    _info_suffix = ""
    # gameplay_frame_clock ns until which a wheel seek's pause holds the
    # playhead; 0 when none is running.
    _seek_hold_until_ns = 0

    def _timeline_info_text(self, duration_text: str, position_text: str, snap_text: str) -> str:
        """Build the timeline help line from individually translated pieces.

        The wheel hints describe the real step size in wheelEvent, which moves
        one snap division per notch and one whole beat with Shift held.
        """
        # The snap is the slider beside this, and the wheel hints are key
        # caps after it; this line is what they cannot say.
        return (
            f"{tr('MainWindow', 'Duration')}: {duration_text}   ·   "
            f"{tr('MainWindow', 'Now')}: {position_text}"
        )

    def _update_timeline_info(self) -> None:
        """Re-derive everything around the playhead readout.

        The duration, the snap and the six translated labels only change on a
        load or a click, so this stays on its own slow timer; the two numbers
        that move are in _refresh_position_readouts, which the frame loop
        drives. Splitting them is not a micro-optimisation: this one also
        re-asserts the duration on every timing bar, and doing that per frame
        dirtied their cached static layers.
        """
        if not hasattr(self, "timeline_info"):
            return
        self._refresh_save_label()
        duration = max(0, self.player.duration())
        for bar in getattr(self,"_timing_bars",()): bar.set_duration(duration)
        self._info_duration_ms = duration
        # Built once here and reused per frame: the labels either side of the
        # position never move, so looking them up again at 120Hz would rebuild
        # a string that is identical apart from the middle.
        prefix, suffix = self._timeline_info_text(
            format_time(duration),
            _INFO_POSITION_SLOT,
            f"1/{int(self.snap_combo.currentData())}",
        ).split(_INFO_POSITION_SLOT)
        self._info_prefix, self._info_suffix = prefix, suffix
        self._refresh_position_readouts(
            round(self.timeline.current_time) if hasattr(self,"timeline")
            else self.player.position())

    def _refresh_position_readouts(self, position: float) -> None:
        """The two numbers that follow the playhead, once per rendered frame.

        They used to sit on a 16ms timer of their own while the views scrolled
        on the 8.33ms one, so the text lagged the timeline it was describing and
        beat against it at the difference between the two rates. Reading them
        off the same frame that moves everything else is what keeps them in
        step. QLabel.setText returns early when the text has not changed, so
        the idle timer calling this as well costs nothing.
        """
        if not hasattr(self, "timeline_info"):
            return
        value = max(0, int(position))
        duration = getattr(self, "_info_duration_ms", 0)
        if hasattr(self,"timeline_time"):
            percent=value/duration*100.0 if duration>0 else 0.0
            self.timeline_time.setText(f"{value//60000:02d}:{(value%60000)//1000:02d}:{value%1000:03d}   {percent:.1f}%")
        self.timeline_info.setText(
            f"{self._info_prefix}{format_time(value)}{self._info_suffix}")

    def _source_clock_position(self) -> float:
        """osu!'s framedSourceClock: the last backend report, extrapolated
        forward by elapsed real time * rate while playing.

        This is the value _advance_interpolated_clock blends the displayed
        playhead toward every frame, rather than only at the report itself --
        reports land tens of milliseconds apart, which is far too sparse to
        drive a per-frame display directly.
        """
        position = float(self.latest_audio_position)
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            position += self._source_report_clock.nsecsElapsed() / 1_000_000.0 * self.player.playbackRate()
        return position

    def _advance_interpolated_clock(self, frame_elapsed_ms: float) -> None:
        """One frame of osu!(lazer)'s InterpolatingFramedClock.ProcessFrame.

        Extrapolate the displayed clock by real elapsed time, then either
        snap it to the source clock (drifted more than
        POSITION_ALLOWABLE_ERROR_MS *of real time*, or not currently playing)
        or decay toward it, halving the remaining gap every
        DRIFT_RECOVERY_HALF_LIFE_MS -- every frame, not just at the report. The monotonic clamp is mandatory: a
        blended *negative* error would otherwise step the displayed playhead
        backwards, which is the visible bug this whole scheme replaces. An
        explicit reset (seek_audio, toggle_playback) is the only legitimate
        way to move it backwards, and those reset audio_anchor_position
        directly rather than going through this method.
        """
        last_interpolated = self.audio_anchor_position
        running = self.player.playbackState() == QMediaPlayer.PlayingState
        rate = self.player.playbackRate()
        if running and self._awaiting_rate_report:
            # Measured on WMF: setPlaybackRate stops the backend producing song
            # time for ~120ms (114ms of it lost across a 0.25x -> 1.0x switch,
            # 67ms at 1.0x -> 0.75x). Both clocks here extrapolate blind, so
            # free-running through that stall puts the playhead a tenth of a
            # second ahead of the music -- which then either snaps back
            # visibly or, at the slow rates where the drift stays under the
            # tolerance, simply stays wrong. The audio genuinely is not moving,
            # so neither is the playhead: hold it until the backend reports
            # again, and _player_position_changed re-anchors on that report.
            self.audio_anchor_clock.restart()
            return
        if running and self.gameplay_frame_clock.nsecsElapsed() < self._seek_hold_until_ns:
            # A wheel seek's pause (seek_audio): the engine is writing silence
            # for the same span, so the playhead waits with it.
            self.audio_anchor_clock.restart()
            return
        source = self._source_clock_position()

        interpolated = last_interpolated + (frame_elapsed_ms * rate if running else 0.0)
        if not running or abs(interpolated - source) > POSITION_ALLOWABLE_ERROR_MS * abs(rate):
            interpolated = source
        else:
            interpolated += (source - interpolated) * (
                1.0 - 2.0 ** (-frame_elapsed_ms / DRIFT_RECOVERY_HALF_LIFE_MS))
            if rate >= 0:
                interpolated = max(last_interpolated, interpolated)
            else:
                interpolated = min(last_interpolated, interpolated)
        self.audio_anchor_position = interpolated
        self.audio_anchor_clock.restart()

    def _predicted_audio_position(self) -> float:
        """The interpolated clock, plus sub-frame extrapolation for a caller
        that runs between rendered frames.

        Nanosecond precision keeps the extrapolation error under a
        microsecond regardless of rate (integer-millisecond elapsed()
        truncation used to compound into `rate` ms of error). Monotonic for
        the same reason _advance_interpolated_clock is: a caller between
        frames must never see the playhead step backwards either.
        """
        position = float(self.audio_anchor_position)
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            rate = self.player.playbackRate()
            position += self.audio_anchor_clock.nsecsElapsed() / 1_000_000.0 * rate
            position = max(self._last_predicted_position, position) if rate >= 0 else min(
                self._last_predicted_position, position
            )
        self._last_predicted_position = position
        return position

    def _player_position_changed(self, position: int) -> None:
        """Adopt a fresh backend report as the source clock's new base.

        No blending happens here any more -- that is
        _advance_interpolated_clock's job, run once per rendered frame
        against whatever _source_clock_position() (built from what this
        method stores) currently says. This is deliberately the only place
        that touches `latest_audio_position` and its report clock.
        """
        pending = self._unechoed_seeks
        if pending and position == pending[0]:
            pending.popleft()
            if pending:
                # The echo of a seek a newer one has already replaced. Adopted,
                # it put the playhead back where it was until the newer echo
                # landed ~90ms later: seeking just after opening a map showed
                # the map's start again for that long (2026-10-11, the flaky
                # test_a_seek_while_paused_lands_where_it_was_put).
                return
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            # Qt reports whole milliseconds, so its echo of a seek we just made
            # would round the fractional playhead away again. Only a real move
            # -- someone dragging the media control, a backend jump -- is worth
            # adopting while paused.
            if abs(position - self.latest_audio_position) >= 1.0:
                self.latest_audio_position = float(position)
                self._source_report_clock.restart()
                self.audio_anchor_position = float(position)
                self.audio_anchor_clock.restart()
                self._last_predicted_position = float(position)
            return
        self.latest_audio_position = float(position)
        self._source_report_clock.restart()
        if self._awaiting_rate_report:
            # First word from the backend since the rate changed, so it is the
            # only trustworthy thing in the room: re-anchor on it outright
            # rather than blending toward it from a clock that spent the stall
            # frozen at the old position.
            self._awaiting_rate_report = False
            self.audio_anchor_position = float(position)
            self.audio_anchor_clock.restart()
            self._last_predicted_position = float(position)

    def _render_gameplay_frame(self) -> None:
        now_ns = self.gameplay_frame_clock.nsecsElapsed()
        due = next_frame_due(self._next_frame_due_ns, now_ns, self.gameplay_frame_interval_ns)
        if due is None:
            return
        self._next_frame_due_ns = due

        # osu!'s InterpolatingFramedClock advances once per rendered frame,
        # by that frame's own real elapsed time -- not the fixed interval
        # above, so a render that fires a little late advances by how late
        # it actually was rather than by a nominal amount.
        frame_elapsed_ms = max(0.0, (now_ns - self._last_frame_ns) / 1_000_000.0)
        self._last_frame_ns = now_ns
        self._advance_interpolated_clock(frame_elapsed_ms)

        if self.document is None:return
        position=self._predicted_audio_position()
        duration=self.player.duration()
        if duration>0:position=min(position,duration)
        # Paused, the playhead does not move: _predicted_audio_position only
        # advances in PlayingState, so every view below would repaint an
        # identical picture 120 times a second. Seeks and edits repaint through
        # their own paths (seek_audio, refresh_notes), never through this one.
        if position==self._last_broadcast_position:return
        self._last_broadcast_position=position
        # Nothing here makes a sound any more. The notes are mixed into the
        # music stream a grain at a time (`audio_engine.HitsoundMixer`), so a
        # frame that renders late, early or not at all cannot move them.
        # What the backend reports is the sample it handed to the device, not
        # the one reaching the ears -- the difference is the output latency,
        # which is a constant number of *real* milliseconds. Song time is what
        # the views are drawn against, so it converts by the playback rate,
        # Left in real time it would be four times too large at 0.25x.
        #
        # The hitsounds do not need this and must not have it: they are in the
        # same buffer as the music now, so they already carry exactly this
        # latency, and applying it to them as well would move them off the
        # music by it. `audio/hitsound_offset_ms` survives as a *trim* against
        # the music, which is a different thing and much smaller.
        #
        # **Only while actually playing.** What the offset compensates is the
        # lag between the backend handing a sample to the device and that sample
        # reaching the ears -- a thing that exists only while sound is coming
        # out. Applied at rest it was subtracted again from every seek: a wheel
        # notch moved the view forward, the seek came back through here, and the
        # view landed `offset` short of where it had just been put. At an
        # ordinary BPM that turned a 125ms notch into 96ms and looked like
        # nothing; on a high-BPM section, where a notch is a few milliseconds,
        # it was larger than the step and the view walked *backwards*.
        display = position
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            display -= self.audio_output_offset_ms * self.player.playbackRate()
        # Broadcast to every chart view on screen, not just the shared
        # player-deck timeline: Editor-page chart views scroll in lockstep.
        for view in self._chart_views:
            view.set_time(display,force=True)
        # SV editors ride the same clock, so the green lines scroll past at
        # exactly the rate the notes above them do.
        for view in self._sv_views:
            view.set_time(display,force=True)
        for view in self._gameplay_views:
            view.set_time(display,force=True)
        for view in self._density_views:
            view.set_time(display)
            view.set_viewport(display,self.timeline.window_ms)
        for bar in self._timing_bars:
            bar.set_time(display)
            bar.set_viewport(display,self.timeline.window_ms)
        # Same frame as the views above, so the number and the bar it labels
        # never disagree about where the playhead is.
        self._refresh_position_readouts(display)
        if hasattr(self,"editor_timeline_strip"):
            percent=display/duration*100.0 if duration>0 else 0.0
            text=f"{format_time(display)}   {percent:.1f}%"
            self.editor_timeline_strip.setText(text)
            self.gimmick_timeline_strip.setText(text)

    def _refresh_save_label(self) -> None:
        """Save says how many difficulties it would write.

        Polled from the info timer rather than signalled: History has no change
        signal, and counting a handful of flags is cheaper than growing one.
        """
        count = sum(1 for state in self._states.values() if state.history.dirty)
        if count == getattr(self, "_save_label_count", None):
            return
        self._save_label_count = count
        label = tr("MainWindow", "Save")
        self.save_all_button.setText(f"{label} · {count}" if count else label)

    def save_all_states(self) -> None:
        """Write every changed difficulty's original file at once (global header's Save)."""
        dirty_states = [state for state in self._states.values() if state.history.dirty]
        if not dirty_states:
            self.status.setText(tr("MainWindow", "Nothing to save."))
            return
        errors = []
        for state in dirty_states:
            try:
                for note in state.document.hit_objects:
                    # .get, not []: a note inserted by an older session (or
                    # restored by undoing a delete) may predate its
                    # applied_positions entry, and failing the whole save for
                    # that is far worse than writing the note where it is.
                    note.x, note.y = state.applied_positions.get(note.original_index, (note.x, note.y))
                write_osu(
                    state.document, state.source_path, state.document.version,
                    allow_overwrite_source=True, create_backup=True,
                    force_ar=self.approach_rate_control.value(), force_cs=self.circle_size_control.value(),
                )
                state.history.mark_saved()
                self._discard_acknowledged.pop(state.source_path, None)
            except PermissionError as error:
                # The one failure whose message reads as nonsense on its own:
                # the file is plainly there, and Windows only refuses the
                # replace because something else is holding it open -- osu!
                # with the map loaded, which is exactly the gimmick loop.
                errors.append(
                    f"{state.source_path.name}: {error}\n"
                    + tr("MainWindow", "Close the beatmap in osu! (or any editor holding it) and save again.")
                )
            except Exception as error:
                errors.append(f"{state.source_path.name}: {type(error).__name__}: {error}")
        if errors:
            QMessageBox.critical(self, tr("MainWindow", "Save failed"), "\n".join(errors))
        else:
            self.status.setText(tr("MainWindow", "Saved all changed difficulties."))

    def export_new_difficulty(self) -> None:
        """Branch the active difficulty off into a new file in the same song folder."""
        if self.state is None:
            QMessageBox.information(
                self,
                tr("MainWindow", "Open a map first"),
                tr("MainWindow", "Open a beatmap before exporting a new difficulty."),
            )
            return
        name, ok = QInputDialog.getText(
            self, tr("MainWindow", "Export new difficulty"), tr("MainWindow", "New difficulty name:")
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        source = self.state.source_path
        candidate = source.with_name(f"{source.stem} [{name}].osu")
        number = 2
        while candidate.exists():
            candidate = source.with_name(f"{source.stem} [{name}] ({number}).osu")
            number += 1
        try:
            for note in self.document.hit_objects:
                # .get, not []: same reason as save_all_states -- a note
                # restored by undoing a delete has lost its applied_positions
                # entry, and failing the whole export for that is far worse
                # than writing the note where it already is.
                note.x, note.y = self.applied_positions.get(
                    note.original_index, (note.x, note.y)
                )
            write_osu(
                self.document, candidate, name,
                force_ar=self.approach_rate_control.value(), force_cs=self.circle_size_control.value(),
            )
        except Exception as error:
            QMessageBox.critical(self, tr("MainWindow", "Export failed"), str(error))
            return
        QMessageBox.information(self, tr("MainWindow", "Export complete"), tr("MainWindow", "Created:") + "\n" + str(candidate))

    def export_map(self) -> None:
        if self.document is None or self.source_path is None:
            return

        candidate = self.source_path.with_name(
            f"{self.source_path.stem} [arranged].osu"
        )

        number = 2

        while candidate.exists():
            candidate = self.source_path.with_name(
                f"{self.source_path.stem} "
                f"[arranged] ({number}).osu"
            )
            number += 1

        destination, _ = QFileDialog.getSaveFileName(
            self,
            tr("MainWindow", "Export Applied Map"),
            str(candidate),
            tr("MainWindow", "osu! beatmaps (*.osu)"),
        )

        if not destination:
            return

        destination_path = Path(destination).resolve()

        if destination_path == self.source_path:
            QMessageBox.warning(
                self,
                tr("MainWindow", "Source protected"),
                tr("MainWindow", "Choose a different filename."),
            )
            return

        try:
            for note in self.document.hit_objects:
                note.x, note.y = self.applied_positions.get(
                    note.original_index, (note.x, note.y)
                )

            write_osu(
                self.document,
                destination_path,
                f"{self.document.version} (arranged)",
                force_ar=self.approach_rate_control.value(),
                force_cs=self.circle_size_control.value(),
            )

        except Exception as error:
            QMessageBox.critical(
                self,
                tr("MainWindow", "Export failed"),
                str(error),
            )
            return

        QMessageBox.information(
            self,
            tr("MainWindow", "Export complete"),
            tr("MainWindow", "Created:") + "\n" + str(destination_path),
        )


# Every gimmick layer's toolbox gets its own numbered keys, derived from the
# tables above rather than restated: adding a layer to GIMMICK_LAYERS and
# GIMMICK_TOOLSETS is all it takes for that layer's tools to turn up in
# Settings with their own group header. settings.py cannot reach in for them
# (gui.py imports settings, not the other way round), so the tables are
# handed over instead.
#
# The scope is the layer id, which is what stops the seven toolboxes that all
# start at "1" being reported as conflicts -- see `duplicate_shortcuts`.
register_shortcut_definitions(
    ShortcutDefinition(
        f"tool_{layer_id}_{index}",
        f"Tool {index}: {tool_label}",
        f"Tools — {layer_label}",
        str(index),
        layer_id,
    )
    for layer_id, _view_type, layer_label in MainWindow.GIMMICK_LAYERS
    for index, (_tool_id, tool_label) in enumerate(MainWindow.GIMMICK_TOOLSETS[layer_id], start=1)
)


def main() -> None:
    # Before anything else: this may be the staged build, started only to copy
    # itself over the app folder. It shows no window and relaunches that copy.
    if updater.run_apply_mode(sys.argv):
        return
    update_failure = updater.cleanup_leftovers() if updater.can_install_in_place() else ""
    settings = SettingsManager()
    # FreeType, not DirectWrite: DirectWrite draws Exo 2 with ClearType's red
    # and blue fringes whatever the font's style strategy asks for, which on
    # this navy read as smeared text (owner's screenshots, 2026-09-26). On
    # the real screen FreeType cut the fringe pixels of one song row from 709
    # to 104 (the rest is the pink selection bar), with Japanese fallback and
    # the ★ intact. An explicit -platform or QT_QPA_PLATFORM still wins.
    if sys.platform == "win32" and "-platform" not in sys.argv:
        os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=freetype")
    app=QApplication(sys.argv)
    theme.apply_palette(app)
    # Without these, QStandardPaths.AppDataLocation resolves to
    # AppData/Roaming/python -- shared with every other PySide app run by the
    # same interpreter, and where the song index would have been written.
    app.setOrganizationName(ORGANIZATION_NAME)
    app.setApplicationName(APPLICATION_NAME)
    # First start only: pick a language before any UI text is built, since Qt
    # does not retranslate widgets that already exist.
    #
    # Keyed on setup/completed, not on language/current: the Settings dialog
    # writes a language on every Apply, so an existing install already has one
    # stored and would have skipped this screen without ever showing it.
    # setup/completed is only set once a songs folder has actually been
    # chosen, so a cancelled setup is offered again next launch.
    if not settings.bool_value("setup/completed", False):
        dialog = LanguageDialog()
        dialog.exec()
        settings.set_value("language/current", dialog.language)
        settings.sync()
    install_translator(app, settings)
    icon=application_icon()
    if not icon.isNull():app.setWindowIcon(icon)
    window = MainWindow()
    window.showMaximized()
    window._library.start()
    if update_failure:
        QMessageBox.warning(
            window,
            tr("MainWindow", "Update failed"),
            tr("MainWindow", "The update could not be installed. Your current copy was not changed.")
            + "\n\n" + update_failure,
        )
    window.maybe_check_for_updates()
    # Everything built so far -- modules, the window, its pages -- lives for
    # the whole session, and the cyclic collector rescanned all of it on every
    # full pass: 80k objects, 12-13ms, longer than a 120Hz frame, at moments
    # nothing chose. Frozen, those passes cost nothing measurable (2026-10-11).
    # Only cyclic garbage made before this line could leak, and there is none
    # worth having: refcounting still frees everything else.
    gc.collect()
    gc.freeze()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()