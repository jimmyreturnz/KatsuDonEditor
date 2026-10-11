"""M5 gimmick editor: pairing index, base timing and the structure builders.

The structure assertions are deliberately written as the literal file lines a
placement produces. These structures are the whole feature -- a barline note is
*only* its red lines -- so a test that checked counts or ranges would pass while
drawing the wrong note.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chart import gimmick_session as gs
from model.hit_object import HITSOUND_CLAP, HITSOUND_FINISH, TYPE_CIRCLE, TYPE_SLIDER, HitObject
from osu_io.timing import TimingPoint, serialize_timing_point

# 180 BPM from 0, the base timing every case below is placed against.
BASE = [TimingPoint.uninherited_at(0, 180.0)]


def lines(points) -> list[str]:
    return [serialize_timing_point(point, "") for point in points]


class ConfigTests(unittest.TestCase):
    def test_a_real_fake_slider_length_is_rejected(self):
        """A length long enough to derive a duration is a real, hittable
        drumroll -- the opposite of the object the tool exists to place."""
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(fake_slider_length=1.0)
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(
                fake_slider_length=gs.FAKE_SLIDER_MAX_LENGTH * 2)

    def test_the_near_zero_positive_form_is_allowed(self):
        """`0.001` derives a duration of 0.0045ms -- no tick, nothing to hit.
        It is the form most maps in the wild actually use."""
        for length in (0.0, gs.FAKE_SLIDER_MAX_LENGTH, -0.0001):
            with self.subTest(length=length):
                self.assertEqual(
                    gs.GimmickConfig(fake_slider_length=length).fake_slider_length,
                    length,
                )

    def test_defaults_are_the_agreed_ones(self):
        config = gs.GimmickConfig()
        self.assertEqual(config.gimmick_bpm, 60000.0)
        self.assertEqual(config.spacing_ms, 1)
        # Kat's three independently configurable pairs default to what the
        # old derived formula (n, n+2, n+4 with n=1) used to produce, so a
        # map built against today's shipped defaults sees no change.
        self.assertEqual(config.kat_spacing1_ms, 1)
        self.assertEqual(config.kat_spacing2_ms, 3)
        self.assertEqual(config.kat_spacing3_ms, 5)
        self.assertEqual(config.fake_slider_length, -0.001)
        self.assertEqual(config.red_line_offset_ms, 0)

    def test_a_spacing_below_one_is_rejected(self):
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(spacing_ms=0)
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(kat_spacing1_ms=0)
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(kat_spacing2_ms=0)
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(kat_spacing3_ms=0)

    def test_duplicate_kat_spacings_are_rejected(self):
        """Two equal values would write two red lines onto one millisecond,
        where only the first is meaningful -- a quietly thinner note than the
        three spin boxes promised."""
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(kat_spacing1_ms=3, kat_spacing2_ms=3)
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(kat_spacing2_ms=5, kat_spacing3_ms=5)


class BarlineStructureTests(unittest.TestCase):
    def test_don_is_one_gimmick_line_and_a_mirrored_pair(self):
        points, notes = gs.barline_note(
            10000, BASE, gs.GimmickConfig(mirror_don_lines=True), kind="don",
        )
        # The bars are drawn for a note the player actually hits, so one goes
        # on the snap unless place_notes says otherwise.
        self.assertEqual([note.time for note in notes], [10000])
        self.assertEqual(lines(points), [
            "9999,333.3333333333333,4,0,0,100,1,0",
            "10000,1,4,0,0,100,1,0",
            "10001,333.3333333333333,4,0,0,100,1,0",
        ])

    def test_place_notes_off_writes_the_bars_and_nothing_else(self):
        """A gimmick that only wants the barlines as scenery."""
        points, notes = gs.barline_note(
            10000, BASE, gs.GimmickConfig(place_notes=False, mirror_don_lines=True), kind="don",
        )
        self.assertEqual(notes, [])
        self.assertEqual(len(points), 3, "the bars are unaffected")

    def test_kat_mirrors_n_n2_n4_around_the_gimmick_line(self):
        points, _ = gs.barline_note(10000, BASE, gs.GimmickConfig(), kind="kat")
        self.assertEqual([point.time for point in points], [
            9995.0, 9997.0, 9999.0, 10000.0, 10001.0, 10003.0, 10005.0,
        ])
        # Seven uninherited points, so seven barlines: the gimmick line plus
        # six restores whose only job is to draw a bar each.
        self.assertTrue(all(point.uninherited for point in points))
        self.assertEqual(sum(1 for point in points if point.bpm == 60000.0), 1)

    def test_dons_own_spacing_widens_it_without_touching_kat(self):
        """Don's spacing is `spacing_ms` alone now -- it no longer feeds a
        formula Kat's pairs are derived from."""
        config = gs.GimmickConfig(spacing_ms=3, mirror_don_lines=True)
        points, _ = gs.barline_note(10000, BASE, config, kind="don")
        self.assertEqual([point.time for point in points], [9997.0, 10000.0, 10003.0])

    def test_kats_three_pairs_are_independently_configurable(self):
        """Each pair lands exactly where its own field asks, free of the old
        n/n+2/n+4 progression -- so hand-layering several structures on one
        millisecond region no longer forces the three pairs' widths apart in
        lockstep."""
        config = gs.GimmickConfig(kat_spacing1_ms=2, kat_spacing2_ms=6, kat_spacing3_ms=9)
        points, _ = gs.barline_note(10000, BASE, config, kind="kat")
        self.assertEqual([point.time for point in points], [
            9991.0, 9994.0, 9998.0, 10000.0, 10002.0, 10006.0, 10009.0,
        ])

    def test_restores_take_the_base_bpm_in_force_not_the_first_one(self):
        base = [TimingPoint.uninherited_at(0, 180.0), TimingPoint.uninherited_at(5000, 240.0)]
        points, _ = gs.barline_note(10000, base, gs.GimmickConfig(), kind="don")
        restores = [point for point in points if point.time != 10000]
        for point in restores:
            self.assertAlmostEqual(point.bpm, 240.0)

    def test_gimmick_bpm_is_configurable(self):
        config = gs.GimmickConfig(gimmick_bpm=30000.0)
        points, _ = gs.barline_note(10000, BASE, config, kind="don")
        centre = next(point for point in points if point.time == 10000)
        self.assertAlmostEqual(centre.bpm, 30000.0)

    def test_a_typed_restore_bpm_replaces_the_charts_own(self):
        """`gimmick_bpm` is how invisible the note is; `restore_bpm` is how
        fast the chart runs afterwards. They were one number."""
        config = gs.GimmickConfig(gimmick_bpm=30000.0, restore_bpm=90.0)
        points, _ = gs.barline_note(10000, BASE, config, kind="don")
        centre = next(point for point in points if point.time == 10000)
        restores = [point for point in points if point.time != 10000]
        self.assertAlmostEqual(centre.bpm, 30000.0)
        self.assertTrue(restores)
        for point in restores:
            self.assertAlmostEqual(point.bpm, 90.0)

    def test_no_restore_bpm_is_still_the_charts_own(self):
        points, _ = gs.barline_note(10000, BASE, gs.GimmickConfig(), kind="don")
        for point in (p for p in points if p.time != 10000):
            self.assertAlmostEqual(point.bpm, 180.0)

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(gs.GimmickConfigError):
            gs.barline_note(0, BASE, gs.GimmickConfig(), kind="slider")


