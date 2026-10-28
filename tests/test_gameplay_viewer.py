"""M6 gameplay viewer: an osu!taiko-like preview where every object approaches
the hit position at the speed in force at *its own* time.

That per-object rule is the whole feature. A chart view shows notes on a time
axis, where an SV change is invisible by construction; integrating one shared
scroll position instead would be osu!mania's model, in which objects can never
pass each other. So most of these tests pin positions against the full_v14
fixture, whose timing points were written for exactly this kind of arithmetic:

    0     500ms/beat (120 BPM), uninherited
    2000  SV 0.75, inherited, kiai
    4000  SV 2.00, inherited
    6000  400ms/beat (150 BPM), uninherited, omit-first-barline
"""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QWheelEvent
from PySide6.QtWidgets import QApplication

from ui import gui
from ui import common
# The preview calls draw_note_sprite through its own module; a spy goes there.
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


def _wheel(view, delta: int, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    event = QWheelEvent(
        QPointF(100.0, 50.0), QPointF(100.0, 50.0), QPoint(0, 0), QPoint(0, delta),
        Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False,
    )
    view.wheelEvent(event)


def _viewer(document) -> gui.GameplayViewerView:
    view = gui.GameplayViewerView()
    view.resize(800, 200)
    view.load_document(document)
    return view


class ScrollVelocityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        path = write_fixture(Path(self._temp.name), "full_v14")
        self.view = _viewer(parse_osu(path))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_velocity_is_one_beat_per_beat_length_at_1x(self):
        """Scroll speed is BPM x SV, so one beat covers the same distance at
        every BPM -- that is what makes px-per-beat a constant."""
        self.assertAlmostEqual(self.view.velocity_at(1000.0), 1 / 500.0, places=9)
        # 6000 onward is 150 BPM (400ms/beat), and the red line resets SV.
        self.assertAlmostEqual(self.view.velocity_at(6400.0), 1 / 400.0, places=9)

    def test_sv_scales_the_velocity_of_the_section_it_governs(self):
        self.assertAlmostEqual(self.view.velocity_at(2500.0), 0.75 / 500.0, places=9)
        self.assertAlmostEqual(self.view.velocity_at(4500.0), 2.0 / 500.0, places=9)

    def test_each_object_uses_the_speed_at_its_own_time(self):
        """The osu!taiko rule, and the reason this is not an integral: a note
        in a 2.0x section is twice as far out as one the same time away in a
        1.0x section, whatever lies between them and the playhead."""
        self.view.current_time = 0.0
        hit_x = self.view._hit_x()
        slow = self.view.x_for_time(1000.0) - hit_x  # 1.0x
        fast = self.view.x_for_time(4500.0) - hit_x  # 2.0x
        self.assertAlmostEqual(slow, 1000.0 / 500.0 * self.view.px_per_beat, places=6)
        self.assertAlmostEqual(fast, 4500.0 * 2.0 / 500.0 * self.view.px_per_beat, places=6)

    def test_a_green_line_moves_the_note_sitting_on_it(self):
        """'Every note to the right follows this speed, including the current
        note the green line is on.'"""
        self.view.current_time = 0.0
        before = self.view.x_for_time(4000.0)
        point = next(p for p in self.view.timing_points if p.time == 4000.0)
        point.set_sv(point.sv_multiplier * 2)
        self.view._rebuild_velocities()
        self.assertAlmostEqual(self.view.x_for_time(4000.0) - self.view._hit_x(),
                               (before - self.view._hit_x()) * 2, places=6)

    def test_a_green_line_leaves_earlier_notes_alone(self):
        self.view.current_time = 0.0
        before = self.view.x_for_time(1000.0)
        point = next(p for p in self.view.timing_points if p.time == 4000.0)
        point.set_sv(point.sv_multiplier * 2)
        self.view._rebuild_velocities()
        self.assertAlmostEqual(self.view.x_for_time(1000.0), before, places=6)

    def test_the_current_time_sits_on_the_hit_position(self):
        self.view.current_time = 3000.0
        self.assertAlmostEqual(
            self.view.x_for_time(3000.0), self.view._hit_x(), places=6
        )

    def test_scroll_speed_only_scales_the_picture(self):
        """A taller view scrolls more pixels per beat but must not change
        *where* in the map it is -- velocity is stored in beats for this reason."""
        self.view.current_time = 3000.0
        before = self.view.velocity_at(5000.0)
        pixels = self.view.px_per_beat
        self.view.resize(self.view.width(), self.view.height() * 2)
        self.assertAlmostEqual(self.view.px_per_beat, pixels * 2, places=6)
        self.assertAlmostEqual(self.view.velocity_at(5000.0), before, places=9)
        self.assertAlmostEqual(self.view.x_for_time(3000.0), self.view._hit_x(), places=6)

    def test_a_zero_beat_length_does_not_divide_by_zero(self):
        """The invisible-note gimmick authors beat_length near zero, and a
        hand-edited map can carry a literal 0."""
        self.view.timing_points[0].beat_length = 0.0
        self.view._rebuild_velocities()
        self.assertTrue(all(velocity > 0 for velocity in self.view._velocities))


class TaikoProportionTests(unittest.TestCase):
    """Sizes and speeds pinned to osu!taiko's own constants.

    The preview exists to answer "what will this look like in game", so the
    numbers it draws with are the ruleset's, not ones chosen to look right --
    and a test is the only thing that stops them drifting back.
    """

    def test_a_note_is_the_rulesets_fraction_of_the_lane(self):
        """`TaikoHitObject.DEFAULT_SIZE = 0.475f`, a fraction of the playfield
        height. It used to be `min(30.0, height * 0.19)`, which stopped growing
        with the view entirely once it was tall enough."""
        self.assertAlmostEqual(gameplay_view.TAIKO_NOTE_SIZE, 0.475, places=6)
        for height in (120, 180, 400, 900):
            with self.subTest(height=height):
                self.assertAlmostEqual(
                    height * gameplay_view.TAIKO_NOTE_SIZE / 2.0, height * 0.2375, places=6)

    def test_a_finisher_is_the_rulesets_strong_scale(self):
        """`TaikoStrongableHitObject.STRONG_SCALE = 1 / 0.65f`. 1.4 was assumed
        here before, which drew every big note about 10% small."""
        self.assertAlmostEqual(common.TAIKO_STRONG_SCALE, 1.0 / 0.65, places=6)
        self.assertGreater(common.TAIKO_STRONG_SCALE, 1.5)


class SkinnedCircleElementTests(unittest.TestCase):
    """Which circle artwork each note kind asks the skin for.

    `taikobigcircle` is a separate file that skinners **redraw** rather than
    scale -- a different shape, an extra ring, a different rim -- so drawing
    `taikohitcircle` at the finisher diameter is the wrong art at the right
    size. Big drumrolls were doing exactly that: the head went through
    `_draw_skinned_roll`, which never took the flag.
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        folder = Path(self._temp.name)
        for name in ("taikohitcircle", "taikohitcircleoverlay",
                     "taikobigcircle", "taikobigcircleoverlay",
                     "taiko-roll-middle", "taiko-roll-end"):
            image = QImage(16, 16, QImage.Format_ARGB32)
            image.fill(QColor("white"))
            image.save(str(folder / f"{name}.png"))
        self.skin = skin.TaikoSkin(folder)
        self.asked: list[str] = []
        inner = self.skin.scaled
        self.skin.scaled = lambda element, diameter, colour=None: (
            self.asked.append(element) or inner(element, diameter, colour))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _circles_drawn_for(self, note) -> list[str]:
        view = gui.GameplayViewerView()
        view.resize(800, 200)
        view.timing_points = [TimingPoint(time=0.0, beat_length=500.0)]
        view.beat_points = list(view.timing_points)
        view._beat_times = [0.0]
        view.slider_multiplier = 1.4
        view._rebuild_velocities()
        view.set_skin(self.skin)
        view.notes = [note]
        view.note_times = [note.time]
        view._end_times = {}
        view._phantom_ends = {}
        view._max_extend_ms = 0.0
        end = view._compute_end_time(note)
        if end is not None:
            view._end_times[note.uid] = end
            view._max_extend_ms = end - note.time
        # A millisecond short of the target: a circle *on* it has been hit and
        # is no longer drawn, which is what HitPositionTests covers.
        view.current_time = float(note.time) - 1.0
        self.asked.clear()
        view.grab()
        # `approachcircle` is the hit target, not a note; it is asked for on
        # every frame regardless of what is on screen.
        return [e for e in self.asked if e.startswith("taiko") and "circle" in e]

    @staticmethod
    def _circle(finisher: bool):
        return HitObject(x=256, y=192, time=1000, type=1,
                         hit_sound=4 if finisher else 0)

    @staticmethod
    def _drumroll(finisher: bool):
        return HitObject(x=256, y=192, time=1000, type=2,
                         hit_sound=4 if finisher else 0,
                         extras=("L|624:192", "1", "200.0"))

    def test_a_normal_note_uses_the_small_circle(self):
        self.assertEqual(self._circles_drawn_for(self._circle(False)),
                         ["taikohitcircle", "taikohitcircleoverlay"])

    def test_a_finisher_uses_the_big_circle(self):
        self.assertEqual(self._circles_drawn_for(self._circle(True)),
                         ["taikobigcircle", "taikobigcircleoverlay"])

    def test_a_normal_drumroll_head_uses_the_small_circle(self):
        self.assertEqual(self._circles_drawn_for(self._drumroll(False)),
                         ["taikohitcircle", "taikohitcircleoverlay"])

    def test_a_big_drumroll_head_uses_the_big_circle(self):
        """The one that was wrong: the right diameter, the wrong file."""
        self.assertEqual(self._circles_drawn_for(self._drumroll(True)),
                         ["taikobigcircle", "taikobigcircleoverlay"])


class HitPositionTests(unittest.TestCase):
    """What reaching the hit position does to each kind of object.

    osu!taiko takes a circle away the moment it lands: that is what being hit
    looks like, and the explosion is the only thing left saying it happened. A
    drumroll is still being hit while its body crosses the target, so it
    travels straight through; a spinner occupies its span in place.
    """

    def _view(self, notes, at):
        view = gui.GameplayViewerView()
        view.resize(900, 200)
        point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
        view.timing_points = [point]
        view.beat_points = [point]
        view._beat_times = [0.0]
        view.slider_multiplier = 1.4
        view._rebuild_velocities()
        view.notes = sorted(notes, key=lambda n: n.time)
        view.note_times = [n.time for n in view.notes]
        view._end_times, view._phantom_ends, view._max_extend_ms = {}, {}, 0.0
        for note in view.notes:
            end = view._compute_end_time(note)
            if end is not None:
                view._end_times[note.uid] = end
                view._max_extend_ms = max(view._max_extend_ms, end - note.time)
        view.current_time = at
        return view

    @staticmethod
    def _circle(time_ms, finisher=False):
        return HitObject(x=256, y=192, time=time_ms, type=1,
                         hit_sound=4 if finisher else 0)

    @staticmethod
    def _drumroll(time_ms):
        return HitObject(x=256, y=192, time=time_ms, type=2, hit_sound=0,
                         extras=("L|624:192", "1", "400.0"))

    @staticmethod
    def _spinner(time_ms, end_ms):
        return HitObject(x=256, y=192, time=time_ms, type=8, hit_sound=0,
                         extras=(str(end_ms),))

    def test_a_circle_is_gone_the_moment_it_lands(self):
        note = self._circle(1000)
        view = self._view([note], at=1000.0)
        self.assertTrue(view._has_been_hit(note))

    def test_a_circle_short_of_the_target_is_still_drawn(self):
        note = self._circle(1000)
        view = self._view([note], at=999.0)
        self.assertFalse(view._has_been_hit(note))

    def test_a_finisher_goes_the_same_way(self):
        note = self._circle(1000, finisher=True)
        view = self._view([note], at=1000.0)
        self.assertTrue(view._has_been_hit(note))

    def test_a_drumroll_passes_through_to_the_back(self):
        """Its body is still being hit as it crosses, so it keeps travelling."""
        note = self._drumroll(1000)
        view = self._view([note], at=1400.0)
        self.assertFalse(view._has_been_hit(note))

    def test_a_spinner_is_unchanged(self):
        note = self._spinner(1000, 2000)
        view = self._view([note], at=1500.0)
        self.assertFalse(view._has_been_hit(note))

    def test_a_hit_circle_is_not_painted_and_its_neighbour_still_is(self):
        """Through the paint path rather than the predicate, because the flash
        pass iterates the same notes separately and has to agree with it."""
        landed, coming = self._circle(1000), self._circle(2000)
        view = self._view([landed, coming], at=1000.0)
        drawn = []
        original = gameplay_view.draw_note_sprite

        def spy(painter, fill, outline, x, y, radius, skin=None, big=False):
            drawn.append(round(x))
            return original(painter, fill, outline, x, y, radius, skin, big)

        gameplay_view.draw_note_sprite = spy
        try:
            view.grab()
        finally:
            gameplay_view.draw_note_sprite = original
        self.assertEqual(len(drawn), 1, "only the note still approaching")
        self.assertGreater(drawn[0], view._hit_x())

    @staticmethod
    def _fake(time_ms, length=-400.0):
        return HitObject(x=256, y=192, time=time_ms, type=2, hit_sound=0,
                         extras=("L|624:192", "1", repr(length)))

    def _fake_view(self, note, at):
        view = self._view([note], at=at)
        view._phantom_ends[note.uid] = view._compute_phantom_end(note)
        return view

    def test_a_fake_slider_loses_its_head_when_its_cap_lands(self):
        """The cap sits ahead of the head, so it lands first and takes the
        head with it."""
        note = self._fake(1000, -100.0)
        view = self._fake_view(note, at=0.0)
        cap = view._phantom_ends[note.uid]
        view.current_time = cap - 1.0
        self.assertFalse(view._has_been_hit(note))
        view.current_time = cap
        self.assertTrue(view._has_been_hit(note))

    def _what_is_drawn(self, note, at):
        """(skin elements asked for, tracks drawn, note heads) for one frame."""
        view = self._view([note], at=at)
        phantom = view._compute_phantom_end(note)
        if phantom is not None:
            view._phantom_ends[note.uid] = phantom
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("taikohitcircle", "taikohitcircleoverlay",
                         "taiko-roll-middle", "taiko-roll-end"):
                image = QImage(32, 32, QImage.Format_ARGB32)
                image.fill(QColor("white"))
                image.save(str(folder / f"{name}.png"))
            loaded = skin.TaikoSkin(folder)
            asked = []
            inner = loaded.scaled
            loaded.scaled = lambda e, d, c=None: (asked.append(e) or inner(e, d, c))
            view.set_skin(loaded)

            bodies, heads = [], []
            body = gui.GameplayViewerView._draw_fake_slider
            sprite = gameplay_view.draw_note_sprite
            gui.GameplayViewerView._draw_fake_slider = (
                lambda s, *a: bodies.append(1))
            gameplay_view.draw_note_sprite = (
                lambda *a, **k: heads.append(1))
            try:
                view.grab()
            finally:
                gui.GameplayViewerView._draw_fake_slider = body
                gameplay_view.draw_note_sprite = sprite
        return asked, bodies, heads

    def test_before_the_target_it_is_drawn_by_its_own_routine(self):
        """A fake slider is not routed through the real roll's drawing -- it
        has no cap, and its track reaches the other way."""
        note = self._fake(1000)
        asked, bodies, heads = self._what_is_drawn(note, at=900.0)
        self.assertEqual(len(bodies), 1)
        self.assertNotIn("taiko-roll-end", asked)

    def test_past_the_target_it_is_still_drawn_but_headless(self):
        """Only the head is taken away at the hit position, the way a circle
        is. The track and its cap pass straight through, so the object is still
        being drawn -- it used to return before anything at all."""
        note = self._fake(1000)
        _asked, bodies, heads = self._what_is_drawn(note, at=1200.0)
        self.assertEqual(len(bodies), 1, "the object stopped being drawn")
        self.assertEqual(heads, [], "the head is played and gone")

    def test_the_canonical_short_one_is_still_a_visible_circle(self):
        """Its track collapses to a thousandth of a pixel, so the head is the
        whole object. Dropping the head because a long one hides it behind its
        own track made every short fake slider -- which is most of them --
        invisible."""
        for length in (-0.001, -0.0001):
            with self.subTest(length=length):
                note = self._fake(1000, length)
                view = self._view([note], at=1000.0 - 1.0)
                view._phantom_ends[note.uid] = view._compute_phantom_end(note)
                heads = []
                sprite = gameplay_view.draw_note_sprite
                gameplay_view.draw_note_sprite = lambda *a, **k: heads.append(1)
                try:
                    view.grab()
                finally:
                    gameplay_view.draw_note_sprite = sprite
                    view.close()
                self.assertEqual(len(heads), 1)

    def _tracks_and_heads(self, view):
        view.skin.scaled = lambda *a, **k: None  # built-in track, one drawRect
        rects, heads, opacities = [], [], []
        rect, sprite = gui.QPainter.drawRect, gameplay_view.draw_note_sprite

        def spy(painter, r):
            rects.append(r)
            opacities.append(painter.opacity())

        gui.QPainter.drawRect = spy
        gameplay_view.draw_note_sprite = lambda *a, **k: heads.append(1)
        try:
            view.grab()
        finally:
            gui.QPainter.drawRect = rect
            gameplay_view.draw_note_sprite = sprite
            view.close()
        return rects, heads, opacities

    def test_a_long_slow_one_has_faded_before_it_shows(self):
        """-800 at 1x: its cap lands 2.9s ahead of its head and it is faded
        500ms later (owner's stable test map, 2026-10-28), long before any of
        it is on screen.
        ponytail: earlier (2026-10-26) a -800 was seen showing its track;
        re-check that map against this rule."""
        note = self._fake(1000, -800.0)
        rects, heads, _ = self._tracks_and_heads(self._fake_view(note, at=900.0))
        self.assertEqual((rects, heads), ([], []))

    def test_nothing_shows_before_the_head_comes_on_screen(self):
        """The cap is ahead of the head, but the object is not alive yet."""
        note = self._fake(3000, -100.0)
        view = self._fake_view(note, at=0.0)
        view.grab()  # settles the geometry the first paint lays out
        view.current_time += (view.x_for_time(3000.0) - view.width() - 50) / (
            view.velocity_at(3000.0) * view.px_per_beat)
        self.assertGreater(view.x_for_time(3000.0), view.width())
        self.assertLess(view.x_for_time(view._phantom_ends[note.uid]), view.width())
        rects, heads, _ = self._tracks_and_heads(view)
        self.assertEqual((rects, heads), ([], []))

    def test_it_fades_out_from_when_its_cap_lands(self):
        """Owner's stable test map, 2026-10-28: once the cap reaches the
        target the head is gone, and the track fades out over
        FAKE_SLIDER_FADE_MS. -20's cap is 71ms ahead of its head."""
        fade_ms = gameplay_view.FAKE_SLIDER_FADE_MS
        note = self._fake(1000, -20.0)
        lands = 1000.0 - abs(self._view([note], at=0.0)._compute_phantom_end(note) - 1000.0)
        rects, heads, opacities = self._tracks_and_heads(self._fake_view(note, at=lands + fade_ms / 2))
        self.assertTrue(rects)
        self.assertEqual(heads, [])
        self.assertAlmostEqual(opacities[-1], 0.5, delta=0.02)
        note = self._fake(1000, -20.0)
        rects, heads, _ = self._tracks_and_heads(self._fake_view(note, at=lands + fade_ms + 1))
        self.assertEqual((rects, heads), ([], []))

    def test_the_slice_is_widened_by_the_forward_extent(self):
        """The same thing one level up: the visible-range slice has to reach
        back far enough to still find the note whose track is on screen."""
        note = self._fake(1000, -800.0)
        probe = self._view([note], at=1000.0)
        span = abs(probe._compute_phantom_end(note) - 1000.0)
        probe.close()

        document = SimpleNamespace(
            hit_objects=[note], slider_multiplier=1.4,
            timing_points=[TimingPoint(time=0.0, beat_length=500.0, meter=4)],
        )
        view = gui.GameplayViewerView()
        view.resize(900, 200)
        view.refresh_notes(document)
        self.assertGreaterEqual(view._max_extend_ms, span)
        view.close()

    def test_the_track_reaches_right_by_the_negative_length(self):
        """Its extent is the magnitude of the negative length, drawn to scale
        and to the right of the object."""
        view = self._view([self._fake(1000, -100.0)], at=600.0)
        note = view.notes[0]
        view._phantom_ends[note.uid] = view._compute_phantom_end(note)
        drawn = []
        view.skin.scaled = lambda *a, **k: None  # built-in track, one drawRect
        original = gui.QPainter.drawRect
        gui.QPainter.drawRect = lambda self, rect: drawn.append(rect)
        try:
            view.grab()
        finally:
            gui.QPainter.drawRect = original
        self.assertTrue(drawn, "no track drawn")
        track = drawn[-1]
        x = view.x_for_time(1000.0)
        expected = abs(view._phantom_ends[note.uid] - 1000.0) *             view.velocity_at(1000.0) * view.px_per_beat
        self.assertAlmostEqual(track.left(), x, delta=0.5)
        self.assertAlmostEqual(track.width(), expected, delta=0.5)


class DrumrollPartsTests(unittest.TestCase):
    """A drumroll is `taikohitcircle` + `taiko-roll-middle` + `taiko-roll-end`,
    a fake slider included, in **both** views that draw notes.

    A fake slider's length is zero or negative, so its body has no width and
    only the caps land; drawing the cap only when there was a body to cap left
    it a bare head. And the editor timeline drew its own bar beside a skinned
    head, which was the one place the two views disagreed about the object.
    """

    ELEMENTS = ("taikohitcircle", "taikohitcircleoverlay",
                "taiko-roll-middle", "taiko-roll-end")

    def _skin_with_spy(self, folder):
        for name in self.ELEMENTS:
            image = QImage(32, 32, QImage.Format_ARGB32)
            image.fill(QColor("white"))
            image.save(str(folder / f"{name}.png"))
        loaded = skin.TaikoSkin(folder)
        asked = []
        inner = loaded.scaled
        loaded.scaled = lambda e, d, c=None: (asked.append((e, c)) or inner(e, d, c))
        return loaded, asked

    def _preview_parts(self, length, at=1000.0):
        with tempfile.TemporaryDirectory() as directory:
            loaded, asked = self._skin_with_spy(Path(directory))
            view = gui.GameplayViewerView()
            view.resize(900, 200)
            point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
            view.timing_points = [point]
            view.beat_points = [point]
            view._beat_times = [0.0]
            view.slider_multiplier = 1.4
            view._rebuild_velocities()
            view.set_skin(loaded)
            note = HitObject(x=256, y=192, time=1500, type=2, hit_sound=0,
                             extras=("L|624:192", "1", repr(length)))
            view.notes = [note]
            view.note_times = [1500.0]
            view._end_times, view._phantom_ends, view._max_extend_ms = {}, {}, 0.0
            end = view._compute_end_time(note)
            if end is not None:
                view._end_times[note.uid] = end
                view._max_extend_ms = end - note.time
            else:
                phantom = view._compute_phantom_end(note)
                if phantom is not None:
                    view._phantom_ends[note.uid] = phantom
            view.current_time = at
            asked.clear()
            view.grab()
            return asked

    def test_a_real_drumroll_draws_all_three_pieces(self):
        names = [element for element, _colour in self._preview_parts(400.0)]
        for piece in ("taiko-roll-middle", "taiko-roll-end", "taikohitcircle"):
            with self.subTest(piece=piece):
                self.assertIn(piece, names)

    def test_a_negative_length_slider_is_all_three_pieces_too(self):
        """Head, track and cap, the same three a real roll has, until its cap
        (-100 here, 357ms ahead of the head) lands."""
        names = [element for element, _c in self._preview_parts(-100.0)]
        for piece in ("taiko-roll-middle", "taiko-roll-end", "taikohitcircle"):
            with self.subTest(piece=piece):
                self.assertIn(piece, names)

    def test_once_its_cap_lands_the_head_goes_and_the_rest_fades_on(self):
        """Owner's stable test map, 2026-10-28: when the cap reaches the hit
        position the head is gone; track and cap carry on, fading."""
        for length in (-100.0, -20.0):
            with self.subTest(length=length):
                # The head is at 1500; -100's cap is 357ms ahead, -20's 71.
                names = [element for element, _c in self._preview_parts(length, at=1450.0)]
                self.assertIn("taiko-roll-middle", names)
                self.assertIn("taiko-roll-end", names)
                self.assertNotIn("taikohitcircle", names)
        # -100's landed at ~1143: faded by ~1643.
        self.assertEqual(self._preview_parts(-100.0, at=1650.0), [])

    def test_a_sub_ms_ones_cap_alone_passes_through_the_target(self):
        """Owner, 2026-10-28: a -0.001 fake slider's head lands first and
        goes; only its cap carries on through the hit position."""
        # Its track is under half a pixel, so not drawn.
        names = [element for element, _c in self._preview_parts(-0.001, at=1600.0)]
        self.assertIn("taiko-roll-end", names)
        self.assertNotIn("taikohitcircle", names)
        # Gone once its FAKE_SLIDER_FADE_MS are up.
        names = [element for element, _c in self._preview_parts(-0.001, at=2010.0)]
        self.assertNotIn("taiko-roll-end", names)
        # Before it gets there, all of it.
        names = [element for element, _c in self._preview_parts(-0.001, at=1400.0)]
        for piece in ("taiko-roll-end", "taikohitcircle"):
            with self.subTest(piece=piece):
                self.assertIn(piece, names)

    def test_every_piece_takes_the_drumroll_colour(self):
        for element, colour in self._preview_parts(400.0):
            if element in ("taiko-roll-middle", "taiko-roll-end", "taikohitcircle"):
                with self.subTest(element=element):
                    self.assertIsNotNone(colour, "tinted, not raw art")
                    self.assertEqual(
                        (colour.red(), colour.green(), colour.blue()),
                        gui.DRUMROLL_COLOR,
                    )

    def test_the_cap_is_exactly_as_tall_as_the_track(self):
        """Owner, 2026-10-24: in a skin whose track and cap are padded alike
        and the head less, only the cap was scaled to the head's ink, so it
        stood 3px proud of the body above and below."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name, size, pad in (("taikohitcircle", (256, 256), 2),
                                    ("taiko-roll-middle", (2, 256), 9),
                                    ("taiko-roll-end", (128, 256), 9)):
                image = QImage(*size, QImage.Format_ARGB32)
                image.fill(Qt.transparent)
                painter = QPainter(image)
                painter.fillRect(0, pad, size[0], size[1] - 2 * pad, QColor("white"))
                painter.end()
                image.save(str(folder / f"{name}.png"))
            view = gui.GameplayViewerView()
            view.resize(900, 230)
            view.set_skin(skin.TaikoSkin(folder))
            image = QImage(900, 230, QImage.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            view._draw_skinned_roll(painter, 100.0, 600.0, 115.0, 54.6)
            painter.end()

        def rows(x):
            opaque = [y for y in range(image.height()) if image.pixelColor(x, y).alpha() > 128]
            return opaque[0], opaque[-1]
        self.assertEqual(rows(599), rows(601), "track and cap meet on the same rows")
        for track, head in zip(rows(400), rows(100)):
            self.assertAlmostEqual(track, head, delta=1, msg="and the track is as tall as the head")

    def test_the_editor_timeline_draws_the_same_pieces(self):
        """The skin is meant to reach every view that draws a note."""
        with tempfile.TemporaryDirectory() as directory:
            loaded, asked = self._skin_with_spy(Path(directory))
            document = parse_osu(write_fixture(Path(directory), "full_v14"))
            document.hit_objects = [
                HitObject(x=256, y=192, time=2000, type=2, hit_sound=0,
                          extras=("L|624:192", "1", "400.0"))
            ]
            view = gui.TimelineGameplay()
            view.resize(900, 200)
            view.skin = loaded
            view.load_document(document)
            view.current_time = 2000.0
            asked.clear()
            view.grab()
        names = [element for element, _colour in asked]
        self.assertIn("taiko-roll-middle", names)
        self.assertIn("taiko-roll-end", names)


class SpinnerFadeTests(unittest.TestCase):
    def test_a_spinner_fades_in_from_a_beat_and_a_half_before_it(self):
        """ppy/osu#21769: hidden, then in over 666ms (the half beat if
        shorter) from 1.5 beats before."""
        from ui.gameplay_view import spinner_fade
        self.assertEqual(spinner_fade(500.0, 850.0), 0.0)
        self.assertEqual(spinner_fade(500.0, 625.0), 0.25)
        self.assertEqual(spinner_fade(500.0, 500.0), 1.0)
        self.assertEqual(spinner_fade(500.0, 0.0), 1.0)
        self.assertEqual(spinner_fade(2000.0, 3000.0), 0.0)
        self.assertEqual(spinner_fade(2000.0, 2334.0), 1.0)


class PopUpTests(unittest.TestCase):
    """senpai, notice me! [w] 189511: long fake sliders on 1ms red lines stand
    by their length and fade in like a spinner (owner, 2026-10-28)."""

    def _view(self):
        view = gui.GameplayViewerView()
        view.resize(900, 200)
        points = [TimingPoint(time=0.0, beat_length=387.0, meter=4),
                  TimingPoint(time=1000.0, beat_length=1.0, meter=4),
                  TimingPoint(time=1001.0, beat_length=387.0, meter=4)]
        view.timing_points = view.beat_points = points
        view._beat_times = [0.0, 1000.0, 1001.0]
        view.slider_multiplier = 1.8
        view._rebuild_velocities()
        return view

    @staticmethod
    def _fake(length, time=1000):
        return HitObject(x=166, y=117, time=time, type=2, hit_sound=4,
                         extras=("L|181:87", "1", repr(length)))

    def test_it_stands_by_its_length_and_fades_in(self):
        view = self._view()
        hit = view._hit_x()
        lane = view.width() - hit
        view.current_time = 950.0
        at = lambda length: (view._pop_up(self._fake(length))[0] - hit) / lane
        # Stable: -300 at ~17% of the lane, the farthest well inside its first 75%.
        self.assertAlmostEqual(at(-300.0), 0.17, delta=0.01)
        self.assertLess(at(-510.0), 0.75)
        view.current_time = 600.0
        self.assertEqual(view._pop_up(self._fake(-510.0))[2], 1.0)
        # Shown 300 / 180 beats before (645ms), -510 2.83 beats (1097ms).
        view.current_time = 1000.0 - 650.0
        self.assertEqual(view._pop_up(self._fake(-300.0))[2], 0.0)
        self.assertGreater(view._pop_up(self._fake(-510.0))[2], 0.0)
        view.current_time = 1000.0 - 600.0
        self.assertGreater(view._pop_up(self._fake(-300.0))[2], 0.0)
        # Gone at its time: no fade out in stable.
        view.current_time = 1000.0
        self.assertIsNone(view._pop_up(self._fake(-300.0)))

    def test_at_an_ordinary_speed_it_is_the_usual_fake_slider(self):
        view = self._view()
        view.current_time = 450.0
        self.assertIsNone(view._pop_up(self._fake(-300.0, time=500)))


class RollTickTintTests(unittest.TestCase):
    """As stable tints `sliderscorepoint` (owner's screenshot, IT'S SOOOO
    CREATIVE): yellow on the beat, pale orange on the half beat, the image's
    own colour elsewhere."""

    def _tints(self, beat_length):
        view = gui.GameplayViewerView()
        view.resize(900, 200)
        point = TimingPoint(time=0.0, beat_length=beat_length, meter=4)
        view.timing_points = view.beat_points = [point]
        view._beat_times = [0.0]
        view.slider_multiplier = 1.4
        view._rebuild_velocities()
        roll = HitObject(x=256, y=192, time=1000, type=2, hit_sound=0,
                         extras=("L|624:192", "1", "140.0"))
        view.notes, view.note_times = [roll], [1000.0]
        view._end_times = {roll.uid: view._compute_end_time(roll)}
        view._phantom_ends, view._max_extend_ms = {}, view._end_times[roll.uid] - 1000.0
        view.current_time = 900.0
        seen = []
        view._draw_roll_ticks = lambda painter, ticks, *rest: seen.extend(t for _x, t in ticks)
        view.grab()
        return seen

    def test_yellow_on_the_beat_orange_on_the_half(self):
        from ui.common import DRUMROLL_HALF_TICK_COLOR as half, DRUMROLL_TICK_COLOR as beat
        # 200 BPM, 1/4 ticks from 1000 (not on the 300ms grid: 1000 = 3.33 beats).
        self.assertTrue(all(t is None for t in self._tints(300.0)))
        # 120 BPM, 1/8 ticks from 1000 (beat 2): beat, then 1/8s, the half at 4.
        tints = self._tints(500.0)
        self.assertEqual(tints[0], beat)
        self.assertEqual(tints[4], half)
        self.assertEqual(tints[8], beat)
        self.assertTrue(all(tints[i] is None for i in (1, 2, 3, 5, 6, 7)))


class AutoplayRollOverNotesTests(unittest.TestCase):
    """Owner, 2026-10-28: a drumroll mapped across notes is played as the
    notes alone; one with nothing under it is rolled as before."""

    def _hits(self, notes):
        from chart.autoplay import FULL_ALT
        view = gui.GameplayViewerView()
        view.resize(900, 200)
        point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
        view.timing_points = view.beat_points = [point]
        view._beat_times = [0.0]
        view.slider_multiplier = 1.4
        view._rebuild_velocities()
        view.notes = sorted(notes, key=lambda n: n.time)
        view.note_times = [n.time for n in view.notes]
        view._end_times = {}
        for note in view.notes:
            end = view._compute_end_time(note)
            if end is not None:
                view._end_times[note.uid] = end
        view.set_autoplay((FULL_ALT, "1234", False))
        return view._all_hits

    @staticmethod
    def _roll():
        return HitObject(x=256, y=192, time=1000, type=2, hit_sound=0,
                         extras=("L|624:192", "1", "280.0"))  # 2 beats: 1000-2000

    def test_a_roll_over_notes_plays_only_the_notes(self):
        notes = [HitObject(x=256, y=192, time=t, type=1, hit_sound=h) for t, h in ((1250, 0), (1500, 2))]
        self.assertEqual(self._hits([self._roll(), *notes]), [1250.0, 1500.0])

    def test_a_roll_alone_is_rolled(self):
        self.assertGreater(len(self._hits([self._roll()])), 8)

    def test_the_note_before_a_nan_slider_is_not_hit(self):
        """Owner, 2026-10-28: autoplay can't hit it either (taiko-rs's
        `unhittable`): the circle at 1000, not 500 or 2000."""
        notes = [HitObject(x=256, y=192, time=t, type=1, hit_sound=0) for t in (500, 1000, 2000)]
        nan = HitObject(x=256, y=192, time=1499, type=2, hit_sound=0,
                        extras=("L|268:192", "1", "NaN"))
        self.assertEqual(self._hits([*notes, nan]), [500.0, 2000.0])

    def test_a_sub_ms_fake_slider_is_not_played(self):
        """Owner, 2026-10-28: written 0.1 to -0.1 long, left alone."""
        notes = [HitObject(x=256, y=192, time=t, type=1, hit_sound=0) for t in (500, 2000)]
        fakes = [HitObject(x=256, y=192, time=1000, type=2, hit_sound=0, extras=("L|268:192", "1", length))
                 for length in ("0.1", "-0.1", "0.001", "-0.001")]
        self.assertEqual(self._hits([*notes, *fakes]), [500.0, 2000.0])
        # Just past it, a roll is played again.
        roll = HitObject(x=256, y=192, time=1000, type=2, hit_sound=0, extras=("L|268:192", "1", "0.11"))
        self.assertIn(1000.0, self._hits([*notes, roll]))


class NanSliderTests(unittest.TestCase):
    def test_it_is_invisible_in_the_preview_and_has_no_ticks(self):
        from ui.common import drumroll_tick_times
        note = HitObject(x=256, y=192, time=1500, type=2, hit_sound=0,
                         extras=("L|268:192", "1", "NaN"))
        self.assertTrue(note.is_nan_slider)
        point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
        self.assertEqual(drumroll_tick_times(note, [point], [point], 1.4), [])
        names = [element for element, _c in DrumrollPartsTests()._preview_parts(float("nan"))]
        self.assertFalse({"taiko-roll-middle", "taiko-roll-end", "taikohitcircle"} & set(names))


class FakeSliderFreezeTests(unittest.TestCase):
    def test_it_stops_200_of_length_after_its_cap_lands(self):
        """Owner's stable test map, 2026-10-28, at 3x: a -200 (cap 238ms
        ahead) stops at its head's time, its track's left end on the target;
        a -100 (119ms) 119ms past it, its right end there. taiko-rs's
        `fake_freeze` is the same."""
        freeze = gameplay_view.fake_slider_freeze
        self.assertAlmostEqual(freeze(2000.0, 238.1, 200.0, 2000.0), 0.0)
        self.assertAlmostEqual(freeze(2000.0, 238.1, 200.0, 2100.0), 100.0)
        self.assertAlmostEqual(freeze(5000.0, 119.05, 100.0, 5119.05), 0.0)
        self.assertAlmostEqual(freeze(5000.0, 119.05, 100.0, 5169.05), 50.0)


class LunaticRoughPartyTests(unittest.TestCase):
    def test_a_fast_short_fake_slider_is_no_pop_up(self):
        """Lunatic Rough Party!! (owner, 2026-10-28): a -1 under a 100x green
        line on a 500ms red line crosses the view in under a frame, but in
        stable it scrolls in and goes at the target, not standing still."""
        point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
        fast = TimingPoint(time=0.0, beat_length=-1.0, meter=4, uninherited_flag=0)
        view = gui.GameplayViewerView()
        view.resize(1366, 300)
        view.timing_points = [point, fast]
        view.beat_points = [point]
        view.slider_multiplier = 1.4
        view._rebuild_velocities()
        note = HitObject(x=224, y=192, time=1000, type=2, hit_sound=0, extras=("L", "1", "-1"))
        view.current_time = 980.0
        self.assertGreater(16.0 * view.velocity_at(1000.0) * view.px_per_beat, view.width())
        self.assertIsNone(view._pop_up(note))
        view.close()

    def test_a_tiny_positive_one_goes_at_the_target(self):
        """Its first object, +0.0001: gone at the target like a circle, where
        a real roll passes through."""
        tiny = DrumrollPartsTests()._preview_parts
        names = [element for element, _c in tiny(0.0001, at=1490.0)]
        self.assertIn("taikohitcircle", names)
        self.assertEqual(tiny(0.0001, at=1510.0), [])
        names = [element for element, _c in tiny(400.0, at=1510.0)]
        self.assertIn("taikohitcircle", names)


class GimmickRollTickTests(unittest.TestCase):
    def test_a_roll_under_a_gimmick_line_has_osus_clamped_ticks(self):
        """We Love Hardcore (katacheh), 242401: a 600px roll under a 0.06ms
        red line and a near-zero SV came to 10^12 ticks and froze the editor
        (owner, 2026-10-28). osu!'s 6ms / 0.1x floors make it 600 / 14 beats
        of 6ms: 257ms at 1/2-beat ticks, 86 of them."""
        from ui.common import drumroll_tick_times
        note = HitObject(x=256, y=192, time=1000, type=2, hit_sound=0, extras=("L|268:192", "1", "600.25"))
        red = TimingPoint(time=0.0, beat_length=0.0632386404957066, meter=4)
        green = TimingPoint(time=500.0, beat_length=-1e12, uninherited_flag=0)
        ticks = drumroll_tick_times(note, [red], [red, green], 1.4)
        self.assertEqual(len(ticks), 86)
        self.assertEqual(ticks[1] - ticks[0], 3.0)


class DrawOrderTests(unittest.TestCase):
    """ppy/osu HitObjectContainer.Compare: the earlier StartTime is on top,
    whatever the SV and whatever the object. On one millisecond a slider goes
    over a circle, then file order."""

    def _order(self, notes, at, green=()):
        view = gui.GameplayViewerView()
        view.resize(900, 200)
        point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
        view.timing_points = [point, *green]
        view.beat_points = [point]
        view._beat_times = [0.0]
        view.slider_multiplier = 1.4
        view._rebuild_velocities()
        view.notes = sorted(notes, key=lambda n: n.time)
        view.note_times = [n.time for n in view.notes]
        view._end_times, view._phantom_ends, view._max_extend_ms = {}, {}, 0.0
        for note in view.notes:
            end = view._compute_end_time(note)
            if end is not None:
                view._end_times[note.uid] = end
                view._max_extend_ms = max(view._max_extend_ms, end - note.time)
            else:
                phantom = view._compute_phantom_end(note)
                if phantom is not None:
                    view._phantom_ends[note.uid] = phantom
        view.current_time = at
        painted = []
        original = gui.GameplayViewerView._draw_note

        def spy(self, painter, note, *args):
            painted.append(note.uid)
            return original(self, painter, note, *args)

        gui.GameplayViewerView._draw_note = spy
        try:
            view.grab()
        finally:
            gui.GameplayViewerView._draw_note = original
        return painted

    def _pair(self, second_time, second_sv, second=None):
        """A note at 1500 at 2.5x, and `second` (a fake slider by default) at
        `second_time` and `second_sv`. Returns their positions in paint order."""
        circle = HitObject(x=256, y=192, time=1500, type=1, hit_sound=0)
        other = second or HitObject(x=256, y=192, time=second_time, type=2, hit_sound=0,
                                    extras=("L|624:192", "1", "-0.001"))
        green = [TimingPoint(time=1500.0, beat_length=-40.0, meter=4, uninherited_flag=0)]
        if second_time != 1500:
            green.append(TimingPoint(time=float(second_time), beat_length=-100.0 / second_sv,
                                     meter=4, uninherited_flag=0))
        else:
            green[0].beat_length = -100.0 / second_sv
        # Both on screen: one off the edge is never handed to _draw_note.
        order = self._order([circle, other], at=1450.0, green=green)
        return order.index(circle.uid), order.index(other.uid)

    def test_a_shiny_stack_goes_under_its_note(self):
        """Hyper Bass [Drop the GIMMICK]: the note at T, fake sliders at T+1."""
        circle, fake = self._pair(1501, 6.2)
        self.assertGreater(circle, fake)

    def test_sv_does_not_lift_a_later_object_over_an_earlier_one(self):
        circle, fake = self._pair(1501, 0.5)
        self.assertGreater(circle, fake)

    def test_on_one_millisecond_the_fake_slider_is_on_top(self):
        """Owner, 2026-10-27: a fake slider on a note's own millisecond goes
        over it, whichever comes first in the file."""
        for fake_first in (False, True):
            circle = HitObject(x=256, y=192, time=1500, type=1, hit_sound=0)
            fake = HitObject(x=256, y=192, time=1500, type=2, hit_sound=0,
                             extras=("L|624:192", "1", "-0.001"))
            order = self._order([fake, circle] if fake_first else [circle, fake], at=1000.0)
            self.assertGreater(order.index(fake.uid), order.index(circle.uid), fake_first)

    def test_a_real_drumroll_is_ordered_by_time_like_a_note(self):
        roll = HitObject(x=256, y=192, time=1400, type=2, hit_sound=0,
                         extras=("L|624:192", "1", "400.0"))
        circle = HitObject(x=256, y=192, time=1500, type=1, hit_sound=0)
        order = self._order([roll, circle], at=1000.0)
        self.assertGreater(order.index(roll.uid), order.index(circle.uid))

    def test_a_spinner_is_ordered_by_time_like_a_note(self):
        circle = HitObject(x=256, y=192, time=1500, type=1, hit_sound=0)
        spinner = HitObject(x=256, y=192, time=1600, type=8, hit_sound=0,
                            extras=("2500",))
        order = self._order([circle, spinner], at=1000.0)
        self.assertGreater(order.index(circle.uid), order.index(spinner.uid))


class PlayfieldTests(unittest.TestCase):
    """The skin's playfield, from the wiki's own element list.

    Each element is optional on its own -- the same per-element fallback the
    notes use -- so what is pinned here is *which* element is asked for and
    *when*, not that any particular skin supplies it.
    """

    ELEMENTS = (
        "taiko-bar-right", "taiko-bar-right-glow",
        "taiko-barline", "approachcircle",
        "taikohitcircle", "taikohitcircleoverlay",
        "taiko-bar-left", "taiko-drum-inner", "taiko-drum-outer", "taiko-glow",
        "taikobigcircle",
    )
    DRUM = ("taiko-bar-left", "taiko-drum-inner", "taiko-drum-outer")

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        folder = Path(self._temp.name)
        for name in self.ELEMENTS:
            image = QImage(32, 32, QImage.Format_ARGB32)
            image.fill(QColor("white"))
            image.save(str(folder / f"{name}.png"))
        self.skin = skin.TaikoSkin(folder)
        self.asked: list[str] = []
        for method in ("scaled", "stretched", "natural"):
            inner = getattr(self.skin, method)
            setattr(self.skin, method, self._spy(inner))

    def _spy(self, inner):
        def wrapped(element, *args, **kwargs):
            self.asked.append(element)
            return inner(element, *args, **kwargs)
        return wrapped

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _view(self, kiai: bool, at: float = 1000.0, autoplay=None, plain=False):
        view = gui.GameplayViewerView()
        view.autoplay = autoplay
        view.plain_stage = plain
        view.resize(900, 200)
        point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
        view.timing_points = [point]
        view.beat_points = [point]
        view._beat_times = [0.0]
        view.slider_multiplier = 1.4
        view.kiai_bands = [(0, 20000)] if kiai else []
        view._rebuild_velocities()
        view.set_skin(self.skin)
        view.notes = []
        view.note_times = []
        view._end_times, view._phantom_ends, view._max_extend_ms = {}, {}, 0.0
        view.current_time = at
        self.asked.clear()
        view.grab()
        return view

    def test_the_playfield_is_drawn_outside_kiai_too(self):
        self._view(kiai=False)
        for element in ("taiko-bar-right", "approachcircle"):
            with self.subTest(element=element):
                self.assertIn(element, self.asked)

    def test_the_lane_kiai_overlay_is_only_asked_for_during_kiai(self):
        self._view(kiai=False)
        self.assertNotIn("taiko-bar-right-glow", self.asked)
        self._view(kiai=True, at=0.0)
        self.assertIn("taiko-bar-right-glow", self.asked)

    def test_no_glow_is_drawn_at_the_hit_target_without_autoplay(self):
        """`taiko-glow` is the autoplay's; `lighting` is a hit explosion's,
        and nothing is judged here at all."""
        self._view(kiai=True, at=50.0)
        for element in ("taiko-glow", "lighting"):
            with self.subTest(element=element):
                self.assertNotIn(element, self.asked)

    def test_autoplay_draws_the_drum_and_the_kiai_glow(self):
        """The halves only while lit -- see the next test."""
        self._view(kiai=True, at=50.0, autoplay=("full", "1234", False))
        for element in ("taiko-bar-left", "taiko-glow"):
            with self.subTest(element=element):
                self.assertIn(element, self.asked)

    def test_the_hit_target_holds_a_grey_note(self):
        """TaikoLegacyHitTarget's taikobigcircle, autoplay or not -- but not on
        song select's bare stage."""
        self._view(kiai=False)
        self.assertIn("taikobigcircle", self.asked)
        self._view(kiai=False, plain=True)
        self.assertNotIn("taikobigcircle", self.asked)

    def test_song_selects_bare_stage_never_shows_the_autoplay(self):
        self._view(kiai=True, at=50.0, autoplay=("full", "1234", False), plain=True)
        for element in (*self.DRUM, "taiko-glow"):
            with self.subTest(element=element):
                self.assertNotIn(element, self.asked)

    def test_a_hit_lights_its_half_and_lets_go(self):
        """LegacyHalfDrum: lit by 80ms, fading from 100ms, gone at 150ms --
        Delay counts from the press, not from the end of the fade-in."""
        view = self._view(kiai=False, autoplay=("full", "1234", False))
        view.notes = [HitObject(x=256, y=192, time=1000, type=1, hit_sound=0)]
        view._rebuild_hits()
        view.current_time = 1100.0
        self.assertEqual(view._half_alpha(3), 1.0)  # right don, right-handed
        self.assertEqual(view._half_alpha(2), 0.0)
        self.asked.clear()
        view.grab()
        self.assertIn("taiko-drum-inner", self.asked)
        self.assertNotIn("taiko-drum-outer", self.asked)
        view.current_time = 1125.0
        self.assertAlmostEqual(view._half_alpha(3), 0.5)
        view.current_time = 1150.0
        self.assertEqual(view._half_alpha(3), 0.0)
        # DT/NC: the song runs 1.5x, the flash still lasts nomod's 150ms
        # (owner, 2026-10-19), so it is half gone 187.5ms of song time in.
        for mod in ("DT", "NC"):
            view.set_mods({mod})
            view.current_time = 1000.0 + 125.0 * 1.5
            self.assertAlmostEqual(view._half_alpha(3), 0.5)
            view.current_time = 1000.0 + 150.0 * 1.5
            self.assertEqual(view._half_alpha(3), 0.0)
        view.set_mods(set())

    def test_the_bar_is_stretched_to_the_whole_view(self):
        """1024x200 art "stretched to fit screen width" -- aspect deliberately
        ignored, which is why it does not go through `scaled`."""
        view = self._view(kiai=False)
        bar = self.skin.stretched("taiko-bar-right", view.width(), view.height())
        self.assertEqual((bar.width(), bar.height()), (view.width(), view.height()))

    def test_the_input_drum_is_not_drawn_without_autoplay(self):
        """It is where the player hits, and with autoplay off nobody is."""
        self._view(kiai=False)
        for element in ("taiko-bar-left", "taiko-drum-inner", "taiko-drum-outer"):
            with self.subTest(element=element):
                self.assertNotIn(element, self.asked)

    def test_a_skin_with_no_playfield_still_draws_the_notes(self):
        """Per element, not per skin: the whole point of the fallback."""
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            image = QImage(32, 32, QImage.Format_ARGB32)
            image.fill(QColor("white"))
            image.save(str(folder / "taikohitcircle.png"))
            self.skin = skin.TaikoSkin(folder)
            view = self._view(kiai=False)
            self.assertEqual(view.width(), 900)   # painted without raising


