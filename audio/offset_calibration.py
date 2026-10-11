"""Tap along to a click track to find this machine's audio offset.

**This writes one value: `audio/output_offset_ms`, the app's global music
offset.** It never reads or writes a beatmap's own offset, and never touches a
loaded document -- the number it produces is a property of the sound card and
drivers in this machine, not of anybody's chart, and mixing the two is the bug
class that made rate-dependent offsets so hard to pin down in the first place.
osu! and Quaver both keep the two apart for the same reason; Quaver states it
outright in issue #1204.

The click track is generated and played through `TrackPlayer`, deliberately --
that is the same decode-and-sink path the music takes, so what gets measured is
the latency the music actually has. Playing the clicks through `QSoundEffect`
would have been less code and would have measured the *hitsound* path, which
has its own latency and its own separate setting.

What the measurement includes, and cannot separate: the time between the sample
leaving `QAudioSink` and reaching the ear, plus however long the tap takes to
come back through the keyboard and Qt's event loop. That is the same round trip
every rhythm game's calibration measures.
"""
from __future__ import annotations

import math
import statistics
import struct
import tempfile
import wave
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QEvent, Qt, QUrl
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QVBoxLayout,
)

from audio.audio_engine import SAMPLE_RATE, TrackPlayer

# 120 BPM. Fast enough to gather taps quickly, slow enough that nobody has to
# rush, and the half-interval (250ms) is a wide enough window that a tap can
# never be attributed to the wrong click.
CLICK_INTERVAL_MS = 500
TRACK_SECONDS = 30
LEAD_IN_MS = 1000

# The first taps are people finding the beat, not measuring it.
WARMUP_TAPS = 2
MINIMUM_TAPS = 8
# Quaver refuses a calibration whose spread is above 25ms; so does this. The
# spread is measured robustly (see `suggest_offset`), so the threshold means
# "the typical tap misses by more than this", not "one tap did".
MAXIMUM_SPREAD_MS = 25.0


def write_click_track(path: Path) -> None:
    """A short decaying blip every CLICK_INTERVAL_MS, as a 16-bit stereo WAV.

    Written to a real file because `TrackPlayer` takes a URL -- which is the
    point, since routing the clicks through the ordinary decode path is what
    makes the number apply to the music.
    """
    frames = bytearray()
    interval = int(SAMPLE_RATE * CLICK_INTERVAL_MS / 1000)
    lead_in = int(SAMPLE_RATE * LEAD_IN_MS / 1000)
    blip = int(SAMPLE_RATE * 0.008)
    total = SAMPLE_RATE * TRACK_SECONDS
    for index in range(total):
        offset = index - lead_in
        into = offset % interval if offset >= 0 else -1
        value = 0
        if 0 <= into < blip:
            # A sharp attack with a fast decay: the ear locates the onset of
            # this far more precisely than that of a soft tone, and the whole
            # measurement is an onset comparison.
            decay = math.exp(-into / (blip * 0.25))
            value = int(20000 * decay * math.sin(2.0 * math.pi * 1500.0 * into / SAMPLE_RATE))
        frames += struct.pack("<hh", value, value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))


def click_error_ms(position_ms: float) -> float:
    """How far `position_ms` sits from the nearest click, signed.

    Positive means the tap landed after the click, which is what an
    uncompensated output latency looks like: the sound reaches the ear later
    than the position says it did.
    """
    offset = position_ms - LEAD_IN_MS
    nearest = round(offset / CLICK_INTERVAL_MS) * CLICK_INTERVAL_MS
    return offset - nearest


# Keys that mean something other than "tap". Escape closes, Tab would move
# focus, and a modifier held down on its own is not a tap -- everything else
# counts, because the point of taking any key is that nobody has to think about
# which one.
NON_TAP_KEYS = frozenset({
    Qt.Key_Escape, Qt.Key_Tab, Qt.Key_Backtab,
    Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_AltGr, Qt.Key_Meta,
    Qt.Key_CapsLock, Qt.Key_NumLock, Qt.Key_ScrollLock,
})


def is_tap_key(key: int) -> bool:
    """Whether pressing `key` in the dialog should count as a tap.

    Any key does, deliberately. Restricting it to the spacebar meant the one
    key people reach for was also the one the app plays and pauses with, and
    the one a focused button activates -- see `OffsetCalibrationDialog.event`.
    """
    return key not in NON_TAP_KEYS


def suggest_offset(errors: list[float]) -> tuple[float | None, float, str]:
    """(offset, spread, why not) for the taps collected so far."""
    usable = errors[WARMUP_TAPS:]
    if len(usable) < MINIMUM_TAPS:
        return None, 0.0, "more"
    # Median, not mean: one badly missed tap should not drag the answer.
    offset = statistics.median(usable)
    # And the spread has to be robust for the same reason, or the gate undoes
    # what the median just did -- a standard deviation over a single fumbled tap
    # in an otherwise steady run reads as "too inconsistent" and throws away a
    # perfectly good calibration. Median absolute deviation, scaled so it means
    # the same thing as a standard deviation on well-behaved taps.
    spread = statistics.median([abs(value - offset) for value in usable]) * 1.4826
    if spread > MAXIMUM_SPREAD_MS:
        return None, spread, "spread"
    return offset, spread, ""