class FakeSliderTests(unittest.TestCase):
    def test_a_plain_fake_slider_writes_one_line_at_the_slider_not_the_gimmick_bpm(self):
        """No note, so nothing to hide -- a plain fake slider is one red line
        on the object itself, at the chart's own BPM, never the 60000 squash."""
        points, notes = gs.fake_slider(10000, BASE, gs.GimmickConfig())
        # Effects 8: a fake slider's line omits the barline it would otherwise
        # draw. On by default -- see GimmickConfig.omit_barline.
        # +2, not +1: the shiny note owns +1 (see DEFAULT_SHINY_OFFSET_MS).
        self.assertEqual(lines(points), [
            "10002,333.3333333333333,4,0,0,100,1,8",
        ])
        self.assertEqual(len(notes), 1)
        self.assertNotEqual(points[0].bpm, gs.DEFAULT_GIMMICK_BPM)

    def test_omit_barline_can_be_switched_off(self):
        points, _ = gs.fake_slider(10000, BASE, gs.GimmickConfig(omit_barline=False))
        self.assertEqual(lines(points), [
            "10002,333.3333333333333,4,0,0,100,1,0",
        ])

    def test_a_kat_fake_slider_is_the_big_one(self):
        """A drumroll is yellow whatever is under it, so size is the only thing
        it can say about which button the structure means."""
        _, don = gs.fake_slider(10000, BASE, gs.GimmickConfig(), kind="don")
        _, kat = gs.fake_slider(10000, BASE, gs.GimmickConfig(), kind="kat")
        _, plain = gs.fake_slider(10000, BASE, gs.GimmickConfig())
        self.assertTrue(kat[1].is_finisher)
        self.assertFalse(don[1].is_finisher)
        self.assertFalse(plain[0].is_finisher)

    def test_note_is_a_slider_with_a_negative_length(self):
        # +2: every object this layer writes sits on its restore line, which is
        # what lets the offset say whether it is a fake slider or a shiny.
        _, notes = gs.fake_slider(10000, BASE, gs.GimmickConfig())
        self.assertEqual(notes[0].to_line(""), "256,192,10002,2,0,L|356:192,1,-0.001,0:0:0:0:")
        self.assertTrue(notes[0].is_slider)
        self.assertLess(notes[0].length, 0)

    def test_don_and_kat_write_the_note_the_button_names(self):
        """The note on the snap is a real don or kat. The tools' small/big
        reading lives in the hover preview -- a fake slider is a drumroll and
        has no don/kat of its own to carry it."""
        _, don = gs.fake_slider(10000, BASE, gs.GimmickConfig(), kind="don")
        _, kat = gs.fake_slider(10000, BASE, gs.GimmickConfig(), kind="kat")
        self.assertEqual(don[0].note_kind, "don")
        self.assertEqual(kat[0].note_kind, "kat")
        # ...and the fake slider itself follows each of them.
        self.assertTrue(don[1].is_slider)
        self.assertTrue(kat[1].is_slider)

    def test_a_don_puts_the_gimmick_line_on_the_note_not_on_the_slider(self):
        """Only the note is inside the squashed section; the slider sits on the
        restore line, so it is drawn at the chart's own BPM."""
        config = gs.GimmickConfig(fake_slider_offset_ms=3)
        points, notes = gs.fake_slider(10000, BASE, config, kind="don")

        self.assertEqual([point.time for point in points], [10000.0, 10000.0, 10003.0])
        self.assertEqual(points[0].bpm, config.gimmick_bpm)
        self.assertAlmostEqual(points[2].bpm, 180.0)
        self.assertEqual([note.time for note in notes], [10000, 10003])

    def test_a_don_writes_three_points_squash_first(self):
        """The squash (gimmick BPM), then the SV that finishes taking the note
        off screen, then the restore the fake slider sits on -- in that order,
        because the green line must sort after the red one sharing its
        millisecond."""
        points, _ = gs.fake_slider(10000, BASE, gs.GimmickConfig(), kind="don")
        self.assertEqual(len(points), 3)
        self.assertTrue(points[0].uninherited)
        self.assertAlmostEqual(points[0].bpm, gs.DEFAULT_GIMMICK_BPM)
        self.assertEqual(points[0].time, 10000.0)
        self.assertFalse(points[1].uninherited)
        self.assertEqual(points[1].time, 10000.0)
        self.assertTrue(points[2].uninherited)
        self.assertEqual(points[2].time, 10002.0)
        self.assertNotAlmostEqual(points[2].bpm, gs.DEFAULT_GIMMICK_BPM)

    def test_fake_slider_bpm_multiplier_retimes_only_the_restore_line(self):
        """It governs the line the fake slider itself sits on -- never the
        squash, which stays at `gimmick_bpm` because hiding the note is the
        whole point of it."""
        config = gs.GimmickConfig(fake_slider_bpm_multiplier=0.5)
        points, _ = gs.fake_slider(10000, BASE, config, kind="don")
        self.assertAlmostEqual(points[0].bpm, config.gimmick_bpm)
        self.assertAlmostEqual(points[2].bpm, 90.0)  # 180 * 0.5

        plain_points, _ = gs.fake_slider(10000, BASE, config)
        self.assertAlmostEqual(plain_points[0].bpm, 90.0)

    def test_restore_bpm_outranks_the_multiplier_on_every_slider_line(self):
        """A typed BPM is an absolute, so the multiplier -- which only ever
        scaled the chart's own -- has nothing left to scale."""
        config = gs.GimmickConfig(fake_slider_bpm_multiplier=0.5, restore_bpm=75.0)
        points, _ = gs.fake_slider(10000, BASE, config, kind="don")
        self.assertAlmostEqual(points[0].bpm, config.gimmick_bpm)
        self.assertAlmostEqual(points[2].bpm, 75.0)

        plain, _ = gs.fake_slider(10000, BASE, config)
        self.assertAlmostEqual(plain[0].bpm, 75.0)

    def test_a_shiny_keeps_its_own_multiplier(self):
        """Its line sits on the note rather than restoring anything, so the
        restore dial deliberately does not reach it."""
        config = gs.GimmickConfig(shiny_bpm_multiplier=2.0, restore_bpm=75.0)
        points, _ = gs.fake_slider(10000, BASE, config, shiny=True)
        self.assertAlmostEqual(points[0].bpm, 360.0)  # 180 * 2

    def test_a_restore_bpm_of_zero_or_less_is_refused(self):
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(restore_bpm=0.0)

    def test_a_plain_slider_can_hand_the_chart_bpm_back_one_ms_later(self):
        """Without it the slider's line governs the rest of the map: a red
        line's BPM is its scroll speed. The second line a mapper would add by
        hand is the one the Red Line tool refuses, since the slider's own line
        already owns that millisecond."""
        config = gs.GimmickConfig(restore_bpm=90.0, fake_slider_restore_after=True)
        points, notes = gs.fake_slider(10000, BASE, config)
        slider_at = 10000 + config.fake_slider_offset_ms
        self.assertEqual([round(p.time) for p in points],
                         [slider_at, slider_at + gs.FAKE_SLIDER_RESTORE_OFFSET_MS])
        self.assertAlmostEqual(points[0].bpm, 90.0)
        self.assertAlmostEqual(points[1].bpm, 180.0)  # the chart's own
        self.assertEqual(round(notes[0].time), slider_at)

    def test_off_it_is_still_the_single_line_it_always_was(self):
        config = gs.GimmickConfig(fake_slider_restore_after=False)
        points, _ = gs.fake_slider(10000, BASE, config)
        self.assertEqual(len(points), 1)

    def test_no_line_at_all_means_no_restore_either(self):
        """The restore restores what the line above it changed; with no line
        there is nothing to hand back, and writing one would retime the chart
        for no reason."""
        config = gs.GimmickConfig(
            fake_slider_red_line=False, fake_slider_restore_after=True)
        points, _ = gs.fake_slider(10000, BASE, config)
        self.assertEqual(points, [])

    def test_a_don_stacks_the_gimmick_sv_on_its_red_line(self):
        """The green line comes *after* the red one sharing the millisecond:
        osu! resolves a tie by file order, so the other way round the red line's
        own reset to 1.0x would win."""
        config = gs.GimmickConfig(fake_slider_sv=12.0)
        points, _ = gs.fake_slider(10000, BASE, config, kind="kat")

        self.assertTrue(points[0].uninherited)
        self.assertFalse(points[1].uninherited)
        self.assertEqual(points[1].time, 10000.0)
        self.assertAlmostEqual(points[1].sv_multiplier, 12.0)

    def test_a_plain_fake_slider_carries_no_gimmick_sv(self):
        """It has no note of its own to hide -- the slider *is* the object,
        and there is nothing to squash, so there is only the one line."""
        points, _ = gs.fake_slider(10000, BASE, gs.GimmickConfig())
        self.assertEqual(len(points), 1)
        self.assertTrue(all(point.uninherited for point in points))

    def test_configured_length_is_written_verbatim(self):
        config = gs.GimmickConfig(fake_slider_length=-0.0001)
        _, notes = gs.fake_slider(10000, BASE, config)
        self.assertEqual(notes[0].extras[2], "-0.0001")

    def test_placement_offset_does_not_apply(self):
        """The offset belongs to the plain red-line tool alone."""
        config = gs.GimmickConfig(red_line_offset_ms=-3)
        points, notes = gs.fake_slider(10000, BASE, config)
        self.assertEqual([point.time for point in points], [10002.0])
        self.assertEqual(notes[0].time, 10002)


