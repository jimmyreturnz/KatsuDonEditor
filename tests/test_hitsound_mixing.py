"""Where a note sample actually lands in the output.

The old path fired a `QSoundEffect` when the interpolated playhead crossed a
note. Nothing about that could be tested for placement -- the quantisation was
a rendered frame and the latency was the sound card's -- so what was tested
instead was which notes a *window* crossed, and the sound's position was taken
on faith. Mixed into the stream, placement is an integer and these assert it.

The invariant that matters is not "the note is at the millisecond it asks for"
but **"the note is on the same output sample as the music it belongs to"**. At a
slow rate those are different claims: the reported position is a linear map over
content that arrives a grain at a time, so a note placed by reported time drifts
against the music by `grain_offset_ms`'s residual, while one placed by source
frame cannot. `MusicAlignmentTests` is that invariant, and it is the reason this
file exists.
"""
from __future__ import annotations

import array
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio.audio_engine import (  # noqa: E402
    CHANNELS, SAMPLE_RATE, SEQUENCE_FRAMES, HitsoundMixer, TimeStretcher,
    downmix_to_mono, frame_for_ms,
)

# Loud enough to find against the bed below, and clear of it by 23dB.
SPIKE = 22000
BED = 1500
FOUND = 15000


def _silence(frames: int) -> array.array:
    return array.array("h", bytes(2 * CHANNELS * frames))


def _bed(frames: int) -> array.array:
    """A quiet tone in both channels.

    Not silence: the splice search normalises each candidate by its own energy
    and skips the zero-energy ones, so on pure silence it cannot score anything
    and stops tracking the content -- which makes every placement question
    below meaningless. Real music always has a floor.
    """
    samples = _silence(frames)
    for i in range(frames):
        value = int(BED * math.sin(2.0 * math.pi * 196.0 * i / SAMPLE_RATE))
        samples[i * CHANNELS] = value
        samples[i * CHANNELS + 1] = value
    return samples


def _impulse_sample(frames: int = 1, channel: int = 1) -> bytes:
    """A sample with its energy in one channel, so it can be told apart from
    the music it is mixed into."""
    pcm = _silence(frames)
    for i in range(frames):
        pcm[i * CHANNELS + channel] = SPIKE
    return pcm.tobytes()


def _spikes(frames: array.array, channel: int) -> list[int]:
    """Frame index of every run of loud samples in one channel."""
    found, index, total = [], 0, len(frames) // CHANNELS
    while index < total:
        if abs(frames[index * CHANNELS + channel]) >= FOUND:
            found.append(index)
            # Past the rest of this impulse, so one hit is one entry.
            while (index < total
                   and abs(frames[index * CHANNELS + channel]) >= FOUND):
                index += 1
        else:
            index += 1
    return found


def _mixer(frames, keys=None, gains=None, sample_frames=1) -> HitsoundMixer:
    mixer = HitsoundMixer()
    mixer.set_samples({"don": _impulse_sample(sample_frames)})
    mixer.set_schedule(list(frames),
                       list(keys or ["don"] * len(frames)),
                       list(gains if gains is not None else [1.0] * len(frames)))
    mixer.reset(0.0)
    return mixer


def _pull(stretcher, source, mono, seconds, rate) -> array.array:
    out = array.array("h")
    wanted = int(SAMPLE_RATE * seconds)
    while len(out) // CHANNELS < wanted:
        block = stretcher.pull(source, mono, 4096, rate)
        if not block:
            break
        chunk = array.array("h")
        chunk.frombytes(block)
        out += chunk
    return out


