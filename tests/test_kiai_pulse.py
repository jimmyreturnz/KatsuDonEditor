"""Kiai flash: notes inside a kiai section brighten on every 1/1 beat.

Two properties beyond "it paints": the flash is gated on the *playhead's* kiai
state, and a section too short to finish the pulse it started still gets to
finish it.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from ui import gui
from ui import gameplay_view
from library import skin
from model.hit_object import HitObject
from osu_io.parser import parse_osu
from osu_io.timing import TimingPoint
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _brightness(view, half: int = 34) -> int:
    """Summed channel value of the box around the hit position."""
    image = view.grab().toImage()
    centre_x = round(view._hit_x())
    centre_y = view.height() // 2
    total = 0
    for x in range(centre_x - half, centre_x + half):
        for y in range(centre_y - half, centre_y + half):
            pixel = image.pixel(x, y)
            total += (pixel & 0xFF) + ((pixel >> 8) & 0xFF) + ((pixel >> 16) & 0xFF)
    return total


class PulseTests(unittest.TestCase):
    """stable's `Ruleset.UpdateKiai` (ppy/osu#26990, owner 2026-10-28): on each
    beat of the red line, if kiai is on then, 1 fading linearly to 0 over
    int(3/4 beat) ms, or until the object's own end if that is sooner."""

    def setUp(self) -> None:
        self.points = [TimingPoint(time=0.0, beat_length=500.0)]

    def flash(self, at, bands=((0, 20000),), end=float("inf"), points=None):
        return gameplay_view.kiai_flash(points or self.points, list(bands), at, end)

    def test_it_is_full_on_the_beat_and_gone_by_three_quarters(self):
        self.assertEqual(self.flash(2000.0), 1.0)
        self.assertAlmostEqual(self.flash(2187.5), 0.5, places=9)
        self.assertEqual(self.flash(2375.0), 0.0, "dark for the last quarter")
        self.assertEqual(self.flash(2499.0), 0.0)

    def test_it_is_phased_on_the_red_line_not_the_kiai(self):
        """A kiai starting off the beat waits for the red line's next one."""
        bands = [(3120, 20000)]
        self.assertEqual(self.flash(3120.0, bands), 0.0)
        self.assertEqual(self.flash(3500.0, bands), 1.0)

    def test_a_kiai_with_no_beat_in_it_never_flashes(self):
        bands = [(1100, 1300)]
        self.assertTrue(all(self.flash(float(t), bands) == 0.0 for t in range(1000, 2000, 5)))

    def test_the_last_flash_finishes_after_the_kiai_ends(self):
        """At any BPM: the flash a beat set off has its own fade."""
        for beat in (250.0, 500.0, 2000.0):
            with self.subTest(beat=beat):
                points = [TimingPoint(time=0.0, beat_length=beat)]
                band = [(beat * 4, beat * 4 + 10)]
                self.assertGreater(self.flash(beat * 4 + 20, band, points=points), 0.0)
                self.assertEqual(self.flash(beat * 4.75, band, points=points), 0.0)

    def test_a_flash_stops_at_its_note(self):
        """Fading over what is left to the note, gone once it has passed."""
        self.assertAlmostEqual(self.flash(2025.0, end=2050.0), 0.5, places=9)
        self.assertEqual(self.flash(2060.0, end=2050.0), 0.0)

    def test_a_gimmick_red_line_keeps_the_first_lines_beats(self):
        """Kiai flashes on the first red line's beats all map long (owner's
        source, 2026-10-28): a 1ms line at 2100 changes nothing."""
        points = [TimingPoint(time=0.0, beat_length=500.0), TimingPoint(time=2100.0, beat_length=1.0)]
        self.assertTrue(all(self.flash(2000.0 + t, points=points) == self.flash(2000.0 + t)
                            for t in range(400)))

    def test_only_objects_within_reach_of_the_beat_flash(self):
        """Up to 2.625 first-line beats past the beat (owner's stable test)."""
        reach = 2.625 * 500.0
        self.assertGreater(gameplay_view.kiai_flash(self.points, [(0, 20000)], 2000.0, float("inf"), 2000.0 + reach), 0.0)
        self.assertEqual(gameplay_view.kiai_flash(self.points, [(0, 20000)], 2000.0, float("inf"), 2001.0 + reach), 0.0)

    def test_slower_bpm_fades_slower(self):
        slow = [TimingPoint(time=0.0, beat_length=1000.0)]
        self.assertGreater(self.flash(200.0, points=slow), self.flash(200.0))

    def test_colours_are_brightened(self):
        self.assertEqual(gameplay_view.kiai_brighten((235, 69, 44)), (255, 94, 69))
        self.assertEqual(gameplay_view.kiai_brighten((10, 20, 30)), (250, 255, 255))

    def test_kiai_reads_the_green_lines_too(self):
        """The fixture switches kiai on with an inherited point at 2000 and off
        with the next one at 4000."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        document = parse_osu(write_fixture(Path(temp.name), "full_v14"))
        points = gui.sorted_by_time(document.timing_points)
        self.assertFalse(gui.in_kiai(points, 1500.0))
        self.assertTrue(gui.in_kiai(points, 2500.0))
        self.assertFalse(gui.in_kiai(points, 4500.0))
        # Uninherited points alone cannot answer this, which is why the flash
        # lives in the viewer -- the only view that keeps the full list.
        self.assertFalse(gui.in_kiai(gui.uninherited_points(points), 2500.0))


class ShortKiaiTests(unittest.TestCase):
    """A short kiai with a beat in it flashes once, and that flash finishes
    after the kiai is over (stable)."""

    def _view(self, band):
        view = gui.GameplayViewerView()
        view.resize(900, 200)
        point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
        view.timing_points = [point]
        view.beat_points = [point]
        view._beat_times = [0.0]
        view.slider_multiplier = 1.4
        view.kiai_bands = [band]
        view._rebuild_velocities()
        view.notes, view.note_times = [], []
        view._end_times, view._phantom_ends, view._max_extend_ms = {}, {}, 0.0
        return view

    def test_the_light_is_actually_still_drawn_after_the_section_ends(self):
        """The beat at 1000 is in the kiai; at 1150 the kiai is over."""
        view = self._view((900, 1100))
        view.current_time = 1150.0
        lit = _brightness(view)
        view.kiai_bands = []
        dark = _brightness(view)
        view.close()
        self.assertGreater(lit, dark)


class ShinyGlowTests(unittest.TestCase):
    """A fake slider is #fbb706 outside a chorus, whatever is stacked on it.

    There is no separate "shiny" effect and no brightening with stack depth: a
    pile of fake sliders is a pile of identical opaque objects and reads as
    one, which is what it is. They are drawn at full opacity like anything
    else -- no fade-in -- so nothing about the drawing tells the stack apart.

    Inside a chorus every object on the pile takes its own kiai stamp, so a
    deep pile pulses harder than a lone one. That is the whole of the shiny,
    and nothing counts the stack.

    Several richer models were tried against real readings and none of them
    closed; this one is true by construction, which is why it is here.
    """

    def _view(self, stack: int, kiai: bool = False, note: bool = False,
              kat: bool = False):
        view = gui.GameplayViewerView()
        # 1366 wide is exactly an osu! screen at 200 tall, so the preview's
        # own fit (height follows width) keeps the height these readings were
        # measured at. At 900 it became 170, and the note's antialiased rim
        # entered the sampled box, where stacked rims do compound.
        view.resize(1366, 200)
        point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
        view.timing_points = [point]
        view.beat_points = [point]
        view._beat_times = [0.0]
        view.slider_multiplier = 1.4
        view.kiai_bands = [(0, 20000)] if kiai else []
        view._rebuild_velocities()
        notes = []
        if note:
            notes.append(HitObject(x=256, y=192, time=1500, type=1,
                                   hit_sound=8 if kat else 0))
        notes += [
            HitObject(x=256, y=192, time=1500, type=2, hit_sound=0,
                      extras=("L|624:192", "1", "-100.0"))
            for _ in range(stack)
        ]
        view.notes = notes
        view.note_times = [1500.0] * len(notes)
        view._end_times, view._phantom_ends, view._max_extend_ms = {}, {}, 0.0
        for each in view.notes:
            phantom = view._compute_phantom_end(each)
            if phantom is not None:
                view._phantom_ends[each.uid] = phantom
        view.current_time = 1000.0
        return view

    def _colour(self, stack: int, **kwargs):
        view = self._view(stack, **kwargs)
        image = view.grab().toImage()
        return image.pixelColor(round(view.x_for_time(1500.0)), 100)

    def _brightness(self, stack: int, **kwargs) -> float:
        view = self._view(stack, **kwargs)
        image = view.grab().toImage()
        cx, cy = round(view.x_for_time(1500.0)), 100
        radius = round(view.height() * gameplay_view.TAIKO_NOTE_SIZE / 2.0 * 0.6)
        total = count = 0
        for y in range(cy - radius, cy + radius):
            for x in range(cx - radius, cx + radius):
                pixel = image.pixelColor(x, y)
                total += pixel.red() + pixel.green() + pixel.blue()
                count += 3
        return total / count

    def test_a_pile_outside_kiai_is_flat_drumroll_yellow(self):
        """However deep. This is the rule the whole thing reduces to."""
        for stack in (1, 2, 3, 5, 8, 16):
            with self.subTest(stack=stack):
                shown = self._colour(stack)
                self.assertEqual(
                    (shown.red(), shown.green(), shown.blue()), gui.DRUMROLL_COLOR)

    def test_a_pile_outside_kiai_does_not_brighten_with_depth(self):
        """No fade-in means nothing about the drawing compounds."""
        readings = {self._brightness(stack) for stack in (1, 2, 3, 5, 8, 16)}
        self.assertEqual(len(readings), 1, readings)

    def test_nothing_is_left_of_the_separate_shine(self):
        """It was a second effect on top of the pulse and it never matched a
        real reading; the pulse alone does the job."""
        self.assertFalse(hasattr(gui, "SHINY_GLOW_COLOR"))
        self.assertFalse(hasattr(gui, "draw_shiny_glow"))

    def test_a_pile_on_the_notes_own_millisecond_goes_over_it(self):
        """Owner's rule: on one millisecond a fake slider is drawn over
        the don/kat, whatever the file order -- so the pile's yellow shows. A
        pile at T+1 (Hyper Bass) still goes under its note."""
        for stack in (3, 8):
            with self.subTest(stack=stack):
                shown = self._colour(stack, note=True)
                self.assertEqual(
                    (shown.red(), shown.green(), shown.blue()), gui.DRUMROLL_COLOR)

    def test_a_lone_note_flashes_its_own_colour_and_a_pile_yellow(self):
        """stable: each object flashes in its own colour brightened, so a don
        lifts red, then green, then blue (red clips, so green over blue
        is what shows); the shiny's light is its heads' yellow."""
        lone = self._colour(0, note=True, kiai=True)
        plain = self._colour(0, note=True)
        self.assertGreater(lone.green() - plain.green(), lone.blue() - plain.blue())
        shiny = self._colour(8, note=True, kiai=True)
        self.assertGreater(shiny.green() - lone.green(), shiny.blue() - lone.blue())

    def test_in_kiai_a_deeper_pile_pulses_harder(self):
        """Every object on the pile takes its own stamp, so the pile compounds
        -- the one thing that does, and the whole of the shiny."""
        readings = [self._brightness(stack, kiai=True) for stack in (1, 2, 3, 5, 8)]
        for dimmer, brighter in zip(readings, readings[1:]):
            self.assertGreater(brighter, dimmer, readings)

    def test_the_pulse_leaves_the_black_parts_alone(self):
        """The light is masked to the colourable part of a note, so the rim and
        the face keep whatever the skinner drew them as.

        Asserted on the stamp rather than on a rendered frame: what the mask
        guarantees is that the stamp carries no alpha where the overlay is
        opaque, and `tests/test_skin.py::FlashMaskTests` pins that directly on
        known artwork. Sampling a render for it means guessing which pixels are
        the face, which is how the first version of this test came to measure
        the lit part instead.
        """
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            base = QImage(16, 16, QImage.Format_ARGB32)
            base.fill(QColor("white"))
            base.save(str(folder / "taikohitcircle.png"))
            overlay = QImage(16, 16, QImage.Format_ARGB32)
            overlay.fill(QColor(0, 0, 0, 255))
            overlay.save(str(folder / "taikohitcircleoverlay.png"))
            loaded = skin.TaikoSkin(folder)
            stamp = loaded.flash("taikohitcircle", 16, QColor("white")).toImage()
        self.assertEqual(stamp.pixelColor(8, 8).alpha(), 0, "fully covered")

    def test_and_the_pulse_only_happens_in_kiai(self):
        for stack in (1, 8):
            with self.subTest(stack=stack):
                self.assertGreater(
                    self._brightness(stack, kiai=True), self._brightness(stack))


class TailFlashTests(unittest.TestCase):
    """A roll's `taiko-roll-end` cap is part of the object and pulses with it.

    Stamped on the cap's own shape and anchored the way the cap is (origin
    TopLeft, butted onto the end of the track), or the light lands on the track
    instead of on the thing it is lighting.
    """

    def _tail(self, kiai: bool, at: float):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            image = QImage(32, 32, QImage.Format_ARGB32)
            image.fill(QColor("white"))
            image.save(str(folder / "taiko-roll-end.png"))
            view = gui.GameplayViewerView()
            # Wide enough that the roll's end cap is inside osu!'s screen
            # (osu_edge_x, 1365px at this height) at the preview's real scale.
            view.resize(1800, 200)
            point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
            view.timing_points = [point]
            view.beat_points = [point]
            view._beat_times = [0.0]
            view.slider_multiplier = 1.4
            view.kiai_bands = [(0, 20000)] if kiai else []
            view._rebuild_velocities()
            view.set_skin(skin.TaikoSkin(folder))
            note = HitObject(x=256, y=192, time=1500, type=2, hit_sound=0,
                             extras=("L|624:192", "1", "300.0"))
            view.notes = [note]
            view.note_times = [1500.0]
            view._end_times, view._phantom_ends, view._max_extend_ms = {}, {}, 0.0
            end = view._compute_end_time(note)
            view._end_times[note.uid] = end
            view._max_extend_ms = end - note.time
            view.current_time = at
            painted = view.grab().toImage()
            x = view.x_for_time(note.time)
            end_x = x + (
                (end - note.time) * view.velocity_at(note.time) * view.px_per_beat
            )
            # A few pixels into the cap, which starts at the end and runs right.
            return painted.pixelColor(round(end_x) + 6, 100)

    def test_the_cap_pulses_in_kiai(self):
        plain = self._tail(False, 1000.0)
        on_beat = self._tail(True, 1000.0)
        self.assertGreater(on_beat.green(), plain.green())
        self.assertGreater(on_beat.blue(), plain.blue())

    def test_and_fades_across_the_beat(self):
        on_beat = self._tail(True, 1000.0)
        late = self._tail(True, 1240.0)
        self.assertGreater(on_beat.blue(), late.blue())

    def test_outside_kiai_it_is_just_the_drumroll_colour(self):
        """#fbb706 and nothing added to it -- if the anchor were wrong this
        would be sampling the track, or the lane."""
        plain = self._tail(False, 1000.0)
        self.assertEqual(
            (plain.red(), plain.green(), plain.blue()), gui.DRUMROLL_COLOR)


class FlashRenderTests(unittest.TestCase):
    """Rendered brightness, since the flash is only ever an overlay."""

    def _viewer(self, kiai: bool, stack: int = 1, *, kiai_from: float = 0.0, playhead: float = 2000.0):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        document = parse_osu(write_fixture(Path(temp.name), "full_v14"))
        document.timing_points = [TimingPoint(time=0.0, beat_length=500.0)]
        if kiai:
            document.timing_points.append(
                TimingPoint(time=kiai_from, beat_length=-100.0, uninherited_flag=0, effects=1)
            )
        # Short of the playhead, not on it: a circle *at* the hit position has
        # been hit and is no longer drawn, so a stack parked there measured the
        # lane wash and nothing else -- which is a test that cannot fail.
        # 100ms is ~63px at the preview's real scale: close enough that the
        # note's flash still reaches the box `_brightness` samples.
        note_time = int(playhead) + 100
        document.hit_objects = [
            HitObject(x=256, y=192, time=note_time, type=1, hit_sound=0)
            for _ in range(stack)
        ]
        view = gui.GameplayViewerView()
        view.resize(800, 200)
        view.load_document(document)
        view.current_time = playhead  # on the beat, so the flash is at full
        return view

    def test_a_kiai_note_on_the_beat_is_brighter_than_the_same_note_outside(self):
        self.assertGreater(_brightness(self._viewer(True)), _brightness(self._viewer(False)))

    def test_stacked_objects_amplify_it(self):
        """Three fake sliders on one note: each overlay lands on the last."""
        self.assertGreater(_brightness(self._viewer(True, stack=3)), _brightness(self._viewer(True)))

    def test_it_starts_only_once_the_playhead_reaches_the_section(self):
        """A kiai note already on screen stays plain until the playhead gets
        there -- kiai starts when you arrive at it, not when you can see it."""
        approaching = self._viewer(True, kiai_from=1900.0, playhead=1000.0)
        plain = self._viewer(False, playhead=1000.0)
        self.assertEqual(_brightness(approaching), _brightness(plain))
        # ...and once it arrives, the same note flashes.
        self.assertGreater(
            _brightness(self._viewer(True, kiai_from=1900.0, playhead=2000.0)),
            _brightness(self._viewer(False, playhead=2000.0)),
        )

    def test_a_stack_outside_kiai_does_not_brighten(self):
        """Only kiai amplifies. Stacking translucent note fills does darken the
        spot slightly -- that is the existing note rendering, not the flash."""
        self.assertLessEqual(_brightness(self._viewer(False, stack=3)), _brightness(self._viewer(False)))

    def test_the_chart_editor_does_not_flash(self):
        """Gameplay viewer only: the chart view is for editing, and a pulsing
        note there fights the selection and snap colours.

        It does *tint* -- every layer draws a flat orange band behind a kiai
        section, which is the point: it says which sections are choruses without
        moving. So the test is that the picture does not change with the beat,
        not that kiai is invisible."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        def brightness(kiai: bool, band: bool = True) -> int:
            document = parse_osu(write_fixture(Path(temp.name), "full_v14"))
            document.timing_points = [TimingPoint(time=0.0, beat_length=500.0, effects=1 if kiai else 0)]
            document.hit_objects = [HitObject(x=256, y=192, time=2000, type=1, hit_sound=0)]
            view = gui.TimelineGameplay()
            view.resize(800, 200)
            view.load_document(document)
            view.current_time = 2000.0
            if not band:
                view.kiai_bands = []
            image = view.grab().toImage()
            return sum(
                (image.pixel(x, y) & 0xFF) + ((image.pixel(x, y) >> 8) & 0xFF) + ((image.pixel(x, y) >> 16) & 0xFF)
                for x in range(370, 430)
                for y in range(0, view.height())
            )

        # With the band taken away there is nothing left that kiai changes --
        # no flash on the beat, which is the whole claim.
        self.assertEqual(brightness(True, band=False), brightness(False, band=False))
        # ...and the band itself is the only difference, so it does show.
        self.assertNotEqual(brightness(True), brightness(False))

    def test_the_chart_editor_marks_a_kiai_section(self):
        """The band is background information and is drawn in every layer, so a
        kiai section reads as one wherever you are looking."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        document = parse_osu(write_fixture(Path(temp.name), "full_v14"))
        document.timing_points = [
            TimingPoint(time=0.0, beat_length=500.0, effects=1),
            TimingPoint(time=3000.0, beat_length=-100.0, uninherited_flag=0, effects=0),
        ]
        view = gui.TimelineGameplay()
        view.load_document(document)
        self.assertIn((0, 3000), view.kiai_bands)

        sv_view = gui.SVEditorView()
        sv_view.load_document(document)
        self.assertIn((0, 3000), sv_view.kiai_bands)


if __name__ == "__main__":
    unittest.main()