class AntiBarlineTests(unittest.TestCase):
    """`barline_note` in negative: a wall of bars with the notes as the gaps.

    80 BPM -- a 750ms beat -- because that is the tempo of the reference
    section (`mekurume [eclosion]` 1:52.5-2:15.7) every default here was
    measured off, so the literal lines below are the ones that map writes.
    """

    BASE = [TimingPoint.uninherited_at(0, 80.0)]

    def _circle(self, time_ms, *, kat=False, big=False):
        hit_sound = (HITSOUND_CLAP if kat else 0) | (HITSOUND_FINISH if big else 0)
        return HitObject(x=256, y=192, time=time_ms, type=TYPE_CIRCLE, hit_sound=hit_sound)

    def _wall(self, points):
        """Just the wall ticks -- the note lines are the two extremes of BPM."""
        return [point.time for point in points if point.meter == 4 and point.beat_length > 2]

    def test_a_note_is_a_squash_and_a_restore_both_omitting_the_barline(self):
        """The whole subtlety: each is a red line, and without the flag each
        would stamp a bar in the middle of the slit it exists to open."""
        points = gs.anti_barline(
            [self._circle(1000)], 1000, 1100, self.BASE, gs.GimmickConfig(),
        )
        own = [p for p in points if p.time in (1000.0, 1001.0)]
        self.assertEqual(lines(own), [
            "1000,1,4,0,0,100,1,8",
            "1001,750,999,0,0,100,1,8",
        ])

    def test_the_wall_carries_the_charts_own_bpm_by_default(self):
        """The line that changes nothing but where the bars fall. A BPM of its
        own is the scroll effect, and is asked for rather than assumed."""
        points = gs.anti_barline(
            [self._circle(1000)], 1000, 1100, self.BASE, gs.GimmickConfig(),
        )
        # 1002 and 1023 are the don's own two-tick slit; 1044 is where the
        # wall actually starts.
        wall = [p for p in points if p.time > 1030]
        self.assertEqual(lines(wall)[0], "1044,750,4,0,0,100,1,0")

    def test_a_custom_wall_bpm_is_what_packs_the_bars(self):
        """`2250,4` over a 750ms beat is the reference: a third of the BPM, so
        a third of the scroll speed, so the same lines a third as far apart --
        and `beat_length x meter` = 9000ms against the tick spacing, so every
        line still emits exactly one barline and never a second."""
        config = gs.GimmickConfig(anti_lines_per_beat=72, anti_wall_bpm=80.0 / 3.0)
        points = gs.anti_barline([self._circle(1000)], 1000, 1100, self.BASE, config)
        wall = [p for p in points if p.time > 1020]
        self.assertEqual(lines(wall)[0], "1023,2250,4,0,0,100,1,0")

    def test_the_wall_ticks_a_thirty_sixth_of_a_beat_apart(self):
        points = gs.anti_barline(
            [self._circle(1000)], 1000, 2000, self.BASE, gs.GimmickConfig(),
        )
        # Past the note only: its slit is a hole in this spacing, and which
        # side of the note that hole falls on depends on the slit's width.
        wall = [t for t in self._wall(points) if t > 1000]
        gaps = [b - a for a, b in zip(wall, wall[1:])]
        # 750/36 is 20.833, so whole-millisecond ticks alternate 20 and 21
        # around it rather than drifting off it -- the accumulator is
        # fractional and only the emitted value is rounded.
        self.assertEqual(set(gaps), {20.0, 21.0})
        self.assertAlmostEqual(sum(gaps) / len(gaps), 750 / 36, delta=0.05)

    def test_no_wall_tick_lands_on_a_notes_own_lines(self):
        """A tick on a note's own millisecond would be a second uninherited
        point there, and osu! honours only the first in file order -- so one of
        the two would silently do nothing. That is what the 2ms phase buys, and
        it holds for every note in the range, not only the one it is measured
        from: the notes are on the beat grid and the wall is an odd fraction of
        it, so the two never coincide."""
        notes = [self._circle(1000), self._circle(1750, kat=True), self._circle(2500)]
        points = gs.anti_barline(notes, 1000, 3000, self.BASE, gs.GimmickConfig())
        wall = set(self._wall(points))
        for note in notes:
            self.assertNotIn(float(note.time), wall)
            self.assertNotIn(float(note.time + gs.ANTI_RESTORE_OFFSET_MS), wall)

    def test_the_wall_grid_is_anchored_on_the_first_note(self):
        points = gs.anti_barline(
            [self._circle(1000)], 1000, 2000, self.BASE, gs.GimmickConfig(),
        )
        anchor = 1000 + gs.ANTI_WALL_ANCHOR_OFFSET_MS
        for tick in self._wall(points):
            steps = (tick - anchor) / (750 / 36)
            self.assertAlmostEqual(steps, round(steps), delta=0.5)

    def test_every_wall_line_carries_a_green_handle_after_it(self):
        """The SV (barlines) layer matches its green lines by exact
        millisecond, so a wall with none had a thousand barlines whose speed
        could not be shown, dragged or swept. After, never before: osu!
        resolves a shared timestamp by file order and the red line has just
        reset SV to 1.0x."""
        points = gs.anti_barline(
            [self._circle(1000)], 1000, 1200, self.BASE, gs.GimmickConfig(),
        )
        wall = self._wall(points)
        self.assertTrue(wall)
        for at in wall:
            kinds = [
                "red" if p.uninherited else "green"
                for p in points if p.time == at
            ]
            self.assertEqual(kinds, ["red", "green"], at)

    def test_the_handle_carries_the_charts_own_sv(self):
        """Not a flat 1.0x: a wall drawn across a section the map had sped up
        would otherwise silently flatten it for the length of the section."""
        base = [
            TimingPoint.uninherited_at(0, 80.0),
            TimingPoint.inherited_at(0, 1.4),
        ]
        points = gs.anti_barline(
            [self._circle(1000)], 1000, 1200, base, gs.GimmickConfig(),
        )
        greens = [p for p in points if p.inherited]
        self.assertTrue(greens)
        self.assertTrue(all(p.sv_multiplier == 1.4 for p in greens))

    def test_a_notes_own_lines_get_no_handle(self):
        """They are not barlines -- both carry omit-first-barline precisely so
        they draw none -- so nothing about them is the wall's speed."""
        points = gs.anti_barline(
            [self._circle(1000)], 1000, 1200, self.BASE, gs.GimmickConfig(),
        )
        for at in (1000.0, 1000.0 + gs.ANTI_RESTORE_OFFSET_MS):
            self.assertEqual(
                [p for p in points if p.time == at and p.inherited], [],
            )

    def test_the_slit_width_is_the_note_colour(self):
        """The note itself is invisible, so the hole is the only thing left to
        read a colour off: don and kat each leave out their own tick count."""
        config = gs.GimmickConfig()
        for kat, expected in ((False, config.anti_don_ticks), (True, config.anti_kat_ticks)):
            with self.subTest(kat=kat):
                notes = [self._circle(1000), self._circle(1750, kat=kat)]
                wall = self._wall(gs.anti_barline(notes, 1000, 2500, self.BASE, config))
                # The gap the second note opened, in whole ticks.
                before = max(t for t in wall if t < 1750)
                after = min(t for t in wall if t > 1750)
                self.assertEqual(round((after - before) / (750 / 36)) - 1, expected)

    def test_the_slit_is_centred_on_its_note(self):
        """Its own two lines sit in the middle of the hole, which is what the
        omit-first-barline flag on both of them is for."""
        notes = [self._circle(1000), self._circle(1750, kat=True)]
        wall = self._wall(gs.anti_barline(notes, 1000, 2500, self.BASE, gs.GimmickConfig()))
        before = [t for t in wall if t < 1750][-1]
        after = [t for t in wall if t > 1750][0]
        self.assertAlmostEqual(1750 - before, after - 1750, delta=750 / 36)

    def test_a_finisher_is_left_alone(self):
        """No slit and no squash: the reference section marks its downbeats
        some other way, and the user knows which notes those are."""
        notes = [self._circle(1000), self._circle(1750, big=True)]
        points = gs.anti_barline(notes, 1000, 2500, self.BASE, gs.GimmickConfig())
        self.assertEqual([p.time for p in points if p.beat_length == 1], [1000.0])
        wall = self._wall(points)
        self.assertTrue(
            any(abs(t - 1750) < 750 / 36 for t in wall),
            "the wall runs straight through a finisher",
        )

    def test_a_drumroll_and_a_spinner_are_left_alone(self):
        """A body that outlasts a slit has nothing to be a slit of."""
        roll = HitObject(
            x=256, y=192, time=1750, type=TYPE_SLIDER, hit_sound=0,
            extras=("L|624:192", "1", "800"),
        )
        points = gs.anti_barline(
            [self._circle(1000), roll], 1000, 2500, self.BASE, gs.GimmickConfig(),
        )
        self.assertEqual([p.time for p in points if p.beat_length == 1], [1000.0])

    def test_nothing_at_all_without_a_convertible_note(self):
        big = self._circle(1000, big=True)
        self.assertEqual(gs.anti_barline([big], 1000, 2000, self.BASE, gs.GimmickConfig()), [])
        self.assertEqual(gs.anti_barline([], 1000, 2000, self.BASE, gs.GimmickConfig()), [])

    def test_the_four_numbers_are_configurable(self):
        config = gs.GimmickConfig(
            anti_lines_per_beat=18, anti_wall_bpm=40.0,
            anti_don_ticks=1, anti_kat_ticks=3,
        )
        points = gs.anti_barline(
            [self._circle(1000)], 1000, 2000, self.BASE, config,
        )
        wall = [p for p in points if p.meter == 4 and p.beat_length > 2]
        # 750/18 = 41.667ms apart, and 40 BPM is a beat length of 1500.
        # Measured clear of the note, whose slit widens one gap by design.
        self.assertEqual(wall[0].beat_length, 1500.0)
        later = [p.time for p in wall if p.time > 1200]
        self.assertIn(round(later[1] - later[0]), (41, 42))

    def test_a_wall_bpm_of_zero_or_less_is_refused(self):
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(anti_wall_bpm=0.0)

    def test_a_density_that_cannot_be_told_apart_in_whole_ms_collapses(self):
        """Two ticks under a millisecond apart round together, and only the
        first of two uninherited points on a millisecond ever counted."""
        config = gs.GimmickConfig(anti_lines_per_beat=5000)
        points = gs.anti_barline(
            [self._circle(1000)], 1000, 1200, self.BASE, config,
        )
        wall = self._wall(points)
        self.assertEqual(len(wall), len(set(wall)))

    def test_every_note_has_sheet_on_both_sides_of_it(self):
        """A note is a hole in a sheet, so it needs sheet either side. The wall
        used to be anchored *at* the first note, which left that note the
        leading edge of the sheet rather than anything travelling in it -- and
        the last note lost its trailing bars the same way, since a drag
        naturally ends on it. Dragged exactly note-to-note, the worst case."""
        notes = [self._circle(1000), self._circle(1750, kat=True), self._circle(2500)]
        wall = self._wall(gs.anti_barline(notes, 1000, 2500, self.BASE, gs.GimmickConfig()))
        for note in notes:
            with self.subTest(note=note.time):
                self.assertTrue([t for t in wall if t < note.time], "no bars before it")
                self.assertTrue([t for t in wall if t > note.time], "no bars after it")

    def test_the_wall_fills_the_whole_dragged_range(self):
        """Dragged wider than the notes, the lead-in is the drag's, not a
        fixed amount: anchoring at the first note threw away everything the
        user had selected before it."""
        wall = self._wall(gs.anti_barline(
            [self._circle(2000)], 1000, 3000, self.BASE, gs.GimmickConfig(),
        ))
        self.assertLess(min(wall), 1050)
        self.assertGreater(max(wall), 2950)

    def test_it_reaches_past_the_edge_notes_by_one_slit(self):
        """The floor when the drag starts and ends on the notes themselves:
        half a slit for the hole, and as much again beyond for the bars the
        hole is in. Bounded by the slit's own width rather than a fixed
        number of milliseconds, so it scales with the wall it is part of."""
        notes = [self._circle(1000), self._circle(2500)]
        step = 750 / 36
        # A 1-tick slit reaches 2: its nearer half is the whole tick, and a bar
        # has to be left beyond it.
        for ticks, reach in ((1, 2), (2, 2), (4, 4)):
            with self.subTest(ticks=ticks):
                config = gs.GimmickConfig(anti_don_ticks=ticks)
                wall = self._wall(gs.anti_barline(notes, 1000, 2500, self.BASE, config))
                self.assertGreaterEqual(min(wall), 1000 - reach * step - 1)
                self.assertLessEqual(max(wall), 2500 + reach * step + 1)
                self.assertLess(min(wall), 1000)
                self.assertGreater(max(wall), 2500)


