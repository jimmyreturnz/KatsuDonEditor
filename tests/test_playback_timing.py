"""Playback position accuracy/smoothness.

Adapted from osu!(lazer)'s InterpolatingFramedClock. Two clocks:

* The **source** clock (_source_clock_position) is the last raw
  QMediaPlayer.positionChanged report, extrapolated forward by elapsed real
  time * rate. Reports land tens of milliseconds apart, so this alone is far
  too sparse to drive a per-frame display.
* The **interpolated** clock (self.audio_anchor_position) is what is actually
  displayed. _advance_interpolated_clock nudges it 1/POSITION_INTERPOLATION_
  DIVISOR of the way toward the source clock every *rendered frame* (not
  every report), or snaps it straight to the source clock if the two have
  drifted more than POSITION_ALLOWABLE_ERROR_MS apart.

Blending every frame instead of only at report time is what makes the
correction invisible. A monotonic clamp is mandatory: a blend toward a source
clock that is momentarily *behind* the interpolated one (a negative error --
completely normal report jitter) would otherwise step the displayed playhead
backwards every time it happened, which is the single most visible symptom
this model exists to prevent. _predicted_audio_position (the interpolated
clock plus its own small sub-frame extrapolation, for callers between
rendered frames) carries the same monotonic guarantee.

_change_playback_speed and toggle_playback re-anchor from
_predicted_audio_position(), sampled *before* the rate/state actually changes
-- see their own comments for why reading QMediaPlayer.position() there used
to move the playhead on every speed-button click at 25/50/75%.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui import gui
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class _FakePlaying:
    """Stands in for QMediaPlayer's state/rate so tests don't need real audio."""

    def __init__(self, rate: float = 1.0) -> None:
        self.rate = rate

    def playbackState(self):
        return gui.QMediaPlayer.PlayingState

    def playbackRate(self):
        return self.rate

    def setPlaybackRate(self, rate):
        self.rate = float(rate)


class _FakeClock:
    """QElapsedTimer with the wall clock under the test's control.

    Real elapsed time is unusable here: the loop being tested is driven by how
    much *wall* time passed between two position reports, and back-to-back
    Python calls pass none of it -- which is a playback situation that cannot
    occur and would hide any rate dependence rather than expose it.
    """

    def __init__(self) -> None:
        self.now_ns = 0
        self._started_ns = 0

    def advance_ms(self, wall_ms: float) -> None:
        self.now_ns += int(wall_ms * 1_000_000)

    def start(self) -> None:
        self._started_ns = self.now_ns

    def restart(self) -> None:
        self._started_ns = self.now_ns

    def nsecsElapsed(self) -> int:
        return self.now_ns - self._started_ns


class PlaybackTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        path = write_fixture(directory, "full_v14")

        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(path, refresh_difficulties=True)

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _make_playing(self, rate: float = 1.0) -> _FakePlaying:
        fake = _FakePlaying(rate)
        self.window.player.playbackState = fake.playbackState
        self.window.player.playbackRate = fake.playbackRate
        self.window.player.setPlaybackRate = fake.setPlaybackRate
        return fake

    def _take_the_clock(self) -> _FakeClock:
        clock = _FakeClock()
        self.window.audio_anchor_clock = clock
        return clock

    def _take_source_clock(self) -> _FakeClock:
        clock = _FakeClock()
        self.window._source_report_clock = clock
        return clock

    def _take_frame_clock(self) -> _FakeClock:
        clock = _FakeClock()
        self.window.gameplay_frame_clock = clock
        return clock

    # -- extrapolation --------------------------------------------------------

    def test_predicted_position_is_exact_anchor_when_paused(self):
        self.assertNotEqual(self.window.player.playbackState(), gui.QMediaPlayer.PlayingState)
        self.window.audio_anchor_position = 5000
        self.assertEqual(self.window._predicted_audio_position(), 5000.0)

    def test_predicted_position_extrapolates_while_playing(self):
        self._make_playing(rate=1.0)
        self.window.audio_anchor_position = 1000
        self.window.audio_anchor_clock.restart()
        predicted = self.window._predicted_audio_position()
        self.assertGreaterEqual(predicted, 1000.0)

    def test_predicted_position_never_travels_backwards(self):
        """A stale-but-higher last prediction (e.g. left over from just
        before a rate/state change) must clamp a lower raw reading up to it
        rather than let the displayed playhead visibly jump backwards."""
        self._make_playing(rate=1.0)
        self.window.audio_anchor_position = 1000.0
        self.window.audio_anchor_clock.restart()
        self.window._last_predicted_position = 1005.0

        predicted = self.window._predicted_audio_position()

        self.assertEqual(predicted, 1005.0)

    # -- _advance_interpolated_clock: the per-frame blend/snap -----------------

    def test_advance_halves_the_error_over_one_half_life(self):
        """Upstream decays toward the source (Interpolation.DampContinuously)
        rather than taking a fixed fraction per frame, so the correction is
        measured in real time and not in frames."""
        self._make_playing(rate=1.0)
        source = self._take_source_clock()
        self.window.audio_anchor_position = 1000.0
        self.window.latest_audio_position = 1010.0  # source is 10ms ahead

        # The source has to run too, or the free-run advance alone puts the
        # two further apart than the snap threshold and nothing is blended.
        source.advance_ms(gui.DRIFT_RECOVERY_HALF_LIFE_MS)
        self.window._advance_interpolated_clock(gui.DRIFT_RECOVERY_HALF_LIFE_MS)

        remaining = (self.window._source_clock_position()
                     - self.window.audio_anchor_position)
        self.assertAlmostEqual(remaining, 5.0, places=6)

    def test_the_correction_does_not_depend_on_the_frame_rate(self):
        """The bug this replaces: at 1/8 per frame, 120fps absorbed the same
        drift twice as fast as 60fps, so the clock behaved differently on
        different monitors."""
        seen = []
        for frame_ms in (1000.0 / 60.0, 1000.0 / 120.0):
            self._make_playing(rate=1.0)
            source = self._take_source_clock()
            self.window.audio_anchor_position = 1000.0
            self.window.latest_audio_position = 1010.0
            elapsed = 0.0
            while elapsed < 80.0:
                source.advance_ms(frame_ms)
                self.window._advance_interpolated_clock(frame_ms)
                elapsed += frame_ms
            seen.append(self.window._source_clock_position()
                        - self.window.audio_anchor_position)

        self.assertAlmostEqual(seen[0], seen[1], places=2)

    def test_advance_snaps_when_error_exceeds_allowable(self):
        self._make_playing(rate=1.0)
        self._take_source_clock()
        self.window.audio_anchor_position = 1000.0
        self.window.latest_audio_position = 1000.0 + gui.POSITION_ALLOWABLE_ERROR_MS + 5.0

        self.window._advance_interpolated_clock(0.0)

        self.assertEqual(self.window.audio_anchor_position, self.window.latest_audio_position)

    def test_advance_does_not_snap_just_under_the_allowable_error(self):
        self._make_playing(rate=1.0)
        self._take_source_clock()
        self.window.audio_anchor_position = 1000.0
        self.window.latest_audio_position = 1000.0 + gui.POSITION_ALLOWABLE_ERROR_MS - 1.0

        self.window._advance_interpolated_clock(0.0)

        self.assertNotEqual(self.window.audio_anchor_position, self.window.latest_audio_position)

    def test_the_snap_tolerance_is_measured_in_real_time_not_song_time(self):
        """Upstream multiplies AllowableErrorMilliseconds by the rate, and the
        first version of this port did not. The constant is a budget in real
        milliseconds; the comparison happens in song milliseconds. Unscaled,
        0.25x tolerated four times as much real drift as 1.0x -- the slowest
        speed, which most needs a tight playhead, got the loosest one.
        """
        drift = gui.POSITION_ALLOWABLE_ERROR_MS - 5.0  # under the raw constant

        self._make_playing(rate=1.0)
        self._take_source_clock()
        self.window.audio_anchor_position = 1000.0
        self.window.latest_audio_position = 1000.0 + drift
        self.window._advance_interpolated_clock(0.0)
        self.assertNotEqual(
            self.window.audio_anchor_position, 1000.0 + drift,
            msg="at 1.0x this drift is inside the budget and must only blend",
        )

        self._make_playing(rate=0.25)
        self._take_source_clock()
        self.window.audio_anchor_position = 1000.0
        self.window.latest_audio_position = 1000.0 + drift
        self.window._advance_interpolated_clock(0.0)
        self.assertEqual(
            self.window.audio_anchor_position, 1000.0 + drift,
            msg="the same song-time drift is 4x the real time at 0.25x: snap",
        )

    # -- the backend's rate-change stall --------------------------------------

    def test_a_rate_change_holds_the_playhead_until_the_backend_reports(self):
        """Measured on WMF: setPlaybackRate stops the backend producing song
        time for around 120ms (114ms of it lost across 0.25x -> 1.0x). Both
        clocks here extrapolate blind, so free-running through that stall
        leaves the playhead a tenth of a second ahead of the music.
        """
        self._make_playing(1.0)
        self._take_source_clock()
        clock = self._take_the_clock()
        self.window.player.position = lambda: 0
        self.window.audio_anchor_position = 1000.0
        self.window.latest_audio_position = 1000.0

        self.window._change_playback_speed(0.25)
        self.assertTrue(self.window._awaiting_rate_report)

        clock.advance_ms(120.0)
        self.window._advance_interpolated_clock(120.0)
        self.assertEqual(
            self.window.audio_anchor_position, 1000.0,
            msg="the audio is not moving during the stall, so nor is the playhead",
        )

    def test_the_first_report_after_a_rate_change_re_anchors_the_playhead(self):
        self._make_playing(1.0)
        self._take_source_clock()
        self._take_the_clock()
        self.window.player.position = lambda: 0
        self.window.audio_anchor_position = 1000.0
        self.window.latest_audio_position = 1000.0
        self.window._change_playback_speed(0.25)

        self.window._player_position_changed(1004)

        self.assertFalse(self.window._awaiting_rate_report)
        self.assertEqual(
            self.window.audio_anchor_position, 1004.0,
            msg="the first word from the backend is the only trustworthy value",
        )

    def test_a_rate_change_while_paused_does_not_arm_the_hold(self):
        """Nothing reports while paused, so arming it there would freeze the
        playhead until the next play() rather than until the next report."""
        self.window._change_playback_speed(0.5)
        self.assertFalse(self.window._awaiting_rate_report)

    # -- output latency compensation ------------------------------------------

    def test_the_music_offset_is_real_time_so_it_scales_with_the_rate(self):
        """The offset compensates time spent in the output device, which is
        real time and does not care how fast the song is being read. Applied
        unscaled it would be four times too large at 0.25x -- the same units
        bug HitsoundPlayer.offset_ms already documents on the sample side.
        """
        class _RecordingView:
            def __init__(self) -> None:
                self.time = None

            def set_time(self, time_ms, force=False):
                self.time = time_ms

        self.window.hitsounds.enabled = False
        frames = self._take_frame_clock()
        self.window.audio_output_offset_ms = 40
        seen = []
        for rate, expected in ((1.0, 1000.0 - 40.0), (0.25, 1000.0 - 10.0)):
            self._make_playing(rate)
            self._take_source_clock()
            self._take_the_clock()
            view = _RecordingView()
            self.window._chart_views.append(view)
            self.window.audio_anchor_position = 1000.0
            self.window.latest_audio_position = 1000.0
            self.window._last_broadcast_position = None
            self.window._next_frame_due_ns = 0
            frames.advance_ms(1000.0)
            self.window._render_gameplay_frame()
            self.window._chart_views.remove(view)
            seen.append((view.time, expected))
        for actual, expected in seen:
            self.assertAlmostEqual(actual, expected, places=6)

    def test_advance_never_moves_backwards_on_a_negative_error(self):
        """The source clock reading behind the interpolated one is ordinary
        report jitter, not a seek -- blending toward it must never step the
        displayed playhead backwards."""
        self._make_playing(rate=1.0)
        self._take_source_clock()
        self.window.audio_anchor_position = 1000.0
        self.window.latest_audio_position = 990.0  # source is 10ms behind

        self.window._advance_interpolated_clock(0.0)

        self.assertGreaterEqual(self.window.audio_anchor_position, 1000.0)

    def test_advance_repeated_frames_converge_on_a_steady_bias(self):
        """A consistent small bias converges over a handful of frames even
        though no single frame's error ever exceeds the allowable threshold."""
        self._make_playing(rate=1.0)
        source = self._take_source_clock()
        self.window.audio_anchor_position = 1000.0
        self.window.latest_audio_position = 1015.0

        for _ in range(200):
            source.advance_ms(1000.0 / 120.0)
            self.window._advance_interpolated_clock(1000.0 / 120.0)

        self.assertLess(
            abs(self.window._source_clock_position()
                - self.window.audio_anchor_position), 0.01)

    def test_advance_while_paused_snaps_to_source_without_advancing(self):
        # Not playing: _advance_interpolated_clock must not extrapolate by
        # frame time, only ever reflect the (unextrapolated) source.
        self.window.audio_anchor_position = 1000.0
        self.window.latest_audio_position = 1000.0

        self.window._advance_interpolated_clock(50.0)

        self.assertEqual(self.window.audio_anchor_position, 1000.0)

    def test_player_position_changed_while_playing_only_updates_the_source(self):
        """No blending happens synchronously any more -- a report just moves
        where the source clock reads from; _advance_interpolated_clock is
        what actually moves the displayed playhead, once per frame."""
        self._make_playing(rate=1.0)
        self.window.audio_anchor_position = 1000.0

        self.window._player_position_changed(1500)

        self.assertEqual(self.window.audio_anchor_position, 1000.0)
        self.assertEqual(self.window.latest_audio_position, 1500.0)

    def test_paused_report_re_anchors_exactly(self):
        self.window.audio_anchor_position = 1000
        self.window._player_position_changed(4242)
        self.assertEqual(self.window.audio_anchor_position, 4242)

    def test_paused_report_under_one_ms_is_ignored(self):
        """Qt's own echo of a seek we just made must not perturb the anchor."""
        self.window.audio_anchor_position = 1000.4
        self.window.latest_audio_position = 1000.4
        self.window._player_position_changed(1000)
        self.assertEqual(self.window.audio_anchor_position, 1000.4)

    # -- re-anchoring: never from the backend's rounded, stale position -------

    def test_changing_speed_while_paused_does_not_move_the_playhead(self):
        """QMediaPlayer.position() is 0 until the backend has reported, so this
        used to yank a paused playhead back to the start of the song -- on the
        speed-button click, which is why only 25/50/75% ever looked wrong."""
        self.window.player.position = lambda: 0
        self.window.seek_audio(31234.5)

        self.window._change_playback_speed(0.25)

        self.assertAlmostEqual(self.window.audio_anchor_position, 31234.5, places=6)
        self.assertAlmostEqual(self.window._predicted_audio_position(), 31234.5, places=6)

    def test_changing_speed_while_playing_introduces_no_step(self):
        fake = self._make_playing(1.0)
        clock = self._take_the_clock()
        self.window.player.position = lambda: 0
        self.window.audio_anchor_position = 1000.0
        clock.restart()
        clock.advance_ms(100.0)
        before = self.window._predicted_audio_position()
        self.assertAlmostEqual(before, 1100.0, places=6)

        self.window._change_playback_speed(0.25)

        self.assertEqual(fake.rate, 0.25, "the rate really did change")
        self.assertAlmostEqual(
            self.window._predicted_audio_position(), before, places=6,
            msg="the handover must be continuous: no jump at the instant of the change",
        )
        clock.advance_ms(100.0)
        self.assertAlmostEqual(
            self.window._predicted_audio_position(), before + 25.0, places=6,
            msg="and everything after it runs at the new rate",
        )

    def test_changing_speed_rebases_the_source_clock_too(self):
        """The source clock needs the same handover as the interpolated one.

        _source_clock_position extrapolates the last backend report by elapsed
        real time * playbackRate(). Backend reports land tens of milliseconds
        apart, so leaving the report where it was would replay that whole
        interval at the new rate -- at 0.25x, an error well past
        POSITION_ALLOWABLE_ERROR_MS, which makes the next frame *snap* the
        displayed playhead to a wrong source. That snap is the speed-button
        jump this clock exists to remove, so it must not come back in through
        the source side.
        """
        self._make_playing(1.0)
        source = self._take_source_clock()
        self._take_the_clock()
        self.window.player.position = lambda: 0
        self.window.latest_audio_position = 1000.0
        self.window.audio_anchor_position = 1000.0
        source.restart()
        source.advance_ms(100.0)
        self.assertAlmostEqual(self.window._source_clock_position(), 1100.0, places=6)

        self.window._change_playback_speed(0.25)

        self.assertAlmostEqual(
            self.window._source_clock_position(), 1100.0, places=6,
            msg="rebased at the old rate: the source must not step at the change",
        )
        source.advance_ms(100.0)
        self.assertAlmostEqual(
            self.window._source_clock_position(), 1125.0, places=6,
            msg="and runs at the new rate from there -- not 1000 + 200 * 0.25",
        )

    def test_resuming_playback_keeps_the_fractional_playhead(self):
        """seek_audio keeps the exact position on purpose; play must not round
        it off against the backend's whole-millisecond echo."""
        self.window.player.play = lambda: None
        self.window.player.position = lambda: 0
        self.window.seek_audio(7777.25)

        self.window.toggle_playback()

        self.assertAlmostEqual(self.window.audio_anchor_position, 7777.25, places=6)

    # -- seeking: the one legitimate way to move backwards ---------------------

    def test_seek_backwards_is_not_clamped_away(self):
        """A monotonic clock guard must never fight a real seek: rewinding
        past the interpolated clock's current value has to actually land
        there, not get pinned to the higher pre-seek position."""
        self._make_playing(rate=1.0)
        self._take_source_clock()
        self._take_the_clock()
        self.window.player.position = lambda: 0
        self.window.audio_anchor_position = 5000.0
        self.window._last_predicted_position = 5000.0

        self.window.seek_audio(1000.0)

        self.assertEqual(self.window.audio_anchor_position, 1000.0)
        self.assertEqual(self.window._predicted_audio_position(), 1000.0)
        # And the next frame's blend must not fight the seek either: the
        # source clock was reset to the same position, so there is no error
        # for it to (wrongly) snap or blend away from.
        self.window._advance_interpolated_clock(0.0)
        self.assertEqual(self.window.audio_anchor_position, 1000.0)

    def test_seek_forward_still_works_after_a_backwards_seek(self):
        self._make_playing(rate=1.0)
        self.window.player.position = lambda: 0
        self.window.seek_audio(5000.0)
        self.window.seek_audio(1000.0)

        self.window.seek_audio(2000.0)

        self.assertEqual(self.window.audio_anchor_position, 2000.0)

    # -- frame scheduler --------------------------------------------------------

    def test_frame_scheduler_resyncs_after_a_stall_instead_of_bursting(self):
        self.window._next_frame_due_ns = 1000  # far in the past
        self.window._render_gameplay_frame()

        now_ns = self.window.gameplay_frame_clock.nsecsElapsed()
        self.assertGreater(self.window._next_frame_due_ns, now_ns)
        self.assertLessEqual(
            self.window._next_frame_due_ns,
            now_ns + self.window.gameplay_frame_interval_ns + 1_000_000,
        )

    def test_frame_scheduler_advances_by_exactly_one_interval_normally(self):
        # Just past due, not a stall -- setting this to 0 would itself be
        # more than a full interval behind "now" and trigger the resync
        # branch instead of the normal one.
        now_ns = self.window.gameplay_frame_clock.nsecsElapsed()
        self.window._next_frame_due_ns = now_ns - 1000
        before = self.window._next_frame_due_ns
        self.window._render_gameplay_frame()
        self.assertEqual(self.window._next_frame_due_ns, before + self.window.gameplay_frame_interval_ns)



