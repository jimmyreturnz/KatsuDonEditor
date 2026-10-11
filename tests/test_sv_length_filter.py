"""Layer 5's "Only fake sliders with this length" filter.

Two fake sliders can share the exact millisecond a Generate sweep would land
on and read identically in the editor (both squash a note the same amount)
while differing only in `length` -- a mapper's own encoding trick, e.g.
`-0.001` vs `-0.0011`, to give two structures different SV without a second
position to tell them apart by. The filter narrows the sweep to an inclusive
`length` range, mirroring layer 6's BPM filter (`test_sv_bpm_filter.py`); an
exact single length is just the degenerate case where the two ends are equal.
Defaults to `(-0.001, -0.001)`, the canonical fake slider length.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ui import gui
from chart.gimmick_session import format_length
from model.hit_object import TYPE_SLIDER, HitObject
from tests.osu_fixtures import write_fixture

_APP: QApplication | None = None


def setUpModule() -> None:
    global _APP
    _APP = QApplication.instance() or QApplication([])


def _fake_slider(time_ms: float, length: float) -> HitObject:
    return HitObject(
        x=256, y=192, time=time_ms, type=TYPE_SLIDER, hit_sound=0,
        extras=("L|624:192", "1", format_length(length)),
    )


class _Fixture:
    """A difficulty with three plain fake sliders at different lengths, so a
    filtered sweep and an unfiltered one cannot agree by accident."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        directory = Path(self._temp.name)
        (directory / "audio.mp3").write_bytes(b"\x00")
        path = write_fixture(directory, "full_v14")

        self.window = gui.MainWindow()
        self.window._load_map_path(path, refresh_difficulties=True)
        self.state = self.window._states[path]
        self.document = self.state.document
        self.document.hit_objects = [
            _fake_slider(1000, -0.001), _fake_slider(2000, -0.0011), _fake_slider(3000, -0.001),
        ]
        # The fixture's own green lines would otherwise sit in this range too
        # and confuse an end-to-end "only these milliseconds gained a point"
        # assertion -- this class is about the filter, not the fixture's SV.
        self.document.timing_points = [
            p for p in self.document.timing_points
            if p.uninherited or not (500 <= p.time <= 3500)
        ]
        self.at = {1000: 1000, 2000: 2000, 3000: 3000}

    def tearDown(self) -> None:
        self.window.close()
        self._temp.cleanup()

    def _times(self, length_range: tuple[float, float] | None) -> list[float]:
        return self.window._sv_generation_times(
            self.state, 500, 3500, {"only_fake_slider_length": length_range}, "sv_fake_slider",
        )


class GenerationTimeFilterTests(_Fixture, unittest.TestCase):
    def test_filter_off_generates_on_every_fake_slider_in_range(self):
        self.assertEqual(
            self._times(None),
            [float(self.at[1000]), float(self.at[2000]), float(self.at[3000])],
        )

    def test_a_missing_key_is_the_same_as_off(self):
        times = self.window._sv_generation_times(
            self.state, 500, 3500, {}, "sv_fake_slider",
        )
        self.assertEqual(len(times), 3)

    def test_filter_on_keeps_only_the_matching_lengths(self):
        self.assertEqual(
            self._times((-0.001, -0.001)),
            [float(self.at[1000]), float(self.at[3000])],
        )
        self.assertEqual(self._times((-0.0011, -0.0011)), [float(self.at[2000])])

    def test_a_length_no_fake_slider_has_generates_nothing(self):
        self.assertEqual(self._times((-0.5, -0.5)), [])

    def test_a_true_interval_selects_several_distinct_lengths(self):
        self.assertEqual(
            self._times((-0.0011, -0.001)),
            [float(self.at[1000]), float(self.at[2000]), float(self.at[3000])],
        )

    def test_a_length_just_outside_either_end_is_excluded(self):
        self.assertEqual(self._times((-0.0009, -0.00095)), [])

    def test_the_range_still_bounds_the_filtered_result(self):
        times = self.window._sv_generation_times(
            self.state, 1500, 2500, {"only_fake_slider_length": (-0.0011, -0.0011)}, "sv_fake_slider",
        )
        self.assertEqual(times, [float(self.at[2000])])

    def test_a_filtered_sweep_only_writes_points_at_matching_lengths(self):
        """End to end: the sweep itself, not just where it was told to go."""
        self.window._generate_sv(
            self.state.source_path, 500, 3500,
            {
                "function": "linear", "initial_rate": 1.0, "final_rate": 2.0,
                "position_offset": 0, "omit_barline": False,
                "relative_to_final_bpm": False, "snap_divisor": 4,
                "only_fake_slider_length": (-0.001, -0.001),
            },
            layer_id="sv_fake_slider",
        )
        written = sorted(
            round(point.time) for point in self.document.timing_points
            if point.inherited and 500 <= point.time <= 3500
        )
        self.assertEqual(written, sorted((self.at[1000], self.at[3000])))


