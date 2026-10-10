"""Layer 6's "Only red lines at this BPM" filter.

The SV (barlines) layer owns *every* uninherited point in the difficulty: the
chart's own timing, a barline gimmick's 60000 BPM run, and anything placed by
hand. A dragged Generate range therefore hits all of them at once, which is
wrong the moment a gimmick run is interleaved with ordinary timing lines. The
filter narrows the sweep to an inclusive BPM range ("500-1000 BPM" means every
red line from 500 to 1000); an exact single BPM is just the degenerate case
where the two ends are equal.

Covered here: the filter off (every red line in range), on (only the ones in
range), matching across float drift rather than by ==, both endpoints being
inclusive, the dialog defaulting the low end to the BPM in force where the
drag started, the max clamping up to the min on accept, and the rows hiding
themselves when the checkbox is off.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui import gui
from osu_io.timing import TimingPoint
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


class BpmMatchTests(unittest.TestCase):
    """A red line's BPM is 60000/beat_length, recomputed every time from a
    number that has been through a text file -- so == is the wrong test."""

    def test_exact_values_match(self):
        self.assertTrue(gui.bpm_matches(180.0, 180.0))

    def test_a_last_digit_of_drift_still_matches_at_gimmick_bpm(self):
        # What an authored 60000 and a parsed-then-recomputed one look like.
        drifted = TimingPoint.uninherited_at(0, 60000.0)
        drifted.beat_length += 1e-9
        self.assertNotEqual(drifted.bpm, 60000.0, "the drift has to be real for this to test anything")
        self.assertTrue(gui.bpm_matches(drifted.bpm, 60000.0))

    def test_the_tolerance_is_relative_not_absolute(self):
        # 0.05 apart: inside one part in a million at 60000, far outside it at
        # 180. An absolute epsilon could not tell these two cases apart.
        self.assertTrue(gui.bpm_matches(60000.05, 60000.0))
        self.assertFalse(gui.bpm_matches(180.05, 180.0))

    def test_neighbouring_bpms_do_not_match(self):
        self.assertFalse(gui.bpm_matches(181.0, 180.0))
        self.assertFalse(gui.bpm_matches(60000.0, 66666.0))

    def test_an_inherited_point_has_no_bpm_and_matches_nothing(self):
        self.assertFalse(gui.bpm_matches(None, 180.0))


class BpmRangeMatchTests(unittest.TestCase):
    """bpm_in_range: the inclusive interval that replaced the single-BPM
    filter -- an exact match is just low == high."""

    def test_a_bpm_inside_the_range_matches(self):
        self.assertTrue(gui.bpm_in_range(180.0, 150.0, 200.0))

    def test_both_endpoints_are_inclusive(self):
        self.assertTrue(gui.bpm_in_range(150.0, 150.0, 200.0))
        self.assertTrue(gui.bpm_in_range(200.0, 150.0, 200.0))

    def test_just_outside_either_end_is_excluded(self):
        self.assertFalse(gui.bpm_in_range(149.0, 150.0, 200.0))
        self.assertFalse(gui.bpm_in_range(201.0, 150.0, 200.0))

    def test_float_drift_at_an_endpoint_still_matches(self):
        drifted = TimingPoint.uninherited_at(0, 60000.0)
        drifted.beat_length += 1e-9
        self.assertNotEqual(drifted.bpm, 60000.0, "the drift has to be real for this to test anything")
        self.assertTrue(gui.bpm_in_range(drifted.bpm, 60000.0, 60000.0))
        self.assertTrue(gui.bpm_in_range(drifted.bpm, 1.0, 60000.0))

    def test_low_equal_high_behaves_as_an_exact_match(self):
        self.assertTrue(gui.bpm_in_range(180.0, 180.0, 180.0))
        self.assertFalse(gui.bpm_in_range(181.0, 180.0, 180.0))

    def test_an_inherited_point_has_no_bpm_and_matches_nothing(self):
        self.assertFalse(gui.bpm_in_range(None, 100.0, 200.0))


class _Fixture:
    """A difficulty whose red lines deliberately alternate BPM, so a filtered
    sweep and an unfiltered one cannot agree by accident."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        path = write_fixture(directory, "full_v14")

        self.window = gui.MainWindow()
        self.window._load_map_path(path, refresh_difficulties=True)
        self.state = self.window._states[path]
        self.document = self.state.document
        # The fixture ships red lines at 0 (120 BPM) and 6000 (150 BPM).
        # Everything below 6000 is ours.
        for time_ms, bpm in ((1000, 180.0), (2000, 60000.0), (3000, 180.0), (5000, 60000.0)):
            self.document.timing_points.append(TimingPoint.uninherited_at(time_ms, bpm))

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _times(self, bpm_range: tuple[float, float] | None) -> list[float]:
        return self.window._sv_generation_times(
            self.state, 500, 5500, {"only_red_line_bpm": bpm_range}, "sv_barline",
        )