class PlayfieldKiaiWashTests(unittest.TestCase):
    """The built-in white wash is a stand-in for the skin's own kiai art.
    Drawn as well as it, a skinned lane would be washed twice over."""

    def _corner_lift(self, elements) -> int:
        """How much the far corner of the lane brightens on the beat.

        The corner, deliberately: nothing else that pulses reaches the far end
        of the lane, so a change there is the full-rect wash and nothing else.
        """
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in elements:
                image = QImage(8, 8, QImage.Format_ARGB32)
                image.fill(QColor("white"))
                image.save(str(folder / f"{name}.png"))
            loaded = skin.TaikoSkin(folder)

            def render(at):
                view = gui.GameplayViewerView()
                view.resize(400, 200)
                point = TimingPoint(time=0.0, beat_length=500.0, meter=4)
                view.timing_points = [point]
                view.beat_points = [point]
                view._beat_times = [0.0]
                view.kiai_bands = [(0, 20000)]
                view._rebuild_velocities()
                view.set_skin(loaded)
                view.notes, view.note_times = [], []
                view._end_times, view._phantom_ends, view._max_extend_ms = {}, {}, 0.0
                view.current_time = at
                return view.grab().toImage().pixelColor(399, 4)

            on_beat = render(0.0)          # pulse 1.0
            off_beat = render(499.9)       # pulse ~0.0
            return on_beat.red() - off_beat.red()

    def test_a_skin_with_no_kiai_art_gets_the_built_in_wash(self):
        self.assertGreater(self._corner_lift(["taikohitcircle"]), 0)

    def test_a_skin_that_lights_its_own_lane_is_not_washed_as_well(self):
        self.assertEqual(
            self._corner_lift(["taikohitcircle", "taiko-bar-right-glow"]), 0)


