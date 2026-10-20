"""Generate SV across red lines (owner, 2026-10-18).

A red line resets SV to 1.0x. A note on one used to get its green line
`offset` early, so the red line came after it and threw the sweep away.
"""
from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from osu_io.timing import TimingPoint
from chart.sv_curves import sv_ease
from tests.test_editing_ux_round3 import WindowTestCase


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class GenerateSvRedLineTests(WindowTestCase):
    def _params(self, **overrides):
        params = {
            "initial_rate": 1.0, "final_rate": 2.0, "placement": "notes", "snap_divisor": 4,
            "position_offset": -5, "omit_barline": False, "relative_to_final_bpm": False,
            "function": "linear",
        }
        params.update(overrides)
        return params

    def _notes(self, start, end):
        return sorted(n.time for n in self.state.document.hit_objects if start <= n.time <= end)

    def _add_red(self, time_ms, bpm):
        self.state.document.timing_points.append(TimingPoint(float(time_ms), 60000.0 / bpm))
        self.state.document.timing_points.sort(key=lambda p: p.time)

    def _new(self, before):
        return [p for p in self.state.document.timing_points if p.uid not in before]

    def test_a_note_on_a_red_line_gets_its_green_on_the_red(self):
        notes = self._notes(1000, 3000)
        self.assertGreaterEqual(len(notes), 3)
        on_red = notes[1]
        self._add_red(on_red, 150)
        before = {p.uid for p in self.state.document.timing_points}
        self.window._generate_sv(self.path, 1000.0, 3000.0, self._params())
        times = sorted(round(p.time) for p in self._new(before))
        self.assertIn(on_red, times)
        self.assertNotIn(on_red - 5, times)
        self.assertIn(notes[0] - 5, times)
        # After the red line on their shared millisecond, so it is the one read.
        shared = [p for p in self.state.document.timing_points if round(p.time) == on_red]
        self.assertTrue(shared[0].uninherited)
        self.assertFalse(shared[-1].uninherited)

    def test_a_bare_red_line_is_left_alone(self):
        """Owner, 2026-10-18: a red line with no note on it is not a point of
        the sweep, and gets no green line of its own."""
        notes = self._notes(1000, 3000)
        bare = (notes[0] + notes[1]) // 2
        self._add_red(bare, 120)
        before = {p.uid for p in self.state.document.timing_points}
        self.window._generate_sv(self.path, 1000.0, 3000.0, self._params(position_offset=0))
        self.assertNotIn(bare, {round(p.time) for p in self._new(before)})

    def test_flat_writes_one_value(self):
        before = {p.uid for p in self.state.document.timing_points}
        self.window._generate_sv(self.path, 1000.0, 3000.0, self._params(function="flat", initial_rate=1.3))
        values = {round(p.sv_multiplier, 9) for p in self._new(before)}
        self.assertEqual(values, {1.3})


class FlatCurveTests(unittest.TestCase):
    def test_flat_never_leaves_the_start(self):
        self.assertEqual([sv_ease("flat", t / 4) for t in range(5)], [0.0] * 5)


if __name__ == "__main__":
    unittest.main()