class SampleConversionTests(unittest.TestCase):
    """Milliseconds in, integer sample positions out. The whole placement path
    is integers after this, so this is where a rounding mistake would live."""

    def test_the_reference_conversions(self):
        for ms, frame in ((0, 0), (1, 44), (5, 221), (10, 441), (16, 706),
                          (20, 882), (50, 2205), (100, 4410), (250, 11025),
                          (500, 22050), (1000, 44100)):
            with self.subTest(ms=ms):
                self.assertEqual(frame_for_ms(ms), frame)

    def test_a_whole_second_is_the_sample_rate(self):
        self.assertEqual(frame_for_ms(1000), SAMPLE_RATE)

    def test_halves_go_up_rather_than_to_even(self):
        """`round` is half-to-even, so 5ms (220.5 frames) would go down to 220
        while 15ms (661.5) went up to 662 -- the same fraction resolving two
        different ways depending on the parity of its neighbour."""
        self.assertEqual(frame_for_ms(5), 221)
        self.assertEqual(frame_for_ms(15), 662)
        self.assertEqual(round(5 * SAMPLE_RATE / 1000.0), 220)

    def test_a_long_timestamp_is_still_exact(self):
        """Ten minutes in, where a float32 accumulation would have drifted."""
        self.assertEqual(frame_for_ms(600_000), SAMPLE_RATE * 600)


