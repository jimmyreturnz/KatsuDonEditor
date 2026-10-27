"""Hitsounds: which notes sound, how loud, and which file each one plays.

The *placement* half of the question lives in `test_hitsound_mixing.py` now,
because that is where the sound is made: `HitsoundPlayer` hands a schedule to
the engine, and `audio_engine.HitsoundMixer` mixes it into the music stream.
What is left here is the chart half -- "circles only, and which of the four
samples", the section volume, and the forwarding -- none of which needs an
audio device.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui import gui
from osu_io.timing import TimingPoint
from model.hit_object import (
    HITSOUND_CLAP,
    HITSOUND_FINISH,
    TYPE_CIRCLE,
    TYPE_SLIDER,
    TYPE_SPINNER,
    HitObject,
)

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def circle(time_ms: int, hit_sound: int = 0) -> HitObject:
    return HitObject(x=256, y=192, time=time_ms, type=TYPE_CIRCLE, hit_sound=hit_sound)


def fake_slider(time_ms: int, hit_sound: int = 0) -> HitObject:
    """A drumroll of negative length -- every fake slider and every shiny."""
    return HitObject(
        x=256, y=192, time=time_ms, type=TYPE_SLIDER, hit_sound=hit_sound,
        extras=("L|356:192", "1", "-1"),
    )


class SampleChoiceTests(unittest.TestCase):
    def test_each_kind_of_circle_asks_for_its_own_sample(self):
        self.assertEqual(gui.hitsound_keys(circle(0)), ("normal",))
        self.assertEqual(gui.hitsound_keys(circle(0, HITSOUND_CLAP)), ("clap",))
        # A finisher is its small note's sound with the big one over it.
        self.assertEqual(gui.hitsound_keys(circle(0, HITSOUND_FINISH)), ("normal", "finish"))
        self.assertEqual(
            gui.hitsound_keys(circle(0, HITSOUND_FINISH | HITSOUND_CLAP)), ("clap", "whistle")
        )

    def test_nothing_that_is_not_a_circle_makes_a_sound(self):
        """Circles only is the whole rule: it silences every fake slider and
        every shiny (both are drumrolls) with no gimmick-specific special
        case, and it silences real drumrolls and spinners too."""
        self.assertFalse(gui.hitsound_keys(fake_slider(0)))
        self.assertFalse(gui.hitsound_keys(fake_slider(0, HITSOUND_FINISH)))
        self.assertFalse(gui.hitsound_keys(
            HitObject(x=256, y=192, time=0, type=TYPE_SPINNER, hit_sound=0)
        ))
        real_drumroll = HitObject(
            x=256, y=192, time=0, type=TYPE_SLIDER, hit_sound=0,
            extras=("L|356:192", "1", "120"),
        )
        self.assertFalse(gui.hitsound_keys(real_drumroll))

    def test_a_gimmick_structures_own_note_still_sounds(self):
        """A Don fake slider and a barline note each write a real circle the
        player actually hits -- silencing those would make playback lie."""
        self.assertEqual(gui.hitsound_keys(circle(10000)), ("normal",))

    def test_the_schedule_is_sorted_and_drops_the_silent_objects(self):
        times, keys, volumes = gui.hitsound_schedule([
            fake_slider(300),
            circle(200, HITSOUND_CLAP),
            circle(100),
            fake_slider(301),
        ])
        self.assertEqual(times, [100.0, 200.0])
        self.assertEqual(keys, ["normal", "clap"])
        # No timing points given, so nothing is quieter than as authored.
        self.assertEqual(volumes, [1.0, 1.0])

    def test_a_real_drumrolls_ticks_sound_and_a_fake_sliders_do_not(self):
        """Owner, 2026-10-10: a roll is heard the way the autoplay hits it --
        a don every tick, head to end. At SliderMultiplier 1.2, 1.0x and a
        400ms beat (150 BPM: 1/4 ticks), length 120 is one beat: ticks at 0,
        100, 200, 300, 400. At 120 BPM they are 1/8, and above 250 BPM 1/2."""
        roll = HitObject(x=256, y=192, time=1000, type=TYPE_SLIDER, hit_sound=0,
                         extras=("L|356:192", "1", "120"))
        points = [TimingPoint(time=0.0, beat_length=400.0, meter=4)]
        times, keys, _volumes = gui.hitsound_schedule(
            [roll, fake_slider(3000)], points, slider_multiplier=1.2)
        self.assertEqual(times, [1000.0, 1100.0, 1200.0, 1300.0, 1400.0])
        self.assertEqual(set(keys), {"normal"})
        for beat, spacing in ((500.0, 62.5), (200.0, 100.0)):
            points = [TimingPoint(time=0.0, beat_length=beat, meter=4)]
            times, _keys, _volumes = gui.hitsound_schedule([roll], points, slider_multiplier=1.2)
            self.assertEqual(times[1] - times[0], spacing, beat)

    def test_a_finisher_schedules_both_its_samples_on_one_millisecond(self):
        times, keys, _volumes = gui.hitsound_schedule([
            circle(100, HITSOUND_FINISH),
            circle(200, HITSOUND_FINISH | HITSOUND_CLAP),
        ])
        self.assertEqual(times, [100.0, 100.0, 200.0, 200.0])
        self.assertEqual(sorted(keys[:2]), ["finish", "normal"])
        self.assertEqual(sorted(keys[2:]), ["clap", "whistle"])


class SectionVolumeTests(unittest.TestCase):
    """A timing point's volume is osu!'s hitsound volume for the section it
    opens, and the Kiai and Sound Volume layer is the thing that edits it. If
    it does not reach the sample, that layer draws a graph of nothing."""

    @staticmethod
    def _point(time_ms, volume):
        return TimingPoint(time=float(time_ms), beat_length=500.0, volume=volume)

    def test_a_note_takes_the_volume_of_the_section_it_is_in(self):
        _times, _keys, volumes = gui.hitsound_schedule(
            [circle(1000), circle(2000), circle(3000)],
            [self._point(0, 100), self._point(1500, 40), self._point(2500, 0)],
        )
        self.assertEqual(volumes, [1.0, 0.4, 0.0])

    def test_a_point_exactly_on_a_note_applies_to_it(self):
        """osu! reads the sample volume from the point at or before the note,
        and mappers set the volume by stacking a line on the note itself."""
        _times, _keys, volumes = gui.hitsound_schedule(
            [circle(1000)], [self._point(0, 100), self._point(1000, 25)])
        self.assertEqual(volumes, [0.25])

    def test_a_note_before_the_first_point_plays_at_full(self):
        """There is no section to read a volume from, and silence would be a
        worse guess than the sample as authored."""
        _times, _keys, volumes = gui.hitsound_schedule(
            [circle(100)], [self._point(500, 20)])
        self.assertEqual(volumes, [1.0])

    def test_unsorted_points_still_resolve(self):
        """The forward merge assumes time order; documents do not guarantee it
        after an edit, so the schedule sorts rather than trusting."""
        _times, _keys, volumes = gui.hitsound_schedule(
            [circle(1000), circle(2000)],
            [self._point(1500, 30), self._point(0, 90)],
        )
        self.assertEqual(volumes, [0.9, 0.3])

    def test_the_volumes_line_up_with_the_sorted_times(self):
        """Parallel lists, and the notes get sorted -- so an out-of-order input
        must not pair a note with somebody else's volume."""
        times, keys, volumes = gui.hitsound_schedule(
            [circle(2000), circle(1000)],
            [self._point(0, 100), self._point(1500, 50)],
        )
        self.assertEqual(times, [1000.0, 2000.0])
        self.assertEqual(keys, ["normal", "normal"])
        self.assertEqual(volumes, [1.0, 0.5])