class OutputOffsetSeekTests(unittest.TestCase):
    """The music offset must not move a seek.

    What it compensates is the lag between the backend handing a sample to the
    device and that sample reaching the ears, which exists only while sound is
    coming out. Applied at rest it was subtracted from every seek on the way
    back through the frame loop, so a view landed `offset` short of where it
    had just been put -- invisible at 125ms a notch, and larger than the step
    on a high-BPM section, where the view then walked backwards.
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")
        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.window.audio_output_offset_ms = 29

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _settle(self) -> None:
        for _ in range(6):
            QApplication.processEvents()

    def test_a_seek_while_paused_lands_where_it_was_put(self):
        self.window.seek_audio(4000.0)
        self._settle()
        for view in self.window._chart_views:
            self.assertAlmostEqual(view.current_time, 4000.0, delta=1.0)

    def test_a_stale_seek_report_does_not_pull_the_playhead_back(self):
        """Two seeks while paused, and the first one's report arriving after
        the second: what made the test above fail one run in three. Driven
        directly, so it does not depend on when the audio thread answers."""
        self.window._unechoed_seeks.clear()
        self.window.seek_audio(1000.0)
        self.window.seek_audio(4000.0)
        self.window._player_position_changed(1000.0)
        self.assertEqual(self.window.audio_anchor_position, 4000.0)
        self.window._player_position_changed(4000.0)
        self.assertEqual(self.window.audio_anchor_position, 4000.0)
        self.assertFalse(self.window._unechoed_seeks)

    def test_repeated_small_seeks_never_go_backwards(self):
        """The reported symptom, in the shape it was reported: on a high-BPM
        section a notch is a few milliseconds, which the offset outweighed."""
        self.window.seek_audio(4000.0)
        self._settle()
        seen = [self.window._chart_views[0].current_time]
        for step in range(1, 6):
            self.window.seek_audio(4000.0 + step * 5.0)
            self._settle()
            seen.append(self.window._chart_views[0].current_time)
        for earlier, later in zip(seen, seen[1:]):
            self.assertGreater(later, earlier, seen)

    def test_the_offset_is_still_there_for_playback(self):
        """It is not deleted, only scoped: the calibration has to keep doing
        its job for the thing it was measured against."""
        self.assertEqual(self.window.audio_output_offset_ms, 29)
        source = Path(gui.__file__).read_text(encoding="utf-8")
        self.assertIn("display -= self.audio_output_offset_ms", source)

if __name__ == "__main__":
    unittest.main()