class VisibleNoteTests(unittest.TestCase):
    """`hide_note` off: the structure, drawn around a note you can still see.

    Every one of these gimmicks hides its note in the wild, so hiding stays the
    default -- but the squash is one line, and swapping it for the chart's own
    BPM with the barline detached leaves the rest of the structure alone.
    """

    BASE = [TimingPoint.uninherited_at(0, 120.0)]

    def test_a_barline_note_keeps_its_bars_and_shows_the_note(self):
        config = gs.GimmickConfig(hide_note=False, mirror_don_lines=True)
        points, notes = gs.barline_note(1000, self.BASE, config, kind="don")
        own = [p for p in points if p.time == 1000.0]
        self.assertEqual(len(own), 1)
        self.assertAlmostEqual(own[0].bpm, 120.0, places=6)
        # Detached, or the line stamps a bar over the note it is showing.
        self.assertTrue(own[0].omit_first_barline)
        # The bars either side are the structure and are untouched.
        self.assertEqual(
            [p.time for p in points if p.time != 1000.0],
            [1000.0 - config.spacing_ms, 1000.0 + config.spacing_ms],
        )
        self.assertEqual([n.time for n in notes], [1000])

    def test_hiding_is_still_the_default(self):
        points, _notes = gs.barline_note(1000, self.BASE, gs.GimmickConfig(), kind="don")
        own = next(p for p in points if p.time == 1000.0)
        self.assertAlmostEqual(own.bpm, gs.DEFAULT_GIMMICK_BPM, places=3)
        self.assertFalse(own.omit_first_barline)

    def test_a_visible_fake_slider_note_drops_the_gimmick_sv_and_the_line(self):
        """`fake_slider_sv` exists to take an already-squashed note the rest of
        the way off screen, so leaving it in would fling the visible note off
        the screen the red line was just told to keep it on.

        With the squash gone the whole green line goes: it used to restate the
        chart's own SV instead, and this layer no longer carries that at all.
        """
        config = gs.GimmickConfig(hide_note=False)
        points, _notes = gs.fake_slider(1000, self.BASE, config, kind="don")
        red = next(p for p in points if p.time == 1000.0 and p.uninherited)
        self.assertAlmostEqual(red.bpm, 120.0, places=6)
        self.assertTrue(red.omit_first_barline)
        self.assertEqual(
            [p for p in points if p.time == 1000.0 and p.inherited], [],
            "a visible note's structure carries no green line at all",
        )

    def test_a_hidden_fake_slider_note_is_unchanged(self):
        config = gs.GimmickConfig()
        points, _notes = gs.fake_slider(1000, self.BASE, config, kind="don")
        red = next(p for p in points if p.time == 1000.0 and p.uninherited)
        green = next(p for p in points if p.time == 1000.0 and p.inherited)
        self.assertAlmostEqual(red.bpm, gs.DEFAULT_GIMMICK_BPM, places=3)
        self.assertAlmostEqual(green.sv_multiplier, config.fake_slider_sv, places=6)