class _FakePlayer:
    """Records what the forwarder sends, so the chart half can be tested with
    no audio thread and no device."""

    def __init__(self) -> None:
        self.schedules: list[tuple] = []
        self.samples: list[dict] = []
        self.volumes: list[float] = []
        self.enabled: list[bool] = []
        self.offsets: list[float] = []

    def set_hitsound_schedule(self, times, keys, volumes) -> None:
        self.schedules.append((list(times), list(keys), list(volumes)))

    def set_hitsound_samples(self, paths) -> None:
        self.samples.append(dict(paths))

    def set_hitsound_volume(self, volume) -> None:
        self.volumes.append(volume)

    def set_hitsounds_enabled(self, enabled) -> None:
        self.enabled.append(enabled)

    def set_hitsound_offset_ms(self, offset_ms) -> None:
        self.offsets.append(offset_ms)


class ForwardingTests(unittest.TestCase):
    """`HitsoundPlayer` no longer makes a sound -- it decides which sample each
    note wants and hands the schedule to the engine, which mixes it into the
    music stream.

    The invariants the pool and firing-window tests used to pin have not gone
    away, they have moved to where the sound is now made:

    - each note exactly once, never twice -> `test_hitsound_mixing.DedupeTests`
    - nothing fired by a seek -> `test_hitsound_mixing.SeekTests`
    - a note on a boundary counted once -> `PlacementTests`
    - overlapping samples not cutting each other off, which is what the pool of
      eight existed for -> `LongSampleTests` and `PlacementTests`
    """

    def setUp(self) -> None:
        self.fake = _FakePlayer()
        self.player = gui.HitsoundPlayer(self.fake)

    def test_every_sample_is_resolved_at_construction(self):
        """Not lazily, and this is now safe to do eagerly: the old pools were
        32 QSoundEffect objects per window, which made every MainWindow cost
        more than the last. This is four paths in a dict."""
        self.assertEqual(len(self.fake.samples), 1)
        self.assertEqual(set(self.fake.samples[0]), set(gui.HITSOUND_SAMPLES))

    def test_the_schedule_is_forwarded_as_three_parallel_lists(self):
        self.player.set_schedule([circle(1000), circle(1100, HITSOUND_CLAP)])
        times, keys, volumes = self.fake.schedules[-1]
        self.assertEqual(times, [1000.0, 1100.0])
        self.assertEqual(keys, ["normal", "clap"])
        self.assertEqual(volumes, [1.0, 1.0])

    def test_enabling_and_disabling_reaches_the_engine(self):
        self.player.enabled = False
        self.assertEqual(self.fake.enabled[-1], False)
        self.assertFalse(self.player.enabled)
        self.player.enabled = True
        self.assertEqual(self.fake.enabled[-1], True)

    def test_the_volume_is_clamped_before_it_is_sent(self):
        self.player.set_volume(1.5)
        self.player.set_volume(-0.2)
        self.assertEqual(self.fake.volumes, [1.0, 0.0])

    def test_an_offset_change_re_sends_the_schedule(self):
        """The offset is baked into the schedule, because the schedule is
        integer source frames and that is where a shift belongs. Changing it
        without re-sending would leave it with no effect until the next edit."""
        self.player.set_schedule([circle(1000)])
        before = len(self.fake.schedules)
        self.player.offset_ms = -15
        self.assertEqual(self.fake.offsets[-1], -15)
        self.assertGreater(len(self.fake.schedules), before)

    def test_an_offset_set_before_any_schedule_is_still_applied(self):
        """The settings are read at startup, before a document is open."""
        self.player.offset_ms = 20
        self.player.set_schedule([circle(1000)])
        self.assertEqual(self.fake.offsets[-1], 20)
        self.assertEqual(self.fake.schedules[-1][0], [1000.0])

    def test_a_skin_sample_replaces_only_its_own_key(self):
        """A skin shipping only a don keeps the built-in kat rather than
        falling silent."""
        self.player.set_skin_sounds({"normal": "C:/skin/don.wav"})
        sent = self.fake.samples[-1]
        self.assertEqual(sent["normal"], "C:/skin/don.wav")
        self.assertIn("clap", sent)
        self.assertNotEqual(sent["clap"], "C:/skin/don.wav")

    def test_the_same_skin_twice_sends_nothing_new(self):
        """Every send tears down four decoders and rebuilds them, and
        `_apply_audio_settings` runs on every settings save."""
        before = len(self.fake.samples)
        self.player.set_skin_sounds({})
        self.assertEqual(len(self.fake.samples), before)


if __name__ == "__main__":
    unittest.main()
