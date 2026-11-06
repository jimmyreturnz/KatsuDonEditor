"""`common.VsyncPacer`, with the two tests it came with in taiko-rs's clock.rs."""
import unittest

from ui.common import VsyncPacer

REFRESH = 1000.0 / 60.0


def paced(acquired):
    pacer = VsyncPacer()
    return [pacer.frame_time(a, 60.0) for a in acquired]


class VsyncPacerTests(unittest.TestCase):
    def test_frames_land_on_the_refresh_grid_however_unevenly_they_wake(self):
        jitter = [0.0, 2.1, -1.4, 2.9, -2.6, 0.7, -0.3, 1.8, -2.9, 2.2]
        acquired = [k * REFRESH + jitter[k % len(jitter)] for k in range(200)]
        times = paced(acquired)
        for a, b in zip(times[1:], times[2:]):
            self.assertLess(abs(b - a - REFRESH), 0.25)
        # And still with the real clock.
        self.assertLess(abs(times[-1] - acquired[-1]), 3.0)

    def test_a_slow_frame_costs_one_refresh_and_the_quick_one_after_it_none(self):
        acquired = [k * REFRESH for k in range(40)]
        # 1.4, not clock.rs's 1.5: exactly half way rounds up in plain floats,
        # where Rust's nanosecond Instants land it just under.
        acquired[10] += 1.4 * REFRESH
        for k in range(11, 40):
            acquired[k] += REFRESH
        acquired[11] = acquired[10] + 1.0
        times = paced(acquired)
        steps = [round((b - a) / REFRESH, 2) for a, b in zip(times, times[1:])]
        self.assertEqual(sum(1 for s in steps if abs(s - 2.0) < 0.05), 1, steps)
        self.assertTrue(all(abs(s - 1.0) < 0.05 or abs(s - 2.0) < 0.05 for s in steps), steps)

    def test_goes_by_the_refresh_it_sees_not_the_one_it_is_told(self):
        # Told 60, woken at 100Hz: after the steps it judges by, on a 10ms grid.
        pacer = VsyncPacer()
        times = [pacer.frame_time(k * 10.0, 60.0) for k in range(100)]
        for a, b in zip(times[60:], times[61:]):
            self.assertLess(abs(b - a - 10.0), 0.25)


if __name__ == "__main__":
    unittest.main()