class HiddenAntiBarlineTests(unittest.TestCase):
    """`mew`'s `Nbt-Hwt [The Pharaoh's Curse]` 1:56.9-2:07.3 is the reference.

    185 BPM, because that is the tempo the section is written at, and the
    defaults are the numbers that map uses.
    """

    BASE = [TimingPoint.uninherited_at(0, 185.0)]

    def _circle(self, time_ms, *, kat=False, big=False):
        hit_sound = (HITSOUND_CLAP if kat else 0) | (HITSOUND_FINISH if big else 0)
        return HitObject(x=256, y=192, time=time_ms, type=TYPE_CIRCLE, hit_sound=hit_sound)

    def _at(self, points, time_ms):
        return [p for p in points if p.time == time_ms]

    def test_a_note_is_four_lines_in_the_reference_shape(self):
        """Hide, wall, colour, restore -- and the hide line is the only one of
        the two red lines that omits its barline, because the wall line's bar
        is the first bar of the resumed sheet."""
        points = gs.hidden_anti_barline(
            [self._circle(10000)], 9000, 11000, self.BASE, gs.GimmickConfig(),
        )
        own = [p for p in points if 10000 <= p.time <= 10041]
        self.assertEqual(lines(own), [
            "10000,0.900009000090001,999,0,0,100,1,8",
            "10001,4.860267314702309,1,0,0,100,1,0",
            "10001,-9900,4,0,0,100,0,0",
            "10041,-10000,4,0,0,100,0,0",
        ])

    def test_kat_opens_the_wider_slit(self):
        """The slit is the SV excess times how far away it still is, so a Kat
        1% further above the wall than a Don opens three times the gap."""
        don = gs.hidden_anti_barline(
            [self._circle(10000)], 9000, 11000, self.BASE, gs.GimmickConfig(),
        )
        kat = gs.hidden_anti_barline(
            [self._circle(10000, kat=True)], 9000, 11000, self.BASE, gs.GimmickConfig(),
        )

        def colour_sv(points):
            return next(p.sv_multiplier for p in points if p.time == 10001 and p.inherited)

        config = gs.GimmickConfig()
        self.assertLess(config.hidden_base_sv, colour_sv(don))
        self.assertLess(colour_sv(don), colour_sv(kat))

    def test_the_wall_starts_a_beat_before_the_first_note(self):
        """A note is a hole in a sheet, so it needs sheet in front of it -- and
        a drag naturally starts on the first note."""
        points = gs.hidden_anti_barline(
            [self._circle(10000)], 10000, 11000, self.BASE, gs.GimmickConfig(),
        )
        self.assertEqual(lines(points)[:2], [
            "9676,4.860267314702309,1,0,0,100,1,0",
            "9676,-10000,4,0,0,100,0,0",
        ])

    def test_the_wall_is_closed_after_the_range(self):
        """Its last line has meter 1 and no end of its own, so without this the
        sheet runs on to the map's next red line -- the rest of the song, on a
        chart with clean timing. Closed on the chart's own barline grid so the
        bars resume in phase."""
        points = gs.hidden_anti_barline(
            [self._circle(10000)], 9000, 11000, self.BASE, gs.GimmickConfig(),
        )
        closing = points[-1]
        self.assertTrue(closing.uninherited)
        self.assertAlmostEqual(closing.bpm, 185.0, places=6)
        self.assertGreater(closing.time, 11000)
        # On the base timing's own measure grid: 4 beats of 324.324ms from 0.
        self.assertAlmostEqual(closing.time % (60000.0 / 185.0 * 4), 0.0, delta=0.5)

    def test_only_plain_circles_convert(self):
        """A finisher is marked some other way, and a body outlasting its own
        slit has nothing to be a slit of."""
        notes = [
            self._circle(10000, big=True),
            HitObject(x=256, y=192, time=10500, type=TYPE_SLIDER, hit_sound=0),
        ]
        self.assertEqual(
            gs.hidden_anti_barline(notes, 9000, 11000, self.BASE, gs.GimmickConfig()), [],
        )

    def test_a_restore_never_lands_on_the_wall_line(self):
        """At a high enough tempo 1/n of a beat is under a millisecond, and a
        restore sharing the wall line's timestamp closes the slit before it
        opens -- the later line in file order wins."""
        base = [TimingPoint.uninherited_at(0, 100000.0)]
        points = gs.hidden_anti_barline(
            [self._circle(10000)], 9000, 11000, base,
            gs.GimmickConfig(hidden_window_divisor=64),
        )
        restore = [p for p in points if p.inherited and p.time > 10000]
        self.assertEqual([p.time for p in restore], [10001.0, 10002.0])