class SliderMultiplierScrollTests(unittest.TestCase):
    """The map's SliderMultiplier has to reach the scroll speed.

    osu!taiko's scroll distance is `100 * SliderMultiplier * 1.4 * ScrollSpeed
    / beatLength`. This view left SliderMultiplier out of it, so two maps that
    scroll three times apart in game previewed at identical spacing.
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self._path = write_fixture(Path(self._temp.name), "full_v14")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _view_with_multiplier(self, multiplier: float):
        document = parse_osu(self._path)
        document.slider_multiplier = multiplier
        return _viewer(document)

    def test_a_faster_multiplier_scrolls_proportionally_faster(self):
        slow = self._view_with_multiplier(0.7)
        fast = self._view_with_multiplier(2.1)
        self.assertAlmostEqual(
            fast.velocity_at(1000.0) / slow.velocity_at(1000.0), 3.0, places=6)

    def test_the_assumed_multiplier_is_the_unchanged_baseline(self):
        """Normalised against the value assumed before the parser read the real
        one, so an ordinary map previews at the scale it always did."""
        view = self._view_with_multiplier(gui.SLIDER_MULTIPLIER_ASSUMED)
        self.assertAlmostEqual(view._scroll_scale, 1.0, places=9)
        self.assertAlmostEqual(view.velocity_at(1000.0), 1 / 500.0, places=9)

    def test_a_nonsense_multiplier_cannot_stop_the_chart(self):
        """Hand-edited maps carry zeroes; a zero here would freeze every object
        on the hit position and divide by nothing downstream."""
        view = self._view_with_multiplier(0.0)
        self.assertGreater(view.velocity_at(1000.0), 0.0)


class OsuVisibleRangeTests(unittest.TestCase):
    """The preview shows what osu! shows: the scroll is sized by the view's
    height, as everything else in it is, and nothing past a 16:9 screen.

    Numbers from ppy/osu master (read 2026-09-24): `TaikoPlayfield` puts the
    hit target's centre 256 playfield units in, and
    `TaikoPlayfieldAdjustmentContainer.ComputeTimeRange` gives the time a
    screen holds as `(aspect * 480 - 160) / 100 * 1000 / 1.4`, clamped to 16:9
    and divided by the scroll multiplier `SliderMultiplier * SV * 1000 /
    beatLength`.
    """

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.path = write_fixture(Path(self._temp.name), "full_v14")  # SM 1.4, 500ms beat

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _view(self, width: int, height: int) -> gui.GameplayViewerView:
        view = _viewer(parse_osu(self.path))
        view.resize(width, height)
        view.current_time = 1000.0
        return view

    def test_the_hit_target_is_256_playfield_units_in(self):
        view = self._view(1882, 170)
        self.assertAlmostEqual(view._hit_x(), 170 * 256 / 200, places=6)

    def test_a_screen_holds_what_compute_time_range_says(self):
        view = self._view(1882, 170)
        time_range = (16 / 9 * 480 - 160) / 100 * 1000 / 1.4
        multiplier = 1.4 * 1.0 * 1000 / 500.0
        visible = (view.osu_edge_x() - view._hit_x()) / (view.velocity_at(1000.0) * view.px_per_beat)
        self.assertAlmostEqual(visible, time_range / multiplier, places=3)

    def test_a_wider_view_shows_no_more_of_the_chart(self):
        """LockPlayfieldAspectRange: a wider screen makes osu!'s playfield
        taller, not longer, so past 16:9 there is nothing to show."""
        narrow, wide = self._view(1300, 170), self._view(3000, 170)
        self.assertAlmostEqual(narrow.osu_edge_x(), wide.osu_edge_x(), places=6)
        self.assertLess(wide.osu_edge_x(), wide.width())

    def test_a_narrow_view_ends_at_its_own_width(self):
        view = self._view(600, 170)
        self.assertEqual(view.osu_edge_x(), 600.0)

    def test_the_view_takes_an_osu_screens_height(self):
        """Height follows width, so the whole view is osu!'s 16:9 screen:
        nothing dark past its edge, no chart osu! would not show. The owner's
        call, against an osu!stable screenshot of the same frame."""
        view = self._view(1882, 170)
        view.show()  # a hidden widget's resize event waits until it is shown
        self.addCleanup(view.close)
        self.assertEqual(view.height(), round(1882 / (16 / 9 * 480 * 768 / 480 / 200)))
        self.assertAlmostEqual(view.osu_edge_x(), view.width(), delta=3)

    def test_a_narrow_view_keeps_its_floor(self):
        view = self._view(600, 170)
        view.show()
        self.addCleanup(view.close)
        self.assertEqual(view.height(), common.GAMEPLAY_MIN_HEIGHT)


class BarlineTests(unittest.TestCase):
    """`barline_times` answers for the frame at `current_time`, so each test
    parks the playhead inside the range it asks about. A section is only walked
    across the time it is on screen for at its own velocity -- without that, one
    absurd-BPM section spends the whole per-frame budget off the left edge."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        path = write_fixture(Path(self._temp.name), "full_v14")
        self.view = _viewer(parse_osu(path))
        # Wide: at the preview's real scale an 800px view reaches only ~1.2s,
        # and `barline_times` bounds its walk by the width. These tests are
        # about which bars a section makes, not how far one screen reaches --
        # OsuVisibleRangeTests owns that.
        self.view.resize(3000, 170)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_a_barline_every_meter_beats_from_an_uninherited_point(self):
        # 500ms/beat, meter 4 -> every 2000ms, up to the next red line at 6000.
        self.view.current_time = 2000.0
        self.assertEqual(self.view.barline_times(0.0, 5999.0), [0.0, 2000.0, 4000.0])

    def test_the_range_is_respected_without_walking_the_whole_section(self):
        self.view.current_time = 3500.0
        self.assertEqual(self.view.barline_times(2500.0, 4500.0), [4000.0])

    def test_omit_first_barline_drops_the_line_on_the_point_only(self):
        """The 6000ms red line sets effects bit 3; barline gimmicks set it in
        bulk, so its later measure lines must still appear."""
        self.view.current_time = 7750.0
        times = self.view.barline_times(6000.0, 9500.0)
        self.assertNotIn(6000.0, times)
        # 400ms/beat, meter 4 -> every 1600ms.
        self.assertEqual(times, [7600.0, 9200.0])

    def test_clearing_the_flag_brings_the_points_own_barline_back(self):
        point = next(p for p in self.view.beat_points if p.time == 6000.0)
        point.set_omit_first_barline(False)
        self.view.current_time = 6000.0
        self.assertIn(6000.0, self.view.barline_times(6000.0, 9500.0))

    def test_a_map_with_no_timing_points_draws_no_barlines(self):
        self.view.beat_points = []
        self.assertEqual(self.view.barline_times(0.0, 10000.0), [])

    def _document(self, *points: gui.TimingPoint):
        document = parse_osu(write_fixture(Path(self._temp.name), "full_v14"))
        document.timing_points = list(points)
        return document

    @staticmethod
    def _green(time_ms: float, sv: float) -> gui.TimingPoint:
        """An inherited point. TimingPoint defaults uninherited_flag to 1, so a
        negative beat_length alone still reads as a red line."""
        return gui.TimingPoint(time=time_ms, beat_length=-100.0 / sv, uninherited_flag=0)

    def test_a_green_line_inside_a_section_does_not_cut_its_barlines_short(self):
        """Spacing was judged from the velocity at the *red* line, but a green
        line partway through moves every measure line after it. At 0.25x the
        lines sit four times closer, so four times as many fit on screen -- the
        old reach and line count were both a quarter of what was needed, and the
        right-hand half of the screen lost its barlines."""
        view = _viewer(
            self._document(gui.TimingPoint(time=0.0, beat_length=500.0), self._green(4000.0, 0.25))
        )
        view.current_time = 10000.0
        times = view.barline_times(*view.visible_time_range())

        # 0.25x/500ms-beat at 200px tall -> 0.157 px/ms, so a 2000ms measure
        # spans 314px and the 800px view reaches 5000ms, thousands past where
        # the red line's velocity would have stopped it.
        self.assertIn(12000.0, times, "barlines stop short of the right edge")
        self.assertIn(14000.0, times)
        for time_ms in times:
            self.assertAlmostEqual(time_ms % 2000.0, 0.0, places=6)

    def test_a_crawling_section_gets_every_line_that_fits_not_a_fixed_few(self):
        """The count is geometry, not a budget. At 0.01x a 2000ms measure is 8px,
        so dozens of barlines are genuinely on screen at once -- the old code
        sized the walk from the red line's velocity and handed back three."""
        view = _viewer(
            self._document(gui.TimingPoint(time=0.0, beat_length=500.0), self._green(1000.0, 0.01))
        )
        view.current_time = 30000.0
        times = view.barline_times(*view.visible_time_range())

        self.assertGreater(len(times), 20, "a 0.01x section fits far more than a handful")
        # Both of these sit within the 800px view; the old walk reached neither.
        self.assertIn(2000.0, times)
        self.assertIn(58000.0, times)
        self.assertEqual(len(times), len(set(times)))


class PaintAndInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        path = write_fixture(Path(self._temp.name), "full_v14")
        self.view = _viewer(parse_osu(path))

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_painting_every_note_shape_does_not_raise(self):
        for time_ms in (0.0, 1500.0, 3500.0, 5500.0, 54692.0):
            with self.subTest(time_ms=time_ms):
                self.view.current_time = time_ms
                self.view.grab()

    def test_a_near_stopped_section_reaches_the_end_of_the_map(self):
        """A 0.01x section puts minutes on one screen: the visible range is not
        capped, so the map's last object is still looked for (owner,
        2026-10-28), and painting it does not hang."""
        self.view.timing_points[1].set_sv(0.01)
        self.view._rebuild_velocities()
        self.view.current_time = 2000.0
        _start, end = self.view.visible_time_range()
        self.assertGreater(end, self.view.notes[-1].time)
        self.view.grab()

    def test_a_barline_gimmick_section_does_not_generate_millions_of_lines(self):
        """Absurd-BPM red lines put a measure every 0.0004ms. Past the right
        edge none of them is visible, and asking for the time range's worth
        froze the view.

        There is no line-count cap to lean on -- the bound is the screen, since
        two lines rounding to the same pixel column are one line."""
        gimmick = [gui.TimingPoint(time=0.0, beat_length=500.0)]
        for i in range(200):
            gimmick.append(gui.TimingPoint(time=1000.0 + i * 5.0, beat_length=0.0001))
        self.view.timing_points = gimmick
        self.view.beat_points = list(gimmick)
        self.view._beat_times = [point.time for point in gimmick]
        self.view._rebuild_velocities()
        self.view.current_time = 1000.0
        start, end = self.view.visible_time_range()
        times = self.view.barline_times(start, end)
        self.assertLessEqual(len(times), self.view.width() + 4)
        self.assertEqual(len(times), len(set(times)))
        self.view.grab()

    def _fake_slider(self, length: float, time_ms: int = 1500, slides: int = 1):
        """The shape of a real fake-slider line, extras and all.

            256,192,54692,2,12,L|624:192,643,-0.0001

        `length` and `slides` are read off `extras`, so building the note any
        other way would not exercise the path the parser produces.
        """
        from model.hit_object import HitObject

        return HitObject(
            x=256, y=192, time=time_ms, type=2, hit_sound=12,
            extras=("L|624:192", str(slides), repr(length)),
        )

    def test_a_negative_length_slider_reaches_back_before_its_own_head(self):
        """A fake slider is a drumroll whose length is negative, so the duration
        derived from it is negative and the object ends before it starts. No
        tick is ever generated, which is why nothing is hittable while osu!
        still draws the head."""
        note = self._fake_slider(-50.0)
        self.view.notes = [note]
        self.view.note_times = [note.time]
        self.view._end_times = {}
        self.view._phantom_ends = {}
        phantom = self.view._compute_phantom_end(note)

        self.assertIsNotNone(phantom)
        self.assertLess(phantom, note.time)
        self.assertIsNone(
            self.view._compute_end_time(note),
            "a negative length must never be mistaken for a playable drumroll",
        )

    def test_a_positive_length_slider_has_no_phantom_body(self):
        """The other half of the same rule: a positive length is a real,
        scoreable drumroll and must be drawn forwards, not backwards."""
        note = self._fake_slider(50.0)
        self.assertIsNone(self.view._compute_phantom_end(note))
        self.assertIsNotNone(self.view._compute_end_time(note))

    def test_the_phantom_extent_is_to_scale_not_padded_to_visibility(self):
        """The canonical `-0.0001` fake slider reaches about a tenth of a pixel
        behind its head, and showing it larger would be inventing geometry the
        map does not have. Its invisibility is the trick working."""
        tiny = self._fake_slider(-0.0001, slides=643)
        big = self._fake_slider(-500.0)
        self.view.current_time = 0.0

        def extent(note):
            end = self.view._compute_phantom_end(note)
            velocity = self.view.velocity_at(note.time)
            return abs(end - note.time) * velocity * self.view.px_per_beat

        self.assertLess(extent(tiny), 1.0)
        self.assertGreater(extent(big), 100.0)

    def test_painting_a_fake_slider_stack_does_not_raise(self):
        """A shiny note is a stack of fake sliders on one millisecond."""
        notes = [self._fake_slider(-1.0) for _ in range(20)]
        self.view.notes = notes
        self.view.note_times = [note.time for note in notes]
        self.view._end_times = {}
        self.view._phantom_ends = {
            note.uid: self.view._compute_phantom_end(note) for note in notes
        }
        self.view.current_time = 1500.0
        self.view.grab()

    def test_a_real_drumroll_is_drawn_over_the_barlines(self):
        """TaikoPlayfield puts the barlines in an underlay beneath every hit
        object, drumroll bodies included."""
        from model.hit_object import HitObject

        document = parse_osu(write_fixture(Path(self._temp.name), "full_v14"))
        document.timing_points = [gui.TimingPoint(time=0.0, beat_length=500.0)]
        # 500ms/beat, meter 4 -> a barline every 2000ms; the drumroll covers it.
        document.hit_objects = [
            HitObject(x=256, y=192, time=1000, type=2, hit_sound=0, extras=("L|624:192", "1", "5000"))
        ]
        view = _viewer(document)
        view.current_time = 4000.0
        image = view.grab().toImage()
        row = view.height() // 2
        barline_x = round(view.x_for_time(4000.0))
        self.assertEqual(
            image.pixel(barline_x, row),
            image.pixel(barline_x + 6, row),
            "the drumroll body covers the barline",
        )

    def test_notes_are_blitted_from_the_sprite_cache(self):
        """Stroking an antialiased ellipse per note per frame was most of the
        frame; the cache is what makes a dense map scroll."""
        common._NOTE_SPRITES.clear()
        self.view.grab()
        self.assertTrue(common._NOTE_SPRITES, "no note sprite was cached")
        cached = len(common._NOTE_SPRITES)
        for offset in range(5):
            self.view.current_time = 1000.0 + offset * 16.0
            self.view.grab()
        self.assertEqual(len(common._NOTE_SPRITES), cached, "sprites are being rebuilt per frame")

    def test_a_wheel_notch_moves_one_snap_division(self):
        self.view.snap_divisor = 4
        self.view.current_time = 1000.0
        _wheel(self.view, -120)
        self.assertAlmostEqual(self.view.current_time, 1125.0, places=3)

    def test_shift_wheel_moves_one_whole_beat(self):
        self.view.snap_divisor = 4
        self.view.current_time = 1000.0
        _wheel(self.view, -120, Qt.KeyboardModifier.ShiftModifier)
        self.assertAlmostEqual(self.view.current_time, 1500.0, places=3)

    def test_ctrl_wheel_does_not_rescale_the_view(self):
        """The viewer shows the chart at the speed the player sees it at, so
        there is nothing for a zoom gesture to mean here."""
        before = self.view.px_per_beat
        for _ in range(5):
            _wheel(self.view, 120, Qt.KeyboardModifier.ControlModifier)
            _wheel(self.view, -120, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.view.px_per_beat, before)

    def test_ctrl_wheel_does_not_seek(self):
        self.view.current_time = 1000.0
        _wheel(self.view, 120, Qt.KeyboardModifier.ControlModifier)
        self.assertAlmostEqual(self.view.current_time, 1000.0, places=6)


class GameplayViewIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        self.path = write_fixture(directory, "full_v14")

        self.window = gui.MainWindow()
        self.window.show()
        self.window._load_map_path(self.path, refresh_difficulties=True)
        self.state = self.window.state
        self.window._add_editor_view("gameplay", self.path)

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _frame(self):
        return next(f for f in self.window._editor_views if f.view_type == "gameplay")

    def test_the_gameplay_view_type_is_a_real_widget_not_a_placeholder(self):
        view = self._frame().gameplay_view
        self.assertIsInstance(view, gui.GameplayViewerView)
        self.assertGreater(len(view.notes), 0)
        self.assertIn(view, self.window._gameplay_views)

    def test_it_follows_the_shared_playhead(self):
        view = self._frame().gameplay_view
        self.window.seek_audio(3000)
        self.assertAlmostEqual(view.current_time, 3000.0, places=6)

    def test_placing_a_note_refreshes_it(self):
        view = self._frame().gameplay_view
        before = len(view.notes)
        self.window._place_note(self.path, "don", 1200.0, False, False)
        self.assertEqual(len(view.notes), before + 1)

    def test_an_sv_edit_refreshes_it(self):
        """A green line moves every note after it in this view, so the
        timing-point refresh path has to reach it too."""
        view = self._frame().gameplay_view
        inherited = next(p for p in self.state.document.timing_points if not p.uninherited)
        before = view.velocity_at(inherited.time + 1.0)
        self.window._edit_sv_point(self.path, inherited.uid, inherited.sv_multiplier * 2)
        self.assertAlmostEqual(view.velocity_at(inherited.time + 1.0), before * 2, places=9)

    def test_it_joins_the_editor_pages_global_snap(self):
        view = self._frame().gameplay_view
        self.window._editor_change_snap_from_wheel(1)
        self.assertEqual(view.snap_divisor, int(self.window.editor_snap_combo.currentData()))

    def test_closing_it_unregisters_it_everywhere(self):
        frame = self._frame()
        view = frame.gameplay_view
        self.window._close_editor_view(frame)
        self.assertNotIn(view, self.window._gameplay_views)
        self.assertNotIn(view, self.window._editor_snap_views)


if __name__ == "__main__":
    unittest.main()