class OffsetCalibrationDialog(QDialog):
    """Tap any key with the clicks; apply the number it settles on.

    **No widget in here takes keyboard focus.** That is the fix for "the
    spacebar pauses instead of tapping": clicking Play with the mouse focuses
    the Play button, and a focused `QPushButton` activates itself on Space long
    before any of this dialog's own handlers see the key. Reclaiming the key
    from the application-wide play shortcut -- which `event` still does -- never
    touched that, because the button was the one eating it.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Calibrate music offset"))
        self.setMinimumWidth(420)
        # Keys have to reach `keyPressEvent`, so the dialog itself is the only
        # thing that may hold focus; every button below is set to NoFocus.
        self.setFocusPolicy(Qt.StrongFocus)
        self.result_offset: int | None = None
        self._errors: list[float] = []
        self._last_position = 0.0
        self._since_report = QElapsedTimer()
        self._since_report.start()

        self._temp = tempfile.TemporaryDirectory()
        track = Path(self._temp.name) / "click.wav"
        write_click_track(track)

        self.player = TrackPlayer(self)
        self.player.positionChanged.connect(self._position_reported)
        self.player.setSource(QUrl.fromLocalFile(str(track)))
        self.player.setVolume(0.6)

        layout = QVBoxLayout(self)
        explain = QLabel(self.tr(
            "Press Play, then tap any key in time with the clicks. This "
            "measures your audio device only — it changes the app's music "
            "offset and never touches any beatmap's own offset."
        ))
        explain.setWordWrap(True)
        layout.addWidget(explain)

        self.readout = QLabel()
        self.readout.setAlignment(Qt.AlignCenter)
        self.readout.setStyleSheet("font-size: 20px; padding: 14px;")
        layout.addWidget(self.readout)

        self.progress = QProgressBar()
        self.progress.setRange(0, MINIMUM_TAPS)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        row = QHBoxLayout()
        self.play_button = QPushButton(self.tr("Play"))
        self.play_button.clicked.connect(self._toggle)
        row.addWidget(self.play_button)
        self.reset_button = QPushButton(self.tr("Start over"))
        self.reset_button.clicked.connect(self._restart)
        row.addWidget(self.reset_button)
        layout.addLayout(row)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        self.apply_button = self.buttons.button(QDialogButtonBox.Apply)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        # Every button, including the two inside the button box. A button that
        # can hold focus is a button that swallows the tap that focused it.
        for button in self.findChildren(QPushButton):
            button.setFocusPolicy(Qt.NoFocus)
        self._refresh()

    def showEvent(self, event) -> None:
        # Every button is NoFocus, so on open nothing held focus and taps went
        # nowhere until a click landed on the dialog itself.
        super().showEvent(event)
        self.setFocus(Qt.OtherFocusReason)

    # -- playback -------------------------------------------------------------

    def _position_reported(self, position_ms: float) -> None:
        self._last_position = position_ms
        self._since_report.restart()

    def _position_now(self) -> float:
        """The pump reports every 10ms, and this is measuring milliseconds, so
        the gap since the last report is extrapolated rather than ignored."""
        return self._last_position + self._since_report.nsecsElapsed() / 1_000_000.0

    def _toggle(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.play_button.setText(self.tr("Play"))
        else:
            self.player.play()
            self.play_button.setText(self.tr("Pause"))

    def _restart(self) -> None:
        self._errors.clear()
        self.player.setPosition(0.0)
        self._refresh()

    # -- taps -----------------------------------------------------------------

    def event(self, event) -> bool:
        """Take every tap key back from the application-wide shortcuts.

        `MainWindow` registers play/pause, undo, save and the six tool digits
        with `Qt.ApplicationShortcut`, which fires over dialogs too -- and an
        ApplicationShortcut *consumes* its key, so those presses would never
        arrive here at all. Now that any key is a tap, the claim has to cover
        any key rather than just Space. Accepting the ShortcutOverride is the
        documented way for the focused widget to win; the key then arrives as
        an ordinary press below.
        """
        if event.type() == QEvent.ShortcutOverride and is_tap_key(event.key()):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        # Enter must not close the dialog through the default button while
        # someone is tapping, so it taps like everything else; only Escape gets
        # out, and `is_tap_key` is what keeps it that way.
        if is_tap_key(event.key()):
            if not event.isAutoRepeat():
                self._tap()
            event.accept()
            return
        super().keyPressEvent(event)

    def _tap(self) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        self._errors.append(click_error_ms(self._position_now()))
        self._refresh()

    def _refresh(self) -> None:
        offset, spread, reason = suggest_offset(self._errors)
        counted = max(0, len(self._errors) - WARMUP_TAPS)
        self.progress.setValue(min(counted, MINIMUM_TAPS))
        if offset is not None:
            self.readout.setText(
                self.tr("Offset: %1 ms (spread %2 ms)")
                .replace("%1", f"{offset:+.0f}").replace("%2", f"{spread:.0f}"))
        elif reason == "spread":
            self.readout.setText(
                self.tr("Too inconsistent to trust — keep tapping"))
        else:
            self.readout.setText(
                self.tr("Taps: %1 of %2")
                .replace("%1", str(counted)).replace("%2", str(MINIMUM_TAPS)))
        self.apply_button.setEnabled(offset is not None)

    def _apply(self) -> None:
        offset, _spread, _reason = suggest_offset(self._errors)
        if offset is None:
            return
        self.result_offset = int(round(offset))
        self.accept()

    # -- teardown -------------------------------------------------------------

    def done(self, result: int) -> None:
        self.player.shutdown()
        self._temp.cleanup()
        super().done(result)