class MirroredBarlineTests(unittest.TestCase):
    """Bars either side of the note, or trailing it."""

    def test_don_is_forward_only_and_kat_mirrored_by_default(self):
        points, _ = gs.barline_note(10000, BASE, gs.GimmickConfig(), kind="don")
        self.assertEqual([int(p.time) - 10000 for p in points], [0, 1])
        points, _ = gs.barline_note(10000, BASE, gs.GimmickConfig(), kind="kat")
        self.assertEqual([int(p.time) - 10000 for p in points], [-5, -3, -1, 0, 1, 3, 5])

    def test_forward_only_writes_the_squash_and_its_restores_after_it(self):
        """The owner's map: a squash on the note and one restore a millisecond
        later, nothing before it."""
        config = gs.GimmickConfig(mirror_don_lines=False)
        points, _ = gs.barline_note(87124, BASE, config, kind="don")
        self.assertEqual([int(p.time) - 87124 for p in points], [0, 1])
        self.assertAlmostEqual(points[0].bpm, config.gimmick_bpm)

    def test_forward_only_kat_keeps_all_three_spacings(self):
        points, _ = gs.barline_note(
            10000, BASE, gs.GimmickConfig(mirror_kat_lines=False), kind="kat",
        )
        self.assertEqual([int(p.time) - 10000 for p in points], [0, 1, 3, 5])

    def test_the_two_kinds_mirror_independently(self):
        """The complaint this comes from: one box made a centred Don force a
        centred Kat, when the whole reason the spacings are separate is that
        Don and Kat are separate structures layered on one region."""
        config = gs.GimmickConfig(mirror_don_lines=True, mirror_kat_lines=False)
        don, _ = gs.barline_note(10000, BASE, config, kind="don")
        kat, _ = gs.barline_note(10000, BASE, config, kind="kat")
        self.assertEqual([int(p.time) - 10000 for p in don], [-1, 0, 1])
        self.assertEqual([int(p.time) - 10000 for p in kat], [0, 1, 3, 5])

        flipped = gs.GimmickConfig(mirror_don_lines=False, mirror_kat_lines=True)
        don, _ = gs.barline_note(10000, BASE, flipped, kind="don")
        kat, _ = gs.barline_note(10000, BASE, flipped, kind="kat")
        self.assertEqual([int(p.time) - 10000 for p in don], [0, 1])
        self.assertEqual([int(p.time) - 10000 for p in kat],
                         [-5, -3, -1, 0, 1, 3, 5])

    def test_custom_spacings_survive_forward_only(self):
        config = gs.GimmickConfig(
            mirror_kat_lines=False, kat_spacing1_ms=2, kat_spacing2_ms=6,
            kat_spacing3_ms=9,
        )
        points, _ = gs.barline_note(10000, BASE, config, kind="kat")
        self.assertEqual([int(p.time) - 10000 for p in points], [0, 2, 6, 9])

    def test_omit_note_barline_detaches_only_the_squash_bar(self):
        for omit in (False, True):
            config = gs.GimmickConfig(omit_note_barline=omit)
            points, _ = gs.barline_note(10000, BASE, config, kind="kat")
            by_offset = {int(p.time) - 10000: p.omit_first_barline for p in points}
            self.assertEqual(by_offset.pop(0), omit)
            self.assertFalse(any(by_offset.values()))