class GenerationTimeFilterTests(_Fixture, unittest.TestCase):
    def test_filter_off_generates_on_every_red_line_in_range(self):
        self.assertEqual(self._times(None), [1000.0, 2000.0, 3000.0, 5000.0])

    def test_a_missing_key_is_the_same_as_off(self):
        """Every other caller of _sv_generation_times predates the option."""
        times = self.window._sv_generation_times(self.state, 500, 5500, {}, "sv_barline")
        self.assertEqual(times, [1000.0, 2000.0, 3000.0, 5000.0])

    def test_filter_on_keeps_only_the_matching_red_lines(self):
        self.assertEqual(self._times((60000.0, 60000.0)), [2000.0, 5000.0])
        self.assertEqual(self._times((180.0, 180.0)), [1000.0, 3000.0])

    def test_a_bpm_no_red_line_has_generates_nothing(self):
        self.assertEqual(self._times((200.0, 200.0)), [])

    def test_a_true_interval_selects_several_distinct_bpms(self):
        # 180 and 60000 both fall in this interval; 120 (the fixture's own
        # timing at 0 and 6000, outside the 500-5500 range anyway) does not.
        self.assertEqual(self._times((150.0, 70000.0)), [1000.0, 2000.0, 3000.0, 5000.0])

    def test_a_bpm_just_outside_either_end_is_excluded(self):
        self.assertEqual(self._times((180.1, 59999.9)), [])
        self.assertEqual(self._times((180.0, 59999.9)), [1000.0, 3000.0])

    def test_matching_survives_float_drift_in_the_stored_beat_length(self):
        drifted = next(p for p in self.document.timing_points if p.time == 2000)
        drifted.beat_length += 1e-9
        self.assertNotEqual(drifted.bpm, 60000.0)
        self.assertIn(2000.0, self._times((60000.0, 60000.0)))

    def test_the_range_still_bounds_the_filtered_result(self):
        times = self.window._sv_generation_times(
            self.state, 1500, 2500, {"only_red_line_bpm": (60000.0, 60000.0)}, "sv_barline",
        )
        self.assertEqual(times, [2000.0])

    def test_a_filtered_sweep_only_writes_points_at_matching_lines(self):
        """End to end: the sweep itself, not just where it was told to go."""
        self.window._generate_sv(
            self.state.source_path, 500, 5500,
            {
                "function": "linear", "initial_rate": 1.0, "final_rate": 2.0,
                "position_offset": 0, "omit_barline": False,
                "relative_to_final_bpm": False, "snap_divisor": 4,
                "only_red_line_bpm": (60000.0, 60000.0),
            },
            layer_id="sv_barline",
        )
        written = sorted(
            round(point.time) for point in self.document.timing_points
            if point.inherited and 500 <= point.time <= 5500
        )
        # 2000 and 4000 already carried the fixture's own inherited points.
        # The sweep replaces the one at 2000 (a 60000 BPM red line) and adds
        # 5000; 3000 is a 180 BPM line and 4000 is not a red line at all, so
        # neither may gain or lose anything.
        self.assertEqual(written, [2000, 4000, 5000])

    def test_the_sweep_still_reaches_its_final_rate_across_the_survivors(self):
        """Progress is normalised over the points actually generated, so
        filtering must not leave the last one short of the requested rate."""
        self.window._generate_sv(
            self.state.source_path, 500, 5500,
            {
                "function": "linear", "initial_rate": 1.0, "final_rate": 2.0,
                "position_offset": 0, "omit_barline": False,
                "relative_to_final_bpm": False, "snap_divisor": 4,
                "only_red_line_bpm": (60000.0, 60000.0),
            },
            layer_id="sv_barline",
        )
        by_time = {
            round(point.time): point.sv_multiplier
            for point in self.document.timing_points if point.inherited
        }
        self.assertAlmostEqual(by_time[2000], 1.0, places=4)
        self.assertAlmostEqual(by_time[5000], 2.0, places=4)


