"""sv_curves: reading a chart's green lines back as the curves that wrote them."""
from __future__ import annotations

import random
import unittest

from model.hit_object import HitObject, TYPE_CIRCLE
from osu_io.timing import TimingPoint
from chart import sv_curves
from chart.sv_curves import ANTI, BARLINE, STEP, anchors, build_model, detect_runs, sv_ease


def _note(time_ms: float) -> HitObject:
    return HitObject(x=256, y=192, time=int(time_ms), type=TYPE_CIRCLE, hit_sound=0)


def _sweep(function, start, end, times, offsets=None):
    """Generate SV's numbers: `a + (b - a) * ease(t)` by note time, each
    green line offset back before its note."""
    offsets = offsets or [0] * len(times)
    span = times[-1] - times[0]
    greens = []
    for time_ms, offset in zip(times, offsets):
        t = (time_ms - times[0]) / span
        value = start + (end - start) * sv_ease(function, t, start, end)
        greens.append(TimingPoint.inherited_at(time_ms - offset, value))
    return greens


def _irregular_times(count, seed=1):
    rng = random.Random(seed)
    times, at = [], 1000.0
    for _ in range(count):
        times.append(at)
        at += rng.choice((125, 250, 375, 500))
    return times


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.red = TimingPoint.uninherited_at(0, 120)

    def _runs(self, greens, times, mode=BARLINE):
        notes = [_note(time_ms) for time_ms in times]
        return detect_runs(anchors([self.red, *greens], notes, times[0], times[-1]), mode)

    def test_every_generate_sv_curve_comes_back_as_itself(self):
        times = _irregular_times(12)
        offsets = [random.Random(index).randint(5, 15) for index in range(len(times))]
        for function in ("linear", "exp1.3", "exp1.6", "sin_in", "sin_out", "sin", "true_exp", "exp2.4"):
            with self.subTest(function=function):
                runs = self._runs(_sweep(function, 1.0, 2.5, times, offsets), times)
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0].function, function)
                self.assertEqual((runs[0].start.time, runs[0].end.time), (times[0], times[-1]))

    def test_two_sweeps_split_on_their_shared_turning_point(self):
        times = _irregular_times(15)
        up = _sweep("linear", 1.0, 1.5, times[:8])
        down = _sweep("exp1.6", 1.5, 0.8, times[7:])[1:]
        runs = self._runs([*up, *down], times)
        self.assertEqual([run.function for run in runs], ["linear", "exp1.6"])
        self.assertEqual(runs[0].end.time, times[7])
        self.assertEqual(runs[1].start.time, times[7])

    def test_a_lone_jump_is_a_step_for_barlines_and_a_ramp_for_anti_barline(self):
        times = [1000.0, 2000.0]
        greens = [TimingPoint.inherited_at(990, 1.0), TimingPoint.inherited_at(1990, 1.5)]
        self.assertEqual(self._runs(greens, times, BARLINE)[0].function, STEP)
        self.assertEqual(self._runs(greens, times, ANTI)[0].function, "linear")

    def test_a_green_offset_back_belongs_to_the_next_note_but_never_past_the_previous(self):
        times = [1000.0, 1100.0, 2000.0]
        found = anchors([self.red, TimingPoint.inherited_at(990, 1.2), TimingPoint.inherited_at(1050, 1.4)],
                        [_note(t) for t in times], 1000, 2000)
        self.assertEqual([anchor.time for anchor in found], [1000.0, 1100.0])
        # Two lines before one note: only the last is the note's.
        found = anchors([self.red, TimingPoint.inherited_at(1300, 1.2), TimingPoint.inherited_at(1900, 1.4)],
                        [_note(t) for t in times], 1000, 2000)
        self.assertEqual([anchor.time for anchor in found], [1300.0, 2000.0])


class ModelTests(unittest.TestCase):
    def test_nothing_is_interpolated_where_the_chart_has_no_green_line(self):
        times = [1000.0, 1500.0, 2000.0, 2500.0]
        greens = _sweep("linear", 1.0, 2.5, times[:3], [10, 10, 10])
        red = TimingPoint.uninherited_at(0, 120)
        second_red = TimingPoint.uninherited_at(2200, 120)  # resets SV: a new group
        points = [red, *greens, second_red, TimingPoint.inherited_at(2490, 3.0)]
        model = build_model(points, [_note(t) for t in times], 0, 3000, ANTI)
        self.assertAlmostEqual(model.value_at(1250), 1.375, places=6, msg="inside the sweep: the curve")
        self.assertEqual(model.value_at(500), 1.0, "before the first green line: the chart's own")
        self.assertEqual(model.value_at(2300), 1.0, "after a red line: its reset, not a ramp to 3.0")
        self.assertEqual(model.value_at(2600), 3.0)

    def test_notes_sharing_one_green_line_keep_its_value_and_their_gap_stays_flat(self):
        # One line governs three notes: osu! scrolls all three at its value.
        # Interpolating line to line sped the middle ones up (real maps: 7.7x).
        times = [1000.0, 1250.0, 1500.0, 2000.0]
        points = [TimingPoint.uninherited_at(0, 120), TimingPoint.inherited_at(990, 1.0),
                  TimingPoint.inherited_at(1990, 2.0)]
        model = build_model(points, [_note(t) for t in times], 0, 3000, ANTI)
        self.assertEqual([model.value_at(t) for t in times], [1.0, 1.0, 1.0, 2.0])
        self.assertEqual(model.value_at(1100), 1.0, "no green line in this gap: no ramp")
        self.assertAlmostEqual(model.value_at(1750), 1.5, msg="the gap where the chart changed speed ramps")

    def test_a_step_run_keeps_the_charts_own_step(self):
        times = [1000.0, 2000.0]
        points = [TimingPoint.uninherited_at(0, 120), TimingPoint.inherited_at(990, 1.0),
                  TimingPoint.inherited_at(1990, 1.5)]
        model = build_model(points, [_note(t) for t in times], 1000, 2000, BARLINE)
        self.assertEqual(model.value_at(1500), 1.0)
        self.assertEqual(model.value_at(1995), 1.5)


if __name__ == "__main__":
    unittest.main()