class ShinyNoteTests(unittest.TestCase):
    """A stack of fake sliders on one millisecond, which is what reads white."""

    def test_a_shiny_writes_its_configured_stack_at_its_own_offset(self):
        config = gs.GimmickConfig()
        points, notes = gs.fake_slider(10000, BASE, config, shiny=True)
        self.assertEqual(len(notes), config.shiny_count)
        self.assertTrue(all(note.is_slider for note in notes))
        self.assertEqual({note.time for note in notes}, {10000 + config.shiny_offset_ms})
        # The note the glow decorates stays on the snap, so its line sits there
        # too, not on the stack's own offset. One line, not two: a shiny
        # carries no green line since the chart's SV stopped reaching it.
        self.assertEqual([point.time for point in points], [10000.0])
        self.assertTrue(points[0].uninherited)
        self.assertNotAlmostEqual(points[0].bpm, gs.DEFAULT_GIMMICK_BPM)

    def test_the_defaults_keep_the_two_structures_apart(self):
        config = gs.GimmickConfig()
        self.assertEqual(config.fake_slider_offset_ms, 2)
        self.assertEqual(config.shiny_offset_ms, 1)
        self.assertEqual(config.shiny_count, 3)
        self.assertFalse(gs.shiny_collides(config))

    def test_an_equal_offset_is_reported_as_a_collision(self):
        self.assertTrue(gs.shiny_collides(gs.GimmickConfig(shiny_offset_ms=2)))

    def test_a_stack_of_one_is_allowed(self):
        """The offset says it is a shiny, not the stack size -- a plain fake
        slider gets stacked too, to brighten it."""
        self.assertEqual(gs.GimmickConfig(shiny_count=1).shiny_count, 1)
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(shiny_count=0)

    def test_the_bpm_multiplier_retimes_the_red_line_and_is_not_paid_back(self):
        """200 BPM at 1.2x becomes 100 BPM, and nothing hands the speed back.

        The green line that used to pay the retiming back in SV (2.4x here) is
        gone with the rest of this layer's SV: it was computed from the chart's
        own 1.2x, which is exactly what a fake slider must not inherit. Losing
        the compensation along with it is deliberate.
        """
        base = [
            TimingPoint.uninherited_at(0, 200.0),
            TimingPoint.inherited_at(5, 1.2),
        ]
        config = gs.GimmickConfig(shiny_bpm_multiplier=0.5)
        points, _ = gs.fake_slider(10000, base, config, shiny=True)
        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0].bpm, 100.0)
        self.assertTrue(points[0].uninherited)

    def test_a_shiny_never_carries_the_charts_sv_at_any_multiplier(self):
        """The green line used to be written unconditionally -- as the SV
        layer's handle at multiplier 1.0, as the retiming correction otherwise.
        Both readings are gone, so neither multiplier produces one, and the
        chart's 1.2x never reaches the structure."""
        base = [
            TimingPoint.uninherited_at(0, 200.0),
            TimingPoint.inherited_at(5, 1.2),
        ]
        for multiplier in (1.0, 0.5, 2.0):
            with self.subTest(multiplier=multiplier):
                config = gs.GimmickConfig(shiny_bpm_multiplier=multiplier)
                points, _ = gs.fake_slider(10000, base, config, shiny=True)
                self.assertEqual([p for p in points if p.inherited], [])
                self.assertTrue(all(point.uninherited for point in points))
                self.assertFalse(any(
                    point.bpm == gs.DEFAULT_GIMMICK_BPM
                    for point in points if point.uninherited
                ))

    def test_copies_overrides_the_configured_stack(self):
        _, notes = gs.fake_slider(10000, BASE, gs.GimmickConfig(), shiny=True, copies=7)
        self.assertEqual(len(notes), 7)

    def test_a_don_shiny_still_writes_the_hittable_note(self):
        config = gs.GimmickConfig()
        _, notes = gs.fake_slider(10000, BASE, config, kind="don", shiny=True)
        self.assertEqual(notes[0].note_kind, "don")
        self.assertEqual(len(notes), 1 + config.shiny_count)

    def test_shiny_red_line_defaults_on(self):
        """What every shiny written before this dial existed did."""
        self.assertTrue(gs.GimmickConfig().shiny_red_line)

    def test_shiny_red_line_off_writes_no_timing_point(self):
        """Same trade as `fake_slider_red_line`: the stack is still drawn, its
        line is not."""
        config = gs.GimmickConfig(shiny_red_line=False)
        points, notes = gs.fake_slider(10000, BASE, config, shiny=True)
        self.assertEqual(points, [])
        self.assertEqual(len(notes), config.shiny_count)

    def test_shiny_red_line_off_still_honours_the_bpm_multiplier_field(self):
        """The multiplier has nowhere to write to with the dial off -- it is
        simply unused, not an error."""
        config = gs.GimmickConfig(shiny_red_line=False, shiny_bpm_multiplier=0.5)
        points, _ = gs.fake_slider(10000, BASE, config, shiny=True)
        self.assertEqual(points, [])

    def test_owner_file_case_185_bpm_with_inherited_1_15x_at_5ms(self):
        """The owner's real file: 185 BPM from 0, an inherited 1.15x point at
        5ms, and a shiny placed at 65393 with `shiny_bpm_multiplier=0.5`.

        92.5 BPM = 185 x 0.5. The 2.3x green line this file also carried
        (1.15 / 0.5) is no longer written: it was the chart's own 1.15x reaching
        into the structure, which this layer no longer does. `assertAlmostEqual`
        on the number rather than a literal line: `repr(float)` carries more
        digits of this particular division than the owner's file happened to
        show, and that trailing noise is not what this test is guarding.
        """
        base = [
            TimingPoint.uninherited_at(0, 185.0),
            TimingPoint.inherited_at(5, 1.15),
        ]
        config = gs.GimmickConfig(shiny_bpm_multiplier=0.5)
        points, notes = gs.fake_slider(65393, base, config, shiny=True)

        self.assertAlmostEqual(points[0].bpm, 92.5)
        # One line, on the note's own millisecond, carrying omit_first_barline.
        self.assertEqual([point.time for point in points], [65393.0])
        self.assertTrue(points[0].uninherited and points[0].omit_first_barline)
        self.assertFalse(any(point.bpm == gs.DEFAULT_GIMMICK_BPM for point in points if point.uninherited))
        self.assertEqual(
            [p.sv_multiplier for p in points if p.inherited], [],
            "the chart's 1.15x must not reach the shiny",
        )

        self.assertEqual(len(notes), 3)
        self.assertEqual({note.time for note in notes}, {65394})


class RedLineTests(unittest.TestCase):
    def test_a_custom_bpm_overrides_the_base_timing(self):
        """An uninherited point's BPM is its scroll speed, so the red line tool
        is a standalone speed change once it stops echoing the chart."""
        points, _ = gs.red_line(10000, 180.0, gs.GimmickConfig(red_line_bpm=45.0))
        self.assertAlmostEqual(points[0].bpm, 45.0)

    def test_no_custom_bpm_means_the_bpm_it_was_handed(self):
        points, _ = gs.red_line(10000, 180.0, gs.GimmickConfig())
        self.assertAlmostEqual(points[0].bpm, 180.0)

    def test_a_non_positive_custom_bpm_is_refused(self):
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(red_line_bpm=0.0)

    def test_offset_shifts_the_point_off_the_snap(self):
        config = gs.GimmickConfig(red_line_offset_ms=-1)
        points, notes = gs.red_line(10000, 180.0, config)
        self.assertEqual(notes, [])
        self.assertEqual(points[0].time, 9999.0)
        self.assertAlmostEqual(points[0].bpm, 180.0)

    def test_the_meter_is_what_the_line_carries(self):
        """osu! reads meter from the uninherited point in force, so a
        standalone line re-bars the chart after it unless it is told where."""
        points, _ = gs.red_line(10000, 180.0, gs.GimmickConfig(red_line_meter=7))
        self.assertEqual(points[0].meter, 7)

    def test_the_meter_defaults_to_four(self):
        points, _ = gs.red_line(10000, 180.0, gs.GimmickConfig())
        self.assertEqual(points[0].meter, 4)

    def test_a_meter_below_one_is_refused(self):
        with self.assertRaises(gs.GimmickConfigError):
            gs.GimmickConfig(red_line_meter=0)

    def test_zero_offset_lands_on_the_snap(self):
        points, _ = gs.red_line(10000, 200.0, gs.GimmickConfig())
        self.assertEqual(points[0].time, 10000.0)


class SVRestoreTests(unittest.TestCase):
    """A gimmick's red lines reset SV to 1.0x; this is what hands it back."""

    def test_the_sv_in_force_is_restated_at_the_end_of_the_cluster(self):
        points = [TimingPoint.uninherited_at(0, 180.0), TimingPoint.inherited_at(5000, 1.6)]
        restore = gs.sv_restore_point(points, 9999, 10006)
        self.assertIsNotNone(restore)
        self.assertTrue(restore.inherited)
        self.assertAlmostEqual(restore.sv_multiplier, 1.6)
        self.assertEqual(restore.time, 10006)

    def test_a_chart_already_at_one_gets_a_line_saying_so(self):
        """It changes nothing by itself -- and that is the point: it is the
        handle the structure's SV layer is made of, and a layer matches its
        green lines by exact millisecond."""
        points = [TimingPoint.uninherited_at(0, 180.0)]
        restore = gs.sv_restore_point(points, 9999, 10006)
        self.assertTrue(restore.inherited)
        self.assertAlmostEqual(restore.sv_multiplier, 1.0)
        self.assertEqual(restore.time, 10006)

    def test_an_uninherited_point_later_than_the_sv_cancels_it(self):
        points = [
            TimingPoint.uninherited_at(0, 180.0),
            TimingPoint.inherited_at(5000, 1.6),
            TimingPoint.uninherited_at(8000, 200.0),
        ]
        self.assertAlmostEqual(gs.sv_restore_point(points, 9999, 10006).sv_multiplier, 1.0)

    def test_hitsound_settings_come_from_the_point_being_restored(self):
        source = TimingPoint.inherited_at(5000, 1.6)
        source.volume, source.sample_set = 35, 2
        restore = gs.sv_restore_point(
            [TimingPoint.uninherited_at(0, 180.0), source], 9999, 10006
        )
        self.assertEqual((restore.volume, restore.sample_set), (35, 2))


