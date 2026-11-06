"""`stable_window`: osu! stable's object-window search, derailed by a NaN
slider's int.MinValue end. The cases are worked by hand through .NET's
binary search, not re-derived with the code under test."""
from __future__ import annotations

import unittest

from chart.stable_window import INT_MIN, net_int, unhittable_circles


def _chart(nan_at: int, count: int = 7):
    """Circles 100ms apart, the one at `nan_at` replaced by a NaN slider."""
    return [(100 * i, INT_MIN if i == nan_at else 100 * i, i != nan_at) for i in range(count)]


class StableWindowTests(unittest.TestCase):
    def test_a_cast_of_nan_is_int_min_and_others_truncate(self):
        self.assertEqual(net_int(float("nan")), INT_MIN)
        self.assertEqual(net_int(float("inf")), INT_MIN)
        self.assertEqual(net_int(2.0 ** 31), INT_MIN)
        self.assertEqual(net_int(-2.7), -2)
        self.assertEqual(net_int(2.7), 2)

    def test_a_search_landing_on_it_loses_every_circle_before_it(self):
        # Probe t - 1000 against ends [0, 100, 200, MIN, 400, 500, 600]:
        # mid 3 is the NaN slider, below any probe, so lo = 4 and the window
        # starts past it -- for circles 0, 1 and 2 alike. "The circle before"
        # would have been 2 alone.
        self.assertEqual(unhittable_circles(_chart(3), 1000), {0, 1, 2})

    def test_a_search_never_landing_on_it_loses_nothing(self):
        # Last in the list: every probe settles at 0 before reaching mid 6.
        # "The circle before" would have dropped circle 5, which stable plays.
        self.assertEqual(unhittable_circles(_chart(6), 1000), set())

    def test_no_nan_slider_loses_nothing(self):
        self.assertEqual(unhittable_circles(_chart(-1, 50), 1000), set())


if __name__ == "__main__":
    unittest.main()