class DialogTests(unittest.TestCase):
    """The rows themselves: shown only for layer 6, defaulted from the base
    timing, and reported through parameters()."""

    def _base_timing(self) -> list[TimingPoint]:
        return [
            TimingPoint.uninherited_at(0, 120.0),
            TimingPoint.uninherited_at(4000, 175.0),
        ]

    def test_the_default_bpm_is_the_one_in_force_at_the_range_start(self):
        dialog = gui.SVFunctionDialog(
            5000, 6000, gimmick_layer="sv_barline", base_timing=self._base_timing(),
        )
        self.assertAlmostEqual(dialog.bpm_filter_spin.value(), 175.0, places=2)
        self.assertAlmostEqual(dialog.bpm_filter_max_spin.value(), 175.0, places=2)

    def test_a_start_before_the_second_line_gets_the_first_bpm(self):
        dialog = gui.SVFunctionDialog(
            1000, 2000, gimmick_layer="sv_barline", base_timing=self._base_timing(),
        )
        self.assertAlmostEqual(dialog.bpm_filter_spin.value(), 120.0, places=2)

    def test_no_base_timing_falls_back_to_120(self):
        dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer="sv_barline")
        self.assertAlmostEqual(dialog.bpm_filter_spin.value(), 120.0, places=2)

    def test_the_filter_is_off_by_default_and_reports_none(self):
        dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer="sv_barline")
        self.assertFalse(dialog.bpm_filter_check.isChecked())
        self.assertIsNone(dialog.parameters()["only_red_line_bpm"])

    def test_checking_it_reports_the_bpm_interval(self):
        dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer="sv_barline")
        dialog.bpm_filter_check.setChecked(True)
        dialog.bpm_filter_spin.setValue(500.0)
        dialog.bpm_filter_max_spin.setValue(1000.0)
        low, high = dialog.parameters()["only_red_line_bpm"]
        self.assertAlmostEqual(low, 500.0, places=2)
        self.assertAlmostEqual(high, 1000.0, places=2)

    def test_accepting_with_max_below_min_clamps_it_up_rather_than_refusing(self):
        dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer="sv_barline")
        dialog.bpm_filter_check.setChecked(True)
        dialog.bpm_filter_spin.setValue(500.0)
        dialog.bpm_filter_max_spin.setValue(100.0)

        dialog._accept()

        self.assertEqual(dialog.result(), gui.QDialog.Accepted)
        low, high = dialog.parameters()["only_red_line_bpm"]
        self.assertAlmostEqual(low, 500.0, places=2)
        self.assertAlmostEqual(high, 500.0, places=2)

    def test_the_bpm_rows_are_hidden_until_the_filter_is_on(self):
        dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer="sv_barline")
        self.assertFalse(gui.is_row_visible(dialog._form_layout, dialog.bpm_filter_spin))
        self.assertFalse(gui.is_row_visible(dialog._form_layout, dialog.bpm_filter_max_spin))
        dialog.bpm_filter_check.setChecked(True)
        self.assertTrue(gui.is_row_visible(dialog._form_layout, dialog.bpm_filter_spin))
        self.assertTrue(gui.is_row_visible(dialog._form_layout, dialog.bpm_filter_max_spin))
        dialog.bpm_filter_check.setChecked(False)
        self.assertFalse(gui.is_row_visible(dialog._form_layout, dialog.bpm_filter_spin))
        self.assertFalse(gui.is_row_visible(dialog._form_layout, dialog.bpm_filter_max_spin))

    def test_other_layers_never_show_the_filter_and_never_report_it(self):
        for layer in (False, "sv_chart", "sv_fake_slider"):
            with self.subTest(layer=layer):
                dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer=layer)
                self.assertIsNone(dialog.parameters()["only_red_line_bpm"])

    def test_the_preview_is_unaffected_by_the_filter(self):
        """The preview plots the sweep's *shape* over 20 sample dots and has
        never claimed a point count; filtering changes which milliseconds get a
        point, not the rate curve laid across them, so it stays honest."""
        dialog = gui.SVFunctionDialog(
            1000, 2000, gimmick_layer="sv_barline", initial_rate=1.0, final_rate=2.0,
        )
        before = dialog.preview.rates(20)
        dialog.bpm_filter_check.setChecked(True)
        self.assertEqual(dialog.preview.rates(20), before)
        self.assertAlmostEqual(before[0], 1.0, places=4)
        self.assertAlmostEqual(before[-1], 2.0, places=4)


if __name__ == "__main__":
    unittest.main()