class BaseTimingTests(unittest.TestCase):
    def test_snapshot_is_detached_from_the_document(self):
        original = [TimingPoint.uninherited_at(0, 180.0)]
        snapshot = gs.snapshot_timing(original)
        original[0].beat_length = 1.0  # what a gimmick placement would do
        self.assertAlmostEqual(snapshot[0].bpm, 180.0)

    def test_gimmick_lines_are_detected_but_not_removed(self):
        dirty = [TimingPoint.uninherited_at(0, 180.0), TimingPoint.uninherited_at(500, 60000.0)]
        self.assertTrue(gs.looks_gimmicked(dirty))
        self.assertFalse(gs.looks_gimmicked([TimingPoint.uninherited_at(0, 180.0)]))
        # Warn and continue: the snapshot keeps every point it was given.
        self.assertEqual(len(gs.snapshot_timing(dirty)), 2)


class IndexTests(unittest.TestCase):
    def test_pairing_round_trips_through_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gimmick_index.json"
            pairing = gs.GimmickPairing(
                Path(directory) / "song [Oni] [Gimmick].osu",
                [TimingPoint.uninherited_at(0, 180.0), TimingPoint.inherited_at(500, 1.5)],
            )
            gs.save_index(path, {"source.osu": pairing})

            loaded = gs.load_index(path)
            self.assertEqual(loaded["source.osu"].target, pairing.target)
            self.assertEqual(lines(loaded["source.osu"].base_timing), lines(pairing.base_timing))

    def test_a_corrupt_index_reads_as_empty_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gimmick_index.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(gs.load_index(path), {})
            self.assertEqual(gs.load_index(Path(directory) / "missing.json"), {})

    def test_a_version_bump_discards_the_old_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gimmick_index.json"
            path.write_text('{"version": 0, "pairings": {"a": {"target": "b"}}}', encoding="utf-8")
            self.assertEqual(gs.load_index(path), {})

    def test_gimmick_path_and_version_append_the_suffix(self):
        source = Path("C:/songs/1 Artist - Title (Creator) [Oni].osu")
        self.assertEqual(
            gs.gimmick_path_for(source).name,
            "1 Artist - Title (Creator) [Oni] [Gimmick].osu",
        )
        self.assertEqual(gs.gimmick_version_for("Oni"), "Oni [Gimmick]")


class CarryActiveStateTests(unittest.TestCase):
    """Kiai and hitsound volume both live on the active timing point, so a
    gimmick line written without them redefines both for everything after it."""

    def setUp(self):
        quiet = TimingPoint.inherited_at(5000, 1.0, kiai=True)
        quiet.volume = 20
        self.document = [
            TimingPoint.uninherited_at(0, 180.0),
            quiet,
            TimingPoint.inherited_at(20000, 1.0),
        ]

    def test_a_line_inside_a_kiai_section_carries_kiai(self):
        points, _ = gs.fake_slider(10000, BASE, gs.GimmickConfig(), kind="kat")
        gs.carry_active_state(points, self.document)
        self.assertTrue(all(point.kiai for point in points))

    def test_a_line_outside_one_does_not(self):
        points, _ = gs.fake_slider(30000, BASE, gs.GimmickConfig(), kind="kat")
        gs.carry_active_state(points, self.document)
        self.assertFalse(any(point.kiai for point in points))

    def test_the_omit_barline_bit_survives_it(self):
        """Both live in `effects`; setting one must not clear the other."""
        points, _ = gs.fake_slider(10000, BASE, gs.GimmickConfig())
        gs.carry_active_state(points, self.document)
        self.assertTrue(all(point.omit_first_barline for point in points))
        self.assertEqual(points[0].effects, 9)

    def test_a_line_inside_a_quiet_section_keeps_it_quiet(self):
        """The regression: TimingPoint defaults to 100, so a structure placed
        in a 20% section used to slam the rest of that section to full."""
        points, _ = gs.fake_slider(10000, BASE, gs.GimmickConfig(), kind="kat")
        gs.carry_active_state(points, self.document)
        self.assertEqual([point.volume for point in points], [20] * len(points))

    def test_the_volume_is_read_per_point_not_once(self):
        """A structure can straddle a volume change; each line takes the value
        in force where it actually lands."""
        points = [
            TimingPoint.uninherited_at(10000, 180.0),
            TimingPoint.uninherited_at(25000, 180.0),
        ]
        gs.carry_active_state(points, self.document)
        self.assertEqual([point.volume for point in points], [20, 100])


class OscillatingSeriesTests(unittest.TestCase):
    def test_per_pair_linear_matches_the_worked_example(self):
        values = gs.oscillating_series(1.00, 0.06, 7, lambda t: t, per_pair=True)
        self.assertEqual(
            [round(value, 4) for value in values],
            [1.00, 1.02, 0.98, 1.04, 0.96, 1.06, 0.94],
        )

    def test_per_point_linear_opens_the_fan_on_every_point(self):
        values = gs.oscillating_series(1.00, 0.06, 7, lambda t: t)
        self.assertEqual(
            [round(value, 4) for value in values],
            [1.00, 1.01, 0.98, 1.03, 0.96, 1.05, 0.94],
        )

    def test_both_modes_reach_the_same_total_difference(self):
        """The mode changes how the fan opens, never how wide it ends up."""
        for ease in (lambda t: t, lambda t: t ** 1.6):
            for count in (5, 6, 7, 12):
                for mode in (True, False):
                    values = gs.oscillating_series(1.4, 0.12, count, ease, per_pair=mode)
                    widest = max(abs(value - 1.4) for value in values)
                    self.assertAlmostEqual(widest, 0.12)

    def test_exp16_reaches_the_total_difference_only_at_the_end(self):
        values = gs.oscillating_series(1.40, 0.12, 7, lambda t: t ** 1.6)
        self.assertEqual(
            [round(value, 3) for value in values],
            [1.400, 1.407, 1.379, 1.440, 1.337, 1.490, 1.280],
        )

    def test_first_point_sits_on_the_base(self):
        for count in (1, 2, 7, 20):
            values = gs.oscillating_series(1.4, 0.12, count, lambda t: t ** 1.6)
            self.assertEqual(len(values), count)
            self.assertAlmostEqual(values[0], 1.4)

    def test_sides_alternate_and_stay_positive(self):
        values = gs.oscillating_series(1.0, 0.5, 9, lambda t: t, per_pair=True)
        for index, value in enumerate(values[1:], start=1):
            if index % 2:
                self.assertGreater(value, 1.0)
            else:
                self.assertLess(value, 1.0)
        self.assertTrue(all(value > 0 for value in values))

    def test_empty_range_is_empty(self):
        self.assertEqual(gs.oscillating_series(1.0, 0.1, 0, lambda t: t), [])


if __name__ == "__main__":
    unittest.main()