class DialogTests(unittest.TestCase):
    """The rows themselves: shown only for layer 5, defaulted to -0.001, and
    reported through parameters()."""

    def test_the_filter_is_off_by_default_and_defaults_to_the_canonical_length(self):
        dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer="sv_fake_slider")
        self.assertFalse(dialog.length_filter_check.isChecked())
        self.assertAlmostEqual(dialog.length_filter_spin.value(), -0.001, places=4)
        self.assertAlmostEqual(dialog.length_filter_max_spin.value(), -0.001, places=4)
        self.assertIsNone(dialog.parameters()["only_fake_slider_length"])

    def test_checking_it_reports_the_length_interval(self):
        dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer="sv_fake_slider")
        dialog.length_filter_check.setChecked(True)
        dialog.length_filter_spin.setValue(-0.002)
        dialog.length_filter_max_spin.setValue(-0.0015)
        low, high = dialog.parameters()["only_fake_slider_length"]
        self.assertAlmostEqual(low, -0.002, places=4)
        self.assertAlmostEqual(high, -0.0015, places=4)

    def test_accepting_with_max_below_min_clamps_it_up_rather_than_refusing(self):
        dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer="sv_fake_slider")
        dialog.length_filter_check.setChecked(True)
        dialog.length_filter_spin.setValue(-0.001)
        dialog.length_filter_max_spin.setValue(-0.002)

        dialog._accept()

        self.assertEqual(dialog.result(), gui.QDialog.Accepted)
        low, high = dialog.parameters()["only_fake_slider_length"]
        self.assertAlmostEqual(low, -0.001, places=4)
        self.assertAlmostEqual(high, -0.001, places=4)

    def test_the_length_rows_are_hidden_until_the_filter_is_on(self):
        dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer="sv_fake_slider")
        self.assertFalse(gui.is_row_visible(dialog._form_layout, dialog.length_filter_spin))
        self.assertFalse(gui.is_row_visible(dialog._form_layout, dialog.length_filter_max_spin))
        dialog.length_filter_check.setChecked(True)
        self.assertTrue(gui.is_row_visible(dialog._form_layout, dialog.length_filter_spin))
        self.assertTrue(gui.is_row_visible(dialog._form_layout, dialog.length_filter_max_spin))
        dialog.length_filter_check.setChecked(False)
        self.assertFalse(gui.is_row_visible(dialog._form_layout, dialog.length_filter_spin))
        self.assertFalse(gui.is_row_visible(dialog._form_layout, dialog.length_filter_max_spin))

    def test_other_layers_never_show_the_filter_and_never_report_it(self):
        for layer in (False, "sv_chart", "sv_barline"):
            with self.subTest(layer=layer):
                dialog = gui.SVFunctionDialog(1000, 2000, gimmick_layer=layer)
                self.assertIsNone(dialog.parameters()["only_fake_slider_length"])

    def test_the_preview_is_unaffected_by_the_filter(self):
        dialog = gui.SVFunctionDialog(
            1000, 2000, gimmick_layer="sv_fake_slider", initial_rate=1.0, final_rate=2.0,
        )
        before = dialog.preview.rates(20)
        dialog.length_filter_check.setChecked(True)
        self.assertEqual(dialog.preview.rates(20), before)


if __name__ == "__main__":
    unittest.main()