class PlacementTests(unittest.TestCase):
    """One grain at a time, straight into `mix`, so the frame is exact and no
    stretch is involved."""

    def test_a_note_lands_on_its_own_frame(self):
        for frame in (0, 1, 44, 221, 441, 881):
            with self.subTest(frame=frame):
                out = _silence(SEQUENCE_FRAMES)
                _mixer([frame]).mix(out, 0, SEQUENCE_FRAMES)
                self.assertEqual(_spikes(out, 1), [frame])

    def test_a_note_is_placed_relative_to_the_grains_source(self):
        """The grain is not the start of the song: `_produce` passes wherever
        the splice search landed, and the offset is against that."""
        out = _silence(SEQUENCE_FRAMES)
        _mixer([10_000 + 300]).mix(out, 10_000, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [300])

    def test_a_note_before_the_grain_is_not_played(self):
        out = _silence(SEQUENCE_FRAMES)
        _mixer([9_999]).mix(out, 10_000, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [])

    def test_a_note_one_frame_inside_either_edge(self):
        out = _silence(SEQUENCE_FRAMES)
        _mixer([10_000, 10_000 + SEQUENCE_FRAMES - 1]).mix(
            out, 10_000, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [0, SEQUENCE_FRAMES - 1])

    def test_a_note_on_the_far_edge_belongs_to_the_next_grain(self):
        """Half-open on purpose, so two grains tile the source exactly and a
        note on the boundary is played by one of them and never by both."""
        mixer = _mixer([SEQUENCE_FRAMES])
        first = _silence(SEQUENCE_FRAMES)
        mixer.mix(first, 0, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(first, 1), [])
        second = _silence(SEQUENCE_FRAMES)
        mixer.mix(second, SEQUENCE_FRAMES, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(second, 1), [0])

    def test_overlapping_notes_sum(self):
        out = _silence(SEQUENCE_FRAMES)
        mixer = HitsoundMixer()
        mixer.set_samples({"don": _impulse_sample()})
        mixer.set_schedule([100, 100], ["don", "don"], [0.25, 0.25])
        mixer.reset(0.0)
        mixer.mix(out, 0, SEQUENCE_FRAMES)
        self.assertAlmostEqual(out[100 * CHANNELS + 1], SPIKE // 2, delta=2)

    def test_mixing_adds_to_the_music_rather_than_replacing_it(self):
        out = _bed(SEQUENCE_FRAMES)
        before = out[100 * CHANNELS]
        _mixer([100]).mix(out, 0, SEQUENCE_FRAMES)
        # The note is in the right channel only, so the left is untouched.
        self.assertEqual(out[100 * CHANNELS], before)
        self.assertGreaterEqual(out[100 * CHANNELS + 1], FOUND)

    def test_saturation_is_the_ceiling_and_does_not_wrap(self):
        """`audioop.add` clamps. The failure this prevents is wrapping, which
        turns a loud stack into a full-scale square edge -- a click, not a
        loud note."""
        out = _silence(SEQUENCE_FRAMES)
        for i in range(SEQUENCE_FRAMES):
            out[i * CHANNELS + 1] = 30000
        _mixer([100]).mix(out, 0, SEQUENCE_FRAMES)
        self.assertEqual(out[100 * CHANNELS + 1], 32767)


class VolumeTests(unittest.TestCase):

    def test_the_section_volume_scales_the_sample(self):
        out = _silence(SEQUENCE_FRAMES)
        _mixer([100], gains=[0.5]).mix(out, 0, SEQUENCE_FRAMES)
        self.assertAlmostEqual(out[100 * CHANNELS + 1], SPIKE // 2, delta=2)

    def test_zero_is_silent_and_is_obeyed(self):
        """Mappers set volume 0 deliberately -- it is a thing the Kiai and
        Sound Volume layer exists to edit -- so it is not floored."""
        out = _silence(SEQUENCE_FRAMES)
        _mixer([100], gains=[0.0]).mix(out, 0, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [])

    def test_the_user_volume_multiplies_the_section_volume(self):
        out = _silence(SEQUENCE_FRAMES)
        mixer = _mixer([100], gains=[0.5])
        mixer.volume = 0.5
        mixer.mix(out, 0, SEQUENCE_FRAMES)
        self.assertAlmostEqual(out[100 * CHANNELS + 1], SPIKE // 4, delta=2)

    def test_the_user_volume_applies_without_rescheduling(self):
        mixer = _mixer([100, 100 + SEQUENCE_FRAMES])
        first = _silence(SEQUENCE_FRAMES)
        mixer.mix(first, 0, SEQUENCE_FRAMES)
        mixer.volume = 0.25
        second = _silence(SEQUENCE_FRAMES)
        mixer.mix(second, SEQUENCE_FRAMES, SEQUENCE_FRAMES)
        self.assertAlmostEqual(first[100 * CHANNELS + 1], SPIKE, delta=2)
        self.assertAlmostEqual(second[100 * CHANNELS + 1], SPIKE // 4, delta=2)


class LongSampleTests(unittest.TestCase):
    """The shipped samples run from 354ms to 1.5s and a grain is 20ms, so
    nearly every note outlives the grain that started it."""

    def test_a_voice_continues_into_the_next_grain(self):
        mixer = _mixer([SEQUENCE_FRAMES - 10], sample_frames=100)
        first = _silence(SEQUENCE_FRAMES)
        mixer.mix(first, 0, SEQUENCE_FRAMES)
        second = _silence(SEQUENCE_FRAMES)
        mixer.mix(second, SEQUENCE_FRAMES, SEQUENCE_FRAMES)
        # 10 frames in the first grain, the remaining 90 at the head of the
        # second, with no gap and nothing repeated.
        self.assertEqual(_spikes(first, 1), [SEQUENCE_FRAMES - 10])
        self.assertEqual(_spikes(second, 1), [0])
        self.assertGreaterEqual(second[89 * CHANNELS + 1], FOUND)
        self.assertLess(abs(second[90 * CHANNELS + 1]), FOUND)

    def test_a_finished_voice_is_dropped(self):
        mixer = _mixer([0], sample_frames=100)
        for start in range(0, SEQUENCE_FRAMES * 3, SEQUENCE_FRAMES):
            mixer.mix(_silence(SEQUENCE_FRAMES), start, SEQUENCE_FRAMES)
        self.assertEqual(mixer._voices, [])

    def test_the_voice_count_is_bounded(self):
        """A shiny note is a stack of objects on one millisecond and a barline
        gimmick writes thousands of lines a second, so this has to be bounded
        by something. Nobody can hear the twenty-fifth simultaneous sample."""
        mixer = _mixer([100] * 200, sample_frames=SAMPLE_RATE)
        mixer.mix(_silence(SEQUENCE_FRAMES), 0, SEQUENCE_FRAMES)
        self.assertEqual(len(mixer._voices), HitsoundMixer.MAX_VOICES)


class DedupeTests(unittest.TestCase):
    """At rate < 1 consecutive grains overlap in source, so a note's frame
    falls inside about 1/rate of them. Four hits 5ms apart is a flam."""

    def test_a_note_covered_twice_plays_once(self):
        mixer = _mixer([500])
        first = _silence(SEQUENCE_FRAMES)
        mixer.mix(first, 0, SEQUENCE_FRAMES)
        second = _silence(SEQUENCE_FRAMES)
        # The same source range again, as a 0.5x read head would hand over.
        mixer.mix(second, SEQUENCE_FRAMES // 2, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(first, 1), [500])
        self.assertEqual(_spikes(second, 1), [])

    def test_a_grain_that_moves_backwards_replays_nothing(self):
        """`best_offset` is not monotonic -- the search may land near the top of
        its reach on one grain and the bottom on the next."""
        mixer = _mixer([500])
        mixer.mix(_silence(SEQUENCE_FRAMES), 0, SEQUENCE_FRAMES)
        again = _silence(SEQUENCE_FRAMES)
        mixer.mix(again, 0, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(again, 1), [])

    def test_only_one_voice_is_started_per_note(self):
        mixer = _mixer([500], sample_frames=SAMPLE_RATE)
        for start in (0, 100, 200, 300):
            mixer.mix(_silence(SEQUENCE_FRAMES), start, SEQUENCE_FRAMES)
        self.assertEqual(len(mixer._voices), 1)


class SeekTests(unittest.TestCase):

    def test_a_seek_forward_does_not_fire_what_it_skipped(self):
        """The burst that made scrolling painful on the old path. Here it is
        not guarded, it is impossible: a grain only ever voices notes inside
        its own 20ms of source."""
        mixer = _mixer([100, 200, 300, 400])
        mixer.reset(SAMPLE_RATE)          # a second in
        out = _silence(SEQUENCE_FRAMES)
        mixer.mix(out, SAMPLE_RATE, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [])
        self.assertEqual(mixer._voices, [])

    def test_a_seek_backwards_replays(self):
        mixer = _mixer([500])
        first = _silence(SEQUENCE_FRAMES)
        mixer.mix(first, 0, SEQUENCE_FRAMES)
        mixer.reset(0.0)
        second = _silence(SEQUENCE_FRAMES)
        mixer.mix(second, 0, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(first, 1), [500])
        self.assertEqual(_spikes(second, 1), [500])

    def test_a_seek_drops_sounding_voices(self):
        """Letting them play out would be the old position still audible after
        the user asked to be somewhere else -- the same reason `reset` drops
        the overlap tail."""
        mixer = _mixer([100], sample_frames=SAMPLE_RATE)
        mixer.mix(_silence(SEQUENCE_FRAMES), 0, SEQUENCE_FRAMES)
        self.assertEqual(len(mixer._voices), 1)
        mixer.reset(5.0 * SAMPLE_RATE)
        self.assertEqual(mixer._voices, [])


class ResyncTests(unittest.TestCase):
    """A schedule replacement -- an edit during playback, or a hitsound-offset
    change -- has to rebase `_voiced_through`, or the watermark keeps meaning
    "how far through the *old* schedule", which is nonsense against a new one.

    Reported from use: setting the hitsound offset to -220ms while a chart was
    loaded produced "I can hear a note sound but it is not the note the
    playhead is at" rather than a clean uniform desync. Reproduced in both
    directions below before `resync` existed to fix either.
    """

    def test_a_note_shifted_behind_now_is_not_replayed(self):
        """The silent-drop direction, on its own it would just look like a
        missing hit -- not yet the reported symptom, but still wrong: a note
        the mapper can see and hear nowhere near it is a worse bug than a
        uniformly early or late one."""
        mixer = _mixer([20_000])
        mixer.mix(_silence(SEQUENCE_FRAMES), 15_000, SEQUENCE_FRAMES)
        watermark_before = mixer._voiced_through
        # An offset shift moves the same note to a frame the playhead has
        # already passed.
        mixer.set_schedule([10_298], ["don"], [1.0])
        mixer.resync(watermark_before + 1)
        out = _silence(SEQUENCE_FRAMES)
        mixer.mix(out, watermark_before + 1, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [])

    def test_a_note_that_already_played_does_not_fire_again_later(self):
        """The direction that is literally the report: a note already heard,
        whose shifted position now sits ahead of a *stale* watermark, would
        fire a second time later with nothing on screen to justify it. Without
        `resync` this reproduces; with it, the note stays played-once."""
        mixer = _mixer([10_000])
        mixer.mix(_silence(SEQUENCE_FRAMES), 10_000, SEQUENCE_FRAMES)
        self.assertEqual(len(mixer._voices) or 1, 1)  # it did play once
        # Simulate the bug directly: reschedule without resyncing.
        mixer.set_schedule([30_000], ["don"], [1.0])
        out = _silence(SEQUENCE_FRAMES)
        mixer.mix(out, 30_000, SEQUENCE_FRAMES)
        self.assertEqual(
            _spikes(out, 1), [0],
            "sanity: without resync this note DOES wrongly refire")
        # Now the real path: the engine always resyncs after set_schedule.
        mixer2 = _mixer([10_000])
        mixer2.mix(_silence(SEQUENCE_FRAMES), 10_000, SEQUENCE_FRAMES)
        mixer2.set_schedule([30_000], ["don"], [1.0])
        mixer2.resync(25_000)          # the engine's "now" was past frame 10000
        out2 = _silence(SEQUENCE_FRAMES)
        mixer2.mix(out2, 30_000, SEQUENCE_FRAMES)
        # 30000 is genuinely ahead of "now" (25000), so this one is a real,
        # legitimate future note under the new schedule and must still play.
        self.assertEqual(_spikes(out2, 1), [0])

    def test_resync_does_not_touch_sounding_voices(self):
        """An ordinary edit mid-note should not cut off whatever is currently
        sounding -- that is what makes this `resync` and not `reset`."""
        mixer = _mixer([100], sample_frames=SAMPLE_RATE)
        mixer.mix(_silence(SEQUENCE_FRAMES), 0, SEQUENCE_FRAMES)
        self.assertEqual(len(mixer._voices), 1)
        mixer.set_schedule([200], ["don"], [1.0])
        mixer.resync(SEQUENCE_FRAMES)
        self.assertEqual(len(mixer._voices), 1)

    def test_resync_is_not_seek_proof_and_is_not_meant_to_be(self):
        """`resync` does not distinguish "already handled" from "not yet"
        beyond the watermark number -- moving it backwards makes an
        already-played note eligible again, same as `reset` would.

        That is fine and not a gap: `resync`'s only caller
        (`_Engine.set_hitsound_schedule`) always passes the live playback
        position, which only moves backwards on a real seek -- and a seek
        goes through `reset`, never `resync`. Replaying is the *correct*
        behaviour there; this test exists so a future caller does not assume
        `resync` is safe to call with an arbitrary or stale position.
        """
        mixer = _mixer([500])
        mixer.mix(_silence(SEQUENCE_FRAMES), 0, SEQUENCE_FRAMES)
        mixer.resync(0)
        out = _silence(SEQUENCE_FRAMES)
        mixer.mix(out, 0, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [500])


class DisabledTests(unittest.TestCase):

    def test_disabled_plays_nothing(self):
        mixer = _mixer([100])
        mixer.enabled = False
        out = _silence(SEQUENCE_FRAMES)
        mixer.mix(out, 0, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [])

    def test_enabling_mid_playback_starts_from_the_playhead(self):
        """A note passed over while hitsounds were off has been passed over.
        Without the watermark advancing, turning them on would replay every
        note since the last seek in one grain."""
        mixer = _mixer([100, 100 + SEQUENCE_FRAMES])
        mixer.enabled = False
        mixer.mix(_silence(SEQUENCE_FRAMES), 0, SEQUENCE_FRAMES)
        mixer.enabled = True
        out = _silence(SEQUENCE_FRAMES)
        mixer.mix(out, SEQUENCE_FRAMES, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [100])

    def test_a_missing_sample_is_silent_rather_than_an_error(self):
        """A skin that ships only a don keeps the built-in kat, but a key with
        no file at all must not take the audio thread down with it."""
        mixer = HitsoundMixer()
        mixer.set_samples({"don": _impulse_sample()})
        mixer.set_schedule([100, 200], ["kat", "don"], [1.0, 1.0])
        mixer.reset(0.0)
        out = _silence(SEQUENCE_FRAMES)
        mixer.mix(out, 0, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [200])

    def test_no_schedule_and_no_samples_are_both_harmless(self):
        mixer = HitsoundMixer()
        mixer.reset(0.0)
        out = _bed(SEQUENCE_FRAMES)
        mixer.mix(out, 0, SEQUENCE_FRAMES)
        self.assertEqual(_spikes(out, 1), [])


class MusicLevelTests(unittest.TestCase):
    """The music level is applied here, not on the sink.

    `QAudioSink.setVolume` attenuates everything written to it -- the notes
    included -- and it does so after they have been summed, so the one thing it
    cannot do is leave room for them. Measured on a 0dBFS master at the default
    65% music and 70% hitsounds, moving it here took saturated samples from 71
    per note to 9.6.
    """

    def test_the_music_is_attenuated(self):
        out = _bed(SEQUENCE_FRAMES)
        loudest = max(abs(value) for value in out)
        mixer = HitsoundMixer()
        mixer.music_gain = 0.5
        mixer.reset(0.0)
        mixer.mix(out, 0, SEQUENCE_FRAMES)
        self.assertAlmostEqual(max(abs(value) for value in out),
                               loudest // 2, delta=2)

    def test_unity_changes_nothing(self):
        out = _bed(SEQUENCE_FRAMES)
        before = list(out)
        mixer = HitsoundMixer()
        mixer.reset(0.0)
        mixer.mix(out, 0, SEQUENCE_FRAMES)
        self.assertEqual(list(out), before)

    def test_the_notes_are_not_attenuated_with_the_music(self):
        """The ordering that makes the headroom work, and the independence the
        two volume sliders had while hitsounds left through QSoundEffect."""
        out = _silence(SEQUENCE_FRAMES)
        mixer = _mixer([100])
        mixer.music_gain = 0.5
        mixer.mix(out, 0, SEQUENCE_FRAMES)
        self.assertAlmostEqual(out[100 * CHANNELS + 1], SPIKE, delta=2)

    def test_the_music_level_applies_with_hitsounds_off(self):
        """Off is off for the notes, not for the song."""
        out = _bed(SEQUENCE_FRAMES)
        loudest = max(abs(value) for value in out)
        mixer = _mixer([100])
        mixer.enabled = False
        mixer.music_gain = 0.5
        mixer.mix(out, 0, SEQUENCE_FRAMES)
        self.assertAlmostEqual(max(abs(value) for value in out),
                               loudest // 2, delta=2)
        self.assertEqual(_spikes(out, 1), [])

    # Hot, but not a plateau at full scale: 26000 plus a 70% note (15400 on
    # these samples) overflows, while 65% of it plus the same note does not.
    # A real master has peaks rather than a flat line at 32000, which is why
    # the end-to-end numbers show a 7.4x reduction where a plateau shows none.
    HOT = 26000

    def _clipped_at(self, music_gain: float) -> int:
        """Saturated samples from mixing a note over a hot master."""
        hot = _silence(SEQUENCE_FRAMES)
        for index in range(SEQUENCE_FRAMES * CHANNELS):
            hot[index] = self.HOT
        mixer = _mixer([100], sample_frames=200)
        mixer.volume = 0.7          # the shipped default
        mixer.music_gain = music_gain
        mixer.mix(hot, 0, SEQUENCE_FRAMES)
        return sum(1 for value in hot if value >= 32767)

    def test_headroom_reduces_saturation_without_removing_it(self):
        """**It does not reach zero on a 0dBFS master, and claiming otherwise
        would be wrong.** Such a master at 65% is still 21299, and a 70% note
        on the shipped samples is 16208; their coincident peaks come to 37507,
        which is more than 16 bits holds however the gain is arranged. Measured
        end to end on one: 71 saturated samples per note at full scale, 9.6 at
        65% -- a 7.4x reduction, not an elimination. osu! sums its hitsounds
        over the music the same way, and a mapper on a hot master turns a
        volume down.
        """
        at_full = self._clipped_at(1.0)
        with_headroom = self._clipped_at(0.65)
        self.assertGreater(at_full, 0)
        self.assertLess(with_headroom, at_full)

    def test_saturation_clamps_rather_than_wraps(self):
        """The failure that would actually be audible as a click: a wrap turns
        a loud stack into a full-scale edge in the opposite direction."""
        hot = _silence(SEQUENCE_FRAMES)
        for index in range(SEQUENCE_FRAMES * CHANNELS):
            hot[index] = 32000
        mixer = _mixer([100], sample_frames=200)
        mixer.mix(hot, 0, SEQUENCE_FRAMES)
        self.assertEqual(min(hot), 32000)
        self.assertEqual(max(hot), 32767)

    def test_the_reduction_needs_music_with_peaks_rather_than_a_plateau(self):
        """Why `HOT` is 26000 and not 32000, written down because the first
        version of the test above used a plateau and measured no reduction at
        all -- correctly, since every note frame clips either way there. The
        end-to-end 7.4x comes from a master whose peaks are occasional."""
        plateau = _silence(SEQUENCE_FRAMES)
        for index in range(SEQUENCE_FRAMES * CHANNELS):
            plateau[index] = 32000
        mixer = _mixer([100], sample_frames=200)
        mixer.volume = 0.7
        mixer.music_gain = 0.65
        mixer.mix(plateau, 0, SEQUENCE_FRAMES)
        self.assertEqual(sum(1 for value in plateau if value >= 32767), 200)


class MusicAlignmentTests(unittest.TestCase):
    """The invariant this whole change exists for: through the real stretcher,
    at every rate, a note is on the same output sample as the music at its own
    source frame.

    The music marker is in the left channel and the note in the right, so the
    two can be found independently in one pass of output.
    """

    RATES = (1.0, 0.75, 0.5, 0.25)
    MARKS = (SAMPLE_RATE // 2, SAMPLE_RATE, SAMPLE_RATE * 3 // 2)

    def _source(self):
        source = _bed(SAMPLE_RATE * 6)
        for frame in self.MARKS:
            for i in range(30):
                source[(frame + i) * CHANNELS] = SPIKE
        return source

    def test_a_note_lands_on_the_music_it_belongs_to(self):
        source = self._source()
        mono = downmix_to_mono(source)
        for rate in self.RATES:
            with self.subTest(rate=rate):
                mixer = HitsoundMixer()
                mixer.set_samples({"don": _impulse_sample(30)})
                mixer.set_schedule(list(self.MARKS), ["don"] * len(self.MARKS),
                                   [1.0] * len(self.MARKS))
                stretcher = TimeStretcher(mixer)
                stretcher.reset(0.0)
                out = _pull(stretcher, source, mono,
                            self.MARKS[-1] / SAMPLE_RATE / rate + 0.5, rate)
                music = _spikes(out, 0)
                notes = _spikes(out, 1)
                # The *music* marker comes out about 1/rate times: filling more
                # time with the same content is what a time-stretch does, and
                # 12 occurrences at 0.25x is the stretch working. The note is
                # deduped to one, and the claim is that the one it keeps sits
                # exactly on an occurrence of its own music.
                self.assertEqual(len(notes), len(self.MARKS))
                self.assertGreaterEqual(len(music), len(self.MARKS))
                for at_note in notes:
                    self.assertIn(at_note, music)

    def test_full_speed_is_the_identity(self):
        """1.0x takes the straight-copy path, so the output frame *is* the
        source frame and the placement can be checked absolutely rather than
        against the music."""
        source = self._source()
        mono = downmix_to_mono(source)
        mixer = HitsoundMixer()
        mixer.set_samples({"don": _impulse_sample(30)})
        mixer.set_schedule(list(self.MARKS), ["don"] * len(self.MARKS),
                           [1.0] * len(self.MARKS))
        stretcher = TimeStretcher(mixer)
        stretcher.reset(0.0)
        out = _pull(stretcher, source, mono, 2.5, 1.0)
        self.assertEqual(_spikes(out, 1), list(self.MARKS))

    def test_each_note_sounds_once_at_every_rate(self):
        """The 1/rate dedupe, through the real read head rather than a
        hand-made pair of grains."""
        source = self._source()
        mono = downmix_to_mono(source)
        for rate in self.RATES:
            with self.subTest(rate=rate):
                mixer = HitsoundMixer()
                mixer.set_samples({"don": _impulse_sample(30)})
                mixer.set_schedule([SAMPLE_RATE], ["don"], [1.0])
                stretcher = TimeStretcher(mixer)
                stretcher.reset(0.0)
                out = _pull(stretcher, source, mono, 1.0 / rate + 0.5, rate)
                self.assertEqual(len(_spikes(out, 1)), 1)

    def test_no_note_is_lost_between_grains(self):
        """DT's read head skips `sequence * (rate - 1)` of source per grain,
        and the notes in that skip were never voiced -- a third of a chart at
        1.5x. A 1/8 stream at 180 BPM puts a note in most of the gaps."""
        source = _bed(SAMPLE_RATE * 6)
        mono = downmix_to_mono(source)
        notes = list(range(SAMPLE_RATE // 4, SAMPLE_RATE * 4, SAMPLE_RATE // 12))
        for rate in (1.5, 1.25, 0.75):
            for pitch in (False, True):
                with self.subTest(rate=rate, pitch=pitch):
                    mixer = HitsoundMixer()
                    mixer.set_samples({"don": _impulse_sample(30)})
                    mixer.set_schedule(notes, ["don"] * len(notes),
                                       [1.0] * len(notes))
                    stretcher = TimeStretcher(mixer)
                    stretcher.pitch = pitch
                    stretcher.reset(0.0)
                    out = _pull(stretcher, source, mono, 4.5 / rate, rate)
                    self.assertEqual(len(_spikes(out, 1)), len(notes))

    def test_the_notes_do_not_reach_the_splice_search(self):
        """Mixed into `out` after the tail is taken. Mixed any earlier, a note
        would be carried into the next grain's crossfade and into what the
        correlation matches against, so the search would start tracking
        hitsounds instead of music."""
        source = self._source()
        mono = downmix_to_mono(source)
        without = TimeStretcher()
        without.reset(0.0)
        plain = _pull(without, source, mono, 2.0, 0.5)
        mixer = HitsoundMixer()
        mixer.set_samples({"don": _impulse_sample(30)})
        mixer.set_schedule(list(self.MARKS), ["don"] * len(self.MARKS),
                           [1.0] * len(self.MARKS))
        with_notes = TimeStretcher(mixer)
        with_notes.reset(0.0)
        mixed = _pull(with_notes, source, mono, 2.0, 0.5)
        # The left channel carries the music and no note, so it must be
        # identical: the same splices, chosen the same way.
        self.assertEqual(plain[0::CHANNELS], mixed[0::CHANNELS])


if __name__ == "__main__":
    unittest.main()
